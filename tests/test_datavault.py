import io
import json
import zipfile

import pytest
from PIL import Image

from datavault import codes, media, repo, transfer
from datavault.config import Config
from datavault.db import open_db
from datavault.query import QueryRunner
from datavault.validation import ValidationError, cents
from datavault.web import create_app

H = {"X-DataVault": "1"}


@pytest.fixture
def cfg(tmp_path):
    return Config.from_env(tmp_path / "vault")


@pytest.fixture
def conn(cfg):
    c = open_db(cfg)
    yield c
    c.close()


@pytest.fixture
def client(cfg):
    app = create_app(cfg)
    app.testing = True
    return app.test_client()


def png_bytes(color=(200, 30, 30), size=(64, 48)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


# ------------------------------------------------------------ schema / db

def test_migrations_are_idempotent(cfg):
    a = open_db(cfg)
    assert a.execute("PRAGMA user_version").fetchone()[0] == 3
    a.close()
    b = open_db(cfg)  # second open applies nothing
    assert b.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 3


def test_foreign_keys_enforced_and_cascade(conn):
    c = repo.create_contact(conn, {"first_name": "Ann", "tags": "a, b"})
    assert {t["name"] for t in c["tags"]} == {"a", "b"}
    repo.delete_contact(conn, c["id"])
    assert conn.execute("SELECT COUNT(*) FROM contact_tags").fetchone()[0] == 0


def test_check_constraints_reject_bad_rows(conn):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO expenses (spent_on, amount_cents) VALUES ('yesterday', 100)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO expenses (spent_on, amount_cents) VALUES ('2026-01-01', -5)")


# ------------------------------------------------------------- validation

@pytest.mark.parametrize("raw,expected", [("12", 1200), ("$1,234.56", 123456), ("0.1", 10), ("(5.00)", 500),
                                          ("19.995", 2000), (7, 700)])
def test_cents_parsing(raw, expected):
    assert cents(raw) == expected


def test_contact_validation_messages(conn):
    with pytest.raises(ValidationError) as e:
        repo.create_contact(conn, {"first_name": "", "email": "nope", "birthday": "31/31/2020"})
    assert set(e.value.errors) == {"first_name", "email", "birthday"}


# --------------------------------------------------------------- expenses

def test_expense_summary_and_budget(conn):
    cat = repo.category_id_for(conn, "Groceries", create=False)
    repo.create_expense(conn, {"spent_on": "2026-03-04", "amount": "10.10", "category_id": cat, "merchant": "A"})
    repo.create_expense(conn, {"spent_on": "2026-03-20", "amount": "0.20", "category_id": cat, "merchant": "A"})
    repo.create_expense(conn, {"spent_on": "2026-02-01", "amount": "5", "merchant": "B"})
    s = repo.expense_summary(conn, "2026-03", 3)
    assert [m["month"] for m in s["trend"]] == ["2026-01", "2026-02", "2026-03"]
    assert s["totals"]["month_cents"] == 1030 and s["totals"]["prev_month_cents"] == 500
    groc = next(c for c in s["by_category"] if c["category"] == "Groceries")
    assert groc["total_cents"] == 1030 and groc["budget_cents"] == 60000


def test_expense_filters_and_sort_allowlist(conn):
    for d, a in [("2026-01-01", "5"), ("2026-01-15", "50"), ("2026-02-01", "500")]:
        repo.create_expense(conn, {"spent_on": d, "amount": a})
    r = repo.list_expenses(conn, date_from="2026-01-10", min_amount="10")
    assert r["total"] == 2 and r["sum_cents"] == 55000
    with pytest.raises(ValidationError):
        repo.list_expenses(conn, sort="amount; DROP TABLE expenses")


def test_expense_reference_must_exist(conn):
    with pytest.raises(ValidationError) as e:
        repo.create_expense(conn, {"spent_on": "2026-01-01", "amount": "1", "contact_id": 999})
    assert "contact_id" in e.value.errors


# --------------------------------------------------------------- sections

def test_custom_section_roundtrip(conn):
    s = repo.create_section(conn, {"name": "Wines", "fields": [
        {"label": "Name", "required": True}, {"label": "Vintage", "type": "number"},
        {"label": "Color", "type": "select", "options": "Red,White"}, {"label": "Price", "type": "money"}]})
    assert s["slug"] == "wines" and [f["key"] for f in s["fields"]] == ["name", "vintage", "color", "price"]
    repo.create_record(conn, "wines", {"name": "Rioja", "vintage": "2019", "color": "Red", "price": "$18.50"})
    repo.create_record(conn, "wines", {"name": "Chablis", "vintage": 2021, "color": "White"})
    with pytest.raises(ValidationError) as e:
        repo.create_record(conn, "wines", {"color": "Blue"})
    assert set(e.value.errors) == {"name", "color"}
    r = repo.list_records(conn, "wines", sort="vintage", filters={"color": "Red"})
    assert r["total"] == 1 and r["items"][0]["data"] == {"name": "Rioja", "vintage": 2019, "color": "Red", "price": 1850}
    with pytest.raises(ValidationError):
        repo.list_records(conn, "wines", sort="x') OR 1=1 --")


def test_section_records_are_searchable(conn):
    repo.create_section(conn, {"name": "Notes", "fields": [{"label": "Body"}]})
    repo.create_record(conn, "notes", {"body": "remember the zucchini festival"})
    hits = repo.search(conn, "zucchini")
    assert hits and hits[0]["kind"] == "record"


# ----------------------------------------------------------------- search

def test_fts_tracks_updates_and_deletes(conn):
    c = repo.create_contact(conn, {"first_name": "Grace", "last_name": "Hopper", "company": "Navy"})
    assert repo.search(conn, "hopp")[0]["ref_id"] == c["id"]
    repo.update_contact(conn, c["id"], {"company": "Remington Rand"})
    assert repo.search(conn, "remington") and not repo.search(conn, "navy")
    repo.delete_contact(conn, c["id"])
    assert not repo.search(conn, "grace")


def test_search_input_cannot_inject_fts_syntax(conn):
    assert repo.search(conn, 'NEAR( " OR * AND') == []


def test_audit_log_records_changes(conn):
    c = repo.create_contact(conn, {"first_name": "Al"})
    repo.update_contact(conn, c["id"], {"last_name": "Turing"})
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE table_name='contacts' ORDER BY id")]
    assert actions == ["INSERT", "UPDATE"]  # the updated_at touch does not double-log


# ---------------------------------------------------------- query console

@pytest.mark.parametrize("sql", ["DELETE FROM contacts", "DROP TABLE tags", "PRAGMA user_version = 9",
                                 "ATTACH DATABASE ':memory:' AS x", "SELECT 1; DELETE FROM contacts",
                                 "INSERT INTO tags(name) VALUES ('x')"])
def test_console_blocks_writes(cfg, conn, sql):
    with pytest.raises(ValidationError):
        QueryRunner(cfg).run(sql)


def test_console_reads_and_limits(cfg, conn):
    q = QueryRunner(cfg, timeout_ms=300, max_rows=10)
    r = q.run("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < 50) SELECT i FROM n")
    assert r["row_count"] == 10 and r["truncated"]
    with pytest.raises(ValidationError, match="cancelled"):
        q.run("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) SELECT count(*) FROM n")
    assert q.run("SELECT * FROM search_index WHERE search_index MATCH 'x'")["row_count"] == 0
    assert q.run("SELECT 1", explain=True)["columns"]


# ------------------------------------------------------------------ codes

@pytest.mark.parametrize("kind,raw,canonical", [("ean13", "590123412345", "5901234123457"),
                                                ("upca", "03600029145", "036000291452"),
                                                ("isbn13", "978-0-306-40615", "9780306406157")])
def test_check_digits(kind, raw, canonical):
    assert codes.normalize_payload(kind, raw) == canonical


def test_bad_check_digit_rejected():
    with pytest.raises(ValidationError, match="check digit"):
        codes.normalize_payload("ean13", "5901234123450")


@pytest.mark.parametrize("kind,payload", [("qr", "https://example.com/x?y=1"), ("ean13", "5901234123457"),
                                          ("code128", "INV-2026-0042"), ("upca", "036000291452")])
def test_render_then_decode_roundtrip(kind, payload):
    png, mime = codes.render(kind, payload)
    assert mime == "image/png"
    found = codes.decode_image(png)
    assert any(f["payload"] == payload for f in found)


def test_scanning_same_code_upserts(conn):
    a, new_a = codes.save_code(conn, {"kind": "qr", "payload": "hello", "source": "scanned"})
    b, new_b = codes.save_code(conn, {"kind": "qr", "payload": "hello", "source": "scanned"})
    assert new_a and not new_b and a["id"] == b["id"] and b["scan_count"] == 2


def test_wifi_payload_escaping():
    assert codes.wifi_payload('my;net', 'p:a"ss') == 'WIFI:T:WPA;S:my\\;net;P:p\\:a\\"ss;H:false;;'


# ----------------------------------------------------------------- media

def test_picture_dedup_and_delete(conn, cfg):
    data = png_bytes()
    p1, new1 = media.ingest(conn, cfg, data, "a.png")
    p2, new2 = media.ingest(conn, cfg, data, "copy.png")
    assert new1 and not new2 and p1["id"] == p2["id"]
    assert media.media_path(cfg, p1["stored_name"]).exists() and media.thumb_path(cfg, p1["sha256"]).exists()
    c = repo.create_contact(conn, {"first_name": "P", "photo_id": p1["id"]})
    media.delete_picture(conn, cfg, p1["id"])
    assert repo.get_contact(conn, c["id"])["photo_id"] is None  # ON DELETE SET NULL
    assert not media.media_path(cfg, p1["stored_name"]).exists()


def test_non_image_rejected(conn, cfg):
    with pytest.raises(ValidationError):
        media.ingest(conn, cfg, b"<?php echo 1; ?>", "evil.png")


# -------------------------------------------------------- import / export

def test_csv_import_maps_headers_and_is_atomic(conn):
    good = "Date,Payee,Amount,Category\n03/01/2026,Cafe,4.50,Dining\n2026-03-02,Shop,\"$1,200.00\",NewCat\n"
    rep = transfer.import_csv(conn, "expenses", good)
    assert rep["imported"] == 2 and rep["mapping"]["Payee"] == "merchant"
    assert repo.category_id_for(conn, "NewCat", create=False)
    bad = "Date,Amount\n2026-03-03,1\nnot-a-date,2\n"
    rep = transfer.import_csv(conn, "expenses", bad)
    assert rep["rolled_back"] and rep["errors"][0]["line"] == 3
    assert conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 2  # nothing from the bad file
    rep = transfer.import_csv(conn, "expenses", bad, skip_errors=True)
    assert rep["imported"] == 1


def test_dry_run_writes_nothing(conn):
    rep = transfer.import_csv(conn, "contacts", "Name,Email\nAda Lovelace,ada@example.com\n", dry_run=True)
    assert rep["imported"] == 1 and conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0


def test_vcard_roundtrip(conn):
    repo.create_contact(conn, {"first_name": "Zoë", "last_name": "O'Neil", "company": "A; B", "email": "z@x.io",
                               "notes": "line1\nline2", "tags": "x,y"})
    vcf = transfer.export_vcards(conn)
    conn.execute("DELETE FROM contacts")
    rep = transfer.import_vcards(conn, vcf)
    assert rep["imported"] == 1
    c = repo.list_contacts(conn)["items"][0]
    assert (c["first_name"], c["company"], c["notes"]) == ("Zoë", "A; B", "line1\nline2")
    assert {t["name"] for t in c["tags"]} == {"x", "y"}


def test_backup_and_restore(conn, cfg):
    repo.create_contact(conn, {"first_name": "Keep"})
    media.ingest(conn, cfg, png_bytes(), "p.png")
    path = transfer.backup(conn, cfg)
    with zipfile.ZipFile(path) as z:
        assert "datavault.db" in z.namelist() and json.loads(z.read("manifest.json"))["contacts"] == 1
    repo.create_contact(conn, {"first_name": "Later"})
    conn.close()
    transfer.restore(cfg, path)
    c2 = open_db(cfg)
    assert [r[0] for r in c2.execute("SELECT first_name FROM contacts")] == ["Keep"]


# ------------------------------------------------------------------- HTTP

def test_api_requires_csrf_header(client):
    assert client.post("/api/contacts", json={"first_name": "x"}).status_code == 403
    assert client.post("/api/contacts", json={"first_name": "x"}, headers=H).status_code == 201


def test_api_errors_are_json(client):
    r = client.post("/api/expenses", json={"amount": "abc"}, headers=H)
    assert r.status_code == 400 and set(r.json["fields"]) == {"spent_on", "amount_cents"}
    assert client.get("/api/contacts/999").status_code == 404


def test_api_upload_and_serve_picture(client):
    r = client.post("/api/pictures", data={"file": (io.BytesIO(png_bytes()), "x.png")}, headers=H,
                    content_type="multipart/form-data")
    pid = r.json["items"][0]["id"]
    assert client.get(f"/media/{pid}/thumb").status_code == 200


def test_api_decode_upload(client):
    png, _ = codes.render("qr", "scan me")
    r = client.post("/api/codes/decode", data={"file": (io.BytesIO(png), "q.png"), "save": "1"}, headers=H,
                    content_type="multipart/form-data")
    assert r.json["found"][0]["payload"] == "scan me" and r.json["saved"][0]["source"] == "scanned"


def test_frontend_served(client):
    assert b"DataVault" in client.get("/").data
    assert client.get("/static/app.js").status_code == 200
