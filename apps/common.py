import json
from pathlib import Path

import os
import subprocess
import pymysql

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

COMPOSE_FILE = PROJECT_ROOT / "compose.yaml"
CONFIG_FILE = PROJECT_ROOT / "state/nodes.json"

load_dotenv(PROJECT_ROOT / ".env")

ROOT_PASSWORD = os.environ["MYSQL_ROOT_PASSWORD"]
REPL_PASSWORD = os.environ["MYSQL_REPL_PASSWORD"]

with open(CONFIG_FILE, encoding="utf-8") as f:
    NODES = json.load(f)

#-------------------
# Container function
#-------------------
def stop_container(node):
    """Docker 컨테이너를 중지한다."""

    print(f"    [INFO] Stopping container: {node}")

    subprocess.run(
        ["docker", "stop", "-t", "10", node],
        check=True,
        capture_output=True,
        text=True
    )

    if check_container(node) != "false":
        raise RuntimeError(f"Failed to stop container: {node}")

def check_container(node):
    """Docker 컨테이너 실행 상태를 반환한다."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", node],
        check=True,
        capture_output=True,
        text=True
    )

    return result.stdout.strip()

#-------------------
# MySQL function
#-------------------
def execute(node, sql, params=None, read_timeout=45):
    """MySQL 쿼리를 실행하고 결과를 반환한다."""
    conn = pymysql.connect(
        host="127.0.0.1",
        port=NODES[node],
        user="root",
        password=ROOT_PASSWORD,
        autocommit=True,
        connect_timeout=3,
        read_timeout=read_timeout,
        cursorclass=pymysql.cursors.DictCursor,
    )

    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description else ()
    finally:
        conn.close()


def one(node, sql, params=None, read_timeout=45):
    rows = execute(node, sql, params, read_timeout)
    return rows[0] if rows else None


def get_node_status(node, read_timeout=45):
    """각 Node들의 정보를 조회한다."""
    variables = one(node, """
        SELECT
            @@GLOBAL.server_id AS server_id,
            @@GLOBAL.read_only AS read_only,
            @@GLOBAL.super_read_only AS super_read_only,
            @@GLOBAL.gtid_mode AS gtid_mode,
            @@GLOBAL.gtid_executed AS gtid_executed
    """, read_timeout=read_timeout)

    replica = one(node, "SHOW REPLICA STATUS", read_timeout=read_timeout)

    # Primary 판별 조건
    # 1. SHOW REPLICA STATUS 결과가 없음
    # 2. read_only == 0
    # 3. super_read_only == 0
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
