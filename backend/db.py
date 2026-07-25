"""SQLite 持久层:对话、消息、反馈(微调语料的原始来源)。

单文件数据库(data/feedback.db),零部署依赖,随目录迁移。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from typing import Any

from backend.config import settings

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    kb_name    TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    deleted_at REAL
);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conv_id         TEXT NOT NULL REFERENCES conversations(id),
    role            TEXT NOT NULL,             -- user | assistant
    content         TEXT NOT NULL,
    route_requested TEXT DEFAULT '',
    route_used      TEXT DEFAULT '',
    citations_json  TEXT DEFAULT '[]',
    trace_json      TEXT DEFAULT '[]',
    model           TEXT DEFAULT '',
    latency_ms      INTEGER DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'completed',
    error_json      TEXT DEFAULT '{}',
    run_id          TEXT DEFAULT '',
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, created_at);

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
CREATE INDEX IF NOT EXISTS idx_turn_runs_conv ON turn_runs(conv_id, started_at);
CREATE INDEX IF NOT EXISTS idx_turn_runs_retry ON turn_runs(retry_of_run_id);

CREATE TABLE IF NOT EXISTS feedback (
    id            TEXT PRIMARY KEY,
    message_id    TEXT NOT NULL REFERENCES messages(id),
    rating        INTEGER NOT NULL DEFAULT 0,  -- +1 / -1
    score         INTEGER,                     -- 1~5
    tags_json     TEXT DEFAULT '[]',
    comment       TEXT DEFAULT '',
    better_answer TEXT DEFAULT '',
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_message ON feedback(message_id);
"""


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _connect() -> sqlite3.Connection:
    db_path = settings.data_dir / "feedback.db"
    conn = sqlite3.connect(db_path, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    from backend.migrations import apply_migrations

    with _connect() as conn:
        conn.executescript(_SCHEMA)
        apply_migrations(conn)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def database_status() -> dict[str, Any]:
    from backend.migrations import current_version

    try:
        with _connect() as conn:
            conn.execute("SELECT 1").fetchone()
            version = current_version(conn)
        return {"ok": True, "schema_version": version}
    except sqlite3.Error:
        logger.exception("database health check failed")
        return {"ok": False, "schema_version": None}


# ---------- conversations ----------

def create_conversation(title: str = "") -> str:
    conv_id = _new_id("conv")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, kb_name, created_at) VALUES (?,?,?,?)",
            (conv_id, title, settings.kb_name, time.time()),
        )
    return conv_id


def list_conversations(limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT c.*, COUNT(m.id) AS message_count,
                      MAX(m.created_at) AS last_message_at
               FROM conversations c LEFT JOIN messages m ON m.conv_id = c.id
               WHERE c.deleted_at IS NULL
               GROUP BY c.id ORDER BY COALESCE(last_message_at, c.created_at) DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(
    conv_id: str,
    *,
    include_deleted: bool = False,
) -> dict[str, Any] | None:
    with _connect() as conn:
        deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
        row = conn.execute(
            f"SELECT * FROM conversations WHERE id=?{deleted_clause}", (conv_id,)
        ).fetchone()
        if not row:
            return None
        msgs = conn.execute(
            "SELECT * FROM messages WHERE conv_id=? ORDER BY created_at", (conv_id,)
        ).fetchall()
        fb_rows = conn.execute(
            """SELECT f.* FROM feedback f
               JOIN messages m ON f.message_id = m.id WHERE m.conv_id=?""",
            (conv_id,),
        ).fetchall()
    fb_by_msg: dict[str, dict] = {}
    for f in fb_rows:
        fb_by_msg[f["message_id"]] = dict(f)
    messages = []
    for m in msgs:
        d = dict(m)
        d["citations"] = json.loads(d.pop("citations_json") or "[]")
        d["trace"] = json.loads(d.pop("trace_json") or "[]")
        d["error"] = json.loads(d.pop("error_json") or "{}")
        d["feedback"] = fb_by_msg.get(d["id"])
        messages.append(d)
    return {**dict(row), "messages": messages}


def update_conversation_title(conv_id: str, title: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))


def delete_conversation(conv_id: str) -> bool:
    """Move a conversation to the recycle bin without deleting its transcript."""
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE conversations SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
            (time.time(), conv_id),
        )
    return cursor.rowcount > 0


def list_deleted_conversations(limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT c.*, COUNT(m.id) AS message_count,
                      MAX(m.created_at) AS last_message_at
               FROM conversations c LEFT JOIN messages m ON m.conv_id = c.id
               WHERE c.deleted_at IS NOT NULL
               GROUP BY c.id ORDER BY c.deleted_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def restore_conversation(conv_id: str) -> bool:
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE conversations SET deleted_at=NULL WHERE id=? AND deleted_at IS NOT NULL",
            (conv_id,),
        )
    return cursor.rowcount > 0


def purge_conversation(
    conv_id: str,
    *,
    delete_durable_memories: bool = False,
) -> bool:
    """Permanently delete a transcript and thread-scoped memory.

    Durable memories intentionally survive because they are user-level facts
    that may have been learned from more than one conversation, unless the
    caller explicitly requests their deletion.
    """
    with _connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM conversations WHERE id=? AND deleted_at IS NOT NULL",
            (conv_id,),
        ).fetchone()
        if not exists:
            return False
        conn.execute(
            "DELETE FROM feedback WHERE message_id IN "
            "(SELECT id FROM messages WHERE conv_id=?)",
            (conv_id,),
        )
        conn.execute("DELETE FROM turn_runs WHERE conv_id=?", (conv_id,))
        conn.execute("DELETE FROM messages WHERE conv_id=?", (conv_id,))
        scoped_tables = (
            ("memory_turns", "conv_id"),
            ("thread_state", "conv_id"),
            ("thread_summaries", "conv_id"),
            ("conversation_memory_settings", "conv_id"),
            ("conversation_lineage", "conv_id"),
            ("conversation_lineage", "parent_conv_id"),
        )
        for table, column in scoped_tables:
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if present:
                conn.execute(f"DELETE FROM {table} WHERE {column}=?", (conv_id,))
        if delete_durable_memories:
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='durable_memories'"
            ).fetchone()
            if present:
                conn.execute(
                    "DELETE FROM durable_memories WHERE source_conv_id=?", (conv_id,)
                )
        conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
    return True


def fork_conversation(conv_id: str, through_message_id: str | None = None) -> str | None:
    """Clone a conversation transcript up to a message into a new branch."""
    source = get_conversation(conv_id)
    if not source:
        return None
    new_id = create_conversation(title=f"↳ {source.get('title') or '对话分支'}")
    for message in source.get("messages", []):
        add_message(
            new_id,
            message["role"],
            message["content"],
            route_requested=message.get("route_requested", ""),
            route_used=message.get("route_used", ""),
            citations=message.get("citations", []),
            trace=message.get("trace", []),
            model=message.get("model", ""),
            latency_ms=message.get("latency_ms", 0),
        )
        if through_message_id and message["id"] == through_message_id:
            break
    return new_id


# ---------- messages ----------

def add_message(
    conv_id: str,
    role: str,
    content: str,
    *,
    route_requested: str = "",
    route_used: str = "",
    citations: list | None = None,
    trace: list | None = None,
    model: str = "",
    latency_ms: int = 0,
    status: str = "completed",
    error: dict[str, Any] | None = None,
    run_id: str = "",
) -> str:
    msg_id = _new_id("msg")
    with _connect() as conn:
        conn.execute(
            """INSERT INTO messages
               (id, conv_id, role, content, route_requested, route_used,
                citations_json, trace_json, model, latency_ms, status,
                error_json, run_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                msg_id, conv_id, role, content, route_requested, route_used,
                json.dumps(citations or [], ensure_ascii=False),
                json.dumps(trace or [], ensure_ascii=False),
                model, latency_ms, status,
                json.dumps(error or {}, ensure_ascii=False), run_id, time.time(),
            ),
        )
    return msg_id


def get_message(msg_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["citations"] = json.loads(d.pop("citations_json") or "[]")
    d["trace"] = json.loads(d.pop("trace_json") or "[]")
    d["error"] = json.loads(d.pop("error_json") or "{}")
    return d


def update_message_trace(msg_id: str, trace: list[dict[str, Any]]) -> None:
    """Replace a persisted message trace after post-answer lifecycle events run."""
    with _connect() as conn:
        conn.execute(
            "UPDATE messages SET trace_json=? WHERE id=?",
            (json.dumps(trace, ensure_ascii=False), msg_id),
        )


def update_message(
    msg_id: str,
    *,
    content: str | None = None,
    route_used: str | None = None,
    citations: list | None = None,
    trace: list | None = None,
    model: str | None = None,
    latency_ms: int | None = None,
    status: str | None = None,
    error: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> None:
    values: dict[str, Any] = {
        "content": content,
        "route_used": route_used,
        "citations_json": (
            json.dumps(citations, ensure_ascii=False) if citations is not None else None
        ),
        "trace_json": json.dumps(trace, ensure_ascii=False) if trace is not None else None,
        "model": model,
        "latency_ms": latency_ms,
        "status": status,
        "error_json": json.dumps(error, ensure_ascii=False) if error is not None else None,
        "run_id": run_id,
    }
    assignments = [f"{key}=?" for key, value in values.items() if value is not None]
    params = [value for value in values.values() if value is not None]
    if not assignments:
        return
    with _connect() as conn:
        conn.execute(
            f"UPDATE messages SET {', '.join(assignments)} WHERE id=?",
            [*params, msg_id],
        )


# ---------- turn runs ----------

def create_turn_run(
    conv_id: str,
    user_message_id: str,
    assistant_message_id: str,
    *,
    route_requested: str,
    request: dict[str, Any] | None = None,
    retry_of_run_id: str = "",
) -> str:
    run_id = _new_id("run")
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO turn_runs
               (id, conv_id, user_message_id, assistant_message_id, status, stage,
                route_requested, trace_json, request_json, started_at, updated_at,
                retry_of_run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, conv_id, user_message_id, assistant_message_id,
                "pending", "pending", route_requested, "[]",
                json.dumps(request or {}, ensure_ascii=False), now, now,
                retry_of_run_id,
            ),
        )
        conn.execute(
            "UPDATE messages SET run_id=? WHERE id=?",
            (run_id, assistant_message_id),
        )
    return run_id


def get_turn_run(run_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM turn_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["trace"] = json.loads(result.pop("trace_json") or "[]")
    result["request"] = json.loads(result.pop("request_json") or "{}")
    return result


def update_turn_run(
    run_id: str,
    *,
    status: str | None = None,
    stage: str | None = None,
    route_used: str | None = None,
    model: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    trace: list[dict[str, Any]] | None = None,
    finished: bool = False,
) -> None:
    values: dict[str, Any] = {
        "status": status,
        "stage": stage,
        "route_used": route_used,
        "model": model,
        "error_code": error_code,
        "error_message": error_message,
        "trace_json": json.dumps(trace, ensure_ascii=False) if trace is not None else None,
        "finished_at": time.time() if finished else None,
        "updated_at": time.time(),
    }
    assignments = [f"{key}=?" for key, value in values.items() if value is not None]
    params = [value for value in values.values() if value is not None]
    with _connect() as conn:
        conn.execute(
            f"UPDATE turn_runs SET {', '.join(assignments)} WHERE id=?",
            [*params, run_id],
        )


# ---------- feedback ----------

def upsert_feedback(
    message_id: str,
    *,
    rating: int = 0,
    score: int | None = None,
    tags: list[str] | None = None,
    comment: str = "",
    better_answer: str = "",
) -> str:
    """同一条消息只保留一条反馈(重复打分覆盖)。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM feedback WHERE message_id=?", (message_id,)
        ).fetchone()
        if row:
            fb_id = row["id"]
            conn.execute(
                """UPDATE feedback SET rating=?, score=?, tags_json=?,
                   comment=?, better_answer=?, created_at=? WHERE id=?""",
                (rating, score, json.dumps(tags or [], ensure_ascii=False),
                 comment, better_answer, time.time(), fb_id),
            )
        else:
            fb_id = _new_id("fb")
            conn.execute(
                """INSERT INTO feedback
                   (id, message_id, rating, score, tags_json, comment,
                    better_answer, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (fb_id, message_id, rating, score,
                 json.dumps(tags or [], ensure_ascii=False),
                 comment, better_answer, time.time()),
            )
    return fb_id


def list_feedback(
    *, route: str = "", min_score: int | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """反馈数据台:联表返回问题/回答/链路/打分。"""
    sql = """
        SELECT f.*, m.content AS answer, m.route_used, m.route_requested,
               m.model, m.latency_ms, m.conv_id,
               (SELECT content FROM messages q
                WHERE q.conv_id = m.conv_id AND q.role = 'user'
                  AND q.created_at < m.created_at
                ORDER BY q.created_at DESC LIMIT 1) AS question
        FROM feedback f JOIN messages m ON f.message_id = m.id
        WHERE 1=1
    """
    params: list[Any] = []
    if route:
        sql += " AND m.route_used = ?"
        params.append(route)
    if min_score is not None:
        sql += " AND f.score >= ?"
        params.append(min_score)
    sql += " ORDER BY f.created_at DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d.pop("tags_json") or "[]")
        out.append(d)
    return out


def feedback_stats() -> dict[str, Any]:
    """各链路的打分统计(数据台图表用)。"""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT m.route_used AS route, COUNT(*) AS n,
                      AVG(f.score) AS avg_score,
                      SUM(CASE WHEN f.rating > 0 THEN 1 ELSE 0 END) AS up,
                      SUM(CASE WHEN f.rating < 0 THEN 1 ELSE 0 END) AS down
               FROM feedback f JOIN messages m ON f.message_id = m.id
               GROUP BY m.route_used ORDER BY n DESC"""
        ).fetchall()
    return {"by_route": [dict(r) for r in rows]}


init_db()
