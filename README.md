# 초기 구성, 상태 확인
./reset.sh && ./setup.sh && .venv/bin/python -m apps.check_cluster

# Switch over(Primary 정상), 상태 확인
.venv/bin/python -m apps.failover && .venv/bin/python -m apps.check_cluster

# Primary Down, 상태 확인
docker stop mysql-server-2 && .venv/bin/python -m apps.check_cluster

# Fail over(Primary Down), 상태 확인
.venv/bin/python -m apps.failover &&  .venv/bin/python -m apps.check_cluster
# 백그라운드 기동/중지 (HA Manager + 웹 대시보드). 로그: logs/manager.log, logs/web.log
./ha.sh start              # 둘 다 기동 (하나만: ./ha.sh start manager)
./ha.sh status
./ha.sh restart web
./ha.sh stop               # manager는 진행 중인 Failover를 마치고 종료

# 실행 옵션은 환경 변수로 지정 (기본: manager는 --auto-failover)
MANAGER_OPTS="" ./ha.sh start manager                                   # 감시+알림만 (자동 Failover 끔)
MANAGER_OPTS="--auto-failover --threshold 5" ./ha.sh start manager    # 옵션 변경

# 포그라운드 실행 (디버깅용, 종료: Ctrl+C)
.venv/bin/python -m apps.manager --auto-failover   # HA Manager: 하나만 실행할 것
.venv/bin/python -m apps.web                       # 웹 대시보드(보기 전용): http://127.0.0.1:8000
