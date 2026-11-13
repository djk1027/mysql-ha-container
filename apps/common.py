import json
import time
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

import os
import subprocess
import pymysql

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

COMPOSE_FILE = PROJECT_ROOT / "compose.yaml"
CONFIG_FILE = PROJECT_ROOT / "state/nodes.json"

load_dotenv(PROJECT_ROOT / ".env")

ROOT_PASSWORD = os.environ["MYSQL_ROOT_PASSWORD"]
REPL_PASSWORD = os.environ["MYSQL_REPL_PASSWORD"]

with open(CONFIG_FILE, encoding="utf-8") as f:
    NODES = json.load(f)


#-------------------
# Enums
#-------------------
# StrEnum: 값이 문자열이라 로그/JSON에 그대로 읽히는 이름으로 남고, 기존 문자열과도 == 비교된다
class Role(StrEnum):
    """노드 역할"""
    PRIMARY = "PRIMARY"
    REPLICA = "REPLICA"


class NodeState(StrEnum):
    """노드 상태. 저장하지 않고 매 수집마다 node_state()로 계산한다"""
    OK = "OK"                     # 정상
    DEGRADED = "DEGRADED"         # 서비스는 되지만 확인 필요 (지연, 설정 이상 등)
    REPL_BROKEN = "REPL_BROKEN"   # 접속은 되지만 복제가 중단/미구성
    DOWN = "DOWN"                 # MySQL 접속 불가 (컨테이너 실행 중 또는 확인 불가)
    STOPPED = "STOPPED"           # 컨테이너 중지됨 (Fencing 포함)
    CONFIGURING = "CONFIGURING"   # Failover/Rejoin이 노드를 변경 중 (일시적인 다운/복제 끊김은 정상)


class Severity(StrEnum):
    """알림 심각도"""
    WARNING = "WARNING"     # 서비스는 되지만 확인 필요
    CRITICAL = "CRITICAL"   # 서비스(쓰기) 불가, 노드 다운, 복제 중단 → 즉시 조치 필요


class Mode(StrEnum):
    """Primary 전환 모드"""
    SWITCHOVER = "switchover"   # 기존 Primary 정상: 쓰기 차단 후 계획 전환
    FAILOVER = "failover"       # 기존 Primary 장애: 격리(컨테이너 중지) 후 전환


class IssueKind(StrEnum):
    """이상 종류. 이슈 key는 노드별이면 '<종류>:<node>', 클러스터 전체면 '<종류>'"""
    SPLIT_BRAIN = "split"         # 쓰기 가능한 노드가 여러 개
    NO_PRIMARY = "primary"        # Primary 식별 불가
    PRIMARY_READ_ONLY = "ro"      # Primary 접속은 되지만 쓰기 불가
    DOWN = "down"                 # MySQL 접속 불가
    REPLICA_WRITABLE = "writable" # Replica가 쓰기 가능
    NO_REPLICATION = "norepl"     # 복제 미구성
    WRONG_SOURCE = "source"       # 복제 Source가 현재 Primary가 아님
    IO_STOPPED = "io"             # IO Thread 비정상
    SQL_STOPPED = "sql"           # SQL Thread 비정상
    LAG = "lag"                   # GTID 지연

    @property
    def severity(self):
        return Severity.CRITICAL if self in _CRITICAL_ISSUES else Severity.WARNING


_CRITICAL_ISSUES = {
    IssueKind.SPLIT_BRAIN, IssueKind.NO_PRIMARY, IssueKind.PRIMARY_READ_ONLY,
    IssueKind.DOWN, IssueKind.IO_STOPPED, IssueKind.SQL_STOPPED,
}


def issue_key(kind, node=None):
    return f"{kind}:{node}" if node else str(kind)


def parse_issue_key(key):
    """'io:mysql-server-2' → (IssueKind.IO_STOPPED, 'mysql-server-2'), 'split' → (IssueKind.SPLIT_BRAIN, None)"""
    kind, _, node = key.partition(":")
    return IssueKind(kind), node or None

#-------------------
# Container function
#-------------------
def stop_container(node):
    """Docker 컨테이너를 중지한다."""

    print(f"    [INFO] Stopping container: {node}")

    subprocess.run(
        ["docker", "stop", "-t", "10", node],
        check=True,
        capture_output=True,
        text=True
    )

    if check_container(node) != "false":
        raise RuntimeError(f"Failed to stop container: {node}")

def check_container(node):
    """Docker 컨테이너 실행 상태를 반환한다."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", node],
        check=True,
        capture_output=True,
        text=True
    )

    return result.stdout.strip()

#-------------------
# MySQL function
#-------------------
def execute(node, sql, params=None, read_timeout=45):
    """MySQL 쿼리를 실행하고 결과를 반환한다."""
    conn = pymysql.connect(
        host="127.0.0.1",
        port=NODES[node],
        user="root",
        password=ROOT_PASSWORD,
        autocommit=True,
        connect_timeout=3,
        read_timeout=read_timeout,
        cursorclass=pymysql.cursors.DictCursor,
    )

    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description else ()
    finally:
        conn.close()


def one(node, sql, params=None, read_timeout=45):
    rows = execute(node, sql, params, read_timeout)
    return rows[0] if rows else None


def get_node_status(node, read_timeout=45):
    """각 Node들의 정보를 조회한다."""
    variables = one(node, """
        SELECT
            @@GLOBAL.server_id AS server_id,
            @@GLOBAL.read_only AS read_only,
            @@GLOBAL.super_read_only AS super_read_only,
            @@GLOBAL.gtid_mode AS gtid_mode,
            @@GLOBAL.gtid_executed AS gtid_executed
    """, read_timeout=read_timeout)

    replica = one(node, "SHOW REPLICA STATUS", read_timeout=read_timeout)

    # Primary 판별 조건
    # 1. SHOW REPLICA STATUS 결과가 없음
    # 2. read_only == 0
    # 3. super_read_only == 0
    is_primary = (
        replica is None
        and variables["read_only"] == 0
        and variables["super_read_only"] == 0
    )

    return {
        "name": node,
        "server_id": variables["server_id"],
        "read_only": variables["read_only"],
        "super_read_only": variables["super_read_only"],
        "gtid_mode": variables["gtid_mode"],
        "gtid_executed": variables["gtid_executed"],
        "replica": replica,
        "is_primary": is_primary
    }



#-------------------
# Cluster health (읽기 전용: Manager / 웹 대시보드 공용)
#-------------------
QUERY_TIMEOUT = 3
_REPL_BROKEN_ISSUES = {IssueKind.IO_STOPPED, IssueKind.SQL_STOPPED, IssueKind.NO_REPLICATION}

def node_state(node, statuses, containers, issues, busy=()):
    """수집 결과(collect, cluster_issues)로 노드 상태를 계산한다. busy: configuring_nodes() 결과"""
    if node in busy:
        return NodeState.CONFIGURING
    if node not in statuses:
        return NodeState.STOPPED if containers.get(node) is False else NodeState.DOWN

    kinds = {kind for kind, n in map(parse_issue_key, issues) if n == node}
    if kinds & _REPL_BROKEN_ISSUES:
        return NodeState.REPL_BROKEN
    if kinds:
        return NodeState.DEGRADED
    return NodeState.OK

def container_running(node):
    """컨테이너 실행 여부. docker 조회 자체가 실패하면 None(판단 불가)."""
    try:
        return check_container(node) == "true"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def collect():
    """모든 노드의 상태를 수집한다. 접속 실패 노드는 statuses에서 빠진다."""
    statuses = {}
    containers = {}

    for node in NODES:
        containers[node] = container_running(node)
        try:
            statuses[node] = get_node_status(node, read_timeout=QUERY_TIMEOUT)
        except pymysql.MySQLError:
            pass

    return statuses, containers


def find_primary(statuses):
    """Primary 후보 목록.

    쓰기 가능한 노드가 있으면 그 노드들, 없으면 Replica들의 Source_Host 합의로 추정한다.
    """
    writable = [n for n, s in statuses.items() if s["is_primary"]]
    if writable:
        return writable

    sources = {
        s["replica"]["Source_Host"]
        for s in statuses.values()
        if s["replica"] and s["replica"]["Source_Host"]
    }
    return list(sources)


def cluster_issues(statuses, containers):
    """Primary 식별부터 노드별 검사까지. (primary 또는 None, {key: message})"""
    primaries = find_primary(statuses)

    # Split-brain: 쓰기 가능한 노드가 2개 이상
    if len([n for n in primaries if statuses.get(n, {}).get("is_primary")]) > 1:
        return None, {issue_key(IssueKind.SPLIT_BRAIN): f"SPLIT-BRAIN: 쓰기 가능한 노드가 여러 개 {primaries}"}

    if len(primaries) != 1:
        # 전체 장애이거나 Replica들의 Source가 엇갈림
        return None, {issue_key(IssueKind.NO_PRIMARY): f"Primary를 식별할 수 없음 (후보: {primaries})"}

    primary = primaries[0]
    return primary, check_issues(statuses, containers, primary)


def check_issues(statuses, containers, primary):
    """현재 발견된 이상 목록을 {key: message} 형태로 반환한다. key 형식은 issue_key() 참고."""
    issues = {}

    for node in NODES:
        if node not in statuses:
            state = {True: "실행 중", False: "중지됨", None: "확인 불가"}[containers[node]]
            issues[issue_key(IssueKind.DOWN, node)] = f"{node}: MySQL 접속 불가 (컨테이너 {state})"

    for node, s in statuses.items():
        if node == primary:
            if not s["is_primary"]:
                issues[issue_key(IssueKind.PRIMARY_READ_ONLY, node)] = f"{node}: Primary 접속은 되지만 쓰기 불가 상태"
            continue

        replica = s["replica"]

        if not s["super_read_only"] and not s["is_primary"]:
            issues[issue_key(IssueKind.REPLICA_WRITABLE, node)] = f"{node}: Replica가 쓰기 가능 상태"

        if replica is None:
            if not s["is_primary"]:
                issues[issue_key(IssueKind.NO_REPLICATION, node)] = f"{node}: 복제가 구성되지 않음"
            continue

        if replica["Source_Host"] != primary:
            issues[issue_key(IssueKind.WRONG_SOURCE, node)] = (
                f"{node}: 복제 Source가 {replica['Source_Host']} (예상: {primary})"
            )

        if replica["Replica_IO_Running"] != "Yes":
            issues[issue_key(IssueKind.IO_STOPPED, node)] = (
                f"{node}: IO Thread 비정상 ({replica['Replica_IO_Running']})"
                f" {replica['Last_IO_Error'] or ''}".rstrip()
            )

        if replica["Replica_SQL_Running"] != "Yes":
            issues[issue_key(IssueKind.SQL_STOPPED, node)] = (
                f"{node}: SQL Thread 비정상 ({replica['Replica_SQL_Running']})"
                f" {replica['Last_SQL_Error'] or ''}".rstrip()
            )

        # GTID 지연 (Seconds_Behind_Source 보다 신뢰도 높음)
        if primary in statuses:
            try:
                behind = one(
                    node,
                    "SELECT GTID_SUBTRACT(%s, %s) AS behind",
                    (statuses[primary]["gtid_executed"], s["gtid_executed"]),
                    read_timeout=QUERY_TIMEOUT,
                )["behind"]
            except pymysql.MySQLError:
                behind = None   # 수집 직후 노드가 끊긴 경우. 다음 주기에 다시 확인
            if behind:
                issues[issue_key(IssueKind.LAG, node)] = f"{node}: Primary보다 GTID 지연 {behind}"

    return issues


#-------------------
# 구성 작업 표시 (Failover / Rejoin 진행 중)
#-------------------
# 작업 중인 노드마다 state/ops/<node>.json 을 남긴다. 대시보드와 Manager가 읽어서
# 그 노드를 CONFIGURING 으로 표시하고, 작업 중 생기는 일시적인 이상은 알리지 않는다.
# 수동 실행(python -m apps.failover / apps.rejoin)에도 똑같이 적용된다.
OPS_DIR = PROJECT_ROOT / "state" / "ops"
OPS_MAX_AGE = 30 * 60   # 이보다 오래된 표시는 비정상 종료로 남은 것으로 보고 무시


@contextmanager
def configuring(nodes, op):
    """with 블록 동안 nodes 를 CONFIGURING 으로 표시한다. (예외가 나도 표시는 지워짐)"""
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    info = json.dumps({"op": op, "pid": os.getpid(), "since": time.time()})
    marked = []
    try:
        for node in nodes:
            path = OPS_DIR / f"{node}.json"
            path.write_text(info, encoding="utf-8")
            marked.append(path)
        yield
    finally:
        for path in marked:
            path.unlink(missing_ok=True)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:   # 다른 사용자 프로세스: 살아 있음
        pass
    return True


def configuring_nodes():
    """{node: {"op": "failover"|"rejoin", "since": ts}}. 작업 프로세스가 죽었거나 오래된 표시는 지운다."""
    busy = {}
    if not OPS_DIR.is_dir():
        return busy
    for path in OPS_DIR.glob("*.json"):
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue   # 쓰는 중이거나 방금 지워진 파일
        if not _pid_alive(info.get("pid", -1)) or time.time() - info.get("since", 0) > OPS_MAX_AGE:
            path.unlink(missing_ok=True)
            continue
        busy[path.stem] = {"op": info.get("op"), "since": info.get("since")}
    return busy


def issue_nodes(key):
    """이슈와 관련된 노드. 클러스터 전체 이슈(split, primary)는 모든 노드"""
    _, node = parse_issue_key(key)
    return [node] if node else list(NODES)


def is_busy_issue(key, busy):
    """관련 노드가 모두 구성 작업 중이면 True (작업 중 일시적인 이상으로 보고 알리지 않음)"""
    return bool(busy) and all(n in busy for n in issue_nodes(key))
