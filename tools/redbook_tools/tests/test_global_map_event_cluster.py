from datetime import datetime, timezone

from src.global_map.evidence import build_verified_events
from src.global_map.models import EventCandidate


def _candidate(title: str, summary: str, number: int) -> EventCandidate:
    return EventCandidate(
        event_key=f"story-{number}", title=title, summary=summary,
        source=f"Publisher {number}", url=f"https://example.org/{number}",
        published_at=datetime(2026, 9, 30, 8, number, tzinfo=timezone.utc),
        country="Saudi Arabia", location_name="Saudi Arabia",
        latitude=23.8859, longitude=45.0792, publisher_family=f"publisher-{number}",
    )


def test_same_diverted_flight_is_one_event_with_multiple_evidence_articles():
    events = build_verified_events([
        _candidate("Flight to Tel Aviv diverted to Saudi Arabia", "Pilots reported an incident.", 1),
        _candidate("Tel Aviv flight lands in Saudi after emergency", "Flight diverted to Saudi Arabia.", 2),
        _candidate("Flight to Israel diverted after pilots fight", "Plane landed in Saudi Arabia.", 3),
    ], target_date="2026-09-30", cutoff=datetime(2026, 9, 30, 9, tzinfo=timezone.utc))

    assert len(events) == 1
    assert len(events[0].evidence) == 3


def test_historical_retrospective_is_not_treated_as_today_event():
    events = build_verified_events([
        _candidate("France-Algeria: when a minister wanted to expel a general's son", "A 2025 dispute is recalled.", 1),
        _candidate("France-Algérie : quand un ministre voulait expulser le fils d'un général", "En 2025, le ministre ordonnait cette expulsion.", 2),
    ], target_date="2026-09-30", cutoff=datetime(2026, 9, 30, 9, tzinfo=timezone.utc))

    assert not events


def test_distress_signal_headline_groups_with_diverted_flight():
    events = build_verified_events([
        _candidate("Flight to Tel Aviv diverted to Saudi Arabia", "Pilots reported an incident.", 1),
        _candidate("Dubai-Tel Aviv flight lands in Saudi Arabia after issuing distress signal", "Passengers were safe.", 2),
    ], target_date="2026-09-30", cutoff=datetime(2026, 9, 30, 9, tzinfo=timezone.utc))

    assert len(events) == 1
    assert len(events[0].evidence) == 2
