"""실행: python -m apps.web  →  http://127.0.0.1:8000

읽기 전용 대시보드. 장애 감지/Failover는 HA Manager(python -m apps.manager)가 담당하며,
대시보드는 Manager가 남긴 상태/이벤트 파일을 읽어서 보여 준다.

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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    store = Store()
    Collector(store, args.interval).start()

    try:
        serve(store, args.host, args.port)
    except KeyboardInterrupt:
        logging.info("대시보드를 종료합니다.")


if __name__ == "__main__":
    main()
