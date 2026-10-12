from datetime import datetime, timezone
import json

import pytest

from src.ai_digest import collect
from src.ai_digest.models import AIUpdateItem
from src.ai_digest.sources import AIDigestSource


def update(source_type="aggregator"):
    return AIUpdateItem(
        title="OpenAI releases a model with improved coding and tool use",
        summary="OpenAI releases a coding model with tool calling and a larger context window.",
        url=("https://openai.com/index/test-model" if source_type == "official"
             else "https://arxiv.org/abs/2610.00001"),
        published_at="2026-10-12T01:00:00Z",
        source_name="OpenAI" if source_type == "official" else "arXiv",
        vendor="OpenAI",
        source_type=source_type,
    )


@pytest.mark.parametrize("types,minimum,expected", [
    (["aggregator", "social"], 1, True),
    (["official", "aggregator"], 2, True),
    (["official", "aggregator"], 1, False),
    (["official"], 6, False),
    (["aggregator"], 0, False),
])
def test_search_need_includes_official_evidence(types, minimum, expected):
    assert collect._needs_search_backfill(
        [update(kind) for kind in types], target_count=len(types),
        min_official_count=minimum,
        min_domestic_model_count=0, min_foreign_ai_count=0,
        require_target_count=True,
    ) is expected


def test_full_aggregator_pool_cannot_skip_official_search(monkeypatch):
    monkeypatch.setenv("AI_DIGEST_SEARCH_BACKFILL", "1")
    monkeypatch.delenv("AI_DIGEST_RESEARCH_ITEMS_FILE", raising=False)
    calls = []

    def search(**kwargs):
        calls.append(kwargs)
        return [update("official")], {"errors": []}

    monkeypatch.setattr(collect, "fetch_ai_digest_search_backfill", search)
    monkeypatch.setattr(collect, "fetch_ai_digest_prompt_topic_backfill",
                        lambda **kwargs: ([], {}))
    items, meta = collect.collect_ai_digest_updates(
        sources=[AIDigestSource("papers", "aggregator", "https://arxiv.org/rss/cs.AI", "arXiv")],
        fetch_source=lambda source: [update()], target_count=1,
        min_official_count=1, include_pool_items=True, max_age_days=2,
        now=datetime(2026, 10, 12, 5, tzinfo=timezone.utc),
        persist_source_health=False,
    )
    assert len(calls) == 1
    assert meta["search_backfill_used"] is True
    assert any(item.source_type == "official" for item in items)


@pytest.mark.parametrize("host,accepted", [
    ("sakana.ai", True), ("pub.sakana.ai", True), ("sakana.ai.evil.test", False),
])
def test_sakana_research_supplement_requires_official_host(tmp_path, host, accepted):
    item = update("official").model_copy(update={
        "vendor": "Sakana AI", "url": f"https://{host}/mass/",
        "raw_excerpt": "MASS trains one model from its multi-agent team's executions.",
        "evidence_urls": ["https://sakana.ai/mass/"],
    })
    path = tmp_path / "research.json"
    path.write_text(json.dumps([item.model_dump()]), encoding="utf-8")
    if accepted:
        assert collect.load_ai_digest_research_items(path)[0].vendor == "Sakana AI"
    else:
        with pytest.raises(ValueError, match="official host"):
            collect.load_ai_digest_research_items(path)
