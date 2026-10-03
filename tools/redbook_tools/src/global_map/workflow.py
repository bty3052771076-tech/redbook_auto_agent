from __future__ import annotations

import json
import hashlib
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from src.integrations.worldmonitor.models import WorldMonitorBatch
from src.storage.models import AssetInfo, Post, PostStatus

from .editorial import build_global_map_editorial
from .basemap import load_basemap
from .evidence import build_verified_events
from .geography import is_flight_incident_title, resolve_event_country_location
from .models import EventCandidate, MapSnapshot
from .render import render_global_map
from .review import stored_global_map_review_issues
from .select import select_events


def build_global_map_snapshot(
    batch: WorldMonitorBatch,
    *,
    target_date: str,
    cutoff: datetime,
    max_events: int = 8,
    map_mode: str = "coordinate-grid",
    basemap_path: str | Path | None = None,
) -> MapSnapshot:
    candidates = []
    for item in batch.items:
        country = str(item.raw.get("country") or "").strip()
        location_name = item.location_name
        latitude = item.latitude
        longitude = item.longitude
        location_precision = ""
        location_method = ""
        flight_incident = is_flight_incident_title(item.title)
        if flight_incident:
            resolved = resolve_event_country_location(item.title, item.snippet, basemap_path=basemap_path)
            country = resolved.country if resolved else ""
            location_name = country
            latitude = resolved.latitude if resolved else None
            longitude = resolved.longitude if resolved else None
            location_precision = resolved.precision if resolved else "unknown"
            location_method = resolved.method if resolved else ""
        elif latitude is None or longitude is None:
            resolved = resolve_event_country_location(item.title, basemap_path=basemap_path)
            if resolved is not None:
                country = country or resolved.country
                location_name = location_name or f"{resolved.country}（国家级）"
                latitude = resolved.latitude
                longitude = resolved.longitude
                location_precision = resolved.precision
                location_method = resolved.method
            else:
                # Country hints are not verified event locations. Do not keep
                # a conflicting hint after the text resolver rejected it.
                country = location_name = ""
                latitude = longitude = None
                location_precision = "unknown"
        else:
            location_precision = "city"
            location_method = "explicit_coordinates"
        candidates.append(EventCandidate(
            event_key=item.raw.get("storyId") or item.raw.get("story_id") or item.item_id or item.title,
            title=item.title,
            summary=item.snippet,
            source=item.source,
            url=item.url,
            published_at=item.published_at,
            country=country,
            location_name=location_name,
            latitude=latitude,
            longitude=longitude,
            location_precision=location_precision,
            location_method=location_method,
            authority=_authority(item),
            publisher_family=str(item.raw.get("originPublisher") or item.source),
        ))
    events = build_verified_events(candidates, target_date=target_date, cutoff=cutoff)
    return select_events(
        events,
        max_events=max_events,
        target_date=target_date,
        cutoff=cutoff,
        raw_item_count=len(batch.items),
        source_state=batch.coverage.state,
        map_mode=map_mode,
    )


def save_snapshot(snapshot: MapSnapshot, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def render_snapshot(snapshot: MapSnapshot, path: Path, *, basemap_path: str | Path | None = None) -> Path:
    return render_global_map(snapshot, path, basemap_path=basemap_path)


def create_global_map_post(
    batch: WorldMonitorBatch,
    *,
    target_date: str,
    cutoff: datetime,
    output_dir: Path,
    max_events: int = 8,
    map_mode: str = "coordinate-grid",
    basemap_path: str | Path | None = None,
    translate_events: Callable[[MapSnapshot], MapSnapshot] | None = None,
) -> Post | None:
    snapshot = build_global_map_snapshot(
        batch,
        target_date=target_date,
        cutoff=cutoff,
        max_events=max_events,
        map_mode=map_mode,
        basemap_path=basemap_path,
    )
    output_dir = Path(output_dir)
    if snapshot.upload_allowed:
        issues = stored_global_map_review_issues(snapshot, basemap_path=basemap_path)
        if issues:
            snapshot = replace(snapshot, upload_allowed=False, coverage_status="blocked", warning="; ".join(issues))
    if snapshot.upload_allowed and translate_events is not None:
        snapshot = translate_events(snapshot)
        issues = stored_global_map_review_issues(snapshot, basemap_path=basemap_path)
        if issues:
            snapshot = replace(snapshot, upload_allowed=False, coverage_status="blocked", warning="; ".join(issues))
    basemap = load_basemap(basemap_path)
    map_path = render_snapshot(snapshot, output_dir / f"global-map-{target_date}.png", basemap_path=basemap_path)
    save_snapshot(snapshot, output_dir / f"global-map-{target_date}.json")
    if not snapshot.upload_allowed:
        return None

    editorial = build_global_map_editorial(snapshot)
    located_events = [
        event for event in snapshot.events
        if event.latitude is not None and event.longitude is not None
    ]
    body_lines = [
        str(editorial["summary"]),
        f"时间范围：北京时间 {target_date} 00:00 至 {cutoff.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')}。",
        "以下是本轮信源中已核验、可定位的代表性事件；地图不代表全球全部事件或风险真值。",
    ]
    event_summaries = [event.summary.strip() for event in located_events]
    footer = "完整证据和来源记录已保存在本地任务目录。"

    def compose_body() -> str:
        lines = list(body_lines)
        for index, (event, summary) in enumerate(zip(located_events, event_summaries), 1):
            line = f"{index}. {event.title.rstrip('。')}。"
            if summary:
                line += f"{summary.rstrip('。')}。"
            lines.append(line)
        lines.append(footer)
        return "\n".join(lines)

    body = compose_body()
    for index in range(len(event_summaries) - 1, -1, -1):
        if len(body) <= 980:
            break
        event_summaries[index] = ""
        body = compose_body()
    if len(body) > 980:
        raise RuntimeError("MAP_BODY_TOO_LONG: complete event titles do not fit the platform limit")
    map_sha256 = hashlib.sha256(map_path.read_bytes()).hexdigest()
    return Post(
        title=str(editorial["title"]),
        body=body,
        status=PostStatus.draft,
        assets=[AssetInfo(path=str(map_path), kind="image", size_bytes=map_path.stat().st_size, sha256=map_sha256, validated=True)],
        platform={
            "global_map": snapshot.to_dict(),
            "editorial": editorial,
            "basemap": basemap.to_dict(),
            "render_report": {"map_sha256": map_sha256, "basemap_sha256": basemap.sha256, "feature_count": basemap.feature_count},
        },
    )


def _authority(item) -> float:
    if item.credibility_score is not None:
        return max(0.0, min(1.0, item.credibility_score))
    return 0.8 if item.raw.get("originPublisherTrusted") else 0.5


def _compact_text(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _bounded_body(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    suffix = "\n完整证据和来源记录已保存在本地任务目录。"
    return text[: max(1, limit - len(suffix))].rstrip() + suffix
