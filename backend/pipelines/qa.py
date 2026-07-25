"""问答链路调度:route 参数 → 各检索链路,统一事件流输出。

所有链路统一产出:
- ROUTE_INFO 事件(requested / used / decision)
- TOOL_CALL / TOOL_RESULT 事件(检索轨迹)
- TEXT_DELTA 事件(流式回答)
- CITATIONS 事件(结构化引用,供前端角标与反馈库落库)

支持的 route(三种本质不同的检索范式,便于对比打分):
  mix         知识图谱增强(实体+关系+向量,证据最全)
  progressive 章节级渐进检索(结构感知,引用带页码)
  hybrid      BM25+向量混合(词法+语义,最快)
"""

from __future__ import annotations

import re
import time
from typing import Any, AsyncIterator

from backend.config import settings
from backend.memory.models import MemoryBundle
from backend.events import (
    CITATIONS,
    DEEP_ROUND,
    ROUTE_INFO,
    TASK_STATUS,
    TEXT_DELTA,
    THINKING,
    TOOL_CALL,
    TOOL_RESULT,
    EventBus,
)

DEEP_MAX_ROUNDS = 3          # 深度模式最多补充搜证轮数
DEEP_MODES = {"mix", "hybrid"}  # 支持深度循环的底层检索

ROUTES = ["mix", "progressive", "hybrid"]
DEFAULT_ROUTE = "mix"
RETRIEVAL_CANDIDATE_K = 24
FINAL_CITATION_K = 8

# Domain-specific query expansion avoids an extra translation LLM call on every
# question while still giving the multilingual embedding model strong English
# search terms.  More specific phrases must precede their shorter components.
_ENGLISH_QUERY_TERMS = (
    ("中间包电磁搅拌", "tundish electromagnetic stirring"),
    ("结晶器电磁搅拌", "mold electromagnetic stirring M-EMS"),
    ("凝固末端电磁搅拌", "final electromagnetic stirring F-EMS"),
    ("电磁制动", "electromagnetic braking EMBr"),
    ("电磁搅拌", "electromagnetic stirring EMS"),
    ("中间包", "tundish"),
    ("结晶器", "continuous casting mold"),
    ("连铸", "continuous casting"),
    ("铸坯", "cast strand billet bloom slab"),
    ("板坯", "slab"),
    ("方坯", "billet bloom"),
    ("圆坯", "round billet"),
    ("等轴晶率", "equiaxed crystal ratio"),
    ("中心偏析", "center segregation"),
    ("宏观偏析", "macrosegregation"),
    ("夹杂物", "inclusion removal"),
    ("流场", "fluid flow flow field"),
    ("传热", "heat transfer"),
    ("凝固", "solidification"),
    ("发展脉络", "development history evolution"),
    ("技术发展", "technology development history"),
    ("发展历史", "development history"),
    ("作用机理", "mechanism"),
    ("影响", "effect influence"),
    ("频率", "frequency"),
    ("电流", "current"),
    ("安装位置", "installation position"),
)

_QA_SYSTEM_PROMPT = """你是连铸电磁搅拌领域的科研文献问答助手。
严格依据提供的检索证据回答问题;证据不足时明确说明,不得编造。
回答使用中文,采用学术但易读的表述。
引用规范:在依据某条证据的论断后面标注 [n],n 为证据编号。"""


def _public_name(file_path: str) -> str:
    return (file_path or "").replace("\\", "/").split("/")[-1]


def _expand_english_query(question: str) -> str:
    """Build an English retrieval query without adding another LLM request."""
    terms: list[str] = []
    remaining = question
    for chinese, english in _ENGLISH_QUERY_TERMS:
        if chinese in remaining:
            terms.append(english)
            remaining = remaining.replace(chinese, " ")

    # Preserve useful acronyms/model names already supplied by the user.
    terms.extend(re.findall(r"\b[A-Za-z][A-Za-z0-9-]{1,20}\b", question))
    return " ".join(dict.fromkeys(terms)).strip()


def _source_key(chunk: dict[str, Any]) -> str:
    source = _public_name(chunk.get("file_path") or chunk.get("source_file", ""))
    if source:
        return source.casefold()
    return str(chunk.get("chunk_id") or chunk.get("__id__") or id(chunk))


def _source_language(chunk: dict[str, Any]) -> str:
    """Classify the evidence document, preferring its stable source filename."""
    source = _public_name(chunk.get("file_path") or chunk.get("source_file", ""))
    upper = source.upper()
    if upper.startswith("EN_"):
        return "en"
    if upper.startswith("CN_") or re.search(r"[\u4e00-\u9fff]", source):
        return "zh"

    sample = (chunk.get("content") or "")[:2000]
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", sample))
    latin_count = len(re.findall(r"[A-Za-z]", sample))
    return "zh" if cjk_count > max(30, latin_count * 0.15) else "en"


def _language_targets(question: str, limit: int) -> tuple[int, int]:
    """Return (English, Chinese) evidence targets based on explicit intent."""
    if any(word in question for word in ("国内", "中国", "中文文献", "国产")):
        return min(2, limit), max(0, limit - 2)
    if any(word in question for word in ("国外", "国际", "英文文献", "海外")):
        return min(7, limit), max(0, limit - 7)
    english = min(5, limit)
    return english, max(0, limit - english)


def _select_bilingual_chunks(
    native_chunks: list[dict[str, Any]],
    english_chunks: list[dict[str, Any]],
    question: str,
    *,
    limit: int = FINAL_CITATION_K,
) -> list[dict[str, Any]]:
    """Deduplicate documents and select a balanced bilingual evidence set."""
    candidates: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for chunk in [*native_chunks, *english_chunks]:
        key = _source_key(chunk)
        if key in seen_sources:
            continue
        seen_sources.add(key)
        candidates.append(chunk)

    english_pool = [c for c in candidates if _source_language(c) == "en"]
    chinese_pool = [c for c in candidates if _source_language(c) == "zh"]
    english_target, chinese_target = _language_targets(question, limit)

    selected = english_pool[:english_target] + chinese_pool[:chinese_target]
    selected_keys = {_source_key(c) for c in selected}
    for chunk in candidates:
        if len(selected) >= limit:
            break
        if _source_key(chunk) not in selected_keys:
            selected.append(chunk)
            selected_keys.add(_source_key(chunk))

    # Restore retrieval order so evidence numbering remains relevance-oriented.
    order = {_source_key(c): i for i, c in enumerate(candidates)}
    return sorted(selected[:limit], key=lambda c: order[_source_key(c)])


def _build_context(chunks: list[dict[str, Any]]) -> str:
    """把检索到的 chunks 组装为带编号的证据上下文。"""
    parts = []
    for i, c in enumerate(chunks, 1):
        src = _public_name(c.get("file_path", ""))
        parts.append(f"[证据 {i}](来源: {src})\n{c.get('content', '')}")
    return "\n\n".join(parts)


def _chunks_to_citations(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """统一引用结构(前端角标 + 数据库落库共用)。"""
    from backend.images import find_images_for_text

    citations = []
    for i, c in enumerate(chunks, 1):
        content = c.get("content") or ""
        citations.append({
            "n": i,
            "chunk_id": c.get("chunk_id") or c.get("__id__") or c.get("chunk_uid", ""),
            "source": _public_name(c.get("file_path") or c.get("source_file", "")),
            "preview": " ".join(content[:180].split()),
            "score": c.get("score") or c.get("hybrid_score"),
            "page_range": c.get("page_range")
                          or ({"start": c.get("start_page"), "end": c.get("end_page")}
                              if c.get("start_page") is not None else None),
            "images": find_images_for_text(content),  # 原文插图回链
        })
    return citations


async def _stream_llm(
    bus: EventBus, question: str, context: str, *, model: str | None = None,
    memory: MemoryBundle | None = None,
) -> str:
    """以流式方式调 LLM 生成回答,逐块发 TEXT_DELTA,返回完整回答。"""
    from backend import llm

    synthesis_t0 = time.time()
    bus.emit(THINKING, {
        "stage": "synthesis",
        "status": "running",
        "text": "正在依据检索证据组织回答",
    })
    system_prompt = _QA_SYSTEM_PROMPT
    history: list[dict[str, str]] = []
    if memory:
        memory_context = memory.system_context()
        if memory_context:
            system_prompt += "\n\n以下是会话记忆，仅用于理解用户上下文，不得替代检索证据：\n" + memory_context
        history = memory.recent_messages
    full = []
    async for delta in llm.stream(
        system_prompt,
        f"检索证据:\n\n{context}\n\n问题:{question}",
        model=model,
        history=history,
    ):
        full.append(delta)
        bus.emit(TEXT_DELTA, {"delta": delta})
    bus.emit(THINKING, {
        "stage": "synthesis",
        "status": "completed",
        "text": "证据归纳与回答组织完成",
        "duration_ms": int((time.time() - synthesis_t0) * 1000),
    })
    return "".join(full)


# ---------------- 各链路实现 ----------------

async def _retrieve_lightrag(
    bus: EventBus, question: str, mode: str, *, call_id: str = "r1"
) -> list[dict[str, Any]]:
    """naive/local/global/mix:aquery_data 取结构化证据(仅检索,不生成)。"""
    from backend.rag_client import aquery_data, get_rag

    bus.emit(TOOL_CALL, {"tool": "aquery_data", "call_id": call_id,
                         "args": {"mode": mode, "top_k": 20,
                                  "chunk_top_k": FINAL_CITATION_K,
                                  "bilingual": True}})
    t0 = time.time()
    result = await aquery_data(
        question, mode=mode, top_k=20, chunk_top_k=RETRIEVAL_CANDIDATE_K
    )
    data = result.get("data", {})
    native_chunks = data.get("chunks", [])

    english_query = _expand_english_query(question)
    english_chunks: list[dict[str, Any]] = []
    if english_query:
        rag = await get_rag()
        english_chunks = await rag.lightrag.chunks_vdb.query(
            english_query, top_k=RETRIEVAL_CANDIDATE_K
        )
    chunks = _select_bilingual_chunks(native_chunks, english_chunks, question)
    language_counts = {
        "en": sum(_source_language(c) == "en" for c in chunks),
        "zh": sum(_source_language(c) == "zh" for c in chunks),
    }
    bus.emit(TOOL_RESULT, {
        "call_id": call_id,
        "summary": f"命中 {len(chunks)} 个去重来源"
                   f"(英文 {language_counts['en']} / 中文 {language_counts['zh']}) / "
                   f"{len(data.get('entities', []))} 实体 / "
                   f"{len(data.get('relationships', []))} 关系({time.time()-t0:.1f}s)",
        "detail": {"references": data.get("references", []),
                   "english_query": english_query},
    })
    return chunks


async def _retrieve_hybrid(
    bus: EventBus, question: str, *, call_id: str = "h1"
) -> list[dict[str, Any]]:
    """hybrid:BM25+向量混合(仅检索,不生成)。"""
    from raganything.hybrid_retrieval import create_hybrid_retriever

    from backend.rag_client import get_rag

    rag = await get_rag()
    bus.emit(TOOL_CALL, {"tool": "hybrid_search", "call_id": call_id,
                         "args": {"top_k": FINAL_CITATION_K, "alpha": 0.5,
                                  "bilingual": True}})
    t0 = time.time()
    retriever = create_hybrid_retriever(str(settings.rag_storage_dir))

    async def _search(query: str) -> list[dict[str, Any]]:
        vector_results = await rag.lightrag.chunks_vdb.query(
            query, top_k=RETRIEVAL_CANDIDATE_K
        )
        return retriever.search(
            query, top_k=RETRIEVAL_CANDIDATE_K, alpha=0.5,
            vector_results=vector_results,
        )

    native_chunks = await _search(question)
    english_query = _expand_english_query(question)
    english_chunks = await _search(english_query) if english_query else []
    chunks = _select_bilingual_chunks(native_chunks, english_chunks, question)
    language_counts = {
        "en": sum(_source_language(c) == "en" for c in chunks),
        "zh": sum(_source_language(c) == "zh" for c in chunks),
    }
    bus.emit(TOOL_RESULT, {
        "call_id": call_id,
        "summary": f"BM25+向量融合命中 {len(chunks)} 个去重来源"
                   f"(英文 {language_counts['en']} / 中文 {language_counts['zh']})"
                   f"({time.time()-t0:.1f}s)",
        "detail": {"english_query": english_query},
    })
    return chunks


async def _retrieve_by_mode(
    bus: EventBus, question: str, mode: str, *, call_id: str
) -> list[dict[str, Any]]:
    """按 mode 分发到对应检索器(深度循环复用)。"""
    if mode == "hybrid":
        return await _retrieve_hybrid(bus, question, call_id=call_id)
    return await _retrieve_lightrag(bus, question, mode, call_id=call_id)


def _chunk_key(c: dict[str, Any]) -> str:
    # Deep-search rounds should not let one long thesis occupy several citation
    # slots with different chunks.
    return _source_key(c)


async def _run_lightrag_mode(
    bus: EventBus, question: str, mode: str, *, memory: MemoryBundle | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """naive/local/global/mix:检索 + 流式生成(单次)。"""
    chunks = await _retrieve_lightrag(bus, question, mode)
    context = _build_context(chunks)
    answer_question = memory.original_question if memory else question
    answer = await _stream_llm(bus, answer_question, context, memory=memory)
    return answer, _chunks_to_citations(chunks)


async def _run_progressive(
    bus: EventBus, question: str, *, memory: MemoryBundle | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """progressive:章节级渐进检索,引用带页码。"""
    from backend.rag_client import get_rag

    rag = await get_rag()
    index_path = str(settings.academic_index_path or "")
    bus.emit(TOOL_CALL, {"tool": "aretrieve_progressive_context", "call_id": "p1",
                         "args": {"section_top_k": 3, "chunk_top_k": 6}})
    t0 = time.time()
    ctx = await rag.aretrieve_progressive_context(
        question, index_path, section_top_k=3, chunk_top_k=6,
        include_native_chunks=True, native_query_mode="mix",
    )
    sections = ctx.get("sections", [])
    citations_raw = ctx.get("citations", [])
    bus.emit(TOOL_RESULT, {
        "call_id": "p1",
        "summary": f"命中 {len(sections)} 章节 / {len(ctx.get('chunks', []))} 章节块 / "
                   f"{len(ctx.get('native_chunks', []))} 原生块({time.time()-t0:.1f}s)",
        "detail": {"sections": [
            {"title": s.get("title"), "source": _public_name(s.get("source_file", "")),
             "pages": f"{s.get('start_page')}-{s.get('end_page')}"}
            for s in sections
        ]},
    })
    context = ctx.get("combined_context", "")
    answer_question = memory.original_question if memory else question
    answer = await _stream_llm(bus, answer_question, context, memory=memory)
    # progressive 自带结构化引用(含页码),直接转统一格式
    from backend.images import find_images_for_text

    citations = []
    for i, c in enumerate(citations_raw, 1):
        citations.append({
            "n": i,
            "chunk_id": c.get("chunk_id", ""),
            "source": c.get("source_title") or _public_name(c.get("file_path", "")),
            "preview": c.get("content_preview", ""),
            "score": c.get("score"),
            "page_range": c.get("page_range"),
            "section_title": c.get("section_title"),
            "images": find_images_for_text(c.get("content_preview", "")),
        })
    return answer, citations


async def _run_hybrid(
    bus: EventBus, question: str, *, memory: MemoryBundle | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """hybrid:BM25+向量混合 + 时效重排(单次)。"""
    chunks = await _retrieve_hybrid(bus, question)
    context = _build_context(chunks)
    answer_question = memory.original_question if memory else question
    answer = await _stream_llm(bus, answer_question, context, memory=memory)
    return answer, _chunks_to_citations(chunks)


# ---------------- 深度模式:自省式循环搜证 ----------------

_REFLECT_PROMPT = """你是检索证据评估员。判断已有证据是否足以完整、准确地回答用户问题。

用户问题:{question}

已有证据(共 {n} 条):
{context}

只输出 JSON:
{{"research_aspects": ["从用户问题拆出的待回答方面"],
  "covered_aspects": ["已有证据充分支持的方面"],
  "missing_aspects": ["尚未被证据支持的方面"],
  "sufficient": true 或 false,
  "gap": "若不足,一句话说明缺什么信息;充分则留空",
  "next_query": "若不足,给出一个用于补充检索的查询语句(聚焦缺口,不要重复原问题);充分则留空"}}

判定从严:只有 research_aspects 全部出现在 covered_aspects 且 missing_aspects 为空时才算
sufficient。chunk 数量再多也不能替代问题覆盖;涉及对比/机理/多因素而证据只覆盖一部分,
必须判 false。"""


async def _reflect(question: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """让 LLM 判断证据充分性,返回 {sufficient, gap, next_query}。"""
    from backend import llm

    # 反思只需要证据摘要,截断以省 token
    brief = "\n\n".join(
        f"[{i}] {(c.get('content') or '')[:400]}" for i, c in enumerate(chunks, 1)
    )
    try:
        data = await llm.complete_json(None, _REFLECT_PROMPT.format(
            question=question, n=len(chunks), context=brief[:8000]))
        aspects = [str(x).strip() for x in data.get("research_aspects", []) if str(x).strip()]
        covered = [str(x).strip() for x in data.get("covered_aspects", []) if str(x).strip()]
        missing = [str(x).strip() for x in data.get("missing_aspects", []) if str(x).strip()]
        sufficient = bool(data.get("sufficient", False)) and bool(aspects) and not missing
        source_count = len({
            str(chunk.get("source") or chunk.get("file_path") or "")
            for chunk in chunks
            if chunk.get("source") or chunk.get("file_path")
        })
        coverage = {
            "covered_questions": len(covered),
            "total_questions": len(aspects) or max(1, len(covered) + len(missing)),
            "source_count": source_count,
            "uncovered_questions": missing,
        }
        return {
            "sufficient": sufficient,
            "gap": str(data.get("gap", "")) or ("；".join(missing) if missing else ""),
            "next_query": str(data.get("next_query", "")),
            "coverage": coverage,
        }
    except (ValueError, IndexError, KeyError, TypeError):
        # 解析失败不能等价于证据充分；用原问题补搜一次，若无新增证据则停止。
        return {
            "sufficient": False,
            "gap": "证据覆盖评估失败，无法确认所有研究方面均已覆盖",
            "next_query": question,
            "coverage": {
                "covered_questions": 0,
                "total_questions": 1,
                "source_count": 0,
                "uncovered_questions": [question],
            },
        }


async def _run_deep(
    bus: EventBus, question: str, mode: str, *, memory: MemoryBundle | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """深度模式:初检索 → 反思 → 补充检索(≤DEEP_MAX_ROUNDS)→ 汇总生成。"""
    all_chunks: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _merge(new: list[dict[str, Any]]) -> int:
        added = 0
        for c in new:
            k = _chunk_key(c)
            if k not in seen:
                seen.add(k)
                all_chunks.append(c)
                added += 1
        return added

    # 第 0 轮:原问题检索
    _merge(await _retrieve_by_mode(bus, question, mode, call_id="deep-0"))

    for rnd in range(1, DEEP_MAX_ROUNDS + 1):
        review_t0 = time.time()
        bus.emit(THINKING, {
            "stage": f"evidence_review_{rnd}",
            "status": "running",
            "text": f"正在评估第 {rnd} 轮证据充分性",
        })
        verdict = await _reflect(question, all_chunks)
        bus.emit(THINKING, {
            "stage": f"evidence_review_{rnd}",
            "status": "completed",
            "text": (
                f"第 {rnd} 轮证据充分"
                if verdict["sufficient"]
                else f"第 {rnd} 轮发现证据缺口"
            ),
            "detail": verdict.get("gap", ""),
            "duration_ms": int((time.time() - review_t0) * 1000),
        })
        if verdict["sufficient"]:
            bus.emit(DEEP_ROUND, {"round": rnd, "verdict": "sufficient",
                                  "new_chunks": 0,
                                  "coverage": verdict.get("coverage", {})})
            break
        if not verdict["next_query"]:
            bus.emit(DEEP_ROUND, {"round": rnd, "verdict": "insufficient",
                                  "gap": verdict["gap"], "new_chunks": 0,
                                  "coverage": verdict.get("coverage", {})})
            break
        nq = verdict["next_query"]
        bus.emit(DEEP_ROUND, {"round": rnd, "verdict": "insufficient",
                              "gap": verdict["gap"], "query": nq, "new_chunks": 0,
                              "coverage": verdict.get("coverage", {})})
        added = _merge(await _retrieve_by_mode(bus, nq, mode, call_id=f"deep-{rnd}"))
        bus.emit(DEEP_ROUND, {"round": rnd, "verdict": "searched",
                              "query": nq, "new_chunks": added})
        if added == 0:
            break  # 补搜无新证据,停止

    # 汇总全部去重证据生成(_build_context / _chunks_to_citations 自行按序编号)
    context = _build_context(all_chunks)
    answer_question = memory.original_question if memory else question
    answer = await _stream_llm(bus, answer_question, context, memory=memory)
    return answer, _chunks_to_citations(all_chunks)


# ---------------- 统一入口 ----------------

async def run_qa(
    bus: EventBus, question: str, route: str = DEFAULT_ROUTE, *, deep: bool = False,
    memory: MemoryBundle | None = None,
) -> dict[str, Any]:
    """执行一次问答。事件发往 bus,返回最终结果(落库用)。

    deep=True 时对 mix/hybrid 启用自省式循环搜证:初检索→评估证据→
    补充检索(≤3轮)。progressive 自带章节级多路检索,不额外循环。
    """
    t_start = time.time()
    route = route if route in ROUTES else DEFAULT_ROUTE
    bus.emit(TASK_STATUS, {"status": "running"})

    try:
        route_used = route
        retrieval_question = memory.standalone_query if memory else question
        bus.emit(ROUTE_INFO, {"requested": route, "used": route_used,
                              "deep": deep})
        bus.emit(THINKING, {
            "stage": "intent",
            "status": "completed",
            "text": "已理解问题并选择检索策略",
            "detail": f"检索链路：{route_used}{' · 深度模式' if deep else ''}",
        })

        if deep and route_used in DEEP_MODES:
            answer, citations = await _run_deep(
                bus, retrieval_question, route_used, memory=memory,
            )
        elif route_used == "progressive":
            answer, citations = await _run_progressive(
                bus, retrieval_question, memory=memory,
            )
        elif route_used == "hybrid":
            answer, citations = await _run_hybrid(
                bus, retrieval_question, memory=memory,
            )
        else:
            answer, citations = await _run_lightrag_mode(
                bus, retrieval_question, route_used, memory=memory,
            )

        bus.emit(CITATIONS, {"items": citations})
        latency_ms = int((time.time() - t_start) * 1000)
        bus.emit(TASK_STATUS, {"status": "done", "latency_ms": latency_ms})
        return {
            "answer": answer,
            "route_requested": route,
            "route_used": route_used,
            "deep": deep,
            "citations": citations,
            "latency_ms": latency_ms,
            "model": settings.llm_model,
            "standalone_query": retrieval_question,
        }
    except Exception as exc:  # noqa: BLE001
        bus.emit(TASK_STATUS, {"status": "failed", "error": str(exc)})
        raise
