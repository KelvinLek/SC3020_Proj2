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
     GEMINI_MODEL=gemini-3.1-flash-lite
     ```
     `.env` is in `.gitignore`, so your key is never pushed. `.env.example` has no real key, so it is safe to commit.
4. **Optional: change the model.** The default is `gemini-3.1-flash-lite`. To use another one without editing code, change `GEMINI_MODEL` in `.env`, or run:

   ```
   setx GEMINI_MODEL "model-name"
   ```

   **If you get a `503 UNAVAILABLE` error** ("This model is currently experiencing high demand"), Gemini's servers for that model are overloaded; your code is fine. The code already waits and retries for about 100 seconds before giving up. If it still fails, either wait a few minutes and run again, or switch to another model by changing the `GEMINI_MODEL` line in `.env`, e.g.:

   ```dotenv
   GEMINI_MODEL=gemini-3.6-flash
   ```

   Any Gemini model supported by your API key can be used. Refer to the [Gemini API models documentation](https://ai.google.dev/gemini-api/docs/models) for the available models, or check them in [Google AI Studio](https://aistudio.google.com). Record the model used, as different models may generate different query execution plans.
   Note the model you used, since plans can differ between models.

5. **Check that it works:**
   ```
   python tests\test_ai_part.py
   ```
   It asks for YOUR PostgreSQL password, runs one sample query, and prints PostgreSQL's tree and the AI's tree one after the other.

## 2. How the rest of the team uses it

```python
import preprocessing as pp

conn = pp.connect("TPC-H", password="...")

ai_tree = pp.get_ai_plan(conn, sql)                       # AI's predicted plan as a PlanNode tree
pg_tree = pp.get_postgres_plan(conn, sql, parallel=False) # compare against this (see below)

schema = pp.get_schema(conn)                              # if you already have it, pass it in
ai_tree = pp.get_ai_plan(conn, sql, schema_info=schema)   # to avoid reading the catalogs again

print(pp.ai_prompt_preview(conn, sql))                    # exact prompt sent, e.g. for a GUI tab
print(ai_tree.pretty())
```

**Compare against `parallel=False`.** The AI is told not to plan parallel workers. With parallel plans on, every PostgreSQL tree would have extra `Gather` nodes that show up as differences.

**Errors.** A missing API key, a network problem, or an AI answer that can't be used all raise `pp.AIError` with a readable message for the GUI to show, the same way as `pp.DatabaseError`.

**Retries.** If the AI's answer is not valid JSON, it is asked once more, with the error added to the prompt. If Gemini reports a rate limit or a busy server (errors 429, 500, 503), the code waits 10, 30, then 60 seconds and tries again. This matters for batch runs on the free tier.

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

## 6. Tested on

TPC-H, with this query:

```sql
SELECT c_name, o_orderdate
FROM customer JOIN orders ON c_custkey = o_custkey
WHERE o_totalprice > 100000
LIMIT 10;
```

```
--- PostgreSQL
Limit
  Nested Loop join: (customer.c_custkey = orders.o_custkey)
    Seq Scan on orders filter: (o_totalprice > '100000')
    Memoize
      Index Scan on customer using customer_pkey
--- AI
Limit
  Hash Join join: (customer.c_custkey = orders.o_custkey)
    Seq Scan on customer
    Hash
      Seq Scan on orders filter: (o_totalprice > 100000)
```

The two plans differ in join method, access method on `customer`, and join order. Because of `LIMIT 10`, PostgreSQL looks up matching customers through the primary-key index and stops early. The AI planned a full hash join instead.

The normalisation steps were also checked against made-up AI answers written in non-PostgreSQL wording: renamed operators, aliases as table names, Projection and Filter nodes, missing Hash nodes, invented indexes and broken JSON.
