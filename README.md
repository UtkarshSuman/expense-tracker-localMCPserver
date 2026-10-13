# 💸 Expense Tracker MCP Server

A local [Model Context Protocol](https://modelcontextprotocol.io) server that turns your AI assistant into an expense tracker. Instead of opening an app, you just talk:

> "Spent 450 on lunch at Subway, paid by UPI"
> "Where did my money go this month?"
> "Set a 5000 monthly budget for Food"

Built with **Python**, **FastMCP** and **SQLite**. Everything runs on your machine and your data never leaves it.

> **Status:** local-only (stdio transport). A deployed, multi-user remote version is the next step. See the [Roadmap](#roadmap).

---

## Features

- **Expense management:** add, bulk add, update, soft-delete and restore, get, list, search
- **Rich filtering:** date range, category, merchant, payment method, tag, amount range, sorting and pagination
- **Analytics:** grouped summaries, monthly reports, multi-month trends, period comparison
- **Smart checks:** anomaly detection (unusually large expenses) and possible-duplicate detection
- **Budgets:** per-category and overall monthly budgets, warning and exceeded flags, month-end projection
- **Recurring expenses:** daily, weekly, biweekly, monthly, quarterly and yearly, generated automatically when due
- **Categories:** auto-created on use, with rename and merge
- **Data safety:** CSV export and import (dry-run by default, duplicate skipping), consistent database backups
- **MCP extras:** resources and ready-made prompts

---

## Requirements

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`
- An MCP client such as Claude Desktop

---

## Quick start

### 1. Get the code

Place `main.py` in a folder of your choice, for example `C:\Users\<you>\MCP\fastmcp-demo-server\`.

### 2. Run it manually (optional sanity check)

```bash
uv run --with fastmcp fastmcp run main.py
```

Or open the interactive MCP Inspector (requires Node.js):

```bash
uv run --with fastmcp fastmcp dev main.py
```

### 3. Connect it to Claude Desktop

Add this to your `claude_desktop_config.json`.

```json
{
  "mcpServers": {
    "Expense Tracker": {
      "command": "C:\\Users\\<you>\\AppData\\Roaming\\Python\\Python313\\Scripts\\uv.exe",
      "args": [
        "run",
        "--with",
        "fastmcp",
        "fastmcp",
        "run",
        "C:\\Users\\<you>\\MCP\\fastmcp-demo-server\\main.py"
      ],
      "env": {
        "EXPENSE_CURRENCY": "INR"
      }
    }
  }
}
```

> ⚠️ **Windows paths in JSON:** every backslash must be doubled (`\\`) or you must use forward slashes (`C:/Users/...`). A single `\U` causes an *"Invalid unicode sequence"* error.
> Find your `uv` path with `where.exe uv` (Windows) or `which uv` (macOS/Linux).

Fully quit Claude Desktop (from the system tray) and reopen it. The server appears in the tools menu.

---

## Configuration

All settings are optional environment variables, set through `env` in your MCP config.

| Variable | Default | Description |
|---|---|---|
| `EXPENSE_TRACKER_DIR` | `~/.expense-tracker` | Folder for the database, `exports/` and `backups/` |
| `EXPENSE_DB_PATH` | `<dir>/expenses.db` | Full path to the SQLite file |
| `EXPENSE_CURRENCY` | `INR` | Currency label shown in outputs (display only, no conversion) |

---

## Example prompts

| You say | What happens |
|---|---|
| "Spent 450 on lunch at Subway today, paid by UPI" | Logs the expense with category, merchant and payment method |
| "Add rent of 15000 as a monthly recurring expense starting the 1st" | Creates a recurring rule and back-fills past due dates |
| "Set a 5000 monthly budget for Food and 40000 overall" | Saves two budgets |
| "Give me my monthly report and tell me where to cut costs" | Chains report, budget status and anomaly checks, then gives advice |
| "Compare September with August" | Per-category change between the two periods |
| "Show all expenses over 2000 this quarter" | Filtered, sorted list with totals |
| "Export this year's food expenses to CSV" | Writes a CSV into the `exports/` folder |

---

## Tool reference

### Expenses

| Tool | Description |
|---|---|
| `add_expense` | Add one expense (amount, category, date, merchant, payment method, tags, and more) |
| `add_expenses_bulk` | Add up to 1000 expenses atomically (all or nothing) |
| `update_expense` | Change any fields of an existing expense |
| `delete_expense` | Soft-delete (recoverable) |
| `restore_expense` | Undo a soft-delete |
| `get_expense` | Fetch one expense by id |
| `list_expenses` | Filter, sort and paginate, with total count and amount |
| `search_expenses` | Search description, merchant, category, subcategory and tags |

### Analytics

| Tool | Description |
|---|---|
| `summarize_spending` | Group by `category`, `subcategory`, `merchant`, `payment_method`, `day`, `week`, `month`, `year` or `weekday` |
| `monthly_report` | Totals, category breakdown, top merchants, largest expenses, comparison with the previous month, budgets, projection |
| `spending_trends` | Last N months (max 36) with month-over-month change |
| `compare_periods` | Two date ranges compared overall and per category |
| `detect_anomalies` | Statistical outliers per category, plus possible duplicates |

### Budgets

| Tool | Description |
|---|---|
| `set_budget` | Create or update a budget (use `overall` for a total budget) |
| `list_budgets` | List all budgets |
| `delete_budget` | Remove a budget |
| `budget_status` | Spent vs limit, `ok` / `warning` / `exceeded`, projected month-end |

### Recurring expenses

| Tool | Description |
|---|---|
| `add_recurring_expense` | Define a recurring expense |
| `list_recurring_expenses` | Show definitions and next due dates |
| `set_recurring_active` | Pause or resume |
| `delete_recurring_expense` | Delete the rule (generated expenses are kept) |
| `process_recurring` | Generate everything due up to a date |

### Categories

| Tool | Description |
|---|---|
| `list_categories` | All categories with counts and totals |
| `add_category` | Create a category |
| `rename_category` | Rename, or merge into an existing category |

### Data management

| Tool | Description |
|---|---|
| `export_csv` | Export filtered expenses to a CSV file |
| `import_csv` | Import from CSV (preview first with `dry_run=true`) |
| `backup_database` | Timestamped, consistent SQLite backup |
| `database_info` | DB location, size, row counts and date range |

### Resources and prompts

| Type | Name | Description |
|---|---|---|
| Resource | `expense://categories` | JSON list of categories |
| Resource | `expense://summary/{month}` | Category totals for a month (`YYYY-MM`) |
| Resource | `expense://schema` | The SQLite schema |
| Prompt | `monthly_review` | Guided monthly spending review |
| Prompt | `quick_log` | Turn free text into logged expenses |

---

## Data and storage

- **Location:** `~/.expense-tracker/expenses.db` by default (SQLite, WAL mode)
- **Dates:** `YYYY-MM-DD`, plus `today` and `yesterday`
- **Soft delete:** deleted expenses stay in the database with a `deleted_at` timestamp and are excluded from all reports
- **Recurring catch-up:** due items are generated on server start and by `process_recurring`. Past start dates are back-filled.
- **Categories:** unknown categories are created automatically; names are case-insensitive
- **Tags:** stored normalized (lowercase, deduplicated)

### Tables

| Table | Purpose |
|---|---|
| `expenses` | Core records (amount, date, category, merchant, tags, soft-delete) |
| `categories` | Category names |
| `budgets` | Monthly limits and alert thresholds (`*` = overall) |
| `recurring` | Recurring expense definitions |

### CSV import format

Required columns: `date`, `amount`, `category`.
Optional: `description`, `merchant`, `payment_method`, `subcategory`, `tags` (comma-separated).

```csv
date,amount,category,merchant,description,payment_method,tags
2026-10-01,450,Food,Subway,Lunch,upi,"work,lunch"
2026-10-02,1200,Groceries,BigBasket,Weekly shop,card,
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Invalid unicode sequence in string` | Double the backslashes in Windows paths in your JSON config |
| Server not showing in Claude Desktop | Fully quit the app from the system tray and reopen it; check logs in `%APPDATA%\Claude\logs` |
| `uv` not found | Use the full path from `where.exe uv` in `command` |
| Garbled or failing connection | Never `print()` to stdout in this server; stdio is the protocol channel. Use `logging` (stderr). |
| Want a fresh start | Delete `expenses.db` (or point `EXPENSE_DB_PATH` somewhere new) |

---

## Roadmap

This version is **local-first by design**: stdio transport, single user, data on your own disk. Great for privacy and prototyping, but it can't be reached from your phone, shared or used by multiple people.

Next step: **a deployed version**.

- [ ] Run over HTTP as a remote MCP server (streamable HTTP transport)
- [ ] Authentication (API keys or OAuth)
- [ ] Move from SQLite to a managed database (e.g. PostgreSQL)
- [ ] Multi-user support with per-user data isolation
- [ ] Containerize (Docker) and deploy to a cloud host
- [ ] Income tracking and net cash flow
- [ ] Multi-currency support
- [ ] Receipt parsing

---

## Contributing

Issues and pull requests are welcome. This is an experiment in building capabilities for AI agents rather than UIs for people, so ideas for new tools are especially appreciated.

