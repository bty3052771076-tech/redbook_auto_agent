from __future__ import annotations

import pytest

from src.global_map.service import _fetch_current_digest
from src.integrations.worldmonitor.client import WorldMonitorError
from src.integrations.worldmonitor.models import WorldMonitorBatch, WorldMonitorCoverage


def _batch(state: str, *, served_stale: bool = False) -> WorldMonitorBatch:
    return WorldMonitorBatch(
        items=[object()],
        categories={},
        coverage=WorldMonitorCoverage(state=state, served_stale=served_stale),
    )


class DigestClient:
    def __init__(self, chinese, english):
        self.chinese = chinese
        self.english = english
        self.calls = []

    def fetch_digest(self, *, lang="zh", reuse_cycle=True):
        self.calls.append((lang, reuse_cycle))
        result = self.chinese if lang == "zh" else self.english
        if isinstance(result, Exception):
            raise result
        return result


def test_current_chinese_digest_does_not_request_fallback():
    chinese = _batch("partial")
    client = DigestClient(chinese, _batch("partial"))

    assert _fetch_current_digest(client) is chinese
    assert client.calls == [("zh", False)]


def test_stale_chinese_digest_uses_current_english_batch():
    english = _batch("partial")
    client = DigestClient(_batch("stale", served_stale=True), english)

    assert _fetch_current_digest(client) is english
    assert client.calls == [("zh", False), ("en", False)]


def test_two_stale_digests_keep_quality_gate_blocked():
    chinese = _batch("stale", served_stale=True)
    client = DigestClient(chinese, _batch("stale", served_stale=True))

    assert _fetch_current_digest(client) is chinese


def test_fallback_handles_primary_request_failure():
    english = _batch("partial")
    client = DigestClient(WorldMonitorError("WM_NOT_READY", "unavailable"), english)

    assert _fetch_current_digest(client) is english


def test_two_request_failures_preserve_primary_error():
    client = DigestClient(
        WorldMonitorError("WM_NOT_READY", "primary unavailable"),
        WorldMonitorError("WM_NOT_READY", "secondary unavailable"),
    )

    with pytest.raises(WorldMonitorError, match="primary unavailable"):
        _fetch_current_digest(client)
