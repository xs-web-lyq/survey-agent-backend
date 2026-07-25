"""SQLite persistence for turns, summaries, state, and durable memories."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from backend.config import settings


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_turns (
    id                   TEXT PRIMARY KEY,
    conv_id              TEXT NOT NULL,
    ordinal              INTEGER NOT NULL,
    user_message_id      TEXT NOT NULL,
    assistant_message_id TEXT,
    standalone_query     TEXT NOT NULL,
    topic_shift          INTEGER NOT NULL DEFAULT 0,
    memory_snapshot_json TEXT NOT NULL DEFAULT '{}',
    status               TEXT NOT NULL DEFAULT 'running',
    created_at           REAL NOT NULL,
    completed_at         REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_turn_ordinal
    ON memory_turns(conv_id, ordinal);

CREATE TABLE IF NOT EXISTS thread_state (
    conv_id               TEXT PRIMARY KEY,
    current_topic         TEXT NOT NULL DEFAULT '',
    user_goal             TEXT NOT NULL DEFAULT '',
    entities_json         TEXT NOT NULL DEFAULT '[]',
    constraints_json      TEXT NOT NULL DEFAULT '[]',
    open_questions_json   TEXT NOT NULL DEFAULT '[]',
    cited_sources_json    TEXT NOT NULL DEFAULT '[]',
    updated_at            REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS thread_summaries (
    id                   TEXT PRIMARY KEY,
    conv_id              TEXT NOT NULL,
    version              INTEGER NOT NULL,
    through_message_id   TEXT NOT NULL,
    summary_json         TEXT NOT NULL,
    token_count          INTEGER NOT NULL DEFAULT 0,
    created_at           REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_summary_version
    ON thread_summaries(conv_id, version);

CREATE TABLE IF NOT EXISTS durable_memories (
    id                TEXT PRIMARY KEY,
    scope             TEXT NOT NULL DEFAULT 'user',
    kb_name           TEXT NOT NULL DEFAULT '',
    kind              TEXT NOT NULL,
    content           TEXT NOT NULL,
    evidence_json     TEXT NOT NULL DEFAULT '[]',
    confidence        REAL NOT NULL DEFAULT 0.8,
    status            TEXT NOT NULL DEFAULT 'active',
    source_conv_id    TEXT,
    source_message_id TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    last_used_at      REAL,
    expires_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_durable_memories_active
    ON durable_memories(status, kb_name, updated_at);

CREATE TABLE IF NOT EXISTS conversation_memory_settings (
    conv_id           TEXT PRIMARY KEY,
    use_memories      INTEGER NOT NULL DEFAULT 1,
    generate_memories INTEGER NOT NULL DEFAULT 1,
    updated_at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_lineage (
    conv_id                TEXT PRIMARY KEY,
    parent_conv_id         TEXT NOT NULL,
    forked_from_message_id TEXT,
    created_at             REAL NOT NULL
);
"""


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.data_dir / "feedback.db", factory=_ClosingConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def init_store() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def get_settings(conv_id: str) -> dict[str, bool]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT use_memories, generate_memories FROM conversation_memory_settings WHERE conv_id=?",
            (conv_id,),
        ).fetchone()
    if not row:
        return {"use_memories": True, "generate_memories": True}
    return {"use_memories": bool(row[0]), "generate_memories": bool(row[1])}


def set_settings(conv_id: str, *, use_memories: bool, generate_memories: bool) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO conversation_memory_settings
               (conv_id, use_memories, generate_memories, updated_at) VALUES (?,?,?,?)
               ON CONFLICT(conv_id) DO UPDATE SET use_memories=excluded.use_memories,
               generate_memories=excluded.generate_memories, updated_at=excluded.updated_at""",
            (conv_id, int(use_memories), int(generate_memories), time.time()),
        )


def create_turn(
    conv_id: str, user_message_id: str, standalone_query: str,
    *, topic_shift: bool, snapshot: dict[str, Any],
) -> str:
    with _connect() as conn:
        ordinal = conn.execute(
            "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM memory_turns WHERE conv_id=?",
            (conv_id,),
        ).fetchone()[0]
        turn_id = _id("turn")
        conn.execute(
            """INSERT INTO memory_turns
               (id, conv_id, ordinal, user_message_id, standalone_query,
                topic_shift, memory_snapshot_json, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (turn_id, conv_id, ordinal, user_message_id, standalone_query,
             int(topic_shift), json.dumps(snapshot, ensure_ascii=False), time.time()),
        )
    return turn_id


def finish_turn(turn_id: str, assistant_message_id: str | None, status: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE memory_turns SET assistant_message_id=?, status=?, completed_at=? WHERE id=?",
            (assistant_message_id, status, time.time(), turn_id),
        )


def get_state(conv_id: str) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM thread_state WHERE conv_id=?", (conv_id,)).fetchone()
    if not row:
        return {}
    data = dict(row)
    for column, key in (
        ("entities_json", "entities"),
        ("constraints_json", "constraints"),
        ("open_questions_json", "open_questions"),
        ("cited_sources_json", "cited_sources"),
    ):
        data[key] = json.loads(data.pop(column) or "[]")
    return data


def upsert_state(conv_id: str, state: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO thread_state
               (conv_id, current_topic, user_goal, entities_json, constraints_json,
                open_questions_json, cited_sources_json, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(conv_id) DO UPDATE SET
                 current_topic=excluded.current_topic, user_goal=excluded.user_goal,
                 entities_json=excluded.entities_json,
                 constraints_json=excluded.constraints_json,
                 open_questions_json=excluded.open_questions_json,
                 cited_sources_json=excluded.cited_sources_json,
                 updated_at=excluded.updated_at""",
            (
                conv_id, state.get("current_topic", ""), state.get("user_goal", ""),
                json.dumps(state.get("entities", []), ensure_ascii=False),
                json.dumps(state.get("constraints", []), ensure_ascii=False),
                json.dumps(state.get("open_questions", []), ensure_ascii=False),
                json.dumps(state.get("cited_sources", []), ensure_ascii=False),
                time.time(),
            ),
        )


def latest_summary(conv_id: str) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM thread_summaries WHERE conv_id=? ORDER BY version DESC LIMIT 1",
            (conv_id,),
        ).fetchone()
    if not row:
        return {}
    data = dict(row)
    data["summary"] = json.loads(data.pop("summary_json") or "{}")
    return data


def save_summary(
    conv_id: str, through_message_id: str, summary: dict[str, Any], token_count: int,
) -> str:
    with _connect() as conn:
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM thread_summaries WHERE conv_id=?",
            (conv_id,),
        ).fetchone()[0]
        summary_id = _id("summary")
        conn.execute(
            """INSERT INTO thread_summaries
               (id, conv_id, version, through_message_id, summary_json, token_count, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (summary_id, conv_id, version, through_message_id,
             json.dumps(summary, ensure_ascii=False), token_count, time.time()),
        )
    return summary_id


def add_memory(
    *, kind: str, content: str, evidence: list[str], confidence: float,
    source_conv_id: str, source_message_id: str, scope: str = "user",
) -> str:
    now = time.time()
    with _connect() as conn:
        existing = conn.execute(
            """SELECT id FROM durable_memories
               WHERE status='active' AND kind=? AND lower(content)=lower(?) AND kb_name=?""",
            (kind, content.strip(), settings.kb_name),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE durable_memories SET evidence_json=?, confidence=?, updated_at=? WHERE id=?",
                (json.dumps(evidence, ensure_ascii=False), confidence, now, existing["id"]),
            )
            return str(existing["id"])
        memory_id = _id("mem")
        conn.execute(
            """INSERT INTO durable_memories
               (id, scope, kb_name, kind, content, evidence_json, confidence,
                source_conv_id, source_message_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (memory_id, scope, settings.kb_name, kind, content.strip(),
             json.dumps(evidence, ensure_ascii=False), confidence,
             source_conv_id, source_message_id, now, now),
        )
    return memory_id


def list_memories(*, include_inactive: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    where = "" if include_inactive else "WHERE status='active'"
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM durable_memories {where} ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
        result.append(item)
    return result


def touch_memories(ids: list[str]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    with _connect() as conn:
        conn.execute(
            f"UPDATE durable_memories SET last_used_at=? WHERE id IN ({placeholders})",
            [time.time(), *ids],
        )


def set_memory_status(memory_id: str, status: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE durable_memories SET status=?, updated_at=? WHERE id=?",
            (status, time.time(), memory_id),
        )
    return cur.rowcount > 0


def add_lineage(conv_id: str, parent_conv_id: str, forked_from_message_id: str | None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO conversation_lineage
               (conv_id, parent_conv_id, forked_from_message_id, created_at)
               VALUES (?,?,?,?)""",
            (conv_id, parent_conv_id, forked_from_message_id, time.time()),
        )


init_store()
