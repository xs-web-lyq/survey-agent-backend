"""检索工具:综述 agent 的证据获取层。

- search_evidence: 结构化证据检索(aquery_data 包装)
- survey_scope:   综述选题探查(文献清单 + 实体关系子图)
- expand_graph:   实体关系图邻居扩展(证据不足时的追加检索)
"""

from __future__ import annotations

from typing import Any

from backend import rag_client


def _public_name(file_path: str) -> str:
    return (file_path or "").replace("\\", "/").split("/")[-1]


def _chunk_id(chunk: dict[str, Any]) -> str:
    return str(chunk.get("chunk_id") or chunk.get("__id__") or chunk.get("id") or "")


def _chunk_score(chunk: dict[str, Any]) -> float | None:
    for key in ("score", "hybrid_score", "distance", "__metrics__"):
        value = chunk.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _select_source_balanced(
    candidates: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """按来源轮转选取候选，避免单篇长文献占满一个研究问题的证据槽位。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()
    for chunk in candidates:
        chunk_id = _chunk_id(chunk)
        source = _public_name(
            str(chunk.get("file_path") or chunk.get("source") or "")
        )
        if not chunk_id or not source or chunk_id in seen_ids:
            continue
        seen_ids.add(chunk_id)
        groups.setdefault(source, []).append(chunk)

    selected: list[dict[str, Any]] = []
    depth = 0
    while len(selected) < limit and any(
        depth < len(group) for group in groups.values()
    ):
        for group in groups.values():
            if depth < len(group):
                selected.append(group[depth])
                if len(selected) >= limit:
                    break
        depth += 1
    return selected


async def search_evidence(
    query: str, *, mode: str = "mix", top_k: int = 20, chunk_top_k: int = 8,
    allowed_sources: set[str] | None = None,
) -> dict[str, Any]:
    """检索证据 chunks(带 chunk_id / 来源文件,供撰写引用)。

    allowed_sources:文献范围(文件名集合)。LightRAG 检索原生不支持
    文档过滤,这里应用层后过滤;为补偿过滤损耗,有范围时放大取回量。
    """
    if allowed_sources:
        # 受限综述以本地 scope-first 向量检索作为完整检索路径。若仍先调用
        # LightRAG aquery_data，不但目标文献可能在全库 top-k 阶段被挤出，
        # 还会让基础 chunk 召回无谓依赖 LLM 关键词抽取及外部 API 可用性。
        fetch_k = min(chunk_top_k * 4, 40)
        raw_candidates = await rag_client.query_chunks_scoped(
            query,
            allowed_sources=allowed_sources,
            top_k=max(fetch_k, chunk_top_k * max(2, len(allowed_sources))),
        )
        in_scope_candidates = [
            chunk
            for chunk in raw_candidates
            if _public_name(
                str(chunk.get("file_path") or chunk.get("source") or "")
            ) in allowed_sources
        ]
        raw_candidates = _select_source_balanced(
            in_scope_candidates,
            limit=chunk_top_k,
        )
        data: dict[str, Any] = {}
    else:
        result = await rag_client.aquery_data(
            query, mode=mode, top_k=top_k, chunk_top_k=chunk_top_k
        )
        data = result.get("data", {})
        raw_candidates = list(data.get("chunks", []))

    chunks = []
    for c in raw_candidates:
        src = _public_name(str(c.get("file_path") or c.get("source") or ""))
        if allowed_sources and src not in allowed_sources:
            continue
        chunks.append({
            "chunk_id": _chunk_id(c),
            "source": src,
            "content": c.get("content", ""),
            "score": _chunk_score(c),
        })
        if len(chunks) >= chunk_top_k:
            break
    relations = []
    for r in data.get("relationships", [])[:15]:
        relations.append({
            "src": r.get("src_id") or r.get("source", ""),
            "tgt": r.get("tgt_id") or r.get("target", ""),
            "description": (r.get("description") or "")[:200],
        })
    return {
        "chunks": chunks,
        "relations": relations,
        "entities": [e.get("entity_name") or e.get("entity", "")
                     for e in data.get("entities", [])[:20]],
    }


async def survey_scope(
    topic: str, *, allowed_sources: set[str] | None = None,
) -> dict[str, Any]:
    """综述选题探查:相关文献清单 + 主题相关实体/关系(大纲规划的原料)。

    - 文献清单来自 doc_status(全库)+ 主题向量检索(相关度筛选)
    - 实体/关系来自 aquery_data(global 模式,偏重关系图)
    - allowed_sources:文献范围;related_documents 按范围过滤,并附带
      范围内文献摘要(大纲章节划分只应依据范围内素材)
    """
    # 1) 主题相关的图谱概览
    result = await rag_client.aquery_data(topic, mode="global", top_k=30, chunk_top_k=10)
    data = result.get("data", {})

    # 2) 主题相关文献(从命中 chunks 反查来源文档,去重)
    related_files: dict[str, int] = {}
    for c in data.get("chunks", []):
        name = _public_name(c.get("file_path", ""))
        if not name:
            continue
        if allowed_sources and name not in allowed_sources:
            continue
        related_files[name] = related_files.get(name, 0) + 1

    # 3) 全库规模(告知 agent 素材边界)
    all_docs = await rag_client.list_documents()

    scope: dict[str, Any] = {
        "kb_total_documents": len(allowed_sources) if allowed_sources else len(all_docs),
        "related_documents": [
            {"source": name, "hit_chunks": n}
            for name, n in sorted(related_files.items(), key=lambda x: -x[1])
        ],
        "key_entities": [e.get("entity_name") or e.get("entity", "")
                         for e in data.get("entities", [])[:30]],
        "key_relations": [
            {
                "src": r.get("src_id") or r.get("source", ""),
                "tgt": r.get("tgt_id") or r.get("target", ""),
                "description": (r.get("description") or "")[:150],
            }
            for r in data.get("relationships", [])[:30]
        ],
    }
    # 4) 范围内文献清单(带摘要,供大纲围绕范围素材规划)
    if allowed_sources:
        scope["scope_documents"] = [
            {"source": _public_name(d["file_path"]),
             "summary": (d.get("summary") or "")[:100]}
            for d in all_docs
            if _public_name(d["file_path"]) in allowed_sources
        ][:30]
    return scope


async def expand_graph(entity: str, *, max_neighbors: int = 15) -> dict[str, Any]:
    """实体邻居扩展:围绕某实体找相关实体与关系(跨 chunk 语义连接)。"""
    rag = await rag_client.get_rag()
    graph = rag.lightrag.chunk_entity_relation_graph
    try:
        # NetworkX 存储:直接查邻居边
        nx_graph = getattr(graph, "_graph", None)
        if nx_graph is None or entity not in nx_graph:
            # 尝试模糊匹配实体名
            candidates = [n for n in (nx_graph.nodes if nx_graph else [])
                          if entity.lower() in str(n).lower()][:3]
            if not candidates:
                return {"entity": entity, "found": False, "neighbors": []}
            entity = candidates[0]
        neighbors = []
        for nb in list(nx_graph.neighbors(entity))[:max_neighbors]:
            edge = nx_graph.get_edge_data(entity, nb) or {}
            neighbors.append({
                "entity": str(nb),
                "relation": (edge.get("description") or "")[:200],
                "sources": _public_name(edge.get("file_path", "")),
            })
        return {"entity": entity, "found": True, "neighbors": neighbors}
    except Exception as exc:  # noqa: BLE001
        return {"entity": entity, "found": False, "error": str(exc), "neighbors": []}
