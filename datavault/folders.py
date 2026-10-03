"""Folders holding any mix of items, free-form notes with user-defined
properties, and arbitrary file attachments (migration 0005)."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from pathlib import Path
from typing import Any

from . import validation as v
from .config import Config
from .db import transaction
from .repo import _delete, _dumps, _filter_value, _insert, _like, _page, _update, one, rows
from .validation import NotFound, ValidationError

ITEM_TYPES = {"contact": "contacts", "picture": "pictures", "expense": "expenses", "code": "codes",
              "record": "section_records", "note": "notes", "file": "files"}
PROPERTY_TYPES = ("text", "longtext", "number", "money", "date", "boolean", "url", "email", "phone")
MAX_PROPERTIES = 100
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_DEPTH = 20
# served inline; everything else is forced to download (an uploaded .html or
# .svg must never render on our origin — that would be stored XSS)
INLINE_SAFE = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "text/plain",
               "audio/mpeg", "audio/ogg", "audio/wav", "video/mp4", "video/webm"}


def _check_item_type(t: Any) -> str:
    if t not in ITEM_TYPES:
        raise ValidationError({"item_type": f"must be one of {', '.join(ITEM_TYPES)}"})
    return t


# ============================================================ properties

def _clean_property_def(p: Any, i: int, *, with_value: bool) -> dict:
    """{name, type[, value]} -> validated. Names are free text (that's the
    point), types come from a fixed list so values can be validated."""
    if not isinstance(p, dict):
        raise ValidationError({"properties": f"field #{i + 1} must be an object"})
    try:
        name = v.text(60)(p.get("name"))
    except ValueError as e:
        raise ValidationError({"properties": f"field #{i + 1} name {e}"}) from None
    if not name:
        raise ValidationError({"properties": f"field #{i + 1} needs a name"})
    ptype = p.get("type") or "text"
    if ptype not in PROPERTY_TYPES:
        raise ValidationError({"properties": f"{name!r}: type must be one of {', '.join(PROPERTY_TYPES)}"})
    out = {"name": name, "type": ptype}
    if with_value:
        raw = p.get("value")
        try:
            out["value"] = _coerce_property(ptype, raw)
        except ValueError as e:
            raise ValidationError({"properties": f"{name!r} {e}"}) from None
    return out


def _coerce_property(ptype: str, raw: Any):
    if raw in (None, "") and ptype != "boolean":
        return None
    return {
        "text": v.text(1000), "longtext": v.text(20000), "number": v.number, "money": v.cents,
        "date": v.iso_date, "boolean": lambda x: bool(v.boolean(x)), "url": v.url, "email": v.email,
        "phone": v.phone,
    }[ptype](raw)


def clean_properties(props: Any, *, with_value: bool = True) -> list[dict]:
    if props in (None, ""):
        return []
    if not isinstance(props, list):
        raise ValidationError({"properties": "must be a list of {name, type, value}"})
    if len(props) > MAX_PROPERTIES:
        raise ValidationError({"properties": f"at most {MAX_PROPERTIES} fields"})
    out, seen = [], set()
    for i, p in enumerate(props):
        clean = _clean_property_def(p, i, with_value=with_value)
        key = clean["name"].lower()
        if key in seen:
            raise ValidationError({"properties": f"two fields are named {clean['name']!r}"})
        seen.add(key)
        out.append(clean)
    return out


# ================================================================ folders

def _folder_out(f: dict) -> dict:
    f["template"] = json.loads(f["template"])
    return f


def list_folders(conn) -> list[dict]:
    """Every folder with direct item/subfolder counts and a recursive total
    (items in the folder and all of its descendants, de-duplicated)."""
    folders = rows(conn.execute("""
        WITH RECURSIVE tree(root, id) AS (
            SELECT id, id FROM folders
            UNION ALL
            SELECT tree.root, f.id FROM folders f JOIN tree ON f.parent_id = tree.id
        )
        SELECT f.*,
               (SELECT COUNT(*) FROM folder_items fi WHERE fi.folder_id = f.id) AS item_count,
               (SELECT COUNT(*) FROM folders c WHERE c.parent_id = f.id) AS folder_count,
               (SELECT COUNT(*) FROM (SELECT DISTINCT fi.item_type, fi.item_id FROM tree
                                        JOIN folder_items fi ON fi.folder_id = tree.id WHERE tree.root = f.id)) AS total_items
          FROM folders f
         ORDER BY f.parent_id NULLS FIRST, f.position, f.name COLLATE NOCASE"""))
    return [_folder_out(f) for f in folders]


def _ancestors(conn, folder_id: int) -> list[dict]:
    return rows(conn.execute("""
        WITH RECURSIVE up(id, parent_id, name, icon, depth) AS (
            SELECT id, parent_id, name, icon, 0 FROM folders WHERE id = ?
            UNION ALL
            SELECT f.id, f.parent_id, f.name, f.icon, up.depth + 1 FROM folders f JOIN up ON f.id = up.parent_id
             WHERE up.depth < 100
        )
        SELECT id, name, icon FROM up ORDER BY depth DESC""", (folder_id,)))


def _descendant_ids(conn, folder_id: int) -> set[int]:
    return {r[0] for r in conn.execute("""
        WITH RECURSIVE down(id) AS (
            SELECT id FROM folders WHERE parent_id = ?
            UNION
            SELECT f.id FROM folders f JOIN down ON f.parent_id = down.id
        ) SELECT id FROM down""", (folder_id,))}


def _clean_folder(conn, data: dict, existing: dict | None = None) -> dict:
    out = {}
    try:
        if existing is None or "name" in data:
            out["name"] = v.text(80)(data.get("name"))
            if not out["name"]:
                raise ValidationError({"name": "is required"})
        if "icon" in data or existing is None:
            out["icon"] = v.text(8)(data.get("icon")) or "📁"
        if "color" in data or existing is None:
            out["color"] = v.color(data.get("color") or "#6366f1")
        if "description" in data or existing is None:
            out["description"] = v.text(2000)(data.get("description"))
    except ValueError as e:
        raise ValidationError({"name": str(e)}) from None
    if "template" in data or existing is None:
        out["template"] = json.dumps(clean_properties(data.get("template"), with_value=False), ensure_ascii=False)
    if "parent_id" in data:
        parent = _filter_value("parent_id", v.fk, data.get("parent_id"))
        if parent is not None:
            if not conn.execute("SELECT 1 FROM folders WHERE id = ?", (parent,)).fetchone():
                raise ValidationError({"parent_id": "that folder doesn't exist"})
            if existing and (parent == existing["id"] or parent in _descendant_ids(conn, existing["id"])):
                raise ValidationError({"parent_id": "a folder can't be moved inside itself"})
            if len(_ancestors(conn, parent)) >= MAX_DEPTH:
                raise ValidationError({"parent_id": f"folders can be nested at most {MAX_DEPTH} levels deep"})
        out["parent_id"] = parent
    return out


def create_folder(conn, data: dict) -> dict:
    clean = _clean_folder(conn, data)
    with transaction(conn):
        clean["position"] = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM folders WHERE parent_id IS ?", (clean.get("parent_id"),)).fetchone()[0]
        fid = _insert(conn, "folders", clean)
    return get_folder(conn, fid)


def update_folder(conn, folder_id: int, data: dict) -> dict:
    existing = get_folder(conn, folder_id, with_items=False)
    clean = _clean_folder(conn, data, existing)
    with transaction(conn):
        if clean:
            _update(conn, "folders", folder_id, clean)
    return get_folder(conn, folder_id)


def delete_folder(conn, folder_id: int) -> dict:
    """Deletes the folder and its subfolders. Items are never deleted — they
    only lose this membership (and stay reachable from their own pages)."""
    get_folder(conn, folder_id, with_items=False)
    sub = len(_descendant_ids(conn, folder_id))
    with transaction(conn):
        _delete(conn, "folders", folder_id)
    return {"deleted_subfolders": sub}


def get_folder(conn, folder_id: int, *, with_items: bool = True, item_type: str = "", q: str = "") -> dict:
    f = one(conn, "SELECT * FROM folders WHERE id = ?", (folder_id,))
    if not f:
        raise NotFound(f"folder {folder_id} not found")
    f = _folder_out(f)
    f["path"] = _ancestors(conn, folder_id)
    if not with_items:
        return f
    f["subfolders"] = [x for x in list_folders(conn) if x["parent_id"] == folder_id]
    links = rows(conn.execute(
        "SELECT item_type, item_id, position, added_at FROM folder_items WHERE folder_id = ? ORDER BY position, id",
        (folder_id,)))
    if item_type:
        links = [x for x in links if x["item_type"] == _check_item_type(item_type)]
    items = describe_items(conn, [(x["item_type"], x["item_id"]) for x in links])
    if q:
        needle = q.lower()
        items = [i for i in items if needle in (i["title"] + " " + i["subtitle"]).lower()]
    f["items"] = items
    f["type_counts"] = dict(conn.execute(
        "SELECT item_type, COUNT(*) FROM folder_items WHERE folder_id = ? GROUP BY item_type", (folder_id,)).fetchall())
    return f


# ============================================================ membership

def _require_item(conn, item_type: str, item_id: int) -> None:
    table = ITEM_TYPES[_check_item_type(item_type)]
    if not conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (item_id,)).fetchone():
        raise ValidationError({"item_id": f"{item_type} {item_id} doesn't exist"})


def add_items(conn, folder_id: int, items: Any) -> dict:
    """Add [{item_type, item_id}, …]; already-present items are skipped."""
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        raise ValidationError({"items": "give a list of {item_type, item_id}"})
    get_folder(conn, folder_id, with_items=False)
    added = 0
    with transaction(conn):
        pos = conn.execute("SELECT COALESCE(MAX(position), -1) FROM folder_items WHERE folder_id = ?",
                           (folder_id,)).fetchone()[0]
        for it in items:
            if not isinstance(it, dict):
                raise ValidationError({"items": "each item must be {item_type, item_id}"})
            t = _check_item_type(it.get("item_type"))
            iid = _filter_value("item_id", v.fk, it.get("item_id"))
            if iid is None:
                raise ValidationError({"item_id": "is required"})
            _require_item(conn, t, iid)
            pos += 1
            added += conn.execute("""INSERT INTO folder_items (folder_id, item_type, item_id, position)
                                     VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING""", (folder_id, t, iid, pos)).rowcount
    return {"added": added}


def remove_item(conn, folder_id: int, item_type: str, item_id: int) -> None:
    with transaction(conn):
        n = conn.execute("DELETE FROM folder_items WHERE folder_id = ? AND item_type = ? AND item_id = ?",
                         (folder_id, _check_item_type(item_type), item_id)).rowcount
    if not n:
        raise NotFound("that item isn't in this folder")


def move_item(conn, from_folder: int, to_folder: int, item_type: str, item_id: int) -> None:
    with transaction(conn):
        remove_item(conn, from_folder, item_type, item_id)
        add_items(conn, to_folder, [{"item_type": item_type, "item_id": item_id}])


def reorder_items(conn, folder_id: int, order: Any) -> None:
    if not isinstance(order, list):
        raise ValidationError({"order": "must be a list of {item_type, item_id}"})
    with transaction(conn):
        for pos, it in enumerate(order):
            if isinstance(it, dict):
                conn.execute("UPDATE folder_items SET position = ? WHERE folder_id = ? AND item_type = ? AND item_id = ?",
                             (pos, folder_id, it.get("item_type"), it.get("item_id")))


def folders_of(conn, item_type: str, item_id: int) -> list[dict]:
    return rows(conn.execute("""SELECT f.id, f.name, f.icon, f.color FROM folder_items fi JOIN folders f ON f.id = fi.folder_id
                                 WHERE fi.item_type = ? AND fi.item_id = ? ORDER BY f.name COLLATE NOCASE""",
                             (_check_item_type(item_type), item_id)))


def set_item_folders(conn, item_type: str, item_id: int, folder_ids: Any) -> list[dict]:
    """Replace an item's folder memberships in one go (from an item's own page)."""
    if not isinstance(folder_ids, list):
        raise ValidationError({"folder_ids": "must be a list"})
    ids = {_filter_value("folder_ids", v.fk, f) for f in folder_ids} - {None}
    _require_item(conn, item_type, item_id)
    with transaction(conn):
        for fid in ids:
            if not conn.execute("SELECT 1 FROM folders WHERE id = ?", (fid,)).fetchone():
                raise ValidationError({"folder_ids": f"folder {fid} doesn't exist"})
        current = {r[0] for r in conn.execute(
            "SELECT folder_id FROM folder_items WHERE item_type = ? AND item_id = ?", (item_type, item_id))}
        for fid in current - ids:
            conn.execute("DELETE FROM folder_items WHERE folder_id = ? AND item_type = ? AND item_id = ?",
                         (fid, item_type, item_id))
        for fid in ids - current:
            add_items(conn, fid, [{"item_type": item_type, "item_id": item_id}])
    return folders_of(conn, item_type, item_id)


# ========================================================= item summaries

def describe_items(conn, refs: list[tuple[str, int]]) -> list[dict]:
    """Resolve (type, id) pairs to display cards with one query per type
    (not one per item), preserving the input order."""
    by_type: dict[str, list[int]] = {}
    for t, i in refs:
        by_type.setdefault(t, []).append(i)
    found: dict[tuple[str, int], dict] = {}
    for t, ids in by_type.items():
        marks = ",".join("?" * len(ids))
        for card in _describe(conn, t, marks, ids):
            found[(t, card["id"])] = {"item_type": t, **card}
    return [found[r] for r in refs if r in found]


def _describe(conn, t: str, marks: str, ids: list[int]) -> list[dict]:
    if t == "contact":
        return [{"id": r["id"], "title": r["full_name"], "subtitle": r["company"] or r["email"] or r["phone"],
                 "thumb": f"/media/{r['photo_id']}/thumb" if r["photo_id"] else None, "href": f"#/contacts/{r['id']}"}
                for r in conn.execute(f"SELECT * FROM v_contacts WHERE id IN ({marks})", ids)]
    if t == "picture":
        return [{"id": r["id"], "title": r["title"] or r["original_name"], "subtitle": r["taken_at"] or r["created_at"][:10],
                 "thumb": f"/media/{r['id']}/thumb", "href": f"#/pictures?open={r['id']}"}
                for r in conn.execute(f"SELECT * FROM pictures WHERE id IN ({marks})", ids)]
    if t == "expense":
        return [{"id": r["id"], "title": f"{r['merchant'] or 'Expense'} · ${r['amount_cents'] / 100:,.2f}",
                 "subtitle": f"{r['spent_on']}{' · ' + r['category'] if r['category'] else ''}", "thumb":
                 f"/media/{r['receipt_picture_id']}/thumb" if r["receipt_picture_id"] else None,
                 "href": f"#/expenses?edit={r['id']}"}
                for r in conn.execute(f"SELECT * FROM v_expenses WHERE id IN ({marks})", ids)]
    if t == "code":
        return [{"id": r["id"], "title": r["label"] or r["payload"][:60], "subtitle": f"{r['kind'].upper()} · {r['payload'][:40]}",
                 "thumb": f"/api/codes/{r['id']}/image.png", "href": "#/codes"}
                for r in conn.execute(f"SELECT * FROM codes WHERE id IN ({marks})", ids)]
    if t == "record":
        out = []
        for r in conn.execute(f"""SELECT r.id, r.data, s.name, s.icon, s.slug FROM section_records r
                                   JOIN sections s ON s.id = r.section_id WHERE r.id IN ({marks})""", ids):
            data = json.loads(r["data"])
            first = next((str(x) for x in data.values() if x not in (None, "") and not isinstance(x, bool)), "")
            out.append({"id": r["id"], "title": first[:80] or f"{r['name']} record", "subtitle": f"{r['icon']} {r['name']}",
                        "thumb": None, "href": f"#/s/{r['slug']}?open={r['id']}"})
        return out
    if t == "note":
        out = []
        for r in conn.execute(f"SELECT * FROM notes WHERE id IN ({marks})", ids):
            props = json.loads(r["properties"])
            shown = [f"{p['name']}: {_fmt(p)}" for p in props if p.get("value") not in (None, "")][:3]
            out.append({"id": r["id"], "title": r["title"], "pinned": bool(r["pinned"]),
                        "subtitle": " · ".join(shown) or (r["body"].strip().splitlines() or [""])[0][:120],
                        "thumb": None, "href": f"#/notes/{r['id']}"})
        return out
    if t == "file":
        return [{"id": r["id"], "title": r["original_name"], "subtitle": f"{_human(r['size_bytes'])} · {r['mime']}",
                 "thumb": None, "href": f"/files/{r['id']}/download", "mime": r["mime"]}
                for r in conn.execute(f"SELECT * FROM files WHERE id IN ({marks})", ids)]
    return []


def _fmt(p: dict) -> str:
    val = p.get("value")
    if p["type"] == "money" and isinstance(val, int):
        return f"${val / 100:,.2f}"
    if p["type"] == "boolean":
        return "yes" if val else "no"
    return str(val)[:40]


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def lookup(conn, item_type: str, q: str = "", limit: Any = 30) -> list[dict]:
    """Item picker: search one type (FTS when there's a query, newest first
    otherwise)."""
    from .repo import search
    t = _check_item_type(item_type)
    limit, _ = _page(limit, 0, 100)
    if q.strip():
        refs = [(t, r["ref_id"]) for r in search(conn, q, limit, kind=t)]
    else:
        refs = [(t, r[0]) for r in conn.execute(f"SELECT id FROM {ITEM_TYPES[t]} ORDER BY id DESC LIMIT ?", (limit,))]
    return describe_items(conn, refs)


# ================================================================== notes

def _note_out(conn, n: dict) -> dict:
    n["properties"] = json.loads(n["properties"])
    n["pinned"] = bool(n["pinned"])
    n["folders"] = folders_of(conn, "note", n["id"])
    return n


def get_note(conn, note_id: int) -> dict:
    n = one(conn, "SELECT * FROM notes WHERE id = ?", (note_id,))
    if not n:
        raise NotFound(f"note {note_id} not found")
    return _note_out(conn, n)


def list_notes(conn, *, q: str = "", unfiled: Any = None, limit=200, offset=0) -> dict:
    limit, offset = _page(limit, offset, 1000)
    where, params = [], {}
    if q:
        where.append("(title LIKE :q ESCAPE '\\' OR body LIKE :q ESCAPE '\\' OR "
                     "EXISTS (SELECT 1 FROM json_each(notes.properties) p WHERE json_extract(p.value, '$.value') LIKE :q ESCAPE '\\'))")
        params["q"] = _like(q)
    if _filter_value("unfiled", v.boolean, unfiled) if unfiled not in (None, "") else False:
        where.append("NOT EXISTS (SELECT 1 FROM folder_items fi WHERE fi.item_type = 'note' AND fi.item_id = notes.id)")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM notes {w}", params).fetchone()[0]
    items = rows(conn.execute(f"SELECT * FROM notes {w} ORDER BY pinned DESC, updated_at DESC, id DESC "
                              f"LIMIT {limit} OFFSET {offset}", params))
    return {"items": [_note_out(conn, n) for n in items], "total": total}


def _clean_note(data: dict, partial: bool) -> dict:
    out = {}
    try:
        if not partial or "title" in data:
            out["title"] = v.text(200)(data.get("title"))
            if not out["title"]:
                raise ValidationError({"title": "is required"})
        if not partial or "body" in data:
            out["body"] = v.text(100_000)(data.get("body"))
        if "pinned" in data:
            out["pinned"] = v.boolean(data.get("pinned"))
    except ValueError as e:
        raise ValidationError({"body": str(e)}) from None
    if not partial or "properties" in data:
        out["properties"] = _dumps(clean_properties(data.get("properties")))  # type: ignore[arg-type]
    return out


def create_note(conn, data: dict) -> dict:
    """Create a note; with folder_id it's filed there and starts with that
    folder's field template (values the caller supplied win)."""
    folder_id = _filter_value("folder_id", v.fk, data.get("folder_id"))
    data = dict(data)
    if folder_id is not None:
        folder = get_folder(conn, folder_id, with_items=False)
        supplied = data.get("properties") or []
        if not isinstance(supplied, list):
            raise ValidationError({"properties": "must be a list of {name, type, value}"})
        given = {str(p.get("name", "")).lower() for p in supplied if isinstance(p, dict)}
        data["properties"] = supplied + [
            {**t, "value": None} for t in folder["template"] if t["name"].lower() not in given]
    clean = _clean_note(data, partial=False)
    with transaction(conn):
        nid = _insert(conn, "notes", clean)
        if folder_id is not None:
            add_items(conn, folder_id, [{"item_type": "note", "item_id": nid}])
    return get_note(conn, nid)


def update_note(conn, note_id: int, data: dict) -> dict:
    get_note(conn, note_id)
    clean = _clean_note(data, partial=True)
    with transaction(conn):
        if clean:
            _update(conn, "notes", note_id, clean)
        if "folder_ids" in data:
            set_item_folders(conn, "note", note_id, data["folder_ids"])
    return get_note(conn, note_id)


def delete_note(conn, note_id: int) -> None:
    with transaction(conn):
        _delete(conn, "notes", note_id)


# ================================================================== files

def file_path(cfg: Config, stored_name: str) -> Path:
    return cfg.files_dir / stored_name[:2] / stored_name


def _safe_name(name: str) -> str:
    base = Path(str(name or "")).name.strip() or "file"
    base = re.sub(r"[\x00-\x1f\x7f/\\]", "_", base)
    return base[:200]


def ingest_file(conn, cfg: Config, data: bytes, original_name: str, *, description: str = "",
                folder_id: Any = None) -> tuple[dict, bool]:
    if not data:
        raise ValidationError({"file": "is empty"})
    if len(data) > MAX_FILE_BYTES:
        raise ValidationError({"file": f"is larger than {MAX_FILE_BYTES // 1024 // 1024} MB"})
    folder_id = _filter_value("folder_id", v.fk, folder_id)
    if folder_id is not None:
        get_folder(conn, folder_id, with_items=False)
    name = _safe_name(original_name)
    # the browser-supplied type is untrusted; derive it from the extension instead
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    sha = hashlib.sha256(data).hexdigest()
    existing = conn.execute("SELECT id FROM files WHERE sha256 = ?", (sha,)).fetchone()
    created = existing is None
    if created:
        ext = re.sub(r"[^a-z0-9]", "", Path(name).suffix.lower())[:10]
        stored = f"{sha}.{ext}" if ext else sha
        path = file_path(cfg, stored)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        try:
            with transaction(conn):
                fid = _insert(conn, "files", {"sha256": sha, "stored_name": stored, "original_name": name, "mime": mime,
                                              "size_bytes": len(data), "description": v.text(2000)(description)})
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    else:
        fid = existing[0]
    if folder_id is not None:
        add_items(conn, folder_id, [{"item_type": "file", "item_id": fid}])
    return get_file(conn, fid), created


def get_file(conn, file_id: int) -> dict:
    f = one(conn, "SELECT * FROM files WHERE id = ?", (file_id,))
    if not f:
        raise NotFound(f"file {file_id} not found")
    f["folders"] = folders_of(conn, "file", file_id)
    f["inline"] = f["mime"] in INLINE_SAFE
    return f


def list_files(conn, *, q: str = "", limit=200, offset=0) -> dict:
    limit, offset = _page(limit, offset, 1000)
    params, w = {}, ""
    if q:
        w, params["q"] = "WHERE original_name LIKE :q ESCAPE '\\' OR description LIKE :q ESCAPE '\\'", _like(q)
    total = conn.execute(f"SELECT COUNT(*) FROM files {w}", params).fetchone()[0]
    items = rows(conn.execute(f"SELECT * FROM files {w} ORDER BY id DESC LIMIT {limit} OFFSET {offset}", params))
    return {"items": items, "total": total}


def update_file(conn, file_id: int, data: dict) -> dict:
    clean = {}
    try:
        if "original_name" in data:
            clean["original_name"] = _safe_name(data["original_name"])
        if "description" in data:
            clean["description"] = v.text(2000)(data["description"])
    except ValueError as e:
        raise ValidationError({"description": str(e)}) from None
    with transaction(conn):
        if clean:
            _update(conn, "files", file_id, clean)
        if "folder_ids" in data:
            set_item_folders(conn, "file", file_id, data["folder_ids"])
    return get_file(conn, file_id)


def delete_file(conn, cfg: Config, file_id: int) -> None:
    f = get_file(conn, file_id)
    with transaction(conn):
        _delete(conn, "files", file_id)
    file_path(cfg, f["stored_name"]).unlink(missing_ok=True)
