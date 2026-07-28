"""综述证据检查点与证据矩阵。

原始检查点保存恢复写作所需的完整 chunk；公开矩阵仅保存可展示摘要。
两者由本模块统一生成，避免事件流、任务元数据和 UI 各自维护一份状态。
"""

from __future__ import annotations

import json
import time
from typing import Any

from backend.agent.evidence_coverage import (
    assess_coverage,
    research_questions_for_section,
)
from backend.tools.files import WorkspaceFS

SCHEMA_VERSION = 1
MATRIX_PATH = "evidence_matrix.json"


def checkpoint_path(section_id: str) -> str:
    return f"checkpoints/evidence/{section_id}.json"


def _safe_score(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    """只持久化恢复写作必需且 JSON 稳定的字段。"""
    page_range = chunk.get("page_range")
    if not isinstance(page_range, dict):
        page_range = None
    return {
        "chunk_id": str(chunk.get("chunk_id") or ""),
        "source": str(chunk.get("source") or chunk.get("file_path") or ""),
        "content": str(chunk.get("content") or ""),
        "score": _safe_score(chunk.get("score")),
        "page_range": page_range,
        "section_title": str(chunk.get("section_title") or ""),
    }


def _normalized_groups(
    evidence_by_question: list[list[dict[str, Any]]],
) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for group in evidence_by_question:
        seen: set[str] = set()
        normalized = []
        for raw in group:
            chunk = _normalize_chunk(raw)
            chunk_id = chunk["chunk_id"]
            if not chunk_id or chunk_id in seen:
                continue
            seen.add(chunk_id)
            normalized.append(chunk)
        groups.append(normalized)
    return groups


def load_section_checkpoint(
    fs: WorkspaceFS,
    section_id: str,
    *,
    expected_questions: list[str] | None = None,
) -> dict[str, Any] | None:
    path = checkpoint_path(section_id)
    if not fs.exists(path):
        return None
    try:
        data = json.loads(fs.read(path))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        return None
    questions = [str(item) for item in data.get("research_questions") or []]
    if expected_questions is not None and questions != expected_questions:
        return None
    groups = data.get("evidence_by_question")
    if not isinstance(groups, list):
        return None
    data["evidence_by_question"] = _normalized_groups(groups)
    data["used_queries"] = [
        str(item) for item in data.get("used_queries") or [] if str(item)
    ]
    data["round_history"] = [
        item for item in data.get("round_history") or []
        if isinstance(item, dict)
    ]
    data["stop_reason"] = str(data.get("stop_reason") or "")
    return data


def save_section_checkpoint(
    fs: WorkspaceFS,
    *,
    task_id: str,
    outline: dict[str, Any],
    section: dict[str, Any],
    research_questions: list[str],
    evidence_by_question: list[list[dict[str, Any]]],
    used_queries: set[str] | list[str],
    round_no: int,
    max_rounds: int,
    status: str,
    round_history: list[dict[str, Any]] | None = None,
    stop_reason: str = "",
) -> dict[str, Any]:
    groups = _normalized_groups(evidence_by_question)
    coverage = assess_coverage(research_questions, groups)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "section_id": str(section["id"]),
        "section_title": str(section.get("title") or ""),
        "status": status,
        "round": max(0, int(round_no)),
        "max_rounds": max(1, int(max_rounds)),
        "updated_at": time.time(),
        "research_questions": list(research_questions),
        "used_queries": sorted(set(used_queries)),
        "evidence_by_question": groups,
        "coverage": coverage,
        "round_history": list(round_history or []),
        "stop_reason": str(stop_reason or ""),
    }
    fs.write_atomic(
        checkpoint_path(str(section["id"])),
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    rebuild_evidence_matrix(fs, task_id=task_id, outline=outline)
    return payload


def _public_section(
    section: dict[str, Any],
    checkpoint: dict[str, Any] | None,
) -> dict[str, Any]:
    questions = research_questions_for_section(section)
    if checkpoint is None:
        return {
            "section_id": str(section.get("id") or ""),
            "title": str(section.get("title") or ""),
            "status": "pending",
            "round": 0,
            "max_rounds": 0,
            "stop_reason": "",
            "round_history": [],
            "coverage": assess_coverage(questions, [[] for _ in questions]),
            "questions": [
                {
                    "id": f"Q{index}",
                    "question": question,
                    "covered": False,
                    "chunks": 0,
                    "sources": 0,
                    "evidence": [],
                }
                for index, question in enumerate(questions, 1)
            ],
        }

    groups = checkpoint["evidence_by_question"]
    coverage = assess_coverage(checkpoint["research_questions"], groups)
    rows = []
    for index, question in enumerate(checkpoint["research_questions"], 1):
        group = groups[index - 1] if index <= len(groups) else []
        coverage_row = coverage["questions"][index - 1]
        rows.append({
            "id": f"Q{index}",
            "question": question,
            "covered": coverage_row["covered"],
            "status": coverage_row["status"],
            "chunks": coverage_row["chunks"],
            "sources": coverage_row["sources"],
            "missing_chunks": coverage_row["missing_chunks"],
            "missing_sources": coverage_row["missing_sources"],
            "evidence": [
                {
                    "chunk_id": chunk["chunk_id"],
                    "source": chunk["source"],
                    "score": chunk["score"],
                    "preview": chunk["content"][:180],
                }
                for chunk in group
            ],
        })
    return {
        "section_id": checkpoint["section_id"],
        "title": checkpoint["section_title"],
        "status": checkpoint["status"],
        "round": checkpoint["round"],
        "max_rounds": checkpoint["max_rounds"],
        "stop_reason": checkpoint.get("stop_reason") or "",
        "round_history": checkpoint.get("round_history") or [],
        "coverage": coverage,
        "questions": rows,
        "updated_at": checkpoint.get("updated_at"),
    }


def rebuild_evidence_matrix(
    fs: WorkspaceFS,
    *,
    task_id: str,
    outline: dict[str, Any],
) -> dict[str, Any]:
    sections = []
    global_sources: set[str] = set()
    for section in outline.get("sections") or []:
        checkpoint = load_section_checkpoint(fs, str(section.get("id") or ""))
        public = _public_section(section, checkpoint)
        sections.append(public)
        for question in public["questions"]:
            global_sources.update(
                evidence["source"]
                for evidence in question["evidence"]
                if evidence.get("source")
            )

    total_questions = sum(len(section["questions"]) for section in sections)
    covered_questions = sum(
        int(question["covered"])
        for section in sections
        for question in section["questions"]
    )
    sufficient_sections = sum(
        int(bool(section["coverage"]["sufficient"])) for section in sections
    )
    matrix = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "updated_at": time.time(),
        "summary": {
            "sections_total": len(sections),
            "sections_sufficient": sufficient_sections,
            "questions_total": total_questions,
            "questions_covered": covered_questions,
            "source_count": len(global_sources),
        },
        "sections": sections,
    }
    fs.write_atomic(MATRIX_PATH, json.dumps(matrix, ensure_ascii=False, indent=2))
    return matrix


def read_evidence_matrix(
    fs: WorkspaceFS,
    *,
    task_id: str,
    outline: dict[str, Any],
) -> dict[str, Any]:
    if fs.exists(MATRIX_PATH):
        try:
            matrix = json.loads(fs.read(MATRIX_PATH))
            if matrix.get("schema_version") == SCHEMA_VERSION:
                return matrix
        except (json.JSONDecodeError, OSError):
            pass
    return rebuild_evidence_matrix(fs, task_id=task_id, outline=outline)
