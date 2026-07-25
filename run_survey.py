"""CLI 综述入口。

用法:
  python run_survey.py "电磁搅拌对连铸坯凝固组织的影响" --auto-approve
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
import uuid
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.agent.phases import run_survey  # noqa: E402
from backend.agent.state import SurveyState  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.events import (  # noqa: E402
    CITATION_CHECK,
    FILE_WRITE,
    NEED_INPUT,
    PHASE,
    TASK_STATUS,
    TEXT_DELTA,
    THINKING,
    TOOL_CALL,
    TOOL_RESULT,
    EventBus,
)
from backend.tools.files import WorkspaceFS  # noqa: E402

PHASE_NAMES = {"outline": "规划大纲", "writing": "逐节撰写", "finalize": "整合核查"}


async def consume(bus: EventBus, state: SurveyState, auto_approve: bool) -> None:
    streaming = False
    async for ev in bus.subscribe():
        t, d = ev.type, ev.data
        if t == PHASE:
            if streaming:
                print()
                streaming = False
            name = PHASE_NAMES.get(d["name"], d["name"])
            mark = "▶" if d["status"] == "start" else "✔"
            print(f"\n{mark} 阶段:{name}")
        elif t == THINKING:
            if streaming:
                print()
                streaming = False
            print(f"🧠 {d['text']}")
        elif t == TOOL_CALL:
            print(f"🔍 {d['tool']} {d.get('args', {})}")
        elif t == TOOL_RESULT:
            print(f"   ↳ {d['summary']}")
        elif t == TEXT_DELTA:
            streaming = True
            print(d["delta"], end="", flush=True)
        elif t == FILE_WRITE:
            if streaming:
                print()
                streaming = False
            print(f"💾 落盘: {d['path']}")
        elif t == CITATION_CHECK:
            icon = "✅" if d["verdict"] == "pass" else "❌"
            print(f"{icon} 引用核查 {d['chunk_id'][:20]}… {d.get('claim', '')[:40]}")
        elif t == NEED_INPUT and d.get("kind") == "approve_outline":
            print("\n📋 大纲已生成(见 workspace 下 outline.md)。")
            if auto_approve:
                continue
            ans = input("回车确认大纲,或输入修改意见: ").strip()
            if ans:
                state.push_user_input({"kind": "revise_outline", "text": ans})
            else:
                state.push_user_input({"kind": "approve"})
        elif t == TASK_STATUS:
            if d["status"] == "done":
                print(f"\n🎉 完成!引用核查 {d.get('citations_passed', 0)}/"
                      f"{d.get('citations_total', 0)} 通过,"
                      f"参考文献 {d.get('references', 0)} 篇")
            elif d["status"] == "failed":
                print(f"\n❌ 失败: {d.get('error')}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="综述生成 CLI")
    parser.add_argument("topic", help="综述主题")
    parser.add_argument("--auto-approve", action="store_true",
                        help="跳过大纲人工确认")
    parser.add_argument("--length", default="medium",
                        choices=["short", "medium", "long"],
                        help="章节篇幅档位(默认 medium)")
    parser.add_argument("--task-id", default="", help="指定任务 ID(默认自动生成)")
    args = parser.parse_args()

    task_id = args.task_id or f"survey-{uuid.uuid4().hex[:8]}"
    fs = WorkspaceFS(task_id)
    state = SurveyState(task_id=task_id, topic=args.topic, fs=fs,
                        section_length=args.length)
    bus = EventBus(task_id=task_id, jsonl_path=fs.root / "events.jsonl")

    print(f"📁 工作区: {fs.root}")
    consumer = asyncio.create_task(consume(bus, state, args.auto_approve))
    try:
        await run_survey(bus, state, auto_approve=args.auto_approve)
    finally:
        bus.close()
        await consumer
    print(f"\n📄 综述: {fs.root / 'survey.md'}")


if __name__ == "__main__":
    asyncio.run(main())
