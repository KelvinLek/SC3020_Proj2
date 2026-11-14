# SC3020 Project 2: Comparing PostgreSQL and AI Query Plans

This software takes an SQL query and shows two query execution plans side by side: the plan PostgreSQL's optimizer chose, and the plan predicted by an AI agent (Google Gemini). It colour-codes the operators that match or differ and explains each difference in words.

The program consists of four files:

| File               | Purpose                                                             |
| ------------------ | ------------------------------------------------------------------- |
| `project.py`       | Main file: run this to start the program                            |
| `interface.py`     | Graphical user interface (Tkinter)                                  |
| `preprocessing.py` | Reads the database schema, gets PostgreSQL's plan and the AI's plan |
| `comparison.py`    | Compares the two plans and computes the difference rate             |

The other files are supporting material: `requirements.txt` (Python packages), `.env.example` (configuration template), `setup/load_tpch.py` (builds the TPC-H database), `workload.json` (evaluation queries) and `tests/` (checks).

---

## 1. Requirements

- **Windows 10 or 11**
- **Python 3.10 or later** from [python.org](https://www.python.org/downloads/windows/). During installation, tick **"Add python.exe to PATH"** and keep **"tcl/tk and IDLE"** ticked (it is ticked by default); the user interface needs it.
- **PostgreSQL 16 or 17** from the [EDB Windows installer](https://www.postgresql.org/download/windows/). Keep the default port **5432** and remember the password you set for the `postgres` user.
- **A Google Gemini API key** from [Google AI Studio](https://aistudio.google.com/apikey). The free tier is sufficient; keep billing disabled.
- An internet connection (for installing packages and calling Gemini).

---

## 2. Installation (one time)

All commands below are run in **Command Prompt** or **PowerShell**, from the project folder (the folder containing `project.py`).

### Step 1: Create a virtual environment and install the packages

```
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

After activation, the prompt starts with `(.venv)`. Run `.venv\Scripts\activate` again whenever you open a new terminal.

> If PowerShell refuses to run the activate script, run `Set-ExecutionPolicy -Scope Process Bypass` first, or use Command Prompt instead.

### Step 2: Generate the TPC-H data

Skip this step if the project folder already contains a `tpch_data` folder with the eight `.tbl` files.

```
tpchgen-cli -s 0.1 --output-dir tpch_data
```

`-s 0.1` creates a 0.1 scale-factor dataset (about 100 MB, a few seconds). For the full 1 GB dataset use `-s 1` instead; loading then takes about 5 minutes.

### Step 3: Load the data into PostgreSQL

```
python setup\load_tpch.py --data-dir tpch_data --password YOUR_POSTGRES_PASSWORD
```

This creates a database named `TPC-H` with the eight TPC-H tables, their primary and foreign keys, secondary indexes and statistics. It ends with `Done: database "TPC-H" ready`. Add `--recreate` to delete and reload an existing `TPC-H` database.

### Step 4: Add your Gemini API key and PostgreSQL password

```
copy .env.example .env
```

Open `.env` in a text editor (e.g. `notepad .env`) and replace the placeholders with your Gemini API key and your PostgreSQL password:

```
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.6-flash
PGPASSWORD=your_postgres_password_here
```

Keep `GEMINI_MODEL=gemini-3.6-flash`; all results in the report were produced with this model. The password is used by the evaluation in section 4; the program window still asks for it when connecting. Alternatively, the API key can be pasted into the API key field in the program window.

---

## 3. Running the program

With the virtual environment active, run:

```
python project.py
```

The program window opens. Keep the terminal open while using it.

### Using the program

1. **Connect.** Keep Host `localhost`, Port `5432` and User `postgres`. Enter your PostgreSQL password and click **Load databases**.
2. **Choose the database and schema.** Select `TPC-H`, then `public`. Any other PostgreSQL database on the server can be selected in the same way; nothing is specific to TPC-H.
3. **API key.** Leave the field blank if the key is in `.env`; otherwise paste it here.
4. **Enter a query**, for example:

   ```sql
   SELECT c_name, o_orderdate
   FROM customer
   JOIN orders ON c_custkey = o_custkey
   WHERE o_totalprice > 100000
   LIMIT 10;
   ```

5. Click **Run comparison**. Getting the AI's plan normally takes a few seconds; if Gemini is busy, the program waits and retries automatically, which can take over a minute.
6. **Read the results:**
   - Both plans are drawn as trees: PostgreSQL's plan and Gemini's predicted plan.
   - Node colours: **green** = same, **amber** = changed, **red** = present in only one plan, **grey** = helper node (e.g. Hash, Memoize). The status is also written on each node.
   - Click a node to highlight its matching node in the other plan and see its details.
   - The **Differences** tab lists each difference; click one to read its explanation. The **Similarities** and **Full report** tabs give the rest of the analysis, including the difference rate.
   - Use **Fit**, **+**, **−** and **100%** (or Ctrl + mouse wheel) to zoom the trees.
   - **Export JSON** saves the results to a file. Passwords and API keys are never saved.

Expected result for the sample query: both plans use a Nested Loop join with an Index Scan on `customer_pkey`, with a core difference rate of 0% and a full-tree difference rate of about 11% (PostgreSQL's extra Memoize node). The AI's answer can vary slightly between runs.

---

## 4. Running the evaluation (optional)

This repeats the difference-rate experiment in the report on the 30 queries in `workload.json`. It is not needed to use the program.

```
python project.py --evaluate --confirm-free-tier --dbname "TPC-H" --schema public --scale-factor 0.1
```

- `--scale-factor` must be the value used in Step 2 (`0.1` or `1`).
- `--confirm-free-tier` confirms that billing is disabled for the API key. The run makes at most 20 Gemini requests per day, the free-tier limit, and stops if the quota is used up.
- The PostgreSQL password is read from `PGPASSWORD` in `.env` (Step 4). Alternatively, set it in the terminal first: `set PGPASSWORD=YOUR_POSTGRES_PASSWORD` in Command Prompt, or `$env:PGPASSWORD = "YOUR_POSTGRES_PASSWORD"` in PowerShell.
- Results (JSON, CSV and a chart) are written to the `experiment-results` folder.
- To run only some queries, add e.g. `--query-ids Q04 Q12 Q19`. See `python project.py --help` for all options.

---

## 5. Checking the installation (optional)

```
python tests\test_comparison.py
python -m unittest tests\test_evaluation.py
python tests\test_postgres_part.py --password YOUR_POSTGRES_PASSWORD
python tests\test_ai_part.py
```

The first two need no database or API key. The third checks the PostgreSQL connection and should end with `ALL CHECKS PASSED`. The fourth asks for the PostgreSQL password and makes one Gemini request.

---

## 6. Troubleshooting

| Problem                                    | Solution                                                                                                                      |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| `'python' is not recognized`               | Reinstall Python with "Add python.exe to PATH" ticked, or use `py` instead of `python`.                                       |
| `No module named 'tkinter'`                | Reinstall Python from python.org with "tcl/tk and IDLE" ticked.                                                               |
| `No module named ...` (another package)    | Activate the virtual environment (`.venv\Scripts\activate`) and run `pip install -r requirements.txt`.                        |
| `password authentication failed`           | Check the `postgres` password set when installing PostgreSQL.                                                                 |
| `fe_sendauth: no password supplied`        | Add `PGPASSWORD=...` to `.env` (Step 4), or set it in the terminal as shown in section 4.                                     |
| `Could not connect ... Connection refused` | PostgreSQL is not running. Start the `postgresql-x64-17` (or `-16`) service in Windows **Services**, or restart the computer. |
| `database "TPC-H" does not exist`          | Complete Steps 2 and 3.                                                                                                       |
| `No Gemini API key found`                  | Check `.env` is in the project folder and contains `GEMINI_API_KEY=...`, or paste the key into the program window.            |
| `503 UNAVAILABLE` / "high demand"          | Gemini is temporarily overloaded. Wait a few minutes and run the comparison again.                                            |
| `429` / `RESOURCE_EXHAUSTED`               | The free-tier request limit has been reached. Wait until the limit resets (daily) and try again.                              |
| `404` / model not found                    | Check `GEMINI_MODEL` in `.env` is `gemini-3.6-flash`.                                                                         |
| `An experiment may already be running`     | A previous evaluation was interrupted. If none is running, delete `experiment-results\.api_budget.lock`.                      |
