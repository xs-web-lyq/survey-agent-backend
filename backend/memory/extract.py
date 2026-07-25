"""Conservative durable-memory extraction with secret filtering."""

from __future__ import annotations

import re

from backend.memory import store


_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)(api[_ -]?key|token|password|secret)\s*[:=]\s*\S+"),
)
_PREFERENCE_MARKERS = ("我希望", "以后都", "始终", "优先", "不要", "回答风格", "偏好")
_GOAL_MARKERS = ("我的研究", "研究方向", "我要研究", "我的目标", "准备写", "计划研究")
_DECISION_MARKERS = ("确定采用", "最终选择", "决定使用", "就按", "确认使用")


def _safe(text: str) -> bool:
    return len(text.strip()) >= 4 and not any(p.search(text) for p in _SECRET_PATTERNS)


def extract_explicit_memories(
    conv_id: str, message_id: str, user_text: str, assistant_text: str = "",
) -> list[str]:
    """Persist only user-explicit durable facts; RAG claims are never memorized."""
    if not _safe(user_text):
        return []
    kind = ""
    confidence = 0.9
    if any(marker in user_text for marker in _PREFERENCE_MARKERS):
        kind, confidence = "preference", 0.96
    elif any(marker in user_text for marker in _GOAL_MARKERS):
        kind, confidence = "goal", 0.92
    elif any(marker in user_text for marker in _DECISION_MARKERS):
        kind, confidence = "decision", 0.94
    if not kind:
        return []

    content = " ".join(user_text.split())[:500]
    memory_id = store.add_memory(
        kind=kind,
        content=content,
        evidence=[f"{conv_id}/{message_id}"],
        confidence=confidence,
        source_conv_id=conv_id,
        source_message_id=message_id,
    )
    return [memory_id]
