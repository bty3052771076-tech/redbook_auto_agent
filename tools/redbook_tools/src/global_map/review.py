"""Read-only semantic checks for saved maps and generation boundaries."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path
from urllib.parse import urlsplit

from .geography import is_flight_incident_title, resolve_event_country_location
from .models import MapSnapshot


_BEIJING = timezone(timedelta(hours=8))


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _valid_coordinates(latitude: object, longitude: object) -> bool:
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (latitude, longitude)):
        return False
    return (
        -90 <= latitude <= 90 and -180 <= longitude <= 180
        and math.isfinite(latitude) and math.isfinite(longitude)
        and (latitude != 0 or longitude != 0)
    )


def _fresh_evidence(row: dict, target_date: str, cutoff: datetime) -> list[dict]:
    evidence = row.get("evidence")
    if not isinstance(evidence, list):
        return []
    fresh = []
    for article in evidence:
        if not isinstance(article, dict) or any(
            not isinstance(article.get(field), str) or not article[field].strip()
            for field in ("title", "source", "url")
        ):
            continue
        try:
            url = urlsplit(article["url"])
            if url.scheme not in {"http", "https"} or not url.hostname:
                continue
        except ValueError:
            continue
        instant = _instant(article.get("published_at"))
        if instant is None or instant > cutoff:
            continue
        try:
            if instant.astimezone(_BEIJING).date().isoformat() != target_date:
                continue
        except OverflowError:
            continue
        if not isinstance(article.get("summary", ""), str):
            continue
        fresh.append(article)
    return fresh


def _grounded_location(row: dict, evidence: list[dict], basemap_path: str | Path | None) -> bool:
    latitude, longitude = row.get("latitude"), row.get("longitude")
    country = row.get("country")
    if not _valid_coordinates(latitude, longitude) or not isinstance(country, str) or not country.strip():
        return False
    precise = row.get("location_precision") == "city" and row.get("location_method") == "explicit_coordinates"
    flight = any(is_flight_incident_title(article["title"]) for article in evidence)
    # Reliable upstream nonflight coordinates do not need country-name inference.
    # A destination label must still not turn flight coordinates into evidence.
    if precise and not flight:
        return True
    if not precise and (row.get("location_precision") != "country" or row.get("location_method") != "explicit_country_name"):
        return False
    for article in evidence:
        resolved = resolve_event_country_location(
            article["title"], article.get("summary", ""), basemap_path=basemap_path,
        )
        if resolved is None or resolved.country != country:
            return False
        if not precise and (
            not math.isclose(latitude, resolved.latitude, abs_tol=0.0001, rel_tol=0)
            or not math.isclose(longitude, resolved.longitude, abs_tol=0.0001, rel_tol=0)
        ):
            return False
    return True


def stored_global_map_review_issues(
    metadata: object, *, basemap_path: str | Path | None = None,
) -> list[str]:
    """Audit ``post.platform['global_map']`` or a MapSnapshot without repair.

    Return blocking diagnostics with original 1-based event indices. No network,
    database, model, rendering or writes; only the local country-name catalog is
    read for inferred anchors. Empty output is not a remote-save or image check.
    """
    if isinstance(metadata, MapSnapshot):
        try:
            metadata = metadata.to_dict()
        except (AttributeError, TypeError, ValueError):
            return ["MAP_METADATA_INVALID: snapshot cannot be read"]
    if not isinstance(metadata, dict):
        return ["MAP_METADATA_INVALID: missing stored map snapshot"]
    issues = []
    if metadata.get("upload_allowed") is not True:
        issues.append("MAP_UPLOAD_BLOCKED: snapshot is not explicitly eligible")
    source_state = metadata.get("source_state")
    if not isinstance(source_state, str) or source_state not in {"ready", "complete", "partial"}:
        issues.append("MAP_SOURCE_NOT_FRESH: source state cannot authorize publication")
    target_date = metadata.get("target_date")
    cutoff = _instant(metadata.get("cutoff"))
    try:
        valid_date = isinstance(target_date, str) and date.fromisoformat(target_date).isoformat() == target_date
    except ValueError:
        valid_date = False
    if not valid_date or cutoff is None:
        return issues + ["MAP_METADATA_INVALID: invalid frozen date or cutoff"]
    events = metadata.get("events")
    if not isinstance(events, list):
        return issues + ["MAP_METADATA_INVALID: missing event list"]
    seen_keys = set()
    seen_evidence = set()
    countries = set()
    located_count = 0
    for index, row in enumerate(events, 1):
        if not isinstance(row, dict):
            issues.append(f"MAP_METADATA_INVALID: event {index} is not an event object")
            continue
        if row.get("latitude") is None and row.get("longitude") is None and row.get("location_precision") == "unknown":
            continue
        key = row.get("event_key")
        if not isinstance(key, str) or not key.strip():
            issues.append(f"MAP_METADATA_INVALID: event {index} has no identity")
            continue
        evidence = _fresh_evidence(row, target_date, cutoff)
        if not evidence:
            issues.append(f"MAP_EVIDENCE_UNVERIFIED: event {index} has no fresh traceable source")
            continue
        if not _grounded_location(row, evidence, basemap_path):
            issues.append(f"MAP_LOCATION_UNVERIFIED: event {index} anchor is not supported by source evidence")
            continue
        identities = {(article["url"], article["title"]) for article in evidence}
        if key in seen_keys or identities & seen_evidence:
            issues.append(f"MAP_EVENT_DUPLICATE: event {index} cannot increase coverage")
            continue
        seen_keys.add(key)
        seen_evidence.update(identities)
        located_count += 1
        countries.add(row["country"])
    if located_count < 3 or len(countries) < 2:
        issues.append("MAP_COVERAGE_INSUFFICIENT: fewer than 3 grounded events or 2 countries")
    return issues
