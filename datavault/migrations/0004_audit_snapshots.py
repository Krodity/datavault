"""0004: audit log with before/after row snapshots.

Each audit row now stores the row as JSON before (old_data) and after
(new_data) the change, built with json_object() inside the trigger. That gives
per-record change history with field-level diffs, and lets a deleted row be
restored exactly as it was.

DELETE triggers become BEFORE DELETE so a contact's snapshot can still see its
tags (contact_tags rows are removed by the ON DELETE CASCADE).
"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("dv_m0002b", Path(__file__).with_name("0002_search_and_audit.py"))
_m0002 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m0002)

AUDITED = _m0002.AUDITED


def _snapshot(conn, table: str, ref: str) -> str:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    pairs = ", ".join(f"'{c}', {ref}.{c}" for c in cols)
    if table == "contacts":
        pairs += (f", 'tags', (SELECT json_group_array(t.name) FROM contact_tags ct JOIN tags t ON t.id = ct.tag_id "
                  f"WHERE ct.contact_id = {ref}.id)")
    return f"json_object({pairs})"


def upgrade(conn):
    conn.execute("ALTER TABLE audit_log ADD COLUMN old_data TEXT CHECK (old_data IS NULL OR json_valid(old_data))")
    conn.execute("ALTER TABLE audit_log ADD COLUMN new_data TEXT CHECK (new_data IS NULL OR json_valid(new_data))")
    conn.execute("CREATE INDEX idx_audit_action ON audit_log(action, table_name)")
    for table in AUDITED:
        for action in ("insert", "update", "delete"):
            conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_audit_{action}")
        data_cols = _m0002._data_columns(conn, table)
        conn.execute(f"""CREATE TRIGGER trg_{table}_audit_insert AFTER INSERT ON {table} BEGIN
            INSERT INTO audit_log (table_name, row_id, action, new_data)
            VALUES ('{table}', NEW.id, 'INSERT', {_snapshot(conn, table, 'NEW')}); END""")
        conn.execute(f"""CREATE TRIGGER trg_{table}_audit_update AFTER UPDATE OF {data_cols} ON {table} BEGIN
            INSERT INTO audit_log (table_name, row_id, action, old_data, new_data)
            VALUES ('{table}', NEW.id, 'UPDATE', {_snapshot(conn, table, 'OLD')}, {_snapshot(conn, table, 'NEW')}); END""")
        conn.execute(f"""CREATE TRIGGER trg_{table}_audit_delete BEFORE DELETE ON {table} BEGIN
            INSERT INTO audit_log (table_name, row_id, action, old_data)
            VALUES ('{table}', OLD.id, 'DELETE', {_snapshot(conn, table, 'OLD')}); END""")
