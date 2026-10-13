"""
Expense Tracker MCP Server (FastMCP + SQLite)
=============================================

A fully local expense tracker. All data lives in a SQLite file on your machine.

Features
--------
* Expenses: add, bulk add, update, soft-delete / restore, get, list, search
* Filters: date range, category, merchant, payment method, tag, amount range
* Analytics: grouped summaries, monthly report, trends, period comparison,
  anomaly + duplicate detection, month-end forecast
* Budgets: per-category and overall monthly budgets with alert thresholds
* Recurring expenses: daily / weekly / biweekly / monthly / quarterly / yearly,
  auto-generated when due (on server start and via a tool)
* Categories: list with totals, add, rename / merge
* Data: CSV export / import (with dry-run + duplicate skipping), DB backup
* MCP resources and prompts for monthly reviews

Run:
    uv run --with fastmcp fastmcp run main.py

Environment variables (optional):
    EXPENSE_TRACKER_DIR  folder for DB, exports and backups
                         (default: ~/.expense-tracker)
    EXPENSE_DB_PATH      full path to the SQLite file
    EXPENSE_CURRENCY     currency label used in outputs (default: INR)

IMPORTANT: this server uses stdio, so never print() to stdout.
Logging goes to stderr.
"""

from __future__ import annotations

import calendar
import csv
import json
import logging
import os
import sqlite3
import statistics
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [expense-tracker] %(levelname)s %(message)s",
)
log = logging.getLogger("expense-tracker")

DATA_DIR = Path(os.environ.get("EXPENSE_TRACKER_DIR", Path.home() / ".expense-tracker"))
DB_PATH = Path(os.environ.get("EXPENSE_DB_PATH", DATA_DIR / "expenses.db"))
EXPORT_DIR = DATA_DIR / "exports"
BACKUP_DIR = DATA_DIR / "backups"
CURRENCY = os.environ.get("EXPENSE_CURRENCY", "INR")

DEFAULT_CATEGORIES = [
    "Food", "Groceries", "Transport", "Housing", "Utilities", "Health",
    "Entertainment", "Shopping", "Travel", "Education", "Subscriptions",
    "Insurance", "Personal Care", "Gifts", "Other",
]
FREQUENCIES = ("daily", "weekly", "biweekly", "monthly", "quarterly", "yearly")
OVERALL_BUDGET = "*"

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    name        TEXT PRIMARY KEY COLLATE NOCASE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS expenses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    date           TEXT NOT NULL,
    amount         REAL NOT NULL CHECK (amount > 0),
    category       TEXT NOT NULL REFERENCES categories(name)
                        ON UPDATE CASCADE COLLATE NOCASE,
    subcategory    TEXT NOT NULL DEFAULT '',
    merchant       TEXT NOT NULL DEFAULT '',
    description    TEXT NOT NULL DEFAULT '',
    payment_method TEXT NOT NULL DEFAULT 'other',
    tags           TEXT NOT NULL DEFAULT '',
    recurring_id   INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    deleted_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_expenses_date     ON expenses(date);
CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses(category);
CREATE INDEX IF NOT EXISTS idx_expenses_deleted  ON expenses(deleted_at);

CREATE TABLE IF NOT EXISTS budgets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    category         TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- '*' = overall
    monthly_limit    REAL NOT NULL CHECK (monthly_limit > 0),
    alert_threshold  REAL NOT NULL DEFAULT 0.8,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    amount          REAL NOT NULL CHECK (amount > 0),
    category        TEXT NOT NULL,
    merchant        TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    payment_method  TEXT NOT NULL DEFAULT 'other',
    tags            TEXT NOT NULL DEFAULT '',
    frequency       TEXT NOT NULL,
    next_due        TEXT NOT NULL,
    end_date        TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def db():
    """Open a connection, commit on success, rollback on error."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        now = _now()
        conn.executemany(
            "INSERT OR IGNORE INTO categories(name, created_at) VALUES (?, ?)",
            [(c, now) for c in DEFAULT_CATEGORIES],
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #
def r2(x: Optional[float]) -> float:
    return round(float(x or 0), 2)


def parse_date(value: Optional[str], default_today: bool = True) -> Optional[str]:
    """Accept YYYY-MM-DD, 'today', 'yesterday'. Returns ISO string."""
    if value is None or str(value).strip() == "":
        return date.today().isoformat() if default_today else None
    v = str(value).strip().lower()
    if v == "today":
        return date.today().isoformat()
    if v == "yesterday":
        return (date.today() - timedelta(days=1)).isoformat()
    try:
        return datetime.strptime(v, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ToolError(f"Invalid date '{value}'. Use YYYY-MM-DD, 'today' or 'yesterday'.")


def check_amount(amount) -> float:
    try:
        a = float(amount)
    except (TypeError, ValueError):
        raise ToolError(f"Invalid amount: {amount!r}")
    if a <= 0 or a > 1_000_000_000:
        raise ToolError("Amount must be greater than 0 and below 1,000,000,000.")
    return round(a, 2)


def norm_category(name: Optional[str]) -> str:
    n = (name or "").strip()
    if not n:
        raise ToolError("Category is required.")
    return n.title() if n.islower() else n


def tags_to_db(tags) -> str:
    if not tags:
        return ""
    if isinstance(tags, str):
        tags = tags.split(",")
    cleaned = sorted({t.strip().lower().replace(",", "") for t in tags if t and t.strip()})
    return "," + ",".join(cleaned) + "," if cleaned else ""


def tags_from_db(s: str) -> list[str]:
    return [t for t in (s or "").split(",") if t]


def ensure_category(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute("SELECT name FROM categories WHERE name = ?", (name,)).fetchone()
    if row:
        return row["name"]
    conn.execute("INSERT INTO categories(name, created_at) VALUES (?, ?)", (name, _now()))
    return name


def expense_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["amount"] = r2(d["amount"])
    d["tags"] = tags_from_db(d.get("tags", ""))
    d["is_deleted"] = d.pop("deleted_at", None) is not None
    return d


def add_months(d: date, n: int) -> date:
    idx = d.month - 1 + n
    y, m = d.year + idx // 12, idx % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def advance(d: date, frequency: str) -> date:
    if frequency == "daily":
        return d + timedelta(days=1)
    if frequency == "weekly":
        return d + timedelta(days=7)
    if frequency == "biweekly":
        return d + timedelta(days=14)
    if frequency == "monthly":
        return add_months(d, 1)
    if frequency == "quarterly":
        return add_months(d, 3)
    if frequency == "yearly":
        return add_months(d, 12)
    raise ToolError(f"Unknown frequency '{frequency}'. Use one of {FREQUENCIES}.")


def month_bounds(month: Optional[str]) -> tuple[str, str, str]:
    """Return (YYYY-MM, first_day, last_day) for a month string (default: current)."""
    if not month:
        month = date.today().strftime("%Y-%m")
    try:
        d = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise ToolError(f"Invalid month '{month}'. Use YYYY-MM.")
    last = calendar.monthrange(d.year, d.month)[1]
    return month, d.isoformat(), date(d.year, d.month, last).isoformat()


def build_where(
    start_date=None, end_date=None, category=None, merchant=None,
    payment_method=None, tag=None, min_amount=None, max_amount=None, text=None,
) -> tuple[str, list]:
    clauses, params = ["deleted_at IS NULL"], []
    if start_date:
        clauses.append("date >= ?")
        params.append(parse_date(start_date))
    if end_date:
        clauses.append("date <= ?")
        params.append(parse_date(end_date))
    if category:
        clauses.append("category = ? COLLATE NOCASE")
        params.append(category.strip())
    if merchant:
        clauses.append("merchant LIKE ?")
        params.append(f"%{merchant.strip()}%")
    if payment_method:
        clauses.append("payment_method = ? COLLATE NOCASE")
        params.append(payment_method.strip())
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f"%,{tag.strip().lower()},%")
    if min_amount is not None:
        clauses.append("amount >= ?")
        params.append(float(min_amount))
    if max_amount is not None:
        clauses.append("amount <= ?")
        params.append(float(max_amount))
    if text:
        like = f"%{text.strip()}%"
        clauses.append(
            "(description LIKE ? OR merchant LIKE ? OR category LIKE ? "
            "OR subcategory LIKE ? OR tags LIKE ?)"
        )
        params.extend([like] * 5)
    return " AND ".join(clauses), params


def insert_expense(conn: sqlite3.Connection, *, amount, category, date_str, description="",
                   merchant="", payment_method="other", subcategory="", tags=None,
                   recurring_id=None) -> int:
    cat = ensure_category(conn, norm_category(category))
    now = _now()
    cur = conn.execute(
        """INSERT INTO expenses(date, amount, category, subcategory, merchant, description,
                                payment_method, tags, recurring_id, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (date_str, check_amount(amount), cat, subcategory.strip(), merchant.strip(),
         description.strip(), (payment_method or "other").strip().lower(),
         tags_to_db(tags), recurring_id, now, now),
    )
    return cur.lastrowid


def run_recurring(conn: sqlite3.Connection, as_of: str) -> list[dict]:
    """Create expenses for every recurring item due on or before `as_of`."""
    created = []
    rows = conn.execute(
        "SELECT * FROM recurring WHERE active = 1 AND next_due <= ?", (as_of,)
    ).fetchall()
    for r in rows:
        due = date.fromisoformat(r["next_due"])
        limit = date.fromisoformat(r["end_date"]) if r["end_date"] else None
        guard = 0
        while due.isoformat() <= as_of and (limit is None or due <= limit) and guard < 500:
            eid = insert_expense(
                conn, amount=r["amount"], category=r["category"], date_str=due.isoformat(),
                description=r["description"] or r["name"], merchant=r["merchant"],
                payment_method=r["payment_method"], tags=tags_from_db(r["tags"]),
                recurring_id=r["id"],
            )
            created.append({"expense_id": eid, "recurring_id": r["id"], "name": r["name"],
                            "date": due.isoformat(), "amount": r2(r["amount"])})
            due = advance(due, r["frequency"])
            guard += 1
        active = 0 if (limit is not None and due > limit) else 1
        conn.execute("UPDATE recurring SET next_due = ?, active = ? WHERE id = ?",
                     (due.isoformat(), active, r["id"]))
    return created


def period_total(conn, start: str, end: str) -> dict:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) t, COUNT(*) n FROM expenses "
        "WHERE deleted_at IS NULL AND date BETWEEN ? AND ?", (start, end)
    ).fetchone()
    return {"total": r2(row["t"]), "count": row["n"]}


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
init_db()
with db() as _c:
    _auto = run_recurring(_c, date.today().isoformat())
    if _auto:
        log.info("Auto-created %d recurring expense(s) on startup", len(_auto))

mcp = FastMCP(
    name="Expense Tracker",
    instructions=(
        "Local expense tracker. Dates are YYYY-MM-DD (also 'today'/'yesterday'). "
        f"Amounts are in {CURRENCY}. Prefer summarize_spending / monthly_report for "
        "analysis instead of listing every expense. delete_expense is a soft delete "
        "and can be undone with restore_expense."
    ),
)


# ============================== EXPENSE CRUD ================================ #
@mcp.tool()
def add_expense(
    amount: float,
    category: str,
    date: Optional[str] = None,
    description: str = "",
    merchant: str = "",
    payment_method: str = "other",
    subcategory: str = "",
    tags: Optional[list[str]] = None,
) -> dict:
    """Add a single expense. Unknown categories are created automatically.

    Args:
        amount: Positive amount (e.g. 249.50).
        category: e.g. Food, Transport, Groceries.
        date: YYYY-MM-DD, 'today' or 'yesterday'. Defaults to today.
        description: Free-text note.
        merchant: Shop / vendor name.
        payment_method: e.g. cash, card, upi, bank_transfer.
        subcategory: Optional finer label (e.g. 'Coffee' under Food).
        tags: Optional list of tags like ['work', 'reimbursable'].
    """
    with db() as conn:
        eid = insert_expense(
            conn, amount=amount, category=category, date_str=parse_date(date),
            description=description, merchant=merchant, payment_method=payment_method,
            subcategory=subcategory, tags=tags,
        )
        row = conn.execute("SELECT * FROM expenses WHERE id = ?", (eid,)).fetchone()
        return {"message": "Expense added", "expense": expense_dict(row)}


@mcp.tool()
def add_expenses_bulk(expenses: list[dict]) -> dict:
    """Add many expenses atomically (all succeed or none are saved).

    Each item needs 'amount' and 'category'; optional keys: date, description,
    merchant, payment_method, subcategory, tags (list).
    """
    if not expenses:
        raise ToolError("Provide at least one expense.")
    if len(expenses) > 1000:
        raise ToolError("Maximum 1000 expenses per call.")
    ids = []
    with db() as conn:
        for i, e in enumerate(expenses, start=1):
            try:
                ids.append(insert_expense(
                    conn, amount=e.get("amount"), category=e.get("category"),
                    date_str=parse_date(e.get("date")), description=e.get("description", ""),
                    merchant=e.get("merchant", ""), payment_method=e.get("payment_method", "other"),
                    subcategory=e.get("subcategory", ""), tags=e.get("tags"),
                ))
            except ToolError as err:
                raise ToolError(f"Item #{i} rejected: {err}. Nothing was saved.")
    return {"message": f"Added {len(ids)} expenses", "ids": ids}


@mcp.tool()
def update_expense(
    expense_id: int,
    amount: Optional[float] = None,
    category: Optional[str] = None,
    date: Optional[str] = None,
    description: Optional[str] = None,
    merchant: Optional[str] = None,
    payment_method: Optional[str] = None,
    subcategory: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> dict:
    """Update any fields of an existing (non-deleted) expense. Only provided fields change."""
    sets, params = [], []
    with db() as conn:
        if not conn.execute("SELECT 1 FROM expenses WHERE id=? AND deleted_at IS NULL",
                            (expense_id,)).fetchone():
            raise ToolError(f"Expense {expense_id} not found.")
        if amount is not None:
            sets.append("amount = ?"); params.append(check_amount(amount))
        if category is not None:
            sets.append("category = ?"); params.append(ensure_category(conn, norm_category(category)))
        if date is not None:
            sets.append("date = ?"); params.append(parse_date(date))
        if description is not None:
            sets.append("description = ?"); params.append(description.strip())
        if merchant is not None:
            sets.append("merchant = ?"); params.append(merchant.strip())
        if payment_method is not None:
            sets.append("payment_method = ?"); params.append(payment_method.strip().lower())
        if subcategory is not None:
            sets.append("subcategory = ?"); params.append(subcategory.strip())
        if tags is not None:
            sets.append("tags = ?"); params.append(tags_to_db(tags))
        if not sets:
            raise ToolError("No fields to update were provided.")
        sets.append("updated_at = ?"); params.append(_now())
        params.append(expense_id)
        conn.execute(f"UPDATE expenses SET {', '.join(sets)} WHERE id = ?", params)
        row = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        return {"message": "Expense updated", "expense": expense_dict(row)}


@mcp.tool()
def delete_expense(expense_id: int) -> dict:
    """Soft-delete an expense (recoverable with restore_expense)."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE expenses SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (_now(), expense_id),
        )
        if cur.rowcount == 0:
            raise ToolError(f"Expense {expense_id} not found or already deleted.")
    return {"message": f"Expense {expense_id} deleted. Use restore_expense to undo."}


@mcp.tool()
def restore_expense(expense_id: int) -> dict:
    """Restore a soft-deleted expense."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE expenses SET deleted_at = NULL, updated_at = ? "
            "WHERE id = ? AND deleted_at IS NOT NULL", (_now(), expense_id),
        )
        if cur.rowcount == 0:
            raise ToolError(f"Expense {expense_id} is not in the deleted state.")
    return {"message": f"Expense {expense_id} restored."}


@mcp.tool()
def get_expense(expense_id: int) -> dict:
    """Fetch one expense by id (including deleted ones)."""
    with db() as conn:
        row = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        if not row:
            raise ToolError(f"Expense {expense_id} not found.")
        return expense_dict(row)


@mcp.tool()
def list_expenses(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
    merchant: Optional[str] = None,
    payment_method: Optional[str] = None,
    tag: Optional[str] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    sort_by: str = "date",
    sort_order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List expenses with filters, sorting and pagination.

    sort_by: date | amount | category | merchant | id.  sort_order: asc | desc.
    Returns the page plus the total count and total amount for the whole filter.
    """
    if sort_by not in {"date", "amount", "category", "merchant", "id"}:
        raise ToolError("sort_by must be one of: date, amount, category, merchant, id")
    order = "ASC" if sort_order.lower() == "asc" else "DESC"
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    where, params = build_where(start_date, end_date, category, merchant,
                                payment_method, tag, min_amount, max_amount)
    with db() as conn:
        agg = conn.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(amount),0) t FROM expenses WHERE {where}", params
        ).fetchone()
        rows = conn.execute(
            f"SELECT * FROM expenses WHERE {where} ORDER BY {sort_by} {order}, id DESC "
            f"LIMIT ? OFFSET ?", [*params, limit, offset],
        ).fetchall()
    return {
        "total_matching": agg["n"], "total_amount": r2(agg["t"]), "currency": CURRENCY,
        "returned": len(rows), "offset": offset,
        "expenses": [expense_dict(r) for r in rows],
    }


@mcp.tool()
def search_expenses(query: str, limit: int = 25) -> dict:
    """Full-text style search across description, merchant, category, subcategory and tags."""
    if not query.strip():
        raise ToolError("Query cannot be empty.")
    where, params = build_where(text=query)
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM expenses WHERE {where} ORDER BY date DESC, id DESC LIMIT ?",
            [*params, max(1, min(int(limit), 200))],
        ).fetchall()
    return {"query": query, "count": len(rows), "expenses": [expense_dict(r) for r in rows]}


# ================================ ANALYTICS ================================= #
_GROUPS = {
    "category": "category",
    "subcategory": "category || ' / ' || subcategory",
    "merchant": "CASE WHEN merchant = '' THEN '(none)' ELSE merchant END",
    "payment_method": "payment_method",
    "day": "date",
    "week": "strftime('%Y-W%W', date)",
    "month": "strftime('%Y-%m', date)",
    "year": "strftime('%Y', date)",
    "weekday": "CASE strftime('%w', date) WHEN '0' THEN 'Sun' WHEN '1' THEN 'Mon' "
               "WHEN '2' THEN 'Tue' WHEN '3' THEN 'Wed' WHEN '4' THEN 'Thu' "
               "WHEN '5' THEN 'Fri' ELSE 'Sat' END",
}
_TIME_GROUPS = {"day", "week", "month", "year"}


@mcp.tool()
def summarize_spending(
    group_by: str = "category",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
    merchant: Optional[str] = None,
    payment_method: Optional[str] = None,
    tag: Optional[str] = None,
) -> dict:
    """Aggregate spending grouped by a dimension.

    group_by: category | subcategory | merchant | payment_method | day | week |
              month | year | weekday
    Returns total, count, average and share-of-total for each group.
    """
    if group_by not in _GROUPS:
        raise ToolError(f"group_by must be one of: {', '.join(_GROUPS)}")
    where, params = build_where(start_date, end_date, category, merchant, payment_method, tag)
    order = "grp ASC" if group_by in _TIME_GROUPS else "total DESC"
    with db() as conn:
        rows = conn.execute(
            f"SELECT {_GROUPS[group_by]} AS grp, SUM(amount) total, COUNT(*) n, AVG(amount) avg "
            f"FROM expenses WHERE {where} GROUP BY grp ORDER BY {order}", params,
        ).fetchall()
    grand = sum(r["total"] for r in rows) or 0
    return {
        "group_by": group_by, "currency": CURRENCY,
        "grand_total": r2(grand), "transactions": sum(r["n"] for r in rows),
        "groups": [
            {"group": r["grp"], "total": r2(r["total"]), "count": r["n"],
             "average": r2(r["avg"]), "percent": round(100 * r["total"] / grand, 1) if grand else 0}
            for r in rows
        ],
    }


@mcp.tool()
def monthly_report(month: Optional[str] = None) -> dict:
    """Comprehensive report for a month (YYYY-MM, default current month): totals, category
    breakdown, top merchants, biggest expenses, comparison with the previous month,
    budget status and (for the current month) a month-end projection."""
    month, first, last = month_bounds(month)
    prev_month = add_months(date.fromisoformat(first), -1).strftime("%Y-%m")
    _, pfirst, plast = month_bounds(prev_month)
    with db() as conn:
        cur = period_total(conn, first, last)
        prev = period_total(conn, pfirst, plast)
        by_cat = conn.execute(
            "SELECT category, SUM(amount) t, COUNT(*) n FROM expenses "
            "WHERE deleted_at IS NULL AND date BETWEEN ? AND ? GROUP BY category ORDER BY t DESC",
            (first, last)).fetchall()
        merchants = conn.execute(
            "SELECT merchant, SUM(amount) t, COUNT(*) n FROM expenses "
            "WHERE deleted_at IS NULL AND date BETWEEN ? AND ? AND merchant != '' "
            "GROUP BY merchant ORDER BY t DESC LIMIT 5", (first, last)).fetchall()
        top = conn.execute(
            "SELECT * FROM expenses WHERE deleted_at IS NULL AND date BETWEEN ? AND ? "
            "ORDER BY amount DESC LIMIT 5", (first, last)).fetchall()
        busiest = conn.execute(
            "SELECT date, SUM(amount) t FROM expenses WHERE deleted_at IS NULL "
            "AND date BETWEEN ? AND ? GROUP BY date ORDER BY t DESC LIMIT 1", (first, last)).fetchone()
        budgets = _budget_status(conn, month)

    today = date.today()
    days_in_month = calendar.monthrange(int(month[:4]), int(month[5:]))[1]
    in_progress = first <= today.isoformat() <= last
    days_elapsed = today.day if in_progress else days_in_month
    daily_avg = cur["total"] / days_elapsed if days_elapsed else 0
    change = cur["total"] - prev["total"]
    report = {
        "month": month, "currency": CURRENCY,
        "total": cur["total"], "transactions": cur["count"],
        "daily_average": r2(daily_avg),
        "average_transaction": r2(cur["total"] / cur["count"]) if cur["count"] else 0,
        "vs_previous_month": {
            "month": prev_month, "previous_total": prev["total"], "change": r2(change),
            "change_percent": round(100 * change / prev["total"], 1) if prev["total"] else None,
        },
        "by_category": [
            {"category": r["category"], "total": r2(r["t"]), "count": r["n"],
             "percent": round(100 * r["t"] / cur["total"], 1) if cur["total"] else 0}
            for r in by_cat],
        "top_merchants": [{"merchant": r["merchant"], "total": r2(r["t"]), "count": r["n"]} for r in merchants],
        "largest_expenses": [expense_dict(r) for r in top],
        "highest_spend_day": {"date": busiest["date"], "total": r2(busiest["t"])} if busiest else None,
        "budgets": budgets,
    }
    if in_progress:
        report["projected_month_end_total"] = r2(daily_avg * days_in_month)
        report["days_remaining"] = days_in_month - days_elapsed
    return report


@mcp.tool()
def spending_trends(months: int = 6) -> dict:
    """Month-by-month totals for the last N months (max 36) with month-over-month change
    and the top category in each month."""
    months = max(1, min(int(months), 36))
    start = add_months(date.today().replace(day=1), -(months - 1))
    with db() as conn:
        totals = {r["m"]: (r["t"], r["n"]) for r in conn.execute(
            "SELECT strftime('%Y-%m', date) m, SUM(amount) t, COUNT(*) n FROM expenses "
            "WHERE deleted_at IS NULL AND date >= ? GROUP BY m", (start.isoformat(),))}
        tops = {}
        for r in conn.execute(
            "SELECT strftime('%Y-%m', date) m, category, SUM(amount) t FROM expenses "
            "WHERE deleted_at IS NULL AND date >= ? GROUP BY m, category ORDER BY m, t DESC",
                (start.isoformat(),)):
            tops.setdefault(r["m"], (r["category"], r["t"]))
    out, prev = [], None
    for i in range(months):
        key = add_months(start, i).strftime("%Y-%m")
        t, n = totals.get(key, (0, 0))
        item = {"month": key, "total": r2(t), "count": n,
                "top_category": tops[key][0] if key in tops else None}
        if prev is not None:
            item["change_vs_prev"] = r2(t - prev)
            item["change_percent"] = round(100 * (t - prev) / prev, 1) if prev else None
        out.append(item)
        prev = t
    values = [m["total"] for m in out]
    return {"currency": CURRENCY, "months": out,
            "average_per_month": r2(statistics.mean(values)),
            "highest_month": max(out, key=lambda m: m["total"])["month"],
            "lowest_month": min(out, key=lambda m: m["total"])["month"]}


@mcp.tool()
def compare_periods(start1: str, end1: str, start2: str, end2: str) -> dict:
    """Compare two date ranges overall and per category (period 2 relative to period 1)."""
    s1, e1, s2, e2 = parse_date(start1), parse_date(end1), parse_date(start2), parse_date(end2)

    def by_cat(conn, s, e):
        return {r["category"]: r["t"] for r in conn.execute(
            "SELECT category, SUM(amount) t FROM expenses WHERE deleted_at IS NULL "
            "AND date BETWEEN ? AND ? GROUP BY category", (s, e))}

    with db() as conn:
        t1, t2 = period_total(conn, s1, e1), period_total(conn, s2, e2)
        c1, c2 = by_cat(conn, s1, e1), by_cat(conn, s2, e2)
    cats = []
    for c in sorted(set(c1) | set(c2)):
        a, b = c1.get(c, 0), c2.get(c, 0)
        cats.append({"category": c, "period1": r2(a), "period2": r2(b), "change": r2(b - a),
                     "change_percent": round(100 * (b - a) / a, 1) if a else None})
    cats.sort(key=lambda x: abs(x["change"]), reverse=True)
    diff = t2["total"] - t1["total"]
    return {
        "currency": CURRENCY,
        "period1": {"start": s1, "end": e1, **t1}, "period2": {"start": s2, "end": e2, **t2},
        "change": r2(diff),
        "change_percent": round(100 * diff / t1["total"], 1) if t1["total"] else None,
        "by_category": cats,
    }


@mcp.tool()
def detect_anomalies(days: int = 90, z_threshold: float = 2.0) -> dict:
    """Flag unusually large expenses (relative to each category's history) and possible
    duplicates (same date, amount and merchant) within the last N days."""
    since = (date.today() - timedelta(days=max(1, int(days)))).isoformat()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM expenses WHERE deleted_at IS NULL AND date >= ? ORDER BY date",
            (since,)).fetchall()
    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)

    outliers = []
    for cat, items in by_cat.items():
        amounts = [i["amount"] for i in items]
        if len(amounts) < 5:
            continue
        mean, sd = statistics.mean(amounts), statistics.pstdev(amounts)
        if sd == 0:
            continue
        for i in items:
            z = (i["amount"] - mean) / sd
            if z >= z_threshold:
                outliers.append({**expense_dict(i), "category_average": r2(mean), "z_score": round(z, 2)})
    outliers.sort(key=lambda x: x["z_score"], reverse=True)

    seen: dict[tuple, list] = {}
    for r in rows:
        seen.setdefault((r["date"], r["amount"], r["merchant"].lower()), []).append(r["id"])
    dupes = [{"date": k[0], "amount": r2(k[1]), "merchant": k[2], "expense_ids": v}
             for k, v in seen.items() if len(v) > 1]
    return {"window_days": days, "unusually_large": outliers[:25], "possible_duplicates": dupes[:25]}


# ================================= BUDGETS ================================== #
def _budget_status(conn: sqlite3.Connection, month: str) -> list[dict]:
    month, first, last = month_bounds(month)
    spent = {r["category"].lower(): r["t"] for r in conn.execute(
        "SELECT category, SUM(amount) t FROM expenses WHERE deleted_at IS NULL "
        "AND date BETWEEN ? AND ? GROUP BY category", (first, last))}
    overall = sum(spent.values())
    days_in_month = calendar.monthrange(int(month[:4]), int(month[5:]))[1]
    today = date.today()
    in_progress = first <= today.isoformat() <= last
    out = []
    for b in conn.execute("SELECT * FROM budgets ORDER BY category"):
        s = overall if b["category"] == OVERALL_BUDGET else spent.get(b["category"].lower(), 0)
        pct = s / b["monthly_limit"]
        status = "exceeded" if pct >= 1 else "warning" if pct >= b["alert_threshold"] else "ok"
        item = {
            "category": "OVERALL" if b["category"] == OVERALL_BUDGET else b["category"],
            "limit": r2(b["monthly_limit"]), "spent": r2(s), "remaining": r2(b["monthly_limit"] - s),
            "percent_used": round(100 * pct, 1), "status": status,
        }
        if in_progress and today.day:
            proj = s / today.day * days_in_month
            item["projected_month_end"] = r2(proj)
            item["on_track"] = proj <= b["monthly_limit"]
        out.append(item)
    return out


@mcp.tool()
def set_budget(category: str, monthly_limit: float, alert_threshold: float = 0.8) -> dict:
    """Create or update a monthly budget. Use category 'overall' for a total budget.
    alert_threshold (0-1) is the fraction at which status becomes 'warning'."""
    limit = check_amount(monthly_limit)
    if not 0 < alert_threshold <= 1:
        raise ToolError("alert_threshold must be between 0 and 1.")
    key = OVERALL_BUDGET if category.strip().lower() in {"overall", "total", "*", "all"} \
        else norm_category(category)
    with db() as conn:
        if key != OVERALL_BUDGET:
            key = ensure_category(conn, key)
        conn.execute(
            "INSERT INTO budgets(category, monthly_limit, alert_threshold, created_at) "
            "VALUES (?,?,?,?) ON CONFLICT(category) DO UPDATE SET "
            "monthly_limit = excluded.monthly_limit, alert_threshold = excluded.alert_threshold",
            (key, limit, alert_threshold, _now()))
    return {"message": "Budget saved", "category": "OVERALL" if key == OVERALL_BUDGET else key,
            "monthly_limit": limit, "alert_threshold": alert_threshold}


@mcp.tool()
def list_budgets() -> dict:
    """List all configured budgets."""
    with db() as conn:
        rows = conn.execute("SELECT * FROM budgets ORDER BY category").fetchall()
    return {"currency": CURRENCY, "budgets": [
        {"category": "OVERALL" if r["category"] == OVERALL_BUDGET else r["category"],
         "monthly_limit": r2(r["monthly_limit"]), "alert_threshold": r["alert_threshold"]}
        for r in rows]}


@mcp.tool()
def delete_budget(category: str) -> dict:
    """Delete a budget ('overall' for the total budget)."""
    key = OVERALL_BUDGET if category.strip().lower() in {"overall", "total", "*", "all"} else category.strip()
    with db() as conn:
        cur = conn.execute("DELETE FROM budgets WHERE category = ? COLLATE NOCASE", (key,))
        if cur.rowcount == 0:
            raise ToolError(f"No budget found for '{category}'.")
    return {"message": f"Budget for '{category}' deleted."}


@mcp.tool()
def budget_status(month: Optional[str] = None) -> dict:
    """Spent vs. limit for every budget in a month (YYYY-MM, default current), with
    warning/exceeded flags and month-end projections."""
    month, _, _ = month_bounds(month)
    with db() as conn:
        return {"month": month, "currency": CURRENCY, "budgets": _budget_status(conn, month)}


# ============================ RECURRING EXPENSES ============================ #
@mcp.tool()
def add_recurring_expense(
    name: str,
    amount: float,
    category: str,
    frequency: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    merchant: str = "",
    description: str = "",
    payment_method: str = "other",
    tags: Optional[list[str]] = None,
) -> dict:
    """Define a recurring expense (rent, Netflix, SIP...). Expenses are generated
    automatically when due (on server start or via process_recurring).

    frequency: daily | weekly | biweekly | monthly | quarterly | yearly
    start_date: first due date (default today). Past dates are back-filled.
    """
    if frequency not in FREQUENCIES:
        raise ToolError(f"frequency must be one of {FREQUENCIES}")
    start = parse_date(start_date)
    end = parse_date(end_date, default_today=False)
    if end and end < start:
        raise ToolError("end_date cannot be before start_date.")
    with db() as conn:
        cat = ensure_category(conn, norm_category(category))
        cur = conn.execute(
            "INSERT INTO recurring(name, amount, category, merchant, description, payment_method, "
            "tags, frequency, next_due, end_date, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (name.strip(), check_amount(amount), cat, merchant.strip(), description.strip(),
             payment_method.strip().lower(), tags_to_db(tags), frequency, start, end, _now()))
        rid = cur.lastrowid
        created = run_recurring(conn, date.today().isoformat())
    return {"message": "Recurring expense created", "recurring_id": rid, "first_due": start,
            "expenses_generated_now": len([c for c in created if c["recurring_id"] == rid])}


@mcp.tool()
def list_recurring_expenses(include_inactive: bool = False) -> dict:
    """List recurring expense definitions and when each is next due."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM recurring" + ("" if include_inactive else " WHERE active = 1")
            + " ORDER BY next_due").fetchall()
    return {"currency": CURRENCY, "recurring": [
        {**dict(r), "amount": r2(r["amount"]), "tags": tags_from_db(r["tags"]),
         "active": bool(r["active"])} for r in rows]}


@mcp.tool()
def set_recurring_active(recurring_id: int, active: bool) -> dict:
    """Pause (active=false) or resume (active=true) a recurring expense."""
    with db() as conn:
        cur = conn.execute("UPDATE recurring SET active = ? WHERE id = ?", (int(active), recurring_id))
        if cur.rowcount == 0:
            raise ToolError(f"Recurring expense {recurring_id} not found.")
    return {"message": f"Recurring expense {recurring_id} {'resumed' if active else 'paused'}."}


@mcp.tool()
def delete_recurring_expense(recurring_id: int) -> dict:
    """Delete a recurring definition. Already generated expenses are kept."""
    with db() as conn:
        cur = conn.execute("DELETE FROM recurring WHERE id = ?", (recurring_id,))
        if cur.rowcount == 0:
            raise ToolError(f"Recurring expense {recurring_id} not found.")
    return {"message": f"Recurring expense {recurring_id} deleted (generated expenses kept)."}


@mcp.tool()
def process_recurring(as_of: Optional[str] = None) -> dict:
    """Generate expenses for all recurring items due on or before `as_of` (default today)."""
    with db() as conn:
        created = run_recurring(conn, parse_date(as_of))
    return {"created_count": len(created), "created": created}


# ================================ CATEGORIES ================================ #
@mcp.tool()
def list_categories() -> dict:
    """All categories with transaction counts and lifetime totals."""
    with db() as conn:
        rows = conn.execute(
            "SELECT c.name, COUNT(e.id) n, COALESCE(SUM(e.amount),0) t FROM categories c "
            "LEFT JOIN expenses e ON e.category = c.name AND e.deleted_at IS NULL "
            "GROUP BY c.name ORDER BY t DESC, c.name").fetchall()
    return {"currency": CURRENCY, "categories": [
        {"name": r["name"], "transactions": r["n"], "total": r2(r["t"])} for r in rows]}


@mcp.tool()
def add_category(name: str) -> dict:
    """Create a new category."""
    n = norm_category(name)
    with db() as conn:
        if conn.execute("SELECT 1 FROM categories WHERE name = ?", (n,)).fetchone():
            raise ToolError(f"Category '{n}' already exists.")
        conn.execute("INSERT INTO categories(name, created_at) VALUES (?, ?)", (n, _now()))
    return {"message": f"Category '{n}' created."}


@mcp.tool()
def rename_category(old_name: str, new_name: str) -> dict:
    """Rename a category. If new_name already exists the two are merged
    (expenses, budgets and recurring items move to new_name)."""
    new = norm_category(new_name)
    with db() as conn:
        old_row = conn.execute("SELECT name FROM categories WHERE name = ?", (old_name.strip(),)).fetchone()
        if not old_row:
            raise ToolError(f"Category '{old_name}' not found.")
        old = old_row["name"]
        if old.lower() == new.lower():
            conn.execute("UPDATE categories SET name = ? WHERE name = ?", (new, old))
            return {"message": f"Category renamed to '{new}'."}
        exists = conn.execute("SELECT name FROM categories WHERE name = ?", (new,)).fetchone()
        if exists:
            target = exists["name"]
            n = conn.execute("UPDATE expenses SET category = ? WHERE category = ?", (target, old)).rowcount
            conn.execute("UPDATE recurring SET category = ? WHERE category = ? COLLATE NOCASE", (target, old))
            # Keep the target's budget if both have one; otherwise move it.
            if conn.execute("SELECT 1 FROM budgets WHERE category = ?", (target,)).fetchone():
                conn.execute("DELETE FROM budgets WHERE category = ?", (old,))
            else:
                conn.execute("UPDATE budgets SET category = ? WHERE category = ?", (target, old))
            conn.execute("DELETE FROM categories WHERE name = ?", (old,))
            return {"message": f"Merged '{old}' into '{target}'.", "expenses_moved": n}
        conn.execute("UPDATE categories SET name = ? WHERE name = ?", (new, old))
        conn.execute("UPDATE recurring SET category = ? WHERE category = ? COLLATE NOCASE", (new, old))
        conn.execute("UPDATE budgets SET category = ? WHERE category = ? COLLATE NOCASE", (new, old))
    return {"message": f"Category '{old}' renamed to '{new}'."}


# ============================== IMPORT / EXPORT ============================= #
_CSV_FIELDS = ["id", "date", "amount", "category", "subcategory", "merchant",
               "description", "payment_method", "tags"]


@mcp.tool()
def export_csv(
    filename: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
) -> dict:
    """Export (filtered) expenses to a CSV file inside the exports folder."""
    where, params = build_where(start_date, end_date, category)
    name = Path(filename).name if filename else f"expenses-{datetime.now():%Y%m%d-%H%M%S}.csv"
    if not name.lower().endswith(".csv"):
        name += ".csv"
    path = EXPORT_DIR / name
    with db() as conn:
        rows = conn.execute(f"SELECT * FROM expenses WHERE {where} ORDER BY date, id", params).fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        w.writeheader()
        for r in rows:
            d = expense_dict(r)
            w.writerow({k: (",".join(d["tags"]) if k == "tags" else d.get(k, "")) for k in _CSV_FIELDS})
    return {"message": f"Exported {len(rows)} expenses", "file": str(path)}


@mcp.tool()
def import_csv(file_path: str, dry_run: bool = True, skip_duplicates: bool = True) -> dict:
    """Import expenses from a CSV. Required columns: date, amount, category.
    Optional: description, merchant, payment_method, subcategory, tags (comma-separated).

    Defaults to dry_run=True so you can preview; call again with dry_run=False to save.
    Duplicates (same date, amount, category, merchant, description) are skipped.
    """
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise ToolError(f"File not found: {path}")
    imported, skipped, errors = 0, 0, []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = {c.strip().lower() for c in (reader.fieldnames or [])}
        missing = {"date", "amount", "category"} - cols
        if missing:
            raise ToolError(f"CSV is missing required column(s): {', '.join(sorted(missing))}")
        with db() as conn:
            for line, raw in enumerate(reader, start=2):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
                try:
                    d = parse_date(row["date"])
                    amt = check_amount(row["amount"].replace(",", ""))
                    cat = norm_category(row["category"])
                    if skip_duplicates and conn.execute(
                        "SELECT 1 FROM expenses WHERE deleted_at IS NULL AND date=? AND amount=? "
                        "AND category=? COLLATE NOCASE AND merchant=? AND description=?",
                        (d, amt, cat, row.get("merchant", ""), row.get("description", ""))
                    ).fetchone():
                        skipped += 1
                        continue
                    if not dry_run:
                        insert_expense(conn, amount=amt, category=cat, date_str=d,
                                       description=row.get("description", ""),
                                       merchant=row.get("merchant", ""),
                                       payment_method=row.get("payment_method") or "other",
                                       subcategory=row.get("subcategory", ""),
                                       tags=row.get("tags", ""))
                    imported += 1
                except ToolError as e:
                    errors.append(f"line {line}: {e}")
    return {"dry_run": dry_run, "would_import" if dry_run else "imported": imported,
            "duplicates_skipped": skipped, "error_count": len(errors), "errors": errors[:20]}


@mcp.tool()
def backup_database() -> dict:
    """Create a consistent timestamped backup copy of the SQLite database."""
    dest = BACKUP_DIR / f"expenses-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return {"message": "Backup created", "file": str(dest),
            "size_kb": round(dest.stat().st_size / 1024, 1)}


@mcp.tool()
def database_info() -> dict:
    """Database location, row counts and date range of recorded expenses."""
    with db() as conn:
        e = conn.execute(
            "SELECT COUNT(*) n, MIN(date) first, MAX(date) last, COALESCE(SUM(amount),0) t "
            "FROM expenses WHERE deleted_at IS NULL").fetchone()
        deleted = conn.execute("SELECT COUNT(*) FROM expenses WHERE deleted_at IS NOT NULL").fetchone()[0]
        cats = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        buds = conn.execute("SELECT COUNT(*) FROM budgets").fetchone()[0]
        rec = conn.execute("SELECT COUNT(*) FROM recurring WHERE active = 1").fetchone()[0]
    return {"database": str(DB_PATH), "size_kb": round(DB_PATH.stat().st_size / 1024, 1),
            "currency": CURRENCY, "expenses": e["n"], "deleted_expenses": deleted,
            "first_expense_date": e["first"], "last_expense_date": e["last"],
            "lifetime_total": r2(e["t"]), "categories": cats, "budgets": buds,
            "active_recurring": rec}


# ================================ RESOURCES ================================= #
@mcp.resource("expense://categories")
def categories_resource() -> str:
    """JSON list of all category names."""
    with db() as conn:
        return json.dumps([r["name"] for r in conn.execute("SELECT name FROM categories ORDER BY name")])


@mcp.resource("expense://summary/{month}")
def month_summary_resource(month: str) -> str:
    """JSON category totals for a month (YYYY-MM)."""
    month, first, last = month_bounds(month)
    with db() as conn:
        rows = conn.execute(
            "SELECT category, SUM(amount) t FROM expenses WHERE deleted_at IS NULL "
            "AND date BETWEEN ? AND ? GROUP BY category ORDER BY t DESC", (first, last)).fetchall()
    return json.dumps({"month": month, "currency": CURRENCY,
                       "total": r2(sum(r["t"] for r in rows)),
                       "by_category": {r["category"]: r2(r["t"]) for r in rows}}, indent=2)


@mcp.resource("expense://schema")
def schema_resource() -> str:
    """The SQLite schema used by this server."""
    return SCHEMA


# ================================= PROMPTS ================================== #
@mcp.prompt()
def monthly_review(month: str = "") -> str:
    """Guided monthly spending review."""
    m = month or date.today().strftime("%Y-%m")
    return (
        f"Review my spending for {m}. Call monthly_report and budget_status, then: "
        "1) summarize total spend and how it compares to last month, "
        "2) point out the top 3 categories and any budget warnings or overspending, "
        "3) run detect_anomalies and mention anything suspicious, "
        "4) suggest 3 concrete, realistic ways to save money next month."
    )


@mcp.prompt()
def quick_log(text: str) -> str:
    """Turn a natural-language note into logged expenses."""
    return (
        "Extract every expense from the text below and log them with add_expense "
        "(or add_expenses_bulk for several). Infer category, merchant and date when "
        "obvious; ask me only if the amount is unclear. Confirm what you saved.\n\n"
        f"Text: {text}"
    )


if __name__ == "__main__":
    mcp.run()
