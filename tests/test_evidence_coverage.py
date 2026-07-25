from backend.agent.evidence_coverage import assess_coverage, select_balanced_evidence


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
