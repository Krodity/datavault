"""Command-line interface: everything the web UI does, scriptable.

    datavault serve                     run the web app on 127.0.0.1:8800
    datavault query "SELECT ..."        read-only SQL, pretty table / csv / json
    datavault import expenses bank.csv  CSV import with --dry-run
    datavault backup / restore          consistent snapshots incl. media
    datavault seed                      demo data for a first look
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

from . import codes, media, repo, transfer
from .config import Config
from .db import current_version, migrate, open_db, connect
from .query import QueryRunner
from .validation import ValidationError


def _table(columns: list[str], data: list[list], max_width: int = 40) -> str:
    def cell(x):
        s = "NULL" if x is None else str(x).replace("\n", " ")
        return s if len(s) <= max_width else s[: max_width - 1] + "…"
    cells = [[cell(x) for x in r] for r in data]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(columns)]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [line, "| " + " | ".join(c.ljust(w) for c, w in zip(columns, widths)) + " |", line]
    out += ["| " + " | ".join(c.ljust(w) for c, w in zip(r, widths)) + " |" for r in cells]
    out.append(line)
    return "\n".join(out)


def cmd_init(cfg, a):
    conn = connect(cfg.db_path) if cfg.db_path.exists() else None
    cfg.ensure_dirs()
    conn = conn or connect(cfg.db_path)
    applied = migrate(conn)
    print(f"database: {cfg.db_path}")
    print(f"schema version: {current_version(conn)}" + (f" (applied {', '.join(applied)})" if applied else " (up to date)"))


def cmd_serve(cfg, a):
    from .web import create_app
    app = create_app(cfg, allowed_hosts=set(a.allow_host or []))
    print(f"DataVault on http://{a.host}:{a.port}  (data: {cfg.home})")
    if a.debug:
        app.run(host=a.host, port=a.port, debug=True)
        return
    try:
        from waitress import serve  # optional production server
        serve(app, host=a.host, port=a.port, threads=8)
    except ImportError:
        app.run(host=a.host, port=a.port, threaded=True)


def cmd_query(cfg, a):
    open_db(cfg).close()
    sql = a.sql if a.sql != "-" else sys.stdin.read()
    if a.file:
        sql = Path(a.file).read_text()
    res = QueryRunner(cfg, timeout_ms=a.timeout, max_rows=a.limit).run(sql, explain=a.explain)
    if a.format == "json":
        print(json.dumps([dict(zip(res["columns"], r)) for r in res["rows"]], indent=2, default=str))
    elif a.format == "csv":
        w = csv.writer(sys.stdout)
        w.writerow(res["columns"])
        w.writerows(res["rows"])
    else:
        print(_table(res["columns"], res["rows"]))
        print(f"{res['row_count']} row(s) in {res['elapsed_ms']} ms" + ("  [truncated]" if res["truncated"] else ""))


def cmd_schema(cfg, a):
    open_db(cfg).close()
    for t in QueryRunner(cfg).schema():
        if a.table and t["name"] != a.table:
            continue
        rc = f"  ({t['row_count']} rows)" if t["row_count"] is not None else ""
        print(f"\n{t['type'].upper()} {t['name']}{rc}")
        for c in t["columns"]:
            flags = " PK" if c["pk"] else ""
            flags += " NOT NULL" if c["notnull"] else ""
            print(f"  {c['name']:<22} {c['type'] or '':<10}{flags}")
        for f in t["foreign_keys"]:
            print(f"  FK {f['from']} -> {f['table']}({f['to']}) ON DELETE {f['on_delete']}")
        if a.sql and t["sql"]:
            print("  " + t["sql"].replace("\n", "\n  "))


def cmd_import(cfg, a):
    conn = open_db(cfg)
    text = Path(a.file).read_text(encoding="utf-8-sig")
    if a.entity == "vcard" or a.file.lower().endswith(".vcf"):
        rep = transfer.import_vcards(conn, text, dry_run=a.dry_run)
    else:
        mapping = json.loads(a.mapping) if a.mapping else None
        rep = transfer.import_csv(conn, a.entity, text, mapping=mapping, dry_run=a.dry_run, skip_errors=a.skip_errors)
    if rep.get("mapping"):
        print("column mapping:")
        for h, f in rep["mapping"].items():
            print(f"  {h!r:>28} -> {f}")
        if rep.get("unmapped"):
            print(f"  ignored columns: {', '.join(rep['unmapped'])}")
    for e in rep["errors"][:25]:
        where = f"line {e['line']}" if "line" in e else f"card {e['card']}"
        print(f"  ✗ {where}: " + "; ".join(f"{k}: {v}" for k, v in e["errors"].items()))
    state = "DRY RUN — nothing written" if rep["dry_run"] else ("ROLLED BACK" if rep.get("rolled_back") else "committed")
    print(f"{rep['total']} rows · {rep['imported']} imported · {rep.get('duplicates', 0)} duplicates · "
          f"{len(rep['errors'])} errors · {state}")
    if rep.get("rolled_back"):
        print("fix the rows above, or re-run with --skip-errors to import the valid ones")
        sys.exit(1)


def cmd_export(cfg, a):
    conn = open_db(cfg)
    if a.entity == "json":
        data = json.dumps(transfer.export_json(conn), indent=2, default=str)
    elif a.entity == "vcard":
        data = transfer.export_vcards(conn)
    else:
        data = transfer.export_csv(conn, a.entity)
    if a.output in (None, "-"):
        sys.stdout.write(data)
    else:
        Path(a.output).write_text(data)
        print(f"wrote {a.output} ({len(data):,} bytes)", file=sys.stderr)


def cmd_backup(cfg, a):
    conn = open_db(cfg)
    if a.list:
        for b in transfer.list_backups(cfg):
            print(f"{b['name']:<48} {b['size_bytes'] / 1e6:8.2f} MB  {b['created_at']}")
        return
    p = transfer.backup(conn, cfg, include_media=not a.no_media, label=a.label or "")
    print(f"backup written: {p} ({p.stat().st_size / 1e6:.2f} MB)")
    if a.keep:
        old = sorted(cfg.backups_dir.glob("datavault-*.zip"), reverse=True)[a.keep:]
        for o in old:
            o.unlink()
        if old:
            print(f"pruned {len(old)} old backup(s), keeping {a.keep}")


def cmd_restore(cfg, a):
    if not a.yes:
        ans = input(f"Replace the database in {cfg.home} with {a.archive}? The current state is backed up first. [y/N] ")
        if ans.strip().lower() != "y":
            print("aborted")
            return
    transfer.restore(cfg, Path(a.archive))
    conn = open_db(cfg)  # migrates an older backup forward
    print(f"restored; schema version {current_version(conn)}")


def cmd_add_picture(cfg, a):
    conn = open_db(cfg)
    for f in a.files:
        try:
            pic, new = media.ingest(conn, cfg, Path(f).read_bytes(), Path(f).name, {"album": a.album or ""})
            print(f"{'added' if new else 'exists'}  #{pic['id']}  {pic['original_name']}  {pic['width']}x{pic['height']}")
        except ValidationError as e:
            print(f"skipped {f}: {e}", file=sys.stderr)


def cmd_code(cfg, a):
    conn = open_db(cfg)
    if a.action == "make":
        code, new = codes.save_code(conn, {"kind": a.kind, "payload": a.payload, "label": a.label or ""})
        print(f"{'saved' if new else 'exists'} #{code['id']} {code['kind_label']}: {code['payload']}")
        if a.output:
            data, _ = codes.render(code["kind"], code["payload"], "svg" if a.output.endswith(".svg") else "png")
            Path(a.output).write_bytes(data)
            print(f"image: {a.output}")
    elif a.action == "scan":
        found = codes.decode_image(Path(a.payload).read_bytes())
        if not found:
            print("no barcode or QR code found")
            sys.exit(1)
        for item in found:
            print(f"{item['symbology']:<8} {item['payload']}")
            if a.save and item["kind"]:
                code, _ = codes.save_code(conn, {"kind": item["kind"], "payload": item["payload"], "source": "scanned"})
                print(f"  saved as #{code['id']} (scanned {code['scan_count']}×)")


def cmd_search(cfg, a):
    conn = open_db(cfg)
    for r in repo.search(conn, " ".join(a.terms)):
        print(f"{r['kind']:<8} #{r['ref_id']:<5} {r['title']}  —  {r['snippet'].replace('<mark>', '[').replace('</mark>', ']')}")


def cmd_stats(cfg, a):
    conn = open_db(cfg)
    s = repo.stats(conn)
    print(f"database  {cfg.db_path}  ({s['db_bytes'] / 1e6:.2f} MB, schema v{s['schema_version']})")
    for k, val in s["counts"].items():
        print(f"  {k:<12} {val:,}")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    if a.verify_media:
        print(json.dumps(media.verify_media(conn, cfg), indent=2))


# ------------------------------------------------------------------ seed

def cmd_seed(cfg, a):
    conn = open_db(cfg)
    if conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] and not a.force:
        print("database already has data; use --force to add demo rows anyway")
        return
    rnd = random.Random(a.seed)
    people = [("Maya", "Okafor", "Brightline Design", "Product Designer", "friend,design"),
              ("Daniel", "Reyes", "Reyes Plumbing", "Owner", "home,service"),
              ("Priya", "Nair", "Northwind Health", "Physician", "health"),
              ("Tom", "Becker", "", "", "family"), ("Lena", "Fischer", "Atlas Logistics", "Recruiter", "work,career"),
              ("Jordan", "Kim", "Coastal Credit Union", "Loan Officer", "finance"),
              ("Ava", "Thompson", "", "", "family"), ("Marcus", "Hale", "Hale & Sons Auto", "Mechanic", "service,car"),
              ("Sofia", "Rossi", "Lumen Labs", "Data Engineer", "work,friend"),
              ("Eli", "Turner", "City Library", "Librarian", "friend")]
    today = date.today()
    cids = []
    for i, (f, l, co, t, tags) in enumerate(people):
        bday = date(1975 + rnd.randint(0, 30), 1 + (today.month + i - 1) % 12, rnd.randint(1, 28))
        if i == 0:
            bday = (today + timedelta(days=6)).replace(year=1992)
        c = repo.create_contact(conn, {
            "first_name": f, "last_name": l, "company": co, "job_title": t,
            "email": f"{f.lower()}.{l.lower()}@example.com", "phone": f"(555) {rnd.randint(200, 999)}-{rnd.randint(1000, 9999)}",
            "city": rnd.choice(["Portland", "Austin", "Denver", "Seattle"]), "region": rnd.choice(["OR", "TX", "CO", "WA"]),
            "country": "USA", "birthday": bday.isoformat(), "favorite": i in (0, 3), "tags": tags,
            "notes": rnd.choice(["Met at a conference.", "Great contact for referrals.", "", "Prefers text over calls."])})
        cids.append(c["id"])

    cats = {c["name"]: c["id"] for c in repo.list_categories(conn)}
    merchants = {"Groceries": ["Trader Joe's", "Safeway", "Costco", "Whole Foods"],
                 "Dining": ["Blue Bottle Coffee", "Chipotle", "Pho Saigon", "Local Pizza Co"],
                 "Transport": ["Shell", "Chevron", "Uber", "Metro Transit"],
                 "Bills": ["Electric Co", "Comcast", "Water Utility", "T-Mobile"],
                 "Shopping": ["Amazon", "Target", "REI", "Best Buy"],
                 "Health": ["CVS Pharmacy", "Northwind Health"], "Entertainment": ["Steam", "AMC Theatres", "Spotify"]}
    ranges = {"Groceries": (25, 180), "Dining": (6, 70), "Transport": (12, 65), "Bills": (40, 160),
              "Shopping": (15, 220), "Health": (10, 120), "Entertainment": (5, 60)}
    n = 0
    for back in range(0, 210):
        d = today - timedelta(days=back)
        for _ in range(rnd.choice([0, 0, 1, 1, 2])):
            cat = rnd.choices(list(merchants), weights=[5, 6, 3, 1, 2, 1, 2])[0]
            lo, hi = ranges[cat]
            repo.create_expense(conn, {"spent_on": d.isoformat(), "amount": f"{rnd.uniform(lo, hi):.2f}",
                                       "merchant": rnd.choice(merchants[cat]), "category_id": cats[cat],
                                       "payment_method": rnd.choice(["Visa", "Debit", "Cash", "Apple Pay"]),
                                       "contact_id": cids[1] if cat == "Bills" and rnd.random() < .1 else None})
            n += 1

    for kind, payload, label in [("qr", "https://github.com/Krodity", "GitHub profile"),
                                 ("ean13", "590123412345", "Sample product"),
                                 ("isbn13", "978030640615", "Book — sample ISBN"),
                                 ("code128", "INV-2026-0042", "Invoice label"),
                                 ("upca", "03600029145", "Grocery item")]:
        codes.save_code(conn, {"kind": kind, "payload": payload, "label": label})
    codes.save_code(conn, {"kind": "qr", "payload": codes.wifi_payload("GuestNetwork", "welcome123"), "label": "Guest Wi-Fi"})

    # a couple of generated images so the gallery isn't empty
    from PIL import Image, ImageDraw
    import io
    for i, (hue, name) in enumerate([((99, 102, 241), "Sunset swatch"), ((16, 185, 129), "Forest swatch"),
                                     ((244, 114, 182), "Blossom swatch")]):
        img = Image.new("RGB", (800, 600), hue)
        dr = ImageDraw.Draw(img)
        for k in range(0, 800, 40):
            dr.line([(k, 0), (k + 300, 600)], fill=tuple(min(255, c + 40) for c in hue), width=12)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        media.ingest(conn, cfg, buf.getvalue(), f"demo-{i + 1}.png", {"title": name, "album": "Demo"})

    s = repo.create_section(conn, {"name": "Book Collection", "icon": "📚", "description": "Books I own or want",
                                   "fields": [{"label": "Title", "type": "text", "required": True},
                                              {"label": "Author", "type": "text"},
                                              {"label": "Status", "type": "select", "options": "Owned,Reading,Wishlist"},
                                              {"label": "Rating", "type": "number"},
                                              {"label": "Price", "type": "money"},
                                              {"label": "Finished", "type": "date"},
                                              {"label": "ISBN", "type": "code"}]})
    isbn = conn.execute("SELECT id FROM codes WHERE kind = 'isbn13'").fetchone()[0]
    for t, au, st, r in [("Designing Data-Intensive Applications", "Martin Kleppmann", "Reading", 5),
                         ("SQL Performance Explained", "Markus Winand", "Owned", 4),
                         ("The Pragmatic Programmer", "Hunt & Thomas", "Owned", 5),
                         ("Database Internals", "Alex Petrov", "Wishlist", None)]:
        repo.create_record(conn, s["slug"], {"title": t, "author": au, "status": st, "rating": r,
                                             "price": f"{rnd.uniform(25, 55):.2f}",
                                             "isbn": isbn if t.startswith("Designing") else None})
    repo.create_section(conn, {"name": "Home Inventory", "icon": "🏠", "description": "Serial numbers & warranties",
                               "fields": [{"label": "Item", "type": "text", "required": True},
                                          {"label": "Room", "type": "select", "options": "Living,Kitchen,Office,Garage"},
                                          {"label": "Serial number", "type": "text"},
                                          {"label": "Purchase price", "type": "money"},
                                          {"label": "Warranty until", "type": "date"},
                                          {"label": "Receipt", "type": "picture"}]})
    for name, sql in [("Spending this month by category", """SELECT category, printf('$%.2f', SUM(amount)) AS total
FROM v_expenses WHERE month = strftime('%Y-%m', 'now') GROUP BY category ORDER BY SUM(amount) DESC;""")]:
        repo.save_query(conn, {"name": name, "sql": sql})
    print(f"seeded {len(people)} contacts, {n} expenses, 6 codes, 3 pictures, 2 sections")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="datavault", description="Personal SQLite data vault")
    p.add_argument("--home", help="data directory (default: $DATAVAULT_HOME or ./data)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create / migrate the database").set_defaults(fn=cmd_init)

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8800)
    s.add_argument("--allow-host", action="append", help="extra Host header to accept (e.g. a LAN name)")
    s.add_argument("--debug", action="store_true")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("query", help="run read-only SQL")
    s.add_argument("sql", nargs="?", default="-", help="SQL text, or - for stdin")
    s.add_argument("-f", "--file")
    s.add_argument("--format", choices=["table", "csv", "json"], default="table")
    s.add_argument("--explain", action="store_true", help="show the query plan instead")
    s.add_argument("--limit", type=int, default=1000)
    s.add_argument("--timeout", type=int, default=5000, help="ms")
    s.set_defaults(fn=cmd_query)

    s = sub.add_parser("schema", help="describe tables and views")
    s.add_argument("table", nargs="?")
    s.add_argument("--sql", action="store_true", help="also print CREATE statements")
    s.set_defaults(fn=cmd_schema)

    s = sub.add_parser("import", help="import CSV (contacts/expenses/codes) or vCard")
    s.add_argument("entity", choices=["contacts", "expenses", "codes", "vcard"])
    s.add_argument("file")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--skip-errors", action="store_true", help="commit valid rows even if some fail")
    s.add_argument("--mapping", help='JSON {"CSV header": "field"} overriding the auto-mapping')
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("export", help="export CSV / JSON / vCard")
    s.add_argument("entity", help="contacts | expenses | codes | pictures | section:<slug> | json | vcard")
    s.add_argument("-o", "--output")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("backup", help="snapshot DB + media to a zip")
    s.add_argument("--no-media", action="store_true")
    s.add_argument("--label")
    s.add_argument("--keep", type=int, help="prune to the newest N backups")
    s.add_argument("--list", action="store_true")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("restore", help="restore from a backup zip (stop the server first)")
    s.add_argument("archive")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("add-picture", help="ingest image files")
    s.add_argument("files", nargs="+")
    s.add_argument("--album")
    s.set_defaults(fn=cmd_add_picture)

    s = sub.add_parser("code", help="make or scan barcodes / QR codes")
    s.add_argument("action", choices=["make", "scan"])
    s.add_argument("payload", help="text to encode, or image path for scan")
    s.add_argument("--kind", default="qr", choices=list(codes.KINDS))
    s.add_argument("--label")
    s.add_argument("-o", "--output", help="write PNG/SVG")
    s.add_argument("--save", action="store_true", help="store scanned codes")
    s.set_defaults(fn=cmd_code)

    s = sub.add_parser("search", help="full-text search everything")
    s.add_argument("terms", nargs="+")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("stats", help="counts, size, integrity")
    s.add_argument("--verify-media", action="store_true")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("seed", help="load demo data")
    s.add_argument("--force", action="store_true")
    s.add_argument("--seed", type=int, default=42)
    s.set_defaults(fn=cmd_seed)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    cfg = Config.from_env(a.home)
    try:
        a.fn(cfg, a)
    except ValidationError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
