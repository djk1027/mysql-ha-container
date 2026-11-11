import json
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


QUERY_TIMEOUT = 3


#-------------------
# Cluster health (읽기 전용: Manager / 웹 대시보드 공용)
#-------------------
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
        return None, {"split": f"SPLIT-BRAIN: 쓰기 가능한 노드가 여러 개 {primaries}"}

    if len(primaries) != 1:
        # 전체 장애이거나 Replica들의 Source가 엇갈림
        return None, {"primary": f"Primary를 식별할 수 없음 (후보: {primaries})"}

    primary = primaries[0]
    return primary, check_issues(statuses, containers, primary)


def check_issues(statuses, containers, primary):
    """현재 발견된 이상 목록을 {key: message} 형태로 반환한다. 노드별 key는 '<종류>:<node>'."""
    issues = {}

    for node in NODES:
        if node not in statuses:
            state = {True: "실행 중", False: "중지됨", None: "확인 불가"}[containers[node]]
            issues[f"down:{node}"] = f"{node}: MySQL 접속 불가 (컨테이너 {state})"

    for node, s in statuses.items():
        if node == primary:
            if not s["is_primary"]:
                issues[f"ro:{node}"] = f"{node}: Primary 접속은 되지만 쓰기 불가 상태"
            continue

        replica = s["replica"]

        if not s["super_read_only"] and not s["is_primary"]:
            issues[f"writable:{node}"] = f"{node}: Replica가 쓰기 가능 상태"

        if replica is None:
            if not s["is_primary"]:
                issues[f"norepl:{node}"] = f"{node}: 복제가 구성되지 않음"
            continue

        if replica["Source_Host"] != primary:
            issues[f"source:{node}"] = (
                f"{node}: 복제 Source가 {replica['Source_Host']} (예상: {primary})"
            )

        if replica["Replica_IO_Running"] != "Yes":
            issues[f"io:{node}"] = (
                f"{node}: IO Thread 비정상 ({replica['Replica_IO_Running']})"
                f" {replica['Last_IO_Error'] or ''}".rstrip()
            )

        if replica["Replica_SQL_Running"] != "Yes":
            issues[f"sql:{node}"] = (
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
                issues[f"lag:{node}"] = f"{node}: Primary보다 GTID 지연 {behind}"

    return issues
