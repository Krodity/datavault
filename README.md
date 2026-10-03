# DataVault

A personal, local-first database app for **contacts, pictures, expenses, barcodes / QR codes, user-defined sections, and nested folders that hold any mix of them, plus notes with your own fields and file attachments**. It is built on SQLite, with a Python data layer, a REST API, a CLI, and a dependency-free web frontend.

The project is about **moving data in and out correctly**: validating it at the boundary, storing it in a well-constrained schema, querying it efficiently, and getting it back out in standard formats without loss.

![stack](https://img.shields.io/badge/python-3.11+-blue) ![db](https://img.shields.io/badge/SQLite-FTS5%20%7C%20JSON1%20%7C%20WAL-003b57) [![CI](https://github.com/Krodity/datavault/actions/workflows/ci.yml/badge.svg)](https://github.com/Krodity/datavault/actions/workflows/ci.yml) ![tests](https://img.shields.io/badge/tests-148%20passing-brightgreen)

---

## Features

| Area | What it does |
|---|---|
| **Contacts** | Tags (many-to-many), favorites, photo, birthday reminders, duplicate detection (email / phone / name) with **one-click merge**, vCard import and export, a QR code that adds the contact to a phone |
| **Pictures** | Content-addressed storage (SHA-256 dedup), EXIF date and camera extraction, thumbnails, albums. **iPhone photos work:** both MPO JPEGs and HEIC, with a browser-viewable JPEG copy. Decompression-bomb guard and integrity sweep |
| **Expenses** | Integer-cent money, categories with monthly budgets, receipt photos, 12-month trend, budget-vs-actual, top merchants, **subscription detection** (window functions), filters and pagination |
| **Barcodes / QR** | Generate QR, EAN-13/8, UPC-A, Code 128/39, and ISBN-13 (check digits computed and verified), Wi-Fi QR builder, PNG/SVG export, decode from photos or a **live webcam** |
| **Folders** | Nested folders, as deep as you want, each with its own icon, color and **field template**. A folder can hold any mix of items: notes, files, contacts, pictures, expenses, codes and section records. An item can live in several folders, and deleting a folder never deletes what's inside. You can drag items between folders, drop files onto a folder to upload them, and filter or search inside a folder |
| **Notes** | Free-form items with a title, body and **user-defined fields**: name a field anything and give it a type (text, number, money, date, yes/no, link, email or phone). New notes start from their folder's template. Notes are searchable, including their field values |
| **Files** | Any document up to 100 MB (PDF, spreadsheets, archives…). Files are content-addressed and de-duplicated, and the MIME type comes from the extension rather than the browser. Only an allow-list of types is viewed inline; everything else downloads under a sandboxed CSP |
| **Custom sections** | Define your own "tables" in the UI (13 field types: text, money, date, select, picture/contact/code links, **formulas**, …). Records are validated JSON documents. Sort and filter on any field; numeric columns get totals |
| **Formulas** | Spreadsheet-style computed fields: `price * quantity`, `IF(status = "Owned", price, 0)`, `DAYS_BETWEEN(TODAY(), warranty_until)`, `first & " " & last`. Around 35 functions, exact Decimal math, dependency-ordered evaluation with cycle detection, and live validation in the field builder. Results are stored, so they can be sorted and queried in SQL |
| **SQL console** | Read-only, sandboxed, time-boxed SQL with a schema browser, `EXPLAIN QUERY PLAN`, saved queries, example analytics (window functions, CTEs, FTS), and CSV export |
| **Search** | One full-text index across every entity (FTS5, porter stemming, prefix matching, ranked with bm25 and highlighted snippets) |
| **Import / export** | CSV import with automatic header mapping (Google / Outlook / bank exports, **and custom sections**), dry runs, all-or-nothing transactions, a per-line error report, and duplicate skipping on re-import. CSV, JSON and vCard export, and a section's export re-imports unchanged |
| **Backups** | Online snapshots through SQLite's backup API, integrity-checked and zipped with media. A **nightly systemd timer** keeps 14 automatic backups and never prunes manual ones. Restore validates the archive, backs up the current state, then migrates the backup forward |
| **History & undo** | Triggers snapshot every row before and after each change (`json_object`), giving a field-level diff per record. Deleted contacts, expenses, codes and records can be restored exactly, tags included, with dangling references cleared safely |

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
datavault history contacts 12      # field-level change history
datavault history --deleted        # recently deleted rows …
datavault history --restore 345    # … and undo one
```

## Architecture

```
datavault/
├── migrations/          versioned schema: 0001_init.sql, 0002_search_and_audit.py (generates triggers),
│                        0003_formulas_and_perf.py (table rebuild, rowid-keyed FTS, FK indexes),
│                        0004_audit_snapshots.py (before/after JSON snapshots in triggers),
│                        0005_folders_notes_files.py (folder tree, polymorphic membership, notes, files)
├── db.py                connection setup (WAL, FKs, busy timeout), transactions + savepoints, migration runner
├── validation.py        one Schema per entity; the API, CLI and importers share the same rules
├── repo.py              data-access layer: parameterized SQL, allow-listed identifiers
├── media.py             image ingestion, EXIF, thumbnails, integrity checks
├── codes.py             barcode/QR validation, rendering, decoding (pyzbar)
├── formulas.py          safe formula engine (AST whitelist, Decimal math, dependency graph)
├── history.py           change history (field diffs) and undelete from audit snapshots
├── folders.py           folder tree, any-type membership, notes with custom fields, file attachments
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
- **Indexes** match the access paths, for example composite `(category_id, spent_on)` for filtered date ranges. Every foreign-key child column has an index, which SQLite doesn't create on its own. Month filters are written as half-open ranges (`spent_on >= '2026-03-01' AND spent_on < '2026-04-01'`) rather than `substr()`, so the date index is used. A test asserts this with `EXPLAIN QUERY PLAN`.
- **Search index maintenance** keys each FTS row by a deterministic rowid (`id * 8 + kind`), so triggers update it through a b-tree lookup instead of scanning the index. Bulk imports stay linear rather than quadratic.
- **Schema changes** follow SQLite's table-rebuild procedure (create, copy, drop, rename) where `ALTER TABLE` can't change a `CHECK` constraint. Migrations re-check the version under the write lock, so two processes starting at once don't apply the same migration twice.
- **Views** (`v_contacts`, `v_expenses`, `v_monthly_spend`) hide the joins and aggregate tags with `json_group_array`.
- **Triggers** maintain `updated_at`, the FTS index, and the audit log. The FTS and audit triggers are declared `UPDATE OF <data columns>` so the `updated_at` bookkeeping write doesn't double-fire them.
- **Custom sections** use a typed field registry plus JSON documents rather than runtime DDL. Records are filtered and sorted through `json_extract`, with field keys allow-listed before they reach a JSON path.

### Data-handling details

- **Transactions:** every write uses `BEGIN IMMEDIATE`, and nested helpers join through `SAVEPOINT`s. CSV imports use one savepoint per row, so invalid rows are collected and reported together. The whole file then commits or rolls back.
- **Upserts:** `INSERT … ON CONFLICT DO UPDATE … RETURNING` handles tags and scanned codes (it bumps `scan_count`).
- **Analytics in SQL:**
  - recursive CTEs generate continuous month series, so months with no spending still appear
  - `FULL OUTER JOIN` drives budget-vs-actual
  - the subscription detector partitions by merchant and uses `LAG()` to count consecutive months, with a variance bound on the amount
  - window functions (`LAG`, `RANK`, running `SUM`) power the example queries
- **Folders as a graph problem:**
  - the tree is an adjacency list (`parent_id`)
  - breadcrumbs and "move" cycle checks use recursive CTEs that walk up and down the tree
  - per-folder totals count the distinct items across all descendants
  - membership is **polymorphic** (`item_type, item_id`); since that can't carry a `FOREIGN KEY`, generated `AFTER DELETE` triggers on every item table enforce the integrity
  - a folder's items are resolved with **one query per item type**, not one per item
- **Merging** duplicate contacts runs in one transaction. It fills blank fields, unions tags, and re-points expenses and contact fields stored inside JSON documents (`json_set` with a bound path). A failure leaves nothing half-merged.
- **Migrations:** version tracking uses `PRAGMA user_version`, and each migration runs in its own transaction. SQL files are split with `sqlite3.complete_statement`, so trigger bodies survive.

### Security (it's local, but done properly)

- The server binds to `127.0.0.1`. It checks the **Host header** against DNS rebinding and requires a custom **`X-DataVault` header** on writes, which a cross-site form can't send (CSRF protection). It also sends a strict **CSP**.
- The SQL console has **four layers** of protection: a `mode=ro` connection, `PRAGMA query_only`, an **authorizer callback** that allows only read operations and informational PRAGMAs, and a **progress handler** that cancels long queries. A row cap bounds memory.
- All user values are bound parameters. Identifiers that can't be bound (sort columns, JSON paths) go through allow-lists, and free-text search is re-quoted so it can't inject FTS5 operators.
- **Formulas are never `eval()`'d.** They are parsed with Python's `ast` module and evaluated by walking a whitelist of node types. There is no attribute access, subscripting, lambda or comprehension, and expression size, exponents and text growth are all capped.
- **CSV exports neutralize formula injection.** A cell such as `=HYPERLINK(...)` is prefixed so Excel or Sheets won't execute it, while real numbers like `-12.50` are left intact.
- Uploads are verified with Pillow and fully decoded before they touch disk, so a truncated file leaves no orphan. Size and pixel count are capped. Restores are checked against zip-slip and integrity-checked before the live database is replaced.
- Bad input of any kind (`NaN` amounts, malformed query params, non-scalar JSON) returns a 400 with field-level messages, never a 500. `/api` errors are always JSON.

## Tests

```bash
.venv/bin/python -m pytest -q     # 134 tests
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
- the formula engine: values, blanks, cycles, and **injection attempts** such as `__import__`, attribute access and lambdas
- history and undo: field diffs (including inside JSON documents), restoring with tags, clearing dangling references, and refusing records whose section was deleted
- merges, including an atomicity test where the second ID is bogus and the first merge must roll back
- subscription detection that must ignore varied grocery spending
- section CSV round-trips
- MPO and HEIC photo uploads
- backup pruning that is limited to its own label
- folders:
  - nesting
  - cycle prevention
  - de-duplicated recursive counts
  - cleanup triggers
  - notes with typed custom fields
  - template seeding
  - restoring a deleted note into its folders
- files:
  - de-duplication
  - path-stripped names
  - HTML never rendered inline
  - inclusion in backup and restore
- regression tests for every bug found in a code audit:
  - `NaN` and `Infinity` amounts
  - Unicode stored inside JSON documents
  - duplicate re-imports
  - CSV formula injection
  - overflowing QR payloads
  - orphaned files from truncated uploads
  - backup filename collisions
  - restoring a file that isn't a database
  - the migration race
  - data paths containing spaces, `?` or `#`
  - query plans that must use an index

## Running as a service

`deploy/` has systemd **user** units for the app and a nightly backup:

```bash
cp deploy/*.service deploy/*.timer ~/.config/systemd/user/
systemctl --user enable --now datavault datavault-backup.timer
```

To reach it from other devices through a reverse proxy (for example `tailscale serve --https=8800 http://127.0.0.1:8800`),
list the proxy's hostname in `DATAVAULT_ALLOWED_HOSTS` (comma-separated) or pass `--allow-host`. The app keeps listening on
loopback only, and the Host-header check keeps rejecting every other name.

## CI

GitHub Actions runs lint and the full test suite on Python 3.11, 3.12 and 3.13 on every push (`.github/workflows/ci.yml`).
