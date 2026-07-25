"""Conversation memory subsystem.

The package deliberately separates same-thread working context from durable,
cross-thread memories.  Scientific claims remain grounded in the RAG corpus;
durable memories are only a recall layer for preferences, goals, and decisions.
"""

from backend.memory.service import memory_service

__all__ = ["memory_service"]
