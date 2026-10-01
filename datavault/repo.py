"""Data-access layer. All SQL that the app runs on behalf of a user lives here,
always parameterised; identifiers that can't be bound (sort columns, JSON
paths) are checked against an allow-list first."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Iterable

from datetime import date

from . import formulas as fx
from . import validation as v
from .db import transaction
from .validation import NotFound, ValidationError


def rows(cur: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in cur]


def one(conn, sql: str, params=()) -> dict | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def _insert(conn, table: str, values: dict) -> int:
    cols = ", ".join(values)
    marks = ", ".join(f":{k}" for k in values)
    return conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", values).lastrowid


def _update(conn, table: str, row_id: int, values: dict) -> None:
    if not values:
        return
    sets = ", ".join(f"{k} = :{k}" for k in values)
    cur = conn.execute(f"UPDATE {table} SET {sets} WHERE id = :_id", {**values, "_id": row_id})
    if cur.rowcount == 0:
        raise NotFound(f"{table} {row_id} not found")


def _delete(conn, table: str, row_id: int) -> None:
    if conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,)).rowcount == 0:
        raise NotFound(f"{table} {row_id} not found")


def _require(conn, table: str, row_id: int | None, field: str) -> None:
    if row_id is not None and not conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)).fetchone():
        raise ValidationError({field: f"refers to a missing {table[:-1]} (id {row_id})"})


def _page(limit, offset, max_limit=500) -> tuple[int, int]:
    try:
        limit = max(1, min(int(limit or 50), max_limit))
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        raise ValidationError("limit/offset must be integers") from None
    return limit, offset


def _filter_value(field: str, coerce, raw):
    """Coerce a query-string filter, reporting bad input as a 400 not a 500."""
    try:
        return coerce(raw)
    except ValueError as e:
        raise ValidationError({field: str(e)}) from None


def _dumps(data: dict) -> str:
    # ensure_ascii=False keeps "Zoë" as Zoë in the stored JSON, so LIKE and
    # json_extract comparisons work on the real characters (not \u00eb)
    return json.dumps(data, ensure_ascii=False)


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# =================================================================== tags

def list_tags(conn) -> list[dict]:
    return rows(conn.execute("""
        SELECT t.*, COUNT(ct.contact_id) AS contact_count
          FROM tags t LEFT JOIN contact_tags ct ON ct.tag_id = t.id
         GROUP BY t.id ORDER BY t.name COLLATE NOCASE"""))


def upsert_tag(conn, name: str, color: str | None = None) -> int:
    clean = v.TAG.clean({"name": name, **({"color": color} if color else {})})
    # ON CONFLICT ... DO UPDATE ... RETURNING gives us the id for both paths in one statement
    return conn.execute("""
        INSERT INTO tags (name, color) VALUES (:name, COALESCE(:color, '#6b7280'))
        ON CONFLICT(name) DO UPDATE SET color = COALESCE(:color, tags.color)
        RETURNING id""", {"name": clean["name"], "color": clean.get("color") if color else None}).fetchone()[0]


def delete_tag(conn, tag_id: int) -> None:
    _delete(conn, "tags", tag_id)


def _set_contact_tags(conn, contact_id: int, tags: Any) -> None:
    if tags is None:
        tags = []
    if isinstance(tags, str):
        tags = re.split(r"[;,]", tags)
    if not isinstance(tags, list):
        raise ValidationError({"tags": "must be a list or a comma-separated string"})
    names = [str(t.get("name") if isinstance(t, dict) else t).strip() for t in tags]
    names = list(dict.fromkeys(n for n in names if n))  # de-dupe, keep order
    if len(names) > 50:
        raise ValidationError({"tags": "at most 50 tags per contact"})
    conn.execute("DELETE FROM contact_tags WHERE contact_id = ?", (contact_id,))
    for n in names:
        try:
            tag_id = upsert_tag(conn, n)
        except ValidationError as e:
            raise ValidationError({"tags": f"{n[:20]!r} {next(iter(e.errors.values()))}"}) from None
        conn.execute("INSERT OR IGNORE INTO contact_tags (contact_id, tag_id) VALUES (?, ?)", (contact_id, tag_id))


# =============================================================== contacts

CONTACT_SORTS = {
    "name": "last_name COLLATE NOCASE, first_name COLLATE NOCASE",
    "first": "first_name COLLATE NOCASE, last_name COLLATE NOCASE",
    "company": "company COLLATE NOCASE, last_name COLLATE NOCASE",
    "recent": "updated_at DESC",
    "created": "created_at DESC",
}


def _contact_out(d: dict) -> dict:
    d["tags"] = json.loads(d.pop("tags_json") or "[]")
    return d


def list_contacts(conn, *, q: str = "", tag: str = "", favorite: Any = None,
                  sort: str = "name", limit=50, offset=0) -> dict:
    limit, offset = _page(limit, offset)
    where, params = [], {}
    if q:
        where.append("""(full_name LIKE :q ESCAPE '\\' OR company LIKE :q ESCAPE '\\'
                        OR email LIKE :q ESCAPE '\\' OR phone LIKE :q ESCAPE '\\' OR notes LIKE :q ESCAPE '\\')""")
        params["q"] = _like(q)
    if tag:
        where.append("id IN (SELECT ct.contact_id FROM contact_tags ct JOIN tags t ON t.id = ct.tag_id WHERE t.name = :tag)")
        params["tag"] = tag
    if favorite not in (None, ""):
        where.append("favorite = :fav")
        params["fav"] = _filter_value("favorite", v.boolean, favorite)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    order = CONTACT_SORTS.get(sort)
    if order is None:
        raise ValidationError({"sort": f"must be one of {', '.join(CONTACT_SORTS)}"})
    total = conn.execute(f"SELECT COUNT(*) FROM v_contacts {w}", params).fetchone()[0]
    items = rows(conn.execute(f"SELECT * FROM v_contacts {w} ORDER BY favorite DESC, {order} LIMIT {limit} OFFSET {offset}", params))
    return {"items": [_contact_out(i) for i in items], "total": total, "limit": limit, "offset": offset}


def get_contact(conn, contact_id: int) -> dict:
    d = one(conn, "SELECT * FROM v_contacts WHERE id = ?", (contact_id,))
    if not d:
        raise NotFound(f"contact {contact_id} not found")
    d = _contact_out(d)
    d["expenses"] = one(conn, """SELECT COUNT(*) AS count, COALESCE(SUM(amount_cents), 0) AS total_cents
                                   FROM expenses WHERE contact_id = ?""", (contact_id,))
    return d


def create_contact(conn, data: dict) -> dict:
    clean = v.CONTACT.clean(data)
    with transaction(conn):
        _require(conn, "pictures", clean.get("photo_id"), "photo_id")
        cid = _insert(conn, "contacts", clean)
        if "tags" in data:
            _set_contact_tags(conn, cid, data["tags"])
    return get_contact(conn, cid)


def update_contact(conn, contact_id: int, data: dict) -> dict:
    clean = v.CONTACT.clean(data, partial=True)
    with transaction(conn):
        _require(conn, "pictures", clean.get("photo_id"), "photo_id")
        if clean:
            _update(conn, "contacts", contact_id, clean)
        else:
            get_contact(conn, contact_id)
        if "tags" in data:
            _set_contact_tags(conn, contact_id, data["tags"])
    return get_contact(conn, contact_id)


def delete_contact(conn, contact_id: int) -> None:
    with transaction(conn):
        _delete(conn, "contacts", contact_id)


def find_duplicate_contacts(conn) -> list[dict]:
    """Groups of contacts sharing an email, phone digits, or full name — a
    self-join a real CRM would run before a merge."""
    return rows(conn.execute("""
        WITH keyed AS (
            SELECT id, 'email' AS match_on, lower(email) AS k FROM contacts WHERE email <> ''
            UNION ALL
            SELECT id, 'phone', replace(replace(replace(replace(replace(phone, ' ', ''), '-', ''), '(', ''), ')', ''), '.', '')
              FROM contacts WHERE length(phone) >= 7
            UNION ALL
            SELECT id, 'name', lower(trim(first_name || ' ' || last_name)) FROM contacts
        )
        SELECT match_on, k AS value, COUNT(*) AS n, json_group_array(id) AS ids
          FROM keyed GROUP BY match_on, k HAVING COUNT(*) > 1 ORDER BY n DESC"""))


# =============================================================== expenses

EXPENSE_SORTS = {
    "date": "spent_on DESC, id DESC", "date_asc": "spent_on ASC, id ASC",
    "amount": "amount_cents DESC", "amount_asc": "amount_cents ASC",
    "merchant": "merchant COLLATE NOCASE, spent_on DESC", "merchant_desc": "merchant COLLATE NOCASE DESC, spent_on DESC",
}


def _expense_clean(data: dict, partial=False) -> dict:
    data = dict(data)
    if "amount" in data:  # API/CSV speak dollars; the column is cents
        data["amount_cents"] = data.pop("amount")
    elif isinstance(data.get("amount_cents"), int) and not isinstance(data["amount_cents"], bool):
        # exact: integer cents -> "123.45" without a float round-trip
        c = data["amount_cents"]
        data["amount_cents"] = f"{'-' if c < 0 else ''}{abs(c) // 100}.{abs(c) % 100:02d}"
    for k in ("category", "contact_name", "amount_cents_display", "month", "category_color"):
        data.pop(k, None)
    return v.EXPENSE.clean(data, partial=partial)


def _expense_refs(conn, clean: dict) -> None:
    _require(conn, "expense_categories", clean.get("category_id"), "category_id")
    _require(conn, "contacts", clean.get("contact_id"), "contact_id")
    _require(conn, "pictures", clean.get("receipt_picture_id"), "receipt_picture_id")


def list_expenses(conn, *, q="", category_id=None, date_from=None, date_to=None,
                  min_amount=None, max_amount=None, contact_id=None, sort="date", limit=100, offset=0) -> dict:
    limit, offset = _page(limit, offset, 1000)
    where, params = [], {}
    if q:
        where.append("(merchant LIKE :q ESCAPE '\\' OR description LIKE :q ESCAPE '\\' OR payment_method LIKE :q ESCAPE '\\')")
        params["q"] = _like(q)
    if category_id not in (None, ""):
        if str(category_id) == "none":
            where.append("category_id IS NULL")
        else:
            where.append("category_id = :cat")
            params["cat"] = _filter_value("category_id", v.fk, category_id)
    if contact_id not in (None, ""):
        where.append("contact_id = :cid")
        params["cid"] = _filter_value("contact_id", v.fk, contact_id)
    try:
        if date_from:
            where.append("spent_on >= :df")
            params["df"] = v.iso_date(date_from)
        if date_to:
            where.append("spent_on <= :dt")
            params["dt"] = v.iso_date(date_to)
        if min_amount not in (None, ""):
            where.append("amount_cents >= :mn")
            params["mn"] = v.cents(min_amount)
        if max_amount not in (None, ""):
            where.append("amount_cents <= :mx")
            params["mx"] = v.cents(max_amount)
    except ValueError as e:
        raise ValidationError(f"filter {e}") from None
    order = EXPENSE_SORTS.get(sort)
    if order is None:
        raise ValidationError({"sort": f"must be one of {', '.join(EXPENSE_SORTS)}"})
    w = ("WHERE " + " AND ".join(where)) if where else ""
    agg = one(conn, f"SELECT COUNT(*) AS total, COALESCE(SUM(amount_cents), 0) AS sum_cents FROM v_expenses {w}", params)
    items = rows(conn.execute(f"SELECT * FROM v_expenses {w} ORDER BY {order} LIMIT {limit} OFFSET {offset}", params))
    return {"items": items, "total": agg["total"], "sum_cents": agg["sum_cents"], "limit": limit, "offset": offset}


def get_expense(conn, expense_id: int) -> dict:
    d = one(conn, "SELECT * FROM v_expenses WHERE id = ?", (expense_id,))
    if not d:
        raise NotFound(f"expense {expense_id} not found")
    return d


def create_expense(conn, data: dict) -> dict:
    clean = _expense_clean(data)
    with transaction(conn):
        _expense_refs(conn, clean)
        eid = _insert(conn, "expenses", clean)
    return get_expense(conn, eid)


def update_expense(conn, expense_id: int, data: dict) -> dict:
    clean = _expense_clean(data, partial=True)
    with transaction(conn):
        _expense_refs(conn, clean)
        _update(conn, "expenses", expense_id, clean)
    return get_expense(conn, expense_id)


def delete_expense(conn, expense_id: int) -> None:
    with transaction(conn):
        _delete(conn, "expenses", expense_id)


def _month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return f"{month}-01", nxt.isoformat()


def expense_summary(conn, month: str | None = None, months: Any = 12) -> dict:
    """Dashboard numbers. Month filters are written as half-open date ranges
    (spent_on >= '2026-03-01' AND spent_on < '2026-04-01') rather than
    substr(spent_on, 1, 7) = '2026-03', so SQLite can use idx_expenses_date."""
    if month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValidationError({"month": "must be YYYY-MM"})
    month = month or conn.execute("SELECT strftime('%Y-%m', 'now', 'localtime')").fetchone()[0]
    try:
        months = max(1, min(int(months), 60))
    except (TypeError, ValueError):
        raise ValidationError({"months": "must be an integer"}) from None
    start, end = _month_bounds(month)
    trend = rows(conn.execute("""
        WITH RECURSIVE m(start, i) AS (
            SELECT :start, 0
            UNION ALL
            SELECT date(start, '-1 month'), i + 1 FROM m WHERE i + 1 < :n
        )
        SELECT substr(m.start, 1, 7) AS month,
               (SELECT COALESCE(SUM(amount_cents), 0) FROM expenses
                 WHERE spent_on >= m.start AND spent_on < date(m.start, '+1 month')) AS total_cents,
               (SELECT COUNT(*) FROM expenses
                 WHERE spent_on >= m.start AND spent_on < date(m.start, '+1 month')) AS n
          FROM m ORDER BY m.start""", {"start": start, "n": months}))
    by_category = rows(conn.execute("""
        SELECT COALESCE(c.id, 0) AS category_id, COALESCE(c.name, 'Uncategorized') AS category,
               COALESCE(c.color, '#9ca3af') AS color, c.monthly_budget_cents AS budget_cents,
               COALESCE(SUM(e.amount_cents), 0) AS total_cents, COUNT(e.id) AS n
          FROM expense_categories c
          FULL OUTER JOIN (SELECT * FROM expenses WHERE spent_on >= :start AND spent_on < :end) e
                       ON e.category_id = c.id
         GROUP BY 1, 2, 3, 4
        HAVING total_cents > 0 OR budget_cents IS NOT NULL
         ORDER BY total_cents DESC, category""", {"start": start, "end": end}))
    top_merchants = rows(conn.execute("""
        SELECT merchant, COUNT(*) AS n, SUM(amount_cents) AS total_cents
          FROM expenses WHERE merchant <> '' AND spent_on >= date(:start, '-11 months') AND spent_on < :end
         GROUP BY merchant COLLATE NOCASE ORDER BY total_cents DESC LIMIT 8""", {"start": start, "end": end}))
    totals = one(conn, """
        SELECT COALESCE(SUM(amount_cents), 0) AS month_cents, COUNT(*) AS month_count,
               (SELECT COALESCE(SUM(amount_cents), 0) FROM expenses
                 WHERE spent_on >= date(:start, '-1 month') AND spent_on < :start) AS prev_month_cents,
               (SELECT COALESCE(SUM(monthly_budget_cents), 0) FROM expense_categories) AS budget_cents
          FROM expenses WHERE spent_on >= :start AND spent_on < :end""", {"start": start, "end": end})
    return {"month": month, "totals": totals, "trend": trend, "by_category": by_category, "top_merchants": top_merchants}


def list_categories(conn) -> list[dict]:
    return rows(conn.execute("""
        SELECT c.*, COUNT(e.id) AS expense_count FROM expense_categories c
          LEFT JOIN expenses e ON e.category_id = c.id GROUP BY c.id ORDER BY c.name COLLATE NOCASE"""))


def create_category(conn, data: dict) -> dict:
    data = dict(data)
    if "monthly_budget" in data:
        data["monthly_budget_cents"] = data.pop("monthly_budget")
    clean = v.CATEGORY.clean(data)
    with transaction(conn):
        try:
            cid = _insert(conn, "expense_categories", clean)
        except sqlite3.IntegrityError:
            raise ValidationError({"name": "already exists"}) from None
    return one(conn, "SELECT * FROM expense_categories WHERE id = ?", (cid,))


def update_category(conn, cat_id: int, data: dict) -> dict:
    data = dict(data)
    data.pop("expense_count", None)
    if "monthly_budget" in data:
        data["monthly_budget_cents"] = data.pop("monthly_budget")
    clean = v.CATEGORY.clean(data, partial=True)
    with transaction(conn):
        try:
            _update(conn, "expense_categories", cat_id, clean)
        except sqlite3.IntegrityError:
            raise ValidationError({"name": "already exists"}) from None
    return one(conn, "SELECT * FROM expense_categories WHERE id = ?", (cat_id,))


def delete_category(conn, cat_id: int) -> None:
    with transaction(conn):
        _delete(conn, "expense_categories", cat_id)


def category_id_for(conn, name: str, create: bool = True) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    r = conn.execute("SELECT id FROM expense_categories WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if r:
        return r[0]
    if not create:
        raise ValidationError({"category": f"unknown category {name!r}"})
    try:
        clean = v.CATEGORY.clean({"name": name})
    except ValidationError as e:
        raise ValidationError({"category": e.errors.get("name", str(e))}) from None
    return _insert(conn, "expense_categories", clean)


# =============================================================== pictures

def list_pictures(conn, *, q="", album="", limit=60, offset=0) -> dict:
    limit, offset = _page(limit, offset)
    where, params = [], {}
    if q:
        where.append("(title LIKE :q ESCAPE '\\' OR description LIKE :q ESCAPE '\\' OR original_name LIKE :q ESCAPE '\\')")
        params["q"] = _like(q)
    if album:
        where.append("album = :album")
        params["album"] = album
    w = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM pictures {w}", params).fetchone()[0]
    items = rows(conn.execute(
        f"SELECT * FROM pictures {w} ORDER BY COALESCE(taken_at, created_at) DESC, id DESC LIMIT {limit} OFFSET {offset}", params))
    albums = rows(conn.execute("SELECT album, COUNT(*) AS n FROM pictures WHERE album <> '' GROUP BY album ORDER BY album"))
    return {"items": items, "total": total, "albums": albums, "limit": limit, "offset": offset}


def get_picture(conn, pic_id: int) -> dict:
    d = one(conn, "SELECT * FROM pictures WHERE id = ?", (pic_id,))
    if not d:
        raise NotFound(f"picture {pic_id} not found")
    d["used_by"] = {
        "contacts": rows(conn.execute("SELECT id, first_name, last_name FROM contacts WHERE photo_id = ?", (pic_id,))),
        "expenses": rows(conn.execute("SELECT id, merchant, spent_on FROM expenses WHERE receipt_picture_id = ?", (pic_id,))),
    }
    return d


def update_picture(conn, pic_id: int, data: dict) -> dict:
    data = {k: data[k] for k in ("title", "description", "album", "taken_at") if k in data}
    clean = v.PICTURE_META.clean(data, partial=True)
    with transaction(conn):
        _update(conn, "pictures", pic_id, clean)
    return get_picture(conn, pic_id)


# =============================================================== sections

FIELD_TYPES = ("text", "longtext", "number", "money", "date", "boolean", "select",
               "url", "email", "picture", "contact", "code", "formula")
NUMERIC_RESULTS = ("number", "money")
RESERVED_KEYS = {"id", "section_id", "created_at", "updated_at"}


def _clean_fields(fields: Any) -> list[dict]:
    if not isinstance(fields, list) or not fields:
        raise ValidationError({"fields": "a section needs at least one field"})
    if len(fields) > 100:
        raise ValidationError({"fields": "at most 100 fields per section"})
    out, seen = [], set()
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            raise ValidationError({"fields": f"field #{i + 1} must be an object"})
        try:
            label = v.text(60)(f.get("label") or f.get("key") or "")
            required = v.boolean(f.get("required"))
        except ValueError as e:
            raise ValidationError({"fields": f"field #{i + 1}: {e}"}) from None
        if not label:
            raise ValidationError({"fields": f"field #{i + 1} needs a label"})
        key = v.field_key(str(f.get("key") or label))
        if key in RESERVED_KEYS:
            key = f"f_{key}"
        if key in seen:
            raise ValidationError({"fields": f"two fields would both be stored as {key!r} — rename one"})
        seen.add(key)
        ftype = f.get("type", "text")
        if ftype not in FIELD_TYPES:
            raise ValidationError({"fields": f"field {label!r}: unknown type {ftype!r}"})
        options: Any = f.get("options")
        if ftype == "formula":
            src = options if isinstance(options, dict) else {}
            expr = f.get("formula", src.get("expr", ""))
            result = f.get("result", src.get("result", "number"))
            options = {"expr": v.text(fx.MAX_EXPR_LEN)(expr), "result": result}
            required = 0  # computed, never typed in
        else:
            if isinstance(options, str):
                options = options.split(",")
            if not isinstance(options, list):
                options = []
            # select options are compared as strings, so store them as strings
            options = list(dict.fromkeys(str(o).strip()[:100] for o in options if str(o).strip()))[:200]
            if ftype == "select" and not options:
                raise ValidationError({"fields": f"field {label!r}: a choice list needs options"})
        out.append({"key": key, "label": label, "type": ftype, "required": required,
                    "options": options, "position": i})
    try:  # compile every formula against the final field list (catches typos and cycles)
        fx.compile_section(out)
    except fx.FormulaError as e:
        raise ValidationError({"fields": f"formula {e}"}) from None
    for f in out:
        f["options"] = json.dumps(f["options"], ensure_ascii=False)
    return out


def list_sections(conn) -> list[dict]:
    secs = rows(conn.execute("""
        SELECT s.*, (SELECT COUNT(*) FROM section_records r WHERE r.section_id = s.id) AS record_count
          FROM sections s ORDER BY s.name COLLATE NOCASE"""))
    fields = {}
    for f in _all_fields(conn):  # one query for every section's fields, not one per section
        fields.setdefault(f["section_id"], []).append(f)
    for s in secs:
        s["fields"] = fields.get(s["id"], [])
    return secs


def _decode_field(f: dict) -> dict:
    f["options"] = json.loads(f["options"])
    f["required"] = bool(f["required"])
    return f


def _all_fields(conn) -> list[dict]:
    return [_decode_field(f) for f in rows(conn.execute("SELECT * FROM section_fields ORDER BY section_id, position, id"))]


def _fields(conn, section_id: int) -> list[dict]:
    return [_decode_field(f) for f in rows(conn.execute(
        "SELECT * FROM section_fields WHERE section_id = ? ORDER BY position, id", (section_id,)))]


def get_section(conn, ref: int | str) -> dict:
    col = "id" if str(ref).isdigit() else "slug"
    s = one(conn, f"SELECT * FROM sections WHERE {col} = ?", (ref,))
    if not s:
        raise NotFound(f"section {ref} not found")
    s["fields"] = _fields(conn, s["id"])
    s["record_count"] = conn.execute("SELECT COUNT(*) FROM section_records WHERE section_id = ?", (s["id"],)).fetchone()[0]
    return s


def _section_meta(data: dict, existing: dict | None = None) -> dict:
    meta = {}
    try:
        if existing is None or "name" in data:
            meta["name"] = v.text(80)(data.get("name")) or (existing or {}).get("name", "")
        if existing is None or "icon" in data:
            meta["icon"] = v.text(8)(data.get("icon")) or "📁"
        if existing is None or "description" in data:
            meta["description"] = v.text(1000)(data.get("description"))
    except ValueError as e:
        raise ValidationError({"name": str(e)}) from None
    if "name" in meta and not meta["name"]:
        raise ValidationError({"name": "is required"})
    return meta


def create_section(conn, data: dict) -> dict:
    meta = _section_meta(data)
    fields = _clean_fields(data.get("fields"))
    slug = v.slugify(str(data.get("slug") or meta["name"]))
    with transaction(conn):
        base, n = slug, 2
        while conn.execute("SELECT 1 FROM sections WHERE slug = ?", (slug,)).fetchone():
            slug, n = f"{base}-{n}", n + 1
        sid = _insert(conn, "sections", {**meta, "slug": slug})
        for f in fields:
            _insert(conn, "section_fields", {**f, "section_id": sid})
    return get_section(conn, sid)


def update_section(conn, section_id: int, data: dict) -> dict:
    """Rename/re-describe a section and replace its field list. Fields keep
    their key, so existing record values survive a re-order or relabel;
    removing a field leaves its old values in the JSON (recoverable) but
    hides them. Formulas are recomputed for every record."""
    sec = get_section(conn, section_id)
    meta = _section_meta(data, sec)
    fields = _clean_fields(data["fields"]) if "fields" in data else None
    with transaction(conn):
        if meta:
            meta["updated_at"] = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()[0]
            _update(conn, "sections", sec["id"], meta)
        if fields is not None:
            conn.execute("DELETE FROM section_fields WHERE section_id = ?", (sec["id"],))
            for f in fields:
                _insert(conn, "section_fields", {**f, "section_id": sec["id"]})
            recalculate_section(conn, sec["id"])
    return get_section(conn, sec["id"])


def delete_section(conn, section_id: int) -> None:
    with transaction(conn):
        _delete(conn, "sections", section_id)


def recalculate_section(conn, section_id: int) -> int:
    """Recompute every formula in a section. Returns the number of records
    whose stored values changed."""
    fields = _fields(conn, section_id)
    compiled = fx.compile_section(fields)
    changed = 0
    with transaction(conn):
        if compiled:
            for r in conn.execute("SELECT id, data FROM section_records WHERE section_id = ?", (section_id,)).fetchall():
                old = json.loads(r["data"])
                new = fx.apply(fields, old, compiled)
                if new != old:
                    conn.execute("UPDATE section_records SET data = ? WHERE id = ?", (_dumps(new), r["id"]))
                    changed += 1
        conn.execute("UPDATE sections SET recalculated_on = date('now', 'localtime') WHERE id = ?", (section_id,))
    return changed


def _refresh_volatile(conn, sec: dict) -> None:
    """Formulas using TODAY() go stale overnight; refresh them once per day."""
    if not any(f["type"] == "formula" and "TODAY" in f["options"].get("expr", "").upper() for f in sec["fields"]):
        return
    today = conn.execute("SELECT date('now', 'localtime')").fetchone()[0]
    if sec.get("recalculated_on") != today:
        recalculate_section(conn, sec["id"])
        sec["recalculated_on"] = today


def _coerce_value(conn, f: dict, raw: Any) -> Any:
    t = f["type"]
    if raw in (None, "") and t != "boolean":
        return None
    if t in ("text",):
        return v.text(1000)(raw)
    if t == "longtext":
        return v.text(20000)(raw)
    if t == "number":
        return v.number(raw)
    if t == "money":
        return v.cents(raw)
    if t == "date":
        return v.iso_date(raw)
    if t == "boolean":
        return bool(v.boolean(raw))
    if t == "url":
        return v.url(raw)
    if t == "email":
        return v.email(raw)
    if t == "select":
        s = str(v._scalar(raw)).strip()
        if s not in f["options"]:
            raise ValueError(f"must be one of: {', '.join(f['options'])}")
        return s
    if t in ("picture", "contact", "code"):
        ref = v.fk(raw)
        table = {"picture": "pictures", "contact": "contacts", "code": "codes"}[t]
        if ref and not conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (ref,)).fetchone():
            raise ValueError(f"refers to a missing {t} (id {ref})")
        return ref
    if t == "formula":
        raise ValueError("is calculated by its formula and can't be set directly")
    raise ValueError(f"unsupported type {t}")


def _clean_record(conn, section: dict, data: dict, existing: dict | None = None) -> dict:
    values = data["data"] if isinstance(data.get("data"), dict) else data
    out = dict(existing or {})
    errors = {}
    keys = {f["key"] for f in section["fields"]}
    for f in section["fields"]:
        if f["type"] == "formula":
            continue  # computed below; any submitted value is ignored
        if f["key"] not in values:
            if existing is None and f["required"]:
                errors[f["key"]] = "is required"
            continue
        try:
            val = _coerce_value(conn, f, values[f["key"]])
        except ValueError as e:
            errors[f["key"]] = str(e)
            continue
        if f["required"] and val in (None, ""):
            errors[f["key"]] = "is required"
        out[f["key"]] = val
    unknown = set(values) - keys - RESERVED_KEYS
    if unknown:
        errors["_"] = f"unknown field(s): {', '.join(sorted(map(str, unknown)))}"
    if errors:
        raise ValidationError(errors)
    try:
        return fx.apply(section["fields"], out)
    except fx.FormulaError as e:  # a stored formula no longer compiles
        raise ValidationError({"_": f"formula {e}"}) from None


def _record_out(r: dict) -> dict:
    r["data"] = json.loads(r["data"])
    return r


def _numeric_keys(sec: dict) -> list[str]:
    return [f["key"] for f in sec["fields"]
            if f["type"] in NUMERIC_RESULTS
            or (f["type"] == "formula" and f["options"].get("result") in NUMERIC_RESULTS)]


def list_records(conn, section_ref, *, q="", sort="", desc=False, filters: dict | None = None,
                 limit=100, offset=0) -> dict:
    sec = get_section(conn, section_ref)
    _refresh_volatile(conn, sec)
    limit, offset = _page(limit, offset, 1000)
    keys = {f["key"]: f for f in sec["fields"]}
    where, params = ["section_id = :sid"], {"sid": sec["id"]}
    if q:
        # search values only (not the JSON keys), via the json_each table-valued function
        where.append("EXISTS (SELECT 1 FROM json_each(section_records.data) j WHERE j.value LIKE :q ESCAPE '\\')")
        params["q"] = _like(q)
    for i, (k, val) in enumerate((filters or {}).items()):
        if k not in keys:
            raise ValidationError({"filter": f"unknown field {k!r}"})
        f = keys[k]
        if f["type"] == "formula":
            raise ValidationError({"filter": f"can't filter on formula field {k!r}"})
        # key is allow-listed above, so it is safe to place in the JSON path
        where.append(f"json_extract(data, '$.{k}') = :f{i}")
        val = _filter_value(k, lambda raw, f=f: _coerce_value(conn, f, raw), val)
        params[f"f{i}"] = int(val) if isinstance(val, bool) else val
    if sort and sort not in keys and sort not in ("created_at", "updated_at"):
        raise ValidationError({"sort": f"unknown field {sort!r}"})
    order = (f"json_extract(data, '$.{sort}')" if sort in keys else sort) if sort else "id"
    direction = "DESC" if desc or not sort else "ASC"
    w = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM section_records WHERE {w}", params).fetchone()[0]
    items = rows(conn.execute(
        f"SELECT * FROM section_records WHERE {w} ORDER BY {order} {direction} NULLS LAST, id DESC "
        f"LIMIT {limit} OFFSET {offset}", params))
    totals = {}
    num_keys = _numeric_keys(sec)
    if num_keys and total:
        # column totals over the whole filtered set (not just this page), in one pass
        sums = ", ".join(f"SUM(json_extract(data, '$.{k}')) AS \"{k}\"" for k in num_keys)
        totals = dict(conn.execute(f"SELECT {sums} FROM section_records WHERE {w}", params).fetchone())
    return {"section": sec, "items": [_record_out(r) for r in items], "total": total, "totals": totals,
            "limit": limit, "offset": offset}


def get_record(conn, record_id: int) -> dict:
    r = one(conn, "SELECT * FROM section_records WHERE id = ?", (record_id,))
    if not r:
        raise NotFound(f"record {record_id} not found")
    return _record_out(r)


def create_record(conn, section_ref, data: dict) -> dict:
    sec = get_section(conn, section_ref)
    clean = _clean_record(conn, sec, data)
    with transaction(conn):
        rid = _insert(conn, "section_records", {"section_id": sec["id"], "data": _dumps(clean)})
    return get_record(conn, rid)


def update_record(conn, record_id: int, data: dict) -> dict:
    rec = get_record(conn, record_id)
    sec = get_section(conn, rec["section_id"])
    clean = _clean_record(conn, sec, data, existing=rec["data"])
    with transaction(conn):
        _update(conn, "section_records", record_id, {"data": _dumps(clean)})
    return get_record(conn, record_id)


def delete_record(conn, record_id: int) -> None:
    with transaction(conn):
        _delete(conn, "section_records", record_id)


# ====================================================== search / insights

def _fts_query(q: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression: every word becomes a
    quoted prefix term, so user input can't inject FTS operators."""
    words = re.findall(r"[\w@.+-]+", q, re.UNICODE)
    return " ".join('"' + w.replace('"', '""') + '"*' for w in words[:12])


def search(conn, q: str, limit: int = 30, kind: str = "") -> list[dict]:
    match = _fts_query(q)
    if not match:
        return []
    params = {"m": match, "lim": max(1, min(_filter_value("limit", int, limit), 200))}
    kind_sql = ""
    if kind:
        kind_sql, params["kind"] = "AND kind = :kind", kind
    return rows(conn.execute(f"""
        SELECT kind, CAST(ref_id AS INTEGER) AS ref_id, title,
               snippet(search_index, 3, '<mark>', '</mark>', '…', 12) AS snippet,
               round(bm25(search_index, 0, 0, 10.0, 1.0), 3) AS score
          FROM search_index WHERE search_index MATCH :m {kind_sql}
         ORDER BY score LIMIT :lim""", params))


def stats(conn) -> dict:
    counts = one(conn, """
        SELECT (SELECT COUNT(*) FROM contacts) AS contacts, (SELECT COUNT(*) FROM pictures) AS pictures,
               (SELECT COUNT(*) FROM expenses) AS expenses, (SELECT COUNT(*) FROM codes) AS codes,
               (SELECT COUNT(*) FROM sections) AS sections, (SELECT COUNT(*) FROM section_records) AS records,
               (SELECT COUNT(*) FROM tags) AS tags, (SELECT COALESCE(SUM(size_bytes), 0) FROM pictures) AS media_bytes""")
    page = conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0]
    activity = rows(conn.execute("""
        SELECT a.*, CASE a.table_name
            WHEN 'contacts' THEN (SELECT trim(first_name || ' ' || last_name) FROM contacts WHERE id = a.row_id)
            WHEN 'expenses' THEN (SELECT merchant || ' · ' || printf('$%.2f', amount_cents / 100.0) FROM expenses WHERE id = a.row_id)
            WHEN 'pictures' THEN (SELECT COALESCE(NULLIF(title, ''), original_name) FROM pictures WHERE id = a.row_id)
            WHEN 'codes' THEN (SELECT COALESCE(NULLIF(label, ''), payload) FROM codes WHERE id = a.row_id)
            WHEN 'sections' THEN (SELECT name FROM sections WHERE id = a.row_id)
            WHEN 'section_records' THEN (SELECT s.name FROM section_records r JOIN sections s ON s.id = r.section_id WHERE r.id = a.row_id)
          END AS label
          FROM audit_log a
         WHERE a.table_name IN ('contacts', 'expenses', 'pictures', 'codes', 'sections', 'section_records')
         ORDER BY a.id DESC LIMIT 15"""))
    # Birthdays are built as "Jan 1 of <year> + (month-1) months + (day-1) days"
    # so Feb 29 lands on Mar 1 in non-leap years instead of becoming NULL.
    upcoming = rows(conn.execute("""
        WITH t AS (SELECT date('now', 'localtime') AS today, CAST(strftime('%Y', 'now', 'localtime') AS INTEGER) AS y),
        b AS (
            SELECT c.id, trim(c.first_name || ' ' || c.last_name) AS name, c.birthday,
                   date(t.y || '-01-01', '+' || (CAST(substr(c.birthday, 6, 2) AS INTEGER) - 1) || ' months',
                        '+' || (CAST(substr(c.birthday, 9, 2) AS INTEGER) - 1) || ' days') AS this_year,
                   date((t.y + 1) || '-01-01', '+' || (CAST(substr(c.birthday, 6, 2) AS INTEGER) - 1) || ' months',
                        '+' || (CAST(substr(c.birthday, 9, 2) AS INTEGER) - 1) || ' days') AS next_year,
                   t.today
              FROM contacts c, t WHERE c.birthday IS NOT NULL
        )
        SELECT id, name, birthday,
               CAST(julianday(CASE WHEN this_year >= today THEN this_year ELSE next_year END) - julianday(today) AS INTEGER)
                   AS days_until
          FROM b WHERE days_until <= 30 ORDER BY days_until, name"""))
    return {"counts": counts, "db_bytes": page, "activity": activity, "upcoming_birthdays": upcoming,
            "schema_version": conn.execute("PRAGMA user_version").fetchone()[0]}


# ======================================================== saved queries

def list_saved_queries(conn) -> list[dict]:
    return rows(conn.execute("SELECT * FROM saved_queries ORDER BY name COLLATE NOCASE"))


def save_query(conn, data: dict) -> dict:
    name, sql = v.text(80)(data.get("name")), v.text(20000)(data.get("sql"))
    if not name or not sql:
        raise ValidationError("name and sql are required")
    with transaction(conn):
        conn.execute("""INSERT INTO saved_queries (name, sql, description) VALUES (?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET sql = excluded.sql, description = excluded.description""",
                     (name, sql, v.text(500)(data.get("description"))))
    return one(conn, "SELECT * FROM saved_queries WHERE name = ?", (name,))


def delete_saved_query(conn, qid: int) -> None:
    with transaction(conn):
        _delete(conn, "saved_queries", qid)
