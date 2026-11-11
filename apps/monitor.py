#!/usr/bin/env python3
"""MySQL HA 클러스터 상시 감시.

주기적으로 각 노드 상태를 수집하여 이상을 감지하고,
Primary 장애가 확정되면 알림(기본) 또는 자동 Failover(--auto-failover)를 수행한다.

Primary 장애 확정 조건 (split-brain 방지):
  1. Primary MySQL 접속이 연속 --threshold 회 실패
  2. 교차 확인: 컨테이너가 중지되었거나,
     접속 가능한 모든 Replica의 IO Thread도 Primary를 잃은 상태
     (Replica 중 하나라도 IO=Yes 이면 모니터 쪽 네트워크 문제로 판단하고 보류)
"""

import argparse
import json
import logging
import os
import subprocess
import time
import urllib.request

import pymysql

from apps.common import NODES, check_container, get_node_status, one

log = logging.getLogger("monitor")

WEBHOOK_URL = os.environ.get("MONITOR_WEBHOOK_URL")
QUERY_TIMEOUT = 3


#-------------------
# Alert
#-------------------
def notify(level, message):
    """로그 출력 + (설정 시) webhook 전송."""
    log.log(level, message)

    if not WEBHOOK_URL:
        return

    try:
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=json.dumps({"text": f"[mysql-ha] {message}"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:  # 알림 실패가 감시를 멈추면 안 됨
        log.warning("Webhook 전송 실패: %s", exc)


#-------------------
# Collect
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
    """Primary 식별.

    쓰기 가능한 노드가 있으면 그 노드, 없으면 Replica들의 Source_Host 합의로 추정한다.
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


#-------------------
# Check
#-------------------
def check_issues(statuses, containers, primary):
    """현재 발견된 이상 목록을 {key: message} 형태로 반환한다."""
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
            behind = one(
                node,
                "SELECT GTID_SUBTRACT(%s, %s) AS behind",
                (statuses[primary]["gtid_executed"], s["gtid_executed"]),
                read_timeout=QUERY_TIMEOUT,
            )["behind"]
            if behind:
                issues[f"lag:{node}"] = f"{node}: Primary보다 GTID 지연 {behind}"

    return issues


def primary_down_confirmed(primary, statuses, containers):
    """교차 확인으로 Primary 장애를 확정한다. (확정 여부, 사유)"""
    if containers.get(primary) is False:
        return True, "컨테이너 중지됨"

    replicas = [s for n, s in statuses.items() if n != primary and s["replica"]]
    if not replicas:
        return False, "교차 확인할 Replica에 접속 불가"

    still_connected = [
        s["name"] for s in replicas
        if s["replica"]["Replica_IO_Running"] == "Yes"
    ]
    if still_connected:
        return False, (
            f"Primary와 연결 유지 중인 Replica 있음: {', '.join(still_connected)}"
            " (모니터 쪽 네트워크 문제 가능성)"
        )

    return True, "모든 Replica가 Primary 연결을 잃음"


#-------------------
# Main loop
#-------------------
class Monitor:
    def __init__(self, threshold, cooldown, auto_failover, on_event=None):
        self.threshold = threshold
        self.cooldown = cooldown
        self.auto_failover = auto_failover

        self.active_issues = {}
        self.primary = None
        self.primary_failures = 0
        self.last_failover = 0.0

        # 외부(웹 대시보드 등)에서 쓰는 hook
        self.on_event = on_event      # 알림 발생 시 호출: on_event(level, message)
        self.last_statuses = {}       # 마지막 tick에서 수집한 노드 상태
        self.last_containers = {}     # 마지막 tick에서 수집한 컨테이너 실행 여부

    def emit(self, level, message):
        """알림을 보내고, hook이 있으면 함께 전달한다."""
        notify(level, message)
        if self.on_event:
            self.on_event(level, message)

    def report(self, issues):
        """상태가 바뀐 이슈만 알린다. (같은 알림 반복 방지)"""
        for key, msg in issues.items():
            if key not in self.active_issues:
                self.emit(logging.WARNING, f"[Triggered] {msg}")

        for key, msg in self.active_issues.items():
            if key not in issues:
                self.emit(logging.INFO, f"[Recovered] {msg}")

        self.active_issues = issues

    def tick(self):
        statuses, containers = collect()
        self.last_statuses, self.last_containers = statuses, containers
        primaries = find_primary(statuses)

        # Split-brain: 쓰기 가능한 노드가 2개 이상
        if len([n for n in primaries if statuses.get(n, {}).get("is_primary")]) > 1:
            self.report({"split": f"SPLIT-BRAIN: 쓰기 가능한 노드가 여러 개 {primaries}"})
            self.primary_failures = 0
            return

        if len(primaries) != 1:
            # 전체 장애이거나 Replica들의 Source가 엇갈림
            self.report({"primary": f"Primary를 식별할 수 없음 (후보: {primaries})"})
            self.primary_failures = 0
            return

        primary = primaries[0]
        if primary != self.primary:
            if self.primary is not None:
                self.emit(logging.WARNING, f"Primary 변경됨: {self.primary} -> {primary}")
            self.primary = primary
            self.primary_failures = 0

        self.report(check_issues(statuses, containers, primary))

        # Primary 장애 판정
        if primary in statuses:
            self.primary_failures = 0
            return

        self.primary_failures += 1
        log.info("Primary %s 접속 실패 (%d/%d)",
                 primary, self.primary_failures, self.threshold)

        if self.primary_failures < self.threshold:
            return

        confirmed, reason = primary_down_confirmed(primary, statuses, containers)
        if not confirmed:
            log.info("Primary 장애 확정 보류: %s", reason)
            return

        self.handle_primary_down(primary, reason)

    def handle_primary_down(self, primary, reason):
        if not self.auto_failover:
            # 같은 장애로 매 tick 알림이 쏟아지지 않도록 한 번만
            if self.primary_failures == self.threshold:
                self.emit(logging.CRITICAL,
                       f"PRIMARY 장애: {primary} ({reason}). "
                       "수동 실행 필요: python -m apps.failover")
            return

        remaining = self.cooldown - (time.monotonic() - self.last_failover)
        if remaining > 0:
            log.warning("Failover 쿨다운 중: %.0f초 남음", remaining)
            return

        self.emit(logging.CRITICAL, f"PRIMARY 장애: {primary} ({reason}). Failover를 시작합니다...")
        self.last_failover = time.monotonic()

        from apps.failover import failover  # 필요할 때만 로드

        try:
            failover()
        except Exception as exc:
            self.emit(logging.CRITICAL, f"Failover 실패: {exc}. 수동 조치가 필요합니다.")
            return

        self.emit(logging.WARNING,
               f"Failover 완료. Old Primary {primary}는 격리됨. "
               f"재합류: python -m apps.rejoin --node {primary}")
        self.primary_failures = 0

    def run(self, interval):
        log.info("감시 시작: %s (주기=%s초, 임계치=%d회, 자동 Failover=%s)",
                 ", ".join(NODES), interval, self.threshold, self.auto_failover)
        while True:
            started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:  # 감시 루프는 절대 죽지 않게
                log.exception("감시 중 오류 발생: %s", exc)
            time.sleep(max(0, interval - (time.monotonic() - started)))


def main():
    parser = argparse.ArgumentParser(description="MySQL HA 클러스터 감시")

    parser.add_argument("--interval", type=float, default=5, help="감시 주기(초)")
    
    parser.add_argument("--threshold", type=int, default=3,
                        help="Primary 장애 확정까지 연속 실패 횟수")
    
    parser.add_argument("--cooldown", type=float, default=300,
                        help="자동 Failover 후 재시도 금지 시간(초)")
    
    parser.add_argument("--auto-failover", action="store_true",
                        help="Primary 장애 확정 시 자동으로 Failover 수행")
    
    parser.add_argument("--once", action="store_true", help="한 번만 검사하고 종료")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    monitor = Monitor(args.threshold, args.cooldown, args.auto_failover)

    if args.once:
        monitor.tick()
        return 1 if monitor.active_issues else 0

    try:
        monitor.run(args.interval)
    except KeyboardInterrupt:
        log.info("감시를 종료합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
