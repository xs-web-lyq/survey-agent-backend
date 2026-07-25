import asyncio
import json

from backend.agent.evidence_store import (
    load_section_checkpoint,
    read_evidence_matrix,
    save_section_checkpoint,
)
from backend.agent.evidence_coverage import (
    MAX_RESEARCH_QUESTIONS,
    research_questions_for_section,
)
from backend.agent.phases import phase_supplement_section, phase_write_sections
from backend.agent.state import SurveyState
from backend.config import settings
from backend.events import EventBus
from backend.tools.files import WorkspaceFS


def _chunk(chunk_id: str, source: str, content: str = "evidence") -> dict:
    return {
        "chunk_id": chunk_id,
        "source": source,
        "content": content,
        "score": 0.9,
    }


def _workspace(monkeypatch, tmp_path, task_id: str = "survey-checkpoint") -> WorkspaceFS:
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    return WorkspaceFS(task_id)


def test_checkpoint_and_public_matrix_have_separate_payloads(monkeypatch, tmp_path):
    fs = _workspace(monkeypatch, tmp_path)
    outline = {
        "title": "测试",
        "sections": [{
            "id": "01",
            "title": "机理与应用",
            "points": ["作用机理", "工业应用"],
        }],
    }
    section = outline["sections"][0]
    groups = [
        [_chunk("chunk-q1-a", "a.pdf", "A" * 300), _chunk("chunk-q1-b", "b.pdf")],
        [_chunk("chunk-q2-a", "b.pdf"), _chunk("chunk-q2-b", "c.pdf")],
    ]

    save_section_checkpoint(
        fs,
        task_id="survey-checkpoint",
        outline=outline,
        section=section,
        research_questions=section["points"],
        evidence_by_question=groups,
        used_queries={"作用机理", "工业应用"},
        round_no=2,
        max_rounds=5,
        status="ready",
    )

    checkpoint = load_section_checkpoint(
        fs, "01", expected_questions=section["points"],
    )
    matrix = read_evidence_matrix(
        fs, task_id="survey-checkpoint", outline=outline,
    )

    assert checkpoint is not None
    assert checkpoint["evidence_by_question"][0][0]["content"] == "A" * 300
    assert matrix["summary"]["questions_covered"] == 2
    assert matrix["summary"]["sections_sufficient"] == 1
    assert matrix["sections"][0]["questions"][0]["evidence"][0]["preview"] == "A" * 180
    assert "content" not in matrix["sections"][0]["questions"][0]["evidence"][0]


def test_question_changes_invalidate_old_checkpoint(monkeypatch, tmp_path):
    fs = _workspace(monkeypatch, tmp_path)
    outline = {
        "sections": [{"id": "01", "title": "章节", "points": ["旧问题"]}],
    }
    save_section_checkpoint(
        fs,
        task_id="survey-checkpoint",
        outline=outline,
        section=outline["sections"][0],
        research_questions=["旧问题"],
        evidence_by_question=[[_chunk("chunk-a", "a.pdf")]],
        used_queries={"旧问题"},
        round_no=1,
        max_rounds=5,
        status="retrieving",
    )

    assert load_section_checkpoint(
        fs, "01", expected_questions=["新问题"],
    ) is None


def test_public_matrix_uses_same_question_limit_as_retrieval(monkeypatch, tmp_path):
    fs = _workspace(monkeypatch, tmp_path)
    section = {
        "id": "01",
        "title": "章节",
        "points": [f"问题 {index}" for index in range(1, 8)],
    }
    outline = {"title": "测试", "sections": [section]}

    matrix = read_evidence_matrix(
        fs, task_id="survey-checkpoint", outline=outline,
    )

    assert len(research_questions_for_section(section)) == MAX_RESEARCH_QUESTIONS
    assert matrix["summary"]["questions_total"] == MAX_RESEARCH_QUESTIONS
    assert [
        row["question"] for row in matrix["sections"][0]["questions"]
    ] == section["points"][:MAX_RESEARCH_QUESTIONS]


def test_section_recovery_continues_from_next_retrieval_round(
    monkeypatch, tmp_path,
):
    fs = _workspace(monkeypatch, tmp_path)
    outline = {
        "title": "连铸测试",
        "sections": [{
            "id": "01",
            "title": "机理与应用",
            "points": ["作用机理", "工业应用"],
            "queries": ["机理检索", "应用检索"],
        }],
    }
    save_section_checkpoint(
        fs,
        task_id="survey-checkpoint",
        outline=outline,
        section=outline["sections"][0],
        research_questions=outline["sections"][0]["points"],
        evidence_by_question=[[
            _chunk("chunk-q1-a", "a.pdf"),
            _chunk("chunk-q1-b", "b.pdf"),
        ], []],
        used_queries={"机理检索"},
        round_no=1,
        max_rounds=5,
        status="retrieving",
    )
    calls: list[str] = []

    async def fake_search(query: str, **_kwargs):
        calls.append(query)
        return {
            "chunks": [
                _chunk("chunk-q2-a", "b.pdf"),
                _chunk("chunk-q2-b", "c.pdf"),
            ],
        }

    async def fake_stream(*_args, **_kwargs):
        return "恢复后的章节内容 [E1]"

    monkeypatch.setattr(
        "backend.agent.phases.retrieval.search_evidence", fake_search,
    )
    monkeypatch.setattr("backend.agent.phases._llm_stream", fake_stream)
    monkeypatch.setattr(
        "backend.images.find_images_for_text", lambda *_args, **_kwargs: [],
    )

    state = SurveyState(
        task_id="survey-checkpoint",
        topic="连铸测试",
        fs=fs,
        outline=outline,
    )
    bus = EventBus("survey-checkpoint", fs.root / "events.jsonl")
    asyncio.run(phase_write_sections(bus, state))
    bus.close()

    checkpoint = load_section_checkpoint(
        fs, "01", expected_questions=outline["sections"][0]["points"],
    )
    meta = json.loads(fs.read("task.json"))

    assert calls == ["应用检索"]
    assert checkpoint is not None
    assert checkpoint["round"] == 2
    assert checkpoint["status"] == "written"
    assert checkpoint["coverage"]["sufficient"] is True
    assert state.completed_sections == ["01"]
    assert meta["checkpoint"]["status"] == "completed"


def test_targeted_supplement_rewrites_only_the_selected_section(
    monkeypatch, tmp_path,
):
    fs = _workspace(monkeypatch, tmp_path)
    outline = {
        "title": "连铸测试",
        "sections": [{
            "id": "01",
            "title": "机理与应用",
            "points": ["作用机理", "工业应用"],
            "queries": ["机理检索", "应用检索"],
        }],
    }
    save_section_checkpoint(
        fs,
        task_id="survey-checkpoint",
        outline=outline,
        section=outline["sections"][0],
        research_questions=outline["sections"][0]["points"],
        evidence_by_question=[
            [_chunk("chunk-q1-a", "a.pdf"), _chunk("chunk-q1-b", "b.pdf")],
            [_chunk("chunk-q2-a", "b.pdf")],
        ],
        used_queries={"机理检索", "应用检索"},
        round_no=5,
        max_rounds=5,
        status="written",
    )
    fs.write("sections/01.md", "旧章节")
    calls: list[str] = []

    async def fake_search(query: str, **_kwargs):
        calls.append(query)
        return {"chunks": [_chunk("chunk-q2-b", "c.pdf", "新增工业证据")]}

    async def fake_stream(*_args, **_kwargs):
        return "修订章节 [E1]"

    monkeypatch.setattr(
        "backend.agent.phases.retrieval.search_evidence", fake_search,
    )
    monkeypatch.setattr("backend.agent.phases._llm_stream", fake_stream)
    monkeypatch.setattr(
        "backend.images.find_images_for_text", lambda *_args, **_kwargs: [],
    )

    state = SurveyState(
        task_id="survey-checkpoint",
        topic="连铸测试",
        fs=fs,
        outline=outline,
        completed_sections=["01"],
    )
    bus = EventBus("survey-checkpoint", fs.root / "events.jsonl")
    result = asyncio.run(phase_supplement_section(
        bus,
        state,
        section_id="01",
        question_id="Q2",
        rounds=2,
    ))
    bus.close()

    checkpoint = load_section_checkpoint(
        fs, "01", expected_questions=outline["sections"][0]["points"],
    )
    event_types = [
        json.loads(line)["type"]
        for line in fs.read("events.jsonl").splitlines()
    ]

    assert len(calls) == 1
    assert result["new_chunks"] == 1
    assert checkpoint is not None
    assert checkpoint["status"] == "written"
    assert checkpoint["round"] == 6
    assert checkpoint["coverage"]["sufficient"] is True
    assert fs.read("sections/01.md").startswith("修订章节")
    assert "stream_reset" in event_types
