"""Deterministic quality gate between evidence retrieval and survey delivery."""

from __future__ import annotations

import time
from typing import Any


def build_integration_contract(
    research_brief: dict[str, Any] | None,
    evidence_matrix: dict[str, Any],
) -> dict[str, Any]:
    """Return the compact, non-manuscript constraints passed to final editing."""
    brief = research_brief if isinstance(research_brief, dict) else {}
    return {
        "topic": str(brief.get("topic") or ""),
        "research_questions": [
            str(question)
            for question in brief.get("research_questions") or []
            if str(question).strip()
        ],
        "inclusion_criteria": list(brief.get("inclusion_criteria") or []),
        "exclusion_criteria": list(brief.get("exclusion_criteria") or []),
        "sections": [
            {
                "section_id": section.get("section_id"),
                "title": section.get("title"),
                "sufficient": bool(
                    (section.get("coverage") or {}).get("sufficient")
                ),
                "gap": str((section.get("coverage") or {}).get("gap") or ""),
                "stop_reason": str(section.get("stop_reason") or ""),
                "questions": [
                    {
                        "question": row.get("question"),
                        "covered": bool(row.get("covered")),
                    }
                    for row in section.get("questions") or []
                ],
            }
            for section in evidence_matrix.get("sections") or []
        ],
    }


def _gate(gate_id: str, label: str, status: str, detail: str) -> dict[str, str]:
    return {
        "id": gate_id,
        "label": label,
        "status": status,
        "detail": detail,
    }


def build_quality_report(
    *,
    task_id: str,
    research_brief: dict[str, Any] | None,
    evidence_matrix: dict[str, Any],
    citations_total: int,
    citations_passed: int,
    failed_chunk_ids: set[str] | list[str],
    bibliography_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a persisted delivery report without asking an LLM to self-grade."""
    sections = list(evidence_matrix.get("sections") or [])
    matrix_questions = [
        {
            **row,
            "section_id": str(section.get("section_id") or ""),
            "section_title": str(section.get("title") or ""),
        }
        for section in sections
        for row in section.get("questions") or []
    ]
    brief = research_brief if isinstance(research_brief, dict) else {}
    brief_questions = [
        str(question).strip()
        for question in brief.get("research_questions") or []
        if str(question).strip()
    ]
    brief_rows = []
    for question in brief_questions:
        matches = [
            row for row in matrix_questions
            if str(row.get("question") or "").strip() == question
        ]
        brief_rows.append({
            "question": question,
            "assigned_sections": list(dict.fromkeys(
                str(row.get("section_id") or "") for row in matches
                if row.get("section_id")
            )),
            "covered": bool(matches) and any(
                bool(row.get("covered")) for row in matches
            ),
            "chunks": sum(int(row.get("chunks") or 0) for row in matches),
            "sources": max(
                (int(row.get("sources") or 0) for row in matches),
                default=0,
            ),
        })

    uncovered = [row for row in matrix_questions if not row.get("covered")]
    missing_brief = [row for row in brief_rows if not row["assigned_sections"]]
    uncovered_brief = [row for row in brief_rows if not row["covered"]]
    failed_ids = sorted(set(str(item) for item in failed_chunk_ids if str(item)))
    incomplete_references = [
        {
            "source": str(record.get("source") or ""),
            "title": str(record.get("title") or ""),
            "missing_fields": list(record.get("missing_fields") or []),
        }
        for record in bibliography_records
        if record.get("metadata_status") != "complete"
    ]
    references_total = len(bibliography_records)
    references_complete = references_total - len(incomplete_references)

    gates = []
    if brief_questions:
        brief_status = (
            "action_required"
            if missing_brief or uncovered_brief
            else "pass"
        )
        gates.append(_gate(
            "research_brief",
            "研究简报问题闭环",
            brief_status,
            (
                f"{len(brief_questions) - len(uncovered_brief)}/{len(brief_questions)} "
                f"个简报问题已有充分证据；{len(missing_brief)} 个未分配"
            ),
        ))
    else:
        gates.append(_gate(
            "research_brief",
            "研究简报问题闭环",
            "not_applicable",
            "该任务未由研究简报创建",
        ))

    evidence_status = (
        "pass"
        if matrix_questions and not uncovered
        else "action_required"
    )
    gates.append(_gate(
        "evidence_coverage",
        "研究问题证据覆盖",
        evidence_status,
        (
            f"{len(matrix_questions) - len(uncovered)}/{len(matrix_questions)} "
            "个研究问题满足证据块和独立来源门槛"
        ),
    ))

    citations_failed = len(failed_ids)
    citation_status = (
        "pass"
        if citations_total > 0 and citations_failed == 0
        else "action_required"
    )
    gates.append(_gate(
        "citation_verification",
        "正文引用核查",
        citation_status,
        (
            f"{citations_passed}/{citations_total} 个唯一引用通过原文核查，"
            f"{citations_failed} 个需人工复核"
        ),
    ))

    bibliography_status = (
        "pass"
        if references_total > 0 and not incomplete_references
        else "action_required"
    )
    gates.append(_gate(
        "bibliography_metadata",
        "参考文献元数据",
        bibliography_status,
        f"{references_complete}/{references_total} 条达到论文引用字段要求",
    ))

    actionable = [gate for gate in gates if gate["status"] == "action_required"]
    warnings = [gate for gate in gates if gate["status"] == "warning"]
    if actionable:
        overall_status = "review_required"
    elif warnings:
        overall_status = "ready_with_warnings"
    else:
        overall_status = "ready"

    recommendations = []
    if uncovered:
        recommendations.append("对未覆盖研究问题执行定向补证，或在正文中保留明确研究空白。")
    elif not matrix_questions:
        recommendations.append("尚无可核查的研究问题证据矩阵，不能进入论文交付。")
    if failed_ids:
        recommendations.append("逐条复核正文中的 ⚠ 引用，删除或改写证据不支持的论断。")
    if citations_total == 0:
        recommendations.append("正文尚无可核查引用，不能作为论文综述提交。")
    if incomplete_references:
        recommendations.append("补全文献作者、年份、期刊卷期页码等缺失元数据后再导出。")

    return {
        "schema_version": 1,
        "task_id": task_id,
        "generated_at": time.time(),
        "overall_status": overall_status,
        "summary": {
            "gates_passed": sum(gate["status"] == "pass" for gate in gates),
            "gates_action_required": len(actionable),
            "brief_questions_total": len(brief_rows),
            "brief_questions_covered": sum(row["covered"] for row in brief_rows),
            "research_questions_total": len(matrix_questions),
            "research_questions_covered": len(matrix_questions) - len(uncovered),
            "citations_total": int(citations_total),
            "citations_passed": int(citations_passed),
            "citations_failed": citations_failed,
            "references_total": references_total,
            "references_complete": references_complete,
        },
        "gates": gates,
        "brief_questions": brief_rows,
        "sections": [
            {
                "section_id": section.get("section_id"),
                "title": section.get("title"),
                "sufficient": bool(
                    (section.get("coverage") or {}).get("sufficient")
                ),
                "gap": str((section.get("coverage") or {}).get("gap") or ""),
                "stop_reason": str(section.get("stop_reason") or ""),
            }
            for section in sections
        ],
        "citation_review": {
            "failed_chunk_ids": failed_ids,
        },
        "bibliography_review": {
            "incomplete_references": incomplete_references,
        },
        "recommendations": recommendations,
    }
