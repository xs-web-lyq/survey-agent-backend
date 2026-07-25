"""反馈库 → 大模型微调语料导出。

格式:
  sharegpt  {"conversations": [{"from": "human", ...}, {"from": "gpt", ...}]}
  alpaca    {"instruction", "input", "output"}
  dpo       {"conversations"/"prompt", "chosen", "rejected"}

规则:
  - SFT(sharegpt/alpaca):取 score >= min_score(默认 4)或 rating=+1 且未打星的回答;
    better_answer 非空时优先用 better_answer 作为 output。
  - DPO:better_answer 非空 → (better_answer, 原回答) 构成 (chosen, rejected);
    同一问题下同时存在高分(>=4)与低分(<=2)回答 → 构成偏好对。
  - 每条带 metadata(route/kb/citations 数),便于筛选与分析。
"""

from __future__ import annotations

import json
from typing import Any

from backend import db
from backend.config import settings


def _sft_rows(min_score: int) -> list[dict[str, Any]]:
    rows = db.list_feedback(limit=100000)
    out = []
    for r in rows:
        if not r.get("question"):
            continue
        good = (r.get("score") or 0) >= min_score or (
            r.get("rating") == 1 and r.get("score") is None
        )
        if not good and not r.get("better_answer"):
            continue
        answer = r.get("better_answer") or r.get("answer", "")
        if not answer.strip():
            continue
        out.append({
            "question": r["question"],
            "answer": answer,
            "meta": {
                "route": r.get("route_used", ""),
                "kb": settings.kb_name,
                "score": r.get("score"),
                "rating": r.get("rating"),
                "from_better_answer": bool(r.get("better_answer")),
                "message_id": r.get("message_id", ""),
            },
        })
    return out


def export_sharegpt(min_score: int = 4) -> list[dict[str, Any]]:
    return [
        {
            "conversations": [
                {"from": "human", "value": r["question"]},
                {"from": "gpt", "value": r["answer"]},
            ],
            "metadata": r["meta"],
        }
        for r in _sft_rows(min_score)
    ]


def export_alpaca(min_score: int = 4) -> list[dict[str, Any]]:
    return [
        {
            "instruction": r["question"],
            "input": "",
            "output": r["answer"],
            "metadata": r["meta"],
        }
        for r in _sft_rows(min_score)
    ]


def export_dpo() -> list[dict[str, Any]]:
    rows = db.list_feedback(limit=100000)
    pairs: list[dict[str, Any]] = []

    # 1) better_answer vs 原回答
    for r in rows:
        if r.get("better_answer") and r.get("question"):
            pairs.append({
                "conversations": [{"from": "human", "value": r["question"]}],
                "chosen": {"from": "gpt", "value": r["better_answer"]},
                "rejected": {"from": "gpt", "value": r.get("answer", "")},
                "metadata": {"source": "better_answer",
                             "route": r.get("route_used", ""),
                             "kb": settings.kb_name},
            })

    # 2) 同问题高分 vs 低分
    by_q: dict[str, list[dict]] = {}
    for r in rows:
        q = (r.get("question") or "").strip()
        if q and r.get("score") is not None:
            by_q.setdefault(q, []).append(r)
    for q, group in by_q.items():
        highs = [r for r in group if (r.get("score") or 0) >= 4]
        lows = [r for r in group if (r.get("score") or 0) <= 2]
        for h in highs:
            for low in lows:
                if h["message_id"] == low["message_id"]:
                    continue
                pairs.append({
                    "conversations": [{"from": "human", "value": q}],
                    "chosen": {"from": "gpt", "value": h.get("answer", "")},
                    "rejected": {"from": "gpt", "value": low.get("answer", "")},
                    "metadata": {"source": "score_pair",
                                 "chosen_route": h.get("route_used", ""),
                                 "rejected_route": low.get("route_used", ""),
                                 "kb": settings.kb_name},
                })
    return pairs


def export_jsonl(fmt: str = "sharegpt", min_score: int = 4) -> str:
    """导出为 JSONL 字符串(API 下载用)。"""
    if fmt == "sharegpt":
        data = export_sharegpt(min_score)
    elif fmt == "alpaca":
        data = export_alpaca(min_score)
    elif fmt == "dpo":
        data = export_dpo()
    else:
        raise ValueError(f"未知格式: {fmt}")
    return "\n".join(json.dumps(d, ensure_ascii=False) for d in data)
