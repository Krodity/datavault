"""Folders with any mix of items, notes with user-defined fields, files."""
import io
import json
import zipfile

import pytest

from datavault import folders as fo
from datavault import history, repo, transfer
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


# ---------------------------------------------------------------- folders

def test_nested_folders_paths_and_recursive_counts(conn):
    car = fo.create_folder(conn, {"name": "Car"})
    rec = fo.create_folder(conn, {"name": "Receipts", "parent_id": car["id"]})
    deep = fo.create_folder(conn, {"name": "2026", "parent_id": rec["id"]})
    assert [p["name"] for p in fo.get_folder(conn, deep["id"])["path"]] == ["Car", "Receipts", "2026"]
    n = fo.create_note(conn, {"title": "x", "folder_id": deep["id"]})
    fo.add_items(conn, car["id"], [{"item_type": "note", "item_id": n["id"]}])  # same item twice in the tree
    counts = {f["name"]: (f["item_count"], f["total_items"]) for f in fo.list_folders(conn)}
    assert counts == {"Car": (1, 1), "Receipts": (0, 1), "2026": (1, 1)}  # total is de-duplicated


def test_folder_cannot_move_into_itself_or_descendant(conn):
    a = fo.create_folder(conn, {"name": "A"})
    b = fo.create_folder(conn, {"name": "B", "parent_id": a["id"]})
    with pytest.raises(ValidationError, match="inside itself"):
        fo.update_folder(conn, a["id"], {"parent_id": b["id"]})
    with pytest.raises(ValidationError, match="inside itself"):
        fo.update_folder(conn, a["id"], {"parent_id": a["id"]})
    assert fo.update_folder(conn, b["id"], {"parent_id": None})["parent_id"] is None


def test_any_mix_of_items_in_one_folder(conn, cfg):
    f = fo.create_folder(conn, {"name": "Mix"})
    c = repo.create_contact(conn, {"first_name": "Ann", "company": "Acme"})
    e = repo.create_expense(conn, {"spent_on": "2026-01-01", "amount": "12.5", "merchant": "Shop"})
    s = repo.create_section(conn, {"name": "Books", "fields": [{"label": "Title"}]})
    r = repo.create_record(conn, s["slug"], {"title": "Dune"})
    n = fo.create_note(conn, {"title": "Idea"})
    file, _ = fo.ingest_file(conn, cfg, b"%PDF-1.4 fake", "manual.pdf")
    res = fo.add_items(conn, f["id"], [{"item_type": "contact", "item_id": c["id"]},
                                       {"item_type": "expense", "item_id": e["id"]},
                                       {"item_type": "record", "item_id": r["id"]},
                                       {"item_type": "note", "item_id": n["id"]},
                                       {"item_type": "file", "item_id": file["id"]},
                                       {"item_type": "note", "item_id": n["id"]}])  # duplicate ignored
    assert res["added"] == 5
    got = fo.get_folder(conn, f["id"])
    assert [i["item_type"] for i in got["items"]] == ["contact", "expense", "record", "note", "file"]
    assert got["items"][0]["subtitle"] == "Acme" and got["items"][1]["title"] == "Shop · $12.50"
    assert got["items"][2]["title"] == "Dune" and got["items"][4]["subtitle"].endswith("application/pdf")
    assert [i["item_type"] for i in fo.get_folder(conn, f["id"], item_type="note")["items"]] == ["note"]
    with pytest.raises(ValidationError):
        fo.add_items(conn, f["id"], [{"item_type": "contact", "item_id": 9999}])
    with pytest.raises(ValidationError):
        fo.add_items(conn, f["id"], [{"item_type": "users", "item_id": 1}])


def test_deleting_items_cleans_links_and_deleting_folders_keeps_items(conn):
    f = fo.create_folder(conn, {"name": "F"})
    sub = fo.create_folder(conn, {"name": "Sub", "parent_id": f["id"]})
    c = repo.create_contact(conn, {"first_name": "Bo"})
    fo.add_items(conn, sub["id"], [{"item_type": "contact", "item_id": c["id"]}])
    repo.delete_contact(conn, c["id"])  # polymorphic link removed by trigger
    assert conn.execute("SELECT COUNT(*) FROM folder_items").fetchone()[0] == 0
    n = fo.create_note(conn, {"title": "survivor", "folder_id": sub["id"]})
    assert fo.delete_folder(conn, f["id"]) == {"deleted_subfolders": 1}
    assert fo.get_note(conn, n["id"])["folders"] == []  # the note itself is untouched


def test_move_and_set_memberships(conn):
    a, b = fo.create_folder(conn, {"name": "A"}), fo.create_folder(conn, {"name": "B"})
    n = fo.create_note(conn, {"title": "n", "folder_id": a["id"]})
    fo.move_item(conn, a["id"], b["id"], "note", n["id"])
    assert [f["name"] for f in fo.folders_of(conn, "note", n["id"])] == ["B"]
    assert [f["name"] for f in fo.set_item_folders(conn, "note", n["id"], [a["id"], b["id"]])] == ["A", "B"]
    with pytest.raises(NotFound):
        fo.remove_item(conn, a["id"], "contact", 1)


# ----------------------------------------------------------------- notes

def test_notes_with_custom_typed_fields(conn):
    n = fo.create_note(conn, {"title": "Civic", "properties": [
        {"name": "VIN", "type": "text", "value": "2HG123"}, {"name": "Paid", "type": "money", "value": "$1,250.50"},
        {"name": "Due", "type": "date", "value": "03/15/2027"}, {"name": "Insured", "type": "boolean", "value": "yes"},
        {"name": "Miles", "type": "number", "value": "48,210"}, {"name": "Notes link", "type": "url", "value": "example.com"}]})
    p = {x["name"]: x["value"] for x in n["properties"]}
    assert p == {"VIN": "2HG123", "Paid": 125050, "Due": "2027-03-15", "Insured": True, "Miles": 48210,
                 "Notes link": "https://example.com"}
    with pytest.raises(ValidationError, match="Miles"):
        fo.update_note(conn, n["id"], {"properties": [{"name": "Miles", "type": "number", "value": "lots"}]})
    with pytest.raises(ValidationError, match="two fields"):
        fo.update_note(conn, n["id"], {"properties": [{"name": "a", "type": "text"}, {"name": "A", "type": "text"}]})
    with pytest.raises(ValidationError):
        fo.update_note(conn, n["id"], {"properties": [{"name": "x", "type": "javascript"}]})


def test_folder_template_seeds_new_notes(conn):
    f = fo.create_folder(conn, {"name": "Vehicles", "template": [{"name": "Make", "type": "text"},
                                                                {"name": "Insurance due", "type": "date"}]})
    n = fo.create_note(conn, {"title": "Truck", "folder_id": f["id"],
                              "properties": [{"name": "make", "type": "text", "value": "Ford"}]})
    assert [(p["name"], p["value"]) for p in n["properties"]] == [("make", "Ford"), ("Insurance due", None)]
    assert n["folders"][0]["name"] == "Vehicles"


def test_note_fields_are_searchable_and_filterable(conn):
    fo.create_note(conn, {"title": "Router", "properties": [{"name": "Serial", "type": "text", "value": "XK-99812"}]})
    fo.create_note(conn, {"title": "Other", "folder_id": fo.create_folder(conn, {"name": "F"})["id"]})
    assert repo.search(conn, "XK-99812")[0]["kind"] == "note"
    assert fo.list_notes(conn, q="99812")["total"] == 1
    assert [n["title"] for n in fo.list_notes(conn, unfiled=1)["items"]] == ["Router"]


def test_deleted_note_restores_into_its_folders(conn):
    f = fo.create_folder(conn, {"name": "Keep"})
    n = fo.create_note(conn, {"title": "Oops", "folder_id": f["id"], "body": "important"})
    fo.delete_note(conn, n["id"])
    d = [x for x in history.recently_deleted(conn) if x["table"] == "notes"][0]
    assert d["label"] == "Oops"
    history.restore_deleted(conn, d["audit_id"])
    back = fo.get_note(conn, n["id"])
    assert back["body"] == "important" and [x["name"] for x in back["folders"]] == ["Keep"]


# ----------------------------------------------------------------- files

def test_files_dedup_type_from_extension_and_unsafe_names(conn, cfg):
    a, new_a = fo.ingest_file(conn, cfg, b"hello", "../../etc/notes.txt")
    b, new_b = fo.ingest_file(conn, cfg, b"hello", "copy.txt")
    assert new_a and not new_b and a["id"] == b["id"]
    assert a["original_name"] == "notes.txt" and a["mime"] == "text/plain" and a["inline"]
    page, _ = fo.ingest_file(conn, cfg, b"<script>alert(1)</script>", "evil.html")
    assert page["mime"] == "text/html" and not page["inline"]
    with pytest.raises(ValidationError):
        fo.ingest_file(conn, cfg, b"", "empty.bin")


def test_file_download_never_renders_html_inline(client):
    r = client.post("/api/files", headers=H, content_type="multipart/form-data",
                    data={"file": (io.BytesIO(b"<script>alert(1)</script>"), "evil.html")})
    fid = r.json["items"][0]["id"]
    v = client.get(f"/files/{fid}/view")
    assert v.headers["Content-Type"] == "application/octet-stream"
    assert v.headers["Content-Disposition"].startswith("attachment")
    assert "sandbox" in v.headers["Content-Security-Policy"]
    r = client.post("/api/files", headers=H, content_type="multipart/form-data",
                    data={"file": (io.BytesIO(b"plain"), "a.txt")})
    t = client.get(f"/files/{r.json['items'][0]['id']}/view")
    assert t.headers["Content-Type"].startswith("text/plain") and "attachment" not in t.headers.get("Content-Disposition", "")


def test_files_are_backed_up_and_restored(cfg, conn):
    f, _ = fo.ingest_file(conn, cfg, b"tax return pdf bytes", "taxes.pdf")
    path = transfer.backup(conn, cfg)
    with zipfile.ZipFile(path) as z:
        assert any(n.startswith("files/") for n in z.namelist())
    fo.file_path(cfg, f["stored_name"]).unlink()
    conn.close()
    transfer.restore(cfg, path)
    assert fo.file_path(cfg, f["stored_name"]).read_bytes() == b"tax return pdf bytes"


# ------------------------------------------------------------------- API

def test_folder_api_flow(client):
    f = client.post("/api/folders", headers=H, json={"name": "Pets", "icon": "🐶",
                                                     "template": [{"name": "Vet", "type": "phone"}]}).json
    n = client.post("/api/notes", headers=H, json={"title": "Rex", "folder_id": f["id"]}).json
    assert n["properties"] == [{"name": "Vet", "type": "phone", "value": None}]
    c = client.post("/api/contacts", headers=H, json={"first_name": "Dr. Lee"}).json
    assert client.post(f"/api/folders/{f['id']}/items", headers=H,
                       json={"items": [{"item_type": "contact", "item_id": c["id"]}]}).json == {"added": 1}
    got = client.get(f"/api/folders/{f['id']}").json
    assert {i["item_type"] for i in got["items"]} == {"note", "contact"} and got["type_counts"] == {"contact": 1, "note": 1}
    assert client.get(f"/api/folders/{f['id']}?q=lee").json["items"][0]["title"] == "Dr. Lee"
    looked = client.get("/api/items/lookup?type=contact&q=lee").json
    assert looked[0]["id"] == c["id"]
    assert client.get("/api/items/lookup?type=nope").status_code == 400
    assert client.delete(f"/api/folders/{f['id']}/items/contact/{c['id']}", headers=H).status_code == 204


def test_seed_builds_demo_folders(tmp_path, capsys):
    cli_main(["--home", str(tmp_path / "demo"), "seed"])
    c = open_db(Config.from_env(tmp_path / "demo"))
    names = {f["name"]: f for f in fo.list_folders(c)}
    assert {"Car", "Fuel receipts", "Home", "Recipes"} <= set(names)
    car = fo.get_folder(c, names["Car"]["id"])
    assert {i["item_type"] for i in car["items"]} == {"note", "file", "contact"}
    assert json.loads(json.dumps(car))  # serialisable
