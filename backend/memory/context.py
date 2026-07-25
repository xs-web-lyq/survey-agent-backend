"""Assemble bounded, model-ready memory context for one turn."""

from __future__ import annotations

import re
from typing import Any

from backend import db
from backend.config import settings
from backend.memory import store
from backend.memory.models import MemoryBundle
from backend.memory.rewrite import rewrite_question


def _normalize_history(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Produce alternating user/assistant messages accepted by both providers."""
    normalized: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role", ""))
        content = str(message.get("content", "")).strip()
        if role not in {"user", "assistant"} or not content:
            continue
        if normalized and normalized[-1]["role"] == role:
            if normalized[-1]["content"].strip() == content:
                continue
            normalized[-1]["content"] += "\n\n" + content
        else:
            normalized.append({"role": role, "content": content})
    while normalized and normalized[0]["role"] != "user":
        normalized.pop(0)
    return normalized


def _messages_after_summary(
    messages: list[dict[str, Any]], summary: dict[str, Any],
) -> list[dict[str, Any]]:
    through = summary.get("through_message_id")
    if not through:
        return messages
    for index, message in enumerate(messages):
        if message.get("id") == through:
            return messages[index + 1:]
    return messages


def _memory_score(memory: dict[str, Any], question: str) -> tuple[int, float]:
    if memory.get("kind") == "preference":
        return (100, float(memory.get("updated_at") or 0))
    q_terms = set(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9-]+", question.lower()))
    m_terms = set(re.findall(
        r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9-]+",
        str(memory.get("content", "")).lower(),
    ))
    return (len(q_terms & m_terms), float(memory.get("updated_at") or 0))


def relevant_memories(question: str, limit: int) -> list[dict[str, Any]]:
    candidates = [
        item for item in store.list_memories(limit=100)
        if not item.get("kb_name") or item.get("kb_name") == settings.kb_name
    ]
    ranked = sorted(candidates, key=lambda item: _memory_score(item, question), reverse=True)
    selected = [m for m in ranked if _memory_score(m, question)[0] > 0][:limit]
    store.touch_memories([str(m["id"]) for m in selected])
    return selected


async def prepare_bundle(conv_id: str, question: str) -> MemoryBundle:
    conversation = db.get_conversation(conv_id) or {"messages": []}
    messages = list(conversation.get("messages", []))
    summary = store.latest_summary(conv_id)
    state = store.get_state(conv_id)
    unsummarized = _messages_after_summary(messages, summary)
    recent = _normalize_history(unsummarized[-settings.memory_recent_messages:])

    rewrite = rewrite_question(question, recent, state.get("current_topic", ""))
    if settings.memory_llm_rewrite and recent and rewrite.standalone_query != question.strip():
        # The deterministic result is intentionally the safe fallback.  A slow
        # or malformed provider response must never block retrieval.
        try:
            from backend import llm

            data = await llm.complete_json(
                "你负责把多轮追问改写成可独立检索的问题，只输出JSON。",
                "历史:\n" + str(recent[-6:]) + "\n当前问题:" + question
                + '\n输出:{"standalone_query":"...","topic_shift":false}',
            )
            candidate = str(data.get("standalone_query", "")).strip()
            if candidate:
                rewrite.standalone_query = candidate
                rewrite.topic_shift = bool(data.get("topic_shift", rewrite.topic_shift))
        except Exception:
            pass

    memory_settings = store.get_settings(conv_id)
    durable = (
        relevant_memories(rewrite.standalone_query, settings.memory_max_durable_items)
        if memory_settings["use_memories"] else []
    )
    return MemoryBundle(
        conv_id=conv_id,
        original_question=question.strip(),
        standalone_query=rewrite.standalone_query,
        recent_messages=recent,
        thread_summary=summary,
        thread_state=state,
        durable_memories=durable,
        topic_shift=rewrite.topic_shift,
        resolved_references=rewrite.resolved_references,
    )
