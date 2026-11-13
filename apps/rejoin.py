import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path

import pymysql

from apps.common import one, execute, collect, find_primary, REPL_PASSWORD
from apps.common import NODES, PROJECT_ROOT, COMPOSE_FILE, configuring

def docker(*args):
    """Docker 명령을 실행하고 결과를 반환한다."""
    result = subprocess.run(
        ["docker", *args],
        check=True,
        capture_output=True,
        text=True
    )
    return result.stdout.strip()


def current_primary():
    """현재 Primary를 탐색한다. (공통 Primary 식별 + 실제로 쓰기 가능한지 확인)"""
    statuses, _ = collect()
    primaries = find_primary(statuses)

    # Source_Host 합의로만 추정된 Primary(접속 불가/쓰기 불가)에는 재합류할 수 없다
    if len(primaries) != 1 or not statuses.get(primaries[0], {}).get("is_primary"):
        raise RuntimeError(
            f"Expected one writable Primary, found: {primaries}"
        )

    return primaries[0]


def start_read_only(node):
    """중지된 Old Primary를 읽기 전용으로 재생성하여 기동한다."""

    # 1. 기존 컨테이너 정보 조회
    # docker inspect mysql-server-2
    info = json.loads(docker("inspect", node))[0]

    service = info["Config"]["Labels"].get("com.docker.compose.service")
    if not service:
        raise RuntimeError("Cannot identify Compose service.")

    # 2. 이미 실행 중인 경우: 읽기 전용이면 재사용, 쓰기 가능하면 거부
    if info["State"]["Running"]:
        try:
            status = one(node, """
                SELECT @@GLOBAL.read_only AS read_only,
                       @@GLOBAL.super_read_only AS super_read_only
            """)
        except pymysql.MySQLError as exc:
            raise RuntimeError(f"{node}: Running but MySQL unreachable ({exc}).")

        if status["read_only"] == 1 and status["super_read_only"] == 1:
            execute(node, "SET PERSIST super_read_only = ON")
            print(f"    [INFO] {node}: Already running read-only. Resuming.")
            return service

        # 쓰기 가능한 상태로 떠 있는 노드에 복제를 붙이면 split-brain 위험
        raise RuntimeError(
            f"{node}: Running and writable. Stop it first (docker stop {node})."
        )
    
    # 3. 기존 실행 옵션에서 읽기 전용 관련 설정 제거
    command = info["Config"]["Cmd"]

    if not service or not isinstance(command, list) or not command:
        raise RuntimeError("Cannot identify Compose service or command.")

    safe_command = [
        arg for arg in command
        if not arg.startswith((
            "--read-only",
            "--super-read-only",
            "--persisted-globals-load"
        ))
    ]

    # 4. 안전한 기동 옵션 추가
    safe_command += [
        "--persisted-globals-load=OFF",
        "--read-only=ON",
        "--super-read-only=ON"
    ]

    # 5. 임시 Compose 설정 파일 생성
    override = {
        "services": {
            service: {
                "command": safe_command
            }
        }
    }

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix=".rejoin-",
        dir=PROJECT_ROOT,
        encoding="utf-8",
        delete=False
    ) as f:
        json.dump(override, f)
        override_file = Path(f.name)

    try:
        # 6. 컨테이너 재생성 및 기동
        docker(
            "compose",
            "-f", str(COMPOSE_FILE),
            "-f", str(override_file),
            "up", "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout", "60",
            service
        )
    finally:
        # 7. 임시 파일 삭제
        override_file.unlink(missing_ok=True)

    # 8. 실제 읽기 전용 상태 검증
    status = one(
        node,
        """
        SELECT
            @@GLOBAL.read_only AS read_only,
            @@GLOBAL.super_read_only AS super_read_only
        """
    )

    if status["read_only"] != 1 or status["super_read_only"] != 1:
        raise RuntimeError(f"{node}: Read-only startup failed.")
    
    # 9. 재시작 후에도 읽기 전용 유지
    execute(node, "SET PERSIST super_read_only = ON")

    print(f"    [OK] Started safely: {node}")

    return service


def validate_gtid(node, primary):
    """Old Primary에만 존재하는 GTID가 있는지 검사한다."""

    old_gtid = one(
        node, "SELECT @@GLOBAL.gtid_executed AS gtid"
    )["gtid"]

    new_gtid = one(
        primary, "SELECT @@GLOBAL.gtid_executed AS gtid"
    )["gtid"]


    # old_gtid(node)에 포함되어 있지만,
    # new_gtid(primary)에는 포함되어 있지 않은 GTID들만 추출하여 반환
    result = one(
        node,
        "SELECT GTID_SUBTRACT(%s, %s) AS errant",
        (old_gtid, new_gtid)
    )

    if result["errant"]:
        raise RuntimeError(
            f"Errant GTIDs detected: {result['errant']}. "
            "Manual recovery or rebuild required."
        )

    print("    [OK] No errant GTIDs.")

    return new_gtid

def configure_replication(node, primary):
    """기존 Primary를 새로운 Primary의 Replica로 연결한다."""

    # 재시작 후에도 읽기 전용이 유지되도록 저장
    execute(node, "SET PERSIST super_read_only = ON")

    if one(node, "SHOW REPLICA STATUS"):
        execute(node, "STOP REPLICA")

    execute(
        node,
        """
        CHANGE REPLICATION SOURCE TO
            SOURCE_HOST = %s,
            SOURCE_PORT = 3306,
            SOURCE_USER = %s,
            SOURCE_PASSWORD = %s,
            SOURCE_AUTO_POSITION = 1,
            GET_SOURCE_PUBLIC_KEY = 1
        """,
        (
            primary,
            "repl",
            REPL_PASSWORD
        )
    )

    execute(node, "START REPLICA")


def verify_rejoin(node, primary, target_gtid, timeout=30):
    """복제 연결과 GTID 적용 여부를 확인한다."""

    deadline = time.monotonic() + timeout

    while True:
        status = one(node, "SHOW REPLICA STATUS")

        if not status:
            raise RuntimeError("Replication configuration missing.")

        if status["Last_SQL_Errno"] != 0:
            raise RuntimeError(status["Last_SQL_Error"])

        if status["Last_IO_Errno"] != 0:
            raise RuntimeError(status["Last_IO_Error"])

        threads_ok = (
            status["Source_Host"] == primary
            and status["Auto_Position"] == 1
            and status["Replica_IO_Running"] == "Yes"
            and status["Replica_SQL_Running"] == "Yes"
        )

        if threads_ok:
            result = one(
                node,
                """
                SELECT GTID_SUBSET(
                    %s, @@GLOBAL.gtid_executed
                ) AS synced
                """,
                (target_gtid,)
            )

            if result["synced"] == 1:
                break

        if time.monotonic() >= deadline:
            raise TimeoutError(f"{node}: Rejoin timed out.")

        time.sleep(0.5)

    print(f"    [OK] Replication synchronized: {node}")


def restore_compose_settings(node, service):
    """임시 기동 옵션을 제거하고 원래 Compose 설정으로 복원한다."""

    # 1. 원본 compose.yaml 설정으로 컨테이너 재생성
    docker(
        "compose",
        "-f", str(COMPOSE_FILE),
        "up", "-d",
        "--no-deps",
        "--force-recreate",
        "--wait",
        "--wait-timeout", "60",
        service
    )

    # 2. 재기동 후 읽기 전용 상태 확인
    status = one(
        node,
        """
        SELECT
            @@GLOBAL.read_only AS read_only,
            @@GLOBAL.super_read_only AS super_read_only
        """
    )

    if status["read_only"] != 1 or status["super_read_only"] != 1:
        raise RuntimeError("Read-only setting was not preserved.")

    print(f"    [OK] Compose settings restored: {node}")

###########################
#   Main Rejoin Logic
###########################
def rejoin(node):
    """노드를 현재 Primary의 Replica로 재합류시킨다. 진행하는 동안 그 노드를 CONFIGURING 으로 표시한다."""
    if node not in NODES:
        raise RuntimeError(f"Unknown node: {node}")

    with configuring([node], "rejoin"):
        return _rejoin(node)


def _rejoin(node):

    # ---------------------------
    # 1. Primary 탐색
    # ---------------------------
    print("[1/6] 현재 Primary를 탐색합니다...")
    primary = current_primary()
    print(f"    [INFO] Primary: {primary}")

    if node == primary:
        raise RuntimeError("Cannot rejoin the current Primary.")

    # ---------------------------
    # 2. 임시 읽기 전용 기동
    # ---------------------------
    print("[2/6] 노드를 Read only로 기동합니다...")
    service = start_read_only(node)
    
    # ---------------------------
    # 3. GTID 검증
    # ---------------------------
    print("[3/6] GTID를 검증합니다...")
    target_gtid = validate_gtid(node, primary)

    # ---------------------------
    # 4. 원래 Compose 설정으로 복원
    # ---------------------------
    print("[4/6] 기본 Compose 설정을 복원합니다...")
    restore_compose_settings(node, service)
    
    # ---------------------------
    # 5. DB 복제 재구성
    # ---------------------------
    print("[5/6] Replica 복제를 재구성합니다...")
    configure_replication(node, primary)

    # ---------------------------
    # 6. 복제 검증
    # ---------------------------
    print("[6/6] 복제 동기화를 확인합니다...")
    verify_rejoin(node, primary, target_gtid)

    print(f"\n[OK] Rejoin completed: {node} -> {primary}")
    
    return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--node",
        required=True,
        choices=list(NODES)
    )

    args = parser.parse_args()
    rejoin(args.node)
