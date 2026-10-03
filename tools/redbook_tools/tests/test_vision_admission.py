from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest

from src.workflow import vision_review


def test_independent_callers_share_two_vision_request_slots(monkeypatch):
    start = Barrier(6)
    two_entered = Event()
    release = Event()
    lock = Lock()
    active = 0
    peak = 0
    calls = 0

    def transport(config, **kwargs):
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
            if active == 2:
                two_entered.set()
        try:
            assert release.wait(5)
            return "reviewed"
        finally:
            with lock:
                active -= 1

    def caller():
        start.wait(timeout=5)
        return vision_review.invoke_vision_review(
            SimpleNamespace(provider="test"), prompt="facts", image_path=Path("unused.png")
        )

    monkeypatch.setattr(vision_review, "_invoke_vision_review_request", transport)
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(caller) for _ in range(5)]
        try:
            start.wait(timeout=5)
            assert two_entered.wait(5)
            with lock:
                assert calls == active == peak == 2
        finally:
            release.set()
        assert [future.result(timeout=5) for future in futures] == ["reviewed"] * 5
    assert calls == 5 and active == 0 and peak == 2


def test_transport_exception_releases_vision_slot(monkeypatch):
    calls = 0

    def transport(config, **kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("failed request")
        return "reviewed"

    monkeypatch.setattr(vision_review, "_invoke_vision_review_request", transport)
    options = dict(prompt="facts", image_path=Path("unused.png"))
    config = SimpleNamespace(provider="test")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="failed request"):
            vision_review.invoke_vision_review(config, **options)
    assert vision_review.invoke_vision_review(config, **options) == "reviewed"
