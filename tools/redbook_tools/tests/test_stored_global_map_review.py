from copy import deepcopy
import importlib
import json
from pathlib import Path
import socket

import pytest


def review(metadata):
    try:
        module = importlib.import_module("src.global_map.review")
    except ModuleNotFoundError as exc:
        assert exc.name != "src.global_map.review", "stored map review is not implemented"
        raise
    return module.stored_global_map_review_issues(metadata)


@pytest.fixture(autouse=True)
def catalog(tmp_path, monkeypatch):
    path = tmp_path / "countries.geojson"
    names = ["China", "Germany", "Morocco", "Bolivia", "Peru", "Chile",
             "United States of America", "Israel", "Saudi Arabia"]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"properties": {"name": name}} for name in names
    ]}), encoding="utf-8")
    monkeypatch.setenv("GLOBAL_MAP_BASEMAP_PATH", str(path))
    return path


def event(key, title, country, lat, lon, *, precision="country", method="explicit_country_name"):
    return {
        "event_key": key, "title": "Translated headline", "summary": "",
        "country": country, "location_name": country, "latitude": lat, "longitude": lon,
        "location_precision": precision, "location_method": method,
        "evidence": [{"title": title, "summary": "", "source": "Test Publisher",
                      "url": "https://example.org/" + key,
                      "published_at": "2026-10-01T09:00:00+00:00", "authority": 0.8}],
    }


@pytest.fixture
def metadata():
    return {
        "target_date": "2026-10-01", "cutoff": "2026-10-01T18:00:00+08:00",
        "source_state": "partial", "coverage_status": "limited", "upload_allowed": True,
        "located_event_count": 3, "country_count": 3,
        "events": [
            event("china", "China factory activity expands", "China", 35.8617, 104.1954),
            event("germany", "Germany opens a new factory", "Germany", 51.1657, 10.4515),
            event("morocco", "Morocco completes reconstruction", "Morocco", 31.7917, -7.0926),
        ],
    }


def test_grounded_stored_map_passes_without_mutation_or_external_actions(metadata, monkeypatch):
    before = deepcopy(metadata)

    def forbidden(*args, **kwargs):
        raise AssertionError("review must not perform external actions or write files")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    from src.global_map import workflow
    monkeypatch.setattr(workflow, "render_snapshot", forbidden)
    monkeypatch.setattr(workflow, "save_snapshot", forbidden)
    assert review(metadata) == []
    assert metadata == before


@pytest.mark.parametrize("title", [
    "What to know about the terrifying FlyDubai flight bound for Israel - AP News",
    "Netanyahu praises passengers who helped avert disaster on Israel-bound flight",
    "Passengers recall flight en route to Israel",
])
def test_destination_anchor_is_not_incident_location(metadata, title):
    metadata["events"][0] = event("flight", title, "Israel", 31.0461, 34.8516)
    assert any("MAP_LOCATION_UNVERIFIED: event 1" in issue for issue in review(metadata))


@pytest.mark.parametrize("subject", ["Bolivia", "Peru", "Chile"])
@pytest.mark.parametrize("charges", ["accused by U.S. of drug cartel ties", "facing US charges"])
def test_accuser_country_cannot_locate_arrest(metadata, subject, charges):
    metadata["events"][0] = event("arrest", f"{subject} arrests attorney general {charges}",
                                   "United States", 37.0902, -95.7129)
    assert any("MAP_LOCATION_UNVERIFIED: event 1" in issue for issue in review(metadata))


def test_confirmed_landing_can_support_existing_country_anchor(metadata):
    row = event("flight", "Terrifying flight bound for Israel", "Saudi Arabia", 23.8859, 45.0792)
    row["evidence"][0]["summary"] = "The plane landed in Saudi Arabia after the crew regained control."
    metadata["events"][0] = row
    assert review(metadata) == []


@pytest.mark.parametrize("summary", [
    "The plane never landed in Saudi Arabia.", "The plane may land in Saudi Arabia.",
    "The plane landed in Saudi Arabia. It later landed in Israel.",
])
def test_denied_planned_or_conflicting_landings_fail_closed(metadata, summary):
    row = event("flight", "Terrifying flight bound for Israel", "Saudi Arabia", 23.8859, 45.0792)
    row["evidence"][0]["summary"] = summary
    metadata["events"][0] = row
    assert any("MAP_LOCATION_UNVERIFIED: event 1" in issue for issue in review(metadata))


def test_reliable_nonflight_city_coordinates_survive_ambiguous_country_text(metadata):
    metadata["events"][0] = event("arrest", "Bolivia arrests official facing US charges",
                                   "Bolivia", -16.5, -68.15, precision="city", method="explicit_coordinates")
    assert review(metadata) == []
    assert metadata["events"][0]["latitude"] == -16.5


def test_city_coordinates_on_equator_are_not_lost_to_truthiness(metadata):
    metadata["events"][0] = event("equator", "Factory opens", "Ecuador", 0.0, -78.5,
                                   precision="city", method="explicit_coordinates")
    assert review(metadata) == []


@pytest.mark.parametrize("latitude,longitude", [(None, 104.1954), (0, 0), (91, 104),
                                               (True, 104), (float("nan"), 104), (35, float("inf"))])
def test_invalid_stored_coordinates_cannot_be_repaired_into_approval(metadata, latitude, longitude):
    metadata["events"][0]["latitude"] = latitude
    metadata["events"][0]["longitude"] = longitude
    assert review(metadata)


def test_wrong_stored_anchor_is_rejected_even_if_source_names_same_country(metadata):
    metadata["events"][0]["latitude"] = 30.0
    assert any("MAP_LOCATION_UNVERIFIED: event 1" in issue for issue in review(metadata))


def test_missing_catalog_blocks_inference_but_not_reliable_coordinates(metadata, monkeypatch, tmp_path):
    monkeypatch.setenv("GLOBAL_MAP_BASEMAP_PATH", str(tmp_path / "missing.geojson"))
    assert review(metadata)
    for row in metadata["events"]:
        row["location_precision"] = "city"
        row["location_method"] = "explicit_coordinates"
    assert review(metadata) == []


@pytest.mark.parametrize("as_snapshot", [False, True], ids=["stored-metadata", "generation-snapshot"])
def test_fresh_complete_source_accepts_grounded_map(metadata, as_snapshot):
    metadata["source_state"] = "complete"
    before = deepcopy(metadata)
    value = snapshot_from_metadata(metadata) if as_snapshot else metadata
    assert review(value) == []
    assert metadata == before


def test_complete_source_does_not_bypass_real_bad_map_semantics(real_stored_map):
    real_stored_map["source_state"] = "complete"
    issues = review(real_stored_map)
    assert any("MAP_LOCATION_UNVERIFIED: event 2" in issue for issue in issues)
    assert any("MAP_COVERAGE_INSUFFICIENT" in issue for issue in issues)


@pytest.mark.parametrize("state", ["stale", "error", "failed", "unknown", "", None])
def test_nonfresh_source_cannot_be_overridden_by_stored_upload_flag(metadata, state):
    metadata["source_state"] = state
    assert any("MAP_SOURCE_NOT_FRESH" in issue for issue in review(metadata))


@pytest.mark.parametrize("field,value", [("published_at", None), ("published_at", "bad"),
                                        ("published_at", "2026-09-30T09:00:00Z"),
                                        ("published_at", "2026-10-01T10:01:00Z"),
                                        ("url", ""), ("title", ""), ("source", "")])
def test_missing_stale_or_future_evidence_cannot_approve_location(metadata, field, value):
    metadata["events"][0]["evidence"][0][field] = value
    assert any("MAP_EVIDENCE_UNVERIFIED: event 1" in issue for issue in review(metadata))


def test_one_fresh_evidence_article_is_enough_when_another_is_stale(metadata):
    stale = deepcopy(metadata["events"][0]["evidence"][0])
    stale["published_at"] = "2026-09-30T09:00:00Z"
    metadata["events"][0]["evidence"].append(stale)
    assert review(metadata) == []


def test_distinct_fresh_sources_disagreeing_on_location_cannot_approve(metadata):
    other = deepcopy(metadata["events"][0]["evidence"][0])
    other["title"] = "Germany factory activity expands"
    metadata["events"][0]["evidence"].append(other)
    assert any("MAP_LOCATION_UNVERIFIED: event 1" in issue for issue in review(metadata))


def test_unlocated_audit_event_is_not_counted_as_a_plotted_event(metadata):
    row = event("unknown", "Bolivia arrests official accused by U.S.", "", None, None,
                precision="unknown", method="")
    metadata["events"].append(row)
    assert review(metadata) == []


def test_three_located_events_and_two_countries_are_the_exact_minimum(metadata):
    metadata["events"][2] = event("china-two", "China opens another factory", "China", 35.8617, 104.1954)
    metadata["country_count"] = 2
    assert review(metadata) == []


def test_forged_counts_do_not_replace_recomputed_minimum(metadata):
    metadata["events"].pop()
    metadata["located_event_count"] = metadata["country_count"] = 99
    assert any("MAP_COVERAGE_INSUFFICIENT" in issue for issue in review(metadata))


def test_three_events_in_one_country_fail_coverage_gate(metadata):
    metadata["events"] = [event(str(i), "China opens factory " + str(i), "China", 35.8617, 104.1954)
                          for i in range(3)]
    metadata["country_count"] = 1
    assert any("MAP_COVERAGE_INSUFFICIENT" in issue for issue in review(metadata))


def test_duplicate_event_keys_do_not_inflate_coverage(metadata):
    metadata["events"][1] = deepcopy(metadata["events"][0])
    assert any("MAP_COVERAGE_INSUFFICIENT" in issue for issue in review(metadata))


@pytest.mark.parametrize("value", [False, "true", None])
def test_existing_upload_block_or_nonboolean_flag_remains_blocked(metadata, value):
    metadata["upload_allowed"] = value
    assert any("MAP_UPLOAD_BLOCKED" in issue for issue in review(metadata))


@pytest.mark.parametrize("field,value", [("events", None), ("events", [None]),
                                        ("target_date", "bad"), ("cutoff", "bad"),
                                        ("cutoff", None)])
def test_malformed_snapshot_fails_closed_instead_of_raising(metadata, field, value):
    metadata[field] = value
    assert review(metadata)


@pytest.mark.parametrize("metadata", [None, {}, [], "not a snapshot"])
def test_missing_metadata_fails_closed(metadata):
    assert review(metadata)


def test_diagnostics_never_echo_signed_source_urls(metadata):
    metadata["events"][0]["evidence"][0]["url"] = "https://example.org/?signature=DO-NOT-PRINT"
    metadata["events"][0]["evidence"][0]["title"] = "Flight bound for Israel"
    issues = review(metadata)
    assert issues
    assert "signature" not in " ".join(issues)
    assert "DO-NOT-PRINT" not in " ".join(issues)


def test_real_runtime_post_is_rejected_read_only_without_exposing_urls():
    path = Path("E:/AI/codex/redbook_runtime/data/posts/34c5a4bdcfb9435aae6ca73a4126c13c/post.json")
    if not path.is_file():
        pytest.skip("runtime acceptance post is not present")
    before = path.read_bytes()
    raw = json.loads(before)["platform"]["global_map"]
    issues = review(raw)
    assert any("MAP_LOCATION_UNVERIFIED: event 1" in issue for issue in issues)
    assert any("MAP_LOCATION_UNVERIFIED: event 2" in issue for issue in issues)
    assert any("MAP_LOCATION_UNVERIFIED: event 5" in issue for issue in issues)
    assert any("MAP_COVERAGE_INSUFFICIENT" in issue for issue in issues)
    assert path.read_bytes() == before


@pytest.fixture
def real_stored_map():
    # Public metadata copied read-only from the Oct 1 runtime acceptance post.
    path = Path(__file__).parent / "fixtures/global_map_2026_10_01_stored.json"
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot_from_metadata(raw):
    from datetime import datetime
    from src.global_map.models import EvidenceArticle, MapSnapshot, VerifiedEvent

    events = []
    for row in raw["events"]:
        evidence = [EvidenceArticle(**{
            **article, "published_at": datetime.fromisoformat(article["published_at"]),
        }) for article in row["evidence"]]
        events.append(VerifiedEvent(**{**row, "evidence": evidence}))
    fields = {key: value for key, value in raw.items() if key != "events"}
    return MapSnapshot(**fields, events=events)


def test_frozen_real_map_rejects_destination_and_accuser_not_just_bad_bytes(real_stored_map):
    before = deepcopy(real_stored_map)
    issues = review(real_stored_map)
    for index in (1, 2, 5):
        assert any(f"MAP_LOCATION_UNVERIFIED: event {index}" in issue for issue in issues)
    assert any("MAP_COVERAGE_INSUFFICIENT" in issue for issue in issues)
    assert real_stored_map == before
    assert review(snapshot_from_metadata(real_stored_map)) == issues


def test_generation_blocks_real_bad_map_before_translator(real_stored_map, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime
    from src.global_map import workflow

    snapshot = snapshot_from_metadata(real_stored_map)
    calls = []
    monkeypatch.setattr(workflow, "build_global_map_snapshot", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(workflow, "load_basemap", lambda *args: SimpleNamespace(
        to_dict=lambda: {}, sha256="offline", feature_count=0,
    ))

    def render(value, path, **kwargs):
        calls.append(value)
        path.write_bytes(b"offline-render-sentinel")
        return path

    def forbidden_translate(value):
        raise AssertionError("unverified source locations must be blocked before model translation")

    monkeypatch.setattr(workflow, "render_snapshot", render)
    post = workflow.create_global_map_post(
        None, target_date=snapshot.target_date, cutoff=datetime.fromisoformat(snapshot.cutoff),
        output_dir=tmp_path, translate_events=forbidden_translate,
    )
    assert post is None
    assert not calls[0].upload_allowed
    audit = json.loads((tmp_path / "global-map-2026-10-01.json").read_text(encoding="utf-8"))
    assert audit["upload_allowed"] is False
    assert "MAP_LOCATION_UNVERIFIED" in audit["warning"]
    assert snapshot.upload_allowed is True


def test_generation_rechecks_semantics_after_translation(metadata, real_stored_map, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime
    from src.global_map import workflow

    snapshot = snapshot_from_metadata(metadata)
    monkeypatch.setattr(workflow, "build_global_map_snapshot", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(workflow, "load_basemap", lambda *args: SimpleNamespace(
        to_dict=lambda: {}, sha256="offline", feature_count=0,
    ))

    def render(value, path, **kwargs):
        path.write_bytes(b"offline-render-sentinel")
        return path

    calls = []
    def translate(value):
        calls.append(value)
        return snapshot_from_metadata(real_stored_map)

    monkeypatch.setattr(workflow, "render_snapshot", render)
    assert workflow.create_global_map_post(
        None, target_date=snapshot.target_date, cutoff=datetime.fromisoformat(snapshot.cutoff),
        output_dir=tmp_path, translate_events=translate,
    ) is None
    assert len(calls) == 1


def test_generation_from_real_evidence_never_repeats_us_or_destination_anchors(real_stored_map):
    from datetime import datetime
    from src.global_map.workflow import build_global_map_snapshot
    from src.integrations.worldmonitor.models import WorldMonitorBatch, WorldMonitorCoverage, WorldMonitorItem

    now = datetime.fromisoformat(real_stored_map["cutoff"])
    items = [WorldMonitorItem(
        item_id=str(index), title=row["evidence"][0]["title"],
        snippet=row["evidence"][0]["summary"], source=row["evidence"][0]["source"],
        url=row["evidence"][0]["url"],
        published_at=datetime.fromisoformat(row["evidence"][0]["published_at"]),
    ) for index, row in enumerate(real_stored_map["events"])]
    snapshot = build_global_map_snapshot(
        WorldMonitorBatch(items=items, categories={}, coverage=WorldMonitorCoverage(state="partial"), generated_at=now),
        target_date=real_stored_map["target_date"], cutoff=now,
    )
    for row in snapshot.events:
        if "Bolivia" in row.title or "flight" in row.title.lower():
            assert row.latitude is None
            assert row.country not in {"United States", "Israel"}
    assert not snapshot.upload_allowed


@pytest.mark.parametrize("field,value", [
    ("source_state", {}), ("source_state", []), ("target_date", []),
    ("cutoff", {}), ("events", {}),
])
def test_nested_malformed_metadata_always_returns_blocking_issues(metadata, field, value):
    metadata[field] = value
    assert review(metadata)


def test_extremely_large_coordinate_returns_issue_not_overflow(metadata):
    metadata["events"][0]["latitude"] = 10 ** 400
    assert review(metadata)


def test_same_evidence_under_different_keys_cannot_inflate_coverage(metadata):
    metadata["events"][1] = deepcopy(metadata["events"][0])
    metadata["events"][1]["event_key"] = "another-stored-key"
    assert any("MAP_COVERAGE_INSUFFICIENT" in issue for issue in review(metadata))


def test_ambiguous_source_cannot_be_repaired_from_generated_country_or_title(metadata):
    metadata["events"][0]["evidence"][0]["title"] = "Bolivia arrests official accused by U.S."
    metadata["events"][0]["title"] = "China opens a new factory"
    assert any("MAP_LOCATION_UNVERIFIED: event 1" in issue for issue in review(metadata))
