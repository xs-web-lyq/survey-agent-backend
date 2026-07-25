"""引用核查:claim + chunk_id 回查原文,LLM 判断支持度。"""

from __future__ import annotations

from typing import Any

from backend import llm, rag_client

_VERIFY_PROMPT = """你是严格的引用核查员。判断下面的"论断"是否被"原文片段"支持。

论断:{claim}

原文片段:
{chunk_content}

只输出 JSON:{{"verdict": "pass" 或 "fail", "reason": "一句话理由"}}
判定标准:
- pass:原文直接支持论断,或支持论断的主体内容(数值/结论方向一致,允许论断做了合理概括或改写)。
- fail:原文与论断无关、数值或结论方向矛盾、或论断的关键主张在原文中完全找不到依据。
注意:论断是综述改写后的表述,措辞与原文不同是正常的,判断依据是事实内容是否一致。"""


async def verify_citation(claim: str, chunk_id: str) -> dict[str, Any]:
    """核查一条引用。返回 {verdict, reason, chunk_found}。"""
    chunk = await rag_client.get_chunk_by_id(chunk_id)
    if not chunk:
        return {"verdict": "fail", "reason": "chunk_id 不存在", "chunk_found": False}

    try:
        data = await llm.complete_json(None, _VERIFY_PROMPT.format(
            claim=claim,
            chunk_content=(chunk.get("content") or "")[:6000],
        ))
        return {
            "verdict": data.get("verdict", "fail"),
            "reason": data.get("reason", ""),
            "chunk_found": True,
        }
    except Exception as exc:  # noqa: BLE001
        # 单条核查的模型/网络失败不应让整篇终稿丢失，降级为人工复核。
        return {"verdict": "fail", "reason": f"核查调用失败: {exc}",
                "chunk_found": True}
