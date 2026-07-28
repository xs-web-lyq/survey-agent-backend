"""Versioned SQLite migrations for the application database."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _migration_001_chat_runs(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "messages", "status", "TEXT NOT NULL DEFAULT 'completed'")
    _ensure_column(conn, "messages", "error_json", "TEXT DEFAULT '{}'")
    _ensure_column(conn, "messages", "run_id", "TEXT DEFAULT ''")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS turn_runs (
            id                   TEXT PRIMARY KEY,
            conv_id              TEXT NOT NULL REFERENCES conversations(id),
            user_message_id      TEXT NOT NULL REFERENCES messages(id),
            assistant_message_id TEXT NOT NULL REFERENCES messages(id),
            status               TEXT NOT NULL DEFAULT 'pending',
            stage                TEXT NOT NULL DEFAULT 'pending',
            route_requested      TEXT DEFAULT '',
            route_used           TEXT DEFAULT '',
            model                TEXT DEFAULT '',
            error_code           TEXT DEFAULT '',
            error_message        TEXT DEFAULT '',
            trace_json           TEXT DEFAULT '[]',
            request_json         TEXT DEFAULT '{}',
            started_at           REAL NOT NULL,
            updated_at           REAL NOT NULL,
            finished_at          REAL,
            retry_of_run_id      TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_turn_runs_conv
            ON turn_runs(conv_id, started_at);
        CREATE INDEX IF NOT EXISTS idx_turn_runs_retry
            ON turn_runs(retry_of_run_id);
    """)


def _migration_002_conversation_trash(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "conversations", "deleted_at", "REAL")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_deleted "
        "ON conversations(deleted_at, created_at)"
    )


def _migration_003_research_briefs(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS research_briefs (
            id             TEXT PRIMARY KEY,
            conv_id        TEXT NOT NULL REFERENCES conversations(id),
            version        INTEGER NOT NULL,
            status         TEXT NOT NULL DEFAULT 'draft',
            brief_json     TEXT NOT NULL DEFAULT '{}',
            scope_json     TEXT NOT NULL DEFAULT '{}',
            task_id        TEXT NOT NULL DEFAULT '',
            created_at     REAL NOT NULL,
            updated_at     REAL NOT NULL,
            confirmed_at   REAL,
            handed_off_at  REAL,
            UNIQUE(conv_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_research_briefs_conv
            ON research_briefs(conv_id, version DESC);
        CREATE INDEX IF NOT EXISTS idx_research_briefs_task
            ON research_briefs(task_id);
    """)


MIGRATIONS: tuple[Migration, ...] = (
    (1, "persist chat runs and failed traces", _migration_001_chat_runs),
    (2, "soft delete conversations and add recycle bin", _migration_002_conversation_trash),
    (3, "persist versioned research briefs and survey handoffs",
     _migration_003_research_briefs),
)


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  REAL NOT NULL
        )
    """)
    applied = {
        int(row["version"])
        for row in conn.execute("SELECT version FROM schema_migrations")
    }
    newly_applied: list[int] = []
    for version, name, migration in MIGRATIONS:
        if version in applied:
            continue
        migration(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?,?,?)",
            (version, name, time.time()),
        )
        newly_applied.append(version)
    return newly_applied


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
    return int(row["version"] or 0) if row else 0
