# Evaluation and experiments (Member 5)

## Workload

`workload.json` contains 30 project-authored TPC-H queries. The set covers scans and filters, two- through six-way joins, join variants, aggregation, grouping, subqueries, CTEs, set operations, self-joins, and window functions. The workload is not a copy of the public TPC-H benchmark query set.

## Zero-cost requirement

The evaluation runner is limited to the Gemini API free tier. Before running it, verify in Google AI Studio that billing is disabled for the API key's project. The code cannot inspect the account's billing configuration; `--confirm-free-tier` is an operator confirmation, not an automatic check. Do not enable billing for this run. If free-tier quota is exhausted, failed requests are recorded and the runner does not switch to a paid model or retry the request.

The runner processes queries serially, permits at most 30 API request attempts per run and 30 per UTC day, and reserves each request before calling Gemini. `experiment-results/api_budget.json` tracks the daily total under a project-wide lock, so choosing another output directory cannot bypass the cap. A JSON checkpoint is updated before and after each query; an interrupted run retains completed results and marks any in-flight request. Failed query IDs are saved so they can be selected explicitly later.

## Run

Install the requirements and configure PostgreSQL and Gemini credentials locally. Do not commit `.env` or share API keys. Use an API key whose project has billing disabled.

In PowerShell, set the environment variables for the current terminal session, then run:

```powershell
$env:PGPASSWORD = "your-local-postgres-password"
$env:GEMINI_API_KEY = "your-free-tier-key"
$env:GEMINI_MODEL = "gemini-3.6-flash"
python project.py --evaluate --confirm-free-tier --dbname "TPC-H" --schema public --scale-factor 0.1
```

The model, database scale factor (`--scale-factor`), indexes, and schema should remain fixed for all report runs. For the supplied local data use `--scale-factor 0.1`; set this to the actual value used to generate another database. The API key and database password are read locally and are never written to result files.

To run selected queries, for example after a previous run had failures:

```powershell
python project.py --evaluate --confirm-free-tier --query-ids Q04 Q12 Q19
```

Use `python project.py --help` for the evaluation options. Normal GUI startup remains `python project.py`.

## Outputs and interpretation

Results are written under `experiment-results/`; the result artifacts are suitable for review and inclusion in the report. Only the local request ledger and lock are ignored by Git:

- `evaluation_<UTC timestamp>.json`: run metadata, query outcomes, aggregate rate statistics, and category counts.
- `evaluation_<UTC timestamp>.csv`: one row per query for analysis.
- `evaluation_<UTC timestamp>.png`: per-query core/full-tree difference rates and aggregate difference categories.
- `experiment-results/api_budget.json`: UTC-day request reservations used by the local cap; this file is ignored and should not be submitted.

The primary metric is `difference_rate`, the normalized tree edit distance on core operators. `strict_difference_rate` includes helper nodes. The JSON summary includes means, medians, minima, maxima and category totals across successful comparisons. These are structural differences, not measures of AI correctness, latency, or execution speed. Report successful and failed query counts; do not treat failed queries as zero-difference results. Repeat failed query IDs only when the daily budget allows, and record the same model and database configuration.

## Validation boundaries

The automated evaluation tests use local test doubles and do not call Gemini or require PostgreSQL data. A real evaluation requires a configured PostgreSQL TPC-H database and a billing-disabled Gemini key. The clean-Windows installation and end-to-end acceptance run still need to be performed on Windows; this development environment is macOS.