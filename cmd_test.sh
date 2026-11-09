
#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

# 환경변수 로드
set -a
source .env
set +a

: "${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD is required}"

mysql_exec() {
    local node="$1"
    shift

    docker exec -it \
        -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" \
        "$node" mysql -uroot "$@"
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
                "
        done
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

    *)
        echo "Usage:"
        echo "  $0 status                 # 전체 MySQL 상태 조회"
        echo "  $0 1                      # Server 1 접속"
        echo "  $0 2                      # Server 2 접속"
        echo "  $0 3                      # Server 3 접속"
        echo "  $0 1 'SELECT VERSION();'  # Server 1 SQL 실행"
        exit 1
        ;;
esac
