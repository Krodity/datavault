# DataVault

A personal, local-first database app for **contacts, pictures, expenses, barcodes / QR codes, and user-defined sections**. It is built on SQLite, with a Python data layer, a REST API, a CLI, and a dependency-free web frontend.

The project is about **moving data in and out correctly**: validating it at the boundary, storing it in a well-constrained schema, querying it efficiently, and getting it back out in standard formats without loss.

![stack](https://img.shields.io/badge/python-3.11+-blue) ![db](https://img.shields.io/badge/SQLite-FTS5%20%7C%20JSON1%20%7C%20WAL-003b57) ![tests](https://img.shields.io/badge/tests-46%20passing-brightgreen)

---

## Features

| Area | What it does |
|---|---|
| **Contacts** | Tags (many-to-many), favorites, photo, birthday reminders, duplicate detection (email / phone / name), vCard import and export, a QR code that adds the contact to a phone |
| **Pictures** | Content-addressed storage (SHA-256 dedup), EXIF date and camera extraction, thumbnails, albums, decompression-bomb guard, integrity sweep |
| **Expenses** | Integer-cent money, categories with monthly budgets, receipt photos, 12-month trend, budget-vs-actual, top merchants, filters and pagination |
| **Barcodes / QR** | Generate QR, EAN-13/8, UPC-A, Code 128/39, and ISBN-13 (check digits computed and verified), Wi-Fi QR builder, PNG/SVG export, decode from photos or a **live webcam** |
| **Custom sections** | Define your own "tables" in the UI (12 field types: text, money, date, select, picture/contact/code links, …). Records are validated JSON documents. Sort and filter on any field |
| **SQL console** | Read-only, sandboxed, time-boxed SQL with a schema browser, `EXPLAIN QUERY PLAN`, saved queries, example analytics (window functions, CTEs, FTS), and CSV export |
| **Search** | One full-text index across every entity (FTS5, porter stemming, prefix matching, ranked with bm25 and highlighted snippets) |
| **Import / export** | CSV import with automatic header mapping (Google / Outlook / bank exports), dry runs, all-or-nothing transactions, a per-line error report; CSV, JSON and vCard export |
| **Backups** | Online snapshots through SQLite's backup API, integrity-checked and zipped with media; restore migrates old backups forward and backs up the current state first |
| **Audit log** | Trigger-maintained history of every insert, update and delete |

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # needs libzbar for scanning (pacman -S zbar / apt install libzbar0)
.venv/bin/python -m datavault seed        # optional demo data
.venv/bin/python -m datavault serve       # http://127.0.0.1:8800
```

Data lives in `./data/` by default; override it with `DATAVAULT_HOME` or `--home`.

## CLI

```bash
datavault query "SELECT category, SUM(amount) FROM v_expenses GROUP BY 1" --format csv
datavault query --explain "SELECT * FROM expenses WHERE category_id = 1 AND spent_on > '2026-01-01'"
datavault schema expenses --sql
datavault import expenses bank-export.csv --dry-run
datavault import contacts google-contacts.csv --skip-errors
datavault export section:book-collection -o books.csv
datavault code make "978030640615" --kind isbn13 -o isbn.svg
datavault code scan photo-of-receipt.jpg --save
datavault search "coffee"
datavault backup --keep 10        # snapshot + prune
datavault restore data/backups/datavault-20261001-120000.zip
datavault stats --verify-media
```

## Architecture

```
datavault/
├── migrations/          versioned schema: 0001_init.sql, 0002_search_and_audit.py (generates triggers)
├── db.py                connection setup (WAL, FKs, busy timeout), transactions + savepoints, migration runner
├── validation.py        one Schema per entity; the API, CLI and importers share the same rules
├── repo.py              data-access layer: parameterized SQL, allow-listed identifiers
├── media.py             image ingestion, EXIF, thumbnails, integrity checks
├── codes.py             barcode/QR validation, rendering, decoding (pyzbar)
├── query.py             sandboxed read-only SQL console
├── transfer.py          CSV/vCard/JSON import-export, backup/restore
├── web.py               Flask REST API (≈60 endpoints) + security headers
├── cli.py               argparse CLI over the same layers
└── static/              SPA: hash router, vanilla JS, SVG charts, no build step
```

### Schema highlights

- **Money as `INTEGER` cents.** Parsing goes through `Decimal` with banker-safe rounding, so `0.1 + 0.2` never drifts.
- **`CHECK` constraints** enforce ISO dates (`GLOB`), non-negative amounts, enum columns, valid JSON (`json_valid`), and slug/key formats. Bad data is rejected by the database even if the application is bypassed.
- **Foreign keys** with deliberate `ON DELETE` behavior: `CASCADE` for tag links and section fields, `SET NULL` for photos, receipts and linked contacts.
- **Indexes** match the access paths, for example composite `(category_id, spent_on)` for filtered date ranges. You can check this with `EXPLAIN QUERY PLAN` in the console.
- **Views** (`v_contacts`, `v_expenses`, `v_monthly_spend`) hide the joins and aggregate tags with `json_group_array`.
- **Triggers** maintain `updated_at`, the FTS index, and the audit log. The FTS and audit triggers are declared `UPDATE OF <data columns>` so the `updated_at` bookkeeping write doesn't double-fire them.
- **Custom sections** use a typed field registry plus JSON documents rather than runtime DDL. Records are filtered and sorted through `json_extract`, with field keys allow-listed before they reach a JSON path.

### Data-handling details

- **Transactions:** every write uses `BEGIN IMMEDIATE`, and nested helpers join through `SAVEPOINT`s. CSV imports use one savepoint per row, so invalid rows are collected and reported together. The whole file then commits or rolls back.
- **Upserts:** `INSERT … ON CONFLICT DO UPDATE … RETURNING` handles tags and scanned codes (it bumps `scan_count`).
- **Analytics in SQL:** recursive CTEs generate continuous month series (months with no spending still appear), `FULL OUTER JOIN` drives budget-vs-actual, and window functions (`LAG`, `RANK`, running `SUM`) power the examples.
- **Migrations:** version tracking uses `PRAGMA user_version`, and each migration runs in its own transaction. SQL files are split with `sqlite3.complete_statement`, so trigger bodies survive.

### Security (it's local, but done properly)

- The server binds to `127.0.0.1`. It checks the **Host header** against DNS rebinding and requires a custom **`X-DataVault` header** on writes, which a cross-site form can't send (CSRF protection). It also sends a strict **CSP**.
- The SQL console has **four layers** of protection: a `mode=ro` connection, `PRAGMA query_only`, an **authorizer callback** that allows only read operations and informational PRAGMAs, and a **progress handler** that cancels long queries. A row cap bounds memory.
- All user values are bound parameters. Identifiers that can't be bound (sort columns, JSON paths) go through allow-lists, and free-text search is re-quoted so it can't inject FTS5 operators.
- Uploads are verified with Pillow and capped in size and pixel count. Restores are protected against zip-slip.

## Tests

```bash
.venv/bin/python -m pytest -q     # 46 tests
```

The tests cover the following:
- migrations and constraints
- money parsing
- FTS staying in sync on update and delete
- audit triggers
- SQL-console write blocking (DELETE, DROP, PRAGMA writes, ATTACH, stacked statements, runaway recursion)
- check digits
- **render → decode round-trips** for each barcode type
- picture dedup and FK `SET NULL`
- atomic CSV import with rollback
- vCard round-trip with escaping
- backup and restore
- HTTP CSRF and error contracts

## Running as a service

`datavault.service` is a systemd **user** unit:

```bash
cp datavault.service ~/.config/systemd/user/ && systemctl --user enable --now datavault
```
