"""0003: formula fields, faster search-index maintenance, FK indexes.

* section_fields.type gains 'formula'. SQLite can't ALTER a CHECK constraint,
  so the table is rebuilt (create new -> copy -> drop -> rename), the standard
  SQLite schema-change procedure.
* search_index rows get a deterministic rowid (id * 8 + kind code) so the
  update/delete triggers hit the rowid b-tree instead of scanning the whole
  FTS table — O(log n) per write instead of O(n), which matters for imports.
* Every foreign-key child column gets an index (SQLite doesn't create them),
  so ON DELETE SET NULL and "used by" lookups don't table-scan.
"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("dv_m0002", Path(__file__).with_name("0002_search_and_audit.py"))
_m0002 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m0002)

KIND_CODES = {"contact": 1, "picture": 2, "expense": 3, "code": 4, "record": 5}


def _data_columns(conn, table):
    return _m0002._data_columns(conn, table)


def upgrade(conn):
    # ---------------------------------------------------- section_fields rebuild
    conn.execute("""
        CREATE TABLE section_fields_new (
            id         INTEGER PRIMARY KEY,
            section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
            key        TEXT NOT NULL CHECK (key GLOB '[a-z_]*' AND key NOT GLOB '*[^a-z0-9_]*'),
            label      TEXT NOT NULL,
            type       TEXT NOT NULL CHECK (type IN ('text', 'longtext', 'number', 'money', 'date', 'boolean',
                                                     'select', 'url', 'email', 'picture', 'contact', 'code',
                                                     'formula')),
            required   INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
            options    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(options)),
            position   INTEGER NOT NULL DEFAULT 0,
            UNIQUE (section_id, key)
        )""")
    conn.execute("INSERT INTO section_fields_new SELECT id, section_id, key, label, type, required, options, position "
                 "FROM section_fields")
    conn.execute("DROP TABLE section_fields")  # also drops its audit triggers
    conn.execute("ALTER TABLE section_fields_new RENAME TO section_fields")
    for action, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
        event = f"UPDATE OF {_data_columns(conn, 'section_fields')}" if action == "UPDATE" else action
        conn.execute(f"CREATE TRIGGER trg_section_fields_audit_{action.lower()} AFTER {event} ON section_fields "
                     f"BEGIN INSERT INTO audit_log (table_name, row_id, action) "
                     f"VALUES ('section_fields', {ref}.id, '{action}'); END")

    # formulas that use TODAY() are refreshed once per day on read
    conn.execute("ALTER TABLE sections ADD COLUMN recalculated_on TEXT")

    # ------------------------------------------------------ search index by rowid
    for table in _m0002.SEARCHABLE:
        for suffix in ("ins", "upd", "del"):
            conn.execute(f"DROP TRIGGER trg_{table}_fts_{suffix}")
    conn.execute("DELETE FROM search_index")
    for table, (kind, title, body) in _m0002.SEARCHABLE.items():
        code = KIND_CODES[kind]
        new_row = (f"INSERT INTO search_index (rowid, kind, ref_id, title, body) "
                   f"VALUES (NEW.id * 8 + {code}, '{kind}', NEW.id, COALESCE({title}, ''), COALESCE({body}, ''));")
        drop_row = f"DELETE FROM search_index WHERE rowid = OLD.id * 8 + {code};"
        conn.execute(f"CREATE TRIGGER trg_{table}_fts_ins AFTER INSERT ON {table} BEGIN {new_row} END")
        conn.execute(f"CREATE TRIGGER trg_{table}_fts_upd AFTER UPDATE OF {_data_columns(conn, table)} ON {table} "
                     f"BEGIN {drop_row} {new_row} END")
        conn.execute(f"CREATE TRIGGER trg_{table}_fts_del AFTER DELETE ON {table} BEGIN {drop_row} END")
        # backfill existing rows (same expressions, with NEW -> the table alias)
        conn.execute(f"INSERT INTO search_index (rowid, kind, ref_id, title, body) "
                     f"SELECT NEW.id * 8 + {code}, '{kind}', NEW.id, COALESCE({title}, ''), COALESCE({body}, '') "
                     f"FROM {table} AS NEW")

    # ------------------------------------------------------------- FK indexes
    for name, table, col in [("idx_contacts_photo", "contacts", "photo_id"),
                             ("idx_expenses_contact", "expenses", "contact_id"),
                             ("idx_expenses_receipt", "expenses", "receipt_picture_id")]:
        # (section_fields.section_id is already covered by UNIQUE(section_id, key))
        conn.execute(f"CREATE INDEX {name} ON {table}({col})")
