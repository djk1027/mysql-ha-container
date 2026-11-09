#!/usr/bin/env python3

import os
import subprocess
import time
from pathlib import Path

import pymysql
from dotenv import load_dotenv


# MySQL 구성
OLD_PRIMARY = "mysql-server-1"

# 앞에 있을수록 승격 우선순위가 높음
PRIORITY = [
    "mysql-server-2",
    "mysql-server-3",
]

PORTS = {
    "mysql-server-1": 3376,
    "mysql-server-2": 3377,
    "mysql-server-3": 3378,
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

ROOT_PASSWORD = os.environ["MYSQL_ROOT_PASSWORD"]
REPL_PASSWORD = os.environ["MYSQL_REPL_PASSWORD"]


def execute(node, sql, params=None):
    """MySQL 쿼리를 실행하고 결과를 반환한다."""
    conn = pymysql.connect(
        host="127.0.0.1",
        port=PORTS[node],
        user="root",
        password=ROOT_PASSWORD,
        autocommit=True,
        connect_timeout=3,
        read_timeout=45,
        cursorclass=pymysql.cursors.DictCursor,
    )

    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description else ()
    finally:
        conn.close()


def one(node, sql, params=None):
    rows = execute(node, sql, params)
    return rows[0] if rows else None


def check_replica(node):
    """승격 후보로 사용할 수 있는 Replica인지 확인한다."""
    status = one(node, "SHOW REPLICA STATUS")
    readonly = one(
        node,
        "SELECT @@GLOBAL.super_read_only AS value"
    )

    return (
        status is not None
        and status["Source_Host"] == OLD_PRIMARY
        and status["Auto_Position"] == 1
        and status["Replica_SQL_Running"] == "Yes"
        and status["Last_SQL_Errno"] == 0
        and readonly["value"] == 1
    )


def fence_primary():
    """기존 Primary를 중지하고 중지 상태를 검증한다."""
    print(f"[INFO] Fencing {OLD_PRIMARY}...")

    subprocess.run(
        ["docker", "stop", "-t", "10", OLD_PRIMARY],
        check=True,
        capture_output=True,
        text=True,
    )

    result = subprocess.run(
        [
            "docker", "inspect",
            "-f", "{{.State.Running}}",
            OLD_PRIMARY,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    if result.stdout.strip() != "false":
        raise RuntimeError("Old Primary fencing failed.")


def drain_replica(node):
    """수신한 Relay Log의 GTID를 모두 적용한다."""
    execute(node, "STOP REPLICA IO_THREAD")

    status = one(node, "SHOW REPLICA STATUS")
    received = status["Retrieved_Gtid_Set"] or ""

    result = one(
        node,
        "SELECT WAIT_FOR_EXECUTED_GTID_SET(%s, 30) AS waited",
        (received,),
    )

    if result["waited"] != 0:
        raise RuntimeError(f"{node}: relay log apply timeout")

    execute(node, "STOP REPLICA")

    executed = one(
        node,
        "SELECT @@GLOBAL.gtid_executed AS value"
    )["value"]

    return {"received": received, "executed": executed}


def select_candidate(snapshots):
    """GTID를 검증하고, 승격 가능한 서버 중 우선순위로 선정한다."""
    for candidate in PRIORITY:
        if candidate not in snapshots:
            continue

        candidate_gtid = snapshots[candidate]["executed"]
        is_latest = True

        for other, snapshot in snapshots.items():
            if other == candidate:
                continue

            result = one(
                candidate,
                "SELECT GTID_SUBSET(%s, %s) AS ok",
                (snapshot["received"], candidate_gtid),
            )

            if result["ok"] != 1:
                is_latest = False
                break

        if is_latest:
            return candidate

    raise RuntimeError(
        "승격 가능한 GTID 상위 후보가 없습니다. 수동 점검이 필요합니다."
    )


def promote(node):
    """Replica를 쓰기 가능한 Primary로 승격한다."""
    print(f"[INFO] Promoting {node}...")

    execute(node, "RESET REPLICA ALL")
    execute(node, "SET GLOBAL super_read_only = OFF")
    execute(node, "SET GLOBAL read_only = OFF")

    result = one(
        node,
        "SELECT @@GLOBAL.read_only AS ro, "
        "@@GLOBAL.super_read_only AS super_ro",
    )

    if result["ro"] != 0 or result["super_ro"] != 0:
        raise RuntimeError(f"{node}: promotion verification failed")


def reconnect_replica(node, new_primary):
    """나머지 Replica의 복제 대상을 변경한다."""
    print(f"[INFO] Reconfiguring {node}...")

    execute(
        node,
        """
        CHANGE REPLICATION SOURCE TO
            SOURCE_HOST=%s,
            SOURCE_PORT=3306,
            SOURCE_USER='repl',
            SOURCE_PASSWORD=%s,
            SOURCE_AUTO_POSITION=1,
            GET_SOURCE_PUBLIC_KEY=1
        """,
        (new_primary, REPL_PASSWORD),
    )

    execute(node, "START REPLICA")

    for _ in range(15):
        status = one(node, "SHOW REPLICA STATUS")

        if (
            status["Source_Host"] == new_primary
            and status["Replica_IO_Running"] == "Yes"
            and status["Replica_SQL_Running"] == "Yes"
        ):
            print(f"[OK] {node}: replication running")
            return

        time.sleep(1)

    raise RuntimeError(f"{node}: replication did not become healthy")


def failover():
    print("[1/5] Checking promotion candidates...")

    candidates = []

    for node in PRIORITY:
        try:
            if check_replica(node):
                candidates.append(node)
                print(f"[OK] {node}: eligible")
            else:
                print(f"[WARN] {node}: not eligible")
        except pymysql.MySQLError as exc:
            print(f"[WARN] {node}: unavailable ({exc})")

    if not candidates:
        raise RuntimeError("승격 가능한 Replica가 없습니다.")

    print("[2/5] Fencing old Primary...")
    fence_primary()

    print("[3/5] Draining relay logs...")
    snapshots = {}

    for node in candidates:
        try:
            snapshots[node] = drain_replica(node)
            print(f"[OK] {node}: relay logs applied")
        except (pymysql.MySQLError, RuntimeError) as exc:
            print(f"[WARN] {node}: drain failed ({exc})")

    if not snapshots:
        raise RuntimeError("Relay Log 적용에 성공한 Replica가 없습니다.")

    print("[4/5] Selecting and promoting Primary...")
    new_primary = select_candidate(snapshots)
    promote(new_primary)

    print("[5/5] Reconfiguring remaining replicas...")
    for node in PRIORITY:
        if node == new_primary:
            continue

        if node not in snapshots:
            print(f"[WARN] {node}: manual recovery required")
            continue

        try:
            reconnect_replica(node, new_primary)
        except (pymysql.MySQLError, RuntimeError) as exc:
            print(f"[WARN] {node}: reconfiguration failed ({exc})")

    print()
    print("[OK] Failover completed")
    print(f"[OK] New Primary: {new_primary}")
    print(f"[OK] Client port: {PORTS[new_primary]}")
    print(f"[WARN] Do not restart {OLD_PRIMARY} before rejoining it.")


if __name__ == "__main__":
    try:
        failover()
    except Exception as exc:
        print(f"[ERROR] {exc}")
        raise SystemExit(1)
