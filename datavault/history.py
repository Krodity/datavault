"""Change history and undelete, built on the audit_log snapshots (migration 0004)."""
from __future__ import annotations

import json

from . import repo
from .db import transaction
from .validation import NotFound, ValidationError

# tables a user can browse history for / restore rows into
HISTORY_TABLES = {"contacts", "expenses", "codes", "section_records", "pictures", "sections", "expense_categories",
                  "notes", "files", "folders"}
# pictures/files are excluded from restore: their bytes are removed from disk on delete
RESTORABLE = {"contacts", "expenses", "codes", "section_records", "notes"}
# column -> table it must still point at; dangling references are cleared on restore
FOREIGN_KEYS = {
    "contacts": {"photo_id": "pictures"},
    "expenses": {"category_id": "expense_categories", "contact_id": "contacts", "receipt_picture_id": "pictures"},
    "section_records": {},
    "codes": {},
    "notes": {},
}
HIDDEN = {"updated_at", "created_at", "sha256", "stored_name"}


def _load(s: str | None) -> dict:
    return json.loads(s) if s else {}


def _label(table: str, d: dict) -> str:
    if table == "contacts":
        return f"{d.get('first_name', '')} {d.get('last_name', '')}".strip()
    if table == "expenses":
        return f"{d.get('merchant') or 'Expense'} · ${(d.get('amount_cents') or 0) / 100:,.2f} · {d.get('spent_on', '')}"
    if table == "codes":
        return d.get("label") or d.get("payload", "")
    if table == "section_records":
        data = _load(d.get("data")) if isinstance(d.get("data"), str) else d.get("data") or {}
        first = next((str(v) for v in data.values() if v not in (None, "")), "")
        return first[:60] or f"record #{d.get('id')}"
    return d.get("name") or d.get("title") or d.get("original_name") or f"#{d.get('id')}"


def _diff(old: dict, new: dict) -> list[dict]:
    out = []
    for k in sorted(set(old) | set(new)):
        if k in HIDDEN:
            continue
        a, b = old.get(k), new.get(k)
        if k == "data":  # section record JSON: diff the document's own fields
            da = _load(a) if isinstance(a, str) else (a or {})
            db = _load(b) if isinstance(b, str) else (b or {})
            out += [{"field": f, "old": da.get(f), "new": db.get(f)} for f in sorted(set(da) | set(db))
                    if da.get(f) != db.get(f)]
            continue
        if k == "tags":
            a, b = _load(a) if isinstance(a, str) else a, _load(b) if isinstance(b, str) else b
        if a != b and not (a in (None, "") and b in (None, "")):
            out.append({"field": k, "old": a, "new": b})
    return out


def history(conn, table: str, row_id: int, limit: int = 100) -> list[dict]:
    if table not in HISTORY_TABLES:
        raise ValidationError({"table": f"history is available for {', '.join(sorted(HISTORY_TABLES))}"})
    out = []
    for r in conn.execute("""SELECT * FROM audit_log WHERE table_name = ? AND row_id = ?
                             ORDER BY id DESC LIMIT ?""", (table, row_id, max(1, min(int(limit), 500)))):
        old, new = _load(r["old_data"]), _load(r["new_data"])
        out.append({"id": r["id"], "at": r["at"], "action": r["action"],
                    "changes": _diff(old, new), "snapshot": bool(old or new)})
    return out


def recently_deleted(conn, limit: int = 50) -> list[dict]:
    """Deleted rows that have not been restored since (their id is free)."""
    rows = conn.execute(f"""
        SELECT a.* FROM audit_log a
         WHERE a.action = 'DELETE' AND a.old_data IS NOT NULL
           AND a.table_name IN ({",".join("?" * len(RESTORABLE))})
         ORDER BY a.id DESC LIMIT 500""", tuple(sorted(RESTORABLE))).fetchall()
    out, seen = [], set()
    for r in rows:
        key = (r["table_name"], r["row_id"])
        if key in seen:
            continue
        seen.add(key)
        if conn.execute(f"SELECT 1 FROM {r['table_name']} WHERE id = ?", (r["row_id"],)).fetchone():
            continue  # restored (or the id was reused)
        old = _load(r["old_data"])
        out.append({"audit_id": r["id"], "table": r["table_name"], "row_id": r["row_id"], "at": r["at"],
                    "label": _label(r["table_name"], old)})
        if len(out) >= limit:
            break
    return out


def restore_deleted(conn, audit_id: int) -> dict:
    a = repo.one(conn, "SELECT * FROM audit_log WHERE id = ?", (audit_id,))
    if not a or a["action"] != "DELETE" or not a["old_data"]:
        raise NotFound(f"no restorable delete with audit id {audit_id}")
    table = a["table_name"]
    if table not in RESTORABLE:
        raise ValidationError({"table": f"{table} rows can't be restored"})
    old = _load(a["old_data"])
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    values = {k: v for k, v in old.items() if k in cols}
    notes = []
    with transaction(conn):
        if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (values["id"],)).fetchone():
            raise ValidationError({"_": "this row already exists (was it restored already?)"})
        for col, ref_table in FOREIGN_KEYS.get(table, {}).items():
            if values.get(col) and not conn.execute(f"SELECT 1 FROM {ref_table} WHERE id = ?", (values[col],)).fetchone():
                values[col] = None
                notes.append(f"{col} cleared (the {ref_table[:-1]} no longer exists)")
        if table == "section_records" and not conn.execute(
                "SELECT 1 FROM sections WHERE id = ?", (values["section_id"],)).fetchone():
            raise ValidationError({"_": "its section has been deleted"})
        if table == "codes" and conn.execute("SELECT 1 FROM codes WHERE kind = ? AND payload = ?",
                                             (values["kind"], values["payload"])).fetchone():
            raise ValidationError({"_": "an identical code already exists"})
        repo._insert(conn, table, values)
        if table == "contacts" and old.get("tags"):
            tags = _load(old["tags"]) if isinstance(old["tags"], str) else old["tags"]
            repo._set_contact_tags(conn, values["id"], tags)
        if table == "notes" and old.get("folders"):  # put it back in the folders it was filed in
            folder_ids = _load(old["folders"]) if isinstance(old["folders"], str) else old["folders"]
            for fid in folder_ids or []:
                if conn.execute("SELECT 1 FROM folders WHERE id = ?", (fid,)).fetchone():
                    conn.execute("INSERT OR IGNORE INTO folder_items (folder_id, item_type, item_id) VALUES (?, 'note', ?)",
                                 (fid, values["id"]))
                else:
                    notes.append(f"folder {fid} no longer exists")
    return {"table": table, "id": values["id"], "notes": notes}


def prune(conn, keep_days: int) -> int:
    if int(keep_days) < 1:
        raise ValidationError({"days": "must be at least 1"})
    with transaction(conn):
        return conn.execute("DELETE FROM audit_log WHERE at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
                            (f"-{int(keep_days)} days",)).rowcount
