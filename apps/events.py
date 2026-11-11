"""HA Manager 이벤트 기록.

Manager가 알림(이상 발생/회복, Primary 변경, Failover 등)을 한 줄씩 추가하고,
웹 대시보드는 최근 이벤트를 읽어서 보여 준다.

  logs/events.jsonl   한 줄에 JSON 하나 {"ts", "level", "message"}
"""

import json
import os
import time

from apps.common import PROJECT_ROOT

EVENTS_FILE = PROJECT_ROOT / "logs" / "events.jsonl"
MAX_BYTES = 1_000_000   # 넘으면 최근 KEEP 건만 남긴다
KEEP = 500


def append(level, message):
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": time.time(), "level": level, "message": message},
                      ensure_ascii=False)
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

    if EVENTS_FILE.stat().st_size > MAX_BYTES:
        lines = EVENTS_FILE.read_text(encoding="utf-8").splitlines()[-KEEP:]
        tmp = EVENTS_FILE.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, EVENTS_FILE)


def recent(limit=200):
    """최근 이벤트 (오래된 것 → 최신 순). 파일이 없으면 빈 목록."""
    try:
        lines = EVENTS_FILE.read_text(encoding="utf-8").splitlines()[-limit:]
    except FileNotFoundError:
        return []

    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue   # 쓰는 도중에 읽힌 마지막 줄 등
    return events
