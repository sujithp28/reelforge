"""Connection handling and schema.

All SQL lives in repo.py and is written in one dialect-neutral style:
`?` placeholders and a `{now}` token. `sql()` rewrites both for the configured
dialect, so moving to PostgreSQL means adding a connection factory here rather
than editing queries.

Portability rules the schema follows:
  - tables are created in dependency order (PostgreSQL validates foreign-key
    targets at CREATE TABLE time; SQLite does not, which hid a bug here)
  - no AUTOINCREMENT; ids are application-generated
  - timestamps are written by the app through `{now}`, not by column defaults
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config

# Per-dialect fragments. Adding a dialect means adding a row here plus a
# branch in connect().
_NOW = {"sqlite": "datetime('now')", "postgres": "now()"}

# `assets` is declared BEFORE `scenes` because scenes.asset_id references it.
# SQLite tolerates the reverse order; PostgreSQL raises "relation does not
# exist" on first boot.
SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS projects (
        id            TEXT PRIMARY KEY,
        title         TEXT NOT NULL,
        idea          TEXT NOT NULL,
        category      TEXT NOT NULL,
        input_type    TEXT NOT NULL DEFAULT 'Idea',
        duration      INTEGER NOT NULL,
        aspect_ratio  TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'draft',
        progress      INTEGER NOT NULL DEFAULT 0,
        error         TEXT,
        video_key     TEXT,
        music_volume  REAL NOT NULL DEFAULT 0.8,
        music_fade_out INTEGER NOT NULL DEFAULT 2,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assets (
        id          TEXT PRIMARY KEY,
        project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        kind        TEXT NOT NULL,
        filename    TEXT NOT NULL,
        storage_key TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scenes (
        id           TEXT PRIMARY KEY,
        project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        position     INTEGER NOT NULL,
        title        TEXT NOT NULL,
        prompt       TEXT NOT NULL,
        duration     INTEGER NOT NULL,
        caption      TEXT,
        asset_id     TEXT REFERENCES assets(id) ON DELETE SET NULL,
        regen_count  INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS render_jobs (
        id          TEXT PRIMARY KEY,
        project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        status      TEXT NOT NULL DEFAULT 'queued',
        progress    INTEGER NOT NULL DEFAULT 0,
        error       TEXT,
        provider    TEXT NOT NULL DEFAULT 'mock',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_scenes_project ON scenes(project_id, position)",
    "CREATE INDEX IF NOT EXISTS idx_assets_project ON assets(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_project ON render_jobs(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON render_jobs(status)",
]

# Columns added after the first release. Applied idempotently so an existing
# local database keeps its rows instead of needing a wipe.
MIGRATIONS = [
    ("projects", "video_key", "TEXT"),
    ("projects", "music_volume", "REAL NOT NULL DEFAULT 0.8"),
    ("projects", "music_fade_out", "INTEGER NOT NULL DEFAULT 2"),
    ("scenes", "regen_count", "INTEGER NOT NULL DEFAULT 0"),
    ("assets", "storage_key", "TEXT"),
]


def sql(text: str) -> str:
    """Rewrite dialect-neutral SQL for the configured backend."""
    text = text.replace("{now}", _NOW[config.DB_DIALECT])
    if config.DB_DIALECT != "sqlite":
        text = text.replace("?", "%s")
    return text


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@contextmanager
def connect() -> Iterator[Any]:
    """Commit on success, roll back on error, always close.

    A PostgreSQL implementation belongs here: yield a pooled psycopg
    connection with a dict row factory. Nothing in repo.py changes, because
    every query already goes through sql().
    """
    if config.DB_DIALECT != "sqlite":
        raise RuntimeError(
            f"REELFORGE_DB_DIALECT={config.DB_DIALECT!r} has no connection factory yet."
            " Add one in db.connect(); the SQL layer is already dialect-neutral."
        )
    conn = sqlite3.connect(config.SQLITE_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the render worker write progress while a request reads it.
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _existing_columns(conn: Any, table: str) -> set[str]:
    if config.DB_DIALECT == "sqlite":
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    rows = conn.execute(
        sql("SELECT column_name AS name FROM information_schema.columns"
            " WHERE table_name = ?"), (table,)
    ).fetchall()
    return {r["name"] for r in rows}


def init() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        for statement in SCHEMA:
            conn.execute(sql(statement))
        for table, column, coltype in MIGRATIONS:
            if column not in _existing_columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        _backfill_storage_keys(conn)


def _backfill_storage_keys(conn: Any) -> None:
    """Convert pre-abstraction absolute paths into storage keys.

    The first release stored `assets.stored_path` and `projects.video_path` as
    absolute filesystem paths, which broke the moment the data directory moved.
    """
    if "stored_path" in _existing_columns(conn, "assets"):
        rows = conn.execute(
            "SELECT id, project_id, filename, stored_path FROM assets"
            " WHERE storage_key IS NULL OR storage_key = ''"
        ).fetchall()
        for row in rows:
            conn.execute(
                sql("UPDATE assets SET storage_key = ? WHERE id = ?"),
                (f"uploads/{row['project_id']}/{row['filename']}", row["id"]),
            )
    if "video_path" in _existing_columns(conn, "projects"):
        rows = conn.execute(
            "SELECT id, video_path FROM projects"
            " WHERE video_path IS NOT NULL AND (video_key IS NULL OR video_key = '')"
        ).fetchall()
        for row in rows:
            conn.execute(
                sql("UPDATE projects SET video_key = ? WHERE id = ?"),
                (f"renders/{Path(row['video_path']).name}", row["id"]),
            )


def rows_to_dicts(rows: Any) -> list[dict]:
    return [dict(r) for r in rows]
