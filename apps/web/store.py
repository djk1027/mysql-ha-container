"""메모리 저장소.

- 디스크에 아무것도 쓰지 않는다. 프로세스를 끄면 사라진다.
- Collector 스레드가 쓰고, HTTP 요청 스레드들이 읽는다 → Lock/Condition으로 보호.
"""

import threading
from collections import deque

HISTORY_SIZE = 720   # 5초 주기 기준 1시간


class Store:
    def __init__(self):
        self._cond = threading.Condition()
        self._history = deque(maxlen=HISTORY_SIZE)
        self._events = []   # HA Manager 이벤트 (Collector가 매 주기 파일에서 읽어 넣음)
        self._latest = None
        self._seq = 0   # 스냅샷이 갱신될 때마다 증가 (SSE가 새 데이터 여부 판단)

    # ---------- 쓰기 (Collector) ----------
    def push_snapshot(self, snapshot):
        with self._cond:
            self._seq += 1
            snapshot["seq"] = self._seq
            self._latest = snapshot
            self._history.append(_to_point(snapshot))
            self._cond.notify_all()

    def set_events(self, events):
        with self._cond:
            self._events = list(events)

    # ---------- 읽기 (HTTP) ----------
    def latest(self):
        with self._cond:
            return self._latest

    def history(self):
        with self._cond:
            return list(self._history)

    def events(self):
        with self._cond:
            return list(self._events)

    def wait_for_update(self, last_seq, timeout):
        """last_seq 이후 새 스냅샷이 생길 때까지 대기. 없으면 None."""
        with self._cond:
            self._cond.wait_for(lambda: self._seq > last_seq, timeout=timeout)
            if self._seq > last_seq:
                return self._latest
            return None


def _to_point(snapshot):
    """그래프용으로 필요한 값만 남긴 시계열 포인트."""
    return {
        "ts": snapshot["ts"],
        "nodes": {
            name: {
                "cpu": n["cpu"],
                "mem": n["mem_pct"],
                "qps": n["qps"],
                "conn": n["connections"],
            }
            for name, n in snapshot["nodes"].items()
        },
    }
