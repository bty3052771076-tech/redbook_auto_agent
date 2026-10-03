from datetime import datetime, timezone

from src.sources.models import SourceArticle, SourceRequest, SourceSpec
from src.sources.registry import SourceRegistry
from src.sources.service import UnifiedNewsSourceService


def article(name, publisher, published):
    return SourceArticle(
        article_id=name, title=name, url=f'https://example.test/{name}',
        publisher_id=publisher, publisher_family=publisher,
        discovery_source_id=publisher, discovery_kind='native_rss',
        network_group=publisher, published_at=published,
    )


def collect(tmp_path, monkeypatch, batches):
    specs = [SourceSpec(
        source_id=name, adapter='rss', publisher_id=name,
        publisher_family=name, source_name=name,
        source_url=f'https://example.test/{name}', source_packs=('daily_news',),
    ) for name in batches]
    service = UnifiedNewsSourceService(
        SourceRegistry(specs), snapshot_dir=tmp_path, max_workers=1,
    )

    def fetch(spec, timeout):
        values = batches[spec.source_id]
        return values, dict(source_id=spec.source_id, status='success', item_count=len(values))

    monkeypatch.setattr(service, '_fetch_rss', fetch)
    request = SourceRequest(
        request_id='cap', run_id='run', purpose='daily_news', prompt='world',
        as_of=datetime(2026, 10, 2, 15, tzinfo=timezone.utc),
        target_count=4, max_records=4, search_days=1,
    )
    return service.search(request)


def test_old_early_feed_cannot_evict_fresh_late_feed(tmp_path, monkeypatch):
    batches = {
        'early': [article(f'old-{i}', 'early', '2026-09-10T12:00:00+00:00') for i in range(8)]
                 + [article(f'early-{i}', 'early', '2026-10-02T14:00:00+00:00') for i in range(2)],
        'late': [article(f'late-{i}', 'late', '2026-10-02T13:00:00+00:00') for i in range(3)],
    }
    result = collect(tmp_path, monkeypatch, batches)
    assert len(result.items) == 4
    assert {item.publisher_family for item in result.items} == {'early', 'late'}
    assert all(item.published_at.startswith('2026-10-02') for item in result.items)
    assert result.coverage['unique_articles'] == 13


def test_large_fast_publisher_cannot_remove_other_publisher(tmp_path, monkeypatch):
    batches = {
        'early': [article(f'early-{i}', 'early', '2026-10-02T14:00:00+00:00') for i in range(10)],
        'late': [article('late-0', 'late', '2026-10-02T13:00:00+00:00')],
    }
    result = collect(tmp_path, monkeypatch, batches)
    assert len(result.items) == 4
    assert 'late-0' in {item.article_id for item in result.items}
    reversed_result = collect(tmp_path, monkeypatch, dict(reversed(list(batches.items()))))
    assert [item.article_id for item in reversed_result.items] == [item.article_id for item in result.items]


def test_future_and_unknown_dates_do_not_crowd_out_valid_dates(tmp_path, monkeypatch):
    batches = {'early': [article('future', 'early', '2026-10-03T12:00:00+00:00'),
                         article('unknown', 'early', ''),
                         article('invalid', 'early', 'not-a-date')],
               'late': [article(f'late-{i}', 'late', '2026-10-02T13:00:00+00:00') for i in range(4)]}
    result = collect(tmp_path, monkeypatch, batches)
    assert {item.article_id for item in result.items} == {'late-0', 'late-1', 'late-2', 'late-3'}
    assert result.coverage['unique_articles'] == 7
