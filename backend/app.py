"""Loopback-only API for the independently installed editorial agent."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .settings import configure_runtime

RUNTIME = configure_runtime()
REVIEWABLE = {"draft", "saved_as_draft", "approved"}

from apps.web_service import Workbench, valid_conversation_id, valid_id  # noqa: E402
from src.knowledge.store import KnowledgeStore  # noqa: E402
from src.storage.files import load_post  # noqa: E402

from .evidence import source_evidence
from .runs import local_draft_ids
from .task_recognition import RecognitionService, call_model
from .progress import activity_reply, build_activity, checkpoint_job_records, is_status_question, read_checkpoint, run_overview


class ConversationCreate(BaseModel):
    title: str = Field(default="新对话", max_length=120)


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=12000)


class SourceCheckCreate(BaseModel):
    model_config = {"extra": "forbid"}
    collection: Literal["all", "daily_news", "ai_digest"] = "all"
    keywords: str = Field(default="国际冲突 科技产业 社会民生 财经产业", max_length=400)
    max_age_days: int = Field(default=2, ge=1, le=14, strict=True)


class PlanConfirm(BaseModel):
    conversation_id: str
    version: int
    skill_mode: str = "off"
    skill_names: list[str] = Field(default_factory=list)


class ModelRoles(BaseModel):
    agent: str = ""
    writer: str = ""
    image: str = ""


class TaskRecognitionCreate(BaseModel):
    model_config = {"extra": "forbid"}
    source_message_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    base_plan_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    base_plan_version: int = Field(ge=1, strict=True)


class TaskRecognitionAdopt(BaseModel):
    model_config = {"extra": "forbid"}
    base_plan_version: int = Field(ge=1, strict=True)


class DraftReview(BaseModel):
    updated_at: str
    checks: dict[str, bool]
    note: str = Field(default="", max_length=1000)


app = FastAPI(title="采编智能体", docs_url=None, redoc_url=None, openapi_url=None)
app.state.token = secrets.token_urlsafe(32)
app.state.service = None
app.state.recognitions = None
recognition_call = call_model


def service() -> Workbench:
    if app.state.service is None:
        store = KnowledgeStore.from_env()
        if store.status().get("status") != "ready":
            raise RuntimeError("独立 PostgreSQL 不可用，请检查 E 盘运行区的数据库")
        app.state.service = Workbench(root=RUNTIME)
    return app.state.service


def recognition_service() -> RecognitionService:
    current = service()
    with current.lock:
        existing = app.state.recognitions
        if existing is None or existing.current is not current:
            existing = RecognitionService(current, lambda config, payload: recognition_call(config, payload))
            app.state.recognitions = existing
        return existing


def ensure_review_schema() -> None:
    store = KnowledgeStore.from_env()
    with store.connection("migration") as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('redbook-agent-review-schema'))")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent.draft_reviews (
                post_id text PRIMARY KEY, post_updated_at text NOT NULL,
                checks jsonb NOT NULL, note text NOT NULL DEFAULT '',
                reviewed_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON agent.draft_reviews TO redbook_app")


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.headers.get("host", "")
    expected = f"127.0.0.1:{os.getenv('REDBOOK_AGENT_PORT', '8786')}"
    if host != expected:
        return JSONResponse({"error": "只允许本机 127.0.0.1 访问"}, status_code=403)
    origin = request.headers.get("origin", "")
    if origin and origin != f"http://{expected}":
        return JSONResponse({"error": "已拒绝外部网页访问"}, status_code=403)
    document_navigation = (
        request.method == "GET"
        and request.url.path == "/"
        and request.headers.get("sec-fetch-mode") == "navigate"
        and request.headers.get("sec-fetch-dest") == "document"
    )
    if request.headers.get("sec-fetch-site") == "cross-site" and not document_navigation:
        return JSONResponse({"error": "已拒绝跨站请求"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
    return response


def authenticated(request: Request):
    cookie = request.cookies.get("redbook_agent", "")
    if not secrets.compare_digest(cookie, app.state.token):
        raise HTTPException(403, "会话已失效，请刷新界面")
    if request.method not in {"GET", "HEAD"} and request.headers.get("x-workbench") != "1":
        raise HTTPException(403, "缺少请求校验头")


@app.exception_handler(ValueError)
@app.exception_handler(RuntimeError)
@app.exception_handler(FileNotFoundError)
async def user_error(_request: Request, exc: Exception):
    current = app.state.service
    return JSONResponse({"error": current.redact(str(exc)) if current else str(exc)}, status_code=400)


@app.get("/api/health")
def health():
    return {"status": "ready", "database": KnowledgeStore.from_env().status()}


@app.post("/api/session")
def session(request: Request):
    if request.headers.get("x-workbench") != "1":
        raise HTTPException(403, "缺少请求校验头")
    response = JSONResponse({"status": "ready"})
    response.set_cookie("redbook_agent", app.state.token, httponly=True, samesite="strict", secure=False)
    return response


@app.get("/api/conversations", dependencies=[Depends(authenticated)])
def conversations():
    return {"rows": service().list_agent_conversations()}


@app.get("/api/sources", dependencies=[Depends(authenticated)])
def sources():
    return service().sources()


@app.post("/api/sources/check", dependencies=[Depends(authenticated)])
def check_sources(body: SourceCheckCreate, idempotency_key: str = Header(default="")):
    return service().submit({"kind": "check-sources", "title": "信源检测", **body.model_dump()}, idempotency_key)


@app.post("/api/conversations", dependencies=[Depends(authenticated)])
def create_conversation(body: ConversationCreate):
    return service().create_agent_conversation(body.title)


@app.get("/api/conversations/{conversation_id}", dependencies=[Depends(authenticated)])
def conversation(conversation_id: str):
    return service().get_agent_conversation(valid_conversation_id(conversation_id))


@app.post("/api/conversations/{conversation_id}/messages", dependencies=[Depends(authenticated)])
def add_message(conversation_id: str, body: MessageCreate):
    current = service()
    conversation_id = valid_conversation_id(conversation_id)
    if is_status_question(body.content):
        with current.lock:
            saved = current._read_agent_conversation(conversation_id)
            latest = (saved.get("runs") or [None])[-1]
            detail = run(latest) if latest else None
            content = activity_reply(detail["activity"]) if detail else "这个对话还没有执行记录。请先提交任务并确认执行计划。"
            now = time.time()
            message = {"id": uuid4().hex, "role": "user", "content": body.content.strip(), "created_at": now}
            assistant = {"id": uuid4().hex, "role": "assistant", "content": content, "created_at": now}
            saved["messages"].extend([message, assistant])
            current._write_agent_conversation(saved)
        return current.redact({"message": message, "assistant": assistant, "plan": None, "run": detail})
    try:
        parsed = current._parse_agent_message(body.content)
    except ValueError:
        return current.append_agent_message(conversation_id, body.content)
    if parsed.get("plan_kind") == "draft_management" and (parsed.get("management") or {}).get("mode") == "publish":
        raise ValueError("独立界面暂不执行公开发布；请先生成、审查并上传草稿，发布请在平台人工确认")
    return current.append_agent_message(valid_conversation_id(conversation_id), body.content)


@app.post("/api/plans/{plan_id}/confirm", dependencies=[Depends(authenticated)])
def confirm_plan(plan_id: str, body: PlanConfirm, idempotency_key: str = Header(default="")):
    current = service()
    conversation = current.get_agent_conversation(valid_conversation_id(body.conversation_id))
    plan = next((item for item in conversation.get("plans", []) if item.get("id") == valid_id(plan_id)), None)
    if plan and plan.get("plan_kind") == "draft_management" and (plan.get("management") or {}).get("mode") == "publish":
        raise ValueError("独立界面暂不执行公开发布；请在平台人工确认")
    with current.lock:
        recognition_service().assert_can_execute(body.conversation_id, plan_id)
        return current.execute_agent_plan(
            valid_conversation_id(body.conversation_id), valid_id(plan_id), body.version,
            idempotency_key or uuid4().hex, skill_mode=body.skill_mode, skill_names=body.skill_names,
        )


@app.post("/api/conversations/{conversation_id}/task-recognitions", dependencies=[Depends(authenticated)])
def create_task_recognition(conversation_id: str, body: TaskRecognitionCreate, idempotency_key: str = Header(default="")):
    if len(idempotency_key) > 120:
        raise ValueError("校准请求键过长")
    return recognition_service().start(valid_conversation_id(conversation_id), body.model_dump(), idempotency_key or uuid4().hex)


@app.get("/api/conversations/{conversation_id}/task-recognitions/{recognition_id}", dependencies=[Depends(authenticated)])
def task_recognition(conversation_id: str, recognition_id: str):
    return recognition_service().get(valid_conversation_id(conversation_id), valid_id(recognition_id))


@app.post("/api/conversations/{conversation_id}/task-recognitions/{recognition_id}/adopt", dependencies=[Depends(authenticated)])
def adopt_task_recognition(conversation_id: str, recognition_id: str, body: TaskRecognitionAdopt):
    return recognition_service().adopt(valid_conversation_id(conversation_id), valid_id(recognition_id), body.base_plan_version)


@app.post("/api/conversations/{conversation_id}/task-recognitions/{recognition_id}/discard", dependencies=[Depends(authenticated)])
def discard_task_recognition(conversation_id: str, recognition_id: str):
    return recognition_service().discard(valid_conversation_id(conversation_id), valid_id(recognition_id))


@app.get("/api/runs", dependencies=[Depends(authenticated)])
def runs():
    return {"rows": [{**row, **run_overview(row)} for row in service().list_jobs()]}


@app.get("/api/runs/{run_id}", dependencies=[Depends(authenticated)])
def run(run_id: str):
    run_id = valid_id(run_id)
    current = service()
    try:
        detail = current.job_detail(run_id)
    except KeyError:
        raise HTTPException(404, "没有找到这次运行记录")
    agent_run_id = current.agent_checkpoint_id(run_id)
    detail["agent_run_id"] = agent_run_id
    detail["local_post_ids"] = local_draft_ids(RUNTIME, agent_run_id)
    checkpoint = read_checkpoint(RUNTIME, agent_run_id)
    uploaded = set(checkpoint.get("uploaded_post_ids") or [])
    job_records = checkpoint_job_records(checkpoint)
    delivered = uploaded | set(detail["local_post_ids"])
    detail["retained_post_ids"] = list(dict.fromkeys(
        pid for record in job_records for pid in record.get("reviewed_post_ids") or []
        if isinstance(pid, str) and re.fullmatch(r"[a-f0-9]{32}", pid) and pid not in delivered
    ))
    # Resumed runs can include delivered IDs absent from the current process log.
    known = {row["id"] for row in detail.get("post_rows", [])}
    checkpoint_ids = set(checkpoint.get("post_ids") or []) | uploaded | set(detail["retained_post_ids"])
    checkpoint_ids.update(pid for record in job_records for pid in record.get("post_ids") or [])
    for post_id in sorted(checkpoint_ids - known):
        if not isinstance(post_id, str) or not re.fullmatch(r"[a-f0-9]{32}", post_id):
            continue
        try:
            post = current.post(post_id)
            detail.setdefault("post_rows", []).append({
                "id": post_id, "title": post.get("title"), "status": post.get("status"),
                "images": len(post.get("assets") or []), "readback": post.get("readback"),
            })
        except (OSError, ValueError):
            continue
    detail["activity"] = current.redact(build_activity(detail, checkpoint))
    return detail


@app.get("/api/runs/{run_id}/events", dependencies=[Depends(authenticated)])
def run_events(run_id: str, conversation_id: str, after: int = 0):
    valid_id(run_id)
    return service().agent_events(valid_conversation_id(conversation_id), after)


@app.get("/api/runs/{run_id}/stream", dependencies=[Depends(authenticated)])
async def stream_events(run_id: str, conversation_id: str, after: int = 0):
    valid_id(run_id)
    valid_conversation_id(conversation_id)

    async def events():
        cursor = max(0, after)
        for _ in range(60):
            result = await asyncio.to_thread(service().agent_events, conversation_id, cursor)
            for event in result["events"]:
                cursor = max(cursor, int(event["id"]))
                yield f"id: {cursor}\ndata: {json.dumps(service().redact(event), ensure_ascii=False)}\n\n"
            if all(job.get("status") not in {"queued", "running", "waiting_user"} for job in result.get("jobs", [])):
                break
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/runs/{run_id}/resume", dependencies=[Depends(authenticated)])
def resume(run_id: str, body: dict[str, Any], idempotency_key: str = Header(default="")):
    return service().resume_agent_run(
        valid_conversation_id(str(body.get("conversation_id") or "")), valid_id(run_id),
        idempotency_key or uuid4().hex,
    )


@app.get("/api/drafts", dependencies=[Depends(authenticated)])
def drafts():
    return {"rows": [row for row in service().posts() if row.get("status") in REVIEWABLE]}


@app.get("/api/drafts/{post_id}", dependencies=[Depends(authenticated)])
def draft(post_id: str):
    post_id = valid_id(post_id)
    detail = service().post(post_id)
    record = load_post(post_id, base=RUNTIME / "data")
    detail["evidence"] = source_evidence(record.platform or {})
    return detail


@app.get("/api/drafts/{post_id}/images/{index}", dependencies=[Depends(authenticated)])
def draft_image(post_id: str, index: int):
    return FileResponse(service().image(valid_id(post_id), index))


@app.post("/api/drafts/{post_id}/review", dependencies=[Depends(authenticated)])
def review_draft(post_id: str, body: DraftReview):
    post_id = valid_id(post_id)
    current = service().post(post_id)
    if current["status"] not in REVIEWABLE:
        raise ValueError("只有未发布且可审查的本地草稿可以记录核验")
    if current["updated_at"] != body.updated_at:
        raise ValueError("草稿已变化，请重新核验")
    required = {"source", "date", "body", "image"}
    if set(body.checks) != required or not all(body.checks.values()):
        raise ValueError("来源、日期、正文和图片都要核验后才能通过")
    if not current["assets"]:
        raise ValueError("草稿没有可核验的图片")
    from psycopg.types.json import Jsonb

    with KnowledgeStore.from_env().connection() as conn, conn.transaction():
        conn.execute("""
            INSERT INTO agent.draft_reviews(post_id, post_updated_at, checks, note)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(post_id) DO UPDATE SET
                post_updated_at=excluded.post_updated_at, checks=excluded.checks,
                note=excluded.note, reviewed_at=now()
        """, (post_id, body.updated_at, Jsonb(body.checks), body.note))
    return {"post_id": post_id, "status": "reviewed"}


@app.get("/api/connections", dependencies=[Depends(authenticated)])
def connections():
    root = RUNTIME / "data/browser"
    profile = service().environment().get("XHS_CHROME_USER_DATA_DIR", "")
    return {
        "database": KnowledgeStore.from_env().status(),
        "providers": service().providers(),
        "models": service().models(),
        "profile_configured": bool(profile and Path(profile).is_relative_to(root.resolve())),
        "profile_login": "未验证",
    }


@app.post("/api/profile/open", dependencies=[Depends(authenticated)])
def open_profile():
    current = service()
    current.assert_idle()
    profile = Path(current.environment()["XHS_CHROME_USER_DATA_DIR"]).resolve()
    if not profile.is_relative_to((RUNTIME / "data/browser").resolve()):
        raise ValueError("项目专用 profile 路径不合法")
    chrome = Path(os.getenv("REDBOOK_CHROME_EXECUTABLE") or r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if not chrome.is_file():
        found = shutil.which("chrome.exe")
        if not found:
            raise RuntimeError("未找到 Chrome，请设置 REDBOOK_CHROME_EXECUTABLE")
        chrome = Path(found)
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [str(chrome), f"--user-data-dir={profile}", "--profile-directory=Default",
         "https://creator.xiaohongshu.com/"],
        cwd=str(RUNTIME),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return {"status": "opened", "login": "请在弹出的项目专用浏览器中完成登录"}


@app.put("/api/model-roles", dependencies=[Depends(authenticated)])
def save_roles(body: ModelRoles):
    return service().save_model_bindings(body.model_dump())


from .wool_library import create_wool_library_router

app.include_router(create_wool_library_router(service), dependencies=[Depends(authenticated)])

FRONTEND = Path(__file__).resolve().parents[1] / "frontend/dist"
if FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")


@app.on_event("startup")
def startup():
    ensure_review_schema()
    service()
