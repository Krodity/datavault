"""Moving data in and out: CSV import with header mapping + dry runs, CSV /
JSON / vCard export, and consistent online backups."""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import codes as codes_mod
from . import repo
from .config import Config
from .db import connect
from .validation import ValidationError

# --------------------------------------------------------------- CSV import

# Common header spellings (Google Contacts, Outlook, bank exports...) -> our field
ALIASES = {
    "contacts": {
        "first_name": ["first name", "firstname", "given name", "first"],
        "last_name": ["last name", "lastname", "surname", "family name", "last"],
        "company": ["company", "organization", "organisation", "organization 1 - name", "employer"],
        "job_title": ["job title", "title", "position", "organization 1 - title"],
        "email": ["email", "e-mail", "email address", "e-mail address", "e-mail 1 - value", "email 1"],
        "phone": ["phone", "mobile", "phone number", "mobile phone", "phone 1 - value", "telephone", "cell"],
        "address": ["address", "street", "address 1 - street", "street address"],
        "city": ["city", "address 1 - city", "town"],
        "region": ["state", "region", "province", "address 1 - region"],
        "postal_code": ["zip", "zip code", "postal code", "postcode", "address 1 - postal code"],
        "country": ["country", "address 1 - country"],
        "birthday": ["birthday", "birth date", "dob", "date of birth"],
        "website": ["website", "web page", "url", "website 1 - value"],
        "notes": ["notes", "note", "comments"],
        "tags": ["tags", "labels", "groups", "group membership", "categories"],
        "favorite": ["favorite", "favourite", "starred"],
        "_full_name": ["name", "full name", "display name"],
    },
    "expenses": {
        "spent_on": ["date", "spent on", "transaction date", "posted date", "posting date", "day"],
        "amount": ["amount", "total", "cost", "price", "debit", "value"],
        "currency": ["currency", "ccy"],
        "merchant": ["merchant", "payee", "vendor", "store", "name", "description 1"],
        "category": ["category", "type", "category name"],
        "payment_method": ["payment method", "method", "card", "account"],
        "description": ["description", "memo", "notes", "details"],
    },
    "codes": {
        "kind": ["kind", "type", "format", "symbology"],
        "payload": ["payload", "data", "value", "code", "barcode", "text", "content"],
        "label": ["label", "name", "title"],
        "notes": ["notes", "description"],
    },
}


def _norm(h: str) -> str:
    return re.sub(r"[\s_]+", " ", (h or "").strip().lower().lstrip("﻿"))


def aliases_for(conn, entity: str) -> dict[str, list[str]]:
    """Known fields + header spellings for an import target. Custom sections
    ("section:<slug>") derive theirs from the field labels, so a section's own
    CSV export re-imports without any manual mapping."""
    if entity.startswith("section:"):
        sec = repo.get_section(conn, entity.split(":", 1)[1])
        return {f["key"]: [_norm(f["label"]), _norm(f["key"])] for f in sec["fields"] if f["type"] != "formula"}
    if entity not in ALIASES:
        raise ValidationError({"entity": f"must be one of {', '.join(ALIASES)} or section:<slug>"})
    return ALIASES[entity]


def guess_mapping(entity: str, headers: list[str], aliases: dict | None = None) -> dict[str, str]:
    """header -> field, by exact field name first, then by known alias."""
    aliases = aliases or ALIASES[entity]
    mapping, used = {}, set()
    for h in headers:
        n = _norm(h)
        for field, names in aliases.items():
            if field in used:
                continue
            if n == _norm(field) or n in names:
                mapping[h] = field
                used.add(field)
                break
    return mapping


def _sniff(text: str) -> csv.Dialect:
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _check_mapping(entity: str, headers: list[str], mapping, aliases: dict) -> dict[str, str]:
    """A user-supplied mapping must be {csv header: known field}, one header per field."""
    if not isinstance(mapping, dict):
        raise ValidationError({"mapping": "must be an object of {CSV column: field}"})
    allowed = set(aliases)
    out, used = {}, set()
    for h, f in mapping.items():
        if not f:
            continue
        if h not in headers:
            raise ValidationError({"mapping": f"column {h!r} is not in the file"})
        if f not in allowed:
            raise ValidationError({"mapping": f"{f!r} is not a {entity} field"})
        if f in used:
            raise ValidationError({"mapping": f"two columns are mapped to {f!r}"})
        used.add(f)
        out[h] = f
    if not out:
        raise ValidationError({"mapping": "no columns are mapped"})
    return out


def import_csv(conn, entity: str, text: str, *, mapping: dict[str, str] | None = None,
               dry_run: bool = False, skip_errors: bool = False, create_categories: bool = True) -> dict:
    """Import rows atomically. On any invalid row the whole import is rolled
    back (unless skip_errors), and the report lists every bad row so the user
    can fix the file once rather than one error at a time."""
    aliases = aliases_for(conn, entity)
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), dialect=_sniff(text))
    headers = reader.fieldnames or []
    if not headers:
        raise ValidationError({"file": "has no header row"})
    mapping = (_check_mapping(entity, headers, mapping, aliases) if mapping
               else guess_mapping(entity, headers, aliases))
    if not mapping:
        raise ValidationError({"file": f"none of the columns look like {entity} fields — map them explicitly"})
    report = {"entity": entity, "mapping": mapping, "unmapped": [h for h in headers if h not in mapping],
              "total": 0, "imported": 0, "skipped": 0, "duplicates": 0, "errors": [], "dry_run": dry_run}

    commit = True
    conn.execute("BEGIN IMMEDIATE")
    # rows that existed before this import; used to skip re-imported expenses
    # without treating two identical coffees in the *same* file as duplicates
    ctx = {"max_expense_id": conn.execute("SELECT COALESCE(MAX(id), 0) FROM expenses").fetchone()[0]}
    if entity.startswith("section:"):
        ctx["section"] = repo.get_section(conn, entity.split(":", 1)[1])
    try:
        for line_no, raw in enumerate(reader, start=2):
            if not any((val or "").strip() for val in raw.values() if isinstance(val, str)):
                continue
            report["total"] += 1
            row = {mapping[h]: (raw.get(h) or "").strip() for h in mapping if mapping[h]}
            conn.execute("SAVEPOINT row")
            try:
                created = _import_row(conn, entity, row, create_categories, ctx)
                conn.execute("RELEASE row")
                report["imported" if created else "duplicates"] += 1
            except (ValidationError, sqlite3.IntegrityError) as e:
                conn.execute("ROLLBACK TO row")
                conn.execute("RELEASE row")
                errs = e.errors if isinstance(e, ValidationError) else {"_": str(e)}
                report["errors"].append({"line": line_no, "errors": errs})
                report["skipped"] += 1
        if (report["errors"] and not skip_errors) or dry_run:
            commit = False
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if commit:
        conn.execute("COMMIT")
    else:
        conn.execute("ROLLBACK")
        if not dry_run:
            report["imported"] = 0
            report["rolled_back"] = True
    report["errors"] = report["errors"][:200]
    return report


def _import_row(conn, entity: str, row: dict, create_categories: bool, ctx: dict) -> bool:
    if entity == "contacts":
        full = row.pop("_full_name", "")
        if full and not row.get("first_name"):
            first, _, last = full.partition(" ")
            row["first_name"] = first
            row["last_name"] = row.get("last_name") or last
        if row.get("email") and conn.execute("SELECT 1 FROM contacts WHERE email = ? COLLATE NOCASE",
                                             (row["email"].lower(),)).fetchone():
            return False
        tags = row.pop("tags", None)
        clean = repo.v.CONTACT.clean(row)
        cid = repo._insert(conn, "contacts", clean)
        if tags:
            repo._set_contact_tags(conn, cid, tags.replace(" ::: ", ","))
        return True
    if entity == "expenses":
        cat = row.pop("category", "")
        row["category_id"] = repo.category_id_for(conn, cat, create=create_categories)
        clean = repo._expense_clean(row)
        if conn.execute("""SELECT 1 FROM expenses WHERE id <= ? AND spent_on = ? AND amount_cents = ?
                             AND merchant = ? COLLATE NOCASE AND description = ? LIMIT 1""",
                        (ctx["max_expense_id"], clean["spent_on"], clean["amount_cents"],
                         clean.get("merchant", ""), clean.get("description", ""))).fetchone():
            return False  # already imported from an earlier run of the same statement
        repo._insert(conn, "expenses", clean)
        return True
    if entity == "codes":
        kind = codes_mod.normalize_kind(row.get("kind"))
        payload = codes_mod.normalize_payload(kind, row.get("payload", ""))
        cur = conn.execute("""INSERT INTO codes (kind, payload, label, notes, source) VALUES (?, ?, ?, ?, 'imported')
                              ON CONFLICT(kind, payload) DO NOTHING""",
                           (kind, payload, repo.v.text(200)(row.get("label")), repo.v.text(5000)(row.get("notes"))))
        return cur.rowcount > 0
    if entity.startswith("section:"):
        sec = ctx["section"]
        clean = repo._clean_record(conn, sec, row)  # same validation + formulas as the UI
        repo._insert(conn, "section_records", {"section_id": sec["id"], "data": repo._dumps(clean)})
        return True
    raise AssertionError(entity)


# --------------------------------------------------------------- export

EXPORT_QUERIES = {
    "contacts": """SELECT c.id, first_name, last_name, company, job_title, email, phone, address, city, region,
                          postal_code, country, birthday, website, notes, favorite,
                          (SELECT group_concat(t.name, ', ') FROM contact_tags ct JOIN tags t ON t.id = ct.tag_id
                            WHERE ct.contact_id = c.id) AS tags, created_at, updated_at
                     FROM contacts c ORDER BY last_name, first_name""",
    "expenses": """SELECT id, spent_on, printf('%.2f', amount_cents / 100.0) AS amount, currency, merchant, category,
                          payment_method, description, contact_name, created_at
                     FROM v_expenses ORDER BY spent_on, id""",
    "codes": "SELECT id, kind, payload, label, notes, source, scan_count, created_at FROM codes ORDER BY id",
    "pictures": """SELECT id, original_name, title, description, album, taken_at, camera, width, height,
                          size_bytes, sha256, created_at FROM pictures ORDER BY id""",
}


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_NUMBER_RE = re.compile(r"^[+-]?\d+(\.\d+)?([eE][+-]?\d+)?$")


def safe_cell(value):
    """Neutralise CSV/formula injection: a cell like =HYPERLINK(...) would be
    executed by Excel/Sheets when the export is opened, so text starting with a
    formula character is prefixed with an apostrophe. Real numbers (-12.50)
    are left alone."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES) and not _NUMBER_RE.match(value):
        return "'" + value
    return value


def write_csv(columns, data) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([safe_cell(c) for c in columns])
    for r in data:
        w.writerow([safe_cell(c) for c in r])
    return buf.getvalue()


def export_csv(conn, entity: str) -> str:
    if entity.startswith("section:"):
        return _export_section_csv(conn, entity.split(":", 1)[1])
    if entity not in EXPORT_QUERIES:
        raise ValidationError({"entity": f"must be one of {', '.join(EXPORT_QUERIES)} or section:<slug>"})
    cur = conn.execute(EXPORT_QUERIES[entity])
    return write_csv([d[0] for d in cur.description], cur)


def _export_section_csv(conn, ref: str) -> str:
    sec = repo.get_section(conn, ref)
    def col(f):
        # field keys are allow-listed ([a-z0-9_]), so they are safe in the JSON path
        expr = f"json_extract(data, '$.{f['key']}')"
        result = f["options"].get("result") if f["type"] == "formula" else f["type"]
        if result == "money":  # stored as cents; export dollars so the file re-imports correctly
            expr = f"printf('%.2f', {expr} / 100.0)"
        elif result == "boolean":
            expr = f"CASE {expr} WHEN 1 THEN 'yes' WHEN 0 THEN 'no' END"
        return f"CASE WHEN json_extract(data, '$.{f['key']}') IS NULL THEN NULL ELSE {expr} END AS \"{f['key']}\""
    cols = ", ".join(col(f) for f in sec["fields"])
    cur = conn.execute(f"SELECT id, {cols}, created_at, updated_at FROM section_records WHERE section_id = ? ORDER BY id",
                       (sec["id"],))
    return write_csv(["id", *[f["label"] for f in sec["fields"]], "created_at", "updated_at"], cur)


def export_json(conn) -> dict:
    """Whole-database logical dump (portable across schema versions)."""
    out = {"format": "datavault", "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "schema_version": conn.execute("PRAGMA user_version").fetchone()[0], "tables": {}}
    for (name,) in conn.execute("""SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                                    AND name NOT LIKE 'search_index%' AND name <> 'audit_log' ORDER BY name"""):
        out["tables"][name] = repo.rows(conn.execute(f'SELECT * FROM "{name}"'))
    return out


# ---------------------------------------------------------------- vCard

def _vesc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _vunesc(s: str) -> str:
    return re.sub(r"\\([\\,;nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), s)


def contact_vcard(c: dict) -> str:
    lines = ["BEGIN:VCARD", "VERSION:3.0",
             f"N:{_vesc(c['last_name'])};{_vesc(c['first_name'])};;;",
             f"FN:{_vesc((c['first_name'] + ' ' + c['last_name']).strip())}"]
    if c.get("company"):
        lines.append(f"ORG:{_vesc(c['company'])}")
    if c.get("job_title"):
        lines.append(f"TITLE:{_vesc(c['job_title'])}")
    if c.get("email"):
        lines.append(f"EMAIL;TYPE=INTERNET:{c['email']}")
    if c.get("phone"):
        lines.append(f"TEL;TYPE=CELL:{c['phone']}")
    if any(c.get(k) for k in ("address", "city", "region", "postal_code", "country")):
        lines.append("ADR;TYPE=HOME:;;" + ";".join(_vesc(c.get(k) or "") for k in
                                                   ("address", "city", "region", "postal_code", "country")))
    if c.get("birthday"):
        lines.append(f"BDAY:{c['birthday']}")
    if c.get("website"):
        lines.append(f"URL:{c['website']}")
    if c.get("notes"):
        lines.append(f"NOTE:{_vesc(c['notes'])}")
    tags = c.get("tags") or []
    if tags:
        lines.append("CATEGORIES:" + ",".join(_vesc(t["name"] if isinstance(t, dict) else t) for t in tags))
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


def export_vcards(conn, ids: list[int] | None = None) -> str:
    sql = "SELECT * FROM v_contacts"
    params: tuple = ()
    if ids:
        sql += f" WHERE id IN ({','.join('?' * len(ids))})"
        params = tuple(ids)
    return "".join(contact_vcard(repo._contact_out(r)) for r in repo.rows(conn.execute(sql, params)))


def _vsplit(val: str, sep: str = ";") -> list[str]:
    """Split on separators that are not backslash-escaped."""
    return re.split(rf"(?<!\\){re.escape(sep)}", val)


def parse_vcards(text: str) -> list[dict]:
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold continuation lines
    cards = []
    for block in re.findall(r"BEGIN:VCARD(.*?)END:VCARD", text, re.S | re.I):
        c: dict = {}
        for line in block.strip().splitlines():
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            prop = key.split(";")[0].upper().split(".")[-1]
            if prop == "N":
                parts = (_vsplit(val) + [""] * 5)[:5]
                c["last_name"], c["first_name"] = _vunesc(parts[0]), _vunesc(parts[1])
            elif prop == "FN" and not c.get("first_name"):
                c["_fn"] = _vunesc(val)
            elif prop == "ORG":
                c["company"] = _vunesc(_vsplit(val)[0])
            elif prop == "TITLE":
                c["job_title"] = _vunesc(val)
            elif prop == "EMAIL" and "email" not in c:
                c["email"] = val.strip()
            elif prop == "TEL" and "phone" not in c:
                c["phone"] = val.strip()
            elif prop == "ADR" and "address" not in c:
                p = (_vsplit(val) + [""] * 7)[:7]
                c.update(address=_vunesc(p[2]), city=_vunesc(p[3]), region=_vunesc(p[4]),
                         postal_code=_vunesc(p[5]), country=_vunesc(p[6]))
            elif prop == "BDAY":
                c["birthday"] = val.strip()[:10]
            elif prop == "URL":
                c["website"] = val.strip()
            elif prop == "NOTE":
                c["notes"] = _vunesc(val)
            elif prop == "CATEGORIES":
                c["tags"] = [_vunesc(t) for t in _vsplit(val, ",") if t.strip()]
        if not c.get("first_name") and c.get("_fn"):
            first, _, last = c["_fn"].partition(" ")
            c["first_name"], c["last_name"] = first, c.get("last_name") or last
        c.pop("_fn", None)
        if c.get("first_name"):
            cards.append(c)
    return cards


def import_vcards(conn, text: str, dry_run: bool = False) -> dict:
    cards = parse_vcards(text)
    report = {"total": len(cards), "imported": 0, "duplicates": 0, "errors": [], "dry_run": dry_run}
    with_rollback = dry_run
    conn.execute("BEGIN IMMEDIATE")
    try:
        for i, card in enumerate(cards, 1):
            if card.get("email") and conn.execute("SELECT 1 FROM contacts WHERE email = ? COLLATE NOCASE",
                                                  (card["email"],)).fetchone():
                report["duplicates"] += 1
                continue
            try:
                repo.create_contact(conn, card)  # joins our transaction via SAVEPOINT
                report["imported"] += 1
            except ValidationError as e:
                report["errors"].append({"card": i, "name": card.get("first_name"), "errors": e.errors})
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("ROLLBACK" if with_rollback else "COMMIT")
    return report


# --------------------------------------------------------------- backup

def backup(conn, cfg: Config, include_media: bool = True, label: str = "") -> Path:
    """Online, consistent snapshot via the SQLite backup API (safe while the
    server is running), zipped together with the media files."""
    cfg.ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{label_slug(label)}" if label_slug(label) else ""
    out = cfg.backups_dir / f"datavault-{stamp}{suffix}.zip"
    n = 2
    while out.exists():  # two backups in the same second must not overwrite each other
        out = cfg.backups_dir / f"datavault-{stamp}{suffix}-{n}.zip"
        n += 1
    snap = cfg.backups_dir / f".snapshot-{stamp}.db"
    dest = sqlite3.connect(snap)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    try:
        check = sqlite3.connect(snap).execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"snapshot failed integrity check: {check}")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "datavault.db")
            if include_media:
                for folder, prefix in ((cfg.media_dir, "media"), (cfg.files_dir, "files")):
                    for p in folder.rglob("*"):
                        if p.is_file() and not p.name.endswith(".part"):
                            z.write(p, f"{prefix}/{p.relative_to(folder)}", compress_type=zipfile.ZIP_STORED)
            z.writestr("manifest.json", json.dumps({
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
                "include_media": include_media, **repo.stats(conn)["counts"]}, indent=2))
    finally:
        snap.unlink(missing_ok=True)
    return out


def label_slug(label) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", str(label or "").lower()).strip("-")[:40]


def backups_with_label(cfg: Config, label: str = "") -> list[Path]:
    """Backups carrying exactly this label (none = unlabelled), newest first."""
    slug = label_slug(label)
    pat = re.compile(rf"datavault-\d{{8}}-\d{{6}}{'-' + re.escape(slug) if slug else ''}(-\d+)?\.zip")
    found = [p for p in cfg.backups_dir.glob("datavault-*.zip") if pat.fullmatch(p.name)]
    return sorted(found, key=lambda p: p.name, reverse=True)


def list_backups(cfg: Config) -> list[dict]:
    if not cfg.backups_dir.exists():
        return []
    out = []
    for p in sorted(cfg.backups_dir.glob("datavault-*.zip"), reverse=True):
        info = {"name": p.name, "size_bytes": p.stat().st_size,
                "created_at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")}
        try:
            with zipfile.ZipFile(p) as z:
                info["manifest"] = json.loads(z.read("manifest.json"))
        except (KeyError, zipfile.BadZipFile, json.JSONDecodeError):
            info["manifest"] = None
        out.append(info)
    return out


def _check_snapshot(path: Path) -> None:
    try:
        c = sqlite3.connect(path)
        try:
            ok = c.execute("PRAGMA integrity_check").fetchone()[0]
            has_core = c.execute("SELECT COUNT(*) FROM sqlite_schema WHERE type = 'table' AND name IN "
                                 "('contacts', 'expenses', 'pictures')").fetchone()[0] == 3
        finally:
            c.close()
    except sqlite3.DatabaseError as e:
        path.unlink(missing_ok=True)
        raise ValidationError({"archive": f"datavault.db is not a valid SQLite database ({e})"}) from None
    if ok != "ok" or not has_core:
        path.unlink(missing_ok=True)
        raise ValidationError({"archive": "datavault.db failed its integrity check or is not a DataVault database"})


def restore(cfg: Config, archive: Path) -> None:
    """Replace the live DB + media with a backup. Run with the server stopped.
    The current state is itself backed up first, so a restore is undoable."""
    archive = Path(archive)
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        if "datavault.db" not in names:
            raise ValidationError({"archive": "does not contain datavault.db"})
        for n in names:  # zip-slip guard
            if n.startswith("/") or ".." in Path(n).parts:
                raise ValidationError({"archive": f"unsafe path {n!r}"})
        tmp = cfg.db_path.with_suffix(".restore")
        tmp.write_bytes(z.read("datavault.db"))
        _check_snapshot(tmp)  # validate first: never back up + swap for a file we can't open
        if cfg.db_path.exists():
            live = connect(cfg.db_path)
            backup(live, cfg, label="pre-restore")
            live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            live.close()
        for ext in ("-wal", "-shm"):
            Path(str(cfg.db_path) + ext).unlink(missing_ok=True)
        tmp.replace(cfg.db_path)
        for n in names:
            for prefix, folder in (("media/", cfg.media_dir), ("files/", cfg.files_dir)):
                if n.startswith(prefix) and not n.endswith("/"):
                    target = folder / n[len(prefix):]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(z.read(n))
