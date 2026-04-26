# Schema Documenter (SQL Server → Markdown)  
AI-powered database documentation generator using Python + LangChain + OpenAI.

This project connects to a SQL Server database, extracts table metadata (columns, keys, relationships, indexes, row counts, and a small sample), and then uses an LLM to generate clean Markdown documentation per table — plus index pages for easy navigation.

## Why I built this

Data teams often lose time on “tribal knowledge” about schemas: what each table means, which columns are keys, and how tables relate. This tool automates that documentation end-to-end and makes it repeatable.

It also demonstrates skills across:
- **Data engineering automation** (metadata extraction, repeatable CLI workflows)
- **AI orchestration** (LLM prompting, chunking, retries, cost estimation)
- **Production-minded Python** (logging, progress reporting, safe secret handling)

## What it generates

Output structure:

```
docs/
  README.md
  <database>/
    README.md
    <schema>.<table>.md
```

Each table page includes (when available):
- Table purpose/description (LLM-generated)
- Column descriptions and types
- Primary key / foreign keys (relationships)
- Indexes
- Row count + small sample rows (to support better descriptions)

## Quickstart

### 1) Prerequisites

- Windows (tested on local SQL Server Express)
- Python 3.10+ (works well with Python 3.12)
- Microsoft ODBC Driver for SQL Server
  - Recommended: **ODBC Driver 17 for SQL Server**
  - Driver 18 also works (the code handles common local TLS settings)

### 2) Install dependencies

```powershell
# from repo root
./venv/Scripts/Activate.ps1
pip install -r requirements.txt
```

### 3) Configure secrets (never commit these)

Create a `.env` file (see `.env.example`):

```env
OPENAI_API_KEY=your_key_here

# Optional: for SQL Authentication
SQL_USER=sa
SQL_PASSWORD=your_password

# Leave SQL_USER/SQL_PASSWORD blank for Windows Authentication
```

### 4) Run

Dry-run (extract schema only; **no LLM calls**):

```powershell
python src/main.py --server localhost --database AdventureWorks --dry-run
```

Generate docs for all tables:

```powershell
python src/main.py --server localhost --database AdventureWorks
```

Generate docs for a subset:

```powershell
python src/main.py --server localhost --database AdventureWorks --tables dbo.Person,dbo.Address
```

## How it works (architecture)

The pipeline is intentionally modular:

1. **Extract** (SQL Server → Python dict)
   - `src/schema_extractor.py` reads metadata via `pyodbc` + `SQLAlchemy`.
2. **Generate** (Python dict → Markdown text)
   - `src/doc_generator.py` builds an LLM prompt and generates Markdown using `langchain-openai` (`ChatOpenAI`).
3. **Write** (Markdown text → files)
   - `src/markdown_writer.py` writes per-table docs and index pages.
4. **Orchestrate**
   - `src/main.py` is the CLI entrypoint: config + env loading, progress bars, logging, and per-table error isolation.

## Configuration

- `config.yaml`
  - `sql_server.driver` (e.g., `ODBC Driver 17 for SQL Server`)
  - `llm.model` (default: `gpt-4o-mini`)
  - `llm.temperature`
  - `output.base_dir` (default: `docs`)

## Reliability + cost controls

- **Per-table fault isolation**: one bad table won’t stop the full run.
- **Retries** around LLM calls.
- **Progress bars** with time-per-table to help estimate runtime.
- **Dry-run mode** to validate connectivity + extraction without spending tokens.
- **Cost estimation** printed at the end (approximate; pricing changes over time).

## Logs

- Run logs: `docs/_logs/schema-documenter.log`

## Troubleshooting (SQL Server on Windows)

- **ODBC driver not found**: install ODBC Driver 17 or 18 and confirm it appears in `pyodbc.drivers()`.
- **Login failed**:
  - Windows Auth: ensure your Windows user has DB access.
  - SQL Auth: verify `SQL_USER`/`SQL_PASSWORD` in `.env`.
- **Named instances**: use `--server ".\\SQLEXPRESS"` or `--server "HOST\\INSTANCE"`.
- **If named instance lookup fails locally** (SQL Browser disabled / TCP disabled): use Shared Memory:
  - `python src/main.py --server "lpc:.\\SQLEXPRESS" --database practice --dry-run`

## Key insights I gained building this

- **SQL Server connectivity is environment-dependent**: local named instances can fail via TCP when SQL Browser/TCP is off; Shared Memory (`lpc:`) can be the most reliable path.
- **LLM tooling compatibility matters**: LangChain package versions can break unexpectedly; pinning compatible versions is essential for stable automation.
- **Schema docs benefit from both metadata + tiny samples**: constraints/keys explain structure; a small sample (when safe) helps the LLM infer intent.
- **Automation needs guardrails**: dry-run, per-table error handling, and cost estimation make this usable in real workflows.
