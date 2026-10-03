from datetime import datetime, timezone
import time

from src.sources.models import SourceRequest, SourceSpec
from src.sources.registry import SourceRegistry
from src.sources.service import UnifiedNewsSourceService


def test_queued_sources_get_their_own_request_timeout(tmp_path, monkeypatch):
    specs = [SourceSpec(
        source_id=f"source-{i}", adapter="rss", publisher_id=f"publisher-{i}",
        publisher_family=f"publisher-{i}", source_name=f"Source {i}",
        source_url=f"https://example.test/{i}", source_packs=("daily_news",),
    ) for i in range(4)]
    service = UnifiedNewsSourceService(SourceRegistry(specs), snapshot_dir=tmp_path, max_workers=1)
    calls = []

    def fetch(spec, timeout):
        calls.append((spec.source_id, timeout))
        time.sleep(0.012)
        return [], {"source_id": spec.source_id, "status": "success", "item_count": 0}

    monkeypatch.setattr(service, "_fetch_rss", fetch)
    request = SourceRequest(
        request_id="request", run_id="run", purpose="daily_news", prompt="world",
        as_of=datetime(2026, 10, 2, tzinfo=timezone.utc), timeout_s=0.02,
    )
    snapshot = service.search(request)
    assert len(calls) == 4
    assert all(timeout == 0.5 for _, timeout in calls)
    assert len(snapshot.coverage["source_attempts"]) == 4
    assert not any(a["status"] == "deadline_exceeded" for a in snapshot.coverage["source_attempts"])
