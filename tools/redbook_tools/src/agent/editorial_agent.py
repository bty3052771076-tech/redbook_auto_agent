"""Durable editorial orchestration with retained work and fair deficit recovery.

The graph owns orchestration and recovery. Domain work stays in injected tools,
which keeps the agent testable without network, model, or browser access.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import time
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, TypedDict
from uuid import uuid4


_SENSITIVE_KEY_RE = re.compile(r"(?i)(api[_-]?key|access[_-]?token|authorization|secret|password)")
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|authorization)([=:\s]+)[^\s&,]+"
)


def _redact_text(value: str) -> str:
    text = str(value or "")
    for name, secret in os.environ.items():
        if _SENSITIVE_KEY_RE.search(name) and secret and len(secret) >= 6:
            text = text.replace(secret, "[已隐藏]")
    text = _SENSITIVE_ASSIGNMENT_RE.sub(r"\1\2[已隐藏]", text)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{10,}", "[已隐藏]", text)

try:
    from langgraph.graph import END, START, StateGraph
except ImportError as exc:  # pragma: no cover - dependency is declared in requirements
    raise RuntimeError("LangGraph is required for the editorial agent") from exc


AgentProgress = Callable[[str, str, str], None]

TERMINAL_PLATFORM_FAILURE_CODES = (
    "XHS_RISK_BLOCKED",
    "XHS_CHALLENGE_REQUIRED",
    "XHS_LOGIN_REQUIRED",
    "XHS_RATE_LIMITED",
    "XHS_WRITE_UNCERTAIN",
    "XHS_PENDING_REVIEW",
    "XHS_PLATFORM_RESTRICTED",
    "XHS_PLATFORM_REJECTED",
    "XHS_STATE_STORE_UNAVAILABLE",
)

TERMINAL_PROVIDER_FAILURE_MARKERS = (
    "Token Plan 用量上限",
    "insufficient_quota",
    "insufficient_balance",
    "insufficient balance",
    "quota exhausted",
    "quota exceeded",
    "credits exhausted",
    "额度耗尽",
    "余额不足",
    "invalid api key",
    "missing api key",
)


@dataclass(frozen=True)
class AgentJob:
    """One independent editorial output in a single agent run."""

    kind: str
    title: str
    count: int = 1
    prompt: str = ""
    evaluation_viewpoint: str = "无视角评价"
    lookback_days: object = None

    def normalized(self) -> "AgentJob":
        kind = str(self.kind or "daily_news").strip().lower()
        if kind not in {"daily_news", "daily_ai_digest", "daily_wool", "daily_wow", "daily_global_map"}:
            raise ValueError(f"unsupported agent job kind: {kind}")
        return AgentJob(
            kind=kind,
            title=str(self.title or "").strip() or kind,
            count=max(1, int(self.count)),
            prompt=str(self.prompt or "").strip(),
            evaluation_viewpoint=str(self.evaluation_viewpoint or "无视角评价").strip(),
            lookback_days=self.lookback_days,
        )


@dataclass(frozen=True)
class EditorialAgentConfig:
    """Execution policy; quality gates are always enabled by the adapter."""

    provider: str = "minimax"
    use_subscription: bool = True
    max_attempts_per_job: int = 2
    # Compatibility inputs only: attempts and elapsed time no longer terminate
    # a healthy job. max_steps sizes one checkpointed graph invocation.
    max_steps: int = 64
    max_elapsed_s: float = 0
    no_progress_limit: int = 0
    retry_delay_s: float = 2.0
    checkpoint_dir: Path = Path("data") / "runs" / "agent"
    resume_from: Path | None = None
    checkpoint_backend: str = "json"
    conversation_context: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> "EditorialAgentConfig":
        provider = str(self.provider or "").strip().lower().replace("-", "_")
        checkpoint_backend = str(self.checkpoint_backend or "json").strip().lower()
        if provider not in {"minimax", "aliyun", "volcengine", "siliconflow"}:
            raise ValueError("智能体主控模型供应商必须是已接入的内置供应商")
        if provider == "minimax" and self.use_subscription and str(os.getenv("ALLOW_PAID_LLM_FALLBACK", "0")).lower() in {
            "1", "true", "yes", "on"
        }:
            raise ValueError("paid LLM fallback must be disabled for MiniMax subscription mode")
        if self.max_steps < 1 or self.no_progress_limit < 0 or self.retry_delay_s < 0:
            raise ValueError("agent step window and no-progress policy are invalid")
        if checkpoint_backend not in {"json", "postgres"}:
            raise ValueError("checkpoint backend must be json or postgres")
        if not isinstance(self.conversation_context, dict):
            raise ValueError("conversation context must be an object")
        raw_constraints = self.conversation_context.get("constraints") or []
        if not isinstance(raw_constraints, list) or any(not isinstance(item, str) for item in raw_constraints):
            raise ValueError("conversation context constraints must be strings")
        raw_skills = self.conversation_context.get("skills") or []
        if not isinstance(raw_skills, list) or any(not isinstance(item, dict) for item in raw_skills):
            raise ValueError("conversation context skills must be objects")
        conversation_context = {
            "snapshot_version": max(0, int(self.conversation_context.get("snapshot_version") or 0)),
            "through_seq": max(0, int(self.conversation_context.get("through_seq") or 0)),
            "summary": str(self.conversation_context.get("summary") or "")[:6000],
            "constraints": [item.strip()[:500] for item in raw_constraints[:30] if item.strip()],
            "skills": [
                {
                    "name": str(item.get("name") or "")[:80],
                    "version_hash": str(item.get("version_hash") or "")[:64],
                    "body": str(item.get("body") or "")[:12000],
                }
                for item in raw_skills[:3]
            ],
        }
        return EditorialAgentConfig(
            provider=provider,
            use_subscription=self.use_subscription,
            max_attempts_per_job=int(self.max_attempts_per_job),
            max_steps=max(16, min(200, int(self.max_steps))),
            max_elapsed_s=0,
            no_progress_limit=int(self.no_progress_limit),
            retry_delay_s=float(self.retry_delay_s),
            checkpoint_dir=Path(self.checkpoint_dir),
            resume_from=Path(self.resume_from) if self.resume_from else None,
            checkpoint_backend=checkpoint_backend,
            conversation_context=conversation_context,
        )


@dataclass
class EditorialAgentTools:
    """Business tools supplied by the CLI/GUI adapter or by tests."""

    sync_context: Callable[[AgentJob], dict[str, Any]]
    generate: Callable[[AgentJob, dict[str, Any]], list[Any]]
    review: Callable[[AgentJob, list[Any], dict[str, Any]], list[str] | dict[str, Any]]
    upload: Callable[[AgentJob, Any, dict[str, Any]], tuple[bool, str]]
    plan: Callable[[list[AgentJob], dict[str, Any]], dict[str, Any]] | None = None
    load_posts: Callable[[list[str]], list[Any]] | None = None
    upload_enabled: bool = True
    # The adapter may keep one browser context for the whole reviewed batch.
    # The returned mapping is keyed by post id and is still interpreted one
    # item at a time so checkpoints remain resumable and auditable.
    upload_batch: Callable[[AgentJob, list[Any], dict[str, Any]], dict[str, tuple[bool, str]]] | None = None
    # Read-only audit of freshly loaded completed artifacts on explicit resume.
    revalidate_completed: Callable[[AgentJob, list[Any], dict[str, Any]], list[str]] | None = None


class AgentState(TypedDict, total=False):
    run_id: str
    jobs: list[dict[str, Any]]
    job_index: int
    attempts: dict[str, int]
    current_job: dict[str, Any]
    context: dict[str, Any]
    conversation_memory: dict[str, Any]
    controller_decision: dict[str, Any]
    plan_complete: bool
    posts: list[Any]
    post_ids: list[str]
    reviewed_posts: list[Any]
    reviewed_post_ids: list[str]
    uploaded_posts: list[Any]
    uploaded_post_ids: list[str]
    item_status: dict[str, str]
    errors: list[str]
    events: list[dict[str, Any]]
    status: str
    last_failure: str
    failed_jobs: list[int]
    recovery_attempts: dict[str, int]
    event_log_path: str
    next_event_id: int
    started_at: float
    root_started_at: float
    resume_count: int
    budget_exceeded: bool
    platform_paused: bool
    provider_paused: bool
    last_node: str
    steps: int
    job_states: dict[str, dict[str, Any]]
    completed_job_indices: list[int]
    approved_versions: dict[str, str]
    retryable: bool
    retry_delay_s: float
    review_complete: bool
    job_blocked: bool
    sync_failed: bool


def _job_from_dict(value: dict[str, Any]) -> AgentJob:
    return AgentJob(**value).normalized()


def _post_id(post: Any) -> str:
    return str(getattr(post, "id", "") or "")


def _post_ids(posts: list[Any]) -> list[str]:
    return [post_id for post in posts if (post_id := _post_id(post))]


def _content_version(post: Any) -> str:
    """Return a stable local version so a changed post is never skipped blindly."""
    if hasattr(post, "model_dump"):
        value: Any = post.model_dump()
    elif hasattr(post, "__dict__"):
        value = vars(post)
    else:
        value = {"id": _post_id(post), "value": str(post)}
    return hashlib.sha256(
        json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def _safe_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _safe_value(value.model_dump())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(k): _safe_value(v)
            for k, v in value.items()
            if not _SENSITIVE_KEY_RE.search(str(k))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(v) for v in value]
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _checkpoint_payload(state: AgentState) -> dict[str, Any]:
    """Persist state needed for audit/resume without credentials or raw secrets."""
    return {
        "run_id": state.get("run_id", ""),
        "jobs": _safe_value(state.get("jobs", [])),
        "job_index": int(state.get("job_index", 0)),
        "attempts": _safe_value(state.get("attempts", {})),
        "context": _safe_value(state.get("context", {})),
        "controller_decision": _safe_value(state.get("controller_decision", {})),
        "plan_complete": bool(state.get("plan_complete", False)),
        "post_ids": list(state.get("post_ids") or _post_ids(state.get("posts", []))),
        "reviewed_post_ids": list(state.get("reviewed_post_ids") or _post_ids(state.get("reviewed_posts", []))),
        "uploaded_post_ids": list(state.get("uploaded_post_ids") or _post_ids(state.get("uploaded_posts", []))),
        "item_status": _safe_value(state.get("item_status", {})),
        "errors": list(state.get("errors", [])),
        "failed_jobs": list(state.get("failed_jobs", [])),
        "recovery_attempts": _safe_value(state.get("recovery_attempts", {})),
        "event_log_path": str(state.get("event_log_path", "")),
        "next_event_id": int(state.get("next_event_id", 0)),
        "started_at": float(state.get("started_at", 0.0)),
        "root_started_at": float(state.get("root_started_at", state.get("started_at", 0.0))),
        "resume_count": int(state.get("resume_count", 0)),
        "budget_exceeded": bool(state.get("budget_exceeded", False)),
        "platform_paused": bool(state.get("platform_paused", False)),
        "provider_paused": bool(state.get("provider_paused", False)),
        "events": _safe_value(state.get("events", [])[-80:]),
        "status": state.get("status", "running"),
        "last_failure": state.get("last_failure", ""),
        "last_node": state.get("last_node", ""),
        "steps": int(state.get("steps", 0)),
        "job_states": _safe_value({
            key: {k: v for k, v in record.items() if k not in {"posts", "reviewed_posts"}}
            for key, record in state.get("job_states", {}).items()
        }),
        "completed_job_indices": list(state.get("completed_job_indices", [])),
        "approved_versions": dict(state.get("approved_versions", {})),
        "retryable": bool(state.get("retryable", True)),
        "review_complete": bool(state.get("review_complete", False)),
        "job_blocked": bool(state.get("job_blocked", False)),
        "saved_at": time.time(),
    }


def _save_checkpoint(state: AgentState, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tmp").mkdir(parents=True, exist_ok=True)
    path = directory / "checkpoint.json"
    temporary = directory / "tmp" / "checkpoint.json.tmp"
    temporary.write_text(
        json.dumps(_checkpoint_payload(state), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def load_agent_checkpoint(path: Path | str) -> dict[str, Any]:
    """Load an audit checkpoint for inspection or an adapter-managed resume."""
    checkpoint_path = Path(path)
    if checkpoint_path.is_dir():
        checkpoint_path = checkpoint_path / "checkpoint.json"
    # Keep compatibility with the first implementation, which wrote
    # data/runs/agent/<run_id>.json directly under the base directory.
    return json.loads(checkpoint_path.read_text(encoding="utf-8"))


def _emit(state: AgentState, progress: AgentProgress | None, node: str, status: str, detail: str = "") -> None:
    event_id = int(state.get("next_event_id", 0)) + 1
    safe_detail = _redact_text(detail)
    event = {"id": event_id, "node": node, "status": status, "detail": safe_detail, "at": time.time()}
    state["next_event_id"] = event_id
    state.setdefault("events", []).append(event)
    state["last_node"] = node
    log_path = str(state.get("event_log_path", "")).strip()
    if log_path:
        try:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(_safe_value(event), ensure_ascii=False) + "\n")
        except OSError:
            # The checkpoint remains authoritative if telemetry storage is
            # temporarily unavailable; never fail an editorial job only for a
            # progress-log write.
            pass
    if progress:
        progress(node, status, safe_detail)


def _revalidate_completed_jobs(
    state: dict[str, Any], tools: EditorialAgentTools, progress: AgentProgress | None,
) -> dict[str, Any]:
    """Invalidate only failed completed jobs; never trust serialized post copies."""
    if tools.revalidate_completed is None:
        return state
    state = dict(state)
    records = {key: dict(value) for key, value in (state.get("job_states") or {}).items()}
    completed = set(state.get("completed_job_indices") or [])
    completed.update(int(key) for key, record in records.items() if record.get("status") == "completed")
    state["job_states"] = records
    state["errors"] = list(state.get("errors") or [])
    state["events"] = list(state.get("events") or [])
    jobs = state.get("jobs") or []
    for index in sorted(completed.copy()):
        record = records.setdefault(str(index), {})
        ids = list(record.get("post_ids") or _post_ids(record.get("posts") or []))
        blocked = False
        try:
            if not ids or tools.load_posts is None:
                raise ValueError("completed job has no loadable artifact references")
            posts = list(tools.load_posts(ids) or [])
            if _post_ids(posts) != ids:
                raise ValueError("retained post files missing or mismatched")
        except Exception as exc:
            issues = [_redact_text(f"RETAINED_POST_UNAVAILABLE: job={index}: {exc}")]
            blocked = True
        else:
            record["posts"] = posts
            record["post_ids"] = ids
            try:
                job = _job_from_dict(jobs[index])
                context = dict(record.get("context") or {})
                context.update(agent_run_id=state["run_id"], agent_job_key=str(index),
                               agent_target_count=job.count, agent_approved_post_ids=[])
                issues = tools.revalidate_completed(job, posts, context)
                if not isinstance(issues, list) or any(not isinstance(issue, str) or not issue.strip() for issue in issues):
                    raise TypeError("revalidate_completed must return list[str] (empty means valid)")
                issues = [_redact_text(issue) for issue in issues]
            except Exception as exc:
                issues = [_redact_text(f"COMPLETED_REVALIDATION_UNAVAILABLE: job={index}: {exc}")]
                blocked = True
        if not issues:
            continue
        if any(code in error for error in issues for code in TERMINAL_PLATFORM_FAILURE_CODES):
            state["platform_paused"] = True
            blocked = True
        if any(marker.lower() in error.lower() for error in issues for marker in TERMINAL_PROVIDER_FAILURE_MARKERS):
            state["provider_paused"] = True
            blocked = True
        completed.discard(index)
        record.update(status="blocked" if blocked else "pending", review_complete=False,
                      reviewed_posts=[], reviewed_post_ids=[], approved_versions={},
                      job_blocked=blocked, retryable=not blocked, no_progress_count=0, retry_at=0,
                      last_failure="; ".join(issues[:3]))
        if index == int(state.get("job_index", 0)):
            for key in ("posts", "post_ids", "reviewed_posts", "reviewed_post_ids", "approved_versions",
                        "review_complete", "job_blocked", "retryable", "last_failure"):
                if key in record:
                    state[key] = record[key]
        state["errors"].extend(f"COMPLETED_REVALIDATION_FAILED job={index}: {issue}" for issue in issues)
        last_node = state.get("last_node", "")
        _emit(state, progress, "revalidate_completed", "blocked" if blocked else "invalidated",
              f"job={index} retained={len(ids)}; {record['last_failure']}")
        state["last_node"] = last_node
    state["completed_job_indices"] = sorted(completed)
    state["failed_jobs"] = [int(key) for key, record in records.items() if record.get("status") == "blocked"]
    return state


def _build_graph(
    *,
    config: EditorialAgentConfig,
    tools: EditorialAgentTools,
    progress: AgentProgress | None,
    checkpointer: Any = None,
):
    retained_fields = (
        "posts", "post_ids", "reviewed_posts", "reviewed_post_ids", "approved_versions",
        "context", "last_failure", "retryable", "review_complete", "job_blocked",
    )

    def stash_job(state: AgentState) -> None:
        index = int(state.get("job_index", 0))
        if not state.get("current_job") or index >= len(state.get("jobs", [])):
            return
        record = state.setdefault("job_states", {}).setdefault(str(index), {})
        record.update({key: state.get(key) for key in retained_fields})

    def persist(state: AgentState, node: str) -> AgentState:
        state["steps"] = int(state.get("steps", 0)) + 1
        state["last_node"] = node
        # The append-only event log preserves the full history without copying
        # days of retry telemetry into every PostgreSQL graph checkpoint.
        state["events"] = list(state.get("events") or [])[-200:]
        state["errors"] = list(state.get("errors") or [])[-200:]
        stash_job(state)
        _save_checkpoint(state, config.checkpoint_dir)
        return state

    def tool_context(state: AgentState) -> dict[str, Any]:
        context = dict(state.get("context") or {})
        context.update({
            "agent_run_id": state["run_id"],
            "agent_job_key": str(state["job_index"]),
            "agent_target_count": int(state["current_job"]["count"]),
            "agent_approved_post_ids": list(state.get("reviewed_post_ids") or []),
        })
        state["context"] = context
        return context

    def provider_failure(state: AgentState, errors: list[str]) -> None:
        if any(marker.lower() in error.lower() for error in errors for marker in TERMINAL_PROVIDER_FAILURE_MARKERS):
            state["provider_paused"] = True
            _emit(state, progress, "provider_pause", "warning", "供应商额度耗尽或认证不可用，保留全部进度等待恢复")

    def restore_posts(state: AgentState) -> bool:
        ids = list(state.get("post_ids") or [])
        if not ids or (state.get("posts") and _post_ids(state["posts"]) == ids):
            return True
        try:
            posts = list(tools.load_posts(ids) or []) if tools.load_posts else []
            if _post_ids(posts) != ids:
                raise ValueError("retained post files missing or mismatched")
            state["posts"] = posts
            approved = set(state.get("reviewed_post_ids") or [])
            state["reviewed_posts"] = [post for post in posts if _post_id(post) in approved]
            return True
        except Exception as exc:
            state["job_blocked"] = True
            state["retryable"] = False
            state["last_failure"] = _redact_text(f"RETAINED_POST_UNAVAILABLE: {exc}")
            state.setdefault("errors", []).append(state["last_failure"])
            return False

    def plan(state: AgentState) -> AgentState:
        jobs = [_job_from_dict(item).__dict__ for item in state.get("jobs", [])]
        if not jobs:
            raise ValueError("editorial agent has no jobs")
        decision: dict[str, Any] = {}
        if tools.plan is not None and not state.get("controller_decision"):
            try:
                decision = _safe_value(tools.plan([_job_from_dict(item) for item in jobs], state.get("context", {}))) or {}
                if not isinstance(decision, dict):
                    raise ValueError("controller plan must be an object")
                requested_order = decision.get("job_order")
                if isinstance(requested_order, list):
                    ordered: list[dict[str, Any]] = []
                    used: set[int] = set()
                    for raw_index in requested_order:
                        try:
                            index = int(raw_index)
                        except (TypeError, ValueError):
                            continue
                        if 0 <= index < len(jobs) and index not in used:
                            ordered.append(jobs[index])
                            used.add(index)
                    jobs = ordered + [job for index, job in enumerate(jobs) if index not in used]
                state["controller_decision"] = decision
            except Exception as exc:
                state["controller_decision"] = {"status": "fallback", "error": str(exc)}
                state.setdefault("errors", []).append(_redact_text(f"controller_plan_warning: {exc}"))
        index = min(max(0, int(state.get("job_index", 0))), len(jobs))
        state.update({"jobs": jobs, "job_index": index, "status": "running", "plan_complete": True})
        summary = str(state.get("controller_decision", {}).get("summary", "")).strip()
        _emit(state, progress, "plan", "success", f"jobs={len(jobs)} index={index} provider={config.provider}" + (f" summary={summary}" if summary else ""))
        return persist(state, "plan")

    def sync_context(state: AgentState) -> AgentState:
        if int(state.get("job_index", 0)) >= len(state["jobs"]):
            return persist(state, "sync_context")
        job = _job_from_dict(state["jobs"][state["job_index"]])
        state["current_job"] = job.__dict__
        delay = float(state.pop("retry_delay_s", 0) or 0)
        if delay > 0:
            _emit(state, progress, "recover", "waiting", f"{job.kind} retry in {delay:.1f}s")
            time.sleep(min(60.0, delay))
        state["sync_failed"] = False
        try:
            context = tools.sync_context(job) or {}
        except Exception as exc:
            state["last_failure"] = _redact_text(f"sync_error: {exc}")
            state.setdefault("errors", []).append(state["last_failure"])
            state["sync_failed"] = True
            state["job_blocked"] = isinstance(exc, (ValueError, TypeError, KeyError, PermissionError))
            provider_failure(state, [state["last_failure"]])
            return persist(state, "sync_context")
        if state.get("conversation_memory"):
            context["conversation_memory"] = state["conversation_memory"]
        state["context"] = context
        tool_context(state)
        if context.get("platform_write_ready") is True:
            state["platform_paused"] = False
        if "knowledge_status" in context and context.get("knowledge_status") != "ready":
            code = str(context.get("error_code") or "KNOWLEDGE_DB_UNAVAILABLE")
            warning = str(context.get("knowledge_warning") or "PostgreSQL 知识库未就绪，已阻止生成。")
            state["status"] = "blocked"
            state["last_failure"] = f"{code}: {warning}"
            state.setdefault("errors", []).append(state["last_failure"])
            _emit(state, progress, "sync_context", "blocked", state["last_failure"])
            return persist(state, "sync_context")
        _emit(state, progress, "sync_context", "success", job.kind)
        return persist(state, "sync_context")

    def generate(state: AgentState) -> AgentState:
        job = _job_from_dict(state["current_job"])
        if not restore_posts(state):
            return persist(state, "generate")
        if state.get("posts"):
            _emit(state, progress, "generate", "resumed", f"{job.kind} reused={len(state['posts'])}")
            return persist(state, "generate")
        key = str(state["job_index"])
        attempts = dict(state.get("attempts", {}))
        attempts[key] = int(attempts.get(key, 0)) + 1
        state["attempts"] = attempts
        try:
            posts = list(tools.generate(job, tool_context(state)) or [])
            state["posts"] = posts
            state["post_ids"] = _post_ids(posts)
            state["last_failure"] = ""
            _emit(state, progress, "generate", "success", f"{job.kind} posts={len(posts)} attempt={attempts[key]}")
        except Exception as exc:
            state["last_failure"] = _redact_text(f"generation_error: {exc}")
            state.setdefault("errors", []).append(state["last_failure"])
            provider_failure(state, [state["last_failure"]])
            _emit(state, progress, "generate", "failed", state["last_failure"])
        return persist(state, "generate")

    def review(state: AgentState) -> AgentState:
        job = _job_from_dict(state["current_job"])
        if not restore_posts(state):
            return persist(state, "review")
        posts = list(state.get("posts") or [])
        previous = dict(state.get("approved_versions") or {})
        approved = {
            _post_id(post): post for post in posts
            if _post_id(post) in previous and previous[_post_id(post)] == _content_version(post)
        }
        state["reviewed_post_ids"] = list(approved)
        report: dict[str, Any] = {}
        try:
            raw = tools.review(job, posts, tool_context(state))
            if isinstance(raw, dict):
                report = raw
                errors = list(raw.get("errors") or [])
                by_id = {_post_id(post): post for post in posts}
                if "approved_post_ids" in raw:
                    approved = {}
                for post_id in raw.get("approved_post_ids") or []:
                    if post_id not in by_id:
                        errors.append(f"INVALID_APPROVAL_ID: {post_id}")
                    else:
                        approved[post_id] = by_id[post_id]
                for post_id in raw.get("rejected_post_ids") or []:
                    approved.pop(post_id, None)
            else:
                errors = list(raw or [])
                if not errors:
                    approved = {_post_id(post): post for post in posts}
        except Exception as exc:
            errors = [f"review_error: {exc}"]
        # Tools can append replacements before returning or raising. Persist
        # those artifacts even when the batch is still short of its target.
        retained = {_post_id(post): post for post in state.get("posts") or []}
        retained.update({_post_id(post): post for post in posts})
        state["posts"] = list(retained.values())
        state["post_ids"] = list(retained)
        state["reviewed_posts"] = list(approved.values())
        state["reviewed_post_ids"] = _post_ids(state["reviewed_posts"])
        state["approved_versions"] = {key: _content_version(post) for key, post in approved.items()}
        state["retryable"] = bool(report.get("retryable", True))
        state["retry_delay_s"] = max(0.0, min(60.0, float(report.get("retry_after_s") or 0)))
        if len(approved) < job.count:
            errors.append(f"TARGET_DEFICIT: approved={len(approved)}/{job.count}")
        state["review_complete"] = not errors and len(approved) >= job.count
        if errors:
            safe_errors = [_redact_text(error) for error in errors]
            state["last_failure"] = "; ".join(safe_errors[:3])
            state.setdefault("errors", []).extend(safe_errors)
            provider_failure(state, safe_errors)
            _emit(state, progress, "review", "failed", state["last_failure"])
        else:
            state["last_failure"] = ""
            _emit(state, progress, "review", "success", f"posts={len(posts)}")
        return persist(state, "review")

    def upload(state: AgentState) -> AgentState:
        job = _job_from_dict(state["current_job"])
        tool_context(state)
        uploaded = list(state.get("uploaded_posts", []))
        uploaded_ids = list(state.get("uploaded_post_ids") or _post_ids(uploaded))
        if uploaded_ids and tools.load_posts is not None and not uploaded:
            try:
                uploaded = list(tools.load_posts(uploaded_ids) or [])
            except Exception as exc:
                _emit(state, progress, "upload", "warning", f"恢复已保存稿件失败：{exc}")
        item_status = dict(state.get("item_status") or {})
        failures: list[str] = []
        if state.get("platform_paused"):
            for post in state.get("reviewed_posts", []):
                item_key = f"{state.get('job_index', 0)}:{_post_id(post)}:{_content_version(post)}"
                item_status[item_key] = "skipped_platform_paused"
            _emit(state, progress, "upload", "skipped", f"{job.kind} platform_paused")
            state["item_status"] = item_status
            return persist(state, "upload")
        pending_posts: list[Any] = []
        for post in state.get("reviewed_posts", []):
            post_id = _post_id(post)
            item_key = f"{state.get('job_index', 0)}:{post_id}:{_content_version(post)}"
            # IDs and reloaded post objects cannot prove which version was delivered.
            current_status = item_status.get(item_key)
            if current_status == "saved" or (current_status == "skipped_local" and not tools.upload_enabled):
                _emit(state, progress, "upload", "skipped", f"{job.kind} post={post_id} already_complete")
                continue
            if not tools.upload_enabled:
                item_status[item_key] = "skipped_local"
                _emit(state, progress, "upload", "skipped", f"{job.kind} post={post_id} local_only")
                continue
            pending_posts.append(post)

        if tools.upload_batch is not None and pending_posts:
            _emit(state, progress, "upload_batch", "in_progress", f"{job.kind} posts={len(pending_posts)}")
            outcomes: dict[str, tuple[bool, str]] = {}
            try:
                raw_outcomes = tools.upload_batch(job, pending_posts, state.get("context", {}))
                if not isinstance(raw_outcomes, dict):
                    raise TypeError("batch upload adapter must return a mapping keyed by post id")
                for post in pending_posts:
                    value = raw_outcomes.get(_post_id(post))
                    if isinstance(value, (tuple, list)) and len(value) >= 2:
                        outcomes[_post_id(post)] = (bool(value[0]), str(value[1] or ""))
                    else:
                        outcomes[_post_id(post)] = (
                            False,
                            "batch upload adapter returned no result for post_id=" + _post_id(post),
                        )
            except Exception as exc:
                detail = f"upload_batch_error: {exc}"
                outcomes = {_post_id(post): (False, detail) for post in pending_posts}

            for index, post in enumerate(pending_posts):
                post_id = _post_id(post)
                item_key = f"{state.get('job_index', 0)}:{post_id}:{_content_version(post)}"
                ok, detail = outcomes.get(post_id, (False, f"batch upload result missing for post_id={post_id}"))
                if ok:
                    uploaded.append(post)
                    uploaded_ids.append(post_id)
                    item_status[item_key] = "saved"
                    _emit(state, progress, "upload", "success", f"{job.kind} post={post_id} {detail}")
                    continue
                item_status[item_key] = "uncertain" if "uncertain" in str(detail).lower() else "failed"
                failures.append(f"upload_error: {detail or f'upload failed post={post_id}'}")
                _emit(state, progress, "upload", "failed", failures[-1])
                if any(code in str(detail) for code in TERMINAL_PLATFORM_FAILURE_CODES):
                    state["platform_paused"] = True
                    for remaining in pending_posts[index + 1:]:
                        remaining_id = _post_id(remaining)
                        remaining_key = f"{state.get('job_index', 0)}:{remaining_id}:{_content_version(remaining)}"
                        item_status[remaining_key] = "skipped_platform_paused"
                    break

            state["uploaded_posts"] = uploaded
            state["uploaded_post_ids"] = list(dict.fromkeys(uploaded_ids))
            state["item_status"] = item_status
            failures = [_redact_text(error) for error in failures]
            state["last_failure"] = "; ".join(failures[:3])
            if failures:
                state.setdefault("errors", []).extend(failures)
            _emit(state, progress, "upload_batch", "failed" if failures else "success", f"{job.kind} uploaded={len(uploaded)}")
            return persist(state, "upload")

        # The fallback loop is deliberately serial for adapters that have not
        # opted into the batch contract. Platform locks and idempotency remain
        # owned by the adapter.
        for post in pending_posts:
            post_id = _post_id(post)
            item_key = f"{state.get('job_index', 0)}:{post_id}:{_content_version(post)}"
            try:
                ok, detail = tools.upload(job, post, state.get("context", {}))
            except Exception as exc:
                ok, detail = False, f"upload_error: {exc}"
            if ok:
                uploaded.append(post)
                uploaded_ids.append(post_id)
                item_status[item_key] = "saved"
                _emit(state, progress, "upload", "success", f"{job.kind} post={post_id} {detail}")
            else:
                item_status[item_key] = "uncertain" if "uncertain" in str(detail).lower() else "failed"
                failures.append(f"upload_error: {detail or f'upload failed post={post_id}'}")
                _emit(state, progress, "upload", "failed", failures[-1])
                # A platform write may already have had an external side
                # effect. Stop this serial batch immediately instead of
                # submitting another post or retrying the uncertain action.
                if any(code in str(detail) for code in TERMINAL_PLATFORM_FAILURE_CODES):
                    state["platform_paused"] = True
                    break
        state["uploaded_posts"] = uploaded
        state["uploaded_post_ids"] = list(dict.fromkeys(uploaded_ids))
        state["item_status"] = item_status
        failures = [_redact_text(error) for error in failures]
        state["last_failure"] = "; ".join(failures[:3])
        if failures:
            state.setdefault("errors", []).extend(failures)
        return persist(state, "upload")

    def recover(state: AgentState) -> AgentState:
        job = _job_from_dict(state["current_job"])
        key = str(state["job_index"])
        recoveries = dict(state.get("recovery_attempts", {}))
        recoveries[key] = int(recoveries.get(key, 0)) + 1
        state["recovery_attempts"] = recoveries
        record = state.setdefault("job_states", {}).setdefault(key, {})
        # New rejected IDs alone are not success: use accepted outputs and
        # successful external writes as the monotonic progress signal.
        fingerprint = (
            len(state.get("reviewed_post_ids") or []),
            len(state.get("uploaded_post_ids") or []),
        )
        previous = tuple(record.get("progress_fingerprint") or (0, 0))
        stalls = 0 if fingerprint > previous else int(record.get("no_progress_count") or 0) + 1
        record["progress_fingerprint"] = list(fingerprint)
        record["no_progress_count"] = stalls
        stall_limit_reached = config.no_progress_limit > 0 and stalls >= config.no_progress_limit
        if not state.get("retryable", True) or state.get("job_blocked") or stall_limit_reached:
            state["job_blocked"] = True
            record["status"] = "blocked"
            reason = state.get("last_failure") or "tool returned no actionable result"
            if stall_limit_reached:
                reason = f"NO_PROGRESS: {job.kind} repeated {stalls} rounds without an accepted artifact; {reason}"
            state["last_failure"] = reason
            state.setdefault("errors", []).append(reason)
            _emit(state, progress, "recover", "blocked", reason)
        else:
            delay = max(float(state.get("retry_delay_s") or 0), min(60.0, config.retry_delay_s * (2 ** min(stalls - 1, 5))) if stalls else 0)
            record["retry_at"] = time.time() + delay
            record["status"] = "pending"
            _emit(state, progress, "recover", "retry", f"{job.kind} recovery={recoveries[key]} retained={len(state.get('post_ids') or [])} approved={fingerprint[0]}/{job.count} wait={delay:.1f}s")
        return persist(state, "recover")

    def next_job(state: AgentState) -> AgentState:
        index = int(state.get("job_index", 0))
        stash_job(state)
        records = state.setdefault("job_states", {})
        record = records.setdefault(str(index), {})
        if state.get("platform_paused") and state.get("review_complete"):
            record["status"] = "blocked"
            record["last_failure"] = state.get("last_failure") or "XHS_PLATFORM_PAUSED: reviewed locally; delivery pending"
        elif state.get("review_complete") and not state.get("last_failure"):
            record["status"] = "completed"
            state["completed_job_indices"] = sorted(set(state.get("completed_job_indices") or []) | {index})
        elif state.get("job_blocked"):
            record["status"] = "blocked"
        else:
            record["status"] = "pending"
        size = len(state.get("jobs") or [])
        candidates = [
            (index + offset) % size for offset in range(1, size + 1)
            if records.get(str((index + offset) % size), {}).get("status") not in {"completed", "blocked"}
        ]
        state["failed_jobs"] = [int(key) for key, value in records.items() if value.get("status") == "blocked"]
        if not candidates:
            state["job_index"] = size
            state["current_job"] = {}
            return persist(state, "next_job")
        ready = [key for key in candidates if float(records.get(str(key), {}).get("retry_at") or 0) <= time.time()]
        chosen = ready[0] if ready else min(candidates, key=lambda key: float(records.get(str(key), {}).get("retry_at") or 0))
        state["job_index"] = chosen
        selected = records.get(str(chosen), {})
        # LangGraph merges returned channel updates; omitted keys would keep
        # the previous job's values and leak its drafts into the next job.
        state.update({
            "posts": [], "post_ids": [], "reviewed_posts": [], "reviewed_post_ids": [],
            "approved_versions": {}, "context": {}, "last_failure": "",
            "retryable": True, "review_complete": False, "job_blocked": False,
        })
        state.update({key: value for key, value in selected.items() if key in retained_fields and value is not None})
        state["current_job"] = state["jobs"][chosen]
        state["retry_delay_s"] = min(60.0, max(0.0, float(selected.get("retry_at") or 0) - time.time()))
        return persist(state, "next_job")

    def finish(state: AgentState) -> AgentState:
        requested = len(state.get("jobs", []))
        completed = len(state.get("completed_job_indices") or []) >= requested
        if state.get("platform_paused"):
            # A platform pause after a real submission attempt is a partial
            # run even when the remaining jobs were only generated locally.
            state["status"] = "partial"
        elif state.get("provider_paused"):
            state["status"] = "partial" if state.get("uploaded_posts") else "blocked"
        elif state.get("status") == "blocked":
            state["status"] = "blocked"
        else:
            state["status"] = "completed" if completed and not state.get("failed_jobs") else (
            "partial" if state.get("uploaded_posts") or state.get("completed_job_indices") else "blocked"
            )
        _emit(state, progress, "finish", state["status"], f"uploaded={len(state.get('uploaded_posts', []))}")
        return persist(state, "finish")

    def after_review(state: AgentState) -> str:
        if state.get("provider_paused"):
            return "finish"
        if state.get("review_complete"):
            return "upload"
        return "recover"

    def after_generate(state: AgentState) -> str:
        if state.get("provider_paused"):
            return "finish"
        return "recover" if state.get("job_blocked") or not state.get("posts") else "review"

    def after_sync_context(state: AgentState) -> str:
        if state.get("status") == "blocked" or int(state.get("job_index", 0)) >= len(state.get("jobs", [])):
            return "finish"
        if state.get("provider_paused"):
            return "finish"
        if state.get("job_blocked") or state.get("sync_failed"):
            return "recover"
        if not state.get("plan_complete"):
            return "plan"
        if state.get("review_complete") and state.get("reviewed_posts"):
            return "upload"
        return "review" if state.get("posts") else "generate"

    def after_upload(state: AgentState) -> str:
        last_failure = str(state.get("last_failure") or "")
        # A platform write can have an external side effect even when the
        # browser connection fails. Never retry a risk pause, challenge, login
        # failure, or uncertain submit from the generic recovery branch.
        if any(code in last_failure for code in TERMINAL_PLATFORM_FAILURE_CODES):
            return "next_job"
        if not state.get("last_failure"):
            return "next_job"
        return "recover"

    def after_recover(state: AgentState) -> str:
        if state.get("provider_paused"):
            return "finish"
        return "next_job"

    def after_next(state: AgentState) -> str:
        return "finish" if int(state.get("job_index", 0)) >= len(state.get("jobs", [])) else "sync_context"

    graph = StateGraph(AgentState)
    graph.add_node("plan", plan)
    graph.add_node("sync_context", sync_context)
    graph.add_node("generate", generate)
    graph.add_node("review", review)
    graph.add_node("upload", upload)
    graph.add_node("recover", recover)
    graph.add_node("next_job", next_job)
    graph.add_node("finish", finish)
    graph.add_edge(START, "sync_context")
    graph.add_edge("plan", "sync_context")
    graph.add_conditional_edges("sync_context", after_sync_context)
    graph.add_conditional_edges("generate", after_generate)
    graph.add_conditional_edges("review", after_review)
    graph.add_conditional_edges("upload", after_upload)
    graph.add_conditional_edges("recover", after_recover)
    graph.add_conditional_edges("next_job", after_next)
    graph.add_edge("finish", END)
    # End each scheduling round at a durable boundary. The driver resumes the
    # same pending node in a fresh invocation, so retries cannot exhaust the
    # LangGraph recursion limit and no already-run node is replayed.
    return graph.compile(checkpointer=checkpointer, interrupt_after=["next_job"])


def run_editorial_agent(
    jobs: list[AgentJob],
    *,
    tools: EditorialAgentTools,
    config: EditorialAgentConfig | None = None,
    progress: AgentProgress | None = None,
    run_id: str | None = None,
) -> AgentRunResult:
    """Run durable scheduling rounds until targets complete or work is blocked."""
    cfg = (config or EditorialAgentConfig()).validate()
    normalized_jobs = [job.normalized() for job in jobs]
    postgres_resume = cfg.checkpoint_backend == "postgres" and cfg.resume_from is not None
    resume_checkpoint: dict[str, Any] = {}
    if cfg.resume_from:
        try:
            resume_checkpoint = load_agent_checkpoint(cfg.resume_from)
        except (OSError, ValueError):
            if not postgres_resume:
                raise
    resume_identifier = ""
    if postgres_resume:
        pointer = Path(cfg.resume_from)
        resume_identifier = pointer.parent.name if pointer.name == "checkpoint.json" else pointer.stem if pointer.suffix == ".json" else pointer.name
    if not normalized_jobs and not postgres_resume:
        raise ValueError("at least one editorial job is required")
    original_thread = resume_checkpoint.get("original_thread") if postgres_resume else None
    identifier = str(original_thread or resume_checkpoint.get("run_id") or resume_identifier or run_id or uuid4().hex)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
        raise ValueError("智能体运行编号无效")
    if cfg.checkpoint_backend == "postgres":
        from src.agent.artifact_store import AgentArtifactStore
        run_lease = AgentArtifactStore().lease(identifier)
    else:
        run_lease = nullcontext(lambda: None)
    with run_lease as assert_lease_alive:
        assert_lease_alive()
        run_directory = Path(cfg.checkpoint_dir) / identifier
        cfg = replace(cfg, checkpoint_dir=run_directory)
        run_directory.mkdir(parents=True, exist_ok=True)
        (run_directory / "tmp").mkdir(parents=True, exist_ok=True)
        initial: AgentState = {
            "run_id": identifier,
            "jobs": [job.__dict__ for job in normalized_jobs],
            "job_index": 0,
            "attempts": {},
            "context": {},
            "conversation_memory": dict(cfg.conversation_context),
            "controller_decision": {},
            "plan_complete": False,
            "posts": [],
            "post_ids": [],
            "reviewed_posts": [],
            "reviewed_post_ids": [],
            "uploaded_posts": [],
            "uploaded_post_ids": [],
            "item_status": {},
            "errors": [],
            "events": [],
            "status": "running",
            "failed_jobs": [],
            "recovery_attempts": {},
            "event_log_path": str(run_directory / "events.jsonl"),
            "next_event_id": 0,
            "started_at": time.time(),
            "root_started_at": time.time(),
            "resume_count": 0,
            "budget_exceeded": False,
            "platform_paused": False,
            "provider_paused": False,
            "steps": 0,
            "job_states": {},
            "completed_job_indices": [],
            "approved_versions": {},
            "retryable": True,
            "review_complete": False,
            "job_blocked": False,
        }
        if resume_checkpoint and not postgres_resume:
            checkpoint = resume_checkpoint
            if checkpoint.get("jobs"):
                initial.update({
                    "run_id": str(checkpoint.get("run_id") or identifier),
                    "jobs": checkpoint["jobs"],
                    "job_index": int(checkpoint.get("job_index", 0)),
                    "attempts": dict(checkpoint.get("attempts") or {}),
                    "controller_decision": dict(checkpoint.get("controller_decision") or {}),
                    "plan_complete": bool(checkpoint.get("plan_complete", bool(checkpoint.get("controller_decision")))),
                    "post_ids": list(checkpoint.get("post_ids") or []),
                    "reviewed_post_ids": list(checkpoint.get("reviewed_post_ids") or []),
                    "uploaded_post_ids": list(checkpoint.get("uploaded_post_ids") or []),
                    "item_status": dict(checkpoint.get("item_status") or {}),
                    "errors": list(checkpoint.get("errors") or []),
                    "failed_jobs": list(checkpoint.get("failed_jobs") or []),
                    "recovery_attempts": dict(checkpoint.get("recovery_attempts") or {}),
                    "event_log_path": str(checkpoint.get("event_log_path") or run_directory / "events.jsonl"),
                    "next_event_id": int(checkpoint.get("next_event_id", 0)),
                    # Keep attempt and root clocks for timing reports only.
                    "started_at": time.time(),
                    "root_started_at": float(checkpoint.get("root_started_at") or checkpoint.get("started_at") or time.time()),
                    "resume_count": int(checkpoint.get("resume_count", 0)) + 1,
                    "budget_exceeded": False,
                    "platform_paused": bool(checkpoint.get("platform_paused", False)),
                    "provider_paused": bool(checkpoint.get("provider_paused", False)),
                    "events": list(checkpoint.get("events") or []),
                    "steps": 0,
                    "last_failure": "" if checkpoint.get("budget_exceeded") else str(checkpoint.get("last_failure") or ""),
                    "job_states": dict(checkpoint.get("job_states") or {}),
                    "completed_job_indices": list(checkpoint["completed_job_indices"] if "completed_job_indices" in checkpoint else [
                        index for index in range(int(checkpoint.get("job_index", 0)))
                        if index not in checkpoint.get("failed_jobs", [])
                    ]),
                    "approved_versions": dict(checkpoint.get("approved_versions") or {}),
                    "review_complete": bool(checkpoint.get("review_complete", False)),
                })

        def reopen_retained(state: dict[str, Any]) -> dict[str, Any]:
            state = dict(state)
            state.update({"status": "running", "budget_exceeded": False, "provider_paused": False,
                          "retryable": True, "job_blocked": False})
            records = {key: dict(record) for key, record in (state.get("job_states") or {}).items()}
            for record in records.values():
                if record.get("status") == "blocked":
                    record.update(status="pending", no_progress_count=0, retry_at=0, job_blocked=False, retryable=True)
            state["job_states"] = records
            state["failed_jobs"] = []
            assert_lease_alive()
            state = _revalidate_completed_jobs(state, tools, progress)
            assert_lease_alive()
            return activate_retained(state)

        def activate_retained(state: dict[str, Any]) -> dict[str, Any]:
            state = dict(state)
            records = state.get("job_states") or {}
            if int(state.get("job_index", 0)) >= len(state.get("jobs") or []):
                pending = sorted(int(key) for key, record in records.items() if record.get("status") != "completed")
                if pending:
                    state["job_index"] = pending[0]
                    record = records[str(pending[0])]
                    for key in ("posts", "post_ids", "reviewed_posts", "reviewed_post_ids", "approved_versions", "context", "review_complete"):
                        state[key] = record.get(key) or (False if key == "review_complete" else {} if key in {"context", "approved_versions"} else [])
                    state["job_blocked"] = bool(record.get("job_blocked", False))
                    state["retryable"] = bool(record.get("retryable", True))
                    state["last_failure"] = str(record.get("last_failure") or "")
            return state

        if resume_checkpoint and not postgres_resume and initial.get("status") != "completed":
            initial = reopen_retained(initial)
        graph_config = {
            "recursion_limit": cfg.max_steps + 4,
            "configurable": {"thread_id": identifier},
        }

        def drive(graph: Any, value: Any) -> dict[str, Any]:
            def invoke(next_value: Any) -> dict[str, Any]:
                assert_lease_alive()
                result = graph.invoke(next_value, graph_config)
                assert_lease_alive()
                return result

            final = invoke(value)
            while True:
                # A resumed pending finish node must run as checkpointed. If the
                # completed-job audit queued work, schedule it after that boundary.
                if (final.get("last_node") == "finish" and final.get("status") != "blocked"
                        and not final.get("platform_paused") and not final.get("provider_paused")
                        and any(record.get("status") == "pending" for record in (final.get("job_states") or {}).values())):
                    final = invoke(activate_retained(dict(final, status="running")))
                    continue
                if graph.checkpointer is None:
                    if final.get("last_node") != "next_job":
                        return final
                    final = invoke(final)
                    continue
                snapshot = graph.get_state(graph_config)
                if not snapshot.next or final.get("last_node") != "next_job":
                    return final
                final = invoke(None)

        try:
            if cfg.checkpoint_backend == "postgres":
                from src.agent.postgres_checkpoint import postgres_checkpointer
                with postgres_checkpointer() as checkpointer:
                    assert_lease_alive()
                    graph = _build_graph(config=cfg, tools=tools, progress=progress, checkpointer=checkpointer)
                    if postgres_resume:
                        persisted = graph.get_state(graph_config)
                        assert_lease_alive()
                        if not persisted or not persisted.values:
                            raise RuntimeError("POSTGRES_CHECKPOINT_NOT_FOUND: no durable state exists for this run id")
                        if str(persisted.values.get("run_id") or identifier) != identifier:
                            raise RuntimeError("AGENT_RUN_ID_MISMATCH: durable state does not match locked run")
                        if persisted.next:
                            # Patch channels, not the pending node. An explicit
                            # resume may retry a prior provider pause, but a fresh
                            # quota/auth failure from revalidation must still stop.
                            assert_lease_alive()
                            previous = _revalidate_completed_jobs(
                                dict(persisted.values, provider_paused=False), tools, progress,
                            )
                            assert_lease_alive()
                            update = dict(previous, **{
                                "started_at": time.time(),
                                "root_started_at": float(previous.get("root_started_at") or previous.get("started_at") or time.time()),
                                "resume_count": int(previous.get("resume_count", 0)) + 1,
                                "budget_exceeded": False,
                                "steps": 0,
                                "status": "running",
                                "provider_paused": bool(previous.get("provider_paused")),
                            })
                            graph.update_state(graph_config, update)
                            final = drive(graph, None)
                        else:
                            resumed = reopen_retained(persisted.values)
                            resumed.update({
                                "started_at": time.time(),
                                "root_started_at": float(resumed.get("root_started_at") or resumed.get("started_at") or time.time()),
                                "resume_count": int(resumed.get("resume_count", 0)) + 1,
                                "budget_exceeded": False,
                                "steps": 0,
                                "status": "running",
                            })
                            if (persisted.values.get("status") == "completed"
                                    and len(resumed.get("completed_job_indices") or []) == len(resumed.get("jobs") or [])):
                                resumed["status"] = "completed"
                                graph.update_state(graph_config, resumed)
                                _save_checkpoint(resumed, cfg.checkpoint_dir)
                                final = resumed
                            else:
                                final = drive(graph, resumed)
                    else:
                        final = drive(graph, initial)
            else:
                graph = _build_graph(config=cfg, tools=tools, progress=progress)
                final = drive(graph, initial)
        except Exception as exc:
            initial["status"] = "failed"
            initial.setdefault("errors", []).append(_redact_text(f"agent_runtime_error: {exc}"))
            if cfg.checkpoint_backend != "postgres":
                _save_checkpoint(initial, cfg.checkpoint_dir)
            raise
        assert_lease_alive()
        processed_jobs = min(int(final.get("job_index", 0)), len(final.get("jobs") or []))
        failed_jobs = {
            int(index) for index in (final.get("failed_jobs") or [])
            if isinstance(index, int) or str(index).isdigit()
        }
        return AgentRunResult(
            run_id=str(final.get("run_id") or identifier),
            status=str(final.get("status") or "failed"),
            requested_jobs=len(final.get("jobs") or []),
            completed_jobs=len(final["completed_job_indices"]) if "completed_job_indices" in final else processed_jobs - sum(0 <= index < processed_jobs for index in failed_jobs),
            uploaded_posts=list(final.get("uploaded_posts") or []),
            errors=list(final.get("errors") or []),
            checkpoint_path=cfg.checkpoint_dir / "checkpoint.json",
            events=list(final.get("events") or []),
        )


@dataclass(frozen=True)
class AgentRunResult:
    run_id: str
    status: str
    requested_jobs: int
    completed_jobs: int
    uploaded_posts: list[Any] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    checkpoint_path: Path | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
