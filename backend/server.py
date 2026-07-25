"""FastAPI 服务:问答 SSE、对话管理、反馈、综述任务、静态前端。

单进程部署:uvicorn backend.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend import db
from backend.config import PROJECT_ROOT, settings
from backend.events import EventBus
from backend.pipelines.qa import ROUTES, run_qa

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 预热:启动时初始化 RAG(embedding 模型加载较慢,避免首问超时)
    from backend.rag_client import get_rag
    try:
        await get_rag()
        logger.info("RAG 预热完成")
    except Exception:
        logger.exception("RAG 预热失败(首次请求时将重试)")
    yield


app = FastAPI(title="Research Copilot", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 开发期放开;生产同源部署,无跨域
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------- 问答 ----------------

class ChatRequest(BaseModel):
    question: str
    route: str = "mix"
    conv_id: str | None = None
    deep: bool = False


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """问答:SSE 流式返回事件(text_delta/citations/route_info/...)。"""
    if req.route not in ROUTES:
        raise HTTPException(400, f"route 必须是 {ROUTES}")

    from backend.events import (
        MEMORY_COMPACTED, MEMORY_LOADED, MEMORY_UPDATED, QUERY_REWRITTEN,
    )
    from backend.memory import memory_service

    conv_id = req.conv_id or db.create_conversation(title=req.question[:50])
    memory = await memory_service.prepare_turn(conv_id, req.question)
    user_message_id = db.add_message(
        conv_id, "user", req.question, route_requested=req.route,
    )
    turn_id = memory_service.start_turn(memory, user_message_id)

    bus = EventBus(task_id=conv_id)
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
    result_holder: dict = {}

    async def worker():
        try:
            result = await run_qa(
                bus, req.question, route=req.route, deep=req.deep, memory=memory,
            )
            trace_types = {
                "memory_loaded", "query_rewritten", "thinking", "route_info", "tool_call",
                "tool_result", "deep_round", "memory_compacted", "memory_updated",
            }
            trace = [
                {"type": ev.type, "data": ev.data, "ts": ev.ts}
                for ev in bus.history if ev.type in trace_types
            ]
            # 落库放在 worker 里:即使客户端断线,回答也不丢
            msg_id = db.add_message(
                conv_id, "assistant", result["answer"],
                route_requested=result["route_requested"],
                route_used=result["route_used"],
                citations=result["citations"],
                trace=trace,
                model=result["model"],
                latency_ms=result["latency_ms"],
            )
            result_holder["message_id"] = msg_id
            try:
                memory_result = memory_service.complete_turn(
                    memory, turn_id, user_message_id, msg_id, result["answer"],
                    result["citations"],
                )
                bus.emit(MEMORY_UPDATED, {
                    "topic": memory_result["state"].get("current_topic", ""),
                    "new_memories": len(memory_result["new_memory_ids"]),
                })
                if memory_result.get("compacted"):
                    bus.emit(MEMORY_COMPACTED, memory_result["compacted"])
            except Exception:
                # Memory is an enhancement layer: a persistence or compaction
                # failure must never discard an otherwise valid answer.
                logger.exception("记忆更新失败，回答已正常保存")
            finally:
                final_trace = [
                    {"type": ev.type, "data": ev.data, "ts": ev.ts}
                    for ev in bus.history if ev.type in trace_types
                ]
                db.update_message_trace(msg_id, final_trace)
        except Exception:
            memory_service.fail_turn(turn_id)
            logger.exception("问答执行失败")
        finally:
            bus.close()

    task = asyncio.create_task(worker())

    async def event_stream():
        # 首个事件:告知 conv_id(新会话时前端需要)
        yield {"event": "meta", "data": f'{{"conv_id": "{conv_id}"}}'}
        async for ev in bus.subscribe():
            yield {"event": ev.type, "id": str(ev.seq), "data": ev.to_json()}
        await task
        # 回答已落库,推送 message_id(前端打分用)
        if result_holder.get("message_id"):
            yield {"event": "saved",
                   "data": f'{{"message_id": "{result_holder["message_id"]}"}}'}

    return EventSourceResponse(event_stream())


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
    return {"id": conv_id, "deleted": True}


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
