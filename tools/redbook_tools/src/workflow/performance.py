"""Performance policies and secret-free run telemetry for workflow executions."""

from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterator
from uuid import uuid4


_PERFORMANCE_MODES = {"balanced", "speed"}
_SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|token|secret|password|authorization|credential|cookie)",
    re.IGNORECASE,
)
_TOKEN_COUNTERS = {"input_tokens", "output_tokens", "total_tokens", "cached_tokens"}


@dataclass(frozen=True)
class PerformancePolicy:
    """Runtime policy shared by discovery, model work and progress reporting."""

    mode: str = "balanced"
    llm_workers: int = 2
    minimax_llm_workers: int = 5
    image_workers: int = 2
    news_candidate_deadline_s: float = 900.0
    news_initial_context_count: int = 24
    news_low_water_mark: int = 8
    ai_initial_search_queries: int = 10
    ai_backfill_concurrency: int = 1
    wool_source_concurrency: int = 1

    @classmethod
    def from_value(cls, value: str | None) -> "PerformancePolicy":
        mode = (value or "balanced").strip().lower() or "balanced"
        if mode not in _PERFORMANCE_MODES:
            raise ValueError(
                f"performance mode must be one of {', '.join(sorted(_PERFORMANCE_MODES))}; got {mode!r}"
            )
        if mode == "speed":
            return cls(
                mode="speed",
                news_candidate_deadline_s=480.0,
                news_initial_context_count=24,
                news_low_water_mark=4,
                ai_initial_search_queries=6,
                ai_backfill_concurrency=3,
                wool_source_concurrency=3,
            )
        return cls()

    @classmethod
    def from_environment(cls) -> "PerformancePolicy":
        import os

        return cls.from_value(os.getenv("WORKFLOW_PERFORMANCE_MODE", "balanced"))

    @property
    def is_speed_first(self) -> bool:
        return self.mode == "speed"

    def llm_workers_for_provider(self, provider: str | None) -> int:
        """Select the requested lane width without widening other providers."""
        if (provider or "").strip().lower().replace("-", "_") == "minimax":
            return max(1, min(5, int(self.minimax_llm_workers)))
        return max(1, min(2, int(self.llm_workers)))


def _sanitize(value: Any, *, key: str = "") -> Any:
    if key and key not in _TOKEN_COUNTERS and _SENSITIVE_KEY_RE.search(key):
        return None
    if isinstance(value, dict):
        return {
            str(k): _sanitize(v, key=str(k))
            for k, v in value.items()
            if str(k) in _TOKEN_COUNTERS or not _SENSITIVE_KEY_RE.search(str(k))
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass
class RunContext:
    """Immutable-by-convention execution metadata with append-only telemetry."""

    run_id: str
    policy: PerformancePolicy
    started_at: str
    deadline: float
    telemetry_path: Path
    model_config: dict[str, Any] = field(default_factory=dict)
    _write_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _started_monotonic: float = field(default_factory=time.monotonic, repr=False)

    @classmethod
    def create(
        cls,
        mode: str | None = None,
        *,
        run_id: str | None = None,
        telemetry_dir: str | Path = "data/runs",
        model_config: dict[str, Any] | None = None,
    ) -> "RunContext":
        policy = PerformancePolicy.from_value(mode)
        identifier = re.sub(r"[^A-Za-z0-9_.-]", "-", run_id or uuid4().hex)
        root = Path(telemetry_dir) / identifier
        root.mkdir(parents=True, exist_ok=True)
        started = datetime.now(timezone.utc)
        context = cls(
            run_id=identifier,
            policy=policy,
            started_at=started.isoformat(),
            deadline=time.monotonic() + policy.news_candidate_deadline_s,
            telemetry_path=root / "events.jsonl",
            model_config=_sanitize(model_config or {}),
        )
        context.record(
            "run",
            "started",
            mode=policy.mode,
            model_config=context.model_config,
        )
        return context

    def record(self, stage: str, status: str, **details: Any) -> None:
        event = {
            "at": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "stage": str(stage),
            "status": str(status),
            "elapsed_s": round(max(0.0, time.monotonic() - self._started_monotonic), 3),
            "details": _sanitize(details),
        }
        with self._write_lock:
            with self.telemetry_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def deadline_remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


@contextmanager
def timed_span(context: RunContext, stage: str, **details: Any) -> Iterator[None]:
    started = time.monotonic()
    context.record(stage, "in_progress", **details)
    try:
        yield
    except Exception as exc:
        context.record(stage, "failed", elapsed_s=round(time.monotonic() - started, 3), error=str(exc))
        raise
    else:
        context.record(stage, "success", elapsed_s=round(time.monotonic() - started, 3))


@contextmanager
def stage_span(context: RunContext, stage: str, *, span_id: str, **details: Any) -> Iterator[None]:
    start = time.monotonic()
    fields = dict(details, span_id=span_id, metric_kind="stage", started_at=time.time())
    context.record(stage, "started", **fields)
    try:
        yield
    except BaseException as exc:
        context.record(stage, "failed", **fields, finished_at=time.time(),
                       elapsed_ms=(time.monotonic()-start)*1000, error_type=type(exc).__name__)
        raise
    else:
        context.record(stage, "success", **fields, finished_at=time.time(),
                       elapsed_ms=(time.monotonic()-start)*1000)


@contextmanager
def request_span(context: RunContext, stage: str, *, request_id: str,
                 attempt: int = 1, provider: str = "", model: str = "",
                 queued_at: float | None = None, **details: Any) -> Iterator[None]:
    start = time.monotonic()
    fields = dict(details, request_id=request_id, attempt=attempt, provider=provider,
                  model=model, started_at=time.time(),
                  queue_ms=max(0, start - (queued_at if queued_at is not None else start)) * 1000)
    context.record(stage, "started", **fields)
    try:
        yield fields
    except BaseException as exc:
        context.record(stage, "failed", **fields, finished_at=time.time(),
                       service_ms=(time.monotonic()-start)*1000, error_type=type(exc).__name__)
        raise
    else:
        context.record(stage, "success", **fields, finished_at=time.time(),
                       service_ms=(time.monotonic()-start)*1000)


def summarize_performance(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Union completed intervals, preserving unfinished requests across a restart."""
    requests: dict[str, dict[str, Any]] = {}
    for event in events:
        row = {**event, **(event.get("details") or {})}
        key = row.get("request_id")
        if not key:
            continue
        previous = requests.get(str(key), {})
        if previous.get("finished_at") is not None and row.get("finished_at") is None:
            continue
        requests[str(key)] = row
    completed = [row for row in requests.values() if row.get("finished_at") is not None]
    intervals = sorted((float(r["started_at"]), float(r["finished_at"])) for r in completed)
    merged: list[list[float]] = []
    for start, end in intervals:
        if end < start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    active = sum(end-start for start, end in merged)
    wall = merged[-1][1]-merged[0][0] if merged else 0.0
    return {"requests": len(requests),
            "retries": sum(r.get("retry_identity_known", True) and int(r.get("attempt", 1)) > 1 for r in requests.values()),
            "retry_identity_unknown": sum(r.get("retry_identity_known") is False for r in requests.values()),
            "unfinished_requests": len(requests)-len(completed), "active_seconds": active,
            "wall_seconds": wall, "inactive_seconds": max(0.0, wall-active),
            "service_seconds": sum(float(r.get("service_ms", 0)) for r in completed)/1000,
            "queue_seconds": sum(float(r.get("queue_ms", 0)) for r in completed)/1000,
            "clock_basis": "request_interval_union_not_whole_run"}


def iter_first_completed(
    items: list[T],
    worker: Any,
    *,
    max_workers: int,
) -> Iterator[tuple[T, Any, Exception | None]]:
    """Keep a bounded window full and yield work in completion order."""
    if not items:
        return
    limit = max(1, int(max_workers))
    executor = ThreadPoolExecutor(max_workers=limit, thread_name_prefix="speed-first")
    iterator = iter(items)
    futures: dict[Any, T] = {}

    def refill() -> None:
        while len(futures) < limit:
            try:
                item = next(iterator)
            except StopIteration:
                return
            futures[executor.submit(worker, item)] = item

    try:
        refill()
        while futures:
            done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
            for future in done:
                item = futures.pop(future)
                try:
                    yield item, future.result(), None
                except Exception as exc:
                    yield item, None, exc
                refill()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
