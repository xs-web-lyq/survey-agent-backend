"""综述生成三阶段循环。

Phase 1 规划:survey_scope → LLM 生成大纲 → need_input 等确认
Phase 2 逐节:search_evidence(+expand_graph)→ 流式撰写 → 落盘 + notes
Phase 3 整合:合并 + 引言/结论 + verify_citation 逐条核查 → survey.md + references.md
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from backend import bibliography, llm, rag_client
from backend.agent import prompts
from backend.agent.evidence_coverage import (
    bind_brief_questions_to_outline,
    build_gap_query,
    MAX_RESEARCH_QUESTIONS,
    assess_coverage,
    research_questions_for_section,
    section_search_budget,
    select_balanced_evidence,
)
from backend.agent.evidence_store import (
    MATRIX_PATH,
    load_section_checkpoint,
    read_evidence_matrix,
    rebuild_evidence_matrix,
    save_section_checkpoint,
)
from backend.agent.state import SurveyState
from backend.agent.survey_quality import (
    build_integration_contract,
    build_quality_report,
)
from backend.config import settings
from backend.events import (
    CITATION_CHECK,
    DEEP_ROUND,
    EVIDENCE_MATRIX_UPDATED,
    FILE_WRITE,
    NEED_INPUT,
    PHASE,
    STREAM_RESET,
    TASK_STATUS,
    TEXT_DELTA,
    THINKING,
    TOOL_CALL,
    TOOL_RESULT,
    EventBus,
)
from backend.tools import retrieval, verify

_CITE_RE = re.compile(r"\[\[(chunk-[A-Za-z0-9._:-]+)\]\]")
_EVIDENCE_MARK_RE = re.compile(r"\[{1,2}(E\d+)\]{1,2}")

# 章节篇幅档位:字数区间 + 每节证据条数(长文需更多证据支撑,防注水)+ 证据截断长度
LENGTH_PRESETS = {
    "short":  {"range": "400~600 字",   "evidence": 10, "clip": 1200},
    "medium": {"range": "600~1000 字",  "evidence": 14, "clip": 1500},
    "long":   {"range": "1200~1800 字", "evidence": 20, "clip": 2000},
}
DEFAULT_LENGTH = "medium"
CITATION_VERIFY_CONCURRENCY = 4


def _length_preset(name: str) -> dict:
    return LENGTH_PRESETS.get(name, LENGTH_PRESETS[DEFAULT_LENGTH])


def _normalize_evidence_markers(section_md: str, ev_map: dict[str, str]) -> str:
    """把模型可能输出的 [E1] 或 [[E1]] 统一还原为内部 chunk 标记。"""
    return _EVIDENCE_MARK_RE.sub(
        lambda m: f"[[{ev_map[m.group(1)]}]]" if m.group(1) in ev_map else "",
        section_md,
    )


async def _llm_json(system: str, user: str, *, model: str | None = None) -> dict:
    return await llm.complete_json(system, user, model=model)


async def _llm_stream(
    bus: EventBus, system: str, user: str, *, target: str, model: str | None = None
) -> str:
    parts: list[str] = []
    async for delta in llm.stream(
        system, user, model=model or settings.effective_writer_model,
    ):
        parts.append(delta)
        bus.emit(TEXT_DELTA, {"target": target, "delta": delta})
    return "".join(parts)


# ---------------- Phase 1 规划 ----------------

def _outline_to_md(outline: dict, topic: str) -> str:
    md = f"# {outline.get('title', topic)}\n\n"
    for s in outline.get("sections", []):
        md += f"## {s['id']} {s['title']}\n"
        for p in s.get("points", []):
            md += f"- {p}\n"
        questions = research_questions_for_section(s)
        if questions:
            md += "\n### 研究问题\n"
            for question in questions:
                md += f"- {question}\n"
        md += "\n"
    return md


def _brief_questions(research_brief: dict[str, Any] | None) -> list[str]:
    if not isinstance(research_brief, dict):
        return []
    return [
        str(question).strip()
        for question in research_brief.get("research_questions") or []
        if str(question).strip()
    ][:MAX_RESEARCH_QUESTIONS]


def _validate_outline(
    raw: Any,
    topic: str,
    *,
    brief_questions: list[str] | None = None,
) -> dict | None:
    """校验用户提交的大纲(update_outline payload)。

    返回规范化后的大纲;不合法返回 None。id 按数组顺序重编号 01..0n,
    保证排序/增删后 sections/{id}.md 文件名与写作顺序一致。
    """
    if not isinstance(raw, dict):
        return None
    sections_raw = raw.get("sections")
    if not isinstance(sections_raw, list) or not sections_raw:
        return None
    sections = []
    for s in sections_raw:
        if not isinstance(s, dict):
            return None
        title = str(s.get("title", "")).strip()
        if not title:
            return None
        points = [str(p).strip() for p in (s.get("points") or [])
                  if str(p).strip()]
        research_questions = [
            str(question).strip()
            for question in (s.get("research_questions") or [])
            if str(question).strip()
        ][:MAX_RESEARCH_QUESTIONS]
        queries = [str(q).strip() for q in (s.get("queries") or [])
                   if str(q).strip()] or [title]
        sections.append({"id": f"{len(sections) + 1:02d}", "title": title,
                         "points": points,
                         "research_questions": research_questions or points[:MAX_RESEARCH_QUESTIONS] or [title],
                         "queries": queries})
    title = str(raw.get("title", "")).strip() or topic
    outline = {"title": title, "sections": sections}
    return bind_brief_questions_to_outline(outline, brief_questions or [])


async def phase_outline(bus: EventBus, state: SurveyState, *, auto_approve: bool) -> None:
    bus.emit(PHASE, {"name": "outline", "status": "start"})
    scope_set = set(state.doc_scope) or None
    bus.emit(TOOL_CALL, {"tool": "survey_scope", "call_id": "s1",
                         "args": {"topic": state.topic,
                                  **({"doc_scope": len(state.doc_scope)} if scope_set else {})}})
    scope = await retrieval.survey_scope(state.topic, allowed_sources=scope_set)
    bus.emit(TOOL_RESULT, {
        "call_id": "s1",
        "summary": f"相关文献 {len(scope['related_documents'])} 篇 / "
                   f"关键实体 {len(scope['key_entities'])} 个 / "
                   f"关键关系 {len(scope['key_relations'])} 条"
                   + (f"(范围限定 {len(state.doc_scope)} 篇)" if scope_set else ""),
        "detail": {"related_documents": scope["related_documents"][:10]},
    })

    bus.emit(THINKING, {"text": "基于探查结果规划大纲…"})
    user_parts = [f"综述主题:{state.topic}"]
    if state.research_brief:
        user_parts.append(
            "已确认的结构化研究简报（大纲必须逐项覆盖核心研究问题，并遵守纳入/排除边界）:\n"
            + json.dumps(state.research_brief, ensure_ascii=False)[:6000]
        )
    if state.context:
        user_parts.append(f"选题讨论结论(大纲应体现其中确定的方向与边界):\n{state.context[:2000]}")
    if scope_set:
        user_parts.append(
            "文献范围限定:本综述只能依据以下文献写作,章节划分必须围绕这些文献的素材,"
            "不要规划范围外才有素材的章节。\n"
            + json.dumps(scope.get("scope_documents", []), ensure_ascii=False)
        )
    user_parts.append("知识库探查结果:\n" + json.dumps(scope, ensure_ascii=False)[:8000])
    outline = await _llm_json(prompts.OUTLINE_SYSTEM, "\n\n".join(user_parts))
    outline = bind_brief_questions_to_outline(
        outline, _brief_questions(state.research_brief),
    )
    state.outline = outline
    state.fs.write("outline.md", _outline_to_md(outline, state.topic))
    bus.emit(FILE_WRITE, {"path": "outline.md"})
    state.save_meta()

    # 确认循环:approve 开写 / update_outline 采纳后开写 /
    # revise_outline 重生成后重新征求确认 / 其他输入忽略
    while not auto_approve:
        bus.emit(TASK_STATUS, {"status": "waiting_input"})
        bus.emit(NEED_INPUT, {"kind": "approve_outline", "payload": state.outline})
        reply = await state.wait_input()
        kind = reply.get("kind")

        if kind == "update_outline":
            validated = _validate_outline(
                reply.get("payload"),
                state.topic,
                brief_questions=_brief_questions(state.research_brief),
            )
            if validated is None:
                bus.emit(THINKING, {"text": "提交的大纲格式无效,已忽略;请重新提交或确认。"})
                continue
            state.outline = validated
            state.fs.write("outline.md", _outline_to_md(validated, state.topic))
            bus.emit(FILE_WRITE, {"path": "outline.md"})
            state.save_meta()
            bus.emit(THINKING, {"text": f"已采纳用户编辑的大纲({len(validated['sections'])} 节),开始撰写。"})
            break

        if kind == "revise_outline":
            bus.emit(TASK_STATUS, {"status": "running"})
            bus.emit(THINKING, {"text": f"按用户意见修改大纲:{reply.get('text', '')}"})
            outline = await _llm_json(
                prompts.OUTLINE_SYSTEM,
                f"综述主题:{state.topic}\n\n知识库探查结果:\n"
                + json.dumps(scope, ensure_ascii=False)[:8000]
                + (
                    "\n\n已确认的结构化研究简报:\n"
                    + json.dumps(state.research_brief, ensure_ascii=False)[:6000]
                    if state.research_brief else ""
                )
                + f"\n\n用户对上稿大纲的修改意见(必须遵守):{reply.get('text', '')}"
                + f"\n\n上稿大纲:{json.dumps(state.outline, ensure_ascii=False)}",
            )
            outline = bind_brief_questions_to_outline(
                outline, _brief_questions(state.research_brief),
            )
            state.outline = outline
            state.fs.write("outline.md", _outline_to_md(outline, state.topic))
            bus.emit(FILE_WRITE, {"path": "outline.md"})
            state.save_meta()
            continue  # 回到循环顶部,重新征求确认

        # approve 或未知 kind:视为通过
        break

    bus.emit(TASK_STATUS, {"status": "running"})
    rebuild_evidence_matrix(
        state.fs, task_id=state.task_id, outline=state.outline,
    )
    bus.emit(FILE_WRITE, {"path": MATRIX_PATH})
    state.mark_checkpoint("outline", "completed")
    bus.emit(PHASE, {"name": "outline", "status": "end"})


# ---------------- Phase 2 逐节撰写 ----------------

async def _write_section_document(
    bus: EventBus,
    state: SurveyState,
    *,
    section: dict[str, Any],
    research_questions: list[str],
    evidence_by_question: list[list[dict[str, Any]]],
    coverage: dict[str, Any],
    instructions: list[str] | None = None,
) -> str:
    """由持久化证据生成章节；初次写作和定向补证共用同一条路径。"""
    preset = _length_preset(state.section_length)
    title = state.outline.get("title", state.topic)
    all_evidence: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for group in evidence_by_question:
        for chunk in group:
            chunk_id = str(chunk.get("chunk_id") or "")
            if chunk_id and chunk_id not in seen_ids:
                seen_ids.add(chunk_id)
                all_evidence.append(chunk)

    ev_list = select_balanced_evidence(
        evidence_by_question, all_evidence, preset["evidence"],
    )
    ev_map = {f"E{i}": chunk["chunk_id"] for i, chunk in enumerate(ev_list, 1)}
    chunk_questions = {
        chunk["chunk_id"]: [
            f"Q{question_index + 1}"
            for question_index, group in enumerate(evidence_by_question)
            if any(item["chunk_id"] == chunk["chunk_id"] for item in group)
        ]
        for chunk in ev_list
    }
    ev_text = "\n\n".join(
        f"[证据编号: E{i}](支持: {','.join(chunk_questions[chunk['chunk_id']])}; "
        f"来源: {chunk['source']})\n{chunk['content'][:preset['clip']]}"
        for i, chunk in enumerate(ev_list, 1)
    )
    notes_text = "\n".join(f"- {note}" for note in state.notes) or "(无)"

    from backend import images as kb_images
    figures = kb_images.find_images_for_text(
        "\n".join(chunk["content"] for chunk in ev_list), limit=6,
    )
    figure_map = {f"F{i}": figure["token"] for i, figure in enumerate(figures, 1)}
    figure_text = ""
    if figures:
        figure_lines = "\n".join(
            f"F{i}: {figure['caption'][:120]}(来自 {figure['doc']})"
            for i, figure in enumerate(figures, 1)
        )
        figure_text = (
            "\n\n可用图表(可在与论述强相关处插入,格式 ![图注](F编号),"
            "编号必须来自本列表,禁止虚构;每节至多 2 张,不相关就不插):\n"
            + figure_lines
        )

    section_label = f"{section['id']} {section['title']}"
    user_prompt = (
        f"章节:{section_label}\n"
        f"研究问题:{json.dumps({f'Q{i}': question for i, question in enumerate(research_questions, 1)}, ensure_ascii=False)}\n"
        f"覆盖评估:{json.dumps(coverage, ensure_ascii=False)}\n"
        + (
            f"用户临时指示:{'; '.join(instructions or [])}\n"
            if instructions else ""
        )
        + f"\n检索证据:\n{ev_text}"
        + figure_text
    )
    rel = f"sections/{section['id']}.md"
    bus.emit(STREAM_RESET, {"target": rel})
    section_md = await _llm_stream(
        bus,
        prompts.SECTION_SYSTEM.format(
            survey_title=title,
            section_title=section["title"],
            notes=notes_text,
            length_range=preset["range"],
        ),
        user_prompt,
        target=rel,
    )
    section_md = _normalize_evidence_markers(section_md, ev_map)
    section_md = re.sub(
        r"\]\((F\d+)\)",
        lambda match: (
            f"](/api/kb-images/{figure_map[match.group(1)]})"
            if match.group(1) in figure_map else "](#invalid-fig)"
        ),
        section_md,
    )
    section_md = re.sub(r"!\[[^\]]*\]\(#invalid-fig\)\n?", "", section_md)
    state.fs.write_atomic(rel, section_md)
    bus.emit(FILE_WRITE, {"path": rel})
    return section_md


async def phase_write_sections(bus: EventBus, state: SurveyState) -> None:
    bus.emit(PHASE, {"name": "writing", "status": "start"})
    state.mark_checkpoint("writing", "started")
    sections = state.outline.get("sections", [])
    scope_set = set(state.doc_scope) or None

    for idx, sec in enumerate(sections, 1):
        rel = f"sections/{sec['id']}.md"
        prior_checkpoint = load_section_checkpoint(
            state.fs,
            str(sec["id"]),
            expected_questions=research_questions_for_section(sec),
        )
        if state.fs.exists(rel) and (
            sec["id"] in state.completed_sections
            or (prior_checkpoint and prior_checkpoint.get("status") == "written")
        ):
            if sec["id"] not in state.completed_sections:
                state.completed_sections.append(sec["id"])
            bus.emit(THINKING, {
                "stage": f"section_{idx}_resume",
                "status": "completed",
                "text": f"恢复检查点：第 {idx}/{len(sections)} 节已完成，跳过重复写作",
            })
            state.mark_checkpoint(
                "writing", "section_completed", section_id=sec["id"],
            )
            continue

        # 收取用户中途插话(下一节生效)
        state.collect_nowait()
        extra = state.drain_instructions()
        bus.emit(THINKING, {
            "text": f"撰写第 {idx}/{len(sections)} 节:{sec['id']} {sec['title']}",
        })

        # 研究问题驱动搜证：每个核心要点都必须被独立检索和覆盖。
        # chunk 总数只控制写作上下文容量，不再决定“证据充分”。
        evidence: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        research_questions = research_questions_for_section(sec)
        suggested_queries = list(dict.fromkeys(
            str(q).strip() for q in sec.get("queries", []) if str(q).strip()
        ))
        prior_checkpoint = load_section_checkpoint(
            state.fs,
            str(sec["id"]),
            expected_questions=research_questions,
        )
        if prior_checkpoint:
            evidence_by_question = prior_checkpoint["evidence_by_question"]
            used_queries = set(prior_checkpoint["used_queries"])
            completed_round = int(prior_checkpoint.get("round") or 0)
            round_history = list(prior_checkpoint.get("round_history") or [])
            bus.emit(THINKING, {
                "stage": f"section_{idx}_checkpoint",
                "status": "completed",
                "text": f"恢复第 {idx} 节搜证检查点",
                "detail": (
                    f"从第 {completed_round + 1} 轮继续，"
                    f"已保存 {sum(len(group) for group in evidence_by_question)} 条问题证据"
                ),
            })
        else:
            evidence_by_question = [[] for _ in research_questions]
            used_queries = set()
            completed_round = 0
            round_history = []

        question_seen: list[set[str]] = [
            {
                str(chunk.get("chunk_id") or "")
                for chunk in group
                if chunk.get("chunk_id")
            }
            for group in evidence_by_question
        ]
        evidence = []
        seen_ids = set()
        for group in evidence_by_question:
            for chunk in group:
                chunk_id = str(chunk.get("chunk_id") or "")
                if chunk_id and chunk_id not in seen_ids:
                    seen_ids.add(chunk_id)
                    evidence.append(chunk)
        coverage = assess_coverage(research_questions, evidence_by_question)
        start_round = completed_round + 1
        last_round = completed_round
        max_rounds = section_search_budget(len(research_questions))
        stop_reason = ""
        stagnant_gap_rounds = 0
        for history_item in reversed(round_history):
            if history_item.get("strategy") in {"initial_query", "research_question"}:
                break
            if int(history_item.get("new_question_chunks") or 0) > 0:
                break
            stagnant_gap_rounds += 1

        for qi in range(start_round, max_rounds + 1):
            last_round = qi
            if qi <= len(research_questions):
                question_index = qi - 1
                q = (
                    suggested_queries[question_index]
                    if question_index < len(suggested_queries)
                    else research_questions[question_index]
                )
                strategy = (
                    "initial_query"
                    if question_index < len(suggested_queries)
                    else "research_question"
                )
            else:
                uncovered = [
                    index for index, row in enumerate(coverage["questions"])
                    if not row["covered"]
                ]
                question_index = max(
                    uncovered or range(len(research_questions)),
                    key=lambda index: (
                        int(coverage["questions"][index]["missing_sources"])
                        + int(coverage["questions"][index]["missing_chunks"]),
                        -len(question_seen[index]),
                    ),
                )
                gap_row = dict(coverage["questions"][question_index])
                if not coverage["source_diversity_met"]:
                    gap_row["missing_sources"] = max(
                        int(gap_row["missing_sources"]),
                        int(coverage["required_sources"]) - int(coverage["source_count"]),
                    )
                q, strategy = build_gap_query(
                    sec,
                    research_questions[question_index],
                    gap_row,
                    attempt=qi - len(research_questions),
                )
            if q in used_queries:
                q = f"{q} 第{qi}轮"
            used_queries.add(q)

            call_id = f"w{idx}-{qi}"
            bus.emit(THINKING, {
                "stage": f"section_{idx}_evidence_{qi}",
                "status": "running",
                "text": f"研究问题 {question_index + 1}/{len(research_questions)} 搜证",
                "detail": research_questions[question_index],
            })
            bus.emit(TOOL_CALL, {"tool": "search_evidence", "call_id": call_id,
                                 "args": {"query": q,
                                          "research_question": research_questions[question_index],
                                          "strategy": strategy}})
            result = await retrieval.search_evidence(
                q, chunk_top_k=6, allowed_sources=scope_set)
            question_chunks_before = len(question_seen[question_index])
            for chunk in result["chunks"]:
                chunk_id = chunk["chunk_id"]
                if chunk_id not in question_seen[question_index]:
                    question_seen[question_index].add(chunk_id)
                    evidence_by_question[question_index].append(chunk)
            new_question_chunks = (
                len(question_seen[question_index]) - question_chunks_before
            )
            fresh = [c for c in result["chunks"] if c["chunk_id"] not in seen_ids]
            for c in fresh:
                seen_ids.add(c["chunk_id"])
            evidence.extend(fresh)
            coverage = assess_coverage(research_questions, evidence_by_question)
            is_gap_round = qi > len(research_questions)
            if is_gap_round:
                stagnant_gap_rounds = (
                    stagnant_gap_rounds + 1 if new_question_chunks == 0 else 0
                )
            round_history.append({
                "round": qi,
                "question_id": f"Q{question_index + 1}",
                "question": research_questions[question_index],
                "query": q,
                "strategy": strategy,
                "new_question_chunks": new_question_chunks,
                "new_unique_chunks": len(fresh),
                "coverage_ratio": coverage["coverage_ratio"],
                "covered_questions": coverage["covered_questions"],
                "source_count": coverage["source_count"],
            })
            bus.emit(TOOL_RESULT, {
                "call_id": call_id,
                "summary": (
                    f"「{research_questions[question_index]}」命中 "
                    f"{len(result['chunks'])} chunks / "
                    f"{len({c['source'] for c in result['chunks'] if c.get('source')})} 个来源"
                ),
            })
            all_questions_searched = qi >= len(research_questions)
            enough = all_questions_searched and coverage["sufficient"]
            plateau = (
                all_questions_searched
                and not enough
                and stagnant_gap_rounds >= 2
            )
            if enough:
                stop_reason = "coverage_satisfied"
            elif plateau:
                stop_reason = "plateau"
            checkpoint_status = "ready" if enough else "retrieving"
            save_section_checkpoint(
                state.fs,
                task_id=state.task_id,
                outline=state.outline,
                section=sec,
                research_questions=research_questions,
                evidence_by_question=evidence_by_question,
                used_queries=used_queries,
                round_no=qi,
                max_rounds=max_rounds,
                status=checkpoint_status,
                round_history=round_history,
                stop_reason=stop_reason,
            )
            state.mark_checkpoint(
                "evidence",
                checkpoint_status,
                section_id=sec["id"],
                round=qi,
                covered_questions=coverage["covered_questions"],
                total_questions=coverage["total_questions"],
            )
            bus.emit(EVIDENCE_MATRIX_UPDATED, {
                "section": sec["id"],
                "status": checkpoint_status,
                "coverage": coverage,
            })
            bus.emit(THINKING, {
                "stage": f"section_{idx}_evidence_{qi}",
                "status": "completed",
                "text": f"研究问题覆盖 {coverage['covered_questions']}/{coverage['total_questions']}",
                "detail": (
                    f"累计 {coverage['chunk_count']} 条证据 / "
                    f"{coverage['source_count']} 个独立来源"
                    + (f"；{coverage['gap']}" if coverage["gap"] else "")
                ),
            })
            if enough:
                bus.emit(DEEP_ROUND, {
                    "round": qi,
                    "verdict": "sufficient",
                    "new_chunks": len(fresh),
                    "section": sec["id"],
                    "coverage": coverage,
                })
                break
            if plateau:
                bus.emit(DEEP_ROUND, {
                    "round": qi,
                    "verdict": "plateau",
                    "gap": coverage["gap"],
                    "new_chunks": len(fresh),
                    "section": sec["id"],
                    "coverage": coverage,
                })
                break
            bus.emit(DEEP_ROUND, {
                "round": qi,
                "verdict": "insufficient",
                "gap": coverage["gap"] or "尚未完成所有研究问题检索",
                "new_chunks": len(fresh),
                "section": sec["id"],
                "coverage": coverage,
            })

        if not coverage["sufficient"]:
            if not stop_reason:
                stop_reason = "budget_exhausted"
            save_section_checkpoint(
                state.fs,
                task_id=state.task_id,
                outline=state.outline,
                section=sec,
                research_questions=research_questions,
                evidence_by_question=evidence_by_question,
                used_queries=used_queries,
                round_no=last_round,
                max_rounds=max_rounds,
                status="partial",
                round_history=round_history,
                stop_reason=stop_reason,
            )
            state.mark_checkpoint(
                "evidence",
                "partial",
                section_id=sec["id"],
                round=last_round,
                stop_reason=stop_reason,
                covered_questions=coverage["covered_questions"],
                total_questions=coverage["total_questions"],
            )
            bus.emit(EVIDENCE_MATRIX_UPDATED, {
                "section": sec["id"],
                "status": "partial",
                "coverage": coverage,
            })
            bus.emit(THINKING, {
                "stage": f"section_{idx}_evidence_final",
                "status": "completed",
                "text": (
                    "连续补证未发现新增有效证据"
                    if stop_reason == "plateau"
                    else "已达到本节最大检索轮次"
                ),
                "detail": (
                    f"最终覆盖 {coverage['covered_questions']}/{coverage['total_questions']} "
                    f"个研究问题，{coverage['source_count']} 个独立来源；"
                    "写作时将明确保留研究空白，不使用无依据内容"
                ),
            })

        await _write_section_document(
            bus,
            state,
            section=sec,
            research_questions=research_questions,
            evidence_by_question=evidence_by_question,
            coverage=coverage,
            instructions=extra,
        )
        if sec["id"] not in state.completed_sections:
            state.completed_sections.append(sec["id"])

        # 提炼本节要点进 notes(简单启发:取要点列表,避免再调一次 LLM)
        state.notes.extend(f"{sec['title']}: {p}" for p in sec.get("points", []))
        state.fs.write("notes.md", "\n".join(f"- {n}" for n in state.notes))
        save_section_checkpoint(
            state.fs,
            task_id=state.task_id,
            outline=state.outline,
            section=sec,
            research_questions=research_questions,
            evidence_by_question=evidence_by_question,
            used_queries=used_queries,
            round_no=last_round,
            max_rounds=max_rounds,
            status="written",
            round_history=round_history,
            stop_reason=stop_reason or "coverage_satisfied",
        )
        bus.emit(EVIDENCE_MATRIX_UPDATED, {
            "section": sec["id"],
            "status": "written",
            "coverage": coverage,
        })
        state.mark_checkpoint(
            "writing", "section_completed", section_id=sec["id"],
        )

    state.mark_checkpoint("writing", "completed")
    bus.emit(PHASE, {"name": "writing", "status": "end"})


async def phase_supplement_section(
    bus: EventBus,
    state: SurveyState,
    *,
    section_id: str,
    question_id: str,
    rounds: int = 2,
) -> dict[str, Any]:
    """针对一个未覆盖研究问题补证，并仅重写受影响章节。"""
    sections = state.outline.get("sections") or []
    section = next(
        (item for item in sections if str(item.get("id")) == section_id),
        None,
    )
    if section is None:
        raise ValueError(f"section not found: {section_id}")
    research_questions = research_questions_for_section(section)
    try:
        question_index = int(question_id.removeprefix("Q")) - 1
    except ValueError as exc:
        raise ValueError(f"invalid question id: {question_id}") from exc
    if question_index < 0 or question_index >= len(research_questions):
        raise ValueError(f"question not found: {question_id}")

    checkpoint = load_section_checkpoint(
        state.fs, section_id, expected_questions=research_questions,
    )
    if checkpoint is None:
        raise ValueError("evidence checkpoint is missing")

    bus.emit(PHASE, {"name": "supplement", "status": "start"})
    evidence_by_question = checkpoint["evidence_by_question"]
    used_queries = set(checkpoint["used_queries"])
    round_history = list(checkpoint.get("round_history") or [])
    question_seen = {
        str(chunk.get("chunk_id") or "")
        for chunk in evidence_by_question[question_index]
        if chunk.get("chunk_id")
    }
    completed_round = int(checkpoint.get("round") or 0)
    rounds = min(3, max(1, int(rounds)))
    max_round = completed_round + rounds
    last_round = completed_round
    new_chunks = 0
    scope_set = set(state.doc_scope) or None
    question = research_questions[question_index]

    for round_no in range(completed_round + 1, max_round + 1):
        last_round = round_no
        query = (
            f"{section['title']} {question} 独立研究 对比结果 "
            f"工业验证 补充证据 第{round_no - completed_round}轮"
        )
        if query in used_queries:
            query += " 不同来源"
        used_queries.add(query)
        call_id = f"supplement-{section_id}-{question_id}-{round_no}"
        bus.emit(THINKING, {
            "stage": call_id,
            "status": "running",
            "text": f"定向补证 {section_id}/{question_id}",
            "detail": question,
        })
        bus.emit(TOOL_CALL, {
            "tool": "search_evidence",
            "call_id": call_id,
            "args": {
                "query": query,
                "section": section_id,
                "research_question": question_id,
            },
        })
        result = await retrieval.search_evidence(
            query, chunk_top_k=8, allowed_sources=scope_set,
        )
        fresh = []
        for chunk in result["chunks"]:
            chunk_id = str(chunk.get("chunk_id") or "")
            if chunk_id and chunk_id not in question_seen:
                question_seen.add(chunk_id)
                evidence_by_question[question_index].append(chunk)
                fresh.append(chunk)
        new_chunks += len(fresh)
        coverage = assess_coverage(research_questions, evidence_by_question)
        round_history.append({
            "round": round_no,
            "question_id": question_id,
            "question": question,
            "query": query,
            "strategy": "manual_supplement",
            "new_question_chunks": len(fresh),
            "new_unique_chunks": len(fresh),
            "coverage_ratio": coverage["coverage_ratio"],
            "covered_questions": coverage["covered_questions"],
            "source_count": coverage["source_count"],
        })
        status = "ready" if coverage["sufficient"] else "retrieving"
        save_section_checkpoint(
            state.fs,
            task_id=state.task_id,
            outline=state.outline,
            section=section,
            research_questions=research_questions,
            evidence_by_question=evidence_by_question,
            used_queries=used_queries,
            round_no=round_no,
            max_rounds=max_round,
            status=status,
            round_history=round_history,
            stop_reason=(
                "coverage_satisfied" if coverage["sufficient"] else ""
            ),
        )
        state.mark_checkpoint(
            "evidence_supplement",
            status,
            section_id=section_id,
            question_id=question_id,
            round=round_no,
        )
        bus.emit(TOOL_RESULT, {
            "call_id": call_id,
            "summary": f"新增 {len(fresh)} 条证据，当前覆盖 "
                       f"{coverage['covered_questions']}/{coverage['total_questions']}",
        })
        bus.emit(EVIDENCE_MATRIX_UPDATED, {
            "section": section_id,
            "question": question_id,
            "status": status,
            "coverage": coverage,
        })
        bus.emit(DEEP_ROUND, {
            "round": round_no,
            "verdict": "sufficient" if coverage["sufficient"] else "insufficient",
            "gap": coverage["gap"],
            "new_chunks": len(fresh),
            "section": section_id,
            "question": question_id,
            "coverage": coverage,
        })
        if coverage["questions"][question_index]["covered"]:
            break

    coverage = assess_coverage(research_questions, evidence_by_question)
    await _write_section_document(
        bus,
        state,
        section=section,
        research_questions=research_questions,
        evidence_by_question=evidence_by_question,
        coverage=coverage,
        instructions=[f"本次定向补证聚焦 {question_id}: {question}，据此修订本节"],
    )
    save_section_checkpoint(
        state.fs,
        task_id=state.task_id,
        outline=state.outline,
        section=section,
        research_questions=research_questions,
        evidence_by_question=evidence_by_question,
        used_queries=used_queries,
        round_no=last_round,
        max_rounds=max_round,
        status="written",
        round_history=round_history,
        stop_reason=(
            "coverage_satisfied"
            if coverage["sufficient"]
            else "manual_supplement_exhausted"
        ),
    )
    bus.emit(EVIDENCE_MATRIX_UPDATED, {
        "section": section_id,
        "question": question_id,
        "status": "written",
        "coverage": coverage,
    })
    state.mark_checkpoint(
        "evidence_supplement",
        "section_rewritten",
        section_id=section_id,
        question_id=question_id,
        new_chunks=new_chunks,
    )
    bus.emit(PHASE, {"name": "supplement", "status": "end"})
    return {"new_chunks": new_chunks, "coverage": coverage}


# ---------------- Phase 3 整合与核查 ----------------

async def phase_finalize(bus: EventBus, state: SurveyState) -> dict[str, Any]:
    bus.emit(PHASE, {"name": "finalize", "status": "start"})
    state.mark_checkpoint("finalize", "started")
    title = state.outline.get("title", state.topic)

    drafts = []
    for sec in state.outline.get("sections", []):
        rel = f"sections/{sec['id']}.md"
        if state.fs.exists(rel):
            drafts.append(state.fs.read(rel))
    combined = "\n\n".join(drafts)
    evidence_matrix = read_evidence_matrix(
        state.fs,
        task_id=state.task_id,
        outline=state.outline,
    )
    integration_contract = build_integration_contract(
        state.research_brief,
        evidence_matrix,
    )

    checkpoint = "finalize_draft.md"
    if state.fs.exists(checkpoint):
        bus.emit(THINKING, {"text": "恢复已生成的终稿检查点，继续引用核查…"})
        full_md = state.fs.read(checkpoint)
    else:
        bus.emit(THINKING, {"text": "整合章节、补写引言与结论…"})
        polish_input = (
            "【终稿整合约束（不属于正文，不要原样输出）】\n"
            + json.dumps(integration_contract, ensure_ascii=False)
            + "\n\n【章节草稿】\n"
            + combined
        )
        full_md = await _llm_stream(
            bus,
            prompts.POLISH_SYSTEM.format(survey_title=title),
            polish_input,
            target="survey.md",
        )
        # 在昂贵的长文本调用后立即落检查点。后续核查失败时无需重新生成终稿。
        state.fs.write_atomic(checkpoint, full_md)
    state.mark_checkpoint("finalize", "draft_ready")

    # ---- 引用核查 ----
    cite_ids = list(dict.fromkeys(_CITE_RE.findall(full_md)))
    bus.emit(THINKING, {"text": f"引用核查:共 {len(cite_ids)} 个唯一 chunk 引用"})
    semaphore = asyncio.Semaphore(CITATION_VERIFY_CONCURRENCY)

    async def _check(cid: str) -> tuple[str, str]:
        # 用引用所在完整句子(含前一句上下文)做 claim
        m = re.search(rf"((?:[^。\n]*。)?[^。\n]*)\[\[{re.escape(cid)}\]\]", full_md)
        claim = (m.group(1).strip() if m else "")[-500:]
        async with semaphore:
            result = await verify.verify_citation(claim or title, cid)
        verdict = result["verdict"]
        bus.emit(CITATION_CHECK, {"claim": claim[:80], "chunk_id": cid,
                                  "verdict": verdict})
        return cid, verdict

    checks = await asyncio.gather(*(_check(cid) for cid in cite_ids))
    fail_ids = {cid for cid, verdict in checks if verdict != "pass"}
    passed = len(checks) - len(fail_ids)
    failed = len(fail_ids)

    # ---- 引用编号化 + 参考文献表 ----
    chunk_meta: dict[str, dict] = {}
    for cid in cite_ids:
        chunk = await rag_client.get_chunk_by_id(cid)
        src = ((chunk or {}).get("file_path") or "").replace("\\", "/").split("/")[-1]
        chunk_meta[cid] = {"source": src or "(未知来源)"}

    # 按来源文献分配编号
    sources = list(dict.fromkeys(m["source"] for m in chunk_meta.values()))
    src_no = {s: i + 1 for i, s in enumerate(sources)}
    bibliography_records = await bibliography.resolve_sources(sources)
    state.fs.write(
        "bibliography.json",
        json.dumps({"version": 1, "items": bibliography_records},
                   ensure_ascii=False, indent=2),
    )
    bus.emit(FILE_WRITE, {"path": "bibliography.json"})

    def _replace(m: re.Match) -> str:
        cid = m.group(1)
        meta = chunk_meta.get(cid)
        if not meta:
            return ""
        mark = f"[{src_no[meta['source']]}]"
        return f"{mark}⚠" if cid in fail_ids else mark

    final_md = _CITE_RE.sub(_replace, full_md)
    refs_md = bibliography.format_references(bibliography_records, failed=bool(fail_ids))
    final_md += "\n\n" + refs_md

    state.fs.write("survey.md", final_md)
    bus.emit(FILE_WRITE, {"path": "survey.md"})
    state.fs.write("references.md", refs_md)
    bus.emit(FILE_WRITE, {"path": "references.md"})
    quality_report = build_quality_report(
        task_id=state.task_id,
        research_brief=state.research_brief,
        evidence_matrix=evidence_matrix,
        citations_total=len(cite_ids),
        citations_passed=passed,
        failed_chunk_ids=fail_ids,
        bibliography_records=bibliography_records,
    )
    state.fs.write_atomic(
        "quality_report.json",
        json.dumps(quality_report, ensure_ascii=False, indent=2),
    )
    bus.emit(FILE_WRITE, {"path": "quality_report.json"})
    state.mark_checkpoint(
        "finalize",
        "completed",
        quality_status=quality_report["overall_status"],
        quality_actions=quality_report["summary"]["gates_action_required"],
    )
    bus.emit(PHASE, {"name": "finalize", "status": "end"})

    return {
        "citations_total": len(cite_ids),
        "citations_passed": passed,
        "citations_failed": failed,
        "references": len(sources),
        "bibliography_complete": sum(
            item.get("metadata_status") == "complete"
            for item in bibliography_records
        ),
        "quality_status": quality_report["overall_status"],
        "quality_actions": quality_report["summary"]["gates_action_required"],
    }


# ---------------- 总入口 ----------------

async def run_survey(
    bus: EventBus, state: SurveyState, *, auto_approve: bool = False
) -> dict[str, Any]:
    bus.emit(TASK_STATUS, {"status": "running"})
    try:
        await phase_outline(bus, state, auto_approve=auto_approve)
        await phase_write_sections(bus, state)
        stats = await phase_finalize(bus, state)
        state.mark_checkpoint("task", "completed")
        bus.emit(TASK_STATUS, {"status": "done", **stats})
        return stats
    except Exception as exc:  # noqa: BLE001
        import traceback
        err = f"{type(exc).__name__}: {exc}"
        bus.emit(TASK_STATUS, {"status": "failed", "error": err,
                               "traceback": traceback.format_exc()[-2000:]})
        raise
