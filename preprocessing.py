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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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
# To be added: ask the AI agent for its plan, validate the JSON, normalise
# the operator names to PostgreSQL's (e.g. "Full Table Scan" -> "Seq Scan"),
# and return a PlanNode tree. describe_schema(get_schema(conn)) gives the
# schema, keys and indexes as text for the prompt.
