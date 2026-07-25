import asyncio

from backend import llm
from backend.pipelines import qa


def test_reflection_rejects_partial_research_question_coverage(monkeypatch):
    async def fake_complete_json(_system, _user, **_kwargs):
        return {
            "research_aspects": ["机理", "工业效果"],
            "covered_aspects": ["机理"],
            "missing_aspects": ["工业效果"],
            "sufficient": True,
            "gap": "缺少工业效果证据",
            "next_query": "电磁搅拌 工业应用 效果",
        }

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    result = asyncio.run(qa._reflect(
        "机理和工业效果如何",
        [{"content": "机理证据", "file_path": "a.pdf"}],
    ))

    assert result["sufficient"] is False
    assert result["coverage"]["covered_questions"] == 1
    assert result["coverage"]["total_questions"] == 2


def test_reflection_failure_is_not_treated_as_sufficient(monkeypatch):
    async def fake_complete_json(_system, _user, **_kwargs):
        raise ValueError("invalid json")

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    result = asyncio.run(qa._reflect("研究问题", []))

    assert result["sufficient"] is False
    assert result["next_query"] == "研究问题"
    assert result["coverage"]["covered_questions"] == 0
