"""Retained source materials remain usable after context compaction, offline."""
from dataclasses import asdict, replace
import json
from pathlib import Path

from backend import dialogue_tool_worker as domain
from src.news.daily_news import NewsItem
from test_conversational_agent_cancellation import no_external_work
from test_conversational_agent_store import dialogue_store
from test_conversational_agent_domain_execute import action


def material(action, identity, *, cid=None, date='2026-11-17T01:00:00Z', url=None):
    folder = Path(action.context['session_directory']) / 'materials'
    folder.mkdir(parents=True, exist_ok=True)
    payload = {'material_id': identity, 'conversation_id': cid or action.cid, 'namespace': action.caps.namespace,
        'days': 2, 'item': asdict(NewsItem(title=identity, url=url or 'https://fixture.invalid/' + identity,
            source='Offline fixture', seendate=date, description='Retained evidence summary.', content='Full verified fixture material.'))}
    (folder / (identity + '.json')).write_text(json.dumps(payload), encoding='utf-8')


def test_list_materials_reuses_owned_fresh_cache_without_network(action, monkeypatch):
    material(action, 'fresh')
    material(action, 'duplicate', url='https://fixture.invalid/fresh?spm=tracking')
    material(action, 'foreign', cid='another-conversation')
    material(action, 'stale', date='2025-05-29T01:00:00Z')
    monkeypatch.setattr(domain, '_news_search', lambda *args: (_ for _ in ()).throw(AssertionError('No new source collection allowed')))
    result = action.execute('list_materials', {})
    assert result['ok'] is True and result['network_collection'] is False
    assert result['total'] == result['count'] == 1
    assert result['skipped_records'] == 3
    assert result['materials'][0]['material_id'] in {'fresh', 'duplicate'}
    assert result['materials'][0]['description_excerpt'] == 'Retained evidence summary.'
    assert result['materials'][0]['has_content'] is True


def test_material_paging_preserves_ids_and_excludes_published_urls(action, monkeypatch):
    from src.news import history
    for index in range(4):
        material(action, 'source' + str(index))
    monkeypatch.setattr(history, 'collect_used_news_url_keys', lambda: {'https://fixture.invalid/source3'})
    first = action.execute('list_materials', {'limit': 2})
    second = action.execute('list_materials', {'limit': 2, 'offset': first['next_offset']})
    assert first['total'] == 3 and first['next_offset'] == 2
    assert second['count'] == 1 and second['next_offset'] is None
    assert {row['material_id'] for row in first['materials'] + second['materials']} == {'source0', 'source1', 'source2'}


def test_empty_material_cache_reports_gap_without_new_collection(action):
    result = action.execute('list_materials', {})
    assert result['ok'] is True and result['gap'] is True
    assert result['materials'] == [] and result['total'] == 0


def test_topic_search_uses_existing_qualified_fallback_and_ranks_visible_evidence(action, monkeypatch):
    from src.news import daily_news, history
    unrelated = NewsItem(title='Museum opens', url='https://fixture.invalid/museum', source='quantum provider',
                         seendate='2026-11-17T01:00:00Z')
    matched = NewsItem(title='Quantum chip announced', url='https://fixture.invalid/chip', source='Fixture lab',
                       seendate='2026-11-17T01:00:00Z', description='Verified quantum device announcement.')
    observed = {}
    def fetch(query, **kwargs):
        observed.update(kwargs)
        check = kwargs['qualified_count_callback']
        assert check([unrelated]) == 0  # Provider label alone is not a topic match.
        assert check([replace(matched, seendate='2025-05-29T01:00:00Z')]) == 0
        assert check([unrelated, matched]) == 1
        return [unrelated, matched], {'provider_attempts': ['category_fixture', 'topic_fixture']}
    monkeypatch.setattr(daily_news, 'fetch_daily_news_candidates', fetch)
    monkeypatch.setattr(history, 'collect_used_news_url_keys', lambda: set())
    result = action.execute('news_search', {'query': 'quantum', 'limit': 2, 'days': 2})
    assert result['ok'] is True and observed['minimum_qualified_records'] == 1
    assert observed['max_records'] == 60 and observed['expand_query_variants'] is False
    assert result['count'] == 2 and result['query_match_count'] == 1 and result['query_gap'] is False
    assert result['materials'][0]['url'] == matched.url
    assert result['materials'][1]['relevance_score'] == 0
    assert result['collection']['provider_attempts'] == ['category_fixture', 'topic_fixture']


def test_unmatched_search_reports_topic_gap_without_erasing_retained_material(action, monkeypatch):
    from src.news import daily_news, history
    monkeypatch.setattr(daily_news, 'fetch_daily_news_candidates', lambda *args, **kwargs: ([
        NewsItem(title='Museum opens', url='https://fixture.invalid/museum', source='Fixture feed',
                 seendate='2026-11-17T01:00:00Z')], {'collection_stop_reason': 'provider_plan_exhausted'}))
    monkeypatch.setattr(history, 'collect_used_news_url_keys', lambda: set())
    result = action.execute('news_search', {'query': 'quantum', 'days': 2})
    assert result['ok'] is True and result['query_gap'] is True and result['query_match_count'] == 0
    assert result['count'] == 1 and result['gap'] is False
    assert action.execute('list_materials', {})['total'] == 1
