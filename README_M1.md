# PostgreSQL part (Member 1)

This covers the PostgreSQL section of `preprocessing.py`, plus a helper that builds the TPC-H test database.

## 1. One-time setup on Windows

1. **Install PostgreSQL.** Use the Windows installer from postgresql.org (EDB); version 16 or 17 is fine.
   - Keep the default port, 5432.
   - Remember the password you set for the `postgres` user.
   - pgAdmin 4 is installed along with it.
2. **Install the Python packages.** Run this in a terminal from the project folder:
   ```
   pip install -r requirements.txt
   ```
3. **Generate the TPC-H data**, about 1 GB at scale factor 1:
   ```
   tpchgen-cli -s 1 --output-dir C:\tpch_data
   ```
   This writes the same 8 `.tbl` files as the Appendix's `dbgen`, without building it in Visual Studio. You can also use the Appendix's `dbgen` output; the loader accepts `.tbl` or `.csv`.
4. **Load the data.** This takes about 5 minutes:
   ```
   python setup\load_tpch.py --data-dir C:\tpch_data --password YOUR_PASSWORD
   ```
   It does the following:
   - creates the database `TPC-H`;
   - creates the 8 tables from Appendix II;
   - loads the files, stripping the trailing `|` the Appendix mentions;
   - adds the primary and foreign keys;
   - adds 8 secondary indexes (`--no-extra-indexes` skips them);
   - runs `ANALYZE`.

   Use `--recreate` to drop the database and reload it.
5. **Check that it works:**
   ```
   python tests\test_postgres_part.py --password YOUR_PASSWORD
   ```
   It plans 13 varied queries, prints their trees and should end with `ALL CHECKS PASSED`.

## 2. How the rest of the team uses it

```python
import preprocessing as pp

conn = pp.connect("TPC-H", password="...")            # schema="movies" if not in public
pp.list_databases(password="...")                     # GUI: database selector
pp.list_schemas(conn)                                 # GUI: schema selector

schema = pp.get_schema(conn)                          # tables, columns, keys, indexes, stats
text = pp.describe_schema(schema)                     # same as text, e.g. for the AI prompt

tree = pp.get_postgres_plan(conn, sql)                # PostgreSQL's QEP as a PlanNode tree
tree = pp.get_postgres_plan(conn, sql, parallel=False)  # without Gather / partial Aggregate
tree = pp.get_postgres_plan(conn, sql, analyze=True)  # also runs it: actual rows in details

print(tree.pretty())                                  # indented text view
for node in tree.walk(): ...                          # post-order: leaves first, root last
```

**Errors.** Bad SQL, a missing table or a failed connection all raise `pp.DatabaseError` with a readable message for the GUI to show. The connection stays usable afterwards.

**Safety.** Every `EXPLAIN` runs in a read-only transaction that is rolled back, so even with `analyze=True` a query can't change the data.

## 3. What a `PlanNode` holds

These are the team's six fields:

| Field | PostgreSQL example |
|---|---|
| `operator` | `Seq Scan`, `Index Scan`, `Bitmap Heap Scan`, `Hash Join`, `Nested Loop`, `Merge Join`, `Hash`, `Sort`, `HashAggregate`, `GroupAggregate`, `Gather`, ... |
| `table` | `lineitem` |
| `index` | `idx_lineitem_shipdate` |
| `join_condition` | `(lineitem.l_partkey = part.p_partkey)` |
| `filter` | `((l_shipdate >= '1993-11-01') AND (l_shipdate < '1993-12-01 00:00:00'))` |
| `children` | outer (left) child first, inner (right) child second |

`details` is optional extra information that M3 and M4 can ignore:
- the estimated rows and cost;
- the join type (Semi, Anti, Left);
- whether a node is parallel;
- sort and group keys;
- `index_nested_loop`, and `build_side` for hash joins;
- the original conditions with PostgreSQL's `::date`-style casts.

The casts are removed from `filter` and `join_condition` so the conditions read like the SQL the user wrote.

**Index nested-loop joins.** PostgreSQL puts the join condition inside the inner index scan rather than on the `Nested Loop` node. The parser moves it up to the join, so a join's `join_condition` is always filled in.

## 4. Tested on

- **TPC-H at scale factor 1**, 13 queries:
  - the brief's example;
  - index, bitmap and sequential scans;
  - an index nested-loop join;
  - 2-, 3- and 4-way joins;
  - a non-equi join;
  - a merge join;
  - EXISTS and scalar subqueries;
  - a CTE with DISTINCT;
  - an anti join.

  Each query was run with and without parallel plans.
- **A second, IMDB-style schema** (`title`, `name`, `cast_info`, `kind_type`) in a non-`public` schema, with a hash index. No code changes were needed, which checks the brief's "no schema-specific hardcoding" requirement.
