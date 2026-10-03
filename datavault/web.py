"""Flask REST API + static single-page frontend."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, abort, g, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from . import codes, folders, formulas, history, media, repo, transfer
from .config import Config
from .db import connect, migrate
from .query import EXAMPLES, QueryRunner
from .validation import NotFound, ValidationError

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


def create_app(cfg: Config | None = None, *, allowed_hosts: set[str] | None = None) -> Flask:
    cfg = cfg or Config.from_env()
    cfg.ensure_dirs()
    c = connect(cfg.db_path)  # run migrations once at startup
    migrate(c)
    c.close()

    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
    app.config["DV"] = cfg
    hosts = LOCAL_HOSTS | (allowed_hosts or set())
    runner = QueryRunner(cfg)

    def db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = connect(cfg.db_path)
        return g.db

    @app.teardown_appcontext
    def _close(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.before_request
    def _guard():
        # DNS-rebinding defence: a hostile page can't talk to us through a
        # hostname it controls, because we only answer to known Host headers.
        host = (request.host or "").rsplit(":", 1)[0]
        if host not in hosts and not app.testing:
            abort(421)
        # CSRF defence: state-changing requests must carry a header a
        # cross-site <form> cannot set.
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get("X-DataVault") != "1":
            abort(403, "missing X-DataVault header")

    # ----------------------------------------------------------- errors
    @app.errorhandler(ValidationError)
    def _bad(e):
        return jsonify(error="validation", message=str(e), fields=e.errors), 400

    @app.errorhandler(NotFound)
    def _nf(e):
        return jsonify(error="not_found", message=str(e)), 404

    @app.errorhandler(sqlite3.IntegrityError)
    def _conflict(e):
        return jsonify(error="conflict", message=str(e)), 409

    @app.errorhandler(413)
    def _too_big(_e):
        return jsonify(error="too_large", message="upload exceeds 200 MB"), 413

    @app.errorhandler(HTTPException)
    def _http(e):
        # API clients always get JSON, never an HTML error page
        if request.path.startswith("/api/"):
            return jsonify(error=e.name.lower().replace(" ", "_"), message=e.description), e.code
        return e

    @app.errorhandler(Exception)
    def _unexpected(e):
        app.logger.exception("unhandled error on %s %s", request.method, request.path)
        return jsonify(error="internal", message="unexpected server error — see the server log"), 500

    def body() -> dict:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValidationError("expected a JSON object body")
        return data

    def args(*names) -> dict:
        return {n: request.args.get(n) for n in names if request.args.get(n) not in (None, "")}

    def created(obj, was_created=True):
        return jsonify(obj), (201 if was_created else 200)

    # ------------------------------------------------------------ frontend
    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        resp = send_from_directory(STATIC, name)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                                "style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'")
        return resp

    # ------------------------------------------------------------ overview
    @app.get("/api/stats")
    def stats():
        return jsonify(repo.stats(db()))

    @app.get("/api/search")
    def search():
        return jsonify(repo.search(db(), request.args.get("q", ""), request.args.get("limit", 30),
                                   request.args.get("kind", "")))

    # ------------------------------------------------------------ contacts
    @app.get("/api/contacts")
    def contacts_list():
        return jsonify(repo.list_contacts(db(), **args("q", "tag", "favorite", "sort", "limit", "offset")))

    @app.post("/api/contacts")
    def contacts_create():
        return created(repo.create_contact(db(), body()))

    @app.get("/api/contacts/duplicates")
    def contacts_dupes():
        return jsonify(repo.find_duplicate_contacts(db()))

    @app.post("/api/contacts/<int:cid>/merge")
    def contacts_merge(cid):
        return jsonify(repo.merge_contacts(db(), cid, body().get("merge_ids")))

    @app.get("/api/contacts/<int:cid>")
    def contacts_get(cid):
        return jsonify(repo.get_contact(db(), cid))

    @app.patch("/api/contacts/<int:cid>")
    def contacts_update(cid):
        return jsonify(repo.update_contact(db(), cid, body()))

    @app.delete("/api/contacts/<int:cid>")
    def contacts_delete(cid):
        repo.delete_contact(db(), cid)
        return "", 204

    @app.get("/api/contacts/<int:cid>/vcard")
    def contacts_vcard(cid):
        c = repo.get_contact(db(), cid)
        return Response(transfer.contact_vcard(c), mimetype="text/vcard",
                        headers={"Content-Disposition": f'attachment; filename="contact-{cid}.vcf"'})

    @app.get("/api/contacts/<int:cid>/qr.png")
    def contacts_qr(cid):
        png, mime = codes.render("qr", transfer.contact_vcard(repo.get_contact(db(), cid)), "png", 8)
        return Response(png, mimetype=mime)

    @app.get("/api/tags")
    def tags_list():
        return jsonify(repo.list_tags(db()))

    @app.post("/api/tags")
    def tags_create():
        b = body()
        conn = db()
        tid = repo.upsert_tag(conn, b.get("name", ""), b.get("color"))
        return created(repo.one(conn, "SELECT * FROM tags WHERE id = ?", (tid,)))

    @app.delete("/api/tags/<int:tid>")
    def tags_delete(tid):
        repo.delete_tag(db(), tid)
        return "", 204

    # ------------------------------------------------------------ expenses
    @app.get("/api/expenses")
    def expenses_list():
        return jsonify(repo.list_expenses(db(), **args("q", "category_id", "date_from", "date_to", "min_amount",
                                                         "max_amount", "contact_id", "sort", "limit", "offset")))

    @app.post("/api/expenses")
    def expenses_create():
        return created(repo.create_expense(db(), body()))

    @app.get("/api/expenses/summary")
    def expenses_summary():
        return jsonify(repo.expense_summary(db(), request.args.get("month"), request.args.get("months", 12)))

    @app.get("/api/expenses/recurring")
    def expenses_recurring():
        return jsonify(repo.recurring_charges(db(), request.args.get("months", 12)))

    @app.get("/api/expenses/<int:eid>")
    def expenses_get(eid):
        return jsonify(repo.get_expense(db(), eid))

    @app.patch("/api/expenses/<int:eid>")
    def expenses_update(eid):
        return jsonify(repo.update_expense(db(), eid, body()))

    @app.delete("/api/expenses/<int:eid>")
    def expenses_delete(eid):
        repo.delete_expense(db(), eid)
        return "", 204

    @app.get("/api/categories")
    def categories_list():
        return jsonify(repo.list_categories(db()))

    @app.post("/api/categories")
    def categories_create():
        return created(repo.create_category(db(), body()))

    @app.patch("/api/categories/<int:cid>")
    def categories_update(cid):
        return jsonify(repo.update_category(db(), cid, body()))

    @app.delete("/api/categories/<int:cid>")
    def categories_delete(cid):
        repo.delete_category(db(), cid)
        return "", 204

    # ------------------------------------------------------------ pictures
    @app.get("/api/pictures")
    def pictures_list():
        return jsonify(repo.list_pictures(db(), **args("q", "album", "limit", "offset")))

    @app.post("/api/pictures")
    def pictures_upload():
        files = request.files.getlist("file")
        if not files:
            raise ValidationError({"file": "no file uploaded"})
        meta = {k: request.form.get(k, "") for k in ("title", "description", "album")}
        results, errors = [], []
        for f in files:
            try:
                pic, new = media.ingest(db(), cfg, f.read(), f.filename or "upload", meta if len(files) == 1 else
                                        {"album": meta["album"]})
                results.append({**pic, "duplicate": not new})
            except ValidationError as e:
                errors.append({"name": f.filename, "errors": e.errors})
        if not results and errors:
            raise ValidationError(errors[0]["errors"])
        return jsonify(items=results, errors=errors), 201

    @app.get("/api/pictures/<int:pid>")
    def pictures_get(pid):
        return jsonify(repo.get_picture(db(), pid))

    @app.patch("/api/pictures/<int:pid>")
    def pictures_update(pid):
        return jsonify(repo.update_picture(db(), pid, body()))

    @app.delete("/api/pictures/<int:pid>")
    def pictures_delete(pid):
        media.delete_picture(db(), cfg, pid)
        return "", 204

    @app.get("/media/<int:pid>/<kind>")
    def media_file(pid, kind):
        pic = repo.get_picture(db(), pid)
        if kind == "thumb":
            path, mime = media.ensure_thumb(cfg, pic), "image/jpeg"
        elif kind == "view":
            path, mime = media.ensure_view(cfg, pic) or (None, None)
        elif kind == "file":
            path, mime = media.media_path(cfg, pic["stored_name"]), pic["mime"]
        else:
            abort(404)
        if path is None or not path.exists():
            abort(404)
        # content-addressed, so it can be cached forever
        return send_file(path, mimetype=mime, download_name=pic["original_name"],
                         as_attachment=request.args.get("download") == "1", max_age=31536000)

    # --------------------------------------------------------------- codes
    @app.get("/api/codes")
    def codes_list():
        return jsonify(codes.list_codes(db(), **args("q", "kind", "limit", "offset")))

    @app.post("/api/codes")
    def codes_create():
        b = body()
        if b.get("wifi"):
            w = b.pop("wifi")
            b["payload"] = codes.wifi_payload(w.get("ssid", ""), w.get("password", ""), w.get("security", "WPA"),
                                              bool(w.get("hidden")))
            b["kind"] = "qr"
        code, new = codes.save_code(db(), b)
        return created(code, new)

    @app.get("/api/codes/kinds")
    def codes_kinds():
        return jsonify(codes.KINDS)

    @app.get("/api/codes/preview")
    def codes_preview():
        kind = codes.normalize_kind(request.args.get("kind", "qr"))
        payload = codes.normalize_payload(kind, request.args.get("payload", ""))
        fmt = "svg" if request.args.get("format") == "svg" else "png"
        data, mime = codes.render(kind, payload, fmt)
        return Response(data, mimetype=mime, headers={"Cache-Control": "no-store"})

    @app.post("/api/codes/decode")
    def codes_decode():
        f = request.files.get("file")
        if not f:
            raise ValidationError({"file": "no image uploaded"})
        found = codes.decode_image(f.read())
        saved = []
        if request.form.get("save") == "1":
            for item in found:
                if item["kind"]:
                    code, _ = codes.save_code(db(), {"kind": item["kind"], "payload": item["payload"], "source": "scanned"})
                    saved.append(code)
        return jsonify(found=[{**i, "info": codes.describe_payload(i["kind"] or "", i["payload"])} for i in found],
                       saved=saved)

    @app.get("/api/codes/<int:code_id>")
    def codes_get(code_id):
        return jsonify(codes.get_code(db(), code_id))

    @app.get("/api/codes/<int:code_id>/image.<fmt>")
    def codes_image(code_id, fmt):
        c = codes.get_code(db(), code_id)
        data, mime = codes.render(c["kind"], c["payload"], "svg" if fmt == "svg" else "png")
        dl = request.args.get("download") == "1"
        name = f"{c['kind']}-{code_id}.{'svg' if fmt == 'svg' else 'png'}"
        return Response(data, mimetype=mime,
                        headers={"Content-Disposition": f'{"attachment" if dl else "inline"}; filename="{name}"'})

    @app.patch("/api/codes/<int:code_id>")
    def codes_update(code_id):
        return jsonify(codes.update_code(db(), code_id, body()))

    @app.delete("/api/codes/<int:code_id>")
    def codes_delete(code_id):
        codes.delete_code(db(), code_id)
        return "", 204

    # ------------------------------------------------------------ sections
    @app.get("/api/sections")
    def sections_list():
        return jsonify(repo.list_sections(db()))

    @app.post("/api/sections")
    def sections_create():
        return created(repo.create_section(db(), body()))

    @app.get("/api/sections/<ref>")
    def sections_get(ref):
        return jsonify(repo.get_section(db(), ref))

    @app.put("/api/sections/<int:sid>")
    def sections_update(sid):
        return jsonify(repo.update_section(db(), sid, body()))

    @app.delete("/api/sections/<int:sid>")
    def sections_delete(sid):
        repo.delete_section(db(), sid)
        return "", 204

    @app.get("/api/sections/<ref>/records")
    def records_list(ref):
        filters = {k[2:]: val for k, val in request.args.items() if k.startswith("f.") and val != ""}
        return jsonify(repo.list_records(db(), ref, q=request.args.get("q", ""), sort=request.args.get("sort", ""),
                                         desc=request.args.get("desc") == "1", filters=filters,
                                         limit=request.args.get("limit", 100), offset=request.args.get("offset", 0)))

    @app.post("/api/sections/<ref>/records")
    def records_create(ref):
        return created(repo.create_record(db(), ref, body()))

    @app.get("/api/records/<int:rid>")
    def records_get(rid):
        return jsonify(repo.get_record(db(), rid))

    @app.post("/api/sections/<int:sid>/recalculate")
    def sections_recalc(sid):
        repo.get_section(db(), sid)
        return jsonify(changed=repo.recalculate_section(db(), sid))

    @app.get("/api/formula/functions")
    def formula_functions():
        return jsonify(formulas.function_help())

    @app.post("/api/formula/check")
    def formula_check():
        """Validate one formula against a (possibly unsaved) field list and
        evaluate it on sample values, for live feedback in the field builder."""
        b = body()
        fields = b.get("fields") or []
        if not isinstance(fields, list):
            raise ValidationError({"fields": "must be a list"})
        keys = {repo.v.field_key(str(f.get("key") or f.get("label") or "")) for f in fields if isinstance(f, dict)}
        key = repo.v.field_key(str(b.get("key") or b.get("label") or "result"))
        try:
            f = formulas.compile_formula(key, b.get("expr", ""), b.get("result", "number"), keys - {key})
        except formulas.FormulaError as e:
            return jsonify(ok=False, error=str(e))
        sample = b.get("sample") if isinstance(b.get("sample"), dict) else {}
        env = {k: (formulas._num(val) if isinstance(val, (int, float)) and not isinstance(val, bool) else val)
               for k, val in sample.items() if k in keys}
        return jsonify(ok=True, refs=sorted(f.refs), volatile=f.volatile, value=formulas.evaluate(f, env))

    @app.patch("/api/records/<int:rid>")
    def records_update(rid):
        return jsonify(repo.update_record(db(), rid, body()))

    @app.delete("/api/records/<int:rid>")
    def records_delete(rid):
        repo.delete_record(db(), rid)
        return "", 204

    # --------------------------------------------------------------- query
    @app.post("/api/query")
    def query_run():
        b = body()
        return jsonify(runner.run(b.get("sql", ""), b.get("params"), explain=bool(b.get("explain"))))

    @app.post("/api/query/csv")
    def query_csv():
        b = body()
        res = runner.run(b.get("sql", ""), b.get("params"))
        return Response(transfer.write_csv(res["columns"], res["rows"]), mimetype="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="query.csv"'})

    @app.get("/api/schema")
    def schema():
        return jsonify(runner.schema())

    @app.get("/api/query/examples")
    def query_examples():
        return jsonify([{"name": n, "sql": s} for n, s in EXAMPLES])

    @app.get("/api/queries")
    def saved_list():
        return jsonify(repo.list_saved_queries(db()))

    @app.post("/api/queries")
    def saved_create():
        return created(repo.save_query(db(), body()))

    @app.delete("/api/queries/<int:qid>")
    def saved_delete(qid):
        repo.delete_saved_query(db(), qid)
        return "", 204

    # ------------------------------------------------------- import/export
    @app.post("/api/import/<entity>")
    def import_entity(entity):
        f = request.files.get("file")
        if not f:
            raise ValidationError({"file": "no file uploaded"})
        text = f.read().decode("utf-8-sig", errors="replace")
        dry = request.form.get("dry_run") == "1"
        if entity == "vcard":
            return jsonify(transfer.import_vcards(db(), text, dry_run=dry))
        try:
            mapping = json.loads(request.form["mapping"]) if request.form.get("mapping") else None
        except json.JSONDecodeError:
            raise ValidationError({"mapping": "is not valid JSON"}) from None
        return jsonify(transfer.import_csv(db(), entity, text, mapping=mapping, dry_run=dry,
                                           skip_errors=request.form.get("skip_errors") == "1"))

    @app.post("/api/import/<entity>/headers")
    def import_headers(entity):
        f = request.files.get("file")
        if not f:
            raise ValidationError({"file": "no file uploaded"})
        aliases = transfer.aliases_for(db(), entity)
        text = f.read().decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text), dialect=transfer._sniff(text))
        headers = next(reader, [])
        sample = [r for _, r in zip(range(3), reader)]
        return jsonify(headers=headers, sample=sample, guess=transfer.guess_mapping(entity, headers, aliases),
                       fields=[k for k in aliases if not k.startswith("_")] + (
                           ["_full_name"] if entity == "contacts" else []))

    @app.get("/api/export/<path:name>")
    def export(name):
        stamp = datetime.now().strftime("%Y%m%d")
        if name == "all.json":
            data = json.dumps(transfer.export_json(db()), indent=2, default=str)
            return Response(data, mimetype="application/json",
                            headers={"Content-Disposition": f'attachment; filename="datavault-{stamp}.json"'})
        if name == "contacts.vcf":
            return Response(transfer.export_vcards(db()), mimetype="text/vcard",
                            headers={"Content-Disposition": f'attachment; filename="contacts-{stamp}.vcf"'})
        if name.endswith(".csv"):
            entity = name[:-4]
            return Response(transfer.export_csv(db(), entity), mimetype="text/csv",
                            headers={"Content-Disposition":
                                     f'attachment; filename="{entity.replace(":", "-")}-{stamp}.csv"'})
        abort(404)

    # ------------------------------------------------------------- backups
    @app.get("/api/backups")
    def backups_list():
        return jsonify(transfer.list_backups(cfg))

    @app.post("/api/backups")
    def backups_create():
        b = request.get_json(silent=True) or {}
        path = transfer.backup(db(), cfg, include_media=b.get("include_media", True), label=b.get("label", ""))
        return created({"name": path.name, "size_bytes": path.stat().st_size})

    @app.get("/api/backups/<name>")
    def backups_download(name):
        if "/" in name or not name.startswith("datavault-") or not name.endswith(".zip"):
            abort(404)
        return send_from_directory(cfg.backups_dir, name, as_attachment=True)

    # ---------------------------------------------------- folders & notes
    @app.get("/api/folders")
    def folders_list():
        return jsonify(folders.list_folders(db()))

    @app.post("/api/folders")
    def folders_create():
        return created(folders.create_folder(db(), body()))

    @app.get("/api/folders/<int:fid>")
    def folders_get(fid):
        """A folder with its breadcrumb path, subfolders and resolved items."""
        return jsonify(folders.get_folder(db(), fid, item_type=request.args.get("type", ""),
                                          q=request.args.get("q", "")))

    @app.patch("/api/folders/<int:fid>")
    def folders_update(fid):
        return jsonify(folders.update_folder(db(), fid, body()))

    @app.delete("/api/folders/<int:fid>")
    def folders_delete(fid):
        return jsonify(folders.delete_folder(db(), fid))

    @app.post("/api/folders/<int:fid>/items")
    def folders_add(fid):
        b = body()
        return jsonify(folders.add_items(db(), fid, b.get("items", b)))

    @app.delete("/api/folders/<int:fid>/items/<item_type>/<int:item_id>")
    def folders_remove(fid, item_type, item_id):
        folders.remove_item(db(), fid, item_type, item_id)
        return "", 204

    @app.post("/api/folders/<int:fid>/items/<item_type>/<int:item_id>/move")
    def folders_move(fid, item_type, item_id):
        to = repo._filter_value("to_folder", repo.v.fk, body().get("to_folder"))
        if to is None:
            raise ValidationError({"to_folder": "is required"})
        folders.move_item(db(), fid, to, item_type, item_id)
        return "", 204

    @app.put("/api/folders/<int:fid>/order")
    def folders_order(fid):
        folders.reorder_items(db(), fid, body().get("order"))
        return "", 204

    @app.get("/api/items/<item_type>/<int:item_id>/folders")
    def item_folders(item_type, item_id):
        return jsonify(folders.folders_of(db(), item_type, item_id))

    @app.put("/api/items/<item_type>/<int:item_id>/folders")
    def item_folders_set(item_type, item_id):
        return jsonify(folders.set_item_folders(db(), item_type, item_id, body().get("folder_ids")))

    @app.get("/api/items/lookup")
    def items_lookup():
        return jsonify(folders.lookup(db(), request.args.get("type", ""), request.args.get("q", ""),
                                      request.args.get("limit", 30)))

    @app.get("/api/notes")
    def notes_list():
        return jsonify(folders.list_notes(db(), **args("q", "unfiled", "limit", "offset")))

    @app.post("/api/notes")
    def notes_create():
        return created(folders.create_note(db(), body()))

    @app.get("/api/notes/<int:nid>")
    def notes_get(nid):
        return jsonify(folders.get_note(db(), nid))

    @app.patch("/api/notes/<int:nid>")
    def notes_update(nid):
        return jsonify(folders.update_note(db(), nid, body()))

    @app.delete("/api/notes/<int:nid>")
    def notes_delete(nid):
        folders.delete_note(db(), nid)
        return "", 204

    @app.get("/api/files")
    def files_list():
        return jsonify(folders.list_files(db(), **args("q", "limit", "offset")))

    @app.post("/api/files")
    def files_upload():
        uploads = request.files.getlist("file")
        if not uploads:
            raise ValidationError({"file": "no file uploaded"})
        out, errors = [], []
        for f in uploads:
            try:
                rec, new = folders.ingest_file(db(), cfg, f.read(), f.filename or "file",
                                               description=request.form.get("description", ""),
                                               folder_id=request.form.get("folder_id"))
                out.append({**rec, "duplicate": not new})
            except ValidationError as e:
                errors.append({"name": f.filename, "errors": e.errors})
        if not out and errors:
            raise ValidationError(errors[0]["errors"])
        return jsonify(items=out, errors=errors), 201

    @app.get("/api/files/<int:file_id>")
    def files_get(file_id):
        return jsonify(folders.get_file(db(), file_id))

    @app.patch("/api/files/<int:file_id>")
    def files_update(file_id):
        return jsonify(folders.update_file(db(), file_id, body()))

    @app.delete("/api/files/<int:file_id>")
    def files_delete(file_id):
        folders.delete_file(db(), cfg, file_id)
        return "", 204

    @app.get("/files/<int:file_id>/<mode>")
    def files_download(file_id, mode):
        """Download (or, for a safe allow-list of types, view) an attached file."""
        f = folders.get_file(db(), file_id)
        path = folders.file_path(cfg, f["stored_name"])
        if mode not in ("download", "view") or not path.exists():
            abort(404)
        inline = mode == "view" and f["inline"]
        resp = send_file(path, mimetype=f["mime"] if inline else "application/octet-stream",
                         as_attachment=not inline, download_name=f["original_name"], max_age=0)
        # belt and braces: even if a browser sniffs, the CSP stops scripts. PDFs skip
        # `sandbox` because Chrome's built-in viewer refuses to run in a sandboxed frame.
        sandbox = "" if f["mime"] == "application/pdf" else "sandbox; "
        resp.headers["Content-Security-Policy"] = (f"{sandbox}default-src 'none'; img-src 'self'; media-src 'self'; "
                                                   "object-src 'self'; style-src 'unsafe-inline'")
        return resp

    # ------------------------------------------------------ history/undo
    @app.get("/api/history/<table>/<int:row_id>")
    def history_get(table, row_id):
        return jsonify(history.history(db(), table, row_id))

    @app.get("/api/deleted")
    def deleted_list():
        return jsonify(history.recently_deleted(db()))

    @app.post("/api/deleted/<int:audit_id>/restore")
    def deleted_restore(audit_id):
        return jsonify(history.restore_deleted(db(), audit_id))

    # ------------------------------------------------------------ API index
    @app.get("/api")
    def api_index():
        """Every endpoint with its methods and summary — a lightweight API reference."""
        routes = []
        for rule in app.url_map.iter_rules():
            if not rule.rule.startswith(("/api", "/media")):
                continue
            fn = app.view_functions[rule.endpoint]
            doc = (fn.__doc__ or "").strip().split("\n")[0]
            routes.append({"path": rule.rule, "methods": sorted(rule.methods - {"HEAD", "OPTIONS"}), "summary": doc})
        return jsonify(sorted(routes, key=lambda r: r["path"]))

    # --------------------------------------------------------- maintenance
    @app.post("/api/maintenance/<task>")
    def maintenance(task):
        conn = db()
        if task == "integrity":
            return jsonify(integrity=[r[0] for r in conn.execute("PRAGMA integrity_check")],
                           foreign_keys=repo.rows(conn.execute("PRAGMA foreign_key_check")))
        if task == "optimize":
            before = conn.execute("PRAGMA page_count").fetchone()[0]
            conn.execute("INSERT INTO search_index(search_index) VALUES ('optimize')")
            conn.execute("PRAGMA optimize")
            conn.execute("VACUUM")
            after = conn.execute("PRAGMA page_count").fetchone()[0]
            return jsonify(pages_before=before, pages_after=after)
        if task == "verify-media":
            return jsonify(media.verify_media(conn, cfg))
        if task == "prune-audit":
            b = request.get_json(silent=True) or {}
            return jsonify(deleted=history.prune(conn, b.get("keep_days", 365)))
        abort(404)

    return app
