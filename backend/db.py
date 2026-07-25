"""SQLite 持久层:对话、消息、反馈(微调语料的原始来源)。

单文件数据库(data/feedback.db),零部署依赖,随目录迁移。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from backend.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    kb_name    TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
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
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, created_at);

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


def _connect() -> sqlite3.Connection:
    db_path = settings.data_dir / "feedback.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


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
               GROUP BY c.id ORDER BY COALESCE(last_message_at, c.created_at) DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conv_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id=?", (conv_id,)
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
        d["feedback"] = fb_by_msg.get(d["id"])
        messages.append(d)
    return {**dict(row), "messages": messages}


def update_conversation_title(conv_id: str, title: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))


def delete_conversation(conv_id: str) -> bool:
    """Delete a transcript and thread-scoped memory for one conversation.

    Durable memories intentionally survive because they are user-level facts
    that may have been learned from more than one conversation.
    """
    with _connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM conversations WHERE id=?", (conv_id,)
        ).fetchone()
        if not exists:
            return False
        conn.execute(
            "DELETE FROM feedback WHERE message_id IN "
            "(SELECT id FROM messages WHERE conv_id=?)",
            (conv_id,),
        )
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
) -> str:
    msg_id = _new_id("msg")
    with _connect() as conn:
        conn.execute(
            """INSERT INTO messages
               (id, conv_id, role, content, route_requested, route_used,
                citations_json, trace_json, model, latency_ms, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                msg_id, conv_id, role, content, route_requested, route_used,
                json.dumps(citations or [], ensure_ascii=False),
                json.dumps(trace or [], ensure_ascii=False),
                model, latency_ms, time.time(),
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
    return d


def update_message_trace(msg_id: str, trace: list[dict[str, Any]]) -> None:
    """Replace a persisted message trace after post-answer lifecycle events run."""
    with _connect() as conn:
        conn.execute(
            "UPDATE messages SET trace_json=? WHERE id=?",
            (json.dumps(trace, ensure_ascii=False), msg_id),
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
