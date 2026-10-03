from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .geography import verified_location
from .models import EvidenceArticle, EventCandidate, VerifiedEvent


def build_verified_events(candidates: Iterable[EventCandidate], *, target_date: str, cutoff: datetime) -> list[VerifiedEvent]:
    groups: dict[str, list[EventCandidate]] = defaultdict(list)
    for candidate in candidates:
        if not _is_current(candidate.published_at, target_date, cutoff):
            continue
        if not candidate.title.strip() or not candidate.url.strip():
            continue
        if _is_retrospective_only(candidate, target_date):
            continue
        groups[_same_flight_diversion_key(candidate) or candidate.event_key or _normalise_key(candidate.title)].append(candidate)

    events = []
    for event_key, members in groups.items():
        representative = max(members, key=lambda item: item.published_at or datetime.min.replace(tzinfo=timezone.utc))
        lat, lon, precision = verified_location(
            representative.latitude,
            representative.longitude,
            representative.location_name,
            representative.location_precision,
        )
        evidence = [
            EvidenceArticle(
                title=item.title,
                source=item.source,
                url=item.url,
                published_at=item.published_at,
                summary=item.summary,
                authority=float(item.authority),
            )
            for item in members
        ]
        events.append(VerifiedEvent(
            event_key=event_key,
            title=representative.title,
            summary=representative.summary,
            country=representative.country,
            location_name=representative.location_name,
            latitude=lat,
            longitude=lon,
            evidence=evidence,
            publisher_count=len({item.publisher_family or item.source for item in members}),
            location_precision=precision,
            location_method=representative.location_method,
        ))
    return events


def _is_current(published_at: datetime | None, target_date: str, cutoff: datetime) -> bool:
    if published_at is None:
        return False
    instant = published_at if published_at.tzinfo else published_at.replace(tzinfo=timezone.utc)
    limit = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)
    if instant > limit:
        return False
    return instant.astimezone(timezone(timedelta(hours=8))).date().isoformat() == target_date


def _normalise_key(title: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", " ", title.lower()).strip()


def _is_retrospective_only(candidate: EventCandidate, target_date: str) -> bool:
    title = candidate.title.lower()
    summary = candidate.summary.lower()
    if re.search(r"\bwhen\b.{0,100}\b(?:wanted to|tried to|had planned to)\b", title):
        return True
    if not re.search(r"\b(?:when|quand)\b", title):
        return False
    years = [int(year) for year in re.findall(r"\b(?:in|en)\s+(20\d{2})\b", summary)]
    return bool(years and max(years) < int(target_date[:4]))


def _same_flight_diversion_key(candidate: EventCandidate) -> str:
    text = f"{candidate.title} {candidate.summary}".lower()
    if not re.search(r"\b(?:flight|aircraft|plane|jet|pilot|cockpit)\b", text):
        return ""
    if (
        "flydubai" in text
        and re.search(r"tel[ -]?aviv|israel", text)
        and re.search(r"\b(?:stab\w*|crash\w*|attack|divert\w*|distress)\b", text)
    ):
        return "flight-incident:flydubai:israel"
    if not re.search(r"\b(?:divert|emergency|land|distress signal|forced to land)\w*\b", text):
        return ""
    if not re.search(r"tel[ -]?aviv|israel", text) or not re.search(r"saudi", text):
        return ""
    return "flight-diversion:tel-aviv:saudi-arabia"
