"""CLI 问答入口(评测/调试复用同一事件流)。

用法:
  python run_qa.py "电磁搅拌对等轴晶率的影响?" --route mix --deep
  python run_qa.py "..." --route hybrid --save   # 落库(评测语料)
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.events import (  # noqa: E402
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
from backend.pipelines.qa import ROUTES, run_qa  # noqa: E402


async def consume_events(bus: EventBus) -> None:
    """CLI 消费者:把事件流渲染到终端。"""
    async for ev in bus.subscribe():
        t, d = ev.type, ev.data
        if t == ROUTE_INFO:
            deep_tag = "(深度模式)" if d.get("deep") else ""
            print(f"\n🔀 检索链路: {d['used']} {deep_tag}")
        elif t == TOOL_CALL:
            print(f"🔍 {d['tool']} {d.get('args', {})}")
        elif t == TOOL_RESULT:
            print(f"   ↳ {d['summary']}")
        elif t == THINKING:
            print(f"🧠 {d['text']}")
        elif t == DEEP_ROUND:
            v = d.get("verdict")
            if v == "insufficient":
                print(f"◈ 第{d['round']}轮评估: 证据不足 — {d.get('gap', '')}"
                      f" → 补搜「{d.get('query', '')}」")
            elif v == "searched":
                print(f"   ↳ 补充检索新增 {d.get('new_chunks', 0)} 条证据")
            else:
                print(f"◈ 第{d['round']}轮评估: 证据充分 ✓")
        elif t == TEXT_DELTA:
            print(d["delta"], end="", flush=True)
        elif t == CITATIONS:
            items = d["items"]
            print(f"\n\n📚 引用({len(items)} 条):")
            for c in items[:10]:
                pages = ""
                pr = c.get("page_range")
                if pr and pr.get("start") is not None:
                    pages = f" p{pr['start']}" + (
                        f"-{pr['end']}" if pr.get("end") not in (None, pr["start"]) else ""
                    )
                print(f"  [{c['n']}] {c['source']}{pages}")
        elif t == TASK_STATUS:
            if d["status"] == "done":
                print(f"\n⏱️  {d.get('latency_ms', 0) / 1000:.1f}s")
            elif d["status"] == "failed":
                print(f"\n❌ 失败: {d.get('error')}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="知识库问答 CLI")
    parser.add_argument("question", help="问题")
    parser.add_argument("--route", default="mix", choices=ROUTES)
    parser.add_argument("--deep", action="store_true",
                        help="深度模式:循环搜证(评估证据充分性,不足则补充检索,≤3轮)")
    parser.add_argument("--save", action="store_true", help="结果落库(conversations/messages)")
    args = parser.parse_args()

    bus = EventBus(task_id="cli")
    consumer = asyncio.create_task(consume_events(bus))
    try:
        result = await run_qa(bus, args.question, route=args.route, deep=args.deep)
    finally:
        bus.close()
        await consumer

    if args.save:
        from backend import db
        conv_id = db.create_conversation(title=args.question[:50])
        db.add_message(conv_id, "user", args.question,
                       route_requested=args.route)
        msg_id = db.add_message(
            conv_id, "assistant", result["answer"],
            route_requested=result["route_requested"],
            route_used=result["route_used"],
            citations=result["citations"],
            model=result["model"],
            latency_ms=result["latency_ms"],
        )
        print(f"💾 已落库: {conv_id} / {msg_id}")


if __name__ == "__main__":
    asyncio.run(main())
