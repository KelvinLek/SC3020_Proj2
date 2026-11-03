"""
preprocessing.py - reads the inputs and prepares them for the comparison.

It has two parts:
  * PostgreSQL part (Member 1): connect to the database, read the schema,
    keys, indexes and statistics from the system catalogs, and turn
    PostgreSQL's chosen query execution plan (QEP) into a PlanNode tree.
  * AI part (Member 2): ask the AI agent for its plan and turn that into a
    PlanNode tree too (to be added below, in its own section).

Both parts produce the same PlanNode structure, so comparison.py can compare
the two trees without knowing where each one came from.

Course references in the comments use the lecture week and part
(e.g. "Wk 8-2" = Week 8, second Query Processing lecture) and the
Example Classes (EC).
"""

import json
import re
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable

import psycopg2


# =============================================================================
# Shared data structure
# =============================================================================

@dataclass
class PlanNode:
    """One operator in a query execution plan.

    A QEP is an operator tree: each node is a physical operator, data flows
    from the leaves (scans of base relations) up to the root, and the root's
    output is the query result (Wk 8-2, "Query Plan").

    The six fields agreed by the team:
      operator        physical operator, e.g. "Seq Scan", "Index Scan",
                      "Hash Join", "Nested Loop", "Sort", "HashAggregate"
      table           base relation read by a scan, else None
      index           index used by an index / bitmap scan, else None
      join_condition  condition a join matches tuples on, else None
      filter          selection condition applied at this node, else None
      children        input operators (left/outer first, right/inner second)

    details holds everything else PostgreSQL reports (estimated rows and cost,
    join type, sort keys, ...). It is optional: the AI part can leave it empty,
    and the comparison does not depend on it.
    """
    operator: str
    table: Optional[str] = None
    index: Optional[str] = None
    join_condition: Optional[str] = None
    filter: Optional[str] = None
    children: List["PlanNode"] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    def walk(self):
        """Yield every node in post-order (children before parent), the order
        in which the operators produce their results (bottom-up, EC7 Q4)."""
        for child in self.children:
            yield from child.walk()
        yield self

    def to_dict(self) -> Dict[str, Any]:
        """Plain dict, e.g. for JSON output or the GUI."""
        return {
            "operator": self.operator,
            "table": self.table,
            "index": self.index,
            "join_condition": self.join_condition,
            "filter": self.filter,
            "children": [c.to_dict() for c in self.children],
            "details": self.details,
        }

    def pretty(self, indent: int = 0) -> str:
        """Indented text view of the tree, for debugging and the report."""
        parts = [self.operator]
        join_type = self.details.get("join_type")
        if join_type and join_type != "Inner":
            parts[0] += f" ({join_type})"
        relationship = self.details.get("parent_relationship")
        if relationship in ("InitPlan", "SubPlan"):
            parts.insert(0, f"[{relationship}]")
        if self.table:
            parts.append(f"on {self.table}")
        if self.index:
            parts.append(f"using {self.index}")
        if self.join_condition:
            parts.append(f"join: {self.join_condition}")
        if self.filter:
            parts.append(f"filter: {self.filter}")
        lines = ["  " * indent + " ".join(parts)]
        for child in self.children:
            lines.append(child.pretty(indent + 1))
        return "\n".join(lines)


# =============================================================================
# PostgreSQL part (Member 1)
# =============================================================================

class DatabaseError(Exception):
    """Raised with a readable message when connecting or explaining fails,
    so the GUI can show it instead of a stack trace."""


# -----------------------------------------------------------------------------
# 1. Connecting
# -----------------------------------------------------------------------------

def connect(dbname: str, user: str = "postgres", password: str = "",
            host: str = "localhost", port: int = 5432, schema: Optional[str] = None):
    """Open a connection to one PostgreSQL database.

    Nothing about the database is hardcoded: the GUI passes in whichever
    database (TPC-H, IMDB, ...) the user picked. If the tables are not in
    the default "public" schema, pass `schema` so queries can name the
    tables without a schema prefix.
    """
    options = None
    if schema:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", schema):
            raise DatabaseError(f"Invalid schema name: {schema!r}")
        options = f"-c search_path={schema},public"
    try:
        conn = psycopg2.connect(dbname=dbname, user=user, password=password,
                                host=host, port=port, connect_timeout=10,
                                options=options)
    except psycopg2.Error as e:
        raise DatabaseError(f"Could not connect to database '{dbname}': {e}".strip()) from e
    conn.autocommit = False
    return conn


def list_databases(user: str = "postgres", password: str = "",
                   host: str = "localhost", port: int = 5432) -> List[str]:
    """Names of the user databases on the server, for the GUI's schema selector."""
    conn = connect("postgres", user, password, host, port)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT datname FROM pg_database
                WHERE NOT datistemplate AND datname <> 'postgres'
                ORDER BY datname""")
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# 2. Schema, keys, indexes and statistics from the system catalogs
# -----------------------------------------------------------------------------
# Everything is read from PostgreSQL's catalogs (pg_class, pg_attribute,
# pg_constraint, pg_index, pg_stats), so the same code works on any schema.
#
# The statistics are the ones the course's cost formulas use, which the
# optimizer keeps for cardinality estimation (Wk 9-2):
#   T(R)    number of tuples           -> pg_class.reltuples
#   B(R)    number of blocks (pages)   -> pg_class.relpages
#   V(R,A)  distinct values of A       -> pg_stats.n_distinct
#   histograms on A                    -> pg_stats.histogram_bounds
#                                         and most_common_vals
#   clustered or not (Wk 8-1)          -> pg_stats.correlation (close to +-1
#                                         means tuples are stored in A order)

def get_schema(conn, schema: str = "public") -> Dict[str, Any]:
    """Describe every table in `schema`.

    Returns {table_name: {
        "columns":      [{"name", "type", "nullable"}, ...],
        "primary_key":  [column, ...],
        "foreign_keys": [{"columns", "ref_table", "ref_columns"}, ...],
        "indexes":      [{"name", "columns", "unique", "primary",
                          "method", "clustered"}, ...],
        "stats":        {"tuples", "blocks", "columns": {col: {...}}},
    }}
    """
    tables: Dict[str, Any] = {}
    with conn.cursor() as cur:
        # Tables, with T(R) and B(R).
        cur.execute("""
            SELECT c.oid, c.relname, c.reltuples::bigint, c.relpages
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind IN ('r', 'p')
            ORDER BY c.relname""", (schema,))
        oids = {}
        for oid, name, tuples, blocks in cur.fetchall():
            oids[oid] = name
            tables[name] = {
                "columns": [], "primary_key": [], "foreign_keys": [], "indexes": [],
                # reltuples is -1 if the table has never been analysed.
                "stats": {"tuples": max(tuples, 0), "blocks": blocks, "columns": {}},
            }
        if not tables:
            return tables

        # Columns and their types.
        cur.execute("""
            SELECT a.attrelid, a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull
            FROM pg_attribute a
            WHERE a.attrelid = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attrelid, a.attnum""", (list(oids),))
        for oid, col, col_type, nullable in cur.fetchall():
            tables[oids[oid]]["columns"].append(
                {"name": col, "type": col_type, "nullable": nullable})

        # Primary and foreign keys.
        cur.execute("""
            SELECT con.conrelid, con.contype, con.confrelid,
                   ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(n, i)
                         JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n
                         ORDER BY k.i),
                   ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(n, i)
                         JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.n
                         ORDER BY k.i)
            FROM pg_constraint con
            WHERE con.conrelid = ANY(%s) AND con.contype IN ('p', 'f')""", (list(oids),))
        for oid, kind, ref_oid, cols, ref_cols in cur.fetchall():
            if kind == "p":
                tables[oids[oid]]["primary_key"] = list(cols)
            else:
                ref_table = oids.get(ref_oid) or _relation_name(cur, ref_oid)
                tables[oids[oid]]["foreign_keys"].append(
                    {"columns": list(cols), "ref_table": ref_table, "ref_columns": list(ref_cols)})

        # Indexes: which columns, unique or not, B+ tree or hash (Wk 5-9),
        # and whether the table has been CLUSTERed on it.
        cur.execute("""
            SELECT i.indrelid, ic.relname, am.amname, i.indisunique, i.indisprimary,
                   i.indisclustered,
                   ARRAY(SELECT pg_get_indexdef(i.indexrelid, k.i::int, true)
                         FROM generate_series(1, i.indnkeyatts) k(i))
            FROM pg_index i
            JOIN pg_class ic ON ic.oid = i.indexrelid
            JOIN pg_am am ON am.oid = ic.relam
            WHERE i.indrelid = ANY(%s)
            ORDER BY ic.relname""", (list(oids),))
        for oid, name, method, unique, primary, clustered, cols in cur.fetchall():
            tables[oids[oid]]["indexes"].append({
                "name": name, "columns": list(cols), "unique": unique,
                "primary": primary, "method": method, "clustered": clustered})

        # Column statistics, V(R,A) and histograms.
        cur.execute("""
            SELECT tablename, attname, n_distinct, null_frac, correlation,
                   most_common_vals::text, histogram_bounds::text
            FROM pg_stats WHERE schemaname = %s""", (schema,))
        for table, col, n_distinct, null_frac, corr, mcv, hist in cur.fetchall():
            if table not in tables:
                continue
            tuples = tables[table]["stats"]["tuples"]
            # n_distinct > 0 is the number of distinct values; n_distinct < 0
            # is minus the fraction of rows that are distinct (used when the
            # count grows with the table, e.g. a key column).
            distinct = n_distinct if n_distinct >= 0 else round(-n_distinct * tuples)
            tables[table]["stats"]["columns"][col] = {
                "distinct": int(distinct),          # V(R, A)
                "null_fraction": null_frac,
                "correlation": corr,
                "most_common_values": mcv,
                "histogram_bounds": hist,
            }
    conn.rollback()   # end the read-only catalog transaction
    return tables


def _relation_name(cur, oid) -> str:
    cur.execute("SELECT relname FROM pg_class WHERE oid = %s", (oid,))
    row = cur.fetchone()
    return row[0] if row else str(oid)


def list_schemas(conn) -> List[str]:
    """Schemas in the connected database that contain tables (excluding
    PostgreSQL's own), for the GUI's schema selector."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT n.nspname
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%'
            ORDER BY 1""")
        schemas = [row[0] for row in cur.fetchall()]
    conn.rollback()
    return schemas


def describe_schema(schema_info: Dict[str, Any], with_stats: bool = True) -> str:
    """Compact text description of a schema (tables, keys, indexes, sizes),
    e.g. to show in the GUI or to include in the AI prompt."""
    lines = []
    for table, info in schema_info.items():
        cols = ", ".join(f"{c['name']} {c['type']}" for c in info["columns"])
        lines.append(f"TABLE {table} ({cols})")
        if info["primary_key"]:
            lines.append(f"  PRIMARY KEY ({', '.join(info['primary_key'])})")
        for fk in info["foreign_keys"]:
            lines.append(f"  FOREIGN KEY ({', '.join(fk['columns'])}) REFERENCES "
                         f"{fk['ref_table']} ({', '.join(fk['ref_columns'])})")
        for ix in info["indexes"]:
            kind = "UNIQUE " if ix["unique"] else ""
            lines.append(f"  {kind}INDEX {ix['name']} USING {ix['method']} ({', '.join(ix['columns'])})")
        if with_stats:
            s = info["stats"]
            lines.append(f"  ROWS {s['tuples']}, PAGES {s['blocks']}")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# 3. Getting PostgreSQL's query execution plan
# -----------------------------------------------------------------------------

def get_postgres_plan_json(conn, query: str, analyze: bool = False,
                           parallel: bool = True, timeout_ms: int = 60000) -> Dict[str, Any]:
    """Run EXPLAIN (FORMAT JSON) on `query` and return PostgreSQL's plan as a dict.

    Without ANALYZE, EXPLAIN does not run the query. It returns the physical
    query plan the optimizer picked at compile time, i.e. the alternative
    with the lowest estimated cost, together with the estimated row counts
    and costs it used to decide (Wk 9-2, Wk 10-1).

    analyze=True also runs the query and adds the actual row counts and
    times, which shows how far off the cardinality estimates were (Wk 10-1).

    parallel=False stops PostgreSQL from planning parallel workers, so the
    tree has no Gather or partial Aggregate nodes and looks like the trees
    in the lectures and in Figure 1 of the brief.

    The transaction is READ ONLY and always rolled back, so even with
    ANALYZE a query cannot change the data.
    """
    query = query.strip().rstrip(";").strip()
    if not query:
        raise DatabaseError("The query is empty.")
    options = "ANALYZE, FORMAT JSON" if analyze else "FORMAT JSON"
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = %s", (int(timeout_ms),))
            if not parallel:
                cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            cur.execute(f"EXPLAIN ({options}) {query}")
            result = cur.fetchone()[0]
    except psycopg2.Error as e:
        message = (e.pgerror or str(e)).strip()
        raise DatabaseError(f"PostgreSQL could not plan this query:\n{message}") from e
    finally:
        conn.rollback()
    # psycopg2 already decodes the json column; older drivers return text.
    if isinstance(result, str):
        result = json.loads(result)
    return result[0]


def get_postgres_plan(conn, query: str, analyze: bool = False,
                      parallel: bool = True) -> PlanNode:
    """PostgreSQL's QEP for `query` as a PlanNode tree."""
    explained = get_postgres_plan_json(conn, query, analyze=analyze, parallel=parallel)
    return parse_postgres_plan(explained["Plan"])


# -----------------------------------------------------------------------------
# 4. Converting PostgreSQL's JSON plan into PlanNode trees
# -----------------------------------------------------------------------------
# What the main PostgreSQL node types are, in course terms:
#
#   Access methods (Wk 8-2):
#     Seq Scan           sequential scan of every block of the table
#     Index Scan         index lookup, then fetch the matching tuples
#     Index Only Scan    answered from the index alone, no table access
#     Bitmap Index Scan  index lookup that only collects record ids
#     Bitmap Heap Scan   fetches the tuples for those record ids; with
#                        BitmapAnd / BitmapOr it combines several indexes
#                        (multi-index scan)
#
#   Join algorithms (Wk 7-1, 7-2, 8-1):
#     Nested Loop        nested-loop join; when the inner child is an index
#                        scan on the join attribute it is an index
#                        nested-loop join
#     Hash Join          hash join; its inner child is a Hash node (the
#                        build phase), the outer child is probed against it
#     Merge Join         sort-merge join; inputs arrive sorted on the join key
#
#   Other operators:
#     Sort               sorting (external merge sort if it does not fit in
#                        memory, Wk 7-1)
#     Aggregate          grouping and aggregation: PostgreSQL's Strategy
#                        says plain, sort-based (GroupAggregate) or
#                        hash-based (HashAggregate)
#     Gather             collects the outputs of parallel workers
#     Materialize        stores a child's output so it can be re-read (e.g.
#                        the inner input of a nested loop)
#     Memoize            caches inner results of a nested loop by key
#     Limit, Unique, Hash, Subquery Scan, CTE Scan, ...

# The JSON's "Aggregate" + "Strategy" is shown in EXPLAIN's text output as
# one of these names. We use the same names so the tree matches what people
# see in pgAdmin.
_AGGREGATE_NAMES = {
    "Plain": "Aggregate",
    "Sorted": "GroupAggregate",
    "Hashed": "HashAggregate",
    "Mixed": "MixedAggregate",
}

_JOIN_NODES = {"Nested Loop", "Hash Join", "Merge Join"}
_INDEX_SCANS = {"Index Scan", "Index Only Scan", "Bitmap Index Scan"}

# Type casts PostgreSQL adds to conditions, e.g. '1993-11-01'::date.
# They are removed from the displayed conditions (the originals are kept
# in details) so conditions read like the SQL the user wrote.
_CAST = re.compile(
    r"::(?:\"char\"|character varying|character|bpchar|text|name|date|"
    r"timestamp(?: without time zone| with time zone)?|time(?: without time zone)?|"
    r"numeric|integer|bigint|smallint|double precision|real|interval|boolean)(?:\[\])?")


def clean_condition(condition: Optional[str]) -> Optional[str]:
    """Remove PostgreSQL's type casts from a condition string."""
    if not condition:
        return None
    return _CAST.sub("", condition)


def parse_postgres_plan(node: Dict[str, Any], parent_table: Optional[str] = None) -> PlanNode:
    """Convert one node of PostgreSQL's JSON plan (and its subtree) into a PlanNode.

    parent_table passes the table of a Bitmap Heap Scan down to its Bitmap
    Index Scan children, which PostgreSQL reports without a table name.
    """
    node_type = node["Node Type"]

    # Operator name.
    operator = node_type
    if node_type == "Aggregate":
        operator = _AGGREGATE_NAMES.get(node.get("Strategy"), "Aggregate")

    # Table: base relation for scans; other named inputs (CTEs, functions,
    # subqueries) keep their names too.
    table = node.get("Relation Name") or node.get("CTE Name") or node.get("Function Name")
    if table is None and node_type in ("Bitmap Index Scan", "BitmapAnd", "BitmapOr"):
        table = parent_table

    # Index used by an index-based access method.
    index = node.get("Index Name")

    # Selection condition at this node. For an index scan the selection is
    # split between the Index Cond (used to search the index) and the Filter
    # (checked on each fetched tuple, Wk 10-1 "Choosing a selection method");
    # both together are the selection the node applies.
    selection_parts = []
    if node_type in _INDEX_SCANS and node.get("Index Cond"):
        selection_parts.append(node["Index Cond"])
    if node_type == "Bitmap Heap Scan" and node.get("Recheck Cond"):
        selection_parts.append(node["Recheck Cond"])
    if node.get("Filter"):
        selection_parts.append(node["Filter"])
    if node.get("Join Filter") and node_type not in _JOIN_NODES:
        selection_parts.append(node["Join Filter"])
    selection = " AND ".join(selection_parts) if selection_parts else None

    # Join condition: Hash Cond for a hash join, Merge Cond for a sort-merge
    # join, Join Filter for a nested loop.
    join_condition = None
    if node_type in _JOIN_NODES:
        join_condition = node.get("Hash Cond") or node.get("Merge Cond") or node.get("Join Filter")

    details = {
        "postgres_node_type": node_type,
        "alias": node.get("Alias"),
        "join_type": node.get("Join Type"),
        "parent_relationship": node.get("Parent Relationship"),
        "subplan_name": node.get("Subplan Name"),
        "parallel": node.get("Parallel Aware", False),
        "partial_mode": node.get("Partial Mode"),
        "workers_planned": node.get("Workers Planned"),
        "sort_key": node.get("Sort Key"),
        "group_key": node.get("Group Key"),
        "cache_key": node.get("Cache Key"),
        "scan_direction": node.get("Scan Direction"),
        "index_condition": clean_condition(node.get("Index Cond")),
        "filter_only": clean_condition(node.get("Filter")),
        "raw_conditions": {k: node[k] for k in ("Index Cond", "Recheck Cond", "Filter",
                                                 "Hash Cond", "Merge Cond", "Join Filter")
                           if k in node},
        # Optimizer estimates: cardinality estimation and cost model (Wk 9-2).
        "estimated_rows": node.get("Plan Rows"),
        "startup_cost": node.get("Startup Cost"),
        "total_cost": node.get("Total Cost"),
        # Only present with analyze=True.
        "actual_rows": node.get("Actual Rows"),
        "actual_time_ms": node.get("Actual Total Time"),
        "actual_loops": node.get("Actual Loops"),
    }
    details = {k: v for k, v in details.items() if v not in (None, [], {}, "")}

    children = [parse_postgres_plan(child, table if node_type == "Bitmap Heap Scan" else parent_table)
                for child in node.get("Plans", [])]

    plan_node = PlanNode(
        operator=operator,
        table=table,
        index=index,
        join_condition=clean_condition(join_condition),
        filter=clean_condition(selection),
        children=children,
        details=details,
    )

    if node_type == "Nested Loop":
        _mark_index_nested_loop(plan_node)
    if node_type == "Hash Join" and len(children) == 2:
        # Build/probe roles of hash join (Wk 7-2): PostgreSQL builds the hash
        # table on the inner (second) input and probes it with the outer one.
        plan_node.details["build_side"] = "inner"
    return plan_node


def _mark_index_nested_loop(join: PlanNode) -> None:
    """Recognise an index nested-loop join (Wk 8-1).

    PostgreSQL shows it as a Nested Loop whose inner child (possibly under a
    Memoize or Materialize node) is an index scan, with the join condition
    inside that scan's Index Cond, e.g. (p_partkey = lineitem.l_partkey),
    rather than on the join itself. When the inner scan's index condition
    refers to a relation from the outer side, that condition is the join
    condition, so it is copied up to the join node.
    """
    if len(join.children) != 2:
        return
    outer, inner = join.children
    while inner.operator in ("Memoize", "Materialize") and inner.children:
        inner = inner.children[0]
    if inner.operator == "Bitmap Heap Scan" and inner.children:
        lookup = inner.children[0]
    else:
        lookup = inner
    condition = lookup.details.get("index_condition")
    if not condition or lookup.operator not in _INDEX_SCANS:
        return
    outer_names = {n.details.get("alias") for n in outer.walk()} | {n.table for n in outer.walk()}
    outer_names.discard(None)
    if any(re.search(rf"\b{re.escape(name)}\.", condition) for name in outer_names):
        join.details["index_nested_loop"] = True
        if join.join_condition is None:
            # PostgreSQL writes the inner index column without its table,
            # e.g. (id = c.person_id); add it so the condition reads
            # (n.id = c.person_id) at the join.
            inner_name = lookup.details.get("alias") or inner.details.get("alias") or lookup.table
            if inner_name:
                condition = re.sub(r"\(([A-Za-z_]\w*)(\s*(?:=|<>|<=|>=|<|>))",
                                   rf"({inner_name}.\1\2", condition)
            join.join_condition = condition
        # The index lookup is the join, not a selection: keep only the real
        # filter (if any) on the inner scan nodes.
        for scan in {id(lookup): lookup, id(inner): inner}.values():
            scan.filter = scan.details.get("filter_only")


# =============================================================================
# AI part (Member 2)
# =============================================================================
# Asks an AI agent (Google Gemini) for the query plan it predicts and turns the
# answer into the same PlanNode tree the PostgreSQL part produces, so
# comparison.py can compare the two trees directly.
#
# Main functions:
#
#   build_ai_prompt     schema (from describe_schema) + query -> prompt
#   ask_ai              calls Gemini; retries on rate limits
#   extract_plan_json   gets the JSON out of the answer
#   normalize_operator  maps AI operator names to PostgreSQL's
#                       (e.g. "Full Table Scan" -> "Seq Scan")
#   parse_ai_plan       builds the PlanNode tree and normalises it
#   get_ai_plan         runs all of the above; raises AIError on failure
#   ai_prompt_preview   the exact prompt sent, e.g. for the GUI
#
# The API key is read from GEMINI_API_KEY or .env. Only operator names are
# mapped, so nothing here is specific to TPC-H.

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

class AIError(Exception):
    """Readable error for the GUI when the AI request or parsing fails."""


# Set GEMINI_MODEL to override the default model.
DEFAULT_AI_MODEL = "gemini-3.1-flash-lite"

AI_OPERATORS = [
    "Seq Scan",
    "Index Scan",
    "Index Only Scan",
    "Bitmap Heap Scan",
    "Bitmap Index Scan",
    "BitmapAnd",
    "BitmapOr",
    "Nested Loop",
    "Hash Join",
    "Merge Join",
    "Hash",
    "Sort",
    "Incremental Sort",
    "Aggregate",
    "HashAggregate",
    "GroupAggregate",
    "Unique",
    "Limit",
    "Materialize",
    "Memoize",
    "Subquery Scan",
    "CTE Scan",
    "Result",
    "Append",
    "SetOp",
    "WindowAgg",
]

JOIN_OPERATORS = {"Nested Loop", "Hash Join", "Merge Join"}
JOIN_TYPES = {"inner", "left", "right", "full", "semi", "anti"}

# A small alias list, only for common alternate names.
OPERATOR_ALIASES = {
    "sequential scan": "Seq Scan",
    "full table scan": "Seq Scan",
    "table scan": "Seq Scan",
    "index seek": "Index Scan",
    "index lookup": "Index Scan",
    "covering index scan": "Index Only Scan",
    "hash match": "Hash Join",
    "sort merge join": "Merge Join",
    "nested loop join": "Nested Loop",
    "hash aggregate": "HashAggregate",
    "stream aggregate": "GroupAggregate",
    "sort aggregate": "GroupAggregate",
    "plain aggregate": "Aggregate",
    "bitmap scan": "Bitmap Heap Scan",
    "hash build": "Hash",
    "hash table build": "Hash",
    "seqscan": "Seq Scan",
    "full scan": "Seq Scan",
    "index range scan": "Index Scan",
    "index unique scan": "Index Scan",
    "btree index scan": "Index Scan",
    "b tree index scan": "Index Scan",
    "clustered index scan": "Index Scan",
    "nested loops": "Nested Loop",
    "loop join": "Nested Loop",
    "hash aggregation": "HashAggregate",
    "hash group by": "HashAggregate",
    "hash distinct": "HashAggregate",
    "group aggregate": "GroupAggregate",
    "sorted aggregate": "GroupAggregate",
    "scalar aggregate": "Aggregate",
    "aggregation": "Aggregate",
    "external sort": "Sort",
    "external merge sort": "Sort",
    "top n sort": "Sort",
    "distinct": "Unique",
    "top": "Limit",
    "materialise": "Materialize",
    "bitmap and": "BitmapAnd",
    "bitmap or": "BitmapOr",
}
# Also accept PostgreSQL's own names in any capitalisation ("index only scan").
OPERATOR_ALIASES.update({op.lower(): op for op in AI_OPERATORS})

# Nodes PostgreSQL never has on their own, so they are removed or merged.
PROJECTION_NAMES = {"projection", "project", "output"}
SELECTION_NAMES = {"filter", "selection"}
# -----------------------------------------------------------------------------
# 1. Build the prompt
# -----------------------------------------------------------------------------

def build_ai_prompt(
    schema_text: str,
    query: str,
    postgres_version: Optional[str] = None,
) -> tuple[str, str]:
    """Build the system prompt and user prompt sent to the AI."""

    version_text = f" PostgreSQL {postgres_version}" if postgres_version else ""

    system_prompt = f"""
You are predicting a physical query execution plan (QEP) for PostgreSQL{version_text}.
Given the database schema and SQL query, predict a plausible plan PostgreSQL
could choose. This is a prediction, not a guaranteed reproduction of its optimizer.

Return exactly one JSON object, with no markdown or explanation outside JSON.

The JSON must have this format:
{{
  "plan": {{
    "operator": "one allowed operator",
    "table": "real table name or null",
    "index": "real index name or null",
    "join_type": "Inner, Left, Right, Full, Semi, Anti, or null",
    "join_condition": "SQL condition or null",
    "filter": "SQL filter condition or null",
    "estimated_rows":  integer,
    "reason": "one short reason",
    "children": []
  }},
  "summary": "brief summary of the main plan choices"
}}

Allowed operators:
{", ".join(AI_OPERATORS)}

Rules:
- Use only table and index names provided in the schema.
- Use real table names, not aliases, in the table field.
- Put the outer/left input first and the inner/right input second.
- For a Hash Join, its second child must be a Hash node with one child.
- For a Bitmap Heap Scan, include a Bitmap Index Scan child.
- Include join conditions on join nodes.
- Put selection conditions in filter.
- Use Aggregate for aggregation without GROUP BY.
- Use HashAggregate or GroupAggregate for aggregation with GROUP BY.
- estimated_rows must be a non-negative integer.
- children must always be a JSON array.
- Do not use Gather or Gather Merge.
- Do not invent schema objects.
""".strip()

    user_prompt = f"""
Database schema, keys, indexes, and table statistics:
{schema_text}

SQL query:
{query.strip().rstrip(";")}

Return the JSON plan.
""".strip()

    return system_prompt, user_prompt


# -----------------------------------------------------------------------------
# 2. Ask Gemini
# -----------------------------------------------------------------------------

def ask_ai(
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout_s: int = 120,
) -> str:
    """Call Gemini and return its text response."""
 
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise AIError(
            "Gemini package is missing. Install it with: pip install google-genai"
        ) from exc
 
    key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise AIError(
            "No Gemini API key found. Set the GEMINI_API_KEY environment variable."
        )
 
    model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_AI_MODEL
 
    client = genai.Client(
        api_key=key,
        http_options=types.HttpOptions(timeout=timeout_s * 1000),
    )
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0,
        response_mime_type="application/json",
    )
 
    # Free-tier keys have a per-minute limit: on "too many requests" (429) or
    # "server busy" (500/503), wait 10s, 30s, 60s and try again.
    for wait in (10, 30, 60, None):
        try:
            response = client.models.generate_content(
                model=model, contents=user_prompt, config=config)
            break
        except Exception as exc:
            if getattr(exc, "code", None) in (429, 500, 503) and wait is not None:
                time.sleep(wait)
                continue
            raise AIError(f"Could not get a response from Gemini: {exc}") from exc
 
    answer = getattr(response, "text", None)
    if not answer or not answer.strip():
        raise AIError("Gemini returned an empty response.")
 
    return answer


# -----------------------------------------------------------------------------
# 3. Extract and validate the JSON response
# -----------------------------------------------------------------------------

def extract_plan_json(answer: str) -> Dict[str, Any]:
    """Parse JSON from Gemini's response, including optional code fences."""

    if not answer or not answer.strip():
        raise ValueError("The AI response was empty.")

    cleaned = re.sub(r"```(?:json)?", "", answer, flags=re.IGNORECASE).strip()
    cleaned = cleaned.replace("```", "").strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: extract the first JSON object from surrounding text.
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("The response did not contain a JSON object.")
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"The JSON response is invalid: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("The JSON response must be an object.")

    plan = data.get("plan")
    if plan is None and "operator" in data:
        plan = data

    if not isinstance(plan, dict):
        raise ValueError('The JSON response must contain a "plan" object.')

    return {"plan": plan, "summary": data.get("summary")}


# -----------------------------------------------------------------------------
# 4. Convert AI JSON into the shared PlanNode tree
# -----------------------------------------------------------------------------

def normalize_operator(raw_operator: str):
    """Return (postgres_name, join_type or None, recognised)."""
    name = raw_operator.strip()
    if name in AI_OPERATORS:
        return name, None, True

    lowered = name.lower()
    if "hash" in lowered and "build" in lowered:          # "Hash (build phase)"
        return "Hash", None, True

    key = re.sub(r"\([^)]*\)", " ", lowered)              # drop "(...)"
    key = re.sub(r"[-_]", " ", key)                        # "sort-merge" -> "sort merge"
    key = re.sub(r"\s+", " ", key).strip()
    if key in OPERATOR_ALIASES:
        return OPERATOR_ALIASES[key], None, True

    join_type = next((w.title() for w in key.split() if w in JOIN_TYPES), None)
    if "hash" in key and "join" in key:
        return "Hash Join", join_type, True
    if "merge" in key and "join" in key:
        return "Merge Join", join_type, True
    if "nested loop" in key:
        return "Nested Loop", join_type, True

    return name, None, False

def _clean_optional_text(value: Any) -> Optional[str]:
    """Convert null-like values to None and other values to clean strings."""

    if value is None:
        return None

    text = str(value).strip()
    if text.lower() in {"", "null", "none"}:
        return None

    return text

def _clean_condition_text(value: Any) -> Optional[str]:
    """Remove casts (Member 1's clean_condition) and wrap in parentheses,
    so conditions look the same as in the PostgreSQL tree."""
    text = _clean_optional_text(value)
    if text is None:
        return None
    text = clean_condition(text)
    depth = 0
    for i, ch in enumerate(text):
        depth += (ch == "(") - (ch == ")")
        if depth == 0 and i < len(text) - 1:      # "(a) AND (b)" or "a > 5"
            return f"({text})"
    return text if text.startswith("(") else f"({text})"


def _query_aliases(query: str, table_names) -> Dict[str, str]:
    """Aliases the query gives its tables, e.g. {"o": "orders"}."""
    tables = {str(t).lower(): str(t) for t in table_names}
    skip = {"where", "join", "inner", "left", "right", "full", "cross", "natural",
            "on", "using", "group", "order", "limit", "having", "as"}
    aliases = {}
    pattern = r"(?:\bfrom|\bjoin|,)\s+(\w+)\s+(?:as\s+)?(\w+)"
    for table, alias in re.findall(pattern, query, flags=re.IGNORECASE):
        if table.lower() in tables and alias.lower() not in skip:
            aliases[alias.lower()] = tables[table.lower()]
    return aliases


def _postgres_version(conn) -> Optional[str]:
    """e.g. 160015 -> '16.15'."""
    try:
        v = conn.server_version
        return f"{v // 10000}.{v % 100}"
    except Exception:
        return None
    
def parse_ai_plan(
    data: Dict[str, Any],
    schema_info: Dict[str, Any],
    query: str = "",
) -> PlanNode:
    """Validate the plan's basic structure and convert it to PlanNode."""

    root_json = data.get("plan", data)
    if not isinstance(root_json, dict):
        raise ValueError("The plan root must be a JSON object.")

    # Build sets of real tables and indexes from Member 1's schema output.
    tables = {str(table).lower(): str(table) for table in schema_info}
    indexes: Dict[str, tuple[str, str]] = {}

    for table_name, table_info in schema_info.items():
        for index_info in table_info.get("indexes", []):
            index_name = index_info.get("name")
            if index_name:
                indexes[str(index_name).lower()] = (str(index_name), str(table_name))

    aliases = _query_aliases(query, schema_info) if query else {}
    node_count = 0

    def convert(node_data: Any, depth: int = 0) -> PlanNode:
        nonlocal node_count
        node_count += 1

        if not isinstance(node_data, dict):
            raise ValueError("Each plan node must be a JSON object.")
        if depth > 40 or node_count > 500:
            raise ValueError("The AI plan is too large or deeply nested.")

        raw_operator = node_data.get("operator")
        if not isinstance(raw_operator, str) or not raw_operator.strip():
            raise ValueError('Each node must have a non-empty "operator".')

        children_data = node_data.get("children") or []
        if not isinstance(children_data, list):
            raise ValueError('"children" must be a JSON array.')

        # Drop Projection nodes; merge Filter nodes into the node below.
        op_key = raw_operator.strip().lower()
        if op_key in PROJECTION_NAMES and len(children_data) == 1:
            return convert(children_data[0], depth + 1)
        if op_key in SELECTION_NAMES and len(children_data) == 1:
            child = convert(children_data[0], depth + 1)
            condition = _clean_condition_text(node_data.get("filter"))
            if condition:
                child.filter = f"{child.filter} AND {condition}" if child.filter else condition
            return child

        operator, name_join_type, known = normalize_operator(raw_operator)
        table = _clean_optional_text(node_data.get("table"))
        index = _clean_optional_text(node_data.get("index"))
        filter_text = _clean_condition_text(node_data.get("filter"))
        join_condition = _clean_condition_text(node_data.get("join_condition"))

        details: Dict[str, Any] = {"ai_operator": raw_operator}
        if not known:
            details["unknown_operator"] = True

        # Check table names against the database schema (aliases allowed).
        if table:
            name = table.split(".")[-1].lower()            # public.orders -> orders
            if name in tables:
                table = tables[name]
            elif name in aliases:
                details["alias"] = table
                table = aliases[name]
            else:
                details["unknown_table"] = True

        # Check index names against the schema.
        if index:
            index_info = indexes.get(index.lower())
            if index_info:
                real_index, index_table = index_info
                index = real_index

                # If the AI omitted the table on an index scan, infer it.
                if table is None and operator in {
                    "Index Scan", "Index Only Scan", "Bitmap Index Scan"
                }:
                    table = index_table
                elif table and table.lower() != index_table.lower():
                    details["index_wrong_table"] = True
            else:
                details["unknown_index"] = True

        if operator in JOIN_OPERATORS:
            join_type = (_clean_optional_text(node_data.get("join_type")) or name_join_type or "Inner")
            join_type = join_type.lower().replace("outer", "").strip().title()
            if join_type.lower() not in JOIN_TYPES:
                join_type = "Inner"
            details["join_type"] = join_type

        reason = _clean_optional_text(node_data.get("reason"))
        if reason:
            details["ai_reason"] = reason

        estimated_rows = node_data.get("estimated_rows")
        if isinstance(estimated_rows, (int, float)) and not isinstance(estimated_rows, bool):
            details["estimated_rows"] = max(0, int(estimated_rows))

        children = [convert(child, depth + 1) for child in children_data]

        # PostgreSQL represents the build input of a Hash Join with a Hash node.
        if operator == "Hash Join" and len(children) == 2:
            if children[1].operator != "Hash":
                children[1] = PlanNode(
                    operator="Hash",
                    children=[children[1]],
                    details={"added_by_normalization": True},
                )

        # PostgreSQL's Bitmap Heap Scan usually has a Bitmap Index Scan child.
        if operator == "Bitmap Heap Scan" and not children and index:
            children = [
                PlanNode(
                    operator="Bitmap Index Scan",
                    table=table,
                    index=index,
                    filter=filter_text,
                    details={"added_by_normalization": True},
                )
            ]
            index = None

        return PlanNode(
            operator=operator,
            table=table,
            index=index,
            join_condition=join_condition,
            filter=filter_text,
            children=children,
            details=details,
        )

    root = convert(root_json)

    summary = _clean_optional_text(data.get("summary"))
    if summary:
        root.details["ai_summary"] = summary

    return root


# -----------------------------------------------------------------------------
# 5. Main function
# -----------------------------------------------------------------------------

_AI_CACHE: Dict[tuple, str] = {}


def get_ai_plan(
    conn,
    query: str,
    schema_info: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    llm: Optional[Callable[[str, str], str]] = None,
    retries: int = 1,
    use_cache: bool = True,
) -> PlanNode:
    """
    Get the AI-predicted QEP as a PlanNode tree.
 
    conn:
        Member 1's PostgreSQL connection. May be None if schema_info is supplied.
    schema_info:
        Output of get_schema(conn). If omitted, it is obtained from conn.
    llm:
        Optional custom callable(system_prompt, user_prompt) -> response text.
    retries:
        Number of retries after an invalid AI response. Default is one retry.
    """
 
    query = (query or "").strip().rstrip(";").strip()
    if not query:
        raise AIError("The SQL query is empty.")
 
    if schema_info is None:
        if conn is None:
            raise AIError("A database connection or schema_info must be provided.")
        schema_info = get_schema(conn)
 
    if not schema_info:
        raise AIError("No tables were found in the selected schema.")
 
    system_prompt, user_prompt = build_ai_prompt(
        describe_schema(schema_info),
        query,
        _postgres_version(conn),
    )
 
    selected_model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_AI_MODEL
 
    def call_ai(system: str, user: str) -> str:
        if llm is not None:
            return llm(system, user)
        return ask_ai(
            system,
            user,
            model=selected_model,
            api_key=api_key,
        )
 
    cache_key = (selected_model, system_prompt, user_prompt)
    response_text = _AI_CACHE.get(cache_key) if use_cache else None
 
    attempts = 0
    last_error: Optional[Exception] = None
 
    while attempts <= retries:
        attempts += 1
 
        try:
            if response_text is None:
                response_text = call_ai(system_prompt, user_prompt)
 
            parsed_json = extract_plan_json(response_text)
            plan = parse_ai_plan(parsed_json, schema_info, query)
 
            plan.details["ai_model"] = selected_model if llm is None else "custom"
            plan.details["ai_attempts"] = attempts
            plan.details["ai_raw_response"] = response_text
 
            if use_cache:
                _AI_CACHE[cache_key] = response_text
 
            return plan
 
        except (ValueError, TypeError, KeyError) as exc:
            last_error = exc
            response_text = None
 
            if attempts <= retries:
                user_prompt = (
                    f"{user_prompt}\n\n"
                    f"Your previous response could not be parsed: {exc}\n"
                    "Please return a corrected JSON plan only."
                )
 
    raise AIError(
        f"The AI did not return a usable plan after {attempts} attempt(s): "
        f"{last_error}"
    )
 
 
def ai_prompt_preview(
    conn,
    query: str,
    schema_info: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the prompt text, useful for previewing it in the GUI."""
 
    if schema_info is None:
        if conn is None:
            raise AIError("A database connection or schema_info must be provided.")
        schema_info = get_schema(conn)
 
    system_prompt, user_prompt = build_ai_prompt(
        describe_schema(schema_info),
        query,
    )
 
    return (
        "=== System prompt ===\n"
        f"{system_prompt}\n\n"
        "=== User prompt ===\n"
        f"{user_prompt}"
    )