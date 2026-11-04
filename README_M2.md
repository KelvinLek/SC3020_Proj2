# AI part (Member 2)

This covers the AI section of `preprocessing.py`. It asks Google Gemini for the query plan it predicts, then turns the answer into the same `PlanNode` tree the PostgreSQL part produces, so the two trees can be compared directly.

## 1. One-time setup

1. **Install the extra packages.** Make sure `requirements.txt` includes these lines, then run `pip install -r requirements.txt` again:
   ```
   google-genai
   python-dotenv
   ```
   `python-dotenv` is optional. Without it, the code still runs, but the key must be set as an environment variable (step 3).
2. **Get a free Gemini API key** at https://aistudio.google.com/apikey.
3. **Give the key to the program.** Never put it in the code. Use either:
   - an environment variable (close and reopen the terminal afterwards):
     ```
     setx GEMINI_API_KEY "your-key-here"
     ```
   - or a `.env` file in the project folder. Copy `.env.example` to `.env` and put in your own key:
     ```
     copy .env.example .env
     ```
     `.env.example` contains:
     ```dotenv
     GEMINI_API_KEY=your_gemini_api_key_here
     GEMINI_MODEL=gemini-3.6-flash
     ```
     `.env` is in `.gitignore`, so your key is never pushed. `.env.example` has no real key, so it is safe to commit.
4. **Use the same model throughout.** The project currently uses **`gemini-3.6-flash`** (the default). Keep the same model for all testing and experiments, because different models can predict different plans for the same query. For example, `gemini-3.1-flash-lite` predicted a Hash Join for the test query in section 6, while `gemini-3.6-flash` predicted a Nested Loop. Mixing models would make the difference rates impossible to compare.

   `gemini-3.6-flash` was chosen over the Flash Lite models because query planning needs reasoning (join order, when an index helps, what `LIMIT` changes), and Lite models are tuned for speed and cost instead.

   **Changing the model** (only if the team agrees, or temporarily while testing): change `GEMINI_MODEL` in `.env`, or run:

   ```
   setx GEMINI_MODEL "model-name"
   ```

   **If you get a `503 UNAVAILABLE` error** ("This model is currently experiencing high demand"), Gemini's servers for that model are overloaded; your code is fine. The code already waits and retries for about 100 seconds before giving up. If it still fails, either wait a few minutes and run again, or switch to another model by changing the `GEMINI_MODEL` line in `.env`, e.g.:

   ```dotenv
   GEMINI_MODEL=another-gemini-model
   ```

   Any Gemini model supported by your API key can be used. Refer to the [Gemini API models documentation](https://ai.google.dev/gemini-api/docs/models) for the available models, or check them in [Google AI Studio](https://aistudio.google.com). Switch back to `gemini-3.6-flash` before running any experiments whose results go in the report, and record the model used with every result.

5. **Check that it works:**
   ```
   python tests\test_ai_part.py
   ```
   It asks for your PostgreSQL password, runs one sample query, and prints PostgreSQL's actual plan, Gemini's predicted plan, the model used and Gemini's summary. If Gemini cannot be reached, it prints a one-line `AI error:` message instead of a traceback.

## 2. How the rest of the team uses it

### Functions to call

| Function                                         | Returns                                                          | Who uses it                   |
| ------------------------------------------------ | ---------------------------------------------------------------- | ----------------------------- |
| `get_ai_plan(conn, sql, schema_info=None)`       | Gemini's predicted plan as a `PlanNode` tree                     | M3, M4, M5                    |
| `ai_prompt_preview(conn, sql, schema_info=None)` | the exact prompt sent to Gemini, as text                         | M4 (optional GUI tab), report |
| `AIError`                                        | the exception raised for any AI failure, with a readable message | M4, M5                        |

Everything else in the AI section (`build_ai_prompt`, `ask_ai`, `extract_plan_json`, `normalize_operator`, `parse_ai_plan`) is called by `get_ai_plan` and does not need to be called directly.

Always compare against M1's `get_postgres_plan(conn, sql, parallel=False)`. The AI is told not to plan parallel workers, so with parallel plans on, every PostgreSQL tree would have extra `Gather` nodes that show up as differences.

```python
import preprocessing as pp

conn = pp.connect("TPC-H", password="...")
schema = pp.get_schema(conn)                                  # read once, pass to both calls

pg_tree = pp.get_postgres_plan(conn, sql, parallel=False)     # PostgreSQL's actual plan (M1)
try:
    ai_tree = pp.get_ai_plan(conn, sql, schema_info=schema)   # Gemini's predicted plan (M2)
except pp.AIError as e:
    print("AI error:", e)                                     # show in the GUI instead
```

### M3: comparison (`comparison.py`)

- Input: `pg_tree` and `ai_tree`. Both are `PlanNode` trees with the same operator names, real table names and condition style.
- Useful `details` on the AI tree: `ai_reason` (why Gemini chose each operator, for the explanations), `ai_summary` on the root, `estimated_rows` (to compare with PostgreSQL's estimates), and `added_by_normalization` on nodes the code added.
- See section 5 for differences that mean the same thing and should not be counted.

### M4: GUI (`interface.py`, `project.py`)

- Call `get_ai_plan` when the user presses Run, and catch `pp.AIError`: show its message in a popup, the same way as `pp.DatabaseError`.
- Show `ai_tree.details["ai_model"]` and `ai_tree.details.get("ai_summary")` in the results panel if useful.
- `ai_prompt_preview(conn, sql)` gives the prompt for an optional "Prompt" tab.
- If the user picks a schema other than `public`, pass it in: `schema_info=pp.get_schema(conn, "<schema>")`.

### M5: experiments

- Call `get_ai_plan` once per query, catch `pp.AIError` for each query, record which ones failed and rerun only those later. Busy periods (error 503) should not stop the whole batch.
- Add a short pause between queries (e.g. `time.sleep(5)`) to stay under the free-tier rate limit.
- Record `ai_tree.details["ai_model"]` with the results; all runs should use the same model.
- Pass `use_cache=False` to ask Gemini again for the same query, e.g. to check how repeatable its answers are.

### Behaviour to know about

**Errors.** A missing API key, a network problem, or an AI answer that can't be used all raise `pp.AIError` with a readable message.

**Retries.** If the AI's answer is not valid JSON, it is asked once more, with the error added to the prompt. If Gemini reports a rate limit or a busy server (errors 429, 500, 503), the code waits 10, 30, then 60 seconds and tries again.

**Cache.** Asking about the same query twice in one session reuses the first answer, which saves API quota. Pass `use_cache=False` to ask again.

## 3. How the AI's answer becomes a `PlanNode` tree

1. **Prompt** (`build_ai_prompt`). The AI gets the schema, keys, indexes, and row and page counts from `describe_schema(get_schema(conn))`, plus the PostgreSQL version and the query. It must answer with one JSON object shaped like a `PlanNode`, using only PostgreSQL's operator names, and give a one-line reason for each node. Nothing in the prompt is specific to TPC-H.
2. **Call** (`ask_ai`). Gemini runs at temperature 0, in JSON mode, so answers repeat as closely as possible.
3. **Extract** (`extract_plan_json`). The JSON is read even when the AI wraps it in ` ```json ` fences or adds text around it.
4. **Normalise** (`parse_ai_plan`). The answer is converted to PostgreSQL's conventions:
   - operator names are mapped to PostgreSQL's, e.g. "Full Table Scan" → `Seq Scan`, "Hash Match" → `Hash Join`, "Stream Aggregate" → `GroupAggregate`;
   - a join type written in the name is kept, e.g. "Hash Left Join" → `Hash Join` with join type `Left`;
   - aliases used as table names are replaced by the real table, e.g. `o` → `orders`;
   - `Projection` nodes are removed and `Filter` nodes are merged into the node below, since PostgreSQL has neither as a separate operator;
   - a missing `Hash` node is added under a Hash Join's inner input, and a missing `Bitmap Index Scan` under a Bitmap Heap Scan;
   - conditions are cleaned with M1's `clean_condition()` and wrapped in parentheses, so they are written the same way as PostgreSQL's.

   Only operator names are mapped, never table or column names, so this is not schema-specific hardcoding.

## 4. What the AI tree's `details` hold

| Key                              | Meaning                                                                                                      |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `ai_operator`                    | the AI's original operator name, before normalising                                                          |
| `ai_reason`                      | the AI's one-line reason for choosing this operator; useful for M3's explanations                            |
| `estimated_rows`                 | the AI's row estimate for this node                                                                          |
| `join_type`                      | `Inner`, `Left`, `Right`, `Full`, `Semi` or `Anti`, on join nodes                                            |
| `alias`                          | the alias the AI used as a table name, if it was replaced                                                    |
| `added_by_normalization`         | `True` on `Hash` / `Bitmap Index Scan` nodes the code added; M3 may choose not to count these as differences |
| `unknown_operator`               | `True` if the operator name could not be recognised (it is kept as written)                                  |
| `unknown_table`, `unknown_index` | `True` if the AI named a table or index that does not exist                                                  |
| `index_wrong_table`              | `True` if the AI used a real index on the wrong table                                                        |

Made-up tables and indexes are flagged, not removed, because they are findings about the AI for the report.

The root node also holds:

- `ai_summary`: the AI's short summary of its plan;
- `ai_model`: the model used;
- `ai_attempts`: 1, or 2 if the first answer was unusable;
- `ai_raw_response`: the AI's raw answer, for debugging and the report.

## 5. Notes for M3 (comparison)

- Conditions can still differ in small ways that mean the same thing, e.g. PostgreSQL's `(o_totalprice > '100000')` and the AI's `(o_totalprice > 100000)`. Strip quotes and spaces before comparing conditions.
- Nodes with `added_by_normalization` are added only to match PostgreSQL's layout; decide whether they count as differences.
- PostgreSQL has helper nodes the AI often leaves out, e.g. `Memoize`, `Materialize`, or a `Sort` before a `Merge Join`. Decide whether these count as differences or are reported separately.
- The AI sometimes leaves out a join condition. In the test below, Gemini's `Nested Loop` has no `join_condition`, while PostgreSQL's has `(customer.c_custkey = orders.o_custkey)`. Match joins by the tables beneath them, not by their condition text.

## 6. Tested on

TPC-H, with `gemini-3.6-flash` and this query:

```sql
SELECT c_name, o_orderdate
FROM customer JOIN orders ON c_custkey = o_custkey
WHERE o_totalprice > 100000
LIMIT 10;
```

```
--- PostgreSQL (actual plan)
Limit
  Nested Loop join: (customer.c_custkey = orders.o_custkey)
    Seq Scan on orders filter: (o_totalprice > '100000')
    Memoize
      Index Scan on customer using customer_pkey
--- AI (Gemini predicted plan)
Limit
  Nested Loop
    Seq Scan on orders filter: (o_totalprice > 100000)
    Index Scan on customer using customer_pkey

Model: gemini-3.6-flash
Summary: The query uses a Nested Loop join optimized for a LIMIT of 10 rows. It sequentially scans the orders table to find rows satisfying o_totalprice > 100000 and uses an Index Scan on the customer primary key to fetch matching customer details until 10 rows are produced.
```

Gemini predicted the same strategy as PostgreSQL: a nested-loop join that scans `orders`, looks up each customer through `customer_pkey`, and stops after 10 rows. The remaining differences are minor:

- PostgreSQL adds a `Memoize` node to cache customer lookups; Gemini does not.
- Gemini left out the join condition on the `Nested Loop`.
- The filter is written `'100000'` by PostgreSQL and `100000` by Gemini, which mean the same thing.

With the earlier model, `gemini-3.1-flash-lite`, the same query gave a Hash Join with a Seq Scan on `customer` instead. This shows that results depend on the model used.

The normalisation steps were also checked against made-up AI answers written in non-PostgreSQL wording: renamed operators, aliases as table names, Projection and Filter nodes, missing Hash nodes, invented indexes and broken JSON.
