import asyncio

from backend.events import DEEP_ROUND, TOOL_CALL, EventBus
from backend.pipelines import brainstorm


def _scope(prefix: str, count: int) -> dict:
    return {
        "kb_total_documents": 100,
        "related_documents": [
            {"source": f"{prefix}-{i}.pdf", "hit_chunks": 1}
            for i in range(count)
        ],
        "key_entities": [f"{prefix}-entity"],
        "key_relations": [],
    }


def test_brainstorm_expands_search_when_coverage_is_low(monkeypatch):
    scopes = {
        "narrow topic": _scope("initial", 2),
        "parent topic": _scope("parent", 5),
        "adjacent topic": _scope("adjacent", 6),
    }

    async def fake_scope(query: str):
        return scopes[query]

    async def fake_complete_json(_system, _user, **_kwargs):
        return {"queries": ["parent topic", "adjacent topic"]}

    async def fake_stream(_system, _user, **_kwargs):
        yield "analysis complete"

    monkeypatch.setattr(brainstorm.retrieval, "survey_scope", fake_scope)
    monkeypatch.setattr(brainstorm.llm, "complete_json", fake_complete_json)
    monkeypatch.setattr(brainstorm.llm, "stream", fake_stream)

    bus = EventBus("test-brainstorm")
    result = asyncio.run(brainstorm.run_brainstorm(bus, "narrow topic", []))

    assert result["scope_brief"]["search_rounds"] == 3
    assert len(result["scope_brief"]["related_documents"]) == 13
    assert result["scope_brief"]["evidence_status"] == "sufficient"
    assert len([event for event in bus.history if event.type == TOOL_CALL]) == 3
    assert any(event.type == DEEP_ROUND for event in bus.history)


def test_conclusion_caps_readiness_when_evidence_is_insufficient(monkeypatch):
    async def fake_complete_json(_system, _user, **_kwargs):
        return {
            "topic": "A focused survey",
            "section_hints": ["mechanism"],
            "doc_keywords": ["EMS"],
            "summary": "summary",
            "research_questions": ["question"],
            "inclusion_criteria": ["criterion"],
            "exclusion_criteria": [],
            "evidence_gaps": ["missing evidence"],
            "readiness_score": 92,
            "readiness_reason": "topic is clear",
        }

    monkeypatch.setattr(brainstorm.llm, "complete_json", fake_complete_json)
    scope = _scope("limited", 3)
    result = asyncio.run(brainstorm.conclude_brainstorm([], scope))

    assert result["readiness_score"] == 45
    assert result["evidence_documents"] == 3
    assert len(result["doc_scope"]) == 3

    one_turn = asyncio.run(brainstorm.conclude_brainstorm(
        [{"role": "user", "content": "broad direction"}], _scope("rich", 15),
    ))
    assert one_turn["readiness_score"] == 65
    assert "仅完成一轮" in one_turn["readiness_reason"]
