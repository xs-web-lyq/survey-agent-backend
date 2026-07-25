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


def research_questions_for_section(section: dict[str, Any]) -> list[str]:
    """Return the canonical, bounded research-question list for a section.

    Retrieval, checkpoints, recovery validation, and the public matrix must all
    use this function so the UI never promises coverage for questions the
    engine will not process.
    """
    questions = list(dict.fromkeys(
        str(question).strip()
        for question in (
            section.get("points")
            or section.get("queries")
            or [section.get("title")]
        )
        if str(question).strip()
    ))[:MAX_RESEARCH_QUESTIONS]
    return questions or [str(section.get("title") or "本节核心研究问题").strip()]


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
        rows.append({
            "question": question,
            "covered": covered,
            "chunks": len(chunk_ids),
            "sources": len(sources),
        })
        all_sources.update(sources)
        all_chunks.update(chunk_ids)

    covered_count = sum(bool(row["covered"]) for row in rows)
    required_sources = min(3, max(2, len(rows)))
    source_diversity_met = len(all_sources) >= required_sources
    sufficient = covered_count == len(rows) and source_diversity_met
    uncovered = [str(row["question"]) for row in rows if not row["covered"]]
    gap_parts = []
    if uncovered:
        gap_parts.append("未覆盖：" + "；".join(uncovered[:3]))
    if not source_diversity_met:
        gap_parts.append(f"独立来源 {len(all_sources)}/{required_sources}")

    return {
        "sufficient": sufficient,
        "covered_questions": covered_count,
        "total_questions": len(rows),
        "coverage_ratio": covered_count / len(rows),
        "source_count": len(all_sources),
        "required_sources": required_sources,
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
