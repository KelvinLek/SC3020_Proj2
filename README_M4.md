# Member 4: GUI and integration

## Install these files

Copy `interface.py` and `project.py` into your existing Member 3 project folder,
next to `preprocessing.py`, `comparison.py`, `requirements.txt`, and `.venv`.
Do not replace your teammates' files. No additional Python packages are needed
beyond the requirements you already installed. Tkinter must be available.

Launch from that folder in the VS Code PowerShell terminal:

```powershell
.\.venv\Scripts\python.exe project.py
```

Keep the terminal open while using the application. Closing the GUI ends the app.

## First run

1. Keep Host `localhost`, Port `5432`, and User `postgres` for your local setup.
2. Enter your PostgreSQL password and click **Load databases**.
3. Select `TPC-H`, then `public`. The lists are read from the database server;
   the program does not hardcode TPC-H or a particular schema.
4. If you launch from the same terminal used for the successful AI test, your
   `GEMINI_API_KEY` and `GEMINI_MODEL` environment variables are inherited.
   Leave the optional API key field blank. Otherwise paste your key into that
   masked field, or configure a local `.env` using your team's `.env.example`.
   Do not commit or share a real key or password.
5. Confirm the configured model shown at the top matches your group's model.
   The code preserves M2's default or your `GEMINI_MODEL` environment setting.
6. Paste this SQL into the query box (sample for your loaded TPC-H data only):

```sql
SELECT c_name, o_orderdate
FROM customer
JOIN orders ON c_custkey = o_custkey
WHERE o_totalprice > 100000
LIMIT 10;
```

7. Click **Run comparison**. The window stays responsive while Gemini works.
   Its existing retry policy can take over a minute when the service is busy.
8. Inspect both trees. Scroll vertically/horizontally for larger plans.
   Click a node to highlight its partner and open its details. Match numbers
   identify corresponding operators. Click a difference to highlight all
   nodes involved and read the explanation. The Differences tab lists items on
   the left; click an item to populate the explanation on the right. Until then,
   the right panel says "Select a difference on the left."
9. The **Similarities** and **Full report** tabs show M3's text results.
10. **Export JSON** saves the comparison, both annotated trees, SQL, database,
    schema, model, UTC timestamp and cache setting. Credentials are excluded.
    The exported SQL and plan details are still project data; review before sharing.

## Colours and rates

- Green / same: matching operator choices according to M3's algorithm.
- Amber / changed: a matched operator with a significant difference.
- Red / unmatched: no matching operator in the other plan.
- Grey / helper: helper nodes treated separately by M3.

Status words are also displayed so colour is not the only cue.
Minor condition differences may appear in the list even when nodes are green.
The main rate uses M3's core-tree metric; the second includes helper nodes.
These numbers measure structural differences, not AI correctness or speed.

The cache checkbox passes `use_cache` to M2's code. With caching enabled,
repeated requests may reuse an answer in the same process. Untick it to request
another answer. Export records whether caching was enabled, not whether a
particular response was a cache hit (M2 does not expose that flag).

## How your two files work

### interface.py

`QueryPlanApp` creates the controls, receives clicks and displays results.
It knows nothing about how PostgreSQL or Gemini generate plans.
`PlanCanvas` draws operator boxes and edges. `tree_layout` gives each leaf a
separate horizontal position, centres each parent above its children, and
preserves the original left-to-right child order. Layout takes O(n) time.
The actual diagram shows parent-child relationships with roots at the top;
query data flows from leaves toward the root.

`start` runs one background job. The worker places progress, success or error
messages into a queue. `poll`, scheduled by Tkinter's `after`, reads the queue
and updates the widgets on the main thread. All input widgets are disabled
while the job runs, preventing duplicate requests. Changes to the SQL,
database or schema clear old results so they are not mistaken for new ones.

### project.py

`main` loads local configuration and starts the GUI.
`ProjectService` supplies the GUI's database, schema and comparison operations.
For each comparison it:

1. Opens a connection using the entered settings and selected schema.
2. Reads schema metadata with M1's `get_schema`.
3. Obtains the PostgreSQL plan with `parallel=False` to follow M2's convention.
4. Obtains the AI plan with M2's `get_ai_plan`.
5. Calls M3's `compare_plans`.
6. Returns the comparison and non-secret run metadata to the GUI.
7. Closes the connection in `finally`, including when an error occurs.

Schema names are quoted as SQL identifiers. The selected search path is
committed before planning so it survives M1's transaction rollbacks.
No query-specific comparisons, table names or dataset choices are hardcoded
into these two program files. They use the existing M1-M3 interfaces.

## Suggested explanation in a demonstration

"My role is the GUI and integration. The user connects to a PostgreSQL server,
selects a database and schema, and enters SQL. project.py calls the preprocessing
module to obtain the PostgreSQL and AI plans, then passes both to comparison.py.
interface.py displays the annotated trees and explanations. Matching IDs link
corresponding nodes. Database and API calls run in a background thread, while
Tkinter updates the screen on the main thread, so the window stays responsive."

Use your own words and be prepared to explain the code. This is a guide, not
a claim that you implemented or tested work you have not reviewed.

## Your Windows acceptance check

- Run the sample query; see two diagrams and the actual model name.
- Click one matched node; both partners get thick outlines.
- Click a listed difference; its explanation appears.
- Export JSON and open it; check the SQL, model, rates and both trees.
- Edit SQL; old results and export availability should clear.
- Run invalid SQL; see a readable error and then successfully run valid SQL.
- Try a wrong database password; correct it and reconnect.
- Try a multi-table query; verify scrollbars reveal the whole tree.
- If available, select a different database/schema and use its SQL.

## Limitations and validation

The application inherits M1-M3's plan parsing, matching and AI limitations.
Only the selected schema's metadata is sent through the existing M2 interface;
cross-schema queries are not fully described by that interface. Very deep or
very large trees can exceed Python recursion limits or become slow to draw.
The GUI provides Fit, zoom and 100% controls plus scrollbars. Fit may make labels small on large plans; zoom in to inspect them. There is no live cancellation;
closing during a request discards its result but cannot undo a request already
sent to Gemini. Request retry/network behavior is controlled by M2.

The supplied code was syntax-checked and tested with the actual M3 comparison
module plus isolated integration checks. See VALIDATION.txt for exact checks.
A live Windows run has now been demonstrated by the user with PostgreSQL 17,
TPC-H scale factor 0.1 and gemini-3.6-flash. Screenshots and the exported JSON
confirm the sample comparison and refined tree display. The user also confirmed
invalid-SQL handling and successful recovery in the earlier GUI version.
Repeat those checks after updates; testing additional queries, schemas and a
second Windows machine remains part of team acceptance testing.

## Submission and LLM acknowledgement

Your final application has the four required program files: preprocessing.py,
comparison.py, interface.py and project.py. Coordinate with the team about
packaging setup scripts, tests and supporting documentation separately.

Keep a truthful log of this assistance. Suggested acknowledgement to adapt:
"ChatGPT assisted with the initial implementation of Member 4's Tkinter GUI
and integration code, setup troubleshooting, code explanations and test design.
The team reviewed and tested the generated code before submission."
Only retain the final sentence after your team has actually done that review
and testing. Record the model name if your course requires it and it is known;
do not guess it.

## Refined GUI
Connection settings collapse after a successful result; use Show connection settings to reopen them. Each tree has Fit, minus, plus and 100% buttons. Ctrl+mouse wheel zooms; the wheel scrolls vertically. The progress bar is hidden when idle. Drag the horizontal divider to change the tree/report proportions.

## Latest exported example and test evidence

The user-supplied comparison_result.json was parsed successfully and its tree
counts and difference-node references were checked. It records:

| Field | Value |
|---|---|
| Database / schema | TPC-H / public |
| Model | gemini-3.6-flash |
| PostgreSQL parallel planning | Disabled |
| AI cache setting | Enabled (not proof of a cache hit) |
| Matching core operators | 4 |
| Core difference rate | 0% |
| Full-tree difference rate | 11.11% |
| Reported differences | One HELPER_NODE: PostgreSQL's Memoize |

This run has no reported CONDITION differences, unlike an earlier run. Record
what each export actually contains rather than assuming all AI runs are identical.
The 11.11% value is one deletion divided by the combined 5 + 4 full-tree nodes.
A zero core rate does not establish full semantic equivalence or AI accuracy.
The export records UTC; this run's 12:11:18 UTC corresponds to 20:11:18 in Singapore
on 5 November 2026.

### Reading the fitted diagrams

Fit centres each full tree with margins. Each tree is fitted independently,
so deeper trees may have smaller boxes and labels. Use + or 100% for details.
For a larger overall view, maximise the window and move the divider above the
report tabs downwards, then click Fit again. Small fitted labels on large trees
are a known usability tradeoff, not evidence of missing plan nodes.

### Team handover

Share interface.py, project.py and this README. Keep the team's existing
preprocessing.py and comparison.py alongside them. Do not share .venv, real
.env files, API keys or passwords. No additional GUI package was introduced.
The local demo uses TPC-H scale factor 0.1; agree with M5 on the dataset scale,
indexes, model and cache policy for recorded evaluation runs.

The GUI is ready for broader team testing, not a claim that all queries or
schemas have passed. Test larger plans, another schema, connection failures,
AI failures and a clean Windows installation before final submission.
