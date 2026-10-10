from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import wraps
import time
from typing import Callable
from uuid import uuid4

from .models import bounded_summary
from .policy import validate_tool


_ACTIVE: ContextVar = ContextVar('agent_tool_dispatcher', default=None)
_PARENT: ContextVar = ContextVar('agent_parent_call', default=None)
_RESOURCE: ContextVar = ContextVar('agent_active_resource', default=None)


def current_invocation():
    return _ACTIVE.get(), _PARENT.get(), _RESOURCE.get()


class ToolDispatcher:
    def __init__(self, store, snapshot: dict, *, origin: str = 'business', progress=None):
        self.store, self.snapshot, self.origin, self.progress = store, snapshot, origin, progress

    @contextmanager
    def bound(self):
        token = _ACTIVE.set(self)
        try:
            yield self
        finally:
            _ACTIVE.reset(token)

    def call(self, resource_id: str, callback: Callable, *, stage: str, arguments=None, operation_key: str = ''):
        start = time.monotonic()
        tools = self.snapshot['tools']
        row = tools.get(resource_id, {})
        call_id = self.store.start_call({
            'run_id': self.snapshot['run_id'], 'resource_id': resource_id,
            'resource_name': row.get('name', resource_id), 'stage': stage, 'status': 'running',
            'origin': self.origin, 'version': row.get('revision', 0), 'parent_call_id': _PARENT.get(),
            'operation_key': operation_key or uuid4().hex, 'input_summary': bounded_summary(arguments),
            'queue_ms': 0, 'connection_ms': 0, 'retries': 0,
            **({'action':'resource_read','loaded_resources':0} if row.get('kind')=='skill_resource' else {}),
        })
        parent_token = _PARENT.set(call_id)
        active_token = _ACTIVE.set(self)
        resource_token = _RESOURCE.set(resource_id)
        try:
            current = self.store.get(resource_id)
            validate_tool(tools, resource_id, stage, revoked=bool(current and current.get('revoked_at')))
            # Revocation of a nested dependency is immediate even for a frozen parent.
            for dependency in row.get('dependencies', []):
                current_dependency = self.store.get(dependency) if dependency.startswith(('builtin:', 'mcp:')) else None
                if current_dependency and current_dependency.get('revoked_at'):
                    validate_tool(tools, dependency, (tools[dependency].get('stages') or [stage])[0], revoked=True)
            if self.progress:
                self.progress('capability', 'in_progress', row.get('name', resource_id))
            result = callback()
            elapsed = (time.monotonic()-start)*1000
            usage = {key:result.get(key) for key in ('loaded_resources','loaded_characters')} if row.get('kind')=='skill_resource' else {}
            status='succeeded'
            if resource_id=='builtin:xhs.drafts.save_batch' and stage=='upload':
                outcomes=result.values() if isinstance(result,dict) else [result]
                failures=[str(outcome[1]) for outcome in outcomes
                          if isinstance(outcome,(tuple,list)) and len(outcome)>=2 and not outcome[0]]
                if failures:
                    status='uncertain' if any('UNCERTAIN' in failure.upper() for failure in failures) else 'failed'
            self.store.finish_call(call_id, status, {'result_summary': bounded_summary(result), 'execution_ms': elapsed, 'wall_ms': elapsed, **usage})
            return result
        except Exception as exc:
            code = getattr(exc, 'code', '')
            status = 'uncertain' if 'UNCERTAIN' in code or 'XHS_WRITE_UNCERTAIN' in str(exc) else 'denied' if code.startswith('CAPABILITY_') else 'timed_out' if isinstance(exc, TimeoutError) else 'failed'
            elapsed = (time.monotonic()-start)*1000
            self.store.finish_call(call_id, status, {'error': bounded_summary(str(exc)), 'execution_ms': elapsed, 'wall_ms': elapsed})
            raise
        finally:
            _PARENT.reset(parent_token)
            _ACTIVE.reset(active_token)
            _RESOURCE.reset(resource_token)


def managed_call(resource_id: str, stage: str, callback: Callable, *, arguments=None):
    current = _ACTIVE.get()
    return current.call(resource_id, callback, stage=stage, arguments=arguments) if current else callback()


def contextual_callback(callback: Callable) -> Callable:
    """Carry capability policy across explicitly submitted worker threads."""
    context = copy_context()
    return lambda *args, **kwargs: context.copy().run(callback, *args, **kwargs)


def request_timeout(default):
    current = _ACTIVE.get()
    row = current.snapshot['tools'].get(_RESOURCE.get(), {}) if current else {}
    timeout = row.get('timeout_seconds') if row.get('timeout_configurable') else None
    return float(timeout) if timeout is not None else default


def governed(resource_id: str, stage: str, *, passthrough_resources=(), refresh_context=False):
    """Enforce policy at the adapter boundary only within an agent run."""
    def decorate(callback):
        @wraps(callback)
        def call(*args, **kwargs):
            if refresh_context and _ACTIVE.get() is not None:
                from src.agent.execution_context import refresh_model_inputs
                kwargs=refresh_model_inputs(kwargs,_ACTIVE.get().store)
            if _RESOURCE.get() in {resource_id, *passthrough_resources}:
                return callback(*args, **kwargs)
            return managed_call(resource_id, stage, lambda: callback(*args, **kwargs),
                                arguments={'adapter':callback.__name__})
        return call
    return decorate
