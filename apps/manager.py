#!/usr/bin/env python3
"""MySQL HA Manager (상주 데몬).

클러스터를 주기적으로 감시하고, Primary 장애가 확정되면
알림(기본) 또는 자동 Failover(--auto-failover)를 수행한다.

알림은 로그 + 이벤트 파일(logs/events.jsonl, 웹 대시보드가 읽음)에 남긴다.
Manager는 하나만 실행할 것 (여러 개면 동시에 Failover를 시도할 수 있음).

Primary 장애 확정 조건 (split-brain 방지):
  1. Primary MySQL 접속이 연속 --threshold 회 실패
  2. 교차 확인: 컨테이너가 중지되었거나,
     접속 가능한 모든 Replica의 IO Thread도 Primary를 잃은 상태
     (Replica 중 하나라도 IO=Yes 이면 Manager 쪽 네트워크 문제로 판단하고 보류)
"""

import argparse
import logging
import signal
import threading
import time

from apps import events
from apps.common import NODES, cluster_issues, collect

log = logging.getLogger("manager")


#-------------------
# Failure decision
#-------------------
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
            " (Manager 쪽 네트워크 문제 가능성)"
        )

    return True, "모든 Replica가 Primary 연결을 잃음"


#-------------------
# Manager
#-------------------
class Manager:
    def __init__(self, interval, threshold, cooldown, auto_failover, record_events=True):
        self.interval = interval
        self.threshold = threshold
        self.cooldown = cooldown
        self.auto_failover = auto_failover
        self.record_events = record_events   # False면 이벤트 파일에 쓰지 않음 (--once)

        self.active_issues = {}
        self.primary = None
        self.primary_failures = 0
        self.last_failover = 0.0      # 마지막 Failover 시도 시각 (monotonic, 쿨다운 기준)
        self.stop_event = threading.Event()

        self.rejoin_thread = None
        self.rejoin_result = None     # ("ok" | "fail", node, 메시지)
    
    # ---------- 알림 ----------
    def emit(self, level, message):
        """알림 지점: 로그 + 이벤트 파일."""
        log.log(level, message)
        if self.record_events:
            events.append(level, message)

    def report(self, issues):
        """상태가 바뀐 이슈만 알린다. (같은 알림 반복 방지)"""
        for key, msg in issues.items():
            if key not in self.active_issues:
                self.emit(logging.WARNING, f"[Triggered] {msg}")

        for key, msg in self.active_issues.items():
            if key not in issues:
                self.emit(logging.INFO, f"[Recovered] {msg}")

        self.active_issues = issues

    # ---------- rejoin thread ----------
    def start_rejoin(self, node, new_primary):
        if self.rejoin_thread and self.rejoin_thread.is_alive():
            self.emit(logging.WARNING, f"Rejoin 이미 진행 중: {node} 요청 건너뜀")
            return

        def worker():
            from apps.rejoin import rejoin
            try:
                rejoin(node)
                self.rejoin_result = ("ok", node, f"Rejoin 완료: {node} -> {new_primary}")
            except Exception as exc:          # 스레드 밖으로 전달되지 않으므로 여기서 잡는다
                self.rejoin_result = ("fail", node,
                                      f"Rejoin 실패: {node} - {exc}. 수동 재합류가 필요합니다. "
                                      f"python -m apps.rejoin --node {node}")

        self.rejoin_result = None
        self.rejoin_thread = threading.Thread(target=worker, name=f"rejoin-{node}")
        self.rejoin_thread.start()            # join 하지 않고 바로 반환
        self.emit(logging.INFO, f"Rejoin 시작: {node} -> {new_primary}")

    def check_rejoin(self):
        """tick 마다 호출: 끝났으면 결과만 처리 (기다리지 않음)"""
        t = self.rejoin_thread
        if t is None or t.is_alive():
            return
        t.join()                              # 이미 끝났으므로 즉시 반환
        status, node, msg = self.rejoin_result
        self.emit(logging.INFO if status == "ok" else logging.WARNING, msg)
        self.rejoin_thread = None

    # ---------- 감시 ----------
    def tick(self):
        statuses, containers = collect()
        primary, issues = cluster_issues(statuses, containers)
        self.report(issues)

        # Primary를 식별할 수 없으면 판정하지 않음
        # 1) split-brain
        # 2) 식별 불가
        if primary is None:
            self.primary_failures = 0
            return

        # primary가 변경되었으면 기록하고 다시 셈
        if primary != self.primary:
            if self.primary is not None:
                self.emit(logging.WARNING, f"Primary 변경됨: {self.primary} -> {primary}")
            self.primary = primary
            self.primary_failures = 0

        # Primary에 접속되면 정상
        # (statuses에는 접속에 성공한 노드만 들어 있음)
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

    def handle_primary_down(self, old_primary, reason):
        if not self.auto_failover:
            if self.primary_failures >= self.threshold:
                self.emit(logging.CRITICAL,
                          f"PRIMARY 장애: {old_primary} ({reason}). "
                          "수동 실행 필요: python -m apps.failover")
            return

        remaining = self.cooldown - (time.monotonic() - self.last_failover)
        if remaining > 0:
            log.warning("Failover 쿨다운 중: %.0f초 남음", remaining)
            return

        self.emit(logging.CRITICAL, f"PRIMARY 장애: {old_primary} ({reason}). Failover를 시작합니다...")
        self.last_failover = time.monotonic()

        from apps.failover import failover
        
        #-----------------
        # 1. Failover 실행
        #-----------------
        try:
            new_primary = failover()

        except Exception as exc:
            self.emit(
                logging.CRITICAL,
                f"Failover 실패: {exc}. 수동 조치가 필요합니다."
            )
            return

        self.emit(
            logging.WARNING,
            f"Failover 완료: New Primary = {new_primary}, "
            f"Old Primary = {old_primary}"
        )

        #-----------------
        # 2. 기존 Primary 재합류 (백그라운드 스레드)
        #-----------------
        # start_rejoin()은 스레드를 시작만 하고 바로 반환한다.
        # 완료/실패 결과는 다음 tick부터 check_rejoin()이 기록한다.
        self.start_rejoin(old_primary, new_primary)

        self.primary_failures = 0

    def request_stop(self, signum, _frame):
        """SIGTERM/SIGINT: 진행 중인 감시(Failover 포함)는 끝까지 마치고 종료한다.

        Failover 도중에 끊기면 클러스터가 반쯤 바뀐 상태로 남기 때문.
        같은 신호를 한 번 더 받으면 즉시 종료한다.
        """
        if self.stop_event.is_set():
            log.warning("강제 종료합니다.")
            raise KeyboardInterrupt
        log.info("종료 요청(%s) 수신: 진행 중인 작업을 마친 뒤 종료합니다.",
                 signal.Signals(signum).name)
        self.stop_event.set()

    def run(self):
        log.info("HA Manager 시작: %s (주기=%s초, 임계치=%d회, 자동 Failover=%s)",
                 ", ".join(NODES), self.interval, self.threshold, self.auto_failover)
        self.emit(logging.INFO,
                  f"HA Manager 시작 (자동 Failover {'ON' if self.auto_failover else 'OFF'})")
        
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.check_rejoin()
                self.tick()
            except Exception as exc:  # 감시 루프는 절대 죽지 않게
                log.exception("감시 중 오류 발생: %s", exc)
            # 대기 중에 종료 요청이 오면 바로 깨어남
            self.stop_event.wait(max(0, self.interval - (time.monotonic() - started)))
        
        # 종료시 진행 중인 rejoin을 기다림
        if self.rejoin_thread:
            log.info("진행 중인 Rejoin 완료 대기...")
            self.rejoin_thread.join()
            self.check_rejoin()

        self.emit(logging.INFO, "HA Manager 종료")


def main():
    parser = argparse.ArgumentParser(description="MySQL HA Manager (감시 + Failover)")

    parser.add_argument("--interval", type=float, default=5, help="감시 주기(초)")
    parser.add_argument("--threshold", type=int, default=3, help="Primary 장애 확정까지 연속 실패 횟수")
    parser.add_argument("--cooldown", type=float, default=60, help="자동 Failover 후 재시도 금지 시간(초)")
    parser.add_argument("--auto-failover", action="store_true", help="Primary 장애 확정 시 자동으로 Failover 수행")
    parser.add_argument("--once", action="store_true", help="한 번만 검사하고 종료")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    manager = Manager(args.interval, args.threshold, args.cooldown, args.auto_failover,
                      record_events=not args.once)

    if args.once:
        manager.tick()
        return 1 if manager.active_issues else 0

    signal.signal(signal.SIGTERM, manager.request_stop)
    signal.signal(signal.SIGINT, manager.request_stop)
    try:
        manager.run()
    except KeyboardInterrupt:
        log.warning("HA Manager 강제 종료")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
