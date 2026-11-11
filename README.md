# 초기 구성, 상태 확인
./reset.sh && ./setup.sh && .venv/bin/python -m apps.check_cluster

# Switch over(Primary 정상), 상태 확인
.venv/bin/python -m apps.failover && .venv/bin/python -m apps.check_cluster

# Primary Down, 상태 확인
docker stop mysql-server-2 && .venv/bin/python -m apps.check_cluster

# Fail over(Primary Down), 상태 확인
.venv/bin/python -m apps.failover &&  .venv/bin/python -m apps.check_cluster
# 상시 감시 (알림만 / 자동 Failover)
.venv/bin/python -m apps.monitor
.venv/bin/python -m apps.monitor --auto-failover --threshold 3 --cooldown 300

# 웹 대시보드 (추가 설치 없음, 표준 라이브러리만 사용)
.venv/bin/python -m apps.web                    # http://127.0.0.1:8000
.venv/bin/python -m apps.web --auto-failover    # 감시 + 자동 Failover
# 종료: Ctrl+C / 제거: apps/web 폴더 삭제 (데이터는 메모리에만 저장)
