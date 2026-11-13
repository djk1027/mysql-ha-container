"""수집기: 주기적으로 클러스터 상태를 모아 Store에 넣는다.

- 읽기 전용: 클러스터를 변경하거나 Failover를 판단하지 않는다 (HA Manager의 역할)
- 노드 상태/이상 검사는 apps.common (HA Manager와 같은 로직)
- 이벤트는 HA Manager가 남긴 logs/events.jsonl 을 읽기만 한다 (apps.events)
- CPU/Memory는 `docker stats` 한 번 호출로 전체 컨테이너를 수집
- MySQL 지표는 SHOW GLOBAL STATUS
- 어떤 수집이 실패해도 루프는 계속 돈다 (실패한 값만 None)
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymysql

from apps import events
from apps.common import (
    NODES, QUERY_TIMEOUT, Role, cluster_issues, collect, configuring_nodes, execute,
    find_primary, is_busy_issue, node_state, parse_issue_key,
)

log = logging.getLogger("collector")

# 서버 프로세스가 메모리에 올려 둔 파이썬 코드. 실행 중에 이 파일들이 바뀌면
# 재시작 전까지는 예전 코드로 돌기 때문에, 새 정적 파일(JS)과 데이터 형식이 어긋날 수 있다.
_APPS_DIR = Path(__file__).resolve().parent.parent
SERVER_CODE = [
    *sorted((_APPS_DIR / "web").glob("*.py")),
    _APPS_DIR / "state.py",
    _APPS_DIR / "common.py",
    _APPS_DIR / "events.py",
]


def _code_mtime():
    return max((f.stat().st_mtime for f in SERVER_CODE if f.exists()), default=0)


def _alert(key, message, candidates):
    """대시보드 상단 알림 1건. 노드별 이슈는 해당 노드, 클러스터 전체 이슈(split, primary)는 관련 노드(Primary 후보)"""
    kind, node = parse_issue_key(key)
    if node:
        nodes = [node]
        if message.startswith(f"{node}: "):
            message = message[len(node) + 2:]   # 인스턴스는 별도 칸에 표시하므로 앞의 "node: " 제거
    else:
        nodes = candidates or list(NODES)       # 후보가 없으면(전체 장애) 모든 노드
    return {"level": kind.severity, "nodes": nodes, "message": message}

_UNITS = {
    "b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
}


def _to_bytes(text):
    """'561.4MiB' → 588673433"""
    m = re.fullmatch(r"\s*([\d.]+)\s*([a-zA-Z]+)\s*", text or "")
    if not m:
        return None
    return int(float(m.group(1)) * _UNITS.get(m.group(2).lower(), 1))


def _to_float(text):
    try:
        return float(text.strip().rstrip("%"))
    except (AttributeError, ValueError):
        return None


def _docker_ncpu():
    """Docker 호스트의 CPU 코어 수."""
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{.NCPU}}"],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout
        return int(out.strip())
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        return os.cpu_count() or 1


HOST_CPUS = _docker_ncpu()


def resource_limits():
    """컨테이너별 자원 한도.

    cpus       : CPU 한도(코어 수). 제한이 없으면 호스트 전체 코어 수
    mem_limited: compose의 mem_limit 설정 여부 (없으면 docker stats 한도 = 호스트 전체 메모리)
    """
    limits = {node: {"cpus": HOST_CPUS, "mem_limited": False} for node in NODES}
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f",
             "{{.Name}} {{.HostConfig.NanoCpus}} {{.HostConfig.CpuQuota}} "
             "{{.HostConfig.CpuPeriod}} {{.HostConfig.Memory}}",
             *NODES],
            capture_output=True, text=True, timeout=10,
        ).stdout   # 일부 컨테이너가 없어도 나머지는 출력되므로 check 하지 않음
    except (subprocess.SubprocessError, FileNotFoundError):
        return limits

    for line in out.splitlines():
        name, nano, quota, period, memory = line.split()
        name = name.lstrip("/")
        if name not in limits:
            continue
        if int(nano) > 0:                       # compose `cpus: 2`
            limits[name]["cpus"] = int(nano) / 1e9
        elif int(quota) > 0 and int(period) > 0:  # --cpu-quota/--cpu-period
            limits[name]["cpus"] = int(quota) / int(period)
        limits[name]["mem_limited"] = int(memory) > 0   # compose `mem_limit: 1g`
    return limits


def docker_stats():
    """모든 노드 컨테이너의 CPU/Memory. 실패하면 빈 dict.

    docker stats의 CPUPerc는 "코어 1개 = 100%" 기준이라 여러 코어를 쓰면 100%를 넘는다.
    → 할당 가능한 코어 수로 나눠서 0~100% 로 정규화한다.
    """
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--all", "--format", "{{json .}}"],
            check=True, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        log.warning("docker stats 실패: %s", exc)
        return {}

    limits = resource_limits()
    stats = {}
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("Name") not in NODES:
            continue

        used, _, limit = row.get("MemUsage", "").partition("/")

        # 중지된 컨테이너는 "0B / 0B"로 나온다 → 0이 아니라 "값 없음"으로 처리
        if not _to_bytes(limit):
            continue

        raw_cpu = _to_float(row.get("CPUPerc"))
        cores = limits[row["Name"]]["cpus"]

        stats[row["Name"]] = {
            "cpu": None if raw_cpu is None else round(min(100.0, raw_cpu / cores), 2),
            "cpu_cores_used": None if raw_cpu is None else round(raw_cpu / 100, 2),
            "cpu_cores": cores,
            "mem_used": _to_bytes(used),
            "mem_limit": _to_bytes(limit),
            "mem_pct": _to_float(row.get("MemPerc")),
            "mem_limited": limits[row["Name"]]["mem_limited"],
        }
    return stats


def mysql_status(node):
    """연결 수, 누적 쿼리 수, 가동 시간."""
    rows = execute(
        node,
        """
        SHOW GLOBAL STATUS
        WHERE Variable_name IN ('Threads_connected', 'Questions', 'Uptime')
        """,
        read_timeout=QUERY_TIMEOUT,
    )
    values = {r["Variable_name"]: int(r["Value"]) for r in rows}
    return values


EVENTS_LIMIT = 200        # 화면으로 넘길 최근 이벤트 수
PROCESS_LIMIT = 100       # 노드당 최대 세션 수 (화면/전송량 보호)
SQL_TEXT_LIMIT = 4000     # SQL 원문 최대 길이 (넘으면 잘리고 info_truncated=True)
SYSTEM_USERS = {"system user", "event_scheduler"}
SYSTEM_COMMANDS = {"Daemon", "Binlog Dump", "Binlog Dump GTID"}
# 대시보드 조회 쿼리 표식. 다른 대시보드 인스턴스의 processlist 조회도 목록에서 제외하기 위함
DASHBOARD_TAG = "/* mysql-ha-dashboard */"


def processlist(node):
    """SHOW FULL PROCESSLIST 와 같은 내용. 대시보드(자신 + 다른 인스턴스)의 조회 세션은 제외."""
    rows = execute(
        node,
        DASHBOARD_TAG + """
        SELECT ID, USER, HOST, DB, COMMAND, TIME, STATE,
               LEFT(INFO, %s) AS INFO, CHAR_LENGTH(INFO) AS INFO_LEN
        FROM performance_schema.processlist
        WHERE ID <> CONNECTION_ID()
        ORDER BY TIME DESC
        LIMIT %s
        """,
        (SQL_TEXT_LIMIT, PROCESS_LIMIT),
        read_timeout=QUERY_TIMEOUT,
    )
    return [
        {
            "id": r["ID"],
            "user": r["USER"],
            "host": r["HOST"],
            "db": r["DB"],
            "command": r["COMMAND"],
            "time": r["TIME"],
            "state": r["STATE"],
            "info": r["INFO"],
            "info_truncated": (r["INFO_LEN"] or 0) > SQL_TEXT_LIMIT,
            # 복제 스레드, 이벤트 스케줄러, binlog 전송 등 MySQL 내부 스레드
            "system": r["USER"] in SYSTEM_USERS or r["COMMAND"] in SYSTEM_COMMANDS,
        }
        for r in rows
        if DASHBOARD_TAG not in (r["INFO"] or "")
    ]


class Collector(threading.Thread):
    def __init__(self, store, interval):
        super().__init__(name="collector", daemon=True)  # 메인 종료 시 함께 종료
        self.store = store
        self.interval = interval
        self._prev_questions = {}   # QPS 계산용: node → (Questions, Uptime, time)
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._started_code_mtime = _code_mtime()
        self._alert_since = {}      # 이슈 key → 처음 감지된 시각. 해소되면 삭제 (서버 재시작 시 초기화)

    def run(self):
        log.info("수집 시작 (주기=%s초)", self.interval)
        while True:
            started = time.monotonic()
            try:
                self.store.push_snapshot(self.collect_once())
            except Exception:  # 수집 루프는 절대 죽지 않게
                log.exception("수집 중 오류")
            time.sleep(max(0, self.interval - (time.monotonic() - started)))

    def collect_once(self):
        # docker stats(1~2초 소요)는 노드 상태 수집과 병렬로 실행
        stats_future = self._pool.submit(docker_stats)

        statuses, containers = collect()
        primary, issues = cluster_issues(statuses, containers)

        now = time.time()
        # 구성 작업(Failover/Rejoin) 중인 노드의 이슈는 일시적이므로 알림에서 뺀다 (노드는 CONFIGURING 표시)
        busy = configuring_nodes()
        alert_issues = {k: m for k, m in issues.items() if not is_busy_issue(k, busy)}
        self._alert_since = {k: self._alert_since.get(k, now) for k in alert_issues}

        stats = stats_future.result()
        self.store.set_events(events.recent(EVENTS_LIMIT))

        nodes = {}
        for node in NODES:
            nodes[node] = self._node_snapshot(
                node, statuses.get(node), containers.get(node),
                stats.get(node, {}), issues,
            )
            nodes[node]["state"] = node_state(node, statuses, containers, issues, busy)
            nodes[node]["configuring"] = busy.get(node)   # {"op", "since"} 또는 None

        return {
            "ts": now,
            "interval": self.interval,
            "primary": primary,
            "code_changed": _code_mtime() > self._started_code_mtime,
            "issues": list(issues.values()),
            "alerts": [
                {**_alert(k, m, find_primary(statuses)), "since": self._alert_since[k]}
                for k, m in alert_issues.items()
            ],
            "nodes": nodes,
        }

    def _node_snapshot(self, node, status, container, stats, issues):
        snap = {
            "name": node,
            "port": NODES[node],
            "container": container,          # True / False / None(확인 불가)
            "reachable": status is not None,
            "role": None,
            "read_only": None,
            "super_read_only": None,
            "replica": None,
            "connections": None,
            "qps": None,
            "uptime": None,
            "cpu": stats.get("cpu"),                  # 할당 CPU 대비 % (0~100)
            "cpu_cores_used": stats.get("cpu_cores_used"),
            "cpu_cores": stats.get("cpu_cores"),
            "mem_used": stats.get("mem_used"),
            "mem_limit": stats.get("mem_limit"),
            "mem_pct": stats.get("mem_pct"),          # 한도 대비 % (0~100)
            "mem_limited": stats.get("mem_limited"),
            "issues": [m for k, m in issues.items() if k.endswith(f":{node}")],
            "processes": None,                # None = 조회 실패/접속 불가
        }

        if status is None:
            self._prev_questions.pop(node, None)
            return snap

        snap["role"] = Role.PRIMARY if status["is_primary"] else Role.REPLICA
        snap["read_only"] = status["read_only"]
        snap["super_read_only"] = status["super_read_only"]

        r = status["replica"]
        if r:
            snap["replica"] = {
                "source": r["Source_Host"],
                "io": r["Replica_IO_Running"],
                "sql": r["Replica_SQL_Running"],
                "seconds_behind": r["Seconds_Behind_Source"],
                "last_error": r["Last_IO_Error"] or r["Last_SQL_Error"] or None,
            }

        try:
            g = mysql_status(node)
        except pymysql.MySQLError:
            return snap

        snap["connections"] = g.get("Threads_connected")
        snap["uptime"] = g.get("Uptime")
        snap["qps"] = self._qps(node, g)

        try:
            snap["processes"] = processlist(node)
        except pymysql.MySQLError as exc:
            log.warning("%s: processlist 조회 실패: %s", node, exc)
        return snap

    def _qps(self, node, g):
        """Questions 누적값의 차이 / 경과 시간. 재시작(Uptime 감소) 시 리셋."""
        now = time.monotonic()
        questions, uptime = g.get("Questions"), g.get("Uptime")
        prev = self._prev_questions.get(node)
        self._prev_questions[node] = (questions, uptime, now)

        if not prev or questions is None or uptime < prev[1]:
            return None

        elapsed = now - prev[2]
        if elapsed <= 0:
            return None
        return round(max(0, questions - prev[0]) / elapsed, 1)
