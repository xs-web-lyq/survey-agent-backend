"""FastAPI 服务:问答 SSE、对话管理、反馈、综述任务、静态前端。

单进程部署:uvicorn backend.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend import db
from backend.config import PROJECT_ROOT, settings
from backend.events import EventBus
from backend.health import admin_access_status, readiness_snapshot, runtime_health
from backend.pipelines.qa import ROUTES, run_qa

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async def warm_rag() -> None:
        from backend.rag_client import get_rag

        # Let the ASGI lifespan yield first. RAG initialization performs heavy
        # synchronous imports/model loading before its first internal await.
        await asyncio.sleep(0.1)
        runtime_health.set_rag("warming")
        try:
            await get_rag()
            runtime_health.set_rag("ready")
            logger.info("RAG 预热完成")
        except Exception as exc:
            runtime_health.set_rag("failed", type(exc).__name__)
            logger.exception("RAG 预热失败，readyz 将保持不可用")

    warmup_task: asyncio.Task | None = None
    if settings.startup_rag_warmup:
        warmup_task = asyncio.create_task(warm_rag())
    else:
        runtime_health.set_rag("deferred")
    yield
    if warmup_task and not warmup_task.done():
        warmup_task.cancel()
        with suppress(asyncio.CancelledError):
            await warmup_task


app = FastAPI(title="Research Copilot", lifespan=lifespan, debug=settings.debug)
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Admin-Token"],
    )


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex[:16])
    logger.exception("unhandled request error request_id=%s", request_id)
    detail = (
        str(exc)
        if settings.debug and not settings.is_production
        else "服务器内部错误，请稍后重试。"
    )
    return JSONResponse(
        status_code=500,
        content={"detail": detail, "request_id": request_id},
        headers={"X-Request-ID": request_id},
    )


# ---------------- 健康检查与预检 ----------------

@app.get("/healthz")
async def healthz():
    """Process liveness only; never initializes or calls a model."""
    return {"status": "ok", "service": "survey-agent-backend"}


@app.get("/readyz")
async def readyz():
    """Traffic readiness: database, configured paths and RAG warmup state."""
    ready, components = readiness_snapshot()
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "components": components},
    )


def _verify_admin_access(token: str | None) -> None:
    allowed, status_code, message = admin_access_status(token)
    if not allowed:
        raise HTTPException(status_code, message)


@app.post("/api/admin/preflight/model")
async def model_preflight(
    admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    """Explicit low-token provider/model permission check with a hard timeout."""
    from backend import llm

    _verify_admin_access(admin_token)
    try:
        return await llm.preflight()
    except TimeoutError:
        logger.warning("model preflight timed out model=%s", settings.llm_model)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "code": "timeout", "message": "模型预检超时。"},
        )
    except Exception as exc:
        logger.exception("model preflight failed model=%s", settings.llm_model)
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "code": type(exc).__name__,
                "message": "模型、权限或账户状态预检失败，请检查服务端配置。",
            },
        )


# ---------------- 问答 ----------------

class ChatRequest(BaseModel):
    question: str
    route: str = "mix"
    conv_id: str | None = None
    deep: bool = False


CHAT_TRACE_TYPES = {
    "memory_loaded", "query_rewritten", "thinking", "route_info", "tool_call",
    "tool_result", "deep_round", "memory_compacted", "memory_updated", "task_status",
}


def _chat_trace(bus: EventBus) -> list[dict]:
    return [
        {"type": ev.type, "data": ev.data, "ts": ev.ts}
        for ev in bus.history if ev.type in CHAT_TRACE_TYPES
    ]


def _safe_chat_error(exc: Exception) -> dict[str, str]:
    """Return a stable client-facing error without leaking provider internals."""
    return {
        "code": type(exc).__name__,
        "message": "本轮问答执行失败，请稍后重试。",
    }


async def _start_chat_run(
    *,
    question: str,
    route: str,
    conv_id: str | None,
    deep: bool,
    user_message_id: str | None = None,
    retry_of_run_id: str = "",
) -> EventSourceResponse:
    from backend.events import (
        MEMORY_COMPACTED, MEMORY_LOADED, MEMORY_UPDATED, QUERY_REWRITTEN,
        TASK_STATUS, TEXT_DELTA, THINKING,
    )
    from backend.memory import memory_service

    conv_id = conv_id or db.create_conversation(title=question[:50])
    if user_message_id is None:
        user_message_id = db.add_message(
            conv_id, "user", question, route_requested=route,
        )
    assistant_message_id = db.add_message(
        conv_id,
        "assistant",
        "",
        route_requested=route,
        status="running",
    )
    run_id = db.create_turn_run(
        conv_id,
        user_message_id,
        assistant_message_id,
        route_requested=route,
        request={"question": question, "route": route, "deep": deep},
        retry_of_run_id=retry_of_run_id,
    )

    current_stage = "preparing"

    def persist_event(ev):
        nonlocal current_stage
        stage_by_type = {
            MEMORY_LOADED: "memory",
            QUERY_REWRITTEN: "memory",
            "route_info": "retrieving",
            "tool_call": "retrieving",
            "tool_result": "retrieving",
            "deep_round": "retrieving",
            TEXT_DELTA: "generating",
            "citations": "generating",
            MEMORY_UPDATED: "memory_update",
            MEMORY_COMPACTED: "memory_update",
        }
        current_stage = stage_by_type.get(ev.type, current_stage)
        if ev.type not in CHAT_TRACE_TYPES:
            return
        trace = _chat_trace(bus)
        db.update_turn_run(run_id, stage=current_stage, trace=trace)
        db.update_message(assistant_message_id, trace=trace)

    bus = EventBus(task_id=run_id, on_emit=persist_event)
    result_holder: dict[str, str] = {
        "message_id": assistant_message_id,
        "run_id": run_id,
        "status": "running",
    }

    async def worker():
        nonlocal current_stage
        turn_id: str | None = None
        try:
            db.update_turn_run(run_id, status="retrieving", stage="memory")
            memory = await memory_service.prepare_turn(conv_id, question)
            turn_id = memory_service.start_turn(memory, user_message_id)
            bus.emit(MEMORY_LOADED, {
                "recent_messages": len(memory.recent_messages),
                "summary_version": memory.thread_summary.get("version"),
                "durable_count": len(memory.durable_memories),
                "topic": memory.thread_state.get("current_topic", ""),
            })
            if memory.standalone_query != memory.original_question:
                bus.emit(QUERY_REWRITTEN, {
                    "original": memory.original_question,
                    "standalone": memory.standalone_query,
                    "resolved_references": memory.resolved_references,
                    "topic_shift": memory.topic_shift,
                })

            result = await run_qa(
                bus, question, route=route, deep=deep, memory=memory,
            )
            try:
                memory_result = memory_service.complete_turn(
                    memory,
                    turn_id,
                    user_message_id,
                    assistant_message_id,
                    result["answer"],
                    result["citations"],
                )
                bus.emit(MEMORY_UPDATED, {
                    "topic": memory_result["state"].get("current_topic", ""),
                    "new_memories": len(memory_result["new_memory_ids"]),
                })
                if memory_result.get("compacted"):
                    bus.emit(MEMORY_COMPACTED, memory_result["compacted"])
            except Exception:
                logger.exception("记忆更新失败，回答已正常保存")

            bus.emit(TASK_STATUS, {
                "status": "done",
                "stage": "completed",
                "run_id": run_id,
                "latency_ms": result["latency_ms"],
            })
            trace = _chat_trace(bus)
            db.update_message(
                assistant_message_id,
                content=result["answer"],
                route_used=result["route_used"],
                citations=result["citations"],
                trace=trace,
                model=result["model"],
                latency_ms=result["latency_ms"],
                status="completed",
                error={},
                run_id=run_id,
            )
            db.update_turn_run(
                run_id,
                status="completed",
                stage="completed",
                route_used=result["route_used"],
                model=result["model"],
                trace=trace,
                finished=True,
            )
            result_holder["status"] = "completed"
        except Exception as exc:
            if turn_id is not None:
                memory_service.fail_turn(turn_id)
            error = _safe_chat_error(exc)
            logger.exception("问答执行失败 run_id=%s stage=%s", run_id, current_stage)
            bus.emit(THINKING, {
                "text": "本轮执行失败",
                "stage": current_stage,
                "status": "failed",
                "detail": error["message"],
            })
            bus.emit(TASK_STATUS, {
                "status": "failed",
                "stage": current_stage,
                "run_id": run_id,
                "error": error["message"],
                "error_code": error["code"],
            })
            partial_answer = "".join(
                str(ev.data.get("delta", ""))
                for ev in bus.history if ev.type == TEXT_DELTA
            )
            trace = _chat_trace(bus)
            db.update_message(
                assistant_message_id,
                content=partial_answer,
                trace=trace,
                status="failed",
                error={**error, "stage": current_stage},
                run_id=run_id,
            )
            db.update_turn_run(
                run_id,
                status="failed",
                stage=current_stage,
                error_code=error["code"],
                error_message=error["message"],
                trace=trace,
                finished=True,
            )
            result_holder["status"] = "failed"
        finally:
            bus.close()

    task = asyncio.create_task(worker())

    async def event_stream():
        yield {
            "event": "meta",
            "data": json.dumps({
                "conv_id": conv_id,
                "run_id": run_id,
                "message_id": assistant_message_id,
            }),
        }
        async for ev in bus.subscribe():
            yield {"event": ev.type, "id": str(ev.seq), "data": ev.to_json()}
        await task
        yield {
            "event": "saved",
            "data": json.dumps(result_holder),
        }

    return EventSourceResponse(event_stream())


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """问答:SSE 流式返回事件，并持久化运行状态和失败轨迹。"""
    if req.route not in ROUTES:
        raise HTTPException(400, f"route 必须是 {ROUTES}")
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "question 不能为空")
    return await _start_chat_run(
        question=question,
        route=req.route,
        conv_id=req.conv_id,
        deep=req.deep,
    )


@app.get("/api/runs/{run_id}")
async def chat_run_detail(run_id: str):
    run = db.get_turn_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run


@app.post("/api/runs/{run_id}/retry")
async def retry_chat_run(run_id: str):
    run = db.get_turn_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if run["status"] != "failed":
        raise HTTPException(409, "only failed runs can be retried")
    user_message = db.get_message(run["user_message_id"])
    if not user_message:
        raise HTTPException(409, "source user message is missing")
    request = run.get("request") or {}
    route = str(request.get("route") or run.get("route_requested") or "mix")
    if route not in ROUTES:
        route = "mix"
    return await _start_chat_run(
        question=str(user_message["content"]),
        route=route,
        conv_id=str(run["conv_id"]),
        deep=bool(request.get("deep", False)),
        user_message_id=str(run["user_message_id"]),
        retry_of_run_id=run_id,
    )


# ---------------- 头脑风暴(选题顾问) ----------------

class BrainstormRequest(BaseModel):
    message: str
    conv_id: str | None = None


@app.post("/api/brainstorm")
async def brainstorm(req: BrainstormRequest):
    """选题头脑风暴:SSE 流式,KB 感知的多轮选题讨论。"""
    from backend.pipelines.brainstorm import run_brainstorm

    if not req.message.strip():
        raise HTTPException(400, "message 不能为空")

    conv_id = req.conv_id or db.create_conversation(
        title=f"💡 {req.message[:40]}")
    history: list[dict] = []
    conv = db.get_conversation(conv_id)
    if conv:
        history = [{"role": m["role"], "content": m["content"]}
                   for m in conv.get("messages", [])]
    db.add_message(conv_id, "user", req.message, route_requested="brainstorm")

    bus = EventBus(task_id=conv_id)
    result_holder: dict = {}

    async def worker():
        try:
            result = await run_brainstorm(bus, req.message, history)
            msg_id = db.add_message(
                conv_id, "assistant", result["answer"],
                route_requested="brainstorm", route_used="brainstorm",
                trace=[
                    {"type": ev.type, "data": ev.data, "ts": ev.ts}
                    for ev in bus.history
                    if ev.type in {"thinking", "tool_call", "tool_result", "deep_round"}
                ],
                model=settings.llm_model,
                latency_ms=result["latency_ms"],
            )
            result_holder["message_id"] = msg_id
        except Exception:
            logger.exception("头脑风暴执行失败")
        finally:
            bus.close()

    task = asyncio.create_task(worker())

    async def event_stream():
        yield {"event": "meta", "data": f'{{"conv_id": "{conv_id}"}}'}
        async for ev in bus.subscribe():
            yield {"event": ev.type, "id": str(ev.seq), "data": ev.to_json()}
        await task
        if result_holder.get("message_id"):
            yield {"event": "saved",
                   "data": f'{{"message_id": "{result_holder["message_id"]}"}}'}

    return EventSourceResponse(event_stream())


@app.post("/api/brainstorm/{conv_id}/conclude")
async def brainstorm_conclude(conv_id: str):
    """把讨论和最终证据包总结为结构化研究简报。"""
    from backend.pipelines.brainstorm import conclude_brainstorm

    conv = db.get_conversation(conv_id)
    if not conv or not conv.get("messages"):
        raise HTTPException(404, "conversation not found or empty")
    history = [{"role": m["role"], "content": m["content"]}
               for m in conv["messages"]]
    latest_scope: dict = {}
    for message in reversed(conv["messages"]):
        if message.get("role") != "assistant":
            continue
        for item in reversed(message.get("trace") or []):
            if item.get("type") != "tool_result":
                continue
            detail = (item.get("data") or {}).get("detail")
            if isinstance(detail, dict) and "related_documents" in detail:
                latest_scope = detail
                break
        if latest_scope:
            break
    return await conclude_brainstorm(history, latest_scope)


# ---------------- 对话 ----------------

@app.get("/api/conversations")
async def conversations():
    return db.list_conversations()


@app.get("/api/trash/conversations")
async def deleted_conversations():
    return db.list_deleted_conversations()


@app.post("/api/trash/conversations/{conv_id}/restore")
async def restore_conversation(conv_id: str):
    if not db.restore_conversation(conv_id):
        raise HTTPException(404, "deleted conversation not found")
    return {"id": conv_id, "restored": True}


@app.delete("/api/trash/conversations/{conv_id}")
async def purge_conversation(
    conv_id: str,
    delete_durable_memories: bool = False,
):
    if not db.purge_conversation(
        conv_id, delete_durable_memories=delete_durable_memories,
    ):
        raise HTTPException(404, "deleted conversation not found")
    return {
        "id": conv_id,
        "purged": True,
        "durable_memories_deleted": delete_durable_memories,
    }


@app.get("/api/conversations/{conv_id}")
async def conversation_detail(conv_id: str):
    conv = db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "conversation not found")
    return conv


class ConversationUpdateRequest(BaseModel):
    title: str


@app.patch("/api/conversations/{conv_id}")
async def update_conversation(conv_id: str, req: ConversationUpdateRequest):
    title = req.title.strip()
    if not title:
        raise HTTPException(400, "title cannot be empty")
    if len(title) > 120:
        raise HTTPException(400, "title is too long (max 120 characters)")
    if not db.get_conversation(conv_id):
        raise HTTPException(404, "conversation not found")
    db.update_conversation_title(conv_id, title)
    return {"id": conv_id, "title": title}


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    if not db.delete_conversation(conv_id):
        raise HTTPException(404, "conversation not found")
    return {"id": conv_id, "deleted": True, "moved_to_trash": True}


@app.get("/api/conversations/{conv_id}/export.md")
async def export_conversation_markdown(conv_id: str):
    from fastapi.responses import PlainTextResponse

    from backend.conversation_export import conversation_to_markdown

    conversation = db.get_conversation(conv_id)
    if not conversation:
        raise HTTPException(404, "conversation not found")
    title = (conversation.get("title") or conv_id).strip()
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in title).strip("_")
    filename = f"{safe_name or conv_id}.md"
    return PlainTextResponse(
        conversation_to_markdown(conversation),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="conversation-{conv_id}.md"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


class MemorySettingsRequest(BaseModel):
    use_memories: bool = True
    generate_memories: bool = True


@app.get("/api/conversations/{conv_id}/memory")
async def conversation_memory(conv_id: str):
    from backend.memory import memory_service
    if not db.get_conversation(conv_id):
        raise HTTPException(404, "conversation not found")
    return memory_service.debug_state(conv_id)


class ForkConversationRequest(BaseModel):
    through_message_id: str | None = None


@app.post("/api/conversations/{conv_id}/fork")
async def fork_conversation(conv_id: str, req: ForkConversationRequest):
    from backend.memory import store
    new_id = db.fork_conversation(conv_id, req.through_message_id)
    if not new_id:
        raise HTTPException(404, "conversation not found")
    state = store.get_state(conv_id)
    if state:
        store.upsert_state(new_id, state)
    store.add_lineage(new_id, conv_id, req.through_message_id)
    return {"id": new_id}


@app.patch("/api/conversations/{conv_id}/memory")
async def update_conversation_memory(conv_id: str, req: MemorySettingsRequest):
    from backend.memory import memory_service
    if not db.get_conversation(conv_id):
        raise HTTPException(404, "conversation not found")
    return memory_service.update_settings(
        conv_id,
        use_memories=req.use_memories,
        generate_memories=req.generate_memories,
    )


@app.get("/api/memories")
async def memories(include_inactive: bool = False):
    from backend.memory import store
    return store.list_memories(include_inactive=include_inactive)


@app.delete("/api/memories/{memory_id}")
async def forget_memory(memory_id: str):
    from backend.memory import store
    if not store.set_memory_status(memory_id, "forgotten"):
        raise HTTPException(404, "memory not found")
    return {"ok": True}


# ---------------- 反馈 ----------------

class FeedbackRequest(BaseModel):
    message_id: str
    rating: int = 0            # +1 / -1
    score: int | None = None   # 1~5
    tags: list[str] = []
    comment: str = ""
    better_answer: str = ""


@app.post("/api/feedback")
async def submit_feedback(req: FeedbackRequest):
    if not db.get_message(req.message_id):
        raise HTTPException(404, "message not found")
    fb_id = db.upsert_feedback(
        req.message_id, rating=req.rating, score=req.score,
        tags=req.tags, comment=req.comment, better_answer=req.better_answer,
    )
    return {"feedback_id": fb_id}


@app.get("/api/feedback")
async def feedback_list(route: str = "", min_score: int | None = None):
    return db.list_feedback(route=route, min_score=min_score)


@app.get("/api/feedback/stats")
async def feedback_statistics():
    return db.feedback_stats()


@app.get("/api/export/finetune")
async def export_finetune(format: str = "sharegpt", min_score: int = 4):
    """微调语料 JSONL 下载。format: sharegpt | alpaca | dpo"""
    from fastapi.responses import PlainTextResponse

    from backend.export_finetune import export_jsonl
    try:
        content = export_jsonl(fmt=format, min_score=min_score)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return PlainTextResponse(
        content,
        media_type="application/jsonl",
        headers={
            "Content-Disposition":
                f'attachment; filename="finetune_{format}_{settings.kb_name}.jsonl"'
        },
    )


# ---------------- 综述任务 ----------------

class SurveyCreateRequest(BaseModel):
    topic: str
    auto_approve: bool = False
    section_length: str = "medium"       # short | medium | long
    doc_scope: list[str] = []            # 文献范围(文件名);空=全库
    context: str = ""                    # 头脑风暴讨论结论(注入大纲生成)


class SurveyInputRequest(BaseModel):
    kind: str            # approve | revise_outline | update_outline | instruction
    text: str = ""
    payload: dict | None = None   # update_outline 携带完整大纲 JSON


class EvidenceSupplementRequest(BaseModel):
    section_id: str
    question_id: str
    rounds: int = 2


@app.post("/api/tasks")
async def create_survey_task(req: SurveyCreateRequest):
    from backend.task_manager import manager
    if not req.topic.strip():
        raise HTTPException(400, "topic 不能为空")
    task_id = manager.create(
        req.topic.strip(), auto_approve=req.auto_approve,
        section_length=req.section_length, doc_scope=req.doc_scope,
        context=req.context,
    )
    return {"task_id": task_id}


@app.get("/api/documents")
async def list_kb_documents():
    """知识库文献清单(综述创建时的文献范围选择器用)。"""
    from backend import rag_client
    return await rag_client.list_documents()


@app.get("/api/tasks")
async def list_survey_tasks():
    from backend.task_manager import manager
    return manager.list_tasks()


@app.get("/api/tasks/{task_id}")
async def survey_task_detail(task_id: str):
    """返回含重启后有效状态的任务快照。"""
    from backend.task_manager import manager

    task = manager.get_task(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return task


@app.get("/api/tasks/{task_id}/events")
async def survey_events(task_id: str, after: int = 0):
    """SSE 事件流:活动任务实时推送;已结束任务回放 events.jsonl。"""
    from backend.task_manager import manager

    bus = manager.get_bus(task_id)

    async def stream():
        if bus is not None:
            async for ev in bus.subscribe(after_seq=after):
                yield {"event": ev.type, "id": str(ev.seq), "data": ev.to_json()}
        else:
            for e in manager.replay(task_id, after_seq=after):
                yield {"event": e["type"], "id": str(e["seq"]),
                       "data": json.dumps(e, ensure_ascii=False)}
        yield {"event": "stream_end", "data": "{}"}

    return EventSourceResponse(stream())


@app.post("/api/tasks/{task_id}/message")
async def survey_input(task_id: str, req: SurveyInputRequest):
    """人在环:大纲确认/编辑/修改、中途插话。"""
    from backend.task_manager import manager
    ok = manager.push_input(task_id, {"kind": req.kind, "text": req.text,
                                      "payload": req.payload})
    if not ok:
        raise HTTPException(404, "task not active")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/retry-finalize", status_code=202)
async def retry_survey_finalize(task_id: str):
    """从已有章节恢复失败任务，只重跑终稿整合和引用核查。"""
    from backend.task_manager import manager

    try:
        manager.retry_finalize(task_id)
    except FileNotFoundError:
        raise HTTPException(404, "task not found") from None
    except RuntimeError:
        raise HTTPException(409, "task is already running") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "task_id": task_id, "phase": "finalize"}


@app.post("/api/tasks/{task_id}/resume", status_code=202)
async def resume_survey_task(task_id: str):
    """从最后一个持久化检查点继续，自动选择逐节写作或终稿阶段。"""
    from backend.task_manager import manager

    try:
        phase = manager.resume(task_id)
    except FileNotFoundError:
        raise HTTPException(404, "task not found") from None
    except RuntimeError:
        raise HTTPException(409, "task is already running") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "task_id": task_id, "phase": phase}


@app.get("/api/tasks/{task_id}/evidence-matrix")
async def survey_evidence_matrix(task_id: str):
    """返回面向 UI 的证据覆盖矩阵，不暴露完整 chunk 正文。"""
    from backend.agent.evidence_store import read_evidence_matrix
    from backend.tools.files import WorkspaceFS

    fs = WorkspaceFS(task_id)
    if not fs.exists("task.json"):
        raise HTTPException(404, "task not found")
    try:
        meta = json.loads(fs.read("task.json"))
    except json.JSONDecodeError as exc:
        raise HTTPException(409, "task metadata is invalid") from exc
    outline = meta.get("outline")
    if not isinstance(outline, dict):
        raise HTTPException(409, "outline is not ready")
    return read_evidence_matrix(
        fs, task_id=task_id, outline=outline,
    )


@app.post("/api/tasks/{task_id}/evidence/supplement", status_code=202)
async def supplement_survey_evidence(
    task_id: str,
    req: EvidenceSupplementRequest,
):
    """对一个研究问题追加检索，随后重写受影响章节并重新整合终稿。"""
    from backend.task_manager import manager

    try:
        manager.supplement_evidence(
            task_id,
            section_id=req.section_id,
            question_id=req.question_id,
            rounds=req.rounds,
        )
    except FileNotFoundError:
        raise HTTPException(404, "task not found") from None
    except RuntimeError:
        raise HTTPException(409, "task is already running") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "ok": True,
        "task_id": task_id,
        "section_id": req.section_id,
        "question_id": req.question_id,
    }


@app.get("/api/tasks/{task_id}/files")
async def survey_files(task_id: str):
    from backend.tools.files import WorkspaceFS
    fs = WorkspaceFS(task_id)
    return fs.list()


@app.get("/api/tasks/{task_id}/export.zip")
async def survey_export_zip(task_id: str):
    """导出综述 zip:survey.md(图片链接重写为相对路径)+ images/ + references.md。"""
    import io
    import re as _re
    import zipfile

    from fastapi.responses import Response

    from backend import images as kb_images
    from backend.tools.files import WorkspaceFS

    fs = WorkspaceFS(task_id)
    if not fs.exists("survey.md"):
        raise HTTPException(404, "survey.md 不存在(任务未完成?)")
    survey = fs.read("survey.md")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 收集图片 token,打包并重写链接为相对路径
        tokens = list(dict.fromkeys(_re.findall(r"/api/kb-images/([0-9a-f]+)", survey)))
        for tok in tokens:
            p = kb_images.resolve_token(tok)
            if p is None:
                continue
            arcname = f"images/{tok}{p.suffix}"
            zf.write(p, arcname)
            survey = survey.replace(f"/api/kb-images/{tok}", arcname)
        zf.writestr("survey.md", survey)
        for extra in ("references.md", "bibliography.json", "outline.md"):
            if fs.exists(extra):
                zf.writestr(extra, fs.read(extra))

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="{task_id}.zip"'},
    )


@app.get("/api/tasks/{task_id}/files/{file_path:path}")
async def survey_file_content(task_id: str, file_path: str):
    from backend.tools.files import WorkspaceFS
    fs = WorkspaceFS(task_id)
    try:
        return {"path": file_path, "content": fs.read(file_path)}
    except (FileNotFoundError, PermissionError):
        raise HTTPException(404, "file not found")


# ---------------- 知识库图片(引用溯源卡)----------------

@app.get("/api/kb-images/{token}")
async def kb_image(token: str):
    from backend.images import resolve_token
    path = resolve_token(token)
    if not path:
        raise HTTPException(404, "image not found")
    return FileResponse(path)


# ---------------- 元信息 ----------------

@app.get("/api/meta")
async def meta():
    return {
        "kb_name": settings.kb_name,
        "llm_model": settings.llm_model,
        "routes": ROUTES,
    }


# ---------------- 静态前端(构建产物)----------------

_dist = settings.frontend_dist_dir
if _dist.exists():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        file = _dist / full_path
        if full_path and file.is_file():
            return FileResponse(file)
        return FileResponse(_dist / "index.html")
