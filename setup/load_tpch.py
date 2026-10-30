"""
load_tpch.py - one-off helper that loads TPC-H .tbl files into PostgreSQL.

This is a SETUP tool, not one of the four submitted program files.
The submitted code (project.py and friends) works on whatever database
the user points it at; this script only builds our own test database.

What it does (follows the Appendix of the Project 2 brief):
  1. Creates the database (default name "TPC-H") if it does not exist.
  2. Creates the 8 TPC-H tables (same columns and types as Appendix II).
  3. Loads each .tbl file, stripping the trailing '|' on every line
     (the Appendix asks you to do this by hand; we do it while streaming).
  4. Adds the primary keys and foreign keys from Appendix II.
  5. Adds a few secondary indexes (see EXTRA_INDEXES below).
  6. Runs ANALYZE so the optimizer has statistics.

Usage (Windows or Linux):
  python load_tpch.py --data-dir C:\\tpch_data --password <your postgres password>
  python load_tpch.py --help     for all options
"""

import argparse
import io
import os
import sys
import time

import psycopg2
from psycopg2 import sql


# ---------------------------------------------------------------------------
# Table definitions (Appendix II of the brief, without the PK/FK clauses,
# which are added after loading because bulk loading is much faster that way)
# ---------------------------------------------------------------------------
TABLES = {
    "region": """
        r_regionkey  integer      NOT NULL,
        r_name       char(25)     NOT NULL,
        r_comment    varchar(152)""",
    "nation": """
        n_nationkey  integer      NOT NULL,
        n_name       char(25)     NOT NULL,
        n_regionkey  integer      NOT NULL,
        n_comment    varchar(152)""",
    "part": """
        p_partkey     integer        NOT NULL,
        p_name        varchar(55)    NOT NULL,
        p_mfgr        char(25)       NOT NULL,
        p_brand       char(10)       NOT NULL,
        p_type        varchar(25)    NOT NULL,
        p_size        integer        NOT NULL,
        p_container   char(10)       NOT NULL,
        p_retailprice numeric(15,2)  NOT NULL,
        p_comment     varchar(23)    NOT NULL""",
    "supplier": """
        s_suppkey    integer        NOT NULL,
        s_name       char(25)       NOT NULL,
        s_address    varchar(40)    NOT NULL,
        s_nationkey  integer        NOT NULL,
        s_phone      char(15)       NOT NULL,
        s_acctbal    numeric(15,2)  NOT NULL,
        s_comment    varchar(101)   NOT NULL""",
    "partsupp": """
        ps_partkey    integer        NOT NULL,
        ps_suppkey    integer        NOT NULL,
        ps_availqty   integer        NOT NULL,
        ps_supplycost numeric(15,2)  NOT NULL,
        ps_comment    varchar(199)   NOT NULL""",
    "customer": """
        c_custkey    integer        NOT NULL,
        c_name       varchar(25)    NOT NULL,
        c_address    varchar(40)    NOT NULL,
        c_nationkey  integer        NOT NULL,
        c_phone      char(15)       NOT NULL,
        c_acctbal    numeric(15,2)  NOT NULL,
        c_mktsegment char(10)       NOT NULL,
        c_comment    varchar(117)   NOT NULL""",
    "orders": """
        o_orderkey      integer        NOT NULL,
        o_custkey       integer        NOT NULL,
        o_orderstatus   char(1)        NOT NULL,
        o_totalprice    numeric(15,2)  NOT NULL,
        o_orderdate     date           NOT NULL,
        o_orderpriority char(15)       NOT NULL,
        o_clerk         char(15)       NOT NULL,
        o_shippriority  integer        NOT NULL,
        o_comment       varchar(79)    NOT NULL""",
    "lineitem": """
        l_orderkey      integer        NOT NULL,
        l_partkey       integer        NOT NULL,
        l_suppkey       integer        NOT NULL,
        l_linenumber    integer        NOT NULL,
        l_quantity      numeric(15,2)  NOT NULL,
        l_extendedprice numeric(15,2)  NOT NULL,
        l_discount      numeric(15,2)  NOT NULL,
        l_tax           numeric(15,2)  NOT NULL,
        l_returnflag    char(1)        NOT NULL,
        l_linestatus    char(1)        NOT NULL,
        l_shipdate      date           NOT NULL,
        l_commitdate    date           NOT NULL,
        l_receiptdate   date           NOT NULL,
        l_shipinstruct  char(25)       NOT NULL,
        l_shipmode      char(10)       NOT NULL,
        l_comment       varchar(44)    NOT NULL""",
}

# Primary keys and foreign keys exactly as in Appendix II.
CONSTRAINTS = [
    "ALTER TABLE region   ADD CONSTRAINT region_pkey   PRIMARY KEY (r_regionkey)",
    "ALTER TABLE nation   ADD CONSTRAINT nation_pkey   PRIMARY KEY (n_nationkey)",
    "ALTER TABLE part     ADD CONSTRAINT part_pkey     PRIMARY KEY (p_partkey)",
    "ALTER TABLE supplier ADD CONSTRAINT supplier_pkey PRIMARY KEY (s_suppkey)",
    "ALTER TABLE partsupp ADD CONSTRAINT partsupp_pkey PRIMARY KEY (ps_partkey, ps_suppkey)",
    "ALTER TABLE customer ADD CONSTRAINT customer_pkey PRIMARY KEY (c_custkey)",
    "ALTER TABLE orders   ADD CONSTRAINT orders_pkey   PRIMARY KEY (o_orderkey)",
    "ALTER TABLE lineitem ADD CONSTRAINT lineitem_pkey PRIMARY KEY (l_orderkey, l_partkey, l_suppkey, l_linenumber)",
    "ALTER TABLE nation   ADD CONSTRAINT fk_nation   FOREIGN KEY (n_regionkey) REFERENCES region (r_regionkey)",
    "ALTER TABLE supplier ADD CONSTRAINT fk_supplier FOREIGN KEY (s_nationkey) REFERENCES nation (n_nationkey)",
    "ALTER TABLE partsupp ADD CONSTRAINT fk_ps_partkey_partkey FOREIGN KEY (ps_partkey) REFERENCES part (p_partkey)",
    "ALTER TABLE partsupp ADD CONSTRAINT fk_ps_suppkey_suppkey FOREIGN KEY (ps_suppkey) REFERENCES supplier (s_suppkey)",
    "ALTER TABLE customer ADD CONSTRAINT fk_customer FOREIGN KEY (c_nationkey) REFERENCES nation (n_nationkey)",
    "ALTER TABLE orders   ADD CONSTRAINT fk_orders   FOREIGN KEY (o_custkey) REFERENCES customer (c_custkey)",
    "ALTER TABLE lineitem ADD CONSTRAINT fk_lineitem_orderkey FOREIGN KEY (l_orderkey) REFERENCES orders (o_orderkey)",
    "ALTER TABLE lineitem ADD CONSTRAINT fk_lineitem_partkey  FOREIGN KEY (l_partkey)  REFERENCES part (p_partkey)",
    "ALTER TABLE lineitem ADD CONSTRAINT fk_lineitem_suppkey  FOREIGN KEY (l_suppkey)  REFERENCES supplier (s_suppkey)",
]

# Secondary indexes.
# The Appendix only creates primary keys, and PostgreSQL does NOT index
# foreign-key columns automatically. With PKs alone almost every plan would be
# sequential scans + hash joins. These indexes give the optimizer real
# choices between access methods (seq scan / index scan / bitmap scan, Week 8-2)
# and join methods (index nested-loop needs an index on the join attribute of
# the probe relation, Week 8-1 and Week 10-1).
EXTRA_INDEXES = [
    "CREATE INDEX idx_lineitem_shipdate ON lineitem (l_shipdate)",
    "CREATE INDEX idx_lineitem_partkey  ON lineitem (l_partkey)",
    "CREATE INDEX idx_lineitem_suppkey  ON lineitem (l_suppkey)",
    "CREATE INDEX idx_orders_custkey    ON orders (o_custkey)",
    "CREATE INDEX idx_orders_orderdate  ON orders (o_orderdate)",
    "CREATE INDEX idx_customer_nationkey ON customer (c_nationkey)",
    "CREATE INDEX idx_supplier_nationkey ON supplier (s_nationkey)",
    "CREATE INDEX idx_partsupp_suppkey  ON partsupp (ps_suppkey)",
]

# Load order respects the foreign keys (parents before children).
LOAD_ORDER = ["region", "nation", "part", "supplier", "partsupp", "customer", "orders", "lineitem"]


class StripTrailingPipe(io.RawIOBase):
    """File-like wrapper that removes the trailing '|' from every line of a
    .tbl file as PostgreSQL reads it, so no converted copy is needed on disk."""

    def __init__(self, path):
        self._file = open(path, "r", encoding="utf-8", newline="")
        self._buffer = b""

    def readable(self):
        return True

    def readinto(self, out):
        while len(self._buffer) < len(out):
            line = self._file.readline()
            if not line:
                break
            line = line.rstrip("\r\n")
            if line.endswith("|"):
                line = line[:-1]
            self._buffer += (line + "\n").encode("utf-8")
        n = min(len(out), len(self._buffer))
        out[:n] = self._buffer[:n]
        self._buffer = self._buffer[n:]
        return n

    def close(self):
        self._file.close()
        super().close()


def find_data_file(data_dir, table):
    """Accept customer.tbl or customer.csv (the Appendix saves .tbl as .csv)."""
    for ext in (".tbl", ".csv"):
        path = os.path.join(data_dir, table + ext)
        if os.path.exists(path):
            return path
    sys.exit(f"Cannot find {table}.tbl or {table}.csv in {data_dir}")


def create_database(args):
    conn = psycopg2.connect(host=args.host, port=args.port, user=args.user,
                            password=args.password, dbname="postgres")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (args.dbname,))
        if cur.fetchone():
            if not args.recreate:
                sys.exit(f'Database "{args.dbname}" already exists. Use --recreate to drop and reload it.')
            print(f'Dropping existing database "{args.dbname}"')
            cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(args.dbname)))
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(args.dbname)))
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Load TPC-H .tbl files into PostgreSQL.")
    parser.add_argument("--data-dir", required=True, help="folder containing region.tbl, nation.tbl, ...")
    parser.add_argument("--dbname", default="TPC-H")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password", default=os.environ.get("PGPASSWORD", ""))
    parser.add_argument("--recreate", action="store_true", help="drop the database first if it exists")
    parser.add_argument("--no-extra-indexes", action="store_true", help="only create the Appendix's primary keys")
    args = parser.parse_args()

    start = time.time()
    create_database(args)
    conn = psycopg2.connect(host=args.host, port=args.port, user=args.user,
                            password=args.password, dbname=args.dbname)
    cur = conn.cursor()

    for table in LOAD_ORDER:
        cur.execute(f"CREATE TABLE {table} ({TABLES[table]})")
        path = find_data_file(args.data_dir, table)
        t0 = time.time()
        reader = io.BufferedReader(StripTrailingPipe(path), buffer_size=1 << 20)
        cur.copy_expert(f"COPY {table} FROM STDIN WITH (FORMAT text, DELIMITER '|')", reader)
        reader.close()
        conn.commit()
        print(f"  loaded {table:9s} {cur.rowcount:>10,} rows  ({time.time() - t0:.0f}s)")

    print("Adding primary and foreign keys ...")
    for stmt in CONSTRAINTS:
        cur.execute(stmt)
    conn.commit()

    if not args.no_extra_indexes:
        print("Adding secondary indexes ...")
        for stmt in EXTRA_INDEXES:
            cur.execute(stmt)
        conn.commit()

    # ANALYZE fills pg_class / pg_stats with the statistics the optimizer's
    # cost model and cardinality estimation use (Week 9-2).
    print("Running ANALYZE ...")
    conn.autocommit = True
    cur.execute("ANALYZE")
    conn.close()
    print(f'Done: database "{args.dbname}" ready in {time.time() - start:.0f}s')


if __name__ == "__main__":
    main()
