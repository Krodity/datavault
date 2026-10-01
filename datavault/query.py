"""Read-only SQL console with layered safety:

1. a separate connection opened with mode=ro + PRAGMA query_only
2. an authorizer callback that denies anything except reads
3. a progress handler that aborts runaway queries after a time budget
4. a row cap so a SELECT over a huge join can't exhaust memory
"""
from __future__ import annotations

import re
import sqlite3
import time

from .config import Config
from .db import connect
from .validation import ValidationError

_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE,
}
# PRAGMAs that only report information
_SAFE_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list", "user_version",
                 "page_count", "page_size", "freelist_count", "integrity_check", "quick_check",
                 "foreign_key_check", "compile_options", "database_list", "function_list", "table_list",
                 "data_version"}  # data_version is read internally by FTS5

_PRAGMAS_WITH_ARG = {"table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list",
                     "foreign_key_check", "integrity_check", "quick_check"}


def _authorizer(action, arg1, arg2, dbname, source):
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1:
        name = arg1.lower()
        # a bare PRAGMA only reads; an argument is allowed only where it names a table/index
        if name in _SAFE_PRAGMAS and (arg2 is None or name in _PRAGMAS_WITH_ARG):
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class QueryRunner:
    def __init__(self, cfg: Config, timeout_ms: int = 3000, max_rows: int = 5000):
        self.cfg, self.timeout_ms, self.max_rows = cfg, timeout_ms, max_rows

    def _conn(self) -> sqlite3.Connection:
        conn = connect(self.cfg.db_path, readonly=True)
        conn.row_factory = None
        conn.set_authorizer(_authorizer)
        return conn

    def run(self, sql: str, params: list | dict | None = None, *, explain: bool = False) -> dict:
        if not isinstance(sql, str):
            raise ValidationError({"sql": "must be text"})
        sql = sql.strip().rstrip(";").strip()
        if not sql:
            raise ValidationError({"sql": "is empty"})
        if len(sql) > 100_000:
            raise ValidationError({"sql": "is too long"})
        # newline first, so a trailing "-- comment" doesn't swallow the terminator
        if not sqlite3.complete_statement(sql + "\n;"):
            raise ValidationError({"sql": "is incomplete (unbalanced quotes or parentheses?)"})
        params = _check_params(params)
        if explain and not re.match(r"(?is)^\s*(--[^\n]*\n\s*)*explain\b", sql):
            sql = "EXPLAIN QUERY PLAN " + sql
        conn = self._conn()
        deadline = time.perf_counter() + self.timeout_ms / 1000
        conn.set_progress_handler(lambda: 1 if time.perf_counter() > deadline else 0, 10_000)
        started = time.perf_counter()
        try:
            cur = conn.execute(sql, params or ())
            cols = [d[0] for d in cur.description or []]
            data = cur.fetchmany(self.max_rows + 1)
        except sqlite3.DatabaseError as e:
            msg = str(e)
            if "interrupted" in msg:
                msg = f"query cancelled after {self.timeout_ms} ms"
            elif "not authorized" in msg or "authorization denied" in msg or "prohibited" in msg or "readonly" in msg:
                msg = "only read-only statements (SELECT, WITH, EXPLAIN, informational PRAGMAs) are allowed here"
            raise ValidationError({"sql": msg}) from None
        except (sqlite3.ProgrammingError, sqlite3.InterfaceError, OverflowError) as e:
            raise ValidationError({"sql": str(e)}) from None
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            conn.close()
        truncated = len(data) > self.max_rows
        data = data[: self.max_rows]
        out_rows = [[_jsonable(c) for c in r] for r in data]
        return {"columns": cols, "rows": out_rows, "row_count": len(out_rows), "truncated": truncated,
                "elapsed_ms": round(elapsed, 2), "types": _column_types(out_rows, len(cols))}

    def schema(self) -> list[dict]:
        """Introspect tables/views with columns, indexes and foreign keys."""
        conn = connect(self.cfg.db_path, readonly=True)
        try:
            objs = conn.execute("""
                SELECT name, type, sql FROM sqlite_schema
                 WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
                   AND name NOT LIKE 'search_index_%' ORDER BY type, name""").fetchall()
            out = []
            for o in objs:
                name = o["name"]
                cols = [dict(c) for c in conn.execute("SELECT * FROM pragma_table_info(?)", (name,))]
                fks = [dict(f) for f in conn.execute("SELECT * FROM pragma_foreign_key_list(?)", (name,))]
                idx = [dict(i) for i in conn.execute("SELECT name, \"unique\", origin FROM pragma_index_list(?)", (name,))]
                count = None
                if o["type"] == "table" and name != "search_index":
                    count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                out.append({"name": name, "type": o["type"], "sql": o["sql"], "columns": cols,
                            "foreign_keys": fks, "indexes": idx, "row_count": count})
            return out
        finally:
            conn.close()


def _check_params(params):
    """Bound parameters must be a flat list or {name: value} of scalars."""
    if params in (None, "", [], {}):
        return ()
    scalar = (str, int, float, bool, type(None))
    if isinstance(params, list) and all(isinstance(p, scalar) for p in params):
        return [int(p) if isinstance(p, bool) else p for p in params]
    if isinstance(params, dict) and all(isinstance(k, str) and isinstance(p, scalar) for k, p in params.items()):
        return {k: int(p) if isinstance(p, bool) else p for k, p in params.items()}
    raise ValidationError({"params": "must be a list or object of plain values"})


def _jsonable(x):
    if isinstance(x, bytes):
        return f"<blob {len(x)} bytes>"
    return x


def _column_types(data: list[list], ncols: int) -> list[str]:
    types = []
    for i in range(ncols):
        seen = {type(r[i]).__name__ for r in data[:200] if r[i] is not None}
        types.append("number" if seen and seen <= {"int", "float"} else "text" if seen else "null")
    return types


EXAMPLES = [
    ("Spending by category (this year)", """SELECT category, COUNT(*) AS n, printf('$%.2f', SUM(amount)) AS total
FROM v_expenses
WHERE spent_on >= strftime('%Y-01-01', 'now')
GROUP BY category
ORDER BY SUM(amount) DESC;"""),
    ("Month-over-month change", """WITH m AS (
  SELECT substr(spent_on, 1, 7) AS month, SUM(amount_cents) / 100.0 AS total
  FROM expenses GROUP BY month
)
SELECT month, total,
       round(total - LAG(total) OVER (ORDER BY month), 2) AS change,
       round(100.0 * (total - LAG(total) OVER (ORDER BY month)) / LAG(total) OVER (ORDER BY month), 1) AS pct
FROM m ORDER BY month DESC;"""),
    ("Running total this month", """SELECT spent_on, merchant, amount,
       SUM(amount) OVER (ORDER BY spent_on, id) AS running_total
FROM v_expenses
WHERE month = strftime('%Y-%m', 'now')
ORDER BY spent_on, id;"""),
    ("Over-budget categories", """SELECT c.name,
       c.monthly_budget_cents / 100.0 AS budget,
       COALESCE(SUM(e.amount_cents), 0) / 100.0 AS spent
FROM expense_categories c
LEFT JOIN expenses e ON e.category_id = c.id
  AND substr(e.spent_on, 1, 7) = strftime('%Y-%m', 'now')
WHERE c.monthly_budget_cents IS NOT NULL
GROUP BY c.id
HAVING spent > budget;"""),
    ("Contacts with tags", """SELECT c.full_name, c.company, group_concat(t.name, ', ') AS tags
FROM v_contacts c
LEFT JOIN contact_tags ct ON ct.contact_id = c.id
LEFT JOIN tags t ON t.id = ct.tag_id
GROUP BY c.id ORDER BY c.last_name;"""),
    ("Top merchants (rank)", """SELECT merchant, SUM(amount) AS total,
       RANK() OVER (ORDER BY SUM(amount) DESC) AS rnk
FROM v_expenses WHERE merchant <> ''
GROUP BY merchant LIMIT 10;"""),
    ("Custom section fields as columns", """-- pivot JSON documents into columns with json_extract
SELECT r.id, s.name AS section,
       json_extract(r.data, '$.title') AS title,
       r.data
FROM section_records r JOIN sections s ON s.id = r.section_id
ORDER BY r.id DESC;"""),
    ("Full-text search", """SELECT kind, ref_id, title, snippet(search_index, 3, '[', ']', '…', 8) AS hit
FROM search_index
WHERE search_index MATCH 'coffee*'
ORDER BY rank;"""),
    ("Change history", """SELECT table_name, action, COUNT(*) AS n, MAX(at) AS last_change
FROM audit_log GROUP BY table_name, action ORDER BY last_change DESC;"""),
    ("Index usage check", """EXPLAIN QUERY PLAN
SELECT * FROM expenses WHERE category_id = 1 AND spent_on >= '2026-01-01';"""),
]
