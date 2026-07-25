"""High-level orchestration for conversation memory lifecycle."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from backend import db
from backend.config import settings
from backend.memory import store
from backend.memory.compact import maybe_compact
from backend.memory.context import prepare_bundle
from backend.memory.extract import extract_explicit_memories
from backend.memory.models import MemoryBundle
from backend.memory.rewrite import infer_topic, is_context_dependent


def _unique(items: list[str], limit: int = 30) -> list[str]:
    return list(dict.fromkeys(x for x in items if x))[-limit:]


class MemoryService:
    def __init__(self) -> None:
        self._idle_tasks: dict[str, asyncio.Task] = {}

    async def prepare_turn(self, conv_id: str, question: str) -> MemoryBundle:
        return await prepare_bundle(conv_id, question)

    def start_turn(self, bundle: MemoryBundle, user_message_id: str) -> str:
        snapshot = {
            "summary_version": bundle.thread_summary.get("version"),
            "recent_message_count": len(bundle.recent_messages),
            "durable_memory_ids": [m.get("id") for m in bundle.durable_memories],
            "resolved_references": bundle.resolved_references,
        }
        return store.create_turn(
            bundle.conv_id,
            user_message_id,
            bundle.standalone_query,
            topic_shift=bundle.topic_shift,
            snapshot=snapshot,
        )

    def complete_turn(
        self,
        bundle: MemoryBundle,
        turn_id: str,
        user_message_id: str,
        assistant_message_id: str,
        assistant_text: str,
        citations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        store.finish_turn(turn_id, assistant_message_id, "completed")
        state = store.get_state(bundle.conv_id)
        history = [*bundle.recent_messages,
                   {"role": "user", "content": bundle.original_question}]
        topic = (
            bundle.original_question[:120]
            if bundle.topic_shift or not is_context_dependent(bundle.original_question)
            else infer_topic(history, state.get("current_topic", ""))
        )
        if not topic:
            topic = bundle.standalone_query[:120]

        entities = list(state.get("entities", []))
        entities.extend(bundle.resolved_references.values())
        entities.extend(re.findall(r"\b[A-Z][A-Za-z0-9-]{1,20}\b", bundle.standalone_query))
        constraints = list(state.get("constraints", []))
        if any(marker in bundle.original_question for marker in ("优先", "不要", "必须", "只要", "希望")):
            constraints.append(bundle.original_question[:240])
        sources = [str(c.get("source", "")) for c in citations]
        next_state = {
            "current_topic": topic,
            "user_goal": state.get("user_goal") or topic,
            "entities": _unique(entities),
            "constraints": _unique(constraints, 12),
            "open_questions": [],
            "cited_sources": _unique([*state.get("cited_sources", []), *sources], 40),
        }
        store.upsert_state(bundle.conv_id, next_state)

        memory_settings = store.get_settings(bundle.conv_id)
        new_memory_ids: list[str] = []
        if memory_settings["generate_memories"]:
            new_memory_ids = extract_explicit_memories(
                bundle.conv_id, user_message_id, bundle.original_question, assistant_text,
            )
        compacted = maybe_compact(bundle.conv_id, next_state)
        self._schedule_idle_pass(bundle.conv_id)
        return {"state": next_state, "new_memory_ids": new_memory_ids,
                "compacted": compacted}

    def fail_turn(self, turn_id: str) -> None:
        store.finish_turn(turn_id, None, "failed")

    def _schedule_idle_pass(self, conv_id: str) -> None:
        previous = self._idle_tasks.pop(conv_id, None)
        if previous and not previous.done():
            previous.cancel()

        async def idle_pass() -> None:
            try:
                await asyncio.sleep(settings.memory_idle_extract_seconds)
                # Explicit memories are already extracted synchronously.  This
                # checkpoint intentionally performs conservative compaction only;
                # future model-based consolidation can plug in here safely.
                maybe_compact(conv_id, store.get_state(conv_id))
            except asyncio.CancelledError:
                return

        self._idle_tasks[conv_id] = asyncio.create_task(idle_pass())

    def debug_state(self, conv_id: str) -> dict[str, Any]:
        return {
            "settings": store.get_settings(conv_id),
            "state": store.get_state(conv_id),
            "summary": store.latest_summary(conv_id),
            "memories": [
                m for m in store.list_memories(limit=100)
                if m.get("source_conv_id") == conv_id or m.get("scope") == "user"
            ][:20],
        }

    def update_settings(
        self, conv_id: str, *, use_memories: bool, generate_memories: bool,
    ) -> dict[str, bool]:
        store.set_settings(
            conv_id, use_memories=use_memories, generate_memories=generate_memories,
        )
        return store.get_settings(conv_id)


memory_service = MemoryService()
