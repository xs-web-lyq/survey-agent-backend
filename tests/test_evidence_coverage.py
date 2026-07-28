from backend.agent.evidence_coverage import (
    assess_coverage,
    bind_brief_questions_to_outline,
    build_gap_query,
    research_questions_for_section,
    section_search_budget,
    select_balanced_evidence,
)


def _chunk(chunk_id: str, source: str) -> dict:
    return {"chunk_id": chunk_id, "source": source, "content": chunk_id}


def test_many_chunks_for_one_question_cannot_mark_section_sufficient():
    questions = ["机理是什么", "工业效果如何"]
    first_only = [
        [_chunk(f"a-{index}", f"source-{index}.pdf") for index in range(10)],
        [],
    ]

    coverage = assess_coverage(questions, first_only)

    assert coverage["sufficient"] is False
    assert coverage["covered_questions"] == 1
    assert coverage["total_questions"] == 2
    assert coverage["uncovered_questions"] == ["工业效果如何"]


def test_all_questions_need_independent_source_coverage():
    questions = ["机理", "参数", "工业效果"]
    evidence = [
        [_chunk(f"q{q}-a", "a.pdf"), _chunk(f"q{q}-b", "b.pdf")]
        for q in range(3)
    ]

    coverage = assess_coverage(questions, evidence)

    assert coverage["covered_questions"] == 3
    assert coverage["sufficient"] is False
    assert coverage["source_count"] == 2
    assert coverage["required_sources"] == 3


def test_complete_question_and_source_coverage_is_sufficient():
    questions = ["机理", "参数", "工业效果"]
    evidence = [
        [_chunk("q1-a", "a.pdf"), _chunk("q1-b", "b.pdf")],
        [_chunk("q2-b", "b.pdf"), _chunk("q2-c", "c.pdf")],
        [_chunk("q3-a", "a.pdf"), _chunk("q3-c", "c.pdf")],
    ]

    coverage = assess_coverage(questions, evidence)

    assert coverage["sufficient"] is True
    assert coverage["coverage_ratio"] == 1.0


def test_balanced_selection_keeps_each_research_question():
    groups = [
        [_chunk("q1-a", "a.pdf"), _chunk("q1-b", "b.pdf")],
        [_chunk("q2-a", "c.pdf"), _chunk("q2-b", "d.pdf")],
        [_chunk("q3-a", "e.pdf"), _chunk("q3-b", "f.pdf")],
    ]
    all_evidence = [chunk for group in groups for chunk in group]

    selected = select_balanced_evidence(groups, all_evidence, limit=3)

    assert [item["chunk_id"] for item in selected] == ["q1-a", "q2-a", "q3-a"]


def test_explicit_section_questions_are_the_retrieval_contract():
    section = {
        "title": "结晶器流动",
        "points": ["介绍工艺背景"],
        "research_questions": ["电磁制动如何改变弯月面流速？"],
    }

    assert research_questions_for_section(section) == [
        "电磁制动如何改变弯月面流速？",
    ]


def test_every_brief_question_is_bound_to_an_outline_section():
    outline = {
        "title": "连铸电磁控制",
        "sections": [
            {
                "id": "01",
                "title": "流动与传热机理",
                "points": ["结晶器流场"],
                "queries": ["流场模拟"],
            },
            {
                "id": "02",
                "title": "工业应用",
                "points": ["铸坯质量"],
                "queries": ["工业试验"],
            },
        ],
    }
    brief_questions = [
        "电磁制动如何改变结晶器流场？",
        "工业应用中铸坯缺陷改善幅度是多少？",
    ]

    bound = bind_brief_questions_to_outline(outline, brief_questions)
    assigned = [
        question
        for section in bound["sections"]
        for question in section["research_questions"]
    ]

    assert all(question in assigned for question in brief_questions)
    assert brief_questions[0] in bound["sections"][0]["research_questions"]
    assert brief_questions[1] in bound["sections"][1]["research_questions"]


def test_search_budget_leaves_bounded_gap_rounds():
    assert section_search_budget(1) == 4
    assert section_search_budget(2) == 5
    assert section_search_budget(5) == 8
    assert section_search_budget(99) == 8


def test_gap_query_prioritizes_source_diversity_and_rotates_strategy():
    section = {"title": "工业应用"}
    row = {"missing_sources": 1, "missing_chunks": 1}

    first, first_strategy = build_gap_query(
        section, "质量改善如何", row, attempt=1,
    )
    second, second_strategy = build_gap_query(
        section, "质量改善如何", row, attempt=2,
    )

    assert first_strategy == "source_diversity"
    assert "多来源" in first
    assert second_strategy == "quantitative_evidence"
    assert first != second


def test_coverage_explains_question_level_deficits():
    coverage = assess_coverage(
        ["工业效果如何"],
        [[_chunk("q1-a", "a.pdf")]],
    )
    row = coverage["questions"][0]

    assert row["status"] == "missing_sources"
    assert row["missing_chunks"] == 1
    assert row["missing_sources"] == 1
    assert "证据块差 1" in coverage["gap"]
