"""0005: folders that hold any mix of information.

* folders     — a tree (parent_id self-reference). Each folder can carry a
                field *template* that new notes created inside it start with.
* notes       — free-form items: title + body + user-defined properties
                (an ordered JSON array of {name, type, value}), so every note
                can have whatever fields it needs.
* files       — arbitrary documents (PDF, spreadsheets, …), content-addressed
                like pictures.
* folder_items — polymorphic membership: (folder, item_type, item_id). An item
                can be in many folders; removing it from a folder never deletes
                it. Because a polymorphic column can't carry a FOREIGN KEY,
                AFTER DELETE triggers on every item table clean up the links.
"""
import importlib.util
from pathlib import Path


def _load(name):
    spec = importlib.util.spec_from_file_location(f"dv_{name}", Path(__file__).with_name(name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_m0002 = _load("0002_search_and_audit.py")
_m0004 = _load("0004_audit_snapshots.py")

ITEM_TABLES = {"contact": "contacts", "picture": "pictures", "expense": "expenses", "code": "codes",
               "record": "section_records", "note": "notes", "file": "files"}
# search_index rowid = id * 8 + code (0003); 0 and 6/7 were still free
NEW_SEARCH = {
    "notes": ("note", 6, "NEW.title",
              "concat_ws(' ', NEW.body, (SELECT group_concat(json_extract(p.value, '$.name') || ' ' || "
              "COALESCE(json_extract(p.value, '$.value'), ''), ' ') FROM json_each(NEW.properties) p))"),
    "files": ("file", 7, "NEW.original_name", "concat_ws(' ', NEW.description, NEW.mime)"),
    "folders": ("folder", 0, "NEW.name", "NEW.description"),
}


def _snapshot(conn, table: str, ref: str) -> str:
    """0004's row snapshot, plus folder membership for notes/files so an
    undelete puts them back where they were (the cleanup trigger runs AFTER
    DELETE, so a BEFORE DELETE snapshot still sees the links)."""
    snap = _m0004._snapshot(conn, table, ref)
    item_type = {"notes": "note", "files": "file"}.get(table)
    if not item_type:
        return snap
    return (snap[:-1] + f", 'folders', (SELECT json_group_array(folder_id) FROM folder_items "
            f"WHERE item_type = '{item_type}' AND item_id = {ref}.id))")


def upgrade(conn):
    conn.execute("""
        CREATE TABLE folders (
            id          INTEGER PRIMARY KEY,
            parent_id   INTEGER REFERENCES folders(id) ON DELETE CASCADE,
            name        TEXT NOT NULL CHECK (length(trim(name)) > 0),
            icon        TEXT NOT NULL DEFAULT '📁',
            color       TEXT NOT NULL DEFAULT '#6366f1',
            description TEXT NOT NULL DEFAULT '',
            template    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(template) AND json_type(template) = 'array'),
            position    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            CHECK (parent_id IS NULL OR parent_id <> id)
        )""")
    conn.execute("CREATE INDEX idx_folders_parent ON folders(parent_id, position)")

    conn.execute("""
        CREATE TABLE notes (
            id          INTEGER PRIMARY KEY,
            title       TEXT NOT NULL CHECK (length(trim(title)) > 0),
            body        TEXT NOT NULL DEFAULT '',
            properties  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(properties) AND json_type(properties) = 'array'),
            pinned      INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )""")

    conn.execute("""
        CREATE TABLE files (
            id            INTEGER PRIMARY KEY,
            sha256        TEXT NOT NULL UNIQUE,
            stored_name   TEXT NOT NULL,
            original_name TEXT NOT NULL,
            mime          TEXT NOT NULL,
            size_bytes    INTEGER NOT NULL CHECK (size_bytes >= 0),
            description   TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )""")

    conn.execute(f"""
        CREATE TABLE folder_items (
            id        INTEGER PRIMARY KEY,
            folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
            item_type TEXT NOT NULL CHECK (item_type IN ({", ".join(f"'{t}'" for t in ITEM_TABLES)})),
            item_id   INTEGER NOT NULL,
            position  INTEGER NOT NULL DEFAULT 0,
            added_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            UNIQUE (folder_id, item_type, item_id)
        )""")
    # "which folders is this item in?" lookups
    conn.execute("CREATE INDEX idx_folder_items_item ON folder_items(item_type, item_id)")

    for table in ("folders", "notes", "files"):
        conn.execute(f"""CREATE TRIGGER trg_{table}_touch AFTER UPDATE ON {table} WHEN NEW.updated_at = OLD.updated_at
            BEGIN UPDATE {table} SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END""")

    # polymorphic "foreign key": drop memberships when the item itself is deleted
    for item_type, table in ITEM_TABLES.items():
        conn.execute(f"""CREATE TRIGGER trg_{table}_folder_cleanup AFTER DELETE ON {table} BEGIN
            DELETE FROM folder_items WHERE item_type = '{item_type}' AND item_id = OLD.id; END""")

    # full-text search for the new tables (same rowid scheme as 0003)
    for table, (kind, code, title, body) in NEW_SEARCH.items():
        new_row = (f"INSERT INTO search_index (rowid, kind, ref_id, title, body) "
                   f"VALUES (NEW.id * 8 + {code}, '{kind}', NEW.id, COALESCE({title}, ''), COALESCE({body}, ''));")
        drop_row = f"DELETE FROM search_index WHERE rowid = OLD.id * 8 + {code};"
        cols = _m0002._data_columns(conn, table)
        conn.execute(f"CREATE TRIGGER trg_{table}_fts_ins AFTER INSERT ON {table} BEGIN {new_row} END")
        conn.execute(f"CREATE TRIGGER trg_{table}_fts_upd AFTER UPDATE OF {cols} ON {table} BEGIN {drop_row} {new_row} END")
        conn.execute(f"CREATE TRIGGER trg_{table}_fts_del AFTER DELETE ON {table} BEGIN {drop_row} END")

    # history + undelete for the new tables (same snapshot triggers as 0004)
    for table in ("folders", "notes", "files"):
        cols = _m0002._data_columns(conn, table)
        snap = lambda ref, t=table: _snapshot(conn, t, ref)  # noqa: E731
        conn.execute(f"""CREATE TRIGGER trg_{table}_audit_insert AFTER INSERT ON {table} BEGIN
            INSERT INTO audit_log (table_name, row_id, action, new_data) VALUES ('{table}', NEW.id, 'INSERT', {snap('NEW')}); END""")
        conn.execute(f"""CREATE TRIGGER trg_{table}_audit_update AFTER UPDATE OF {cols} ON {table} BEGIN
            INSERT INTO audit_log (table_name, row_id, action, old_data, new_data)
            VALUES ('{table}', NEW.id, 'UPDATE', {snap('OLD')}, {snap('NEW')}); END""")
        conn.execute(f"""CREATE TRIGGER trg_{table}_audit_delete BEFORE DELETE ON {table} BEGIN
            INSERT INTO audit_log (table_name, row_id, action, old_data) VALUES ('{table}', OLD.id, 'DELETE', {snap('OLD')}); END""")
