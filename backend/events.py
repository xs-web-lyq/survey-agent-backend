"""事件协议:harness 的地基。

内核(问答链路/综述 agent)每步产出 Event;CLI、Web SSE、评测脚本
消费同一条事件流。事件同步落盘 JSONL,支持断线回放与轨迹复盘。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

# 事件类型常量(前端/CLI 依据 type 渲染)
PHASE = "phase"                    # {name, status: start|end}
THINKING = "thinking"              # {text}
TOOL_CALL = "tool_call"            # {tool, args, call_id}
TOOL_RESULT = "tool_result"        # {call_id, summary, detail?}
TEXT_DELTA = "text_delta"          # {target?, delta}
STREAM_RESET = "stream_reset"      # {target} 重写产物前清空前端历史流缓冲
FILE_WRITE = "file_write"          # {path}
CITATION_CHECK = "citation_check"  # {claim, chunk_id, verdict}
NEED_INPUT = "need_input"          # {kind, payload}
TASK_STATUS = "task_status"        # {status: running|waiting_input|done|failed, error?}
CITATIONS = "citations"           # {items: [...]} 问答回答的引用列表
ROUTE_INFO = "route_info"         # {requested, used, decision?} 实际走的检索链路
DEEP_ROUND = "deep_round"         # {round, verdict, gap?, query?, new_chunks} 深度模式每轮搜证
EVIDENCE_MATRIX_UPDATED = "evidence_matrix_updated"  # {section, status, coverage}
MEMORY_LOADED = "memory_loaded"   # {recent_messages, summary_version, durable_count}
QUERY_REWRITTEN = "query_rewritten"  # {original, standalone, resolved_references}
MEMORY_UPDATED = "memory_updated" # {topic, new_memories}
MEMORY_COMPACTED = "memory_compacted"  # {summary_id, through_message_id}


@dataclass
class Event:
    type: str
    data: dict[str, Any]
    task_id: str = ""
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "data": self.data,
            "task_id": self.task_id,
            "seq": self.seq,
            "ts": self.ts,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class EventBus:
    """单任务事件总线:内存队列(实时消费)+ JSONL 落盘(回放)。"""

    def __init__(
        self,
        task_id: str,
        jsonl_path: Path | None = None,
        on_emit: Callable[[Event], None] | None = None,
    ):
        self.task_id = task_id
        self.jsonl_path = jsonl_path
        self.on_emit = on_emit
        self._seq = 0
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._history: list[Event] = []
        self._closed = False
        if jsonl_path:
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            # 同一任务恢复执行时延续事件序号并预载历史，保证 SSE 重连
            # 能看到完整轨迹，且不会因序号从 1 重新开始而漏事件。
            if jsonl_path.exists():
                for raw in replay_events(jsonl_path):
                    try:
                        ev = Event(
                            type=str(raw["type"]),
                            data=dict(raw.get("data") or {}),
                            task_id=str(raw.get("task_id") or task_id),
                            seq=int(raw.get("seq") or 0),
                            ts=float(raw.get("ts") or time.time()),
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                    self._history.append(ev)
                    self._seq = max(self._seq, ev.seq)

    def emit(self, type: str, data: dict[str, Any]) -> Event:
        self._seq += 1
        ev = Event(type=type, data=data, task_id=self.task_id, seq=self._seq)
        self._history.append(ev)
        if self.jsonl_path:
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(ev.to_json() + "\n")
        if self.on_emit:
            self.on_emit(ev)
        self._queue.put_nowait(ev)
        return ev

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(None)

    @property
    def history(self) -> list[Event]:
        return list(self._history)

    async def subscribe(self, after_seq: int = 0) -> AsyncIterator[Event]:
        """订阅事件流。after_seq>0 时先回放历史(断线重连)。

        注意:历史回放和实时队列可能重叠,用 last_seq 去重。
        """
        last_seq = after_seq
        for ev in self._history:
            if ev.seq > last_seq:
                last_seq = ev.seq
                yield ev
        if self._closed and self._queue.empty():
            return
        while True:
            ev = await self._queue.get()
            if ev is None:
                return
            if ev.seq > last_seq:
                last_seq = ev.seq
                yield ev


def replay_events(jsonl_path: Path) -> list[dict[str, Any]]:
    """从落盘 JSONL 读取完整事件历史(任务结束后的回放/复盘)。"""
    if not jsonl_path.exists():
        return []
    events = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events
