from datetime import date

from PIL import Image

from apps import cli
from src.agent.editorial_agent import AgentJob
from src.wool import collect, workflow
from src.ai_digest.models import AIUpdateItem
from test_cli_agent_non_news_repair import adapter, digest


def test_agent_passes_wool_preferences_to_actual_generator(adapter, monkeypatch):
    tools, _, context = adapter
    received = []
    monkeypatch.setattr(cli, 'create_daily_wool_posts', lambda **kw: received.append(kw) or [digest('wool')])
    tools.generate(AgentJob('daily_wool', '每日羊毛', prompt='优先关注OpenAI免费额度'), context)
    assert '优先关注OpenAI免费额度' in received[0]['prompt_hint']


def test_wool_preferences_reach_search_before_speed_mode_query_limit(monkeypatch):
    queries = []

    def fetch(**kwargs):
        queries.extend(kwargs['search_backfill_queries'])
        return [], {'source_health': {'attempts': [{'status': 'empty'}]}}

    monkeypatch.setattr(collect, 'collect_ai_digest_updates', fetch)
    offers, meta = collect.collect_daily_wool_offers(
        prompt_hint='优先关注OpenAI免费额度', now=date(2026, 10, 16), performance_mode='speed')
    assert 'OpenAI' in queries[0]
    assert offers == []
    assert meta['editorial_preferences'] == '优先关注OpenAI免费额度'
    assert 'Codex credits reset free quota' in queries


def test_wool_workflow_preserves_preferences_without_inventing_empty_offers(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('REDBOOK_RUNTIME_ROOT', str(tmp_path))
    monkeypatch.setenv('IMAGE_PROVIDER', 'test')
    monkeypatch.setenv('WOOL_IMAGE_MODE', 'local')
    received = []
    image = tmp_path / 'cover.png'
    Image.new('RGB', (80, 80), 'white').save(image)

    def fetch(**kwargs):
        received.append(kwargs)
        return [], {'source_meta': {'errors': [], 'source_health': {'attempts': [{'status': 'empty'}]}}}

    monkeypatch.setattr(workflow, 'collect_daily_wool_offers', fetch)
    monkeypatch.setattr(workflow, 'ensure_wool_assets', lambda: {'without_wool': image})
    post = workflow.create_daily_wool_posts(prompt_hint='优先关注OpenAI免费额度',
        now=date(2026, 10, 16), copy_assets=False)[0]
    assert received[0]['prompt_hint'] == '优先关注OpenAI免费额度'
    assert post.platform['daily_wool']['editorial_preferences'] == '优先关注OpenAI免费额度'
    assert post.platform['daily_wool']['availability_status'] == 'no_verified_offers'
    assert 'OpenAI免费额度' not in post.body


def test_provider_preference_only_reorders_verified_offers_without_dropping_other_vendors(monkeypatch):
    from datetime import datetime, timezone

    updates = [AIUpdateItem(title=f'{vendor}提供免费额度', vendor=vendor,
        summary=f'{vendor}用户可领取1000 tokens免费额度，有效期至2026年10月18日。',
        published_at='2026-10-16T08:00:00+08:00', source_name=vendor,
        url=f'https://example.test/{vendor}', source_type='official') for vendor in ('Gemini', 'OpenAI')]
    monkeypatch.setattr(collect, 'collect_ai_digest_updates', lambda **kw: (updates, {}))
    offers, _ = collect.collect_daily_wool_offers(prompt_hint='优先OpenAI',
        now=datetime(2026, 10, 16, 12, tzinfo=timezone.utc))
    assert [offer.provider for offer in offers] == ['OpenAI', 'Gemini']
    assert all('1000 tokens' in offer.benefit for offer in offers)
