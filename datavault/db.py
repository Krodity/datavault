"""SQLite connection handling and a small, transactional migration runner."""
from __future__ import annotations

import importlib.util
import itertools
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import Config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_savepoints = itertools.count(1)


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    """Open a connection with sane defaults for an app database."""
    if readonly:
        # as_uri() percent-encodes, so paths with spaces, '?' or '#' work
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
        conn.execute("PRAGMA query_only = ON")
    else:
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)  # autocommit; we manage txns
        conn.execute("PRAGMA journal_mode = WAL")       # readers don't block the writer
        conn.execute("PRAGMA synchronous = NORMAL")     # safe with WAL, much faster than FULL
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception.

    Nested use joins the outer transaction via a SAVEPOINT so helpers can be
    composed (e.g. a CSV import calling the single-row create function).
    """
    if conn.in_transaction:
        name = f"sp_{next(_savepoints)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:  # SQLite may already have rolled back (e.g. disk full)
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _discover() -> list[tuple[int, str, Path]]:
    found = []
    for p in sorted(MIGRATIONS_DIR.iterdir()):
        if p.suffix in (".sql", ".py") and p.name[:4].isdigit():
            found.append((int(p.name[:4]), p.stem, p))
    versions = [v for v, _, _ in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError("duplicate migration version numbers")
    return found


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations in order, each in its own transaction.

    The schema version is tracked in PRAGMA user_version (stored in the DB
    header, so it can't drift from the schema it describes).
    """
    applied = []
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY, name TEXT NOT NULL,
                        applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))""")
    version = current_version(conn)
    for num, name, path in _discover():
        if num <= version:
            continue
        with transaction(conn):
            # re-check under the write lock: another process (CLI vs. service)
            # may have applied this migration while we waited for BEGIN IMMEDIATE
            if current_version(conn) >= num:
                continue
            if path.suffix == ".sql":
                # executescript would COMMIT our transaction; split on ';' boundaries instead
                for stmt in _split_sql(path.read_text()):
                    conn.execute(stmt)
            else:
                spec = importlib.util.spec_from_file_location(f"datavault_migration_{num}", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.upgrade(conn)
            conn.execute("INSERT INTO schema_migrations (version, name) VALUES (?, ?)", (num, name))
            conn.execute(f"PRAGMA user_version = {num:d}")
        applied.append(name)
    return applied


def _split_sql(script: str) -> list[str]:
    """Split a script into complete statements using sqlite3.complete_statement,
    so semicolons inside trigger bodies and string literals are handled."""
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        if not buf and line.strip().startswith("--"):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete SQL statement at end of migration: {buf[:80]!r}")
    return out


def open_db(cfg: Config) -> sqlite3.Connection:
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    migrate(conn)
    return conn
