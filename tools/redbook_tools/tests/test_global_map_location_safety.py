from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from src.global_map.workflow import build_global_map_snapshot
from src.integrations.worldmonitor.models import WorldMonitorBatch, WorldMonitorCoverage, WorldMonitorItem


@pytest.fixture(autouse=True)
def country_catalog(tmp_path, monkeypatch):
    # A deterministic country-name catalog, not invented event evidence.
    path = tmp_path / "countries.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"properties": {"name": name}} for name in
        ("China", "Russia", "Ukraine", "Iran", "Iraq", "Morocco", "Israel", "Saudi Arabia")
    ]}), encoding="utf-8")
    monkeypatch.setenv("GLOBAL_MAP_BASEMAP_PATH", str(path))


def test_map_does_not_geolocate_a_snippet_or_ambiguous_headline():
    now = datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)
    headlines = [
        ("Morocco protests challenge Israel ties", "protest held in Rabat"),
        ("US forces exit Iraq amid Iran tensions", "troops leave bases"),
        ("Oil market reacts to new sanctions", "Iran is mentioned in the article"),
        ("China factory activity expands", "new survey published"),
        ("Russia launches a strike on Kyiv", "latest military updates"),
    ]
    batch = WorldMonitorBatch(
        items=[
            WorldMonitorItem(
                item_id=str(index), title=title, snippet=snippet,
                url=f"https://example.org/{index}", source=f"Publisher {index}", published_at=now,
            )
            for index, (title, snippet) in enumerate(headlines)
        ],
        categories={},
        coverage=WorldMonitorCoverage(state="partial"),
        generated_at=now,
    )

    snapshot = build_global_map_snapshot(batch, target_date="2026-09-30", cutoff=now)
    events = {event.title: event for event in snapshot.events}

    assert events[headlines[0][0]].latitude is None
    assert events[headlines[1][0]].latitude is None
    assert events[headlines[2][0]].latitude is None
    assert events[headlines[3][0]].country == "China"
    assert events[headlines[3][0]].latitude is not None
    assert events[headlines[4][0]].latitude is None


def test_diverted_flight_uses_explicit_landing_country_not_intended_destination():
    now = datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)
    batch = WorldMonitorBatch(
        items=[
            WorldMonitorItem(
                item_id="unknown", title="Israel-bound flight diverted after fight between pilots",
                snippet="The hijacking alert was ruled out.", url="https://example.org/unknown",
                source="Example", published_at=now, latitude=31.0, longitude=35.0,
            ),
            WorldMonitorItem(
                item_id="known", title="Flight to Tel Aviv diverted to Saudi Arabia",
                snippet="Passengers landed safely.", url="https://example.org/known",
                source="Example", published_at=now, latitude=31.0, longitude=35.0,
            ),
        ], categories={}, coverage=WorldMonitorCoverage(state="partial"), generated_at=now,
    )

    snapshot = build_global_map_snapshot(batch, target_date="2026-09-30", cutoff=now)
    events = {event.title: event for event in snapshot.events}

    assert events["Israel-bound flight diverted after fight between pilots"].latitude is None
    assert events["Flight to Tel Aviv diverted to Saudi Arabia"].country == "Saudi Arabia"
    assert events["Flight to Tel Aviv diverted to Saudi Arabia"].latitude is not None


def test_in_flight_attack_is_not_plotted_at_destination_without_landing_evidence():
    now = datetime(2026, 9, 30, 15, 30, tzinfo=timezone.utc)
    batch = WorldMonitorBatch(
        items=[
            WorldMonitorItem(
                item_id="one", title="Co-pilot stabs pilot, tries to crash passenger jet en route to Israel, Netanyahu says",
                snippet="Passengers helped thwart an attack by a FlyDubai pilot.",
                url="https://example.org/one", source="Publisher One", published_at=now,
                latitude=31.0461, longitude=34.8516,
            ),
            WorldMonitorItem(
                item_id="two", title="What we know about stabbing on Flydubai flight to Israel",
                snippet="The captain and first officer were injured.",
                url="https://example.org/two", source="Publisher Two", published_at=now,
                latitude=31.0461, longitude=34.8516,
            ),
        ], categories={}, coverage=WorldMonitorCoverage(state="partial"), generated_at=now,
    )

    snapshot = build_global_map_snapshot(batch, target_date="2026-09-30", cutoff=now)

    assert len(snapshot.events) == 1
    assert len(snapshot.events[0].evidence) == 2
    assert snapshot.events[0].latitude is None
    assert snapshot.events[0].country != "Israel"
    assert not snapshot.upload_allowed
