"""0002: full-text search index + audit log, generated from a table spec.

Written as a Python migration because the trigger bodies are repetitive;
generating them keeps every table's triggers consistent.
"""

# table -> (search kind, SQL expr for title, SQL expr for body) using NEW.<col>
SEARCHABLE = {
    "contacts": (
        "contact",
        "trim(NEW.first_name || ' ' || NEW.last_name)",
        "concat_ws(' ', NEW.company, NEW.job_title, NEW.email, NEW.phone, NEW.city, NEW.region, NEW.country, NEW.notes)",
    ),
    "pictures": (
        "picture",
        "CASE WHEN NEW.title <> '' THEN NEW.title ELSE NEW.original_name END",
        "concat_ws(' ', NEW.original_name, NEW.description, NEW.album, NEW.camera)",
    ),
    "expenses": (
        "expense",
        "CASE WHEN NEW.merchant <> '' THEN NEW.merchant ELSE 'Expense ' || NEW.spent_on END",
        "concat_ws(' ', NEW.description, NEW.payment_method, NEW.spent_on, printf('%.2f', NEW.amount_cents / 100.0))",
    ),
    "codes": (
        "code",
        "CASE WHEN NEW.label <> '' THEN NEW.label ELSE NEW.payload END",
        "concat_ws(' ', NEW.payload, NEW.kind, NEW.notes)",
    ),
    "section_records": (
        "record",
        "(SELECT name FROM sections WHERE id = NEW.section_id)",
        "(SELECT group_concat(value, ' ') FROM json_each(NEW.data) WHERE type IN ('text', 'integer', 'real'))",
    ),
}

AUDITED = ["contacts", "pictures", "expenses", "expense_categories", "codes",
           "sections", "section_fields", "section_records", "tags"]


def _data_columns(conn, table):
    """Every column except updated_at, so triggers declared "UPDATE OF <these>"
    ignore the bookkeeping write made by the touch triggers."""
    return ", ".join(r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] != "updated_at")


def upgrade(conn):
    conn.execute("""
        CREATE VIRTUAL TABLE search_index USING fts5(
            kind UNINDEXED, ref_id UNINDEXED, title, body,
            tokenize = 'porter unicode61 remove_diacritics 2'
        )""")
    for table, (kind, title, body) in SEARCHABLE.items():
        insert = (f"INSERT INTO search_index (kind, ref_id, title, body) "
                  f"VALUES ('{kind}', NEW.id, COALESCE({title}, ''), COALESCE({body}, ''));")
        delete = f"DELETE FROM search_index WHERE kind = '{kind}' AND ref_id = OLD.id;"
        conn.execute(f"CREATE TRIGGER trg_{table}_fts_ins AFTER INSERT ON {table} BEGIN {insert} END")
        conn.execute(f"CREATE TRIGGER trg_{table}_fts_upd AFTER UPDATE OF {_data_columns(conn, table)} ON {table} "
                     f"BEGIN {delete} {insert} END")
        conn.execute(f"CREATE TRIGGER trg_{table}_fts_del AFTER DELETE ON {table} BEGIN {delete} END")

    conn.execute("""
        CREATE TABLE audit_log (
            id         INTEGER PRIMARY KEY,
            at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            table_name TEXT NOT NULL,
            row_id     INTEGER NOT NULL,
            action     TEXT NOT NULL CHECK (action IN ('INSERT', 'UPDATE', 'DELETE'))
        )""")
    conn.execute("CREATE INDEX idx_audit_table_row ON audit_log(table_name, row_id)")
    for table in AUDITED:
        for action, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
            event = f"UPDATE OF {_data_columns(conn, table)}" if action == "UPDATE" else action
            conn.execute(
                f"CREATE TRIGGER trg_{table}_audit_{action.lower()} AFTER {event} ON {table} "
                f"BEGIN INSERT INTO audit_log (table_name, row_id, action) VALUES ('{table}', {ref}.id, '{action}'); END")
