"""RAG-Anything/LightRAG 唯一封装层。

迁移原则:本工程所有代码只通过本模块访问底层 RAG 库;
底层库升级、路径变化、embedding 更换均只改这里 + .env。

初始化模式照抄主工程已验证的 working_test.py:
本地 SentenceTransformers embedding + 预初始化 LightRAG 传入 RAGAnything
(绕开 parser 检查,以只读方式打开既有知识库)。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import threading
from typing import Any

import numpy as np

from backend.config import settings

logger = logging.getLogger(__name__)

# sys.path 注入(迁移时只改 .env 的 RAG_ANYTHING_REPO)
_repo = str(settings.rag_anything_repo)
if _repo not in sys.path:
    sys.path.insert(0, _repo)

# 注入后才能导入
from lightrag import LightRAG, QueryParam  # noqa: E402
from lightrag.llm.openai import openai_complete_if_cache  # noqa: E402
from lightrag.utils import EmbeddingFunc  # noqa: E402
from raganything import RAGAnything, RAGAnythingConfig  # noqa: E402

_rag: RAGAnything | None = None
_init_lock = threading.Lock()


def llm_model_func(prompt, system_prompt=None, history_messages=None, **kwargs):
    """LLM 补全函数(LightRAG 内部检索链路使用)。

    按 LLM_BINDING_TYPE 分发到 OpenAI 兼容端点或 Anthropic messages 协议。
    LightRAG 会传入 keyword_extraction 等自有参数,anthropic 分支忽略之。
    """
    if settings.llm_binding_type.lower() == "anthropic":
        from backend import llm as llm_adapter

        return llm_adapter.complete(
            system_prompt, prompt,
            history=list(history_messages or []),
        )
    return openai_complete_if_cache(
        settings.llm_model,
        prompt,
        system_prompt=system_prompt,
        history_messages=history_messages or [],
        api_key=settings.llm_binding_api_key,
        base_url=settings.llm_binding_host,
        **kwargs,
    )


def _build_embedding_func() -> EmbeddingFunc:
    import os

    # 模型已缓存本地;避免每次启动联网校验 HF(国内访问超时会刷屏重试)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    from sentence_transformers import SentenceTransformer

    logger.info("加载本地 embedding 模型 %s (%s)...",
                settings.embedding_model_name, settings.embedding_device)
    model = SentenceTransformer(
        settings.embedding_model_name, device=settings.embedding_device
    )

    async def local_embed(texts: list[str]) -> np.ndarray:
        return model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )

    return EmbeddingFunc(
        embedding_dim=settings.embedding_dim,
        max_token_size=512,
        func=local_embed,
    )


async def get_rag() -> RAGAnything:
    """获取全局 RAGAnything 单例(懒初始化,只读打开既有 KB)。"""
    global _rag
    if _rag is not None:
        return _rag

    errors = settings.validate_paths()
    if errors:
        raise RuntimeError("配置路径错误:\n" + "\n".join(errors))

    embedding_func = _build_embedding_func()

    logger.info("预初始化 LightRAG(KB: %s)...", settings.kb_name)
    lightrag = LightRAG(
        working_dir=str(settings.rag_storage_dir),
        llm_model_func=llm_model_func,
        embedding_func=embedding_func,
    )
    await lightrag.initialize_storages()

    config = RAGAnythingConfig(
        working_dir=str(settings.rag_storage_dir),
        parser="mineru",  # 占位;传入预初始化 lightrag 后不会触发解析
    )
    _rag = RAGAnything(
        config=config,
        llm_model_func=llm_model_func,
        embedding_func=embedding_func,
        lightrag=lightrag,
    )
    logger.info("RAGAnything 就绪(KB: %s)", settings.kb_name)
    return _rag


def get_rag_sync() -> RAGAnything:
    """同步入口(CLI/自检用)。"""
    with _init_lock:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(get_rag())
        raise RuntimeError(
            "get_rag_sync() 不能在事件循环内调用,请使用 await get_rag()"
        )


# ---- 便捷再导出:上层统一从本模块拿 QueryParam,不直接 import lightrag ----
__all__ = [
    "get_rag",
    "get_rag_sync",
    "QueryParam",
    "llm_model_func",
    "query_chunks_scoped",
]


async def aquery_data(query: str, *, mode: str = "mix", top_k: int = 20,
                      chunk_top_k: int = 10) -> dict[str, Any]:
    """结构化检索:返回 entities/relationships/chunks/references。"""
    rag = await get_rag()
    param = QueryParam(
        mode=mode,
        top_k=top_k,
        chunk_top_k=chunk_top_k,
        include_references=True,
    )
    return await rag.lightrag.aquery_data(query, param)


def _source_basename(file_path: str) -> str:
    return (file_path or "").replace("\\", "/").split("/")[-1]


async def query_chunks_scoped(
    query: str,
    *,
    allowed_sources: set[str],
    top_k: int = 20,
) -> list[dict[str, Any]]:
    """在指定文献范围内执行 chunk 向量检索。

    LightRAG 的公开 ``chunks_vdb.query`` 暂不接受元数据过滤器。范围综述若
    采用“全库召回后过滤”，目标文献很容易在 top-k 之前被全库其他文献挤掉。
    过滤细节因此集中封装在本适配层：优先使用 NanoVectorDB 的 filter_lambda，
    若底层实现变化，则回退为扩大召回后过滤。上层不依赖向量库私有结构。
    """
    normalized_sources = {
        _source_basename(source).casefold()
        for source in allowed_sources
        if _source_basename(source)
    }
    if not normalized_sources or top_k <= 0:
        return []

    rag = await get_rag()
    vdb = rag.lightrag.chunks_vdb

    def in_scope(item: dict[str, Any]) -> bool:
        return _source_basename(str(item.get("file_path") or "")).casefold() in normalized_sources

    try:
        embedding = await vdb.embedding_func([query], _priority=5)
        client = await vdb._get_client()
        raw = client.query(
            query=embedding[0],
            top_k=top_k,
            better_than_threshold=vdb.cosine_better_than_threshold,
            filter_lambda=in_scope,
        )
        if inspect.isawaitable(raw):
            raw = await raw
        return [
            {
                **{key: value for key, value in item.items() if key != "vector"},
                "id": item.get("__id__") or item.get("id", ""),
                "distance": item.get("__metrics__", item.get("distance")),
                "created_at": item.get("__created_at__", item.get("created_at")),
            }
            for item in raw
        ]
    except (AttributeError, TypeError):
        # 兼容未来替换的向量存储：公开 API 不支持过滤时扩大候选集再过滤。
        candidates = await vdb.query(query, top_k=max(top_k * 20, 100))
        return [item for item in candidates if in_scope(item)][:top_k]


async def get_chunk_by_id(chunk_id: str) -> dict[str, Any] | None:
    """按 chunk_id 回查原文(引用核查用)。"""
    rag = await get_rag()
    return await rag.lightrag.text_chunks.get_by_id(chunk_id)


async def list_documents() -> list[dict[str, Any]]:
    """知识库文献清单(survey_scope 用):doc_id、文件名、摘要、chunk 数。

    直接读 doc_status JSON(只读),避免依赖存储实现的遍历接口。
    """
    import json

    status_file = settings.rag_storage_dir / "kv_store_doc_status.json"
    if not status_file.exists():
        return []
    with open(status_file, encoding="utf-8") as f:
        all_docs = json.load(f)
    docs = []
    for doc_id, meta in all_docs.items():
        if meta.get("status") not in (None, "processed"):
            continue
        docs.append({
            "doc_id": doc_id,
            "file_path": meta.get("file_path", ""),
            "summary": (meta.get("content_summary") or "")[:300],
            "chunks_count": meta.get("chunks_count", 0),
        })
    return docs
