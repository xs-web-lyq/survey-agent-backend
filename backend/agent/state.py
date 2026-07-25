"""任务状态:大纲、进度、人在环输入队列。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from backend.tools.files import WorkspaceFS


@dataclass
class SurveyState:
    task_id: str
    topic: str
    fs: WorkspaceFS
    outline: dict[str, Any] = field(default_factory=dict)
    completed_sections: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)       # 已覆盖要点(防跨节重复)
    section_length: str = "medium"                       # short | medium | long
    doc_scope: list[str] = field(default_factory=list)   # 文献范围(文件名);空=全库
    context: str = ""                                    # 头脑风暴讨论结论
    checkpoint: dict[str, Any] = field(default_factory=dict)
    input_queue: asyncio.Queue[dict] = field(default_factory=asyncio.Queue)
    pending_instructions: list[str] = field(default_factory=list)  # 用户插话

    def save_meta(self) -> None:
        # 保留 task_manager 写入的 status/created_at 等字段,只更新进度
        meta: dict[str, Any] = {}
        try:
            meta = json.loads(self.fs.read("task.json"))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        meta.update({
            "task_id": self.task_id,
            "topic": self.topic,
            "outline": self.outline,
            "completed_sections": self.completed_sections,
            "section_length": self.section_length,
            "doc_scope": self.doc_scope,
            "context": self.context,
            "checkpoint": self.checkpoint,
        })
        meta.setdefault("status", "running")
        self.fs.write_atomic("task.json", json.dumps(meta, ensure_ascii=False, indent=2))

    def mark_checkpoint(self, phase: str, status: str, **detail: Any) -> None:
        """记录可恢复边界；只描述进度，不复制大体积证据。"""
        self.checkpoint = {
            "phase": phase,
            "status": status,
            "updated_at": time.time(),
            **detail,
        }
        self.save_meta()

    def push_user_input(self, payload: dict) -> None:
        """外部(API/CLI)注入用户输入:大纲确认或中途插话。"""
        self.input_queue.put_nowait(payload)

    async def wait_input(self) -> dict:
        """阻塞等待用户输入(need_input 事件后调用)。"""
        return await self.input_queue.get()

    def drain_instructions(self) -> list[str]:
        """取走积压的插话指令(每节开始前检查)。"""
        out = list(self.pending_instructions)
        self.pending_instructions.clear()
        return out

    def collect_nowait(self) -> None:
        """非阻塞收取队列中的插话(不等待)。"""
        while True:
            try:
                item = self.input_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item.get("kind") == "instruction":
                self.pending_instructions.append(str(item.get("text", "")))
