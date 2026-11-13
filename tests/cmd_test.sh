#!/usr/bin/env bash
set -euo pipefail

# 프로젝트 루트 (.env 위치) 로 이동
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# 환경변수 로드
set -a
source .env
set +a

: "${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD is required}"

PROXYSQL="proxysql"
APP_DB="${MYSQL_APP_DB:-appdb}"

# 터미널에서 실행할 때만 -t (파이프/스크립트에서 실행 시 "not a TTY" 에러 방지)
TTY_OPTS=(-i)
[[ -t 0 && -t 1 ]] && TTY_OPTS=(-it)

mysql_exec() {
    local node="$1"
    shift

    docker exec "${TTY_OPTS[@]}" \
        -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" \
        "$node" mysql -uroot "$@"
}

# ProxySQL 경유 접속 (애플리케이션 경로: app 계정 → 현재 Primary)
# ProxySQL 이미지의 클라이언트는 MariaDB 클라이언트라서 인증서 검증 경고를 끈다
proxy_exec() {
    : "${MYSQL_APP_PASSWORD:?MYSQL_APP_PASSWORD is required}"

    docker exec "${TTY_OPTS[@]}" \
        -e MYSQL_PWD="$MYSQL_APP_PASSWORD" \
        "$PROXYSQL" mysql --skip-ssl-verify-server-cert \
        -uapp -h127.0.0.1 -P6033 "$APP_DB" "$@"
}

# ProxySQL 관리 포트 (admin 계정은 컨테이너 내부에서만 접속 가능)
proxy_admin() {
    docker exec "${TTY_OPTS[@]}" \
        "$PROXYSQL" mysql --skip-ssl-verify-server-cert \
        -uadmin -padmin -h127.0.0.1 -P6032 "$@"
}

# 인자가 없으면 접속, 있으면 SQL 실행
connect_or_run() {
    local fn="$1"
    shift
    if [[ $# -eq 0 ]]; then
        "$fn"
    else
        "$fn" -e "$*"
    fi
}

case "${1:-}" in
    status)
        echo "===== MySQL Cluster Status ====="
        for node in mysql-server-{1..3}; do
            echo
            echo "[$node]"

            docker exec \
                -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" \
                "$node" mysql -uroot -e "
                    SELECT
                        @@hostname AS hostname,
                        @@server_id AS server_id,
                        @@read_only AS read_only,
                        @@super_read_only AS super_read_only,
                        @@gtid_executed AS gtid_executed;
                " || echo "  (접속 불가)"
        done
        ;;

    proxy-status)
        echo "===== ProxySQL Status ====="
        echo
        echo "[Hostgroups]  10 = Writer, 20 = Reader"
        proxy_admin -e "
            SELECT hostgroup_id, hostname, port, status
            FROM runtime_mysql_servers
            ORDER BY hostgroup_id, hostname;"

        echo
        echo "[Connection pool]"
        proxy_admin -e "
            SELECT hostgroup AS hg, srv_host, status, ConnUsed, ConnFree, ConnOK, ConnERR, Queries
            FROM stats_mysql_connection_pool
            ORDER BY hostgroup, srv_host;"

        echo
        # 읽기/쓰기 분리 규칙이 있으면 일반 SELECT는 Reader로 간다.
        # 트랜잭션은 Writer에서 시작해 끝날 때까지 같은 서버에 고정되므로 쓰기 경로 확인에 사용
        echo "[Routing] 6033 (app) 접속 시 연결되는 서버"
        echo "  쓰기 경로 (트랜잭션/INSERT/UPDATE):"
        proxy_exec -N -e "START TRANSACTION;
                          SELECT CONCAT('    server_id=', @@server_id, ', read_only=', @@read_only);
                          COMMIT;"
        echo "  읽기 경로 (SELECT, 5회):"
        for _ in 1 2 3 4 5; do
            proxy_exec -N -e "SELECT CONCAT('    server_id=', @@server_id, ', read_only=', @@read_only);"
        done

        echo
        echo "[Query rules]"
        proxy_admin -e "
            SELECT rule_id, active, match_digest, destination_hostgroup AS dest_hg, apply
            FROM runtime_mysql_query_rules
            ORDER BY rule_id;"
        ;;

    1|2|3)
        node="mysql-server-$1"
        shift
        if [[ $# -eq 0 ]]; then
            mysql_exec "$node"
        else
            mysql_exec "$node" -e "$*"
        fi
        ;;

    proxy)
        shift
        connect_or_run proxy_exec "$@"
        ;;

    admin)
        shift
        connect_or_run proxy_admin "$@"
        ;;

    *)
        echo "Usage:"
        echo "  $0 status                        # 전체 MySQL 상태 조회"
        echo "  $0 1                             # Server 1 접속"
        echo "  $0 2                             # Server 2 접속"
        echo "  $0 3                             # Server 3 접속"
        echo "  $0 1 'SELECT VERSION();'         # Server 1 SQL 실행"
        echo
        echo "  $0 proxy-status                  # ProxySQL Writer/Reader, 커넥션 풀, 라우팅 확인"
        echo "  $0 proxy                         # ProxySQL 경유 접속 (app 계정 → 현재 Primary)"
        echo "  $0 proxy 'SELECT @@server_id;'   # ProxySQL 경유 SQL 실행"
        echo "  $0 admin                         # ProxySQL 관리 콘솔 접속"
        echo "  $0 admin 'SELECT * FROM runtime_mysql_servers;'"
        exit 1
        ;;
esac
