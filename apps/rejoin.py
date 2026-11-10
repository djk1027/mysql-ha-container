
import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pymysql
from dotenv import load_dotenv

from apps.common import NODES
from apps.failover import one, execute, get_node_status


PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = PROJECT_ROOT / "compose.yaml"

load_dotenv(PROJECT_ROOT / ".env")

ROOT_PASSWORD = os.environ["MYSQL_ROOT_PASSWORD"]
REPL_PASSWORD = os.environ["MYSQL_REPL_PASSWORD"]


def docker(*args):
    """Docker 명령을 실행하고 결과를 반환한다."""
    result = subprocess.run(
        ["docker", *args],
        check=True,
        capture_output=True,
        text=True
    )
    return result.stdout.strip()


def find_primary():
    """현재 Primary를 탐색한다."""
    primaries = []

    for node in NODES:
        try:
            if get_node_status(node)["is_primary"]:
                primaries.append(node)
        except pymysql.MySQLError:
            continue

    if len(primaries) != 1:
        raise RuntimeError(
            f"Expected one Primary, found: {primaries}"
        )

    return primaries[0]


def start_read_only(node):
    """중지된 Old Primary를 읽기 전용으로 재생성하여 기동한다."""

    # 1. 기존 컨테이너 정보 조회
    # docker inspect mysql-server-2
    info = json.loads(docker("inspect", node))[0]

    # 2. 중지된 컨테이너인지 확인
    if info["State"]["Running"]:
        raise RuntimeError(
            f"{node}: Already running. Safe startup required."
        )

    # 3. Compose 서비스 이름과 실행 옵션 추출
    service = info["Config"]["Labels"].get(
        "com.docker.compose.service"
    )

    command = info["Config"]["Cmd"]

    if not service or not isinstance(command, list) or not command:
        raise RuntimeError("Cannot identify Compose service or command.")

    # 4. 기존 실행 옵션에서 읽기 전용 관련 설정 제거
    safe_command = [
        arg for arg in command
        if not arg.startswith((
            "--read-only",
            "--super-read-only",
            "--persisted-globals-load"
        ))
    ]

    # 5. 안전한 기동 옵션 추가
    safe_command += [
        "--persisted-globals-load=OFF",
        "--read-only=ON",
        "--super-read-only=ON"
    ]

    # 6. 임시 Compose 설정 파일 생성
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
        dir=ROOT,
        encoding="utf-8",
        delete=False
    ) as f:
        json.dump(override, f)
        override_file = Path(f.name)

    try:
        # 7. 컨테이너 재생성 및 기동
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
        # 8. 임시 파일 삭제
        override_file.unlink(missing_ok=True)

    # 9. 실제 읽기 전용 상태 검증
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
            os.environ["REPL_PASSWORD"]
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


def restore_compose(node, service, primary):
    """원래 Compose 설정으로 재생성하고 복제 상태를 재검증한다."""

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

    target_gtid = one(
        primary, "SELECT @@GLOBAL.gtid_executed AS gtid"
    )["gtid"]

    verify_rejoin(node, primary, target_gtid)

    print("    [OK] Original Compose configuration restored.")

###########################
#   Main Rejoin Logic
###########################
def rejoin(node):
    if node not in NODES:
        raise RuntimeError(f"Unknown node: {node}")

    print("[1/6] 현재 Primary를 탐색합니다...")
    primary = find_primary()
    print(f"    [INFO] Primary: {primary}")

    if node == primary:
        raise RuntimeError("Cannot rejoin the current Primary.")

    print("[2/6] Old Primary를 기동합니다...")
    service = start_read_only(node)

    print("[3/6] GTID를 검증합니다...")
    target_gtid = validate_gtid(node, primary)

    print("[4/6] Replica 복제를 재구성합니다...")
    configure_replication(node, primary)

    print("[5/6] 복제 동기화를 확인합니다...")
    verify_rejoin(node, primary, target_gtid)

    print("[6/6] 기본 Compose 설정을 복원합니다...")
    restore_compose(node, service, primary)

    print(f"\n[OK] Rejoin completed: {node} -> {primary}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--node",
        required=True,
        choices=list(NODES)
    )

    args = parser.parse_args()
    rejoin(args.node)
