"""Optional adapter-level observations; never store prompts or credentials."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import time
from uuid import uuid4

from .performance import request_span
from .request_outcome import remote_outcome_unknown

_ACTIVE = ContextVar("news_model_observation", default=None)


def completed_request_count():
    active = _ACTIVE.get()
    return int(active["completed"]) if active else 0


def failure_outcome_unknown(error, *, completed_before):
    active = _ACTIVE.get()
    if active and active["unknown"]:
        return True
    if active and active["completed"] > completed_before:
        return False
    return remote_outcome_unknown(error)


@contextmanager
def model_observation(telemetry, stage, context):
    observed = {"telemetry": telemetry, "stage": stage, "context": context,
                "attempt": 0, "queued_at": time.monotonic(), "unknown": False, "completed": 0}
    token = _ACTIVE.set(observed)
    try:
        yield observed
    finally:
        _ACTIVE.reset(token)


def observe_model_call(config, callback):
    active = _ACTIVE.get()
    if not active:
        return callback()
    from src.agent.capabilities.context_usage import response_tokens
    active["attempt"] += 1
    context = active["context"]
    span = (request_span(active["telemetry"], active["stage"], request_id=uuid4().hex,
                      attempt=1, invocation_index=active["attempt"], retry_identity_known=False,
                      provider=config.provider, model=config.model,
                      queued_at=active["queued_at"] if active["attempt"] == 1 else None,
                      root_run_id=context.root_run_id, candidate_id=context.candidate_id,
                      request_unit="adapter_invocation_sdk_retries_not_visible")
            if active["telemetry"] else nullcontext({}))
    try:
        with span as fields:
            result = callback()
            fields.update(response_tokens(result) or {})
            active["completed"] += 1
            return result
    except BaseException as error:
        active["unknown"] = active["unknown"] or remote_outcome_unknown(error)
        raise
