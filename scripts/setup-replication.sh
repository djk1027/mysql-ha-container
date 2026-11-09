#!/usr/bin/env bash

set -euo pipefail

PRIMARY="mysql-server-1"
REPLICAS=("mysql-server-2" "mysql-server-3")

# .env 파일 로드
set -a
source .env
set +a

: "${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD is required}"
: "${MYSQL_REPL_PASSWORD:?MYSQL_REPL_PASSWORD is required}"

# MySQL 실행
mysql_exec() {
    local container="$1"
    local sql="$2"

    docker exec -i \
        -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" \
        "$container" \
        mysql -uroot -e "$sql"
}

echo "[1/3] Waiting for MySQL containers..."

for container in "$PRIMARY" "${REPLICAS[@]}"; do
    until docker exec \
        -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" \
        "$container" \
        mysqladmin -uroot ping --silent >/dev/null 2>&1; do
        sleep 2
    done

    echo "  $container is ready"
done

echo "[2/3] Creating replication user..."

# 환경변수 값을 SQL 문자열에 안전하게 넣기 위해 이스케이프
REPL_PASSWORD_SQL="${MYSQL_REPL_PASSWORD//\\/\\\\}"
REPL_PASSWORD_SQL="${REPL_PASSWORD_SQL//\'/\'\'}"

mysql_exec "$PRIMARY" "
CREATE USER IF NOT EXISTS 'repl'@'%'
IDENTIFIED BY '${REPL_PASSWORD_SQL}';

ALTER USER 'repl'@'%'
IDENTIFIED BY '${REPL_PASSWORD_SQL}';

GRANT REPLICATION SLAVE ON *.* TO 'repl'@'%';
"

echo "[3/3] Configuring replicas..."

for replica in "${REPLICAS[@]}"; do
    echo "  Configuring $replica..."

    mysql_exec "$replica" "
STOP REPLICA;

CHANGE REPLICATION SOURCE TO
    SOURCE_HOST='${PRIMARY}',
    SOURCE_PORT=3306,
    SOURCE_USER='repl',
    SOURCE_PASSWORD='${REPL_PASSWORD_SQL}',
    SOURCE_AUTO_POSITION=1,
    GET_SOURCE_PUBLIC_KEY=1;

START REPLICA;

SET GLOBAL super_read_only = ON;
"

done

echo "Replication setup completed."