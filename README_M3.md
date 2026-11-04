# Comparison (Member 3)

This covers `comparison.py`. It takes PostgreSQL's plan and the AI's plan for the same query and finds:
- which operators correspond;
- what differs, with a category and an explanation in words;
- what is the same;
- the difference rate.

It also colours every node of both trees for the GUI.

## 1. Check that it works

No extra packages are needed.

```
python tests\test_comparison.py
```
This runs 22 checks on hand-made plan trees. It needs no database and no API key, and should end with `ALL CHECKS PASSED`.

```
python tests\test_comparison.py --password YOUR_PASSWORD
```
This also runs 10 TPC-H queries against PostgreSQL. Each query's real plan is compared with:
- itself, which must give rate 0;
- the plans PostgreSQL picks when one method is switched off (`enable_hashjoin`, `enable_seqscan`, ...). These are realistic alternative plans to compare against.

## 2. How the rest of the team uses it

```python
import preprocessing as pp
import comparison as cmp

schema  = pp.get_schema(conn)
pg_tree = pp.get_postgres_plan(conn, sql, parallel=False)
ai_tree = pp.get_ai_plan(conn, sql, schema_info=schema)

result = cmp.compare_plans(pg_tree, ai_tree, schema)   # schema is optional; it adds table sizes to explanations
```

| Field | Meaning |
|---|---|
| `result.pg_tree`, `result.ai_tree` | **annotated copies** of the two trees; the trees passed in are not changed |
| `result.differences` | list of `Difference(category, severity, explanation, pg_nodes, ai_nodes, match_id)`; most important first |
| `result.similarities` | list of sentences, e.g. "Both plans join {lineitem, part} with a Hash Join." |
| `result.summary` | 3 lines for the top of the results panel |
| `result.difference_rate` | the metric, 0 (same plan) to 1 (nothing in common); see section 5 |
| `result.strict_difference_rate` | the same metric with helper nodes counted |
| `result.category_counts` | e.g. `{"ACCESS_METHOD": 1, "CONDITION": 1}` |
| `result.report()` | everything as plain text, e.g. for the console or the report |
| `result.to_dict()` | everything as a JSON-ready dict; `to_dict(include_trees=True)` adds the trees |

### M4: GUI

Every node of `result.pg_tree` and `result.ai_tree` has these in `node.details`:

| Key | Meaning |
|---|---|
| `cmp_status` | `same`, `changed`, `unmatched` or `helper` |
| `cmp_colour` | the colour for that status, as a hex string |
| `cmp_match_id` | the same number on a node and its partner in the other tree (`None` if no partner); use it to highlight the partner when a node is clicked |
| `cmp_note` | a one-line explanation for a tooltip |
| `cmp_node_id` | a unique id, e.g. `pg-3`, `ai-5` |

| Status | Colour | Meaning |
|---|---|---|
| `same` | green `#4CAF50` | has a partner, and the operator, table and index agree |
| `changed` | amber `#FFB300` | has a partner, but something important differs |
| `unmatched` | red `#E53935` | no partner in the other plan |
| `helper` | grey `#9E9E9E` | PostgreSQL helper node (Hash, Memoize, ...), not counted |

These are also available as `cmp.STATUS_COLOURS` and `cmp.STATUS_LABELS` (for a legend). `cmp.status_colour(node)` returns a node's colour. Please also show the status as text or a symbol on each node, so the colours are not the only cue.

For the explanation panel, list `result.differences` using `d.title`, `d.severity` and `d.explanation`. Then list `result.similarities`. `d.pg_nodes` and `d.ai_nodes` are the nodes of the annotated trees each difference is about, so clicking a difference can highlight them.

### M5: experiments

- Record `result.to_dict()` for each query. It includes both rates, the node counts, the category counts and the explanations.
- Use `difference_rate` as the main number. `strict_difference_rate` and `node_agreement` are useful in the report to show how much of the raw difference is due to helper nodes alone.
- `category_counts` across all queries shows which kinds of decisions the AI gets wrong most often.

## 3. Difference categories

| Category | Severity | Example |
|---|---|---|
| `ACCESS_METHOD` | major | Seq Scan vs Index Scan, or the same scan type on a different index |
| `JOIN_ALGORITHM` | major | Hash Join vs Nested Loop vs Merge Join |
| `JOIN_ORDER` | major | the plans join the tables in a different grouping; or a join's inputs are swapped (minor when the join algorithm also differs, or for a Merge Join) |
| `JOIN_TYPE` | major | Inner vs Semi vs Anti |
| `AGGREGATION` | major | HashAggregate vs GroupAggregate |
| `OTHER_OPERATOR` | major | any other operator change, e.g. Sort vs Incremental Sort |
| `MISSING_NODE` / `EXTRA_NODE` | major | a real operator only one plan has, e.g. no Sort for ORDER BY, or a table only one plan reads |
| `INVALID_REFERENCE` | major | the AI named a table, index or operator that does not exist |
| `CONDITION` | minor | a filter or join condition differs, or is applied at another node |
| `HELPER_NODE` | info | a helper node above an operator in one plan only |

Major differences turn a node amber (if it has a partner) or red (if it has none). Minor and info differences are listed but do not change a node's colour.

## 4. How it works

**Step 1. Core trees.**
- *Helper nodes are spliced out.* Some PostgreSQL nodes only carry out a decision made elsewhere:
  - `Hash`, the build phase of a Hash Join;
  - `Memoize`, `Materialize`, `Gather` and `Gather Merge`;
  - partial aggregates;
  - a `Sort` directly below a Merge Join, GroupAggregate, Unique, WindowAgg or SetOp, which follows from that operator's algorithm. A `Sort` anywhere else (e.g. for ORDER BY) is a real operator.

  These are removed from a "core" copy of each tree. They are still shown in grey and listed as `HELPER_NODE`.
- *Bitmap scans are folded.* A `Bitmap Heap Scan` and its `Bitmap Index Scan` / `BitmapAnd` / `BitmapOr` children become one core node, because together they are one access method.

**Step 2. Matching.** Nodes are matched by what they compute, never by their position in the tree:
- **scans** by the table they read. Several scans of one table (a self-join or a sub-query) are paired by alias, filter similarity and sub-plan membership;
- **joins** by the multiset of base tables beneath them. A join of {lineitem, part} matches whatever algorithm or input order each plan uses;
- **other operators** (aggregates, sorts, limits, ...) by their kind plus the tables beneath them. A second pass pairs the rest by table overlap and matched neighbours, e.g. the final Aggregate when the AI left out a table.

Nothing here uses table or column names from a particular schema, only PostgreSQL's operator names.

**Step 3. Classification.** Each matched pair is checked for differences in access method, index, join algorithm, join type, input order, aggregation strategy and conditions. Unmatched joins on both sides mean a different join order; any other unmatched node is a missing or extra node.

Conditions are compared as sets of `AND`ed predicates after normalising them. The following do not count as differences:
- quotes, case, spaces and parentheses;
- table prefixes;
- `date '...'` literals;
- PostgreSQL's `~~` for `LIKE`;
- `a = b` vs `b = a`.

A predicate on the same column(s) with the value written differently is reported as "probably equivalent", e.g. `< '1993-12-01'` vs `< date '1993-11-01' + interval '1' month`.

**Step 4. Explanations.** Each difference gets a sentence built from the plan:
- PostgreSQL's row estimates, and the selectivity if `schema` is passed;
- whether any index could serve the filter;
- index nested-loop look-ups;
- which side of a Hash Join is hashed;
- the AI's own stated reason (`ai_reason`).

For example:

> PostgreSQL reads lineitem with a Bitmap Heap Scan using idx_lineitem_shipdate, while the AI uses an Index Scan using idx_lineitem_shipdate. A bitmap scan first collects the locations of all matching rows from the index, then reads each needed page once, in physical order. PostgreSQL prefers it when too many rows qualify for a plain index scan but too few for a full scan; here it expects about 7,361 of the table's 600,572 rows, or 1.2%, to qualify.

## 5. The difference rate

```
difference rate = TED(core_pg, core_ai) / (|core_pg| + |core_ai|)
```

- **TED** is the Zhang–Shasha tree edit distance: the cheapest set of node insertions, deletions and relabellings that turns one ordered tree into the other.
- **Costs:**
  - insert or delete a node: 1;
  - relabel to a different kind of operator, or a scan of another table: 1;
  - relabel to another algorithm of the same kind (Seq Scan → Index Scan, Hash Join → Nested Loop): 0.5;
  - relabel where only a detail differs (the index used, the join type): 0.25.
- **Range:** deleting every node and inserting every other one is always possible, so TED ≤ |T1| + |T2| and the rate lies in [0, 1].
- **Running time:** O(|T1|·|T2|·min(depth₁, leaves₁)·min(depth₂, leaves₂)), which is well under a millisecond for real plans.
- **Ordered trees:** the trees are compared as ordered, because the outer and inner inputs of a join play different roles.
- **Example:** in the brief's query, the only difference is Seq Scan vs Index Scan on lineitem. That gives TED 0.5 over 4 + 4 core nodes, so the rate is 0.06.
- **Strict rate:** `strict_difference_rate` is the same formula on the full trees, with helper nodes included.

The node colours come from the matching in step 2, not from the edit distance. The matching is semantic (by tables), so it explains the plans better. The edit distance gives one standard number that can be compared across queries.

## 6. Limitations

- **Sub-queries.** PostgreSQL may run a sub-query as a separate InitPlan/SubPlan, where the AI writes it as a join. The explanation says so, but the two structures cannot be matched one-to-one.
- **Equivalent conditions.** Conditions that are equivalent but written very differently (e.g. `IN (...)` vs `= ANY(...)`, or `BETWEEN` vs two comparisons) are reported as minor `CONDITION` differences. They never affect the colours or the rate.
- **Order sensitivity.** Because the trees are ordered, swapping two large join inputs costs more than swapping two small ones.
- **Join conditions after a different join order.** When the join order differs, join conditions are not compared, because the joins combine different inputs.
- **Explanations are heuristics.** They describe PostgreSQL's usual reasons (selectivity, sortedness, outer input size), not the exact cost calculation.
- **Row estimates are not compared.** They are not part of the rate and are only mentioned when they differ by 10× or more. PostgreSQL's estimates for the inner side of a Nested Loop are per outer row.

## 7. Tested on

- **22 checks on hand-made trees,** covering:
  - every category;
  - the README_M2 Memoize example, which gives rate 0;
  - self-joins and sub-plans;
  - parallel plans and bitmap scans;
  - made-up indexes and tables;
  - condition normalisation;
  - the Zhang–Shasha example from the original paper;
  - an AI answer in non-PostgreSQL wording passed through M2's parser.
- **TPC-H at scale factor 0.1 on PostgreSQL 16.** 10 queries, each compared with itself and with 8 alternative plans (80 comparisons). Every identical pair gave rate 0. Every pair whose operators really changed gave at least one major difference. The mean rate was 0.04 and the maximum 0.41, for a 4-way join with nested loops switched off.
- **End to end with real Gemini** (`gemini-3.6-flash`, TPC-H at scale factor 0.1, PostgreSQL 17), on the query from README_M2 section 6:

  ```sql
  SELECT c_name, o_orderdate FROM customer JOIN orders ON c_custkey = o_custkey
  WHERE o_totalprice > 100000 LIMIT 10;
  ```

  ```
  --- PostgreSQL plan
  [same #4] Limit
    [same #3] Nested Loop join: (customer.c_custkey = orders.o_custkey)
      [same #1] Seq Scan on orders filter: (o_totalprice > '100000')
      [helper] Memoize
        [same #2] Index Scan on customer using customer_pkey

  --- AI plan
  [same #4] Limit
    [same #3] Nested Loop join: (customer.c_custkey = orders.o_custkey)
      [same #1] Seq Scan on orders filter: (o_totalprice > 100000)
      [same #2] Index Scan on customer using customer_pkey
  ```

  The difference rate was 0.00, and 0.11 with helper nodes counted (one Memoize deleted, over 5 + 4 nodes). The only difference listed was the Memoize, as information. The filters `'100000'` and `100000` were correctly treated as the same. In this run Gemini included the join condition, which it had left out in M2's earlier test.
