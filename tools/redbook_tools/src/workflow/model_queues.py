"""Bounded, independent queues for model work.

LLM and image work use separate pools.  MiniMax Token Plan permits a larger
LLM agent fan-out, while other providers retain the conservative two-request
cap.  Image generation remains capped at two and publishing is handled by
the caller's serial upload loop.
"""

from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from threading import Event, Lock
from typing import Callable, Generic, TypeVar
from src.agent.capabilities.dispatcher import contextual_callback


T = TypeVar("T")


class ModelWorkStopped(CancelledError):
    """The batch stopped before this model callable was admitted."""


def _normalize_provider(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _cap_workers(value: int, *, maximum: int) -> int:
    return max(1, min(maximum, int(value)))


def infer_llm_provider(configs: object) -> str | None:
    """Return a provider only when every configured LLM is that provider."""
    try:
        providers = {
            _normalize_provider(getattr(config, "provider", ""))
            for config in configs  # type: ignore[union-attr]
            if _normalize_provider(getattr(config, "provider", ""))
        }
    except TypeError:
        return None
    return next(iter(providers)) if len(providers) == 1 else None


class ModelWorkQueues:
    """Own one bounded executor for LLM work and one for image work."""

    def __init__(
        self,
        *,
        llm_workers: int = 2,
        image_workers: int = 2,
        llm_provider: str | None = None,
    ) -> None:
        self.llm_provider = _normalize_provider(llm_provider)
        llm_maximum = 5 if self.llm_provider == "minimax" else 2
        self.llm_workers = _cap_workers(llm_workers, maximum=llm_maximum)
        self.image_workers = _cap_workers(image_workers, maximum=2)
        self.stop_event = Event()
        self._admission_lock = Lock()
        self.llm = ThreadPoolExecutor(max_workers=self.llm_workers, thread_name_prefix="redbook-llm")
        self.image = ThreadPoolExecutor(max_workers=self.image_workers, thread_name_prefix="redbook-image")

    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    def request_stop(self) -> None:
        with self._admission_lock:
            self.stop_event.set()

    def _invoke(self, fn: Callable[..., T], args: tuple, kwargs: dict) -> T:
        # Admission and stop are ordered; never hold this lock during IO.
        with self._admission_lock:
            if self.stopped:
                raise ModelWorkStopped("model work stopped before invocation")
        return fn(*args, **kwargs)

    def _submit(self, executor: ThreadPoolExecutor, fn: Callable[..., T], args: tuple, kwargs: dict) -> Future[T]:
        with self._admission_lock:
            if self.stopped:
                raise ModelWorkStopped("model work stopped before submission")
            return executor.submit(self._invoke, contextual_callback(fn), args, kwargs)

    def submit_llm(self, fn: Callable[..., T], *args, **kwargs) -> Future[T]:
        return self._submit(self.llm, fn, args, kwargs)

    def submit_image(self, fn: Callable[..., T], *args, **kwargs) -> Future[T]:
        return self._submit(self.image, fn, args, kwargs)

    def close(self) -> None:
        if self.stopped:
            # Cancel both pending lanes before waiting on either running lane.
            self.llm.shutdown(wait=False, cancel_futures=True)
            self.image.shutdown(wait=False, cancel_futures=True)
        self.llm.shutdown(wait=True, cancel_futures=self.stopped)
        self.image.shutdown(wait=True, cancel_futures=self.stopped)

    def __enter__(self) -> "ModelWorkQueues":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.request_stop()
        self.close()
