#!/usr/bin/env bash
# ProxySQL 구성: 애플리케이션은 ProxySQL(6033)로 접속하고,
# ProxySQL이 각 노드의 read_only 값을 보고 Writer(HG 10) / Reader(HG 20)를 자동 배치한다.
# → Failover로 Primary가 바뀌어도 접속 주소는 그대로.
#
# 여러 번 실행해도 같은 결과 (계정/설정을 덮어씀). 비밀번호는 .env 에서만 읽는다.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

NODES=("mysql-server-1" "mysql-server-2" "mysql-server-3")
PROXYSQL="proxysql"

WRITER_HG=10
READER_HG=20

# .env 파일 로드
set -a
source .env
set +a

: "${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD is required}"
: "${MYSQL_MON_PASSWORD:?MYSQL_MON_PASSWORD is required}"
: "${MYSQL_APP_PASSWORD:?MYSQL_APP_PASSWORD is required}"
APP_DB="${MYSQL_APP_DB:-appdb}"

# 환경변수 값을 SQL 문자열에 안전하게 넣기 위해 이스케이프
sql_escape() {
    local v="${1//\\/\\\\}"
    printf '%s' "${v//\'/\'\'}"
}
MON_PASSWORD_SQL="$(sql_escape "$MYSQL_MON_PASSWORD")"
APP_PASSWORD_SQL="$(sql_escape "$MYSQL_APP_PASSWORD")"

# MySQL 실행 (SQL은 stdin으로 전달: 비밀번호가 프로세스 목록에 보이지 않도록)
mysql_exec() {
    local container="$1"
    local sql="$2"

    docker exec -i \
        -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" \
        "$container" \
        mysql -uroot -N -B <<< "$sql"
}

# ProxySQL 관리 SQL 실행 (admin 계정은 컨테이너 내부에서만 접속 가능)
proxysql_admin() {
    docker exec -i "$PROXYSQL" \
        mysql -uadmin -padmin -h127.0.0.1 -P6032 -N -B <<< "$1"
}

echo "[1/5] Finding current Primary..."

# 쓰기 가능(read_only=0)하고 복제 설정이 없는 노드 = Primary
find_primary() {
    local node ro repl
    for node in "${NODES[@]}"; do
        ro=$(mysql_exec "$node" "SELECT @@GLOBAL.read_only;" 2>/dev/null) || continue
        repl=$(mysql_exec "$node" "SHOW REPLICA STATUS;" 2>/dev/null) || continue
        if [[ $ro == 0 && -z $repl ]]; then
            echo "$node"
            return 0
        fi
    done
    return 1
}

PRIMARY=""
for _ in $(seq 1 30); do
    PRIMARY=$(find_primary) && break
    sleep 2
done
if [[ -z $PRIMARY ]]; then
    echo "[ERROR] Primary not found. Check the cluster: python -m apps.check_cluster"
    exit 1
fi
echo "  Primary: $PRIMARY"

echo "[2/5] Creating MySQL users on Primary (replicated to all nodes)..."

mysql_exec "$PRIMARY" "
CREATE DATABASE IF NOT EXISTS \`${APP_DB}\`;

-- ProxySQL 상태 확인용: read_only, 복제 지연 조회
CREATE USER IF NOT EXISTS 'monitor'@'%' IDENTIFIED BY '${MON_PASSWORD_SQL}';
ALTER USER 'monitor'@'%' IDENTIFIED BY '${MON_PASSWORD_SQL}';
GRANT USAGE, REPLICATION CLIENT ON *.* TO 'monitor'@'%';

-- 애플리케이션 계정 (ProxySQL에도 같은 이름/비밀번호로 등록)
CREATE USER IF NOT EXISTS 'app'@'%' IDENTIFIED BY '${APP_PASSWORD_SQL}';
ALTER USER 'app'@'%' IDENTIFIED BY '${APP_PASSWORD_SQL}';
GRANT ALL PRIVILEGES ON \`${APP_DB}\`.* TO 'app'@'%';
"
echo "  users: monitor, app (database: ${APP_DB})"

echo "[3/5] Starting ProxySQL..."

docker compose up -d "$PROXYSQL"

until proxysql_admin "SELECT 1;" >/dev/null 2>&1; do
    sleep 1
done
echo "  ProxySQL admin is ready"

echo "[4/5] Configuring ProxySQL..."

server_rows=""
for node in "${NODES[@]}"; do
    server_rows+="${server_rows:+,}(${READER_HG}, '${node}', 3306)"
done

# 모든 노드를 Reader로 넣어 두면 ProxySQL이 read_only=0 인 노드를 Writer 그룹으로 옮긴다
proxysql_admin "
UPDATE global_variables SET variable_value='monitor'            WHERE variable_name='mysql-monitor_username';
UPDATE global_variables SET variable_value='${MON_PASSWORD_SQL}' WHERE variable_name='mysql-monitor_password';
UPDATE global_variables SET variable_value='1000'               WHERE variable_name='mysql-monitor_read_only_interval';
UPDATE global_variables SET variable_value='2000'               WHERE variable_name='mysql-monitor_connect_interval';
UPDATE global_variables SET variable_value='2000'               WHERE variable_name='mysql-monitor_ping_interval';
UPDATE global_variables SET variable_value='8.4.0'              WHERE variable_name='mysql-server_version';
LOAD MYSQL VARIABLES TO RUNTIME;
SAVE MYSQL VARIABLES TO DISK;

DELETE FROM mysql_replication_hostgroups;
INSERT INTO mysql_replication_hostgroups (writer_hostgroup, reader_hostgroup, check_type, comment)
VALUES (${WRITER_HG}, ${READER_HG}, 'read_only', 'mysql-ha');

DELETE FROM mysql_servers;
INSERT INTO mysql_servers (hostgroup_id, hostname, port) VALUES ${server_rows};
LOAD MYSQL SERVERS TO RUNTIME;
SAVE MYSQL SERVERS TO DISK;

DELETE FROM mysql_users WHERE username = 'app';
INSERT INTO mysql_users (username, password, default_hostgroup, default_schema)
VALUES ('app', '${APP_PASSWORD_SQL}', ${WRITER_HG}, '${APP_DB}');
LOAD MYSQL USERS TO RUNTIME;
SAVE MYSQL USERS TO DISK;

-- 읽기 분산: 위에서부터 검사, apply=1 이면 거기서 결정
--   1) 잠금 조회(FOR UPDATE / FOR SHARE)는 Writer
--   2) 나머지 SELECT는 Reader 그룹 (Primary도 Reader 그룹에 있으므로 3대에 분산)
--   그 외(INSERT/UPDATE/DELETE/DDL)는 규칙에 안 걸림 → default_hostgroup(Writer)
-- 트랜잭션 안의 쿼리는 규칙과 관계없이 트랜잭션을 시작한 노드에서 실행됨
DELETE FROM mysql_query_rules WHERE rule_id IN (1, 2);
INSERT INTO mysql_query_rules (rule_id, active, match_digest, destination_hostgroup, apply, comment)
VALUES (1, 1, '^SELECT.*FOR (UPDATE|SHARE)', ${WRITER_HG}, 1, 'locking read -> writer'),
       (2, 1, '^SELECT',                     ${READER_HG}, 1, 'read -> readers');
LOAD MYSQL QUERY RULES TO RUNTIME;
SAVE MYSQL QUERY RULES TO DISK;
"

echo "[5/5] Verifying..."

# monitor가 read_only를 확인해 Writer를 배치할 때까지 대기
writer=""
for _ in $(seq 1 15); do
    writer=$(proxysql_admin "SELECT hostname FROM runtime_mysql_servers
                             WHERE hostgroup_id=${WRITER_HG} AND status='ONLINE';")
    [[ $writer == "$PRIMARY" ]] && break
    sleep 1
done

proxysql_admin "SELECT hostgroup_id, hostname, status FROM runtime_mysql_servers
                ORDER BY hostgroup_id, hostname;" | sed 's/^/  HG /'

if [[ $writer != "$PRIMARY" ]]; then
    echo "[ERROR] Writer hostgroup is '${writer:-empty}', expected $PRIMARY"
    echo "  monitor 로그 확인:"
    proxysql_admin "SELECT hostname, error FROM monitor.mysql_server_read_only_log
                    WHERE error IS NOT NULL ORDER BY time_start_us DESC LIMIT 5;" | sed 's/^/    /'
    exit 1
fi

# 애플리케이션 경로(6033)로 실제 접속해 확인
app_query() {
    docker exec -i -e MYSQL_PWD="$MYSQL_APP_PASSWORD" "$PRIMARY" \
        mysql -uapp -h"$PROXYSQL" -P6033 -N -B <<< "$1"
}
# 쓰기 경로: 트랜잭션은 default_hostgroup(Writer)에서 시작하고 끝날 때까지 같은 노드에 머문다
result=$(app_query "BEGIN; SELECT @@server_id, @@read_only; COMMIT;")
echo "  app write path -> server_id=$(cut -f1 <<< "$result"), read_only=$(cut -f2 <<< "$result")"
if [[ $(cut -f2 <<< "$result") != 0 ]]; then
    echo "[ERROR] write path is not the Primary"
    exit 1
fi
# 읽기 경로: SELECT는 Reader 그룹으로 분산 (접속마다 노드가 달라질 수 있음)
reads=$(for _ in $(seq 1 12); do app_query "SELECT @@server_id;"; done | sort | uniq -c | awk '{printf "server_id=%s:%s ", $2, $1}')
echo "  app read path  -> ${reads}"

echo "Proxysql setup completed. (app: 127.0.0.1:6033, user=app, db=${APP_DB})"
