"""Checks for the PostgreSQL part of preprocessing.py.

Run:  python tests/test_postgres_part.py --dbname TPC-H [--host localhost --password ...]
It prints each query's PlanNode tree and checks that every node of
PostgreSQL's JSON plan appears in the tree with the right fields.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import preprocessing as pp  # noqa: E402

QUERIES = {
    "brief example (EC7 Q4)": """
        SELECT 100.00 * sum(case when p_type like 'PROMO%' then l_extendedprice * (1 - l_discount)
               else 0 end) / sum(l_extendedprice * (1 - l_discount)) as promo_revenue
        FROM lineitem, part
        WHERE l_partkey = p_partkey AND l_shipdate >= date '1993-11-01'
          AND l_shipdate < date '1993-11-01' + interval '1' month""",
    "point lookup (index scan)":
        "SELECT * FROM orders WHERE o_orderkey = 12345",
    "range with no index (seq scan)":
        "SELECT c_name FROM customer WHERE c_acctbal > 9990",
    "two indexed predicates (bitmap)":
        "SELECT * FROM orders WHERE o_custkey = 100 OR o_orderdate = date '1995-01-01'",
    "small join with index (index nested loop)":
        "SELECT o_orderkey, c_name FROM orders o JOIN customer c ON o.o_custkey = c.c_custkey WHERE o.o_orderkey < 100",
    "3-way join + group by":
        """SELECT n_name, count(*) FROM customer, orders, nation
           WHERE c_custkey = o_custkey AND c_nationkey = n_nationkey AND o_orderdate >= date '1997-01-01'
           GROUP BY n_name ORDER BY count(*) DESC""",
    "4-way join":
        """SELECT s_name, sum(l_quantity) FROM supplier, lineitem, orders, nation
           WHERE s_suppkey = l_suppkey AND l_orderkey = o_orderkey AND s_nationkey = n_nationkey
             AND n_name = 'JAPAN' AND o_orderdate between date '1996-01-01' and date '1996-01-31'
           GROUP BY s_name""",
    "non-equi join":
        "SELECT count(*) FROM region r1, region r2 WHERE r1.r_regionkey < r2.r_regionkey",
    "merge join (sorted inputs)":
        "SELECT o_orderkey, l_linenumber FROM orders, lineitem WHERE o_orderkey = l_orderkey ORDER BY o_orderkey LIMIT 50",
    "subquery (EXISTS)":
        """SELECT c_name FROM customer c WHERE c_nationkey = 3
           AND EXISTS (SELECT 1 FROM orders o WHERE o.o_custkey = c.c_custkey AND o_totalprice > 400000)""",
    "scalar subquery (InitPlan)":
        "SELECT p_name FROM part WHERE p_retailprice > (SELECT avg(p_retailprice) * 1.5 FROM part)",
    "CTE + DISTINCT + ORDER BY":
        """WITH big AS (SELECT o_custkey FROM orders WHERE o_totalprice > 450000)
           SELECT DISTINCT c_mktsegment FROM customer JOIN big ON c_custkey = o_custkey ORDER BY 1""",
    "left join with NULL check (anti join)":
        "SELECT p_partkey FROM part LEFT JOIN partsupp ON p_partkey = ps_partkey WHERE ps_partkey IS NULL",
}


def count_json_nodes(node):
    return 1 + sum(count_json_nodes(c) for c in node.get("Plans", []))


def check(tree: pp.PlanNode):
    """Basic invariants of a parsed PostgreSQL tree."""
    for n in tree.walk():
        if n.operator in ("Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan",
                          "Bitmap Index Scan"):
            assert n.table, f"{n.operator} without a table"
        if n.operator in ("Index Scan", "Index Only Scan", "Bitmap Index Scan"):
            assert n.index, f"{n.operator} without an index"
        if n.operator in ("Hash Join", "Merge Join"):
            assert n.join_condition, f"{n.operator} without a join condition"
            assert len(n.children) == 2
        for text in (n.filter, n.join_condition):
            assert text is None or "::" not in text, f"cast left in condition: {text}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbname", default="TPC-H")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=5432)
    ap.add_argument("--user", default="postgres")
    ap.add_argument("--password", default=os.environ.get("PGPASSWORD", ""))
    args = ap.parse_args()

    conn = pp.connect(args.dbname, args.user, args.password, args.host, args.port)
    schema = pp.get_schema(conn)
    print(f"{len(schema)} tables:", ", ".join(schema))
    print(pp.describe_schema({k: schema[k] for k in list(schema)[:2]}))
    print()

    failures = 0
    for name, query in QUERIES.items():
        for parallel in (True, False):
            try:
                raw = pp.get_postgres_plan_json(conn, query, parallel=parallel)
                tree = pp.parse_postgres_plan(raw["Plan"])
                assert sum(1 for _ in tree.walk()) == count_json_nodes(raw["Plan"]), "node count differs"
                check(tree)
                if parallel is False or name.startswith("brief"):
                    print(f"--- {name} (parallel={parallel})")
                    print(tree.pretty())
                    print()
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name} (parallel={parallel}): {e}")

    # Errors are reported, not raised as raw psycopg2 exceptions.
    for bad in ("SELEC * FROM part", "SELECT * FROM no_such_table", ""):
        try:
            pp.get_postgres_plan(conn, bad)
            failures += 1
            print(f"FAIL: no error for {bad!r}")
        except pp.DatabaseError as e:
            print("error reported OK:", str(e).splitlines()[0])
    # The connection is still usable after an error.
    pp.get_postgres_plan(conn, "SELECT 1")

    # ANALYZE runs inside a read-only transaction.
    try:
        pp.get_postgres_plan(conn, "DELETE FROM region", analyze=True)
        failures += 1
        print("FAIL: DELETE with analyze was allowed")
    except pp.DatabaseError:
        print("read-only guard OK")
    tree = pp.get_postgres_plan(conn, QUERIES["point lookup (index scan)"], analyze=True)
    assert "actual_rows" in tree.details

    print("\nALL CHECKS PASSED" if failures == 0 else f"\n{failures} FAILURES")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
