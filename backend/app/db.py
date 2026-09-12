"""SQLite persistence. stdlib sqlite3, no ORM — the schema is four tables wide."""
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
RENDER_DIR = DATA_DIR / "renders"
DB_PATH = DATA_DIR / "reelforge.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    idea         TEXT NOT NULL,
    category     TEXT NOT NULL,
    input_type   TEXT NOT NULL DEFAULT 'Idea',
    duration     INTEGER NOT NULL,
    aspect_ratio TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft',
    progress     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    video_path   TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS scenes (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    title       TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    duration    INTEGER NOT NULL,
    caption     TEXT,
    asset_id    TEXT REFERENCES assets(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS assets (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    filename    TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scenes_project ON scenes(project_id, position);
CREATE INDEX IF NOT EXISTS idx_assets_project ON assets(project_id);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on error, and always close.

    `with sqlite3.connect(...)` commits but does not close, which leaks a
    handle per request.
    """
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the background render thread write progress while a request reads.
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    for d in (DATA_DIR, UPLOAD_DIR, RENDER_DIR):
        d.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


def rows_to_dicts(rows: Any) -> list[dict]:
    return [dict(r) for r in rows]
