#!/usr/bin/env python3

import sys
import pymysql

from apps.common import one
from apps.common import NODES

def get_node_status(node):
    variables = one(node, """
        SELECT
            @@GLOBAL.server_id AS server_id,
            @@GLOBAL.read_only AS read_only,
            @@GLOBAL.super_read_only AS super_read_only,
            @@GLOBAL.gtid_mode AS gtid_mode,
            @@GLOBAL.gtid_executed AS gtid_executed
    """)

    replica = one(node, "SHOW REPLICA STATUS")

    is_primary = (
        replica is None
        and variables["read_only"] == 0
        and variables["super_read_only"] == 0
    )

    return {
        "name": node,
        "server_id": variables["server_id"],
        "read_only": variables["read_only"],
        "super_read_only": variables["super_read_only"],
        "gtid_mode": variables["gtid_mode"],
        "gtid_executed": variables["gtid_executed"],
        "replica": replica,
        "is_primary": is_primary
    }

def main():
    print("====================================")
    print(" MySQL HA Cluster Status")
    print("====================================")

    statuses = {}
    errors = []

    for node in NODES:
        try:
            statuses[node] = get_node_status(node)
        except pymysql.MySQLError as exc:
            errors.append(f"{node}: Connection failed - {exc}")
            print(f"[{node}]: unavailable")

    # Primary validation check
    primaries = [
        node
        for node, status in statuses.items()
        if status["is_primary"]
    ]

    #if len(primaries) == 0:
    #    raise RuntimeError("No primary candidate detected.")

    if len(primaries) > 1:
        raise RuntimeError("Multiple primary candidates detected.")

    PRIMARY = primaries[0] if primaries else None

    for node, status in statuses.items():
        replica = status["replica"]

        print(f"\n[{node}]")
        print(f"  Role             : {'PRIMARY' if status['is_primary'] else 'REPLICA'}")
        print(f"  Server ID        : {status['server_id']}")
        print(f"  Read Only        : {status['read_only']}")
        print(f"  Super Read Only  : {status['super_read_only']}")
        print(f"  GTID Mode        : {status['gtid_mode']}")

        if status["gtid_mode"] != "ON":
            errors.append(f"{node}: GTID is disabled")

        if status['is_primary']:
            if status["read_only"] or status["super_read_only"]:
                errors.append(f"{node}: Primary is read-only")
            if replica is not None:
                errors.append(f"{node}: Primary has replication configured")
            continue

        if not status["super_read_only"]:
            errors.append(f"{node}: Replica is writable")

        if replica is None:
            errors.append(f"{node}: Replication not configured")
            continue

        print(f"  Source Host      : {replica['Source_Host']}")
        print(f"  IO Thread        : {replica['Replica_IO_Running']}")
        print(f"  SQL Thread       : {replica['Replica_SQL_Running']}")
        print(f"  Seconds Behind   : {replica['Seconds_Behind_Source']}")
        print(f"  Last IO Error    : {replica['Last_IO_Error'] or '-'}")
        print(f"  Last SQL Error   : {replica['Last_SQL_Error'] or '-'}")

        if replica["Source_Host"] != PRIMARY:
            errors.append(f"{node}: Unexpected replication source")

        if replica["Replica_IO_Running"] != "Yes":
            errors.append(f"{node}: Replication IO is stopped")

        if replica["Replica_SQL_Running"] != "Yes":
            errors.append(f"{node}: Replication SQL is stopped")

        if replica["Last_IO_Errno"] or replica["Last_SQL_Errno"]:
            errors.append(f"{node}: Replication error detected")

        # Primary GTID가 Replica에 모두 적용됐는지 확인
        if PRIMARY in statuses:
            primary_gtid = statuses[PRIMARY]["gtid_executed"]

            gtid_check = one(
                node,
                "SELECT GTID_SUBSET(%s, %s) AS synced",
                (primary_gtid, status["gtid_executed"]),
            )

            synced = gtid_check["synced"] == 1
            print(f"  GTID Synced      : {'YES' if synced else 'NO'}")

            if not synced:
                errors.append(f"{node}: GTID is behind Primary")

    print("\n====================================")

    if errors:
        print("[FAIL] Cluster validation failed")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("[OK] All MySQL nodes and replication are healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
