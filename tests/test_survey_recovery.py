import asyncio
import json

import backend.task_manager as task_manager_module
from backend.config import settings
from backend.events import EventBus
from backend.llm import _is_retryable
from backend.task_manager import (
    TaskManager,
    _archive_finalize_checkpoint,
)
from backend.tools.files import WorkspaceFS


class _ProviderError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def test_provider_inner_500_is_retryable_even_with_invalid_parameter_wrapper():
    exc = _ProviderError(
        "InvalidParameter: <500> InternalError.Algo: Receive batching backend response failed!",
        status_code=400,
    )
    assert _is_retryable(exc)
    assert _is_retryable(_ProviderError("rate limit", status_code=429))
    assert not _is_retryable(_ProviderError("invalid prompt", status_code=400))


def test_event_bus_resume_keeps_history_and_sequence(tmp_path):
    event_file = tmp_path / "events.jsonl"
    first = EventBus("survey-test", event_file)
    first.emit("task_status", {"status": "failed"})
    first.close()

    resumed = EventBus("survey-test", event_file)
    event = resumed.emit("task_status", {"status": "running", "resume": "finalize"})

    assert event.seq == 2
    assert [item.seq for item in resumed.history] == [1, 2]
    persisted = [json.loads(line) for line in event_file.read_text(encoding="utf-8").splitlines()]
    assert [item["seq"] for item in persisted] == [1, 2]


def test_stale_finalize_checkpoint_is_archived(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    fs = WorkspaceFS("survey-test")
    fs.write("finalize_draft.md", "旧终稿")

    destination = _archive_finalize_checkpoint(fs)

    assert destination is not None
    assert not fs.exists("finalize_draft.md")
    assert fs.read(destination) == "旧终稿"


def test_failed_supplement_keeps_existing_finalize_checkpoint(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    fs = WorkspaceFS("survey-test")
    fs.write("finalize_draft.md", "旧终稿")
    fs.write("checkpoints/evidence/01.json", "{}")
    fs.write_atomic("task.json", json.dumps({
        "task_id": "survey-test",
        "topic": "测试",
        "status": "done",
        "outline": {
            "title": "测试综述",
            "sections": [{
                "id": "01",
                "title": "测试章节",
                "points": ["研究问题"],
            }],
        },
        "completed_sections": ["01"],
    }, ensure_ascii=False))

    async def fail_before_rewrite(*_args, **_kwargs):
        raise RuntimeError("retrieval unavailable")

    async def finalize_must_not_run(*_args, **_kwargs):
        raise AssertionError("finalize must not run after supplement failure")

    monkeypatch.setattr(
        task_manager_module, "phase_supplement_section", fail_before_rewrite
    )
    monkeypatch.setattr(
        task_manager_module, "phase_finalize", finalize_must_not_run
    )

    async def exercise():
        manager = TaskManager()
        manager.supplement_evidence(
            "survey-test",
            section_id="01",
            question_id="Q1",
            rounds=1,
        )
        await manager._tasks["survey-test"]

    asyncio.run(exercise())

    assert fs.read("finalize_draft.md") == "旧终稿"
    assert not fs.exists("checkpoints/archive")
    assert json.loads(fs.read("task.json"))["status"] == "failed"


def test_hard_restart_running_task_is_reported_as_interrupted(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    fs = WorkspaceFS("survey-test")
    fs.write_atomic("task.json", json.dumps({
        "task_id": "survey-test",
        "topic": "测试",
        "status": "running",
    }, ensure_ascii=False))

    task = TaskManager().get_task("survey-test")

    assert task is not None
    assert task["status"] == "interrupted"
