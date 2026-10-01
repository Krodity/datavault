"""Formula engine tests + regression tests for bugs found in the code audit."""
import csv
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from PIL import Image

from datavault import codes, formulas as fx, media, repo, transfer
from datavault.config import Config
from datavault.db import connect, migrate, open_db
from datavault.query import QueryRunner
from datavault.validation import ValidationError, cents, iso_date, number
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


def run(expr, env=None, result="number", known=None):
    known = set(known or (env or {}).keys())
    return fx.evaluate(fx.compile_formula("out", expr, result, known), env or {})


# ================================================================ formulas

@pytest.mark.parametrize("expr,env,result,expected", [
    ("price * qty", {"price": Decimal("19.99"), "qty": Decimal(3)}, "money", 5997),
    ("0.1 + 0.2", {}, "number", 0.3),                       # Decimal, not float drift
    ("=a = b", {"a": "Red", "b": "red"}, "boolean", True),   # spreadsheet '=' and case-insensitive text
    ("a <> b", {"a": Decimal(1), "b": Decimal(2)}, "boolean", True),
    ('first & " " & last', {"first": "Ada", "last": "Lovelace"}, "text", "Ada Lovelace"),
    ("2 ^ 10", {}, "number", 1024),
    ("IF(x > 5, \"big\", \"small\")", {"x": Decimal(7)}, "text", "big"),
    ("ROUND(10 / 3, 2)", {}, "number", 3.33),
    ("SUM(a, b, c)", {"a": Decimal(1), "b": None, "c": Decimal(2)}, "number", 3),
    ("AVG(a, b)", {"a": Decimal(2), "b": None}, "number", 2),
    ("DAYS_BETWEEN(a, b)", {"a": date(2026, 1, 1), "b": date(2026, 3, 1)}, "number", 59),
    ("ADD_MONTHS(d, 1)", {"d": date(2026, 1, 31)}, "date", "2026-02-28"),
    ("d + 10", {"d": date(2026, 12, 25)}, "date", "2027-01-04"),
    ("UPPER(LEFT(s, 3))", {"s": "hello"}, "text", "HEL"),
    ("COALESCE(a, b, 5)", {"a": None, "b": None}, "number", 5),
    ("IFERROR(1 / 0, -1)", {}, "number", -1),
    ("NOT(ISBLANK(x))", {"x": None}, "boolean", False),
])
def test_formula_values(expr, env, result, expected):
    assert run(expr, env, result) == expected


@pytest.mark.parametrize("expr,env", [("a + 1", {"a": None}), ("1 / 0", {}), ("SQRT(-1)", {}), ("10 ^ 1000", {})])
def test_formula_blanks_instead_of_crashing(expr, env):
    assert run(expr, env) is None


@pytest.mark.parametrize("expr,msg", [
    ("__import__('os').system('id')", "not allowed|only plain function calls"),
    ("x.__class__", "not allowed"),
    ("[1, 2][0]", "not allowed"),
    ("(lambda: 1)()", "not allowed|only plain function calls"),
    ("open('/etc/passwd')", "unknown function"),
    ("nope + 1", "unknown field"),
    ("ROUND()", "takes"),
    ("1 +", "syntax error"),
    ("out + 1", "itself"),
])
def test_formula_rejects_unsafe_or_invalid(expr, msg):
    with pytest.raises(fx.FormulaError, match=msg):
        fx.compile_formula("out", expr, "number", {"x", "a"})


def test_formula_text_blowup_is_capped():
    assert run('x & x & x & x', {"x": "a" * 4000}, "text") is None


def test_formula_cycles_detected():
    fields = [{"key": "a", "label": "A", "type": "formula", "options": {"expr": "b + 1", "result": "number"}},
              {"key": "b", "label": "B", "type": "formula", "options": {"expr": "a + 1", "result": "number"}}]
    with pytest.raises(fx.FormulaError, match="circular"):
        fx.compile_section(fields)


def test_section_formulas_end_to_end(conn):
    s = repo.create_section(conn, {"name": "Orders", "fields": [
        {"label": "Item", "required": True}, {"label": "Price", "type": "money"}, {"label": "Qty", "type": "number"},
        # declared before the field it depends on: evaluation order comes from the dependency graph
        {"label": "Total with tax", "type": "formula", "formula": "subtotal * 1.1", "result": "money"},
        {"label": "Subtotal", "type": "formula", "formula": "price * qty", "result": "money"},
        {"label": "Bulk", "type": "formula", "formula": "qty >= 10", "result": "boolean"}]})
    r = repo.create_record(conn, s["slug"], {"item": "Pens", "price": "1.25", "qty": "12", "subtotal": "999"})
    assert r["data"]["subtotal"] == 1500 and r["data"]["total_with_tax"] == 1650 and r["data"]["bulk"] is True
    r = repo.update_record(conn, r["id"], {"qty": 2})
    assert r["data"]["subtotal"] == 250 and r["data"]["bulk"] is False
    repo.create_record(conn, s["slug"], {"item": "Paper", "price": "4", "qty": 1})
    lst = repo.list_records(conn, s["slug"], sort="subtotal", desc=True)
    assert [x["data"]["item"] for x in lst["items"]] == ["Paper", "Pens"]  # 400 > 250
    assert lst["totals"]["subtotal"] == 650 and lst["totals"]["qty"] == 3
    # editing the formula recomputes every stored record
    fields = [{**f, "options": f["options"]} for f in repo.get_section(conn, s["id"])["fields"]]
    for f in fields:
        if f["key"] == "subtotal":
            f["options"] = {"expr": "price * qty * 2", "result": "money"}
    repo.update_section(conn, s["id"], {"fields": fields})
    assert repo.get_record(conn, r["id"])["data"]["subtotal"] == 500


def test_formula_validation_errors_surface_on_section_save(conn):
    with pytest.raises(ValidationError, match="unknown field"):
        repo.create_section(conn, {"name": "Bad", "fields": [{"label": "A", "type": "number"},
                                                             {"label": "F", "type": "formula", "formula": "b * 2"}]})


def test_today_formulas_refresh_daily(conn):
    s = repo.create_section(conn, {"name": "W", "fields": [
        {"label": "Until", "type": "date"},
        {"label": "Left", "type": "formula", "formula": "DAYS_BETWEEN(TODAY(), until)"}]})
    r = repo.create_record(conn, s["slug"], {"until": (date.today() + timedelta(days=5)).isoformat()})
    assert r["data"]["left"] == 5
    conn.execute("UPDATE section_records SET data = json_set(data, '$.left', 99) WHERE id = ?", (r["id"],))
    conn.execute("UPDATE sections SET recalculated_on = '2000-01-01' WHERE id = ?", (s["id"],))
    assert repo.list_records(conn, s["slug"])["items"][0]["data"]["left"] == 5


def test_formula_check_endpoint(client):
    r = client.post("/api/formula/check", headers=H, json={
        "fields": [{"label": "Price"}, {"label": "Qty"}], "key": "total", "expr": "price * qty",
        "sample": {"price": 10, "qty": 10}})
    assert r.json == {"ok": True, "refs": ["price", "qty"], "volatile": False, "value": 100}
    assert client.post("/api/formula/check", headers=H, json={"fields": [], "expr": "zzz"}).json["ok"] is False


# ============================================== regressions from the audit

def test_migration_from_v2_preserves_and_reindexes(tmp_path):
    """0003 rebuilds section_fields and the FTS index; data must survive."""
    db = tmp_path / "old.db"
    c = connect(db)
    from datavault import db as dbmod
    real = dbmod._discover
    dbmod._discover = lambda: [m for m in real() if m[0] <= 2]
    try:
        migrate(c)
    finally:
        dbmod._discover = real
    c.execute("INSERT INTO contacts (first_name) VALUES ('Legacy')")
    c.execute("INSERT INTO sections (name, slug) VALUES ('S', 's')")
    c.execute("INSERT INTO section_fields (section_id, key, label, type) VALUES (1, 'a', 'A', 'text')")
    assert migrate(c)[0] == "0003_formulas_and_perf"  # plus any later migrations
    assert repo.search(c, "legacy")[0]["ref_id"] == 1
    assert c.execute("SELECT key FROM section_fields").fetchone()[0] == "a"
    repo.update_contact(c, 1, {"first_name": "Renamed"})  # rowid-based FTS triggers work
    assert repo.search(c, "renamed") and not repo.search(c, "legacy")


def test_concurrent_migration_is_skipped_under_lock(cfg):
    a = open_db(cfg)
    b = connect(cfg.db_path)
    assert migrate(b) == []  # already applied: no "table exists" crash
    a.close()


def test_readonly_console_works_with_odd_paths(tmp_path):
    cfg = Config.from_env(tmp_path / "my vault #1?")
    open_db(cfg).close()
    assert QueryRunner(cfg).run("SELECT 1 AS x")["rows"] == [[1]]


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1e400", 10 ** 20, True, [1], {"a": 1}])
def test_bad_amounts_are_validation_errors(raw):
    with pytest.raises(ValueError):
        cents(raw)


def test_non_finite_numbers_rejected():
    for raw in ("nan", "inf", float("nan")):
        with pytest.raises(ValueError):
            number(raw)


def test_dates_are_zero_padded_and_bounded():
    assert iso_date("19900415") == "1990-04-15"
    with pytest.raises(ValueError):
        iso_date("0005-01-01")


def test_unicode_section_values_are_searchable_and_filterable(conn):
    s = repo.create_section(conn, {"name": "People", "fields": [
        {"label": "Name"}, {"label": "City", "type": "select", "options": "Zürich,Köln"}]})
    repo.create_record(conn, s["slug"], {"name": "Zoë", "city": "Zürich"})
    assert "Zoë" in conn.execute("SELECT data FROM section_records").fetchone()[0]  # not ë
    assert repo.list_records(conn, s["slug"], q="zoë")["total"] == 1
    assert repo.list_records(conn, s["slug"], filters={"city": "Zürich"})["total"] == 1
    assert repo.list_records(conn, s["slug"], q="city")["total"] == 0  # JSON keys aren't searched


def test_birthday_reminders_including_leap_day(conn):
    soon = date.today() + timedelta(days=3)
    repo.create_contact(conn, {"first_name": "Soon", "birthday": soon.replace(year=2000).isoformat()
                               if not (soon.month == 2 and soon.day == 29) else "2000-02-29"})
    repo.create_contact(conn, {"first_name": "Leap", "birthday": "2000-02-29"})
    names = {b["name"]: b["days_until"] for b in repo.stats(conn)["upcoming_birthdays"]}
    assert names["Soon"] == 3
    # the month/day-offset construction maps Feb 29 to Mar 1 in a non-leap year
    assert conn.execute("SELECT date('2027-01-01', '+1 months', '+28 days')").fetchone()[0] == "2027-03-01"


def test_expense_reimport_skips_duplicates_but_keeps_same_day_repeats(conn):
    data = "Date,Merchant,Amount\n2026-03-01,Cafe,4.50\n2026-03-01,Cafe,4.50\n"
    assert transfer.import_csv(conn, "expenses", data)["imported"] == 2   # two coffees, same file: both kept
    rep = transfer.import_csv(conn, "expenses", data)                       # same statement imported again
    assert rep["imported"] == 0 and rep["duplicates"] == 2


def test_bad_mapping_is_rejected(conn):
    with pytest.raises(ValidationError):
        transfer.import_csv(conn, "contacts", "Name\nA\n", mapping=["Name"])
    with pytest.raises(ValidationError):
        transfer.import_csv(conn, "contacts", "Name\nA\n", mapping={"Name": "password"})


def test_csv_exports_neutralise_formula_injection(conn):
    repo.create_contact(conn, {"first_name": "=HYPERLINK(\"http://evil\")", "notes": "-5 is fine? @SUM(A1)"})
    repo.create_expense(conn, {"spent_on": "2026-01-01", "amount": "-12.50", "merchant": "+cmd"})
    contacts = list(csv.DictReader(io.StringIO(transfer.export_csv(conn, "contacts"))))
    assert contacts[0]["first_name"].startswith("'=") and contacts[0]["notes"].startswith("'-5")
    exp = list(csv.DictReader(io.StringIO(transfer.export_csv(conn, "expenses"))))
    assert exp[0]["merchant"] == "'+cmd" and exp[0]["amount"] == "12.50"


def test_codes_accept_aliases_and_enforce_real_qr_capacity(conn):
    c, _ = codes.save_code(conn, {"kind": "QR Code", "payload": "hi"})
    assert c["kind"] == "qr"
    with pytest.raises(ValidationError):
        codes.normalize_payload("qr", "x" * 2400)  # used to pass validation then crash the renderer
    with pytest.raises(ValidationError):
        codes.save_code(conn, {"kind": ["qr"], "payload": "x"})


def test_truncated_image_leaves_no_orphan(conn, cfg):
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (10, 200, 30)).save(buf, "JPEG")
    data = buf.getvalue()[:-600]
    with pytest.raises(ValidationError):
        media.ingest(conn, cfg, data, "broken.jpg")
    assert not any(cfg.media_dir.rglob("*.jpg"))


def test_bogus_exif_date_ignored(conn, cfg):
    img = Image.new("RGB", (20, 20))
    exif = Image.Exif()
    exif[306] = "0000:00:00 00:00:00"
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    pic, _ = media.ingest(conn, cfg, buf.getvalue(), "x.jpg")
    assert pic["taken_at"] is None


def test_backups_in_same_second_dont_overwrite(conn, cfg):
    a = transfer.backup(conn, cfg, include_media=False)
    b = transfer.backup(conn, cfg, include_media=False)
    assert a != b and a.exists() and b.exists()


def test_restore_rejects_non_database(cfg, conn, tmp_path):
    import zipfile
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("datavault.db", b"not sqlite at all" * 100)
    repo.create_contact(conn, {"first_name": "Survivor"})
    conn.close()
    with pytest.raises(ValidationError):
        transfer.restore(cfg, bad)
    c = open_db(cfg)
    assert c.execute("SELECT first_name FROM contacts").fetchone()[0] == "Survivor"


def test_console_trailing_comment_and_double_explain(cfg, conn):
    q = QueryRunner(cfg)
    assert q.run("SELECT 1\n-- trailing comment")["rows"] == [[1]]
    assert q.run("EXPLAIN QUERY PLAN SELECT 1", explain=True)["columns"]
    with pytest.raises(ValidationError):
        q.run("SELECT ?", params=[{"nested": 1}])


@pytest.mark.parametrize("url", ["/api/expenses/summary?months=abc", "/api/expenses/summary?month=2026-13",
                                 "/api/search?q=x&limit=abc", "/api/contacts?favorite=maybe",
                                 "/api/expenses?category_id=abc", "/api/expenses?min_amount=NaN"])
def test_bad_query_params_are_400_not_500(client, url):
    assert client.get(url).status_code == 400


def test_api_returns_json_for_unknown_routes_and_bad_mapping(client):
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.is_json
    r = client.post("/api/import/contacts", headers=H, content_type="multipart/form-data",
                    data={"file": (io.BytesIO(b"Name\nA\n"), "c.csv"), "mapping": "{not json"})
    assert r.status_code == 400


def test_tags_validation_reports_on_tags_field(conn):
    with pytest.raises(ValidationError) as e:
        repo.create_contact(conn, {"first_name": "T", "tags": ["x" * 41]})
    assert "tags" in e.value.errors
    with pytest.raises(ValidationError):
        repo.create_contact(conn, {"first_name": "T", "tags": 5})


def test_search_index_uses_rowid_lookup(conn):
    plan = " ".join(r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN DELETE FROM search_index WHERE rowid = 8 + 1"))
    assert "SCAN" not in plan.upper() or "VIRTUAL TABLE INDEX" in plan.upper()


def test_monthly_summary_uses_date_index(conn):
    plan = " ".join(r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT SUM(amount_cents) FROM expenses WHERE spent_on >= '2026-03-01' AND spent_on < '2026-04-01'"))
    assert "idx_expenses_date" in plan or "idx_expenses_category_date" in plan
