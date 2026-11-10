# 초기 구성, 상태 확인
./reset.sh && ./setup.sh && .venv/bin/python -m apps.check_cluster

# Switch over(Primary 정상), 상태 확인
.venv/bin/python -m apps.failover && .venv/bin/python -m apps.check_cluster

# Primary Down, 상태 확인
docker stop mysql-server-2 && .venv/bin/python -m apps.check_cluster

# Fail over(Primary Down), 상태 확인
.venv/bin/python -m apps.failover &&  .venv/bin/python -m apps.check_cluster