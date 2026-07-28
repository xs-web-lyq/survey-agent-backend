"""综述任务管理:后台执行、事件总线注册、断线回放。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from backend.agent.phases import (
    phase_finalize,
    phase_supplement_section,
    phase_write_sections,
    run_survey,
)
from backend.agent.evidence_coverage import research_questions_for_section
from backend.agent.state import SurveyState
from backend.config import settings
from backend.events import TASK_STATUS, EventBus, replay_events
from backend.tools.files import WorkspaceFS

logger = logging.getLogger(__name__)


def _archive_finalize_checkpoint(fs: WorkspaceFS) -> str | None:
    """Archive a stale final draft immediately before final integration."""
    checkpoint = "finalize_draft.md"
    if not fs.exists(checkpoint):
        return None
    destination = (
        f"checkpoints/archive/finalize_draft-{int(time.time() * 1000)}.md"
    )
    fs.move(checkpoint, destination)
    return destination


def _recover_finalize_checkpoint(fs: WorkspaceFS) -> bool:
    """从完整的终稿流事件恢复检查点，用于升级前已开始的任务。"""
    checkpoint = "finalize_draft.md"
    if fs.exists(checkpoint):
        return True
    events = replay_events(fs.root / "events.jsonl")
    finalize_starts = [
        int(event.get("seq", 0))
        for event in events
        if event.get("type") == "phase"
        and event.get("data", {}).get("name") == "finalize"
        and event.get("data", {}).get("status") == "start"
    ]
    if not finalize_starts:
        return False
    start_seq = finalize_starts[-1]
    completed_stream = any(
        event.get("type") == "thinking"
        and str(event.get("data", {}).get("text", "")).startswith("引用核查")
        and int(event.get("seq", 0)) > start_seq
        for event in events
    )
    if not completed_stream:
        return False
    parts = [
        str(event.get("data", {}).get("delta", ""))
        for event in events
        if event.get("type") == "text_delta"
        and event.get("data", {}).get("target") == "survey.md"
        and int(event.get("seq", 0)) > start_seq
    ]
    if not parts:
        return False
    fs.write_atomic(checkpoint, "".join(parts))
    return True


class TaskManager:
    """内存中的活动任务表 + workspace 持久化(重启后可回放历史任务)。"""

    def __init__(self) -> None:
        self._buses: dict[str, EventBus] = {}
        self._states: dict[str, SurveyState] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # ---- 创建与执行 ----

    def create(self, topic: str, *, auto_approve: bool = False,
               section_length: str = "medium", doc_scope: list[str] | None = None,
               context: str = "", research_brief_id: str = "",
               research_brief: dict[str, Any] | None = None,
               task_id: str | None = None) -> str:
        task_id = task_id or f"survey-{uuid.uuid4().hex[:8]}"
        fs = WorkspaceFS(task_id)
        if fs.exists("task.json"):
            return task_id
        state = SurveyState(task_id=task_id, topic=topic, fs=fs,
                            section_length=section_length,
                            doc_scope=list(doc_scope or []), context=context,
                            research_brief_id=research_brief_id,
                            research_brief=dict(research_brief or {}))
        bus = EventBus(task_id=task_id, jsonl_path=fs.root / "events.jsonl")
        fs.write_atomic("task.json", json.dumps({
            "task_id": task_id, "topic": topic, "created_at": time.time(),
            "status": "running", "section_length": section_length,
            "doc_scope": list(doc_scope or []),
            "context": context,
            "research_brief_id": research_brief_id,
            "research_brief": dict(research_brief or {}),
        }, ensure_ascii=False, indent=2))

        self._buses[task_id] = bus
        self._states[task_id] = state

        async def runner():
            status = "done"
            try:
                await run_survey(bus, state, auto_approve=auto_approve)
            except Exception:
                status = "failed"
                logger.exception("综述任务失败: %s", task_id)
            finally:
                bus.close()
                # 更新最终状态
                try:
                    meta = json.loads(fs.read("task.json"))
                    meta["status"] = status
                    meta["finished_at"] = time.time()
                    fs.write_atomic(
                        "task.json",
                        json.dumps(meta, ensure_ascii=False, indent=2),
                    )
                except Exception:
                    logger.exception("任务元数据更新失败")

        self._tasks[task_id] = asyncio.create_task(runner())
        return task_id

    def retry_finalize(self, task_id: str) -> None:
        """复用已完成章节，只重新执行终稿整合与引用核查。"""
        if self.is_active(task_id):
            raise RuntimeError("task is active")

        fs = WorkspaceFS(task_id)
        if not fs.exists("task.json"):
            raise FileNotFoundError(task_id)
        try:
            meta = json.loads(fs.read("task.json"))
        except json.JSONDecodeError as exc:
            raise ValueError("task metadata is invalid") from exc

        outline = meta.get("outline")
        sections = outline.get("sections", []) if isinstance(outline, dict) else []
        if not sections:
            raise ValueError("outline is missing")
        missing = [
            str(sec.get("id", "")) for sec in sections
            if not fs.exists(f"sections/{sec.get('id', '')}.md")
        ]
        if missing:
            raise ValueError(f"section drafts are missing: {', '.join(missing)}")

        _recover_finalize_checkpoint(fs)

        state = SurveyState(
            task_id=task_id,
            topic=str(meta.get("topic") or outline.get("title") or task_id),
            fs=fs,
            outline=outline,
            completed_sections=list(meta.get("completed_sections") or []),
            section_length=str(meta.get("section_length") or "medium"),
            doc_scope=list(meta.get("doc_scope") or []),
            context=str(meta.get("context") or ""),
            research_brief_id=str(meta.get("research_brief_id") or ""),
            research_brief=dict(meta.get("research_brief") or {}),
            checkpoint=dict(meta.get("checkpoint") or {}),
        )
        bus = EventBus(task_id=task_id, jsonl_path=fs.root / "events.jsonl")
        self._buses[task_id] = bus
        self._states[task_id] = state

        meta["status"] = "running"
        meta["retry_count"] = int(meta.get("retry_count") or 0) + 1
        meta["retry_started_at"] = time.time()
        meta.pop("finished_at", None)
        fs.write_atomic("task.json", json.dumps(meta, ensure_ascii=False, indent=2))

        async def runner():
            status = "done"
            try:
                bus.emit(TASK_STATUS, {"status": "running", "resume": "finalize"})
                stats = await phase_finalize(bus, state)
                bus.emit(TASK_STATUS, {"status": "done", **stats})
            except Exception as exc:  # noqa: BLE001
                import traceback

                status = "failed"
                err = f"{type(exc).__name__}: {exc}"
                bus.emit(TASK_STATUS, {
                    "status": "failed",
                    "error": err,
                    "traceback": traceback.format_exc()[-2000:],
                })
                logger.exception("综述终稿重试失败: %s", task_id)
            finally:
                bus.close()
                try:
                    latest = json.loads(fs.read("task.json"))
                    latest["status"] = status
                    latest["finished_at"] = time.time()
                    fs.write_atomic(
                        "task.json",
                        json.dumps(latest, ensure_ascii=False, indent=2),
                    )
                except Exception:
                    logger.exception("任务元数据更新失败")

        self._tasks[task_id] = asyncio.create_task(runner())

    def resume(self, task_id: str) -> str:
        """从章节/搜证检查点恢复；已完成章节不会重复检索或写作。"""
        if self.is_active(task_id):
            raise RuntimeError("task is active")

        fs = WorkspaceFS(task_id)
        if not fs.exists("task.json"):
            raise FileNotFoundError(task_id)
        try:
            meta = json.loads(fs.read("task.json"))
        except json.JSONDecodeError as exc:
            raise ValueError("task metadata is invalid") from exc

        outline = meta.get("outline")
        sections = outline.get("sections", []) if isinstance(outline, dict) else []
        if not sections:
            raise ValueError("outline is missing; the task cannot resume past planning")

        completed = list(dict.fromkeys(str(item) for item in meta.get("completed_sections") or []))
        notes: list[str] = []
        if fs.exists("notes.md"):
            notes = [
                line[2:].strip() if line.startswith("- ") else line.strip()
                for line in fs.read("notes.md").splitlines()
                if line.strip()
            ]
        state = SurveyState(
            task_id=task_id,
            topic=str(meta.get("topic") or outline.get("title") or task_id),
            fs=fs,
            outline=outline,
            completed_sections=completed,
            notes=notes,
            section_length=str(meta.get("section_length") or "medium"),
            doc_scope=list(meta.get("doc_scope") or []),
            context=str(meta.get("context") or ""),
            research_brief_id=str(meta.get("research_brief_id") or ""),
            research_brief=dict(meta.get("research_brief") or {}),
            checkpoint=dict(meta.get("checkpoint") or {}),
        )
        missing = [
            str(section.get("id") or "")
            for section in sections
            if not fs.exists(f"sections/{section.get('id', '')}.md")
        ]
        resume_phase = "writing" if missing else "finalize"
        if resume_phase == "writing":
            _archive_finalize_checkpoint(fs)

        bus = EventBus(task_id=task_id, jsonl_path=fs.root / "events.jsonl")
        self._buses[task_id] = bus
        self._states[task_id] = state

        meta["status"] = "running"
        meta["resume_count"] = int(meta.get("resume_count") or 0) + 1
        meta["resume_started_at"] = time.time()
        meta["resume_phase"] = resume_phase
        meta.pop("finished_at", None)
        fs.write_atomic("task.json", json.dumps(meta, ensure_ascii=False, indent=2))

        async def runner():
            status = "done"
            try:
                bus.emit(TASK_STATUS, {"status": "running", "resume": resume_phase})
                if resume_phase == "writing":
                    await phase_write_sections(bus, state)
                stats = await phase_finalize(bus, state)
                state.mark_checkpoint("task", "completed")
                bus.emit(TASK_STATUS, {"status": "done", **stats})
            except Exception as exc:  # noqa: BLE001
                import traceback

                status = "failed"
                err = f"{type(exc).__name__}: {exc}"
                bus.emit(TASK_STATUS, {
                    "status": "failed",
                    "error": err,
                    "traceback": traceback.format_exc()[-2000:],
                })
                logger.exception("综述检查点恢复失败: %s", task_id)
            finally:
                bus.close()
                try:
                    latest = json.loads(fs.read("task.json"))
                    latest["status"] = status
                    latest["finished_at"] = time.time()
                    fs.write_atomic(
                        "task.json",
                        json.dumps(latest, ensure_ascii=False, indent=2),
                    )
                except Exception:
                    logger.exception("任务元数据更新失败")

        self._tasks[task_id] = asyncio.create_task(runner())
        return resume_phase

    def supplement_evidence(
        self,
        task_id: str,
        *,
        section_id: str,
        question_id: str,
        rounds: int = 2,
    ) -> None:
        """定向补证后重写单节，并使终稿检查点失效后重新整合。"""
        if self.is_active(task_id):
            raise RuntimeError("task is active")

        fs = WorkspaceFS(task_id)
        if not fs.exists("task.json"):
            raise FileNotFoundError(task_id)
        try:
            meta = json.loads(fs.read("task.json"))
        except json.JSONDecodeError as exc:
            raise ValueError("task metadata is invalid") from exc
        outline = meta.get("outline")
        sections = outline.get("sections", []) if isinstance(outline, dict) else []
        section = next(
            (item for item in sections if str(item.get("id")) == section_id),
            None,
        )
        if section is None:
            raise ValueError(f"section not found: {section_id}")
        questions = research_questions_for_section(section)
        try:
            question_index = int(question_id.removeprefix("Q")) - 1
        except ValueError as exc:
            raise ValueError(f"invalid question id: {question_id}") from exc
        if question_index < 0 or question_index >= len(questions):
            raise ValueError(f"question not found: {question_id}")
        if not fs.exists(f"checkpoints/evidence/{section_id}.json"):
            raise ValueError("evidence checkpoint is missing")

        notes: list[str] = []
        if fs.exists("notes.md"):
            notes = [
                line[2:].strip() if line.startswith("- ") else line.strip()
                for line in fs.read("notes.md").splitlines()
                if line.strip()
            ]
        state = SurveyState(
            task_id=task_id,
            topic=str(meta.get("topic") or outline.get("title") or task_id),
            fs=fs,
            outline=outline,
            completed_sections=list(meta.get("completed_sections") or []),
            notes=notes,
            section_length=str(meta.get("section_length") or "medium"),
            doc_scope=list(meta.get("doc_scope") or []),
            context=str(meta.get("context") or ""),
            research_brief_id=str(meta.get("research_brief_id") or ""),
            research_brief=dict(meta.get("research_brief") or {}),
            checkpoint=dict(meta.get("checkpoint") or {}),
        )
        bus = EventBus(task_id=task_id, jsonl_path=fs.root / "events.jsonl")
        self._buses[task_id] = bus
        self._states[task_id] = state
        meta["status"] = "running"
        meta["supplement_count"] = int(meta.get("supplement_count") or 0) + 1
        meta["supplement_started_at"] = time.time()
        meta["supplement_target"] = {
            "section_id": section_id,
            "question_id": question_id,
            "rounds": rounds,
        }
        meta.pop("finished_at", None)
        fs.write_atomic("task.json", json.dumps(meta, ensure_ascii=False, indent=2))

        async def runner():
            status = "done"
            try:
                bus.emit(TASK_STATUS, {
                    "status": "running",
                    "resume": "supplement",
                    "section": section_id,
                    "question": question_id,
                })
                supplement = await phase_supplement_section(
                    bus,
                    state,
                    section_id=section_id,
                    question_id=question_id,
                    rounds=rounds,
                )
                _archive_finalize_checkpoint(fs)
                stats = await phase_finalize(bus, state)
                state.mark_checkpoint("task", "completed")
                bus.emit(TASK_STATUS, {
                    "status": "done",
                    "supplemented_chunks": supplement["new_chunks"],
                    **stats,
                })
            except Exception as exc:  # noqa: BLE001
                import traceback

                status = "failed"
                err = f"{type(exc).__name__}: {exc}"
                bus.emit(TASK_STATUS, {
                    "status": "failed",
                    "error": err,
                    "traceback": traceback.format_exc()[-2000:],
                })
                logger.exception("综述定向补证失败: %s", task_id)
            finally:
                bus.close()
                try:
                    latest = json.loads(fs.read("task.json"))
                    latest["status"] = status
                    latest["finished_at"] = time.time()
                    fs.write_atomic(
                        "task.json",
                        json.dumps(latest, ensure_ascii=False, indent=2),
                    )
                except Exception:
                    logger.exception("任务元数据更新失败")

        self._tasks[task_id] = asyncio.create_task(runner())

    # ---- 查询 ----

    def get_bus(self, task_id: str) -> EventBus | None:
        return self._buses.get(task_id)

    def get_state(self, task_id: str) -> SurveyState | None:
        return self._states.get(task_id)

    def is_active(self, task_id: str) -> bool:
        t = self._tasks.get(task_id)
        return t is not None and not t.done()

    def list_tasks(self) -> list[dict[str, Any]]:
        """扫描 workspace 目录列出全部任务(含历史)。"""
        out = []
        if not settings.workspace_dir.exists():
            return out
        for d in sorted(settings.workspace_dir.iterdir(), reverse=True):
            meta_file = d / "task.json"
            if not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if self.is_active(meta.get("task_id", "")):
                meta["status"] = "running"
            elif meta.get("status") in (None, "running"):
                # CLI 任务(无 status)或进程重启导致的孤儿任务:按产物判断
                task_dir = d
                meta["status"] = (
                    "done" if (task_dir / "survey.md").exists() else "interrupted"
                )
            out.append(meta)
        return out

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Return one task with the same effective status used by the list API."""
        return next(
            (
                task
                for task in self.list_tasks()
                if str(task.get("task_id") or "") == task_id
            ),
            None,
        )

    def replay(self, task_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        """已结束任务的事件回放(读 events.jsonl)。"""
        fs = WorkspaceFS(task_id)
        events = replay_events(fs.root / "events.jsonl")
        return [e for e in events if e.get("seq", 0) > after_seq]

    def push_input(self, task_id: str, payload: dict) -> bool:
        state = self._states.get(task_id)
        if not state:
            return False
        if payload.get("kind") == "instruction":
            state.pending_instructions.append(str(payload.get("text", "")))
        else:
            state.push_user_input(payload)
        return True


manager = TaskManager()
