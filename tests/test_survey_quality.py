import asyncio
import json

from backend.agent.evidence_store import save_section_checkpoint
from backend.agent.phases import phase_finalize
from backend.agent.state import SurveyState
from backend.agent.survey_quality import (
    build_integration_contract,
    build_quality_report,
)
from backend.config import settings
from backend.events import EventBus
from backend.tools.files import WorkspaceFS


def _matrix(*, covered: bool = True) -> dict:
    return {
        "schema_version": 2,
        "summary": {
            "questions_total": 1,
            "questions_covered": int(covered),
        },
        "sections": [{
            "section_id": "01",
            "title": "工业应用",
            "status": "written",
            "stop_reason": (
                "coverage_satisfied" if covered else "plateau"
            ),
            "coverage": {
                "sufficient": covered,
                "gap": "" if covered else "独立来源不足",
            },
            "questions": [{
                "id": "Q1",
                "question": "工业效果如何？",
                "covered": covered,
                "chunks": 2 if covered else 1,
                "sources": 2 if covered else 1,
                "evidence": [{
                    "chunk_id": "chunk-private",
                    "source": "paper.pdf",
                    "preview": "不应进入终稿约束的大段证据",
                }],
            }],
        }],
    }


def test_quality_report_is_ready_only_when_all_delivery_gates_pass():
    report = build_quality_report(
        task_id="survey-test",
        research_brief={"research_questions": ["工业效果如何？"]},
        evidence_matrix=_matrix(),
        citations_total=1,
        citations_passed=1,
        failed_chunk_ids=set(),
        bibliography_records=[{
            "source": "paper.pdf",
            "title": "Industrial validation",
            "metadata_status": "complete",
            "missing_fields": [],
        }],
    )

    assert report["overall_status"] == "ready"
    assert report["summary"]["gates_action_required"] == 0
    assert report["summary"]["brief_questions_covered"] == 1
    assert all(
        gate["status"] in {"pass", "not_applicable"}
        for gate in report["gates"]
    )


def test_quality_report_exposes_evidence_citation_and_metadata_actions():
    report = build_quality_report(
        task_id="survey-test",
        research_brief={"research_questions": ["工业效果如何？"]},
        evidence_matrix=_matrix(covered=False),
        citations_total=1,
        citations_passed=0,
        failed_chunk_ids={"chunk-failed"},
        bibliography_records=[{
            "source": "paper.pdf",
            "title": "Industrial validation",
            "metadata_status": "partial",
            "missing_fields": ["authors", "pages"],
        }],
    )

    assert report["overall_status"] == "review_required"
    assert report["summary"]["gates_action_required"] == 4
    assert report["citation_review"]["failed_chunk_ids"] == ["chunk-failed"]
    assert report["bibliography_review"]["incomplete_references"][0][
        "missing_fields"
    ] == ["authors", "pages"]
    assert len(report["recommendations"]) == 3


def test_empty_delivery_cannot_pass_quality_gates():
    report = build_quality_report(
        task_id="survey-empty",
        research_brief=None,
        evidence_matrix={"sections": []},
        citations_total=0,
        citations_passed=0,
        failed_chunk_ids=set(),
        bibliography_records=[],
    )

    assert report["overall_status"] == "review_required"
    assert report["summary"]["gates_action_required"] == 3
    assert "尚无可核查的研究问题证据矩阵" in report["recommendations"][0]


def test_integration_contract_keeps_boundaries_but_omits_evidence_previews():
    contract = build_integration_contract(
        {
            "topic": "连铸电磁搅拌",
            "research_questions": ["工业效果如何？"],
            "inclusion_criteria": ["纳入工业试验"],
            "exclusion_criteria": ["排除非连铸过程"],
        },
        _matrix(covered=False),
    )

    assert contract["research_questions"] == ["工业效果如何？"]
    assert contract["sections"][0]["questions"][0]["covered"] is False
    assert contract["sections"][0]["stop_reason"] == "plateau"
    assert "preview" not in str(contract)


def test_finalize_persists_quality_report_from_evidence_and_citations(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    fs = WorkspaceFS("survey-quality")
    outline = {
        "title": "连铸工业验证",
        "sections": [{
            "id": "01",
            "title": "工业应用",
            "points": ["工业效果"],
            "research_questions": ["工业效果如何？"],
        }],
    }
    section = outline["sections"][0]
    chunks = [[
        {
            "chunk_id": "chunk-a",
            "source": "a.pdf",
            "content": "工业效果证据 A",
        },
        {
            "chunk_id": "chunk-b",
            "source": "b.pdf",
            "content": "工业效果证据 B",
        },
    ]]
    save_section_checkpoint(
        fs,
        task_id="survey-quality",
        outline=outline,
        section=section,
        research_questions=section["research_questions"],
        evidence_by_question=chunks,
        used_queries={"工业效果"},
        round_no=1,
        max_rounds=4,
        status="written",
        stop_reason="coverage_satisfied",
    )
    fs.write("sections/01.md", "## 工业应用\n工业效果得到验证 [[chunk-a]]。")

    async def fake_stream(*_args, **_kwargs):
        return "# 连铸工业验证\n\n工业效果得到验证 [[chunk-a]]。"

    async def fake_verify(*_args, **_kwargs):
        return {"verdict": "pass"}

    async def fake_chunk(*_args, **_kwargs):
        return {"file_path": "a.pdf"}

    async def fake_bibliography(*_args, **_kwargs):
        return [{
            "source": "a.pdf",
            "title": "工业验证",
            "authors": ["作者"],
            "year": 2025,
            "journal": "期刊",
            "volume": "1",
            "issue": "1",
            "pages": "1-5",
            "document_type": "journal",
            "metadata_status": "complete",
            "missing_fields": [],
        }]

    monkeypatch.setattr("backend.agent.phases._llm_stream", fake_stream)
    monkeypatch.setattr(
        "backend.agent.phases.verify.verify_citation", fake_verify,
    )
    monkeypatch.setattr(
        "backend.agent.phases.rag_client.get_chunk_by_id", fake_chunk,
    )
    monkeypatch.setattr(
        "backend.agent.phases.bibliography.resolve_sources", fake_bibliography,
    )

    state = SurveyState(
        task_id="survey-quality",
        topic="连铸工业验证",
        fs=fs,
        outline=outline,
        research_brief={"research_questions": ["工业效果如何？"]},
    )
    bus = EventBus("survey-quality", fs.root / "events.jsonl")
    stats = asyncio.run(phase_finalize(bus, state))
    bus.close()
    report = json.loads(fs.read("quality_report.json"))

    assert stats["quality_status"] == "ready"
    assert report["overall_status"] == "ready"
    assert report["summary"]["research_questions_covered"] == 1
    assert any(
        item["path"] == "quality_report.json" for item in fs.list()
    )
