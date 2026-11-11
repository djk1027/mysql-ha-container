"""실행: python -m apps.web  →  http://127.0.0.1:8000

종료: Ctrl+C (데이터는 메모리에만 있으므로 남는 파일 없음)
"""

import argparse
import logging

from apps.web.collector import Collector
from apps.web.server import serve
from apps.web.store import Store


def main():
    parser = argparse.ArgumentParser(description="MySQL HA 웹 대시보드")
    parser.add_argument("--host", default="127.0.0.1",
                        help="바인딩 주소 (인증이 없으므로 기본은 로컬 전용)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--interval", type=float, default=5, help="수집 주기(초)")
    parser.add_argument("--threshold", type=int, default=3,
                        help="Primary 장애 확정까지 연속 실패 횟수")
    parser.add_argument("--cooldown", type=float, default=300,
                        help="자동 Failover 후 재시도 금지 시간(초)")
    parser.add_argument("--auto-failover", action="store_true",
                        help="Primary 장애 확정 시 자동으로 Failover 수행")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    store = Store()
    Collector(store, args.interval, args.threshold,
              args.cooldown, args.auto_failover).start()

    try:
        serve(store, args.host, args.port)
    except KeyboardInterrupt:
        logging.info("대시보드를 종료합니다.")


if __name__ == "__main__":
    main()
