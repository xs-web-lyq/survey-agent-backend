"""Loss-aware, versioned compaction of old thread messages."""

from __future__ import annotations

from typing import Any

from backend import db
from backend.config import settings
from backend.memory import store


def _short(text: str, limit: int) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[:limit].rstrip() + "…"


def maybe_compact(conv_id: str, state: dict[str, Any]) -> dict[str, Any] | None:
    conversation = db.get_conversation(conv_id)
    if not conversation:
        return None
    messages = list(conversation.get("messages", []))
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    if (
        len(messages) < settings.memory_compact_after_messages
        and total_chars < settings.memory_compact_after_chars
    ):
        return None

    keep = max(4, settings.memory_recent_messages)
    old_messages = messages[:-keep]
    if not old_messages:
        return None
    through_message_id = str(old_messages[-1]["id"])
    previous = store.latest_summary(conv_id)
    if previous.get("through_message_id") == through_message_id:
        return None

    user_goals = [
        _short(str(m.get("content", "")), 180)
        for m in old_messages if m.get("role") == "user"
    ][-8:]
    findings = [
        _short(str(m.get("content", "")), 280)
        for m in old_messages if m.get("role") == "assistant"
    ][-5:]
    sources: list[str] = []
    for message in old_messages:
        for citation in message.get("citations", []):
            source = str(citation.get("source", ""))
            if source and source not in sources:
                sources.append(source)

    summary = {
        "topic": state.get("current_topic", ""),
        "user_goal": state.get("user_goal", ""),
        "prior_summary": previous.get("summary", {}),
        "user_requests": user_goals,
        "established_context": findings,
        "constraints": state.get("constraints", []),
        "open_questions": state.get("open_questions", []),
        "source_ids": sources[-20:],
    }
    token_estimate = max(1, len(str(summary)) // 3)
    summary_id = store.save_summary(
        conv_id, through_message_id, summary, token_estimate,
    )
    return {"id": summary_id, "through_message_id": through_message_id,
            "token_count": token_estimate}
