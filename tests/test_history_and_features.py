"""Change history / undelete, contact merge, recurring charges, section CSV
import, phone-photo formats, label-scoped backup pruning."""
import io
from datetime import date, timedelta

import pytest
from PIL import Image

from datavault import history, media, repo, transfer
from datavault.cli import main as cli_main
from datavault.config import Config
from datavault.db import open_db
from datavault.validation import NotFound, ValidationError
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


# ---------------------------------------------------------------- history

def test_history_records_field_level_diffs(conn):
    c = repo.create_contact(conn, {"first_name": "Ada", "email": "ada@old.io"})
    repo.update_contact(conn, c["id"], {"email": "ada@new.io", "company": "Analytical"})
    h = history.history(conn, "contacts", c["id"])
    assert [e["action"] for e in h] == ["UPDATE", "INSERT"]
    changes = {x["field"]: (x["old"], x["new"]) for x in h[0]["changes"]}
    assert changes == {"email": ("ada@old.io", "ada@new.io"), "company": ("", "Analytical")}


def test_tag_changes_are_in_history(conn):
    c = repo.create_contact(conn, {"first_name": "T", "tags": "a, b"})
    repo.update_contact(conn, c["id"], {"tags": "b, c"})
    assert history.history(conn, "contacts", c["id"])[0]["changes"] == [{"field": "tags", "old": ["a", "b"], "new": ["b", "c"]}]
    repo.update_contact(conn, c["id"], {"tags": "c, b"})  # same set, different order: no entry
    assert len(history.history(conn, "contacts", c["id"])) == 2


def test_section_record_history_diffs_inside_json(conn):
    s = repo.create_section(conn, {"name": "B", "fields": [{"label": "Title"}, {"label": "Pages", "type": "number"}]})
    r = repo.create_record(conn, s["slug"], {"title": "Dune", "pages": 400})
    repo.update_record(conn, r["id"], {"pages": 412})
    assert history.history(conn, "section_records", r["id"])[0]["changes"] == [{"field": "pages", "old": 400, "new": 412}]


def test_undelete_contact_with_tags_and_references(conn):
    pic_id = conn.execute("""INSERT INTO pictures (sha256, stored_name, original_name, mime, size_bytes)
                             VALUES ('x', 'x.png', 'x.png', 'image/png', 1)""").lastrowid
    c = repo.create_contact(conn, {"first_name": "Grace", "tags": "navy, pioneer", "photo_id": pic_id})
    repo.delete_contact(conn, c["id"])
    conn.execute("DELETE FROM pictures WHERE id = ?", (pic_id,))  # its photo is gone too
    deleted = history.recently_deleted(conn)
    assert deleted[0]["table"] == "contacts" and deleted[0]["label"] == "Grace"
    r = history.restore_deleted(conn, deleted[0]["audit_id"])
    back = repo.get_contact(conn, c["id"])
    assert back["first_name"] == "Grace" and {t["name"] for t in back["tags"]} == {"navy", "pioneer"}
    assert back["photo_id"] is None and r["notes"]  # dangling FK cleared, and reported
    assert not history.recently_deleted(conn)  # no longer listed
    with pytest.raises(NotFound):
        history.restore_deleted(conn, 999999)


def test_undelete_expense_and_record(conn):
    e = repo.create_expense(conn, {"spent_on": "2026-01-02", "amount": "9.99", "merchant": "Cafe"})
    repo.delete_expense(conn, e["id"])
    s = repo.create_section(conn, {"name": "N", "fields": [{"label": "Body"}]})
    rec = repo.create_record(conn, s["slug"], {"body": "keep me"})
    repo.delete_record(conn, rec["id"])
    for d in history.recently_deleted(conn):
        history.restore_deleted(conn, d["audit_id"])
    assert repo.get_expense(conn, e["id"])["amount_cents"] == 999
    assert repo.get_record(conn, rec["id"])["data"] == {"body": "keep me"}


def test_restore_record_of_deleted_section_is_refused(conn):
    s = repo.create_section(conn, {"name": "Gone", "fields": [{"label": "X"}]})
    repo.create_record(conn, s["slug"], {"x": "1"})
    repo.delete_section(conn, s["id"])  # cascades the record (and logs its delete)
    d = [x for x in history.recently_deleted(conn) if x["table"] == "section_records"]
    with pytest.raises(ValidationError, match="section"):
        history.restore_deleted(conn, d[0]["audit_id"])


def test_history_api_and_prune(client):
    cid = client.post("/api/contacts", json={"first_name": "Al"}, headers=H).json["id"]
    client.patch(f"/api/contacts/{cid}", json={"last_name": "Turing"}, headers=H)
    assert len(client.get(f"/api/history/contacts/{cid}").json) == 2
    assert client.get("/api/history/audit_log/1").status_code == 400
    r = client.post("/api/maintenance/prune-audit", json={"keep_days": 1}, headers=H)
    assert r.status_code == 200 and r.json["deleted"] == 0


# ------------------------------------------------------------------ merge

def test_merge_contacts_moves_everything(conn):
    keep = repo.create_contact(conn, {"first_name": "Sam", "email": "sam@x.io", "tags": "work"})
    dup = repo.create_contact(conn, {"first_name": "Sam", "phone": "555-1234", "company": "Acme", "tags": "friend",
                                     "notes": "met at expo", "favorite": True})
    e = repo.create_expense(conn, {"spent_on": "2026-01-01", "amount": "5", "contact_id": dup["id"]})
    s = repo.create_section(conn, {"name": "Loans", "fields": [{"label": "Who", "type": "contact"}, {"label": "Item"}]})
    rec = repo.create_record(conn, s["slug"], {"who": dup["id"], "item": "ladder"})
    merged = repo.merge_contacts(conn, keep["id"], [dup["id"]])
    assert merged["phone"] == "555-1234" and merged["company"] == "Acme" and merged["email"] == "sam@x.io"
    assert merged["favorite"] == 1 and "met at expo" in merged["notes"]
    assert {t["name"] for t in merged["tags"]} == {"work", "friend"}
    assert repo.get_expense(conn, e["id"])["contact_id"] == keep["id"]
    assert repo.get_record(conn, rec["id"])["data"]["who"] == keep["id"]
    with pytest.raises(NotFound):
        repo.get_contact(conn, dup["id"])
    with pytest.raises(ValidationError):
        repo.merge_contacts(conn, keep["id"], [keep["id"]])


def test_merge_is_atomic(conn):
    keep = repo.create_contact(conn, {"first_name": "A"})
    dup = repo.create_contact(conn, {"first_name": "A", "company": "X"})
    with pytest.raises(NotFound):
        repo.merge_contacts(conn, keep["id"], [dup["id"], 424242])  # second id is bogus
    assert repo.get_contact(conn, dup["id"])  # first merge was rolled back
    assert repo.get_contact(conn, keep["id"])["company"] == ""


# --------------------------------------------------------------- recurring

def _month_start(back: int) -> date:
    d = date.today().replace(day=1)
    for _ in range(back):
        d = (d - timedelta(days=1)).replace(day=1)
    return d


def test_recurring_detects_subscriptions_not_groceries(conn):
    for back in range(6):
        m = _month_start(back)
        repo.create_expense(conn, {"spent_on": m.replace(day=3).isoformat(), "amount": "15.49", "merchant": "Netflix"})
        repo.create_expense(conn, {"spent_on": m.replace(day=5).isoformat(), "amount": str(40 + back * 30), "merchant": "Grocer"})
        repo.create_expense(conn, {"spent_on": m.replace(day=7).isoformat(), "amount": "20", "merchant": "Cafe"})
        repo.create_expense(conn, {"spent_on": m.replace(day=8).isoformat(), "amount": "21", "merchant": "Cafe"})
    r = repo.recurring_charges(conn)
    assert [i["merchant"] for i in r["items"]] == ["Netflix"]
    assert r["items"][0]["avg_cents"] == 1549 and r["yearly_cents"] == 1549 * 12


# -------------------------------------------------------- section CSV import

def test_section_csv_round_trip(conn):
    s = repo.create_section(conn, {"name": "Wine", "fields": [
        {"label": "Name", "required": True}, {"label": "Price", "type": "money"},
        {"label": "Color", "type": "select", "options": "Red,White"},
        {"label": "With tax", "type": "formula", "formula": "price * 1.1", "result": "money"}]})
    repo.create_record(conn, s["slug"], {"name": "Rioja", "price": "18", "color": "Red"})
    exported = transfer.export_csv(conn, f"section:{s['slug']}")
    conn.execute("DELETE FROM section_records")
    rep = transfer.import_csv(conn, f"section:{s['slug']}", exported)
    assert rep["imported"] == 1 and "With tax" in rep["unmapped"]  # formula column is computed, not imported
    rec = repo.list_records(conn, s["slug"])["items"][0]["data"]
    assert rec == {"name": "Rioja", "price": 1800, "color": "Red", "with_tax": 1980}


def test_section_csv_import_validates_rows(conn):
    s = repo.create_section(conn, {"name": "T", "fields": [{"label": "Name", "required": True},
                                                          {"label": "Color", "type": "select", "options": "Red"}]})
    rep = transfer.import_csv(conn, f"section:{s['slug']}", "Name,Color\nA,Red\n,Blue\n")
    assert rep["rolled_back"] and rep["errors"][0]["line"] == 3
    assert set(rep["errors"][0]["errors"]) == {"name", "color"}


def test_section_import_api(client):
    sec = client.post("/api/sections", headers=H, json={"name": "Books", "fields": [{"label": "Title"}]}).json
    r = client.post(f"/api/import/section:{sec['slug']}/headers", headers=H, content_type="multipart/form-data",
                    data={"file": (io.BytesIO(b"Title\nDune\n"), "b.csv")})
    assert r.json["guess"] == {"Title": "title"} and r.json["fields"] == ["title"]


# ----------------------------------------------------------- phone photos

def test_mpo_and_heic_are_accepted(conn, cfg):
    # Most phone/camera JPEGs open as MPO; they used to be rejected outright.
    a, b = Image.new("RGB", (64, 48), (200, 0, 0)), Image.new("RGB", (32, 24), (0, 0, 200))
    buf = io.BytesIO()
    a.save(buf, "MPO", save_all=True, append_images=[b])
    assert Image.open(io.BytesIO(buf.getvalue())).format == "MPO"
    pic, _ = media.ingest(conn, cfg, buf.getvalue(), "IMG_0001.JPG")
    assert pic["mime"] == "image/jpeg" and pic["stored_name"].endswith(".jpg")
    if media.HEIF_SUPPORTED:
        heic = io.BytesIO()
        Image.new("RGB", (80, 60), (0, 150, 0)).save(heic, "HEIF")
        p2, _ = media.ingest(conn, cfg, heic.getvalue(), "IMG_0002.HEIC")
        assert p2["mime"] == "image/heic"
        view, mime = media.ensure_view(cfg, p2)  # browsers can't show HEIC: a JPEG copy is served
        assert mime == "image/jpeg" and Image.open(view).format == "JPEG"


def test_missing_thumbnail_is_rebuilt_on_request(client, cfg):
    buf = io.BytesIO()
    Image.new("RGB", (50, 50), (1, 2, 3)).save(buf, "PNG")
    buf.seek(0)
    pid = client.post("/api/pictures", headers=H, content_type="multipart/form-data",
                      data={"file": (buf, "p.png")}).json["items"][0]["id"]
    for t in cfg.thumbs_dir.glob("*.jpg"):
        t.unlink()
    assert client.get(f"/media/{pid}/thumb").status_code == 200
    assert client.get(f"/media/{pid}/view").status_code == 200


# --------------------------------------------------------- backups / misc

def test_keep_prunes_only_same_label(cfg, conn, capsys):
    manual = transfer.backup(conn, cfg, include_media=False)
    for _ in range(3):
        transfer.backup(conn, cfg, include_media=False, label="auto")
    cli_main(["--home", str(cfg.home), "backup", "--no-media", "--label", "auto", "--keep", "2"])
    assert manual.exists()  # a hand-made backup is never pruned by the nightly job
    assert len(transfer.backups_with_label(cfg, "auto")) == 2


def test_restore_validates_before_making_safety_backup(cfg, conn, tmp_path):
    import zipfile
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("datavault.db", b"junk")
    conn.close()
    with pytest.raises(ValidationError):
        transfer.restore(cfg, bad)
    assert not transfer.backups_with_label(cfg, "pre-restore")  # nothing touched


def test_api_index_lists_endpoints(client):
    paths = {r["path"] for r in client.get("/api").json}
    assert {"/api/contacts", "/api/history/<table>/<int:row_id>", "/api/formula/check"} <= paths
