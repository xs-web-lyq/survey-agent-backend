"""Research-question coverage assessment for survey evidence.

Chunk count is a writing-capacity signal, not a sufficiency decision.  This
module keeps the decision tied to explicit research questions and independent
sources, while remaining deterministic and cheap enough to run after every
retrieval round.
"""

from __future__ import annotations

from typing import Any

MIN_CHUNKS_PER_QUESTION = 2
MIN_SOURCES_PER_QUESTION = 2
MAX_RESEARCH_QUESTIONS = 5
MAX_SECTION_SEARCH_ROUNDS = 8
EXTRA_GAP_ROUNDS = 3


def research_questions_for_section(section: dict[str, Any]) -> list[str]:
    """Return the canonical, bounded research-question list for a section.

    Retrieval, checkpoints, recovery validation, and the public matrix must all
    use this function so the UI never promises coverage for questions the
    engine will not process.
    """
    questions = list(dict.fromkeys(
        str(question).strip()
        for question in (
            section.get("research_questions")
            or section.get("points")
            or section.get("queries")
            or [section.get("title")]
        )
        if str(question).strip()
    ))[:MAX_RESEARCH_QUESTIONS]
    return questions or [str(section.get("title") or "本节核心研究问题").strip()]


def _terms(text: str) -> set[str]:
    compact = "".join(character.casefold() for character in str(text) if not character.isspace())
    words = {
        token.strip("，。！？；：,.!?;:()（）[]【】")
        for token in str(text).casefold().split()
        if token.strip("，。！？；：,.!?;:()（）[]【】")
    }
    words.update(
        compact[index:index + 2]
        for index in range(max(0, len(compact) - 1))
    )
    return {term for term in words if term}


def bind_brief_questions_to_outline(
    outline: dict[str, Any],
    brief_questions: list[str],
) -> dict[str, Any]:
    """Bind every confirmed Brief question to the most relevant outline section.

    The LLM may paraphrase or omit a global question while creating the outline.
    This deterministic pass makes the Research Brief an enforceable contract:
    every global question appears in at least one section's canonical question
    list and therefore enters retrieval, checkpointing, and the public matrix.
    """
    sections = [
        dict(section)
        for section in (outline.get("sections") or [])
        if isinstance(section, dict)
    ]
    if not sections:
        return outline
    for section in sections:
        section["research_questions"] = research_questions_for_section(section)

    normalized = list(dict.fromkeys(
        str(question).strip()
        for question in brief_questions
        if str(question).strip()
    ))[:MAX_RESEARCH_QUESTIONS]
    for question in normalized:
        if any(question in section["research_questions"] for section in sections):
            continue
        question_terms = _terms(question)
        candidates = [
            index for index, section in enumerate(sections)
            if len(section["research_questions"]) < MAX_RESEARCH_QUESTIONS
        ] or list(range(len(sections)))
        best_index = max(
            candidates,
            key=lambda index: len(question_terms & _terms(" ".join([
                str(sections[index].get("title") or ""),
                *[str(item) for item in sections[index].get("points") or []],
                *[str(item) for item in sections[index].get("queries") or []],
            ]))),
        )
        questions = sections[best_index]["research_questions"]
        if len(questions) >= MAX_RESEARCH_QUESTIONS:
            replace_index = next(
                (
                    index for index, existing in enumerate(questions)
                    if existing not in normalized
                ),
                None,
            )
            if replace_index is None:
                continue
            questions[replace_index] = question
        else:
            questions.append(question)
    return {**outline, "sections": sections}


def section_search_budget(question_count: int) -> int:
    count = max(1, min(MAX_RESEARCH_QUESTIONS, int(question_count or 1)))
    return min(MAX_SECTION_SEARCH_ROUNDS, count + EXTRA_GAP_ROUNDS)


def build_gap_query(
    section: dict[str, Any],
    question: str,
    coverage_row: dict[str, Any],
    *,
    attempt: int,
) -> tuple[str, str]:
    """Return a deterministic query and the strategy used for one evidence gap."""
    prefix = f"{section.get('title', '')} {question}".strip()
    variants = (
        ("source_diversity", "对比研究 工业试验 多钢种 多来源"),
        ("quantitative_evidence", "定量结果 参数范围 实验数据 统计"),
        ("mechanism_evidence", "作用机制 因果关系 模型验证"),
    )
    if int(coverage_row.get("missing_sources", 0) or 0) > 0:
        preferred = 0
    elif int(coverage_row.get("missing_chunks", 0) or 0) > 0:
        preferred = 1
    else:
        preferred = 2
    strategy, suffix = variants[(preferred + max(0, attempt - 1)) % len(variants)]
    return f"{prefix} {suffix}".strip(), strategy


def _deduplicate(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for chunk in chunks:
        key = str(chunk.get("chunk_id") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(chunk)
    return result


def assess_coverage(
    questions: list[str],
    evidence_by_question: list[list[dict[str, Any]]],
) -> dict[str, Any]:
    """Return a JSON-serializable coverage report for explicit questions."""
    normalized = [str(question).strip() for question in questions if str(question).strip()]
    if not normalized:
        normalized = ["本节核心研究问题"]

    rows = []
    all_sources: set[str] = set()
    all_chunks: set[str] = set()
    for index, question in enumerate(normalized):
        chunks = _deduplicate(
            evidence_by_question[index] if index < len(evidence_by_question) else []
        )
        sources = {str(chunk.get("source") or "") for chunk in chunks if chunk.get("source")}
        chunk_ids = {str(chunk.get("chunk_id") or "") for chunk in chunks if chunk.get("chunk_id")}
        covered = (
            len(chunk_ids) >= MIN_CHUNKS_PER_QUESTION
            and len(sources) >= MIN_SOURCES_PER_QUESTION
        )
        missing_chunks = max(0, MIN_CHUNKS_PER_QUESTION - len(chunk_ids))
        missing_sources = max(0, MIN_SOURCES_PER_QUESTION - len(sources))
        rows.append({
            "question": question,
            "covered": covered,
            "status": "covered" if covered else (
                "missing_sources" if missing_sources else "missing_evidence"
            ),
            "chunks": len(chunk_ids),
            "sources": len(sources),
            "missing_chunks": missing_chunks,
            "missing_sources": missing_sources,
        })
        all_sources.update(sources)
        all_chunks.update(chunk_ids)

    covered_count = sum(bool(row["covered"]) for row in rows)
    required_sources = min(3, max(2, len(rows)))
    source_diversity_met = len(all_sources) >= required_sources
    sufficient = covered_count == len(rows) and source_diversity_met
    uncovered_rows = [row for row in rows if not row["covered"]]
    uncovered = [str(row["question"]) for row in uncovered_rows]
    gap_parts = []
    if uncovered_rows:
        details = []
        for row in uncovered_rows[:3]:
            needs = []
            if row["missing_chunks"]:
                needs.append(f"证据块差 {row['missing_chunks']}")
            if row["missing_sources"]:
                needs.append(f"独立来源差 {row['missing_sources']}")
            details.append(f"{row['question']}（{'、'.join(needs)}）")
        gap_parts.append("未覆盖：" + "；".join(details))
    if not source_diversity_met:
        gap_parts.append(f"独立来源 {len(all_sources)}/{required_sources}")

    return {
        "sufficient": sufficient,
        "covered_questions": covered_count,
        "total_questions": len(rows),
        "coverage_ratio": covered_count / len(rows),
        "decision": "sufficient" if sufficient else "insufficient",
        "source_count": len(all_sources),
        "required_sources": required_sources,
        "source_diversity_met": source_diversity_met,
        "chunk_count": len(all_chunks),
        "questions": rows,
        "uncovered_questions": uncovered,
        "gap": "；".join(gap_parts),
    }


def select_balanced_evidence(
    evidence_by_question: list[list[dict[str, Any]]],
    all_evidence: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """Round-robin evidence selection prevents early questions dominating."""
    groups = [_deduplicate(group) for group in evidence_by_question]
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    depth = 0
    while len(selected) < limit and any(depth < len(group) for group in groups):
        for group in groups:
            if depth >= len(group):
                continue
            chunk = group[depth]
            key = str(chunk.get("chunk_id") or "")
            if key and key not in seen:
                seen.add(key)
                selected.append(chunk)
                if len(selected) >= limit:
                    return selected
        depth += 1

    for chunk in _deduplicate(all_evidence):
        key = str(chunk.get("chunk_id") or "")
        if key and key not in seen:
            seen.add(key)
            selected.append(chunk)
            if len(selected) >= limit:
                break
    return selected
