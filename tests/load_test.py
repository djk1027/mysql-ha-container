#!/usr/bin/env python3
"""ProxySQL 경유 부하 테스트: 쓰기/읽기가 어느 노드로 가는지 확인한다.

  1. 테스트 스키마 생성   : appdb.lb_test 테이블
  2. 대용량 INSERT        : 기본 10만 건 (배치 단위 commit)
  3. 복제 반영 대기       : 모든 노드의 건수가 같아질 때까지
  4. 대용량 조회          : 여러 스레드로 SELECT 반복
  5. 정리                 : 테이블 DROP (테스트가 실패해도 실행, --keep 이면 생략)

단계만 따로 실행할 수 있다 (테이블은 남겨 둠, --drop 을 주면 정리):
  --only insert   1~3 (스키마 생성 + INSERT + 복제 대기)
  --only select   4   (남아 있는 테이블로 조회만, 같은 데이터로 조건을 바꿔 반복 측정할 때)
  --only continuous   Ctrl+C 전까지 읽기/쓰기 혼합 부하를 계속 준다 (장애조치 중 동작 확인용)
                      연결이 끊기면 재접속하며, --interval 초마다 처리량·오류·노드별 분산을 출력.
                      테이블이 없으면 먼저 생성+INSERT 한다.

노드별 분산은 단계별 ProxySQL stats_mysql_connection_pool 의 노드별 Queries 증가량으로 본다.
(조회에 @@server_id 같은 변수를 넣으면 ProxySQL이 그 세션을 한 노드에 고정해 분산이 안 되므로 쓰지 않음)

실행: .venv/bin/python -m tests.load_test [--only insert|select|continuous] [--rows 100000] [--reads 5000] [--threads 8]
     .venv/bin/python -m tests.load_test --only continuous --threads 8 --write-ratio 0.2 --interval 5
"""

import argparse
import os
import random
import subprocess
import sys
import threading
import time
from collections import Counter

import pymysql

from apps.common import NODES, one

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 6033
PROXYSQL_CONTAINER = "proxysql"
APP_USER = "app"
APP_PASSWORD = os.environ["MYSQL_APP_PASSWORD"]   # apps.common 이 .env 를 로드함
APP_DB = os.environ.get("MYSQL_APP_DB", "appdb")
TABLE = "lb_test"

WRITER_HG, READER_HG = 10, 20


# ---------------------------------------------------------------- 접속
def proxy_conn(autocommit=False, read_timeout=60):
    """애플리케이션과 같은 경로: ProxySQL(6033)로 app 계정 접속.

    autocommit=False 면 첫 쿼리부터 트랜잭션이 열려, ProxySQL이 그 세션을 한 노드에 고정한다.
    조회는 autocommit=True 로 열어야 쿼리마다 Reader 그룹에 분배된다.
    """
    return pymysql.connect(
        host=PROXY_HOST, port=PROXY_PORT, user=APP_USER, password=APP_PASSWORD,
        database=APP_DB, autocommit=autocommit, connect_timeout=5, read_timeout=read_timeout,
    )


def proxy_admin(sql):
    """ProxySQL 관리 SQL (관리 포트는 컨테이너 안에서만 열려 있음)."""
    out = subprocess.run(
        ["docker", "exec", "-i", PROXYSQL_CONTAINER,
         "mysql", "-uadmin", "-padmin", "-h127.0.0.1", "-P6032", "-N", "-B"],
        input=sql, capture_output=True, text=True, check=True,
    ).stdout
    return [line.split("\t") for line in out.splitlines() if line]


# ---------------------------------------------------------------- ProxySQL 통계
def pool_queries():
    """{(hostgroup, host): 누적 Queries}"""
    rows = proxy_admin("SELECT hostgroup, srv_host, Queries FROM stats_mysql_connection_pool;")
    return {(int(hg), host): int(q) for hg, host, q in rows}


def print_pool_delta(title, before, after):
    """단계 동안 노드별로 처리한 쿼리 수를 출력하고, 쿼리를 받은 노드 수를 돌려준다."""
    print(f"  [ProxySQL] {title} 동안 노드별 처리 쿼리 수")
    keys = sorted(set(before) | set(after))
    deltas = {k: after.get(k, 0) - before.get(k, 0) for k in keys}
    total = sum(deltas.values()) or 1
    for (hg, host), d in deltas.items():
        if d:
            role = {WRITER_HG: "Writer", READER_HG: "Reader"}.get(hg, f"HG{hg}")
            print(f"    HG {hg:<3}({role:<6}) {host:<16} {d:>8,}  ({d / total:5.1%})")
    return len({host for (_, host), d in deltas.items() if d})


def print_routing():
    print("  [ProxySQL] 현재 라우팅")
    for hg, host, status in proxy_admin(
        "SELECT hostgroup_id, hostname, status FROM runtime_mysql_servers ORDER BY 1, 2;"
    ):
        print(f"    HG {hg} {host:<16} {status}")
    rules = proxy_admin("SELECT COUNT(*) FROM runtime_mysql_query_rules WHERE active=1;")[0][0]
    print(f"    활성 쿼리 규칙: {rules}개"
          + ("  → 규칙이 없으면 모든 쿼리가 default_hostgroup(Writer)로 감" if rules == "0" else ""))


# ---------------------------------------------------------------- 1. 스키마
def create_schema():
    with proxy_conn() as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS `{TABLE}`")
        cur.execute(f"""
            CREATE TABLE `{TABLE}` (
                id         BIGINT AUTO_INCREMENT PRIMARY KEY,
                user_id    INT           NOT NULL,
                name       VARCHAR(50)   NOT NULL,
                amount     DECIMAL(10,2) NOT NULL,
                created_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
                KEY idx_user (user_id)
            ) ENGINE=InnoDB
        """)
        conn.commit()


# ---------------------------------------------------------------- 2. INSERT
def bulk_insert(rows, batch, users):
    sql = f"INSERT INTO `{TABLE}` (user_id, name, amount) VALUES (%s, %s, %s)"
    rnd = random.Random(42)
    done = 0
    with proxy_conn() as conn, conn.cursor() as cur:
        while done < rows:
            n = min(batch, rows - done)
            cur.executemany(sql, [
                (rnd.randint(1, users), f"user-{done + i}", round(rnd.uniform(1, 10000), 2))
                for i in range(n)
            ])
            conn.commit()
            done += n
            if done % (batch * 20) == 0 or done == rows:
                print(f"    {done:>9,} / {rows:,}")


# ---------------------------------------------------------------- 3. 복제 대기
def wait_replication(rows, timeout=120):
    """모든 노드에 rows 건이 반영될 때까지 대기 (Replica 조회 결과가 같아야 비교 가능)."""
    deadline = time.monotonic() + timeout
    while True:
        counts = {}
        for node in NODES:
            try:
                counts[node] = one(node, f"SELECT COUNT(*) AS c FROM `{APP_DB}`.`{TABLE}`",
                                   read_timeout=10)["c"]
            except pymysql.MySQLError as exc:
                counts[node] = f"오류: {exc.args[0]}"
        if all(c == rows for c in counts.values()):
            print("    " + ", ".join(f"{n}={c:,}" for n, c in counts.items()))
            return True
        if time.monotonic() > deadline:
            print(f"    [WARN] {timeout}초 안에 복제가 맞춰지지 않음: {counts}")
            return False
        time.sleep(1)


# ---------------------------------------------------------------- 4. 조회
READ_QUERIES = [
    ("pk",    f"SELECT id, name FROM `{TABLE}` WHERE id = %s",                lambda r, u: (r.randint(1, ROWS),)),
    ("index", f"SELECT COUNT(*), SUM(amount) FROM `{TABLE}` WHERE user_id = %s", lambda r, u: (r.randint(1, u),)),
    ("range", f"SELECT COUNT(*), AVG(amount) FROM `{TABLE}` WHERE id BETWEEN %s AND %s",
              lambda r, u: (lambda s: (s, s + 1000))(r.randint(1, max(1, ROWS - 1000)))),
]
ROWS = 0   # main 에서 설정 (조회 범위용)


def read_worker(n, users, seed, by_kind, errors, lock):
    rnd = random.Random(seed)
    local_kind, local_err = Counter(), 0
    with proxy_conn(autocommit=True) as conn, conn.cursor() as cur:
        for _ in range(n):
            kind, sql, args = rnd.choice(READ_QUERIES)
            try:
                cur.execute(sql, args(rnd, users))
                cur.fetchall()
                local_kind[kind] += 1
            except pymysql.MySQLError:
                local_err += 1
    with lock:
        by_kind.update(local_kind)
        errors[0] += local_err


def bulk_read(reads, threads, users):
    by_kind, errors, lock = Counter(), [0], threading.Lock()
    per = [reads // threads + (1 if i < reads % threads else 0) for i in range(threads)]
    workers = [
        threading.Thread(target=read_worker,
                         args=(per[i], users, 1000 + i, by_kind, errors, lock))
        for i in range(threads)
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    return by_kind, errors[0]


# ---------------------------------------------------------------- 4'. 지속 부하
class LoadStats:
    """스레드들이 함께 쓰는 성공/오류 카운터. take() 는 구간 값을 돌려주고 비운다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ok, self.err = Counter(), Counter()
        self.total_ok, self.total_err = Counter(), Counter()

    def add_ok(self, kind):
        with self.lock:
            self.ok[kind] += 1

    def add_err(self, exc):
        code = exc.args[0] if exc.args else "?"
        msg = str(exc.args[1]) if len(exc.args) > 1 else str(exc)
        with self.lock:
            self.err[f"{type(exc).__name__}({code}) {msg[:60]}"] += 1

    def take(self):
        with self.lock:
            ok, err = self.ok, self.err
            self.ok, self.err = Counter(), Counter()
            self.total_ok.update(ok)
            self.total_err.update(err)
        return ok, err


def continuous_worker(idx, users, write_ratio, stop, stats):
    """stop 이 설정될 때까지 쿼리를 반복. 오류가 나면 연결을 버리고 재접속한다."""
    rnd = random.Random(2000 + idx)
    insert_sql = f"INSERT INTO `{TABLE}` (user_id, name, amount) VALUES (%s, %s, %s)"
    conn = None
    while not stop.is_set():
        try:
            if conn is None:
                conn = proxy_conn(autocommit=True, read_timeout=10)
            with conn.cursor() as cur:
                if rnd.random() < write_ratio:
                    kind = "write"
                    cur.execute(insert_sql, (rnd.randint(1, users), f"cont-{idx}",
                                             round(rnd.uniform(1, 10000), 2)))
                else:
                    kind, sql, args = rnd.choice(READ_QUERIES)
                    cur.execute(sql, args(rnd, users))
                    cur.fetchall()
            stats.add_ok(kind)
        except pymysql.MySQLError as exc:
            stats.add_err(exc)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            stop.wait(0.5)   # 장애 중 재접속 폭주 방지
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def safe_pool_queries():
    try:
        return pool_queries()
    except (subprocess.CalledProcessError, OSError, ValueError):
        return None


def format_pool_delta(before, after):
    """'host:W=12,R=34' 형태의 한 줄 요약."""
    if before is None or after is None:
        return "ProxySQL 통계 조회 실패"
    per_host = {}
    for key in sorted(set(before) | set(after)):
        d = after.get(key, 0) - before.get(key, 0)
        if d:
            hg, host = key
            role = {WRITER_HG: "W", READER_HG: "R"}.get(hg, f"HG{hg}")
            per_host.setdefault(host, []).append(f"{role}={d:,}")
    return "  ".join(f"{h}:{','.join(v)}" for h, v in per_host.items()) or "-"


def run_continuous(args):
    """Ctrl+C 전까지 읽기/쓰기 혼합 부하. 오류 없이 끝났는지를 돌려준다."""
    print(f"\n[지속 부하] 스레드 {args.threads}, 쓰기 비율 {args.write_ratio:.0%}, "
          f"{args.interval}초마다 출력 (중단: Ctrl+C)")
    stop, stats = threading.Event(), LoadStats()
    workers = [
        threading.Thread(target=continuous_worker, daemon=True,
                         args=(i, args.users, args.write_ratio, stop, stats))
        for i in range(args.threads)
    ]
    start = time.monotonic()
    first_pool = prev_pool = safe_pool_queries()
    for w in workers:
        w.start()

    last = start
    try:
        while True:
            time.sleep(args.interval)
            ok, err = stats.take()
            now = time.monotonic()
            pool = safe_pool_queries()
            span = now - last
            reads = sum(v for k, v in ok.items() if k != "write")
            writes = ok["write"]
            print(f"  [{time.strftime('%H:%M:%S')}] {(reads + writes) / span:>8,.0f} qps "
                  f"(R {reads / span:,.0f} / W {writes / span:,.0f})  오류 {sum(err.values()):,}"
                  f"  | {format_pool_delta(prev_pool, pool)}")
            for msg, n in err.most_common(3):
                print(f"      ! {n:,} × {msg}")
            if pool is not None:
                prev_pool = pool
            last = now
    except KeyboardInterrupt:
        print("\n  중단 요청 → 스레드 종료 대기")
    finally:
        stop.set()
        for w in workers:
            w.join(timeout=15)

    stats.take()   # 마지막 구간 반영
    elapsed = time.monotonic() - start
    total_ok, total_err = stats.total_ok, stats.total_err
    n_ok = sum(total_ok.values())
    print(f"\n[지속 부하 요약] {elapsed:,.1f}초, 성공 {n_ok:,}건 ({n_ok / elapsed:,.0f} qps), "
          f"오류 {sum(total_err.values()):,}건")
    print("    쿼리 종류: " + ", ".join(f"{k}={v:,}" for k, v in sorted(total_ok.items())))
    for msg, n in total_err.most_common(5):
        print(f"    오류 {n:,} × {msg}")
    final_pool = safe_pool_queries()
    if first_pool is not None and final_pool is not None:
        print_pool_delta("지속 부하", first_pool, final_pool)
    return not total_err


def table_info():
    """기존 테이블의 (최대 id, 최대 user_id, 건수). 테이블이 없으면 None."""
    try:
        with proxy_conn(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT MAX(id), MAX(user_id), COUNT(*) FROM `{TABLE}`")
            return cur.fetchone()
    except pymysql.err.ProgrammingError as exc:
        if exc.args[0] == 1146:   # Table doesn't exist
            return None
        raise


# ---------------------------------------------------------------- 5. 정리
def drop_schema():
    with proxy_conn() as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS `{TABLE}`")
        conn.commit()


# ---------------------------------------------------------------- main
def run_insert(args):
    """스키마 생성 → INSERT → 복제 대기. 성공 여부를 돌려준다."""
    print("\n[스키마] 테스트 테이블 생성")
    create_schema()
    print(f"    {APP_DB}.{TABLE} 생성")

    print(f"\n[INSERT] {args.rows:,}건, 배치 {args.batch:,}")
    before = pool_queries()
    t = time.monotonic()
    bulk_insert(args.rows, args.batch, args.users)
    elapsed = time.monotonic() - t
    print(f"    완료: {elapsed:.1f}초, {args.rows / elapsed:,.0f} rows/s")
    print_pool_delta("INSERT", before, pool_queries())

    print("\n[복제] 모든 노드 반영 대기")
    return wait_replication(args.rows)


def run_select(args):
    """조회 부하. 성공 여부를 돌려준다."""
    print(f"\n[SELECT] {args.reads:,}회, 스레드 {args.threads}")
    before = pool_queries()
    t = time.monotonic()
    by_kind, errors = bulk_read(args.reads, args.threads, args.users)
    elapsed = time.monotonic() - t
    print(f"    완료: {elapsed:.1f}초, {args.reads / elapsed:,.0f} qps, 오류 {errors}건")
    print("    쿼리 종류: " + ", ".join(f"{k}={v:,}" for k, v in sorted(by_kind.items())))
    read_nodes = print_pool_delta("SELECT", before, pool_queries())

    print("\n[결과]")
    if read_nodes <= 1:
        print("  조회가 노드 1대로만 갔습니다 → 읽기 분산 안 됨"
              " (SELECT를 Reader 그룹으로 보내는 mysql_query_rules 필요)")
    else:
        print(f"  조회가 {read_nodes}개 노드로 분산되었습니다")
    return errors == 0


def main():
    global ROWS
    parser = argparse.ArgumentParser(description="ProxySQL 경유 대용량 INSERT/SELECT 분산 테스트")
    parser.add_argument("--only", choices=["insert", "select", "continuous"],
                        help="한 단계만 실행 (insert: 생성+INSERT, select: 기존 테이블 조회, "
                             "continuous: Ctrl+C 전까지 읽기/쓰기 부하). 테이블은 남김")
    parser.add_argument("--rows", type=int, default=1_000_000, help="INSERT 건수")
    parser.add_argument("--batch", type=int, default=10_000, help="INSERT 배치 크기 (배치마다 commit)")
    parser.add_argument("--reads", type=int, default=10_000, help="SELECT 횟수")
    parser.add_argument("--threads", type=int, default=8, help="SELECT 동시 실행 스레드 수")
    parser.add_argument("--users", type=int, default=1_000, help="user_id 범위 (인덱스 조회용)")
    parser.add_argument("--write-ratio", type=float, default=0.2,
                        help="continuous: 전체 쿼리 중 INSERT 비율 (0~1)")
    parser.add_argument("--interval", type=float, default=5, help="continuous: 통계 출력 주기(초)")
    cleanup = parser.add_mutually_exclusive_group()
    cleanup.add_argument("--keep", action="store_true", help="종료 후 테이블을 지우지 않음")
    cleanup.add_argument("--drop", action="store_true", help="종료 후 테이블을 지움 (--only 에서도)")
    args = parser.parse_args()

    # 전체 실행은 기본 DROP, 단계 실행은 다음 단계에서 쓰도록 기본 유지
    drop = args.drop or (args.only is None and not args.keep)

    print(f"ProxySQL {PROXY_HOST}:{PROXY_PORT} (user={APP_USER}, db={APP_DB}, table={TABLE})")
    print_routing()

    if not 0 <= args.write_ratio <= 1:
        parser.error("--write-ratio 는 0~1 사이여야 합니다")

    need_insert = args.only != "select"
    if args.only == "continuous":
        info = table_info()
        if info is None or not info[2]:
            print(f"\n[기존 테이블 없음] {APP_DB}.{TABLE} 를 먼저 생성+INSERT ({args.rows:,}건)")
            ROWS = args.rows
        else:
            need_insert = False
            ROWS, args.users = info[0], info[1]
            print(f"\n[기존 테이블] {APP_DB}.{TABLE} {info[2]:,}건 사용")
    elif args.only == "select":
        info = table_info()
        if info is None or not info[2]:
            print(f"\n[ERROR] {APP_DB}.{TABLE} 테이블이 없거나 비어 있습니다."
                  " 먼저 실행: .venv/bin/python -m tests.load_test --only insert")
            return 1
        ROWS, args.users = info[0], info[1]   # 조회 범위를 실제 데이터에 맞춤
        print(f"\n[기존 테이블] {APP_DB}.{TABLE} {info[2]:,}건 사용")
    else:
        ROWS = args.rows

    ok = True
    try:
        if need_insert:
            ok &= run_insert(args)

        if args.only == "continuous":
            ok &= run_continuous(args)
        elif args.only != "insert":
            ok &= run_select(args)
    except Exception as exc:
        ok = False
        print(f"\n[ERROR] {type(exc).__name__}: {exc}")
    finally:
        if not drop:
            print(f"\n[정리] {APP_DB}.{TABLE} 유지 (지우려면 --drop)")
        else:
            try:
                drop_schema()
                print(f"\n[정리] {APP_DB}.{TABLE} DROP")
            except pymysql.MySQLError as exc:
                ok = False
                print(f"\n[정리 실패] DROP TABLE: {exc} (수동 삭제 필요)")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
