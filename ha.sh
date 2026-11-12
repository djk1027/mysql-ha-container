#!/usr/bin/env bash
# MySQL HA 서비스 백그라운드 기동/중지
#   ./ha.sh {start|stop|restart|status} [manager|web|all]
# 로그: logs/<서비스>.log
# 실행 여부는 프로세스 명령줄
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
PYTHON=.venv/bin/python

# 실행 옵션 (환경 변수로 덮어쓸 수 있음)
# MANAGER_OPTS: 미지정 시 자동 Failover 사용. 빈 값("")을 주면 감시+알림만
# (':-' 가 아닌 '-' 를 써야 빈 값으로 끌 수 있음)

# 예: "--auto-failover --threshold 3 --cooldown 60"
MANAGER_OPTS=${MANAGER_OPTS-"--auto-failover"}

# 예: "--port 8000"
WEB_OPTS=${WEB_OPTS:-}

# manager는 진행 중인 Failover를 마치고 종료하므로 넉넉히
STOP_TIMEOUT=120

module_of() { [[ $1 == manager ]] && echo apps.manager || echo apps.web; }
opts_of()   { [[ $1 == manager ]] && echo "$MANAGER_OPTS" || echo "$WEB_OPTS"; }

# 실행 중인 PID 목록
# (-m apps.manager / -m apps.web)
running_pid() {
    local mod
    mod=$(module_of "$1")
    pgrep -f -- "-m ${mod//./\\.}( |\$)" | tr '\n' ' ' | sed 's/ $//' || true
}

start_one() {
    local svc=$1 pid
    pid=$(running_pid "$svc")
    if [[ -n $pid ]]; then echo "$svc: 이미 실행 중 (pid=$pid)"; return 0; fi

    mkdir -p logs
    # shellcheck disable=SC2046,SC2086
    nohup "$PYTHON" -u -m $(module_of "$svc") $(opts_of "$svc") >> "logs/$svc.log" 2>&1 < /dev/null &
    pid=$!

    sleep 3
    # 초기화(docker 조회, 포트 바인딩) 중 죽는지 확인
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "$svc: 기동 실패 (logs/$svc.log 확인)"
        return 1
    fi
    echo "$svc: 시작 (pid=$pid)"
}

stop_one() {
    local svc=$1 pid i
    pid=$(running_pid "$svc")
    if [[ -z $pid ]]; then echo "$svc: 실행 중 아님"; return 0; fi

    # shellcheck disable=SC2086
    kill -TERM $pid
    for ((i = 0; i < STOP_TIMEOUT; i++)); do
        if [[ -z $(running_pid "$svc") ]]; then
            echo "$svc: 중지"
            return 0
        fi
        sleep 1
    done
    echo "$svc: ${STOP_TIMEOUT}초 안에 종료되지 않음 (강제 종료: kill -9 $pid)"
    return 1
}

status_one() {
    local pid
    pid=$(running_pid "$1")
    if [[ -n $pid ]]; then echo "$1: 실행 중 (pid=$pid)"; else echo "$1: 중지됨"; fi
}

case ${2:-all} in
    manager|web) services=("$2") ;;
    all)         services=(manager web) ;;
    *)           echo "usage: $0 {start|stop|restart|status} [manager|web|all]"; exit 1 ;;
esac

case ${1:-} in
    start)   for s in "${services[@]}"; do start_one "$s"; done ;;
    stop)    for s in "${services[@]}"; do stop_one "$s"; done ;;
    restart) for s in "${services[@]}"; do stop_one "$s" && start_one "$s"; done ;;
    status)  for s in "${services[@]}"; do status_one "$s"; done ;;
    *)       echo "usage: $0 {start|stop|restart|status} [manager|web|all]"; exit 1 ;;
esac
