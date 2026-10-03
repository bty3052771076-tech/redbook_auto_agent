from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from src.global_map.geography import resolve_explicit_country_location
from src.global_map.workflow import build_global_map_snapshot
from src.integrations.worldmonitor.models import WorldMonitorBatch, WorldMonitorCoverage, WorldMonitorItem


@pytest.fixture(autouse=True)
def country_catalog(tmp_path, monkeypatch):
    path = tmp_path / "countries.geojson"
    names = ["Bolivia", "Peru", "Chile", "United States of America", "Israel", "Saudi Arabia", "China", "South Sudan", "Sudan"]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": name}, "geometry": {"type": "Polygon", "coordinates": []}}
        for name in names
    ]}), encoding="utf-8")
    monkeypatch.setenv("GLOBAL_MAP_BASEMAP_PATH", str(path))
    return path


def event(title, snippet="", **kwargs):
    now = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    item = WorldMonitorItem(item_id="test", title=title, snippet=snippet,
                            url="https://example.org/test", source="Test", published_at=now, **kwargs)
    snapshot = build_global_map_snapshot(
        WorldMonitorBatch(items=[item], categories={}, coverage=WorldMonitorCoverage(state="partial"), generated_at=now),
        target_date="2026-10-01", cutoff=now,
    )
    assert len(snapshot.events) == 1
    return snapshot.events[0]


@pytest.mark.parametrize("subject", ["Bolivia", "Peru", "Chile"])
def test_unanchored_subject_cannot_be_stolen_by_other_country(subject):
    result = event(f"{subject} arrests attorney general accused by U.S. of drug cartel ties")
    assert result.latitude is None
    assert result.longitude is None
    assert result.country != "United States"
    assert result.location_precision == "unknown"


def test_upstream_country_hint_does_not_resolve_multicountry_headline():
    result = event("Bolivia arrests attorney general accused by U.S. of drug cartel ties",
                   raw={"country": "United States"}, location_name="United States")
    assert result.latitude is None
    assert result.country == ""
    assert result.location_name == ""


def test_missing_catalog_fails_closed_instead_of_reverting_to_incomplete_names(monkeypatch, tmp_path):
    monkeypatch.setenv("GLOBAL_MAP_BASEMAP_PATH", str(tmp_path / "missing.geojson"))
    assert resolve_explicit_country_location("Bolivia arrests official accused by U.S.") is None


def test_malformed_catalog_fails_closed(country_catalog):
    country_catalog.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    assert resolve_explicit_country_location("Bolivia arrests official accused by U.S.") is None


def test_existing_unique_country_still_has_country_anchor():
    resolved = resolve_explicit_country_location("China factory activity expands")
    assert resolved.country == "China"
    assert resolved.precision == "country"
    assert resolved.latitude == 35.8617


def test_explicit_basemap_path_overrides_missing_default(country_catalog, monkeypatch, tmp_path):
    monkeypatch.setenv("GLOBAL_MAP_BASEMAP_PATH", str(tmp_path / "missing.geojson"))
    resolved = resolve_explicit_country_location("China factory activity expands", basemap_path=country_catalog)
    assert resolved.country == "China"
    assert resolve_explicit_country_location("Bolivia arrests official accused by U.S.", basemap_path=country_catalog) is None


def test_nonflight_explicit_coordinates_remain_unchanged():
    result = event("Factory opens after reconstruction", latitude=30.5, longitude=114.3,
                   raw={"country": "China"}, location_name="Wuhan")
    assert (result.latitude, result.longitude) == (30.5, 114.3)
    assert result.location_name == "Wuhan"


def test_multiple_anchored_countries_still_remain_unknown():
    result = event("China and United States discuss trade agreement")
    assert result.latitude is None
    assert result.country == ""


def test_catalog_official_name_and_existing_alias_are_one_country():
    resolved = resolve_explicit_country_location("United States of America releases employment report")
    assert resolved.country == "United States"


@pytest.mark.parametrize("title", [
    "Passengers describe terrifying flight bound for Israel",
    "Terrifying ordeal on Israel-bound flight",
    "Passengers recall flight en route to Israel",
    "Terrifying flight to Israel ends after cockpit struggle",
])
def test_destination_is_not_incident_location_even_without_known_incident_keyword(title):
    result = event(title, latitude=31.0461, longitude=34.8516, raw={"country": "Israel"})
    assert result.latitude is None
    assert result.longitude is None
    assert result.country == ""


def test_actual_landing_is_accepted_but_intended_destination_is_not():
    result = event("Passengers describe terrifying flight bound for Israel",
                   "The plane landed in Saudi Arabia after the crew regained control.")
    assert result.country == "Saudi Arabia"
    assert result.latitude is not None
    assert result.location_precision == "country"


@pytest.mark.parametrize("snippet", ["The plane may land in Saudi Arabia.", "The plane never landed in Saudi Arabia."])
def test_planned_or_denied_landing_is_not_location_evidence(snippet):
    result = event("Terrifying flight bound for Israel", snippet)
    assert result.latitude is None
    assert result.country == ""


def test_local_worldmonitor_catalog_prevents_real_bolivia_mislocation(monkeypatch):
    path = Path("E:/AI/codex/worldmonitor/public/data/countries.geojson")
    if not path.is_file():
        pytest.skip("local WorldMonitor GeoJSON not present")
    monkeypatch.setenv("GLOBAL_MAP_BASEMAP_PATH", str(path))
    assert resolve_explicit_country_location("Bolivia arrests attorney general accused by U.S. of drug cartel ties") is None
