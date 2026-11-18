from datetime import datetime, timezone
import ssl
import time
from urllib.error import HTTPError, URLError

import pytest

from src.ai_digest import collect
from src.ai_digest.models import AIUpdateItem
from src.ai_digest.sources import AIDigestSource
from src.news.daily_news import NewsItem


NOW = datetime(2026, 11, 9, 10, tzinfo=timezone.utc)


def test_queued_digest_sources_receive_a_full_fetch_window(monkeypatch):
    monkeypatch.setenv("AI_DIGEST_SEARCH_BACKFILL", "0")
    sources = [AIDigestSource(f"source-{i}", "official", f"https://example.org/{i}",
        f"vendor-{i}", "rss", tier="official_stream") for i in range(8)]
    calls = []

    def fetch(source):
        calls.append(source.name)
        time.sleep(0.06)
        return []

    _, meta = collect.collect_ai_digest_updates(sources=sources, fetch_source=fetch,
        now=NOW, source_concurrency=2, batch_timeout_s=0.1, allow_social_backfill=False)
    assert len(calls) == len(sources)
    assert not meta["errors"]
    assert {row["status"] for row in meta["source_health"]["attempts"]} == {"empty"}


def test_ai_search_uses_query_providers_not_general_news_pool(monkeypatch):
    from src.news import daily_news
    requests = []
    rows = [NewsItem(title="OpenAI releases GPT-6 API", url="https://example.org/ai",
        seendate=NOW.isoformat(), description="OpenAI adds image input and new tools to GPT-6 API."),
        NewsItem(title="Central bank selects new leader", url="https://example.org/bank",
            seendate=NOW.isoformat(), description="The board will vote next week.")]

    def fetch(query, **kwargs):
        requests.append(kwargs)
        return rows, {}

    monkeypatch.setattr(daily_news, "fetch_daily_news_candidates", fetch)
    items, meta = collect.fetch_ai_digest_search_backfill(max_age_days=2, now=NOW,
        queries=["OpenAI new model release"], performance_mode="balanced")
    assert requests[0]["_unified_sources"] is False
    assert "google_rss" in requests[0]["preferred_providers"]
    assert requests[0]["qualified_count_callback"](rows) == 1
    assert len(items) == 1
    assert meta["queries"][0]["irrelevant_count"] == 1


def test_schannel_transport_falls_back_with_certificate_verification(monkeypatch):
    monkeypatch.setattr(collect, "_curl_executable", lambda: "curl.exe")
    monkeypatch.setattr(collect, "_curl_get_text", lambda *a, **k: (_ for _ in ()).throw(
        URLError("schannel CRYPT_E_REVOCATION_OFFLINE")))
    calls = []

    class Response:
        class headers:
            @staticmethod
            def get_content_charset():
                return "utf-8"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            return b"verified feed"

    def open_url(request, **kwargs):
        calls.append(kwargs)
        assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["context"].check_hostname
        return Response()

    monkeypatch.setattr(collect.urllib.request, "urlopen", open_url)
    assert collect._http_get_text("https://example.org/feed") == "verified feed"
    assert len(calls) == 1


@pytest.mark.parametrize("error", [HTTPError("https://example.org", 403, "Forbidden", None, None),
    URLError("certificate revoked"), URLError("certificate verify failed"),
    URLError("schannel CRYPT_E_REVOKED: SSL/TLS connection failed")],
    ids=["http", "revoked", "untrusted", "revoked-handshake"])
def test_fallback_does_not_hide_http_or_certificate_rejection(monkeypatch, error):
    monkeypatch.setattr(collect, "_curl_executable", lambda: "curl.exe")
    monkeypatch.setattr(collect, "_curl_get_text", lambda *a, **k: (_ for _ in ()).throw(error))
    monkeypatch.setattr(collect.urllib.request, "urlopen", lambda *a, **k: pytest.fail("unexpected fallback"))
    with pytest.raises(type(error)):
        collect._http_get_text("https://example.org/feed")


def test_discovery_url_change_preserves_verified_original(monkeypatch):
    monkeypatch.setenv("AI_DIGEST_SEARCH_BACKFILL", "0")
    source = AIDigestSource("aihot", "aggregator", "https://example.org/daily", "AI HOT", "rss")
    lead = AIUpdateItem(title="OpenAI releases GPT-6 API", url="https://example.org/detail",
        source_type="aggregator", tags=["aihot_original_unverified"],
        summary="OpenAI opens image input and new API tools.")
    original = lead.model_copy(update={"url": "https://openai.com/index/gpt-6/",
        "source_type": "official", "published_at": NOW.isoformat()})
    monkeypatch.setattr(collect, "resolve_ai_digest_discovery_source", lambda *a, **k: original)
    items, meta = collect.collect_ai_digest_updates(sources=[source], fetch_source=lambda s: [lead],
        max_age_days=2, now=NOW, include_pool_items=True)
    assert items == [original]
    assert meta["fresh_count"] == 1


def test_empty_selection_reports_material_exhaustion_not_a_generation_retry(monkeypatch, tmp_path, capsys):
    from src.workflow import create_post
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AI_DIGEST_STRICT_RECENT", "1")
    monkeypatch.setenv("AI_DIGEST_IMPACT_SUPERVISOR", "0")
    monkeypatch.setenv("AI_DIGEST_PROGRESS", "1")
    monkeypatch.setattr(create_post, "_uploaded_ai_digest_history_keys", lambda **kw: set())
    monkeypatch.setattr(create_post, "load_llm_configs", lambda: [])
    monkeypatch.setattr(create_post, "collect_ai_digest_updates", lambda **kw: ([], {
        "pool_diagnostics": {"fresh": 0, "source_errors": 2},
    }))
    monkeypatch.setattr(create_post, "generate_ai_digest_brief_with_llm",
        lambda *a, **k: pytest.fail("must not generate without evidence"))
    with pytest.raises(RuntimeError, match="AI_DIGEST_MATERIAL_EXHAUSTED") as error:
        create_post.create_daily_ai_digest_posts(asset_paths=[])
    assert '"source_errors": 2' in str(error.value)
    assert "fixed 2-day window" in str(error.value)


def test_discovery_original_still_rejects_stale_and_historical_material(monkeypatch):
    from src.ai_digest.rank import ai_update_history_key
    monkeypatch.setenv("AI_DIGEST_SEARCH_BACKFILL", "0")
    source = AIDigestSource("aihot", "aggregator", "https://example.org/daily", "AI HOT", "rss")
    lead = AIUpdateItem(title="OpenAI releases GPT-6 API", url="https://example.org/detail",
        source_type="aggregator", tags=["aihot_original_unverified"],
        summary="OpenAI opens image input and new API tools.")
    for date, history in [("2026-09-03", False), (NOW.isoformat(), True)]:
        original = lead.model_copy(update={"url": "https://openai.com/index/gpt-6/",
            "source_type": "official", "published_at": date})
        monkeypatch.setattr(collect, "resolve_ai_digest_discovery_source", lambda *a, **k: original)
        items, _ = collect.collect_ai_digest_updates(sources=[source], fetch_source=lambda s: [lead],
            max_age_days=2, now=NOW, include_pool_items=True,
            exclude_history_keys={ai_update_history_key(original)} if history else set())
        assert items == []


def test_expired_source_budget_prevents_another_http_request(monkeypatch):
    monkeypatch.setattr(collect, "_curl_executable", lambda: pytest.fail("request started after deadline"))
    token = collect._SOURCE_DEADLINE.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            collect._http_get_text("https://example.org/next-detail")
    finally:
        collect._SOURCE_DEADLINE.reset(token)


def test_transport_retries_share_the_single_request_budget(monkeypatch):
    clock = [0.0]
    calls = []
    monkeypatch.setattr(collect.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(collect.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def fail(url, *, timeout_s, executable):
        calls.append(timeout_s)
        clock[0] += timeout_s
        raise URLError("TLS handshake timed out")
    monkeypatch.setattr(collect, "_curl_get_text_once", fail)
    with pytest.raises(URLError):
        collect._curl_get_text("https://example.org/feed", timeout_s=8, executable="curl.exe")
    assert calls == [8]


@pytest.mark.parametrize("exhaustive", [False, True])
def test_google_rss_applies_window_before_provider_result_cap(monkeypatch, exhaustive):
    from src.news import daily_news
    monkeypatch.setenv("NEWS_PROVIDER", "google_rss")
    monkeypatch.setenv("NEWS_HISTORY_DEDUPE", "0")
    seen = []
    def fetch(**kwargs):
        seen.append(kwargs["query"])
        return [NewsItem(title="OpenAI releases GPT-6 API", url="https://example.org/ai",
            seendate=NOW.isoformat(), description="OpenAI adds image input and new API tools.")]
    monkeypatch.setattr(daily_news, "_google_rss_fetch_articles", fetch)
    kwargs = {}
    if exhaustive:
        kwargs["session"] = daily_news.NewsFetchSession(now=NOW, remaining_seconds=60)
    daily_news.fetch_daily_news_candidates("OpenAI release", search_days=2, max_records=2,
        expand_query_variants=False, exhaustive_sources=exhaustive, _unified_sources=False, **kwargs)
    assert seen
    assert all(query.endswith("when:2d") for query in seen)
