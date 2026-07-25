"""选题头脑风暴:KB 感知的选题顾问对话。

与普通问答的区别:
- 人设是"选题顾问",目标是探讨综述选题而非回答事实问题
- 每轮自动对用户最新消息调 survey_scope 探查知识库(文献量/实体/关系),
  探查数据注入上下文,顾问凭数据说话
- 多轮:带完整对话历史(复用 conversations 表,route 标 brainstorm)
- conclude:把讨论总结为结构化结论 {topic, section_hints, doc_keywords, summary},
  供一键转综述任务(预填题目/上下文/文献范围)
"""

from __future__ import annotations

import json
import time
from typing import Any

from backend import llm
from backend.events import (
    DEEP_ROUND,
    TASK_STATUS,
    TEXT_DELTA,
    THINKING,
    TOOL_CALL,
    TOOL_RESULT,
    EventBus,
)
from backend.tools import retrieval

BRAINSTORM_MAX_SEARCH_ROUNDS = 3
BRAINSTORM_TARGET_DOCUMENTS = 12
BRAINSTORM_MIN_DOCUMENTS = 5

BRAINSTORM_SYSTEM = """你是连铸电磁搅拌领域的资深综述选题顾问。用户想从自己的文献知识库中
确定一个综述选题,你的任务是通过多轮讨论帮 ta 收敛到一个可行的好题目。

行为准则:
1. 凭数据说话:每轮都会提供"知识库探查数据"(相关文献数、关键实体、关键关系)。
   评估一个方向时必须引用这些数据,如"这个方向库里有 N 篇相关文献,集中在……"。
   不要空谈领域常识。
2. 覆盖度判断:相关文献 ≥15 篇 → 素材充足;5~15 篇 → 可写但要收窄;<5 篇 → 明确
   提示素材不足,建议换方向或并入更大的主题。
3. 主动反问:用户方向模糊时,提出 1~2 个澄清问题(如"侧重机理还是工艺应用?")。
4. 给出候选:讨论有一定信息后,给 2~3 个候选题目,各附一句宽窄/素材利弊。
5. 简洁:每轮回复 ≤300 字,用 Markdown,候选题目用列表。"""

_EXPAND_QUERY_PROMPT = """你是综述选题的检索规划器。首轮知识库探查命中文献不足，需要提出最多 2 个
互补的扩展查询。查询应探索原方向的上位主题、相邻技术或更规范的中英文术语，不能只是重复原句。

只输出 JSON:
{{"queries": ["查询1", "查询2"], "rationale": "一句话说明扩展策略"}}

用户当前方向:{message}
最近讨论:{history}
首轮关键实体:{entities}
首轮命中文献:{documents}
"""

_CONCLUDE_PROMPT = """以下是一段综述选题讨论和最终证据覆盖情况。请生成可直接交给综述写作代理的
结构化研究简报。只输出 JSON:

{{
  "topic": "最终确定(或最被认可)的综述题目,一句话",
  "section_hints": ["讨论中提到的综述应覆盖的方向/章节线索,2~5 条"],
  "doc_keywords": ["用于筛选相关文献的关键词,2~4 个,取自讨论中的核心术语"],
  "summary": "讨论结论摘要(方向、边界、侧重点),150 字以内",
  "research_questions": ["综述需要回答的核心研究问题,2~4 条"],
  "inclusion_criteria": ["纳入文献/内容的边界,1~3 条"],
  "exclusion_criteria": ["明确排除的方向,0~3 条"],
  "evidence_gaps": ["当前知识库仍缺少的证据,0~3 条"],
  "readiness_score": 0到100的整数,
  "readiness_reason": "一句话说明是否已经适合进入大纲阶段"
}}

讨论记录:
{history}

最终证据覆盖:
{scope}"""


def _empty_aggregate() -> dict[str, Any]:
    return {
        "kb_total_documents": 0,
        "related_documents": [],
        "key_entities": [],
        "key_relations": [],
        "search_queries": [],
        "search_rounds": 0,
    }


def _merge_scope(
    aggregate: dict[str, Any], scope: dict[str, Any], query: str,
) -> int:
    """Merge one retrieval round and return the number of newly found sources."""
    before = {d["source"] for d in aggregate["related_documents"]}
    documents = {d["source"]: dict(d) for d in aggregate["related_documents"]}
    for doc in scope.get("related_documents", []):
        source = str(doc.get("source", ""))
        if not source:
            continue
        previous = documents.get(source, {})
        documents[source] = {
            "source": source,
            "hit_chunks": max(
                int(previous.get("hit_chunks", 0)), int(doc.get("hit_chunks", 0)),
            ),
        }
    aggregate["related_documents"] = sorted(
        documents.values(), key=lambda item: -item["hit_chunks"],
    )
    aggregate["key_entities"] = list(dict.fromkeys([
        *aggregate["key_entities"],
        *(str(item) for item in scope.get("key_entities", []) if item),
    ]))[:30]
    relation_keys = {
        (r.get("src"), r.get("tgt"), r.get("description"))
        for r in aggregate["key_relations"]
    }
    for relation in scope.get("key_relations", []):
        key = (relation.get("src"), relation.get("tgt"), relation.get("description"))
        if key not in relation_keys:
            aggregate["key_relations"].append(relation)
            relation_keys.add(key)
    aggregate["key_relations"] = aggregate["key_relations"][:30]
    aggregate["kb_total_documents"] = max(
        int(aggregate["kb_total_documents"]),
        int(scope.get("kb_total_documents", 0)),
    )
    aggregate["search_queries"].append(query)
    aggregate["search_rounds"] += 1
    return len(set(documents) - before)


async def _expansion_queries(
    message: str, history: list[dict[str, Any]], scope: dict[str, Any],
) -> list[str]:
    history_text = " | ".join(str(m.get("content", ""))[:180] for m in history[-4:])
    try:
        data = await llm.complete_json(
            None,
            _EXPAND_QUERY_PROMPT.format(
                message=message,
                history=history_text,
                entities=json.dumps(scope.get("key_entities", [])[:12], ensure_ascii=False),
                documents=json.dumps(scope.get("related_documents", [])[:10], ensure_ascii=False),
            ),
        )
        queries = [str(q).strip() for q in data.get("queries", []) if str(q).strip()]
    except (ValueError, TypeError, KeyError):
        queries = []
    if not queries:
        queries = [
            str(entity).strip()
            for entity in scope.get("key_entities", [])
            if str(entity).strip() and str(entity).strip().casefold() not in message.casefold()
        ]
    return list(dict.fromkeys(queries))[: BRAINSTORM_MAX_SEARCH_ROUNDS - 1]


async def run_brainstorm(
    bus: EventBus, message: str, history: list[dict[str, Any]],
) -> dict[str, Any]:
    """执行一轮头脑风暴。事件发往 bus,返回 {answer, scope_brief}。"""
    t0 = time.time()
    bus.emit(TASK_STATUS, {"status": "running"})
    try:
        bus.emit(THINKING, {
            "stage": "intent",
            "status": "completed",
            "text": "已识别研究意图与候选主题",
            "detail": message[:120],
        })

        # 1) 对用户最新消息探查知识库；不足时由检索规划器生成互补查询继续搜。
        scope_t0 = time.time()
        bus.emit(THINKING, {
            "stage": "scope",
            "status": "running",
            "text": "正在检索知识库并评估资料覆盖",
        })
        bus.emit(TOOL_CALL, {"tool": "survey_scope", "call_id": "b1",
                             "args": {"topic": message[:80]}})
        first_scope = await retrieval.survey_scope(message)
        aggregate = _empty_aggregate()
        _merge_scope(aggregate, first_scope, message)
        bus.emit(TOOL_RESULT, {
            "call_id": "b1",
            "summary": f"相关文献 {len(first_scope['related_documents'])} 篇 / "
                       f"关键实体 {len(first_scope['key_entities'])} 个",
            "detail": first_scope,
        })
        queries: list[str] = []
        if len(aggregate["related_documents"]) < BRAINSTORM_TARGET_DOCUMENTS:
            bus.emit(DEEP_ROUND, {
                "round": 1,
                "verdict": "insufficient",
                "gap": f"仅命中 {len(aggregate['related_documents'])} 篇来源，需要扩展相邻主题",
            })
            bus.emit(THINKING, {
                "stage": "query_expansion",
                "status": "running",
                "text": "证据覆盖不足，正在规划扩展检索",
            })
            queries = await _expansion_queries(message, history, first_scope)
            bus.emit(THINKING, {
                "stage": "query_expansion",
                "status": "completed",
                "text": "已生成互补检索方向",
                "detail": " · ".join(queries) if queries else "没有找到有效的扩展查询",
            })

        for offset, query in enumerate(queries, 2):
            if len(aggregate["related_documents"]) >= BRAINSTORM_TARGET_DOCUMENTS:
                break
            call_id = f"b{offset}"
            bus.emit(THINKING, {
                "stage": f"scope_round_{offset}",
                "status": "running",
                "text": f"正在执行第 {offset} 轮扩展检索",
                "detail": query,
            })
            bus.emit(TOOL_CALL, {
                "tool": "survey_scope", "call_id": call_id,
                "args": {"topic": query, "round": offset},
            })
            round_scope = await retrieval.survey_scope(query)
            added = _merge_scope(aggregate, round_scope, query)
            bus.emit(TOOL_RESULT, {
                "call_id": call_id,
                "summary": (
                    f"本轮命中 {len(round_scope['related_documents'])} 篇，"
                    f"新增 {added} 个来源，累计 {len(aggregate['related_documents'])} 篇"
                ),
                "detail": round_scope,
            })
            bus.emit(DEEP_ROUND, {
                "round": offset,
                "verdict": "searched",
                "query": query,
                "new_chunks": added,
            })
            bus.emit(THINKING, {
                "stage": f"scope_round_{offset}",
                "status": "completed",
                "text": f"第 {offset} 轮扩展检索完成",
                "detail": f"新增 {added} 个来源",
            })

        evidence_count = len(aggregate["related_documents"])
        evidence_status = (
            "sufficient" if evidence_count >= BRAINSTORM_TARGET_DOCUMENTS
            else "workable" if evidence_count >= BRAINSTORM_MIN_DOCUMENTS
            else "insufficient"
        )
        bus.emit(DEEP_ROUND, {
            "round": aggregate["search_rounds"],
            "verdict": "sufficient" if evidence_status == "sufficient" else "insufficient",
            "new_chunks": evidence_count,
            "gap": "" if evidence_status == "sufficient" else "当前主题仍需补充文献或调整边界",
        })
        scope_brief = {
            **aggregate,
            "related_documents": aggregate["related_documents"][:30],
            "key_entities": aggregate["key_entities"][:20],
            "key_relations": aggregate["key_relations"][:15],
            "evidence_status": evidence_status,
        }
        bus.emit(TOOL_RESULT, {
            "call_id": "coverage-final",
            "summary": (
                f"覆盖汇总：{aggregate['search_rounds']} 轮 / "
                f"{evidence_count} 篇去重来源 / {evidence_status}"
            ),
            "detail": scope_brief,
        })
        bus.emit(THINKING, {
            "stage": "scope",
            "status": "completed",
            "text": "知识库覆盖评估完成",
            "detail": (
                f"经 {aggregate['search_rounds']} 轮检索累计找到 {evidence_count} 篇相关文献，"
                f"识别 {len(aggregate['key_entities'])} 个关键实体"
            ),
            "duration_ms": int((time.time() - scope_t0) * 1000),
        })

        # 2) 组装多轮对话(探查数据附在最新一轮)
        hist = [{"role": m["role"], "content": m["content"]}
                for m in history[-8:]]  # 最近 8 轮防上下文爆
        user = (
            f"{message}\n\n[知识库探查数据(仅你可见,回复时引用其中数字)]\n"
            + json.dumps(scope_brief, ensure_ascii=False)[:5000]
        )

        # 3) 流式回复。这里展示的是可复核的阶段摘要，不是模型私有思维链。
        synthesis_t0 = time.time()
        bus.emit(THINKING, {
            "stage": "synthesis",
            "status": "running",
            "text": "正在比较选题边界并组织建议",
            "detail": "结合文献覆盖、核心实体和当前对话生成候选方向",
        })
        full: list[str] = []
        async for delta in llm.stream(BRAINSTORM_SYSTEM, user, history=hist):
            full.append(delta)
            bus.emit(TEXT_DELTA, {"delta": delta})
        answer = "".join(full)
        bus.emit(THINKING, {
            "stage": "synthesis",
            "status": "completed",
            "text": "选题可行性分析完成",
            "duration_ms": int((time.time() - synthesis_t0) * 1000),
        })

        latency_ms = int((time.time() - t0) * 1000)
        bus.emit(TASK_STATUS, {"status": "done", "latency_ms": latency_ms})
        return {"answer": answer, "scope_brief": scope_brief,
                "latency_ms": latency_ms}
    except Exception as exc:  # noqa: BLE001
        bus.emit(TASK_STATUS, {"status": "failed", "error": str(exc)})
        raise


async def conclude_brainstorm(
    history: list[dict[str, Any]], scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把讨论和最终证据包总结为可直接交接的结构化研究简报。"""
    lines = []
    for m in history:
        who = "用户" if m["role"] == "user" else "顾问"
        lines.append(f"{who}:{m['content'][:600]}")
    scope = scope or {}
    data = await llm.complete_json(None, _CONCLUDE_PROMPT.format(
        history="\n\n".join(lines)[-6000:],
        scope=json.dumps(scope, ensure_ascii=False)[:6000],
    ))
    evidence_count = len(scope.get("related_documents", []))
    model_score = max(0, min(100, int(data.get("readiness_score", 0) or 0)))
    evidence_cap = 45 if evidence_count < 5 else 75 if evidence_count < 12 else 100
    user_turns = sum(1 for item in history if item.get("role") == "user")
    discussion_cap = 65 if user_turns < 2 else 100
    readiness_score = min(model_score, evidence_cap, discussion_cap)
    readiness_reason = (
        "当前仅完成一轮方向探索；建议先从候选题中选择侧重点，再进入大纲阶段。"
        if user_turns < 2
        else str(data.get("readiness_reason", ""))
    )
    return {
        "topic": str(data.get("topic", "")),
        "section_hints": [str(x) for x in (data.get("section_hints") or [])],
        "doc_keywords": [str(x) for x in (data.get("doc_keywords") or [])],
        "summary": str(data.get("summary", "")),
        "research_questions": [str(x) for x in (data.get("research_questions") or [])],
        "inclusion_criteria": [str(x) for x in (data.get("inclusion_criteria") or [])],
        "exclusion_criteria": [str(x) for x in (data.get("exclusion_criteria") or [])],
        "evidence_gaps": [str(x) for x in (data.get("evidence_gaps") or [])],
        "readiness_score": readiness_score,
        "readiness_reason": readiness_reason,
        "evidence_documents": evidence_count,
        "search_rounds": int(scope.get("search_rounds", 0) or 0),
        "doc_scope": [
            str(item.get("source"))
            for item in scope.get("related_documents", [])
            if item.get("source")
        ],
    }
