import time
import pymysql

from apps.common import one, execute, REPL_PASSWORD
from apps.common import NODES, Mode, check_container, stop_container, get_node_status, find_primary, configuring

# 1. Node들의 Status 세팅 
def collect_node_statuses():
    """노드 상태 수집 및 기존 Primary, 전환 모드 판단"""
    statuses = {}
    errors = []

    for node in NODES:
        try:
            statuses[node] = get_node_status(node)
        except pymysql.MySQLError as exc:
            errors.append(f"{node}: Connection failed - {exc}")
            print(f"[WARN] {node}: unavailable")

    # 1-1. 기존 Primary 식별 (공통 로직: Manager/대시보드와 같은 기준)
    #   쓰기 가능한 노드가 있으면 그 노드, 없으면 Replica들의 Source_Host 합의
    primaries = find_primary(statuses)

    if len(primaries) > 1:
        raise RuntimeError(f"Multiple primary candidates detected: {primaries}")

    if not primaries:
        raise RuntimeError("Unable to identify the Primary.")

    old_primary = primaries[0]

    # 1-2. 전환 전 안전 검사 (failover 자체 판단: 수동 실행 시에도 적용)
    #   접속 가능한 Replica가 모두 old_primary를 Source로 보고 있어야 한다
    sources = {
        status["replica"]["Source_Host"]
        for status in statuses.values()
        if status["replica"] is not None
        and status["replica"]["Source_Host"]
    }

    if sources != {old_primary}:
        raise RuntimeError(
            f"Replication source mismatch: Primary={old_primary}, "
            f"Replica sources={sorted(sources)}"
        )

    # 1-3. Switchover / Failover 판단
    if old_primary not in statuses:
        mode = Mode.FAILOVER

    elif statuses[old_primary]["is_primary"]:
        mode = Mode.SWITCHOVER

    else:
        raise RuntimeError(
            "Old Primary is reachable but not writable."
        )

    print(f"    [INFO] Mode        : {mode.upper()}")
    print(f"    [INFO] Old Primary : {old_primary}")

    return statuses, old_primary, mode

# 2. New Primary 후보 탐색
def find_candidates(statuses, old_primary):
    """승격 가능한 Replica 후보 목록을 반환한다."""
    candidates = []

    for node in NODES:
        if node == old_primary:
            continue

        if node not in statuses:
            continue

        status = statuses[node]

        if status["replica"] is None:
            continue

        if status["replica"]["Source_Host"] != old_primary:
            continue

        if status["super_read_only"] != 1:
            continue

        candidates.append(node)

    if not candidates:
        raise RuntimeError("No eligible Replica candidates.")
    
    print(f"    [INFO] Candidates: {', '.join(candidates)}")
    return candidates

# 3. Old Primary 쓰기 차단 및 격리
def fence_old_primary(old_primary, mode, candidates, timeout=30):
    """기존 Primary의 쓰기를 차단하고 컨테이너를 격리한다."""

    final_gtid = None

    # Switchover: 정상적인 Primary 전환
    if mode == Mode.SWITCHOVER:
        print(f"    [INFO] Blocking writes on {old_primary}...")

        # 1. Old Primary 신규 쓰기 차단
        execute(
            old_primary,
            "SET PERSIST super_read_only = ON"
        )

        # 2. 쓰기 차단 확인 및 최종 GTID 확보
        result = one(old_primary, """
            SELECT
                @@GLOBAL.read_only AS read_only,
                @@GLOBAL.super_read_only AS super_read_only,
                @@GLOBAL.gtid_executed AS gtid_executed
        """)

        if (
            result["read_only"] != 1
            or result["super_read_only"] != 1
        ):
            raise RuntimeError(
                f"Failed to block writes on {old_primary}."
            )

        final_gtid = result["gtid_executed"]

        print(f"    [OK] Writes blocked: {old_primary}")
        print(f"    [INFO] Final GTID: {final_gtid}")

        # 3. Replica들이 최종 GTID까지 적용하도록 대기
        for node in candidates:
            print(f"    [INFO] Waiting for {node} to synchronize...")

            status = one(node, "SHOW REPLICA STATUS")

            if (
                not status
                or status["Source_Host"] != old_primary
                or status["Replica_IO_Running"] != "Yes"
                or status["Replica_SQL_Running"] != "Yes"
                or status["Last_IO_Errno"] != 0
                or status["Last_SQL_Errno"] != 0
            ):
                raise RuntimeError(
                    f"{node}: Replication is not healthy."
                )

            result = one(
                node,
                """
                SELECT WAIT_FOR_EXECUTED_GTID_SET(%s, %s) AS wait_result
                """,
                (final_gtid, timeout)
            )

            if result["wait_result"] != 0:
                raise RuntimeError(
                    f"{node}: GTID synchronization timed out."
                )

            print(f"    [OK] GTID synchronized: {node}")

    elif mode == Mode.FAILOVER:
        # 4. Failover일 때 Old Primary 컨테이너 중지 및 검증
        stop_container(old_primary)
        print(f"    [OK] Old Primary fenced: {old_primary}")

    else:
        raise ValueError(f"Unsupported transition mode: {mode}")

    return final_gtid

def drain_replica(node, timeout=30):
    """Replica가 수신한 Relay Log를 모두 적용한 뒤 복제를 중지한다."""
    
    # STOP REPLICA IO_THREAD
    # SHOW REPLICA STATUS (Retrieved_Gtid_Set)
    #   -> Replica가 Primary로부터 수신한 GTID 집합
    #
    # @@GLOBAL.gtid_executed
    #   -> 해당 MySQL 서버에서 실행 완료한 GTID 집합 (복제로 적용한 트랜잭션 + 자체 실행한 트랜잭션)
    #
    # GTID_SUBSET(Retrieved_Gtid_Set, @@GLOBAL.gtid_executed)
    #   -> 결과가 1이면 수신한 모든 GTID가 실행 완료
    #
    # IO Thread  : 데이터를 받아오는 역할
    # SQL Thread : 받아온 데이터를 실제 DB에 반영하는 역할이야.

    print(f"    [INFO] Draining {node}...")

    # 1. 추가 트랜잭션 수신 중지
    execute(node, "STOP REPLICA IO_THREAD")
    status = one(node, "SHOW REPLICA STATUS")

    if not status:
        raise RuntimeError(f"{node}: Replication is not configured.")

    if status["Replica_IO_Running"] != "No":
        raise RuntimeError(f"{node}: IO Thread is still running.")

    # 2. 수신 완료한 GTID 확보
    received = status["Retrieved_Gtid_Set"] or ""

    # 3. 수신된 GTID가 모두 적용될 때까지 대기
    deadline = time.monotonic() + timeout

    while True:
        status = one(node, "SHOW REPLICA STATUS")

        if status["Last_SQL_Errno"] != 0:
            raise RuntimeError(
                f"{node}: SQL Thread error - {status['Last_SQL_Error']}"
            )

        # Retrieved_Gtid_Set의 모든 트랜잭션이
        # @@GLOBAL.gtid_executed에 포함되어 있는지
        result = one(
            node,
            """
            SELECT
                @@GLOBAL.gtid_executed AS executed,
                GTID_SUBSET(
                    %s, @@GLOBAL.gtid_executed
                ) AS applied
            """,
            (received,)
        )

        if result["applied"] == 1:
            break

        if status["Replica_SQL_Running"] != "Yes":
            raise RuntimeError(f"{node}: SQL Thread is not running.")

        if time.monotonic() >= deadline:
            raise TimeoutError(f"{node}: Relay Log drain timed out.")

        time.sleep(0.5)

    # 4. SQL Thread 중지
    execute(node, "STOP REPLICA SQL_THREAD")

    status = one(node, "SHOW REPLICA STATUS")

    if (
        status["Replica_IO_Running"] != "No"
        or status["Replica_SQL_Running"] != "No"
    ):
        raise RuntimeError(f"{node}: Failed to stop replication.")

    # 5. 최종 실행 GTID 확보
    executed = one(
        node,
        "SELECT @@GLOBAL.gtid_executed AS executed"
    )["executed"]

    print(f"    [OK] Drain completed: {node}")
    print(f"    [INFO] Executed GTID: {executed}")

    return {
        "received": received,
        "executed": executed
    }

# 4. Replica Relay Log Drain
def drain_replicas(candidates):
    """모든 승격 후보 Replica의 Relay Log를 Drain한다."""

    snapshots = {}

    for node in candidates:
        snapshots[node] = drain_replica(node)

    return snapshots

# 5. New Primary 최종 확정
def select_new_primary(snapshots, candidates, mode, old_primary_gtid=None):
    """Drain 완료 후 GTID를 비교하여 새로운 Primary를 선정한다."""

    if not candidates:
        raise RuntimeError("No Primary candidates.")

    if set(candidates) != set(snapshots):
        raise RuntimeError("Some Replica snapshots are missing.")

    # 1. 모든 Replica의 GTID를 포함하는 후보 탐색
    latest = []

    for candidate in candidates:
        candidate_gtid = snapshots[candidate]["executed"]
        is_latest = True

        for other in candidates:
            if other == candidate:
                continue

            other_gtid = snapshots[other]["executed"]

            # GTID_SUBSET(other_gtid, candidate_gtid) :
            # other_gtid의 모든 트랜잭션이 candidate_gtid에 포함되어 있는지
            result = one(
                candidate,
                "SELECT GTID_SUBSET(%s, %s) AS ok",
                (other_gtid, candidate_gtid)
            )

            if result["ok"] != 1:
                is_latest = False
                break

        if is_latest:
            latest.append(candidate)

    # 2. 최신 후보가 없다면 GTID 분기 발생
    if not latest:
        raise RuntimeError(
            "GTID sets are divergent. Manual intervention required."
        )

    # 3. 동일한 GTID라면 후보 목록의 첫 번째 서버 선택
    new_primary = latest[0]

    # 4. Switchover: 기존 Primary의 최종 GTID 검증
    if mode == Mode.SWITCHOVER:
        if old_primary_gtid is None:
            raise RuntimeError("Old Primary GTID is missing.")

        result = one(
            new_primary,
            "SELECT GTID_SUBSET(%s, %s) AS ok",
            (
                old_primary_gtid,
                snapshots[new_primary]["executed"]
            )
        )

        if result["ok"] != 1:
            raise RuntimeError(
                f"{new_primary} has not applied all Old Primary transactions."
            )

    print(f"    [OK] New Primary selected: {new_primary}")
    print(f"    [INFO] GTID: {snapshots[new_primary]['executed']}")

    return new_primary

# 6. New Primary 승격
def promote_new_primary(new_primary, old_primary, mode):
    """선정된 Replica를 새로운 Primary로 승격한다."""

    print(f"    [INFO] Promoting {new_primary}...")

    # 1. Old Primary Fencing 상태 재확인
    if mode == Mode.FAILOVER:
        if check_container(old_primary) != "false":
            raise RuntimeError("Old Primary is not fenced.")

    elif mode == Mode.SWITCHOVER:
        status = one(
            old_primary,
            "SELECT @@GLOBAL.super_read_only AS super_read_only"
        )

        if status["super_read_only"] != 1:
            raise RuntimeError("Old Primary is still writable.")

    # 2. Replica 복제 스레드 중지 확인
    status = one(new_primary, "SHOW REPLICA STATUS")

    if not status:
        raise RuntimeError(
            f"{new_primary}: Replication status not found."
        )

    if (
        status["Replica_IO_Running"] != "No"
        or status["Replica_SQL_Running"] != "No"
    ):
        raise RuntimeError(
            f"{new_primary}: Replication threads are still running."
        )

    # 3. 기존 복제 설정 제거
    execute(new_primary, "RESET REPLICA ALL")

    # 4. 쓰기 허용
    execute(new_primary, "SET PERSIST super_read_only = OFF")
    execute(new_primary, "SET PERSIST read_only = OFF")

    # 5. 최종 상태 검증
    result = one(
        new_primary,
        """
        SELECT
            @@GLOBAL.read_only AS read_only,
            @@GLOBAL.super_read_only AS super_read_only,
            @@GLOBAL.log_bin AS log_bin,
            @@GLOBAL.log_replica_updates AS log_replica_updates
        """
    )

    if (
        result["read_only"] != 0
        or result["super_read_only"] != 0
        or result["log_bin"] != 1
        or result["log_replica_updates"] != 1
    ):
        raise RuntimeError(
            f"{new_primary}: Primary promotion verification failed."
        )

    print(f"    [OK] New Primary promoted: {new_primary}")

# 7. 나머지 Replica 재구성
def reconfigure_replicas(candidates, old_primary, new_primary, mode, timeout=30):
    """나머지 Replica의 Source를 새로운 Primary로 변경한다."""
    if mode == Mode.SWITCHOVER:
        replicas = candidates + [old_primary]
    else:
        replicas = candidates

    for node in replicas:
        if node == new_primary:
            continue

        print(f"    [INFO] Reconfiguring {node} -> {new_primary}")

        # 1. 복제 중지 (Drain 단계에서 이미 중지했더라도 재확인)
        execute(node, "STOP REPLICA")

        # 2. 읽기 전용 상태 확인
        status = one(
            node,
            """
            SELECT
                @@GLOBAL.read_only AS read_only,
                @@GLOBAL.super_read_only AS super_read_only
            """
        )

        if status["super_read_only"] != 1:
            raise RuntimeError(
                f"{node}: Replica is not read-only."
            )

        # 3. 새로운 Source 지정
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
            (new_primary, "repl", REPL_PASSWORD)
        )

        # 4. 복제 시작
        execute(node, "START REPLICA")

        # 5. 복제 상태 확인
        deadline = time.monotonic() + timeout

        while True:
            status = one(node, "SHOW REPLICA STATUS")

            if not status:
                raise RuntimeError(
                    f"{node}: Replication configuration missing."
                )

            if status["Last_IO_Errno"] != 0 or status["Last_SQL_Errno"] != 0:
                raise RuntimeError(
                    f"{node}: Replication error - "
                    f"{status['Last_IO_Error']} "
                    f"{status['Last_SQL_Error']}"
                )

            if (
                status["Source_Host"] == new_primary
                and status["Auto_Position"] == 1
                and status["Replica_IO_Running"] == "Yes"
                and status["Replica_SQL_Running"] == "Yes"
            ):
                break

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"{node}: Replication startup timed out."
                )

            time.sleep(0.5)

        print(f"    [OK] Replication started: {node} -> {new_primary}")

# 8. HA 상태 및 데이터 정합성 검증
def verify_cluster(new_primary, old_primary, candidates, mode):
    """Failover 이후 HA 구성 상태를 검증한다."""

    # 1. Old Primary Fencing 확인
    if mode == Mode.FAILOVER:
        if check_container(old_primary) != "false":
            raise RuntimeError("Old Primary is not fenced.")

    elif mode == Mode.SWITCHOVER:
        status = one(
            old_primary,
            "SELECT @@GLOBAL.super_read_only AS super_read_only"
        )

        if status["super_read_only"] != 1:
            raise RuntimeError("Old Primary is still writable.")

    # 2. New Primary 상태 확인
    primary = one(
        new_primary,
        """
        SELECT
            @@GLOBAL.read_only AS read_only,
            @@GLOBAL.super_read_only AS super_read_only,
            @@GLOBAL.log_bin AS log_bin
        """
    )

    if (
        primary["read_only"] != 0
        or primary["super_read_only"] != 0
        or primary["log_bin"] != 1
    ):
        raise RuntimeError(
            f"{new_primary}: Primary status is invalid."
        )

    if one(new_primary, "SHOW REPLICA STATUS"):
        raise RuntimeError(
            f"{new_primary}: Still configured as Replica."
        )

    print(f"    [OK] New Primary writable: {new_primary}")

    # 3. Replica 상태 확인
    for node in candidates:
        if node == new_primary:
            continue

        status = one(
            node,
            """
            SELECT
                @@GLOBAL.read_only AS read_only,
                @@GLOBAL.super_read_only AS super_read_only
            """
        )

        if (
            status["read_only"] != 1
            or status["super_read_only"] != 1
        ):
            raise RuntimeError(
                f"{node}: Replica is writable."
            )

        replica = one(node, "SHOW REPLICA STATUS")

        if (
            not replica
            or replica["Source_Host"] != new_primary
            or replica["Auto_Position"] != 1
            or replica["Replica_IO_Running"] != "Yes"
            or replica["Replica_SQL_Running"] != "Yes"
            or replica["Last_IO_Errno"] != 0
            or replica["Last_SQL_Errno"] != 0
        ):
            raise RuntimeError(
                f"{node}: Replication status is invalid."
            )

        print(f"    [OK] Replica running: {node}")

    print("    [OK] HA cluster verification completed.")

###########################
#   Main Fail over Logic
###########################
def failover():
    """Switchover / Failover 실행. 진행하는 동안 모든 노드를 CONFIGURING 으로 표시한다."""
    with configuring(NODES, "failover"):
        return _failover()


def _failover():
    # ---------------------------
    # 1. Node들의 Status 세팅
    # ---------------------------
    # DB 접속 상태, read_only, 복제 상태, GTID 조회
    # 기존 Primary 식별 및 Switchover / Failover 모드 판단
    print("[1/8] Node들의 상태 확인...")
    statuses, old_primary, mode = collect_node_statuses()

    # ---------------------------
    # 2. New Primary 후보 탐색
    # ---------------------------
    print("\n[2/8] 새로운 Primary 후보를 탐색...")
    candidates = find_candidates(statuses, old_primary)


    # ---------------------------
    # 3. Old Primary 쓰기 차단 및 격리
    # ---------------------------
    # Switchover: super_read_only=ON
    # Failover: 기존 Primary Fencing
    # 격리 실패 시 작업 중단
    print("\n[3/8] Old Primary 쓰기 차단 및 격리...")
    old_primary_gtid = fence_old_primary(old_primary, mode, candidates)

    # ---------------------------
    # 4. Replica Relay Log Drain
    # ---------------------------
    # STOP REPLICA IO_THREAD
    # 수신한 GTID가 모두 적용될 때까지 대기
    # STOP REPLICA
    # 최종 gtid_executed 수집
    print("\n[4/8] 수신한 GTID가 모두 적용될 때까지 대기...")
    snapshots = drain_replicas(candidates)

    # ---------------------------
    # 5. New Primary 최종 확정
    # ---------------------------
    # Replica 간 GTID_SUBSET 비교
    # 가장 최신 GTID를 가진 후보 선택
    # 동일하면 설정한 우선순위 적용
    # GTID가 서로 갈라졌다면 중단
    print("\n[5/8] 새로운 Primary를 최종 선정합니다...")
    new_primary = select_new_primary(
        snapshots,
        candidates,
        mode,
        old_primary_gtid
    )

    # ---------------------------
    # 6. New Primary 승격
    # ---------------------------
    # RESET REPLICA ALL
    # SET PERSIST super_read_only=OFF
    # SET PERSIST read_only=OFF
    # 쓰기 가능 여부 확인
    print("\n[6/8] 새로운 Primary를 승격...")
    promote_new_primary(new_primary, old_primary, mode)

    # ---------------------------
    # 7. 나머지 Replica 재구성
    # ---------------------------
    # CHANGE REPLICATION SOURCE TO
    # SOURCE_HOST = NEW_PRIMARY
    # START REPLICA
    print("\n[7/8] Replica 복제 구성 변경...")
    reconfigure_replicas(
        candidates,
        old_primary,
        new_primary,
        mode
    )

    # ---------------------------
    # 8. HA 상태 검증
    # ---------------------------
    # Primary 1대만 쓰기 허용
    # Replica IO/SQL Thread 확인
    # GTID 동기화 확인
    print("\n[8/8] HA 상태를 검증합니다...")
    verify_cluster(
        new_primary,
        old_primary,
        candidates,
        mode
    )

    print("\n---------------------")
    print(f"{mode} completed!", )
    print("---------------------")
    
    return new_primary


if __name__ == "__main__":
    try:
        failover()
    except Exception as exc:
        print(f"[ERROR] {exc}")
        raise SystemExit(1)
