"""Versioned vocabulary for durable agent runs.

These enums are intentionally transport-agnostic.  FastAPI, background workers,
the Electron host, and the React renderer may project them differently, but the
stored values must remain stable once emitted.
"""

from __future__ import annotations

from enum import Enum


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStage(str, Enum):
    PREPARING = "preparing"
    PLANNING = "planning"
    MEMORY = "memory"
    RETRIEVING = "retrieving"
    WAITING_APPROVAL = "waiting_approval"
    EXECUTING_TOOL = "executing_tool"
    GENERATING = "generating"
    VERIFYING = "verifying"
    MEMORY_UPDATE = "memory_update"
    COMPLETED = "completed"


class RunEventType(str, Enum):
    RUN_STARTED = "run.started"
    RUN_STAGE_CHANGED = "run.stage_changed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    MODEL_DELTA = "model.delta"
    THINKING_SUMMARY = "thinking.summary"
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_RESOLVED = "approval.resolved"
    ARTIFACT_UPDATED = "artifact.updated"
    EVIDENCE_FOUND = "evidence.found"


class ApprovalDecision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ToolRisk(str, Enum):
    READ_ONLY = "read_only"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    OPEN_WORLD = "open_world"
