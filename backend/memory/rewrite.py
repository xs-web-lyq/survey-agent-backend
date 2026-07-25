"""Context-dependent question rewriting and topic-shift detection."""

from __future__ import annotations

import re

from backend.memory.models import RewriteResult


_SHIFT_MARKERS = ("换个话题", "另外一个问题", "另一个问题", "不谈这个", "重新讨论", "题外话")
_REFERENCE_MARKERS = (
    "它", "这个", "这种", "该技术", "上述", "前面", "刚才", "其", "这些",
    "该方法", "这个方法", "该过程", "该模型", "技术发展", "发展脉络",
)
_FOLLOWUP_PREFIXES = ("为什么", "怎么", "如何", "具体", "有哪些", "优缺点", "区别", "那")
_HARD_REFERENCES = ("它", "这个", "这种", "该技术", "上述", "前面", "刚才", "其", "这些", "该方法", "这个方法", "该过程", "该模型")


def is_context_dependent(question: str) -> bool:
    text = question.strip()
    if any(marker in text for marker in _REFERENCE_MARKERS):
        return True
    return len(text) <= 22 and text.startswith(_FOLLOWUP_PREFIXES)


def infer_topic(history: list[dict[str, str]], state_topic: str = "") -> str:
    if state_topic:
        return state_topic
    fallback = ""
    for message in reversed(history):
        if message.get("role") != "user":
            continue
        text = message.get("content", "").strip()
        if text and not fallback:
            fallback = text
        residual = text
        for generic in (*_REFERENCE_MARKERS, *_FOLLOWUP_PREFIXES, "是什么", "怎么样", "介绍一下"):
            residual = residual.replace(generic, "")
        residual = re.sub(r"[？?。！!，,：:\s]", "", residual)
        has_specific_subject = len(residual) >= 4 and not any(
            marker in text for marker in _HARD_REFERENCES
        )
        if text and (not is_context_dependent(text) or has_specific_subject):
            return re.sub(r"[？?。！!]$", "", text)[:120]
    # A first-turn topic may legitimately contain phrases such as “技术发展”.
    # Falling back to the latest user message is safer than dropping context.
    return re.sub(r"[？?。！!]$", "", fallback)[:120]


def rewrite_question(
    question: str, history: list[dict[str, str]], state_topic: str = "",
) -> RewriteResult:
    text = question.strip()
    if any(marker in text for marker in _SHIFT_MARKERS):
        cleaned = text
        for marker in _SHIFT_MARKERS:
            cleaned = cleaned.replace(marker, "")
        return RewriteResult(standalone_query=cleaned.strip("，,：: ") or text, topic_shift=True)

    topic = infer_topic(history, state_topic)
    if not topic or not is_context_dependent(text):
        return RewriteResult(standalone_query=text)

    resolved: dict[str, str] = {}
    for marker in _REFERENCE_MARKERS:
        if marker in text:
            resolved[marker] = topic
    standalone = f"围绕“{topic}”，{text}"
    return RewriteResult(
        standalone_query=standalone,
        topic_shift=False,
        resolved_references=resolved,
    )
