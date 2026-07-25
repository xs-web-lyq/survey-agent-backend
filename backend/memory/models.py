"""Typed values exchanged by the memory pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MemoryBundle:
    conv_id: str
    original_question: str
    standalone_query: str
    recent_messages: list[dict[str, str]] = field(default_factory=list)
    thread_summary: dict[str, Any] = field(default_factory=dict)
    thread_state: dict[str, Any] = field(default_factory=dict)
    durable_memories: list[dict[str, Any]] = field(default_factory=list)
    topic_shift: bool = False
    resolved_references: dict[str, str] = field(default_factory=dict)

    def system_context(self) -> str:
        """Render trusted conversation context for the answer system prompt."""
        parts: list[str] = []
        if self.thread_summary:
            summary = self.thread_summary.get("summary", self.thread_summary)
            parts.append(f"会话压缩摘要：{summary}")
        if self.thread_state:
            state = {
                k: self.thread_state.get(k)
                for k in ("current_topic", "user_goal", "constraints", "open_questions")
                if self.thread_state.get(k)
            }
            if state:
                parts.append(f"当前会话状态：{state}")
        if self.durable_memories:
            memory_lines = [
                f"- [{m.get('kind', 'memory')}] {m.get('content', '')}"
                for m in self.durable_memories
            ]
            parts.append("可参考的用户长期记忆（不得覆盖本轮明确要求）：\n" + "\n".join(memory_lines))
        return "\n\n".join(parts)


@dataclass(slots=True)
class RewriteResult:
    standalone_query: str
    topic_shift: bool = False
    resolved_references: dict[str, str] = field(default_factory=dict)
