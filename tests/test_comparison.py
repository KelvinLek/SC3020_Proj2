"""Checks for comparison.py (Member 3).

Run:  python tests/test_comparison.py
        Part A only: hand-made plan trees, no database or API key needed.
      python tests/test_comparison.py --password YOUR_PASSWORD [--dbname TPC-H]
        Also Part B: real PostgreSQL plans. Each query's plan is compared
        with itself (must give rate 0) and with the plans PostgreSQL picks
        when a join method or scan method is switched off, which gives
        realistic "alternative" plans to compare.
Prints a report for a few cases and ends with ALL CHECKS PASSED.
"""
import argparse
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import comparison as cmp  # noqa: E402
import preprocessing as pp  # noqa: E402
from preprocessing import PlanNode  # noqa: E402


def N(operator, table=None, index=None, join=None, filter=None, children=(), **details):
    """Short way to write a PlanNode in the tests."""
    return PlanNode(operator=operator, table=table, index=index, join_condition=join,
                    filter=filter, children=list(children), details=dict(details))


def categories(result, severity=None):
    return sorted(d.category for d in result.differences
                  if severity is None or d.severity == severity)


def status(tree, operator, table=None):
    for node in tree.walk():
        if node.operator == operator and (table is None or node.table == table):
            return node.details["cmp_status"]
    raise AssertionError(f"no {operator} {table} in tree")


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


# =============================================================================
# Part A: hand-made trees
# =============================================================================

def brief_pg():
    """PostgreSQL's plan for the brief's example (Figure 1, no parallelism)."""
    return N("Aggregate", estimated_rows=1, children=[
        N("Hash Join", join="(lineitem.l_partkey = part.p_partkey)", join_type="Inner", children=[
            N("Seq Scan", "lineitem", filter="((l_shipdate >= '1993-11-01') AND "
                                              "(l_shipdate < '1993-12-01 00:00:00'))", estimated_rows=75000),
            N("Hash", children=[N("Seq Scan", "part", estimated_rows=200000)])])])


def brief_ai():
    """The AI's plan in Figure 2: an index scan on lineitem."""
    return N("Aggregate", children=[
        N("Hash Join", join="(l_partkey = p_partkey)", join_type="Inner", children=[
            N("Index Scan", "lineitem", "idx_lineitem_shipdate",
              filter="(l_shipdate >= date '1993-11-01' AND l_shipdate < date '1993-11-01' + interval '1' month)",
              ai_reason="Uses the shipdate index for the one-month range"),
            N("Hash", children=[N("Seq Scan", "part")], added_by_normalization=True)])])


@check
def identical_plans():
    r = cmp.compare_plans(brief_pg(), brief_pg())
    assert r.difference_rate == 0 and r.strict_difference_rate == 0
    assert r.differences == []
    assert r.matched_same == 4 and r.unmatched_pg == r.unmatched_ai == 0
    assert r.similarities and r.node_agreement == 1.0
    assert status(r.pg_tree, "Hash") == cmp.STATUS_HELPER


@check
def brief_example_access_method():
    schema = {"lineitem": {"indexes": [{"name": "idx_lineitem_shipdate", "columns": ["l_shipdate"]}],
                           "stats": {"tuples": 6001215, "blocks": 112000}},
              "part": {"indexes": [], "stats": {"tuples": 200000, "blocks": 4000}}}
    r = cmp.compare_plans(brief_pg(), brief_ai(), schema)
    print(r.report())
    assert categories(r, cmp.MAJOR) == [cmp.ACCESS_METHOD]
    assert status(r.pg_tree, "Seq Scan", "lineitem") == cmp.STATUS_CHANGED
    assert status(r.ai_tree, "Index Scan", "lineitem") == cmp.STATUS_CHANGED
    assert status(r.pg_tree, "Seq Scan", "part") == cmp.STATUS_SAME
    assert status(r.pg_tree, "Hash Join") == cmp.STATUS_SAME
    # The pair has the same match id in both trees.
    pg_scan = next(n for n in r.pg_tree.walk() if n.table == "lineitem")
    ai_scan = next(n for n in r.ai_tree.walk() if n.table == "lineitem")
    assert pg_scan.details["cmp_match_id"] == ai_scan.details["cmp_match_id"] is not None
    access = next(d for d in r.differences if d.category == cmp.ACCESS_METHOD)
    assert "6,001,215" in access.explanation and "shipdate index" in access.explanation
    # The date range is written differently but recognised as the same columns.
    cond = [d for d in r.differences if d.category == cmp.CONDITION]
    assert len(cond) == 1 and "probably equivalent" in cond[0].explanation
    # One method change (0.5) over 4 + 4 core nodes.
    assert r.edit_distance == 0.5 and r.pg_size == r.ai_size == 4
    assert abs(r.difference_rate - 0.5 / 8) < 1e-9


@check
def join_algorithm():
    pg = N("Hash Join", join="(a.x = b.x)", children=[N("Seq Scan", "a"), N("Hash", children=[N("Seq Scan", "b")])])
    ai = N("Nested Loop", join="(a.x = b.x)", children=[N("Seq Scan", "a"), N("Index Scan", "b", "b_pkey")])
    r = cmp.compare_plans(pg, ai)
    assert categories(r, cmp.MAJOR) == [cmp.ACCESS_METHOD, cmp.JOIN_ALGORITHM]
    assert status(r.pg_tree, "Hash Join") == status(r.ai_tree, "Nested Loop") == cmp.STATUS_CHANGED


@check
def join_order():
    pg = N("Aggregate", children=[
        N("Hash Join", join="(o_custkey = c_custkey)", children=[
            N("Hash Join", join="(l_orderkey = o_orderkey)", children=[
                N("Seq Scan", "lineitem"), N("Hash", children=[N("Seq Scan", "orders")])]),
            N("Hash", children=[N("Seq Scan", "customer")])])])
    ai = N("Aggregate", children=[
        N("Hash Join", join="(l_orderkey = o_orderkey)", children=[
            N("Seq Scan", "lineitem"),
            N("Hash", children=[
                N("Hash Join", join="(o_custkey = c_custkey)", children=[
                    N("Seq Scan", "orders"), N("Hash", children=[N("Seq Scan", "customer")])])])])])
    r = cmp.compare_plans(pg, ai)
    print(r.report())
    assert categories(r, cmp.MAJOR) == [cmp.JOIN_ORDER]
    jo = r.differences[0]
    assert len(jo.pg_nodes) == 1 and len(jo.ai_nodes) == 1
    assert "{lineitem, orders}" in jo.explanation and "{customer, orders}" in jo.explanation
    # The top join covers all three tables in both plans, so it matches.
    assert r.pg_tree.children[0].details["cmp_status"] == cmp.STATUS_SAME
    assert r.pg_tree.children[0].children[0].details["cmp_status"] == cmp.STATUS_UNMATCHED
    for table in ("lineitem", "orders", "customer"):
        assert status(r.pg_tree, "Seq Scan", table) == cmp.STATUS_SAME
    assert "Both plans join the tables in the same order." not in r.similarities
    assert r.difference_rate > 0


@check
def swapped_hash_join_inputs():
    pg = N("Hash Join", children=[N("Seq Scan", "big", estimated_rows=1000000),
                                  N("Hash", children=[N("Seq Scan", "small", estimated_rows=10)])])
    ai = N("Hash Join", children=[N("Seq Scan", "small"),
                                  N("Hash", children=[N("Seq Scan", "big")])])
    r = cmp.compare_plans(pg, ai)
    assert categories(r, cmp.MAJOR) == [cmp.JOIN_ORDER]
    assert "swapped" in r.differences[0].explanation
    assert status(r.pg_tree, "Hash Join") == cmp.STATUS_CHANGED
    assert status(r.pg_tree, "Seq Scan", "big") == cmp.STATUS_SAME


@check
def swap_with_other_algorithm_is_minor():
    pg = N("Hash Join", children=[N("Seq Scan", "a"), N("Hash", children=[N("Seq Scan", "b")])])
    ai = N("Merge Join", children=[N("Index Scan", "b", "b_pkey"), N("Sort", children=[N("Seq Scan", "a")])])
    r = cmp.compare_plans(pg, ai)
    swap = [d for d in r.differences if d.category == cmp.JOIN_ORDER]
    assert len(swap) == 1 and swap[0].severity == cmp.MINOR
    assert "different join algorithm" in swap[0].explanation


@check
def no_join_condition_noise_after_join_order_change():
    pg = N("Hash Join", join="(c.n = n.n)", children=[
        N("Hash Join", join="(o.c = c.c)", children=[N("Seq Scan", "o"), N("Hash", children=[N("Seq Scan", "c")])]),
        N("Hash", children=[N("Seq Scan", "n")])])
    ai = N("Hash Join", join="(o.c = c.c)", children=[
        N("Seq Scan", "o"),
        N("Hash", children=[N("Hash Join", join="(c.n = n.n)", children=[
            N("Seq Scan", "c"), N("Hash", children=[N("Seq Scan", "n")])])])])
    r = cmp.compare_plans(pg, ai)
    assert categories(r) == [cmp.JOIN_ORDER]


@check
def helper_nodes_not_counted():
    """The example in README_M2.md section 6."""
    pg = N("Limit", children=[
        N("Nested Loop", join="(customer.c_custkey = orders.o_custkey)", index_nested_loop=True, children=[
            N("Seq Scan", "orders", filter="(o_totalprice > '100000')"),
            N("Memoize", children=[N("Index Scan", "customer", "customer_pkey")])])])
    ai = N("Limit", children=[
        N("Nested Loop", children=[
            N("Seq Scan", "orders", filter="(o_totalprice > 100000)"),
            N("Index Scan", "customer", "customer_pkey")])])
    r = cmp.compare_plans(pg, ai)
    print(r.report())
    assert r.difference_rate == 0, "helpers must not count"
    assert r.strict_difference_rate > 0, "but they do count in the strict rate"
    assert categories(r) == [cmp.CONDITION, cmp.HELPER_NODE]      # missing join condition + Memoize
    assert categories(r, cmp.MAJOR) == []
    assert status(r.pg_tree, "Memoize") == cmp.STATUS_HELPER
    assert all(n.details["cmp_status"] == cmp.STATUS_SAME for n in r.ai_tree.walk())


@check
def implied_sort_vs_order_by_sort():
    # Sorts feeding a Merge Join are helpers.
    pg = N("Merge Join", children=[N("Sort", children=[N("Seq Scan", "a")]),
                                   N("Sort", children=[N("Seq Scan", "b")])])
    ai = N("Merge Join", children=[N("Seq Scan", "a"), N("Seq Scan", "b")])
    r = cmp.compare_plans(pg, ai)
    assert r.difference_rate == 0 and categories(r) == [cmp.HELPER_NODE, cmp.HELPER_NODE]
    # A Sort for ORDER BY is real: the AI leaving it out is a missing node.
    pg = N("Sort", sort_key=["n_name"], children=[N("HashAggregate", children=[N("Seq Scan", "nation")])])
    ai = N("HashAggregate", children=[N("Seq Scan", "nation")])
    r = cmp.compare_plans(pg, ai)
    assert categories(r) == [cmp.MISSING_NODE]
    assert "sorts by n_name" in r.differences[0].explanation
    assert status(r.pg_tree, "Sort") == cmp.STATUS_UNMATCHED


@check
def aggregation_strategy():
    pg = N("HashAggregate", estimated_rows=25, children=[N("Seq Scan", "nation")])
    ai = N("GroupAggregate", children=[N("Sort", children=[N("Seq Scan", "nation")])])
    r = cmp.compare_plans(pg, ai)
    assert categories(r, cmp.MAJOR) == [cmp.AGGREGATION]
    assert "about 25 groups" in r.differences[0].explanation
    assert r.edit_distance == 0.5   # the Sort under GroupAggregate is a helper


@check
def self_join_scans_paired_by_alias_and_filter():
    pg = N("Nested Loop", join="(r1.r_regionkey < r2.r_regionkey)", children=[
        N("Seq Scan", "region", filter="(r_name = 'ASIA')", alias="r1"),
        N("Seq Scan", "region", filter="(r_name = 'EUROPE')", alias="r2")])
    ai = N("Nested Loop", join="(r1.r_regionkey < r2.r_regionkey)", children=[
        N("Seq Scan", "region", filter="(r_name = 'ASIA')"),
        N("Index Scan", "region", "region_pkey", filter="(r_name = 'EUROPE')")])
    r = cmp.compare_plans(pg, ai)
    assert categories(r, cmp.MAJOR) == [cmp.ACCESS_METHOD]
    europe = [n for n in r.pg_tree.walk() if n.filter and "EUROPE" in n.filter][0]
    asia = [n for n in r.pg_tree.walk() if n.filter and "ASIA" in n.filter][0]
    assert europe.details["cmp_status"] == cmp.STATUS_CHANGED
    assert asia.details["cmp_status"] == cmp.STATUS_SAME


@check
def invalid_references():
    pg = N("Hash Join", children=[N("Seq Scan", "a"), N("Hash", children=[N("Seq Scan", "b")])])
    ai = N("Hash Join", children=[N("Index Scan", "a", "idx_made_up", unknown_index=True),
                                  N("Hash", children=[N("Seq Scan", "ghost", unknown_table=True)])])
    r = cmp.compare_plans(pg, ai)
    cats = categories(r)
    assert cats.count(cmp.INVALID_REFERENCE) == 2 and cmp.ACCESS_METHOD in cats
    assert cmp.EXTRA_NODE not in cats         # the made-up table is reported once, as invalid
    assert cmp.MISSING_NODE in cats           # b is never read by the AI
    assert any("does not exist" in d.explanation for d in r.differences)


@check
def condition_normalisation():
    eq = cmp._normalise_predicate
    assert eq("(o_totalprice > '100000')") == eq("100000 < ORDERS.o_totalprice")
    assert eq("(customer.c_custkey = orders.o_custkey)") == eq("o_custkey = c_custkey")
    assert eq("(p_type ~~ 'PROMO%')") == eq("p_type LIKE 'PROMO%'")
    assert eq("l_shipdate < '1993-12-01 00:00:00'") == eq("l_shipdate < date '1993-12-01'")
    assert eq("a <> 1") == eq("a != 1")
    assert eq("a > 1") != eq("a > 2")
    split = cmp._split_conjuncts
    assert split("((a = 1) AND (b = 2))") == ["a = 1", "b = 2"]
    assert split("((a = 1) AND ((b = 2) AND (c = 3)))") == ["a = 1", "b = 2", "c = 3"]
    assert split("x BETWEEN 1 AND 5 AND y = 2") == ["x BETWEEN 1 AND 5", "y = 2"]
    assert split("(a = 1) OR (b = 2)") == ["(a = 1) OR (b = 2)"]
    assert split("name = 'x and y'") == ["name = 'x and y'"]

    # Same predicates written differently: no difference at all.
    pg = N("Seq Scan", "orders", filter="((o_totalprice > '100000') AND (o_orderstatus = 'F'))")
    ai = N("Seq Scan", "orders", filter="o_orderstatus = 'F' AND orders.o_totalprice > 100000")
    assert cmp.compare_plans(pg, ai).differences == []

    # A predicate the AI applies at another node is pointed out.
    pg = N("Hash Join", join="(a.x = b.x)", children=[
        N("Seq Scan", "a", filter="(a.y > 5)"), N("Hash", children=[N("Seq Scan", "b")])])
    ai = N("Hash Join", join="(a.x = b.x)", filter="(a.y > 5)", children=[
        N("Seq Scan", "a"), N("Hash", children=[N("Seq Scan", "b")])])
    r = cmp.compare_plans(pg, ai)
    assert categories(r) == [cmp.CONDITION, cmp.CONDITION]
    assert categories(r, cmp.MAJOR) == []
    assert any("instead" in d.explanation for d in r.differences)


@check
def bitmap_scans():
    pg = N("Bitmap Heap Scan", "orders", filter="(o_custkey = 100)", children=[
        N("Bitmap Index Scan", "orders", "idx_orders_custkey", filter="(o_custkey = 100)")])
    # M2's normaliser turns a Bitmap Heap Scan with an index into heap + index child.
    ai = N("Bitmap Heap Scan", "orders", filter="(o_custkey = 100)", children=[
        N("Bitmap Index Scan", "orders", "idx_orders_custkey", added_by_normalization=True)])
    r = cmp.compare_plans(pg, ai)
    assert r.difference_rate == 0 and categories(r, cmp.MAJOR) == []
    assert all(n.details["cmp_status"] == cmp.STATUS_SAME for n in r.pg_tree.walk())
    # BitmapOr over two indexes vs a plain index scan: one access-method difference.
    pg = N("Bitmap Heap Scan", "orders", children=[N("BitmapOr", "orders", children=[
        N("Bitmap Index Scan", "orders", "idx_a"), N("Bitmap Index Scan", "orders", "idx_b")])])
    ai = N("Index Scan", "orders", "idx_a")
    r = cmp.compare_plans(pg, ai)
    assert categories(r, cmp.MAJOR) == [cmp.ACCESS_METHOD]
    assert "idx_a + idx_b" in r.differences[0].explanation
    assert r.pg_size == r.ai_size == 1


@check
def ai_leaves_out_a_table():
    pg = N("Aggregate", children=[N("Hash Join", children=[
        N("Seq Scan", "a"), N("Hash", children=[N("Seq Scan", "b")])])])
    ai = N("Aggregate", children=[N("Seq Scan", "a")])
    r = cmp.compare_plans(pg, ai)
    assert categories(r) == [cmp.MISSING_NODE, cmp.MISSING_NODE]
    # The final Aggregate still matches (second matching pass).
    assert status(r.pg_tree, "Aggregate") == cmp.STATUS_SAME


@check
def subplan_scans_paired_correctly():
    pg = N("Seq Scan", "part", filter="(p_retailprice > $0)", children=[
        N("Aggregate", parent_relationship="InitPlan", children=[N("Seq Scan", "part")])])
    ai = N("Nested Loop", join_type="Inner", children=[
        N("Seq Scan", "part"),
        N("Aggregate", children=[N("Seq Scan", "part")])])
    r = cmp.compare_plans(pg, ai)
    assert r.pg_size == 3 and r.ai_size == 4
    # The PostgreSQL main scan (the root) is paired with the AI's main scan.
    root_id = r.pg_tree.details["cmp_match_id"]
    assert root_id is not None and r.ai_tree.children[0].details["cmp_match_id"] == root_id
    extra = [d for d in r.differences if d.category == cmp.EXTRA_NODE]
    assert len(extra) == 1 and "sub-plan" in extra[0].explanation


@check
def parallel_helpers():
    pg = N("Aggregate", partial_mode="Finalize", children=[
        N("Gather", children=[N("Aggregate", partial_mode="Partial", children=[
            N("Seq Scan", "lineitem", parallel=True)])])])
    ai = N("Aggregate", children=[N("Seq Scan", "lineitem")])
    r = cmp.compare_plans(pg, ai)
    assert r.difference_rate == 0 and categories(r, cmp.MAJOR) == []
    assert status(r.pg_tree, "Gather") == cmp.STATUS_HELPER


@check
def gather_at_root():
    pg = N("Gather", children=[N("Seq Scan", "lineitem")])
    ai = N("Seq Scan", "lineitem")
    r = cmp.compare_plans(pg, ai)
    assert r.difference_rate == 0 and r.pg_size == 1


@check
def join_type():
    pg = N("Hash Join", join_type="Semi", children=[N("Seq Scan", "c"), N("Hash", children=[N("Seq Scan", "o")])])
    ai = N("Hash Join", join_type="Inner", children=[N("Seq Scan", "c"), N("Hash", children=[N("Seq Scan", "o")])])
    r = cmp.compare_plans(pg, ai)
    assert categories(r) == [cmp.JOIN_TYPE] and "EXISTS" in r.differences[0].explanation
    assert r.edit_distance == 0.25


@check
def tree_edit_distance_basics():
    ted = cmp.tree_edit_distance

    def T(label, *kids):
        return (label, list(kids))

    kids, lab = (lambda t: t[1]), (lambda t: t[0])
    unit = lambda a, b: 0.0 if a == b else 1.0  # noqa: E731
    # Example from Zhang & Shasha (1989): distance 2 (delete c, insert c).
    t1 = T("f", T("d", T("a"), T("c", T("b"))), T("e"))
    t2 = T("f", T("c", T("d", T("a"), T("b"))), T("e"))
    assert ted(t1, t2, kids, lab, unit) == 2
    assert ted(t1, t1, kids, lab, unit) == 0
    assert ted(T("a"), T("a", T("b")), kids, lab, unit) == 1
    assert ted(T("a", T("b"), T("c")), T("a", T("c"), T("b")), kids, lab, unit) == 2
    assert ted(T("a"), T("b"), kids, lab, unit) == 1
    # Weighted costs.
    assert cmp.relabel_cost(("scan", "Seq Scan", "t", (), None), ("scan", "Index Scan", "t", ("i",), None)) == 0.5
    assert cmp.relabel_cost(("scan", "Seq Scan", "t", (), None), ("scan", "Seq Scan", "u", (), None)) == 1.0
    assert cmp.relabel_cost(("join", "Hash Join", None, (), "Inner"), ("join", "Hash Join", None, (), "Semi")) == 0.25


@check
def inputs_not_modified_and_json():
    pg, ai = brief_pg(), brief_ai()
    before = (copy.deepcopy(pg.to_dict()), copy.deepcopy(ai.to_dict()))
    r = cmp.compare_plans(pg, ai)
    assert (pg.to_dict(), ai.to_dict()) == before
    data = json.loads(json.dumps(r.to_dict(include_trees=True)))
    assert data["difference_rate"] == round(r.difference_rate, 4)
    assert data["differences"][0]["pg_nodes"][0].startswith("pg-")
    for node in r.pg_tree.walk():
        for key in ("cmp_status", "cmp_colour", "cmp_note", "cmp_node_id"):
            assert key in node.details, key
        assert cmp.status_colour(node) == cmp.STATUS_COLOURS[node.details["cmp_status"]]


@check
def through_m2_parser():
    """An AI answer in non-PostgreSQL wording, parsed by M2's code, then compared."""
    schema = {"lineitem": {"indexes": [{"name": "idx_lineitem_shipdate"}]}, "part": {"indexes": []}}
    answer = json.dumps({"plan": {"operator": "Projection", "children": [{
        "operator": "Hash Match", "join_condition": "l_partkey = p_partkey", "children": [
            {"operator": "Index Range Scan", "table": "l", "index": "idx_lineitem_shipdate"},
            {"operator": "Full Table Scan", "table": "part"}]}]}})
    query = "SELECT * FROM lineitem l, part WHERE l_partkey = p_partkey"
    ai = pp.parse_ai_plan(pp.extract_plan_json(answer), schema, query)
    pg = N("Hash Join", join="(l.l_partkey = part.p_partkey)", children=[
        N("Seq Scan", "lineitem", alias="l"), N("Hash", children=[N("Seq Scan", "part")])])
    r = cmp.compare_plans(pg, ai)
    assert categories(r) == [cmp.ACCESS_METHOD]


# =============================================================================
# Part B: real PostgreSQL plans
# =============================================================================

REAL_QUERIES = [
    "SELECT 100.00 * sum(case when p_type like 'PROMO%' then l_extendedprice * (1 - l_discount) else 0 end) "
    "/ sum(l_extendedprice * (1 - l_discount)) FROM lineitem, part WHERE l_partkey = p_partkey "
    "AND l_shipdate >= date '1993-11-01' AND l_shipdate < date '1993-11-01' + interval '1' month",
    "SELECT * FROM orders WHERE o_custkey = 100 OR o_orderdate = date '1995-01-01'",
    "SELECT o_orderkey, c_name FROM orders o JOIN customer c ON o.o_custkey = c.c_custkey WHERE o.o_orderkey < 100",
    "SELECT n_name, count(*) FROM customer, orders, nation WHERE c_custkey = o_custkey "
    "AND c_nationkey = n_nationkey AND o_orderdate >= date '1997-01-01' GROUP BY n_name ORDER BY count(*) DESC",
    "SELECT s_name, sum(l_quantity) FROM supplier, lineitem, orders, nation WHERE s_suppkey = l_suppkey "
    "AND l_orderkey = o_orderkey AND s_nationkey = n_nationkey AND n_name = 'JAPAN' "
    "AND o_orderdate between date '1996-01-01' and date '1996-01-31' GROUP BY s_name",
    "SELECT count(*) FROM region r1, region r2 WHERE r1.r_regionkey < r2.r_regionkey",
    "SELECT c_name FROM customer c WHERE c_nationkey = 3 AND EXISTS "
    "(SELECT 1 FROM orders o WHERE o.o_custkey = c.c_custkey AND o_totalprice > 400000)",
    "SELECT p_name FROM part WHERE p_retailprice > (SELECT avg(p_retailprice) FROM part)",
    "WITH big AS (SELECT o_custkey FROM orders WHERE o_totalprice > 450000) "
    "SELECT DISTINCT c_name FROM customer, big WHERE c_custkey = o_custkey ORDER BY c_name",
    "SELECT p_partkey FROM part LEFT JOIN partsupp ON p_partkey = ps_partkey WHERE ps_partkey IS NULL",
]

SWITCHES = ["enable_hashjoin", "enable_nestloop", "enable_mergejoin", "enable_seqscan",
            "enable_indexscan", "enable_bitmapscan", "enable_hashagg", "enable_memoize"]


def plan_with(conn, sql, switch=None):
    with conn.cursor() as cur:
        cur.execute("RESET ALL")
        if switch:
            cur.execute(f"SET {switch} = off")
    conn.commit()
    try:
        return pp.get_postgres_plan(conn, sql, parallel=False)
    finally:
        with conn.cursor() as cur:
            cur.execute("RESET ALL")
        conn.commit()


def run_real(args):
    conn = pp.connect(args.dbname, password=args.password, host=args.host, port=args.port)
    schema = pp.get_schema(conn)
    rates = []
    try:
        for sql in REAL_QUERIES:
            base = plan_with(conn, sql)
            same = cmp.compare_plans(base, base, schema)
            assert same.difference_rate == 0 and same.differences == [], sql
            for switch in SWITCHES:
                other = plan_with(conn, sql, switch)
                r = cmp.compare_plans(base, other, schema)
                json.dumps(r.to_dict(include_trees=True))
                assert 0 <= r.difference_rate <= 1 and 0 <= r.strict_difference_rate <= 1
                for node in list(r.pg_tree.walk()) + list(r.ai_tree.walk()):
                    assert node.details["cmp_status"] in cmp.STATUS_COLOURS
                ops_base = [n.operator for n in base.walk()]
                ops_other = [n.operator for n in other.walk()]
                if ops_base != ops_other and set(ops_base) - {"Memoize", "Materialize", "Sort", "Hash"} \
                        != set(ops_other) - {"Memoize", "Materialize", "Sort", "Hash"}:
                    assert r.major_differences, (sql, switch)
                rates.append(r.difference_rate)
        print(f"Part B: {len(REAL_QUERIES)} queries x {len(SWITCHES)} switched-off methods compared; "
              f"mean difference rate {sum(rates) / len(rates):.3f}, max {max(rates):.3f}")
        # Show one realistic case in full.
        sql = REAL_QUERIES[4]
        print("\n=== Real example: 4-way join, PostgreSQL's plan vs the plan with hash joins off ===")
        print(cmp.compare_plans(plan_with(conn, sql), plan_with(conn, sql, "enable_hashjoin"), schema).report())
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--password", help="PostgreSQL password; enables Part B")
    parser.add_argument("--dbname", default="TPC-H")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5432)
    args = parser.parse_args()

    failures = 0
    for fn in CHECKS:
        try:
            fn()
            print(f"ok    {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {fn.__name__}: {e}")
    if args.password:
        run_real(args)
    print()
    if failures:
        print(f"{failures} CHECK(S) FAILED")
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
