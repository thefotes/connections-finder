"""SQLite access for the outreach database (data/outreach.db).

Only the app writes here. The pipeline reads it read-only; if the file is absent
the pipeline behaves as if no outreach exists. Schema is fixed by
do not change it here.
"""
from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from pipeline.common import DATA

DB_PATH: Path = DATA / "outreach.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS outreach (
  id                 INTEGER PRIMARY KEY,
  company_normalized TEXT NOT NULL UNIQUE,
  company_display    TEXT NOT NULL,
  person_profile_url TEXT,
  person_name        TEXT,
  status             TEXT NOT NULL,
  contacted_at       TEXT,
  updated_at         TEXT NOT NULL,
  notes              TEXT
);

CREATE TABLE IF NOT EXISTS outreach_role (
  outreach_id INTEGER NOT NULL REFERENCES outreach(id) ON DELETE CASCADE,
  role_url    TEXT NOT NULL UNIQUE,
  PRIMARY KEY (outreach_id, role_url)
);
"""

VALID_STATUSES = ("contacted", "applied", "active", "dead")


@contextlib.contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on error, and always close.

    sqlite3's own connection context manager does the transaction but NOT the close,
    so `with connect() as conn:` per request leaked a handle each time.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    DATA.mkdir(exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


def now_iso() -> str:
    import datetime as dt
    return dt.datetime.now().isoformat(timespec="seconds")


def list_outreaches(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM outreach ORDER BY company_normalized"
    ).fetchall()
    roles = conn.execute(
        "SELECT outreach_id, role_url FROM outreach_role"
    ).fetchall()
    by_id: dict[int, list[str]] = {}
    for r in roles:
        by_id.setdefault(r["outreach_id"], []).append(r["role_url"])
    out = []
    for r in rows:
        d = dict(r)
        d["role_urls"] = sorted(by_id.get(r["id"], []))
        out.append(d)
    return out
