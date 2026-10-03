from __future__ import annotations

import os
from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.integrations.worldmonitor.client import WorldMonitorClient, WorldMonitorError
from src.integrations.worldmonitor.runtime import WorldMonitorRuntime

from .workflow import create_global_map_post
from .models import GlobalMapRequest
from .workflow import build_global_map_snapshot
from .translate import translate_map_snapshot


def _service_client_and_runtime():
    base_url = (os.getenv("WORLDMONITOR_BASE_URL") or "http://127.0.0.1:3000").rstrip("/")
    client = WorldMonitorClient(base_url, timeout=float(os.getenv("WORLDMONITOR_TIMEOUT_S", "12")))
    root = (os.getenv("WORLDMONITOR_DIR") or "").strip()
    auto_start = str(os.getenv("WORLDMONITOR_AUTO_START", "0")).lower() in {"1", "true", "yes", "on"}
    parsed = urlsplit(base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 3000)
    runtime = WorldMonitorRuntime(client, root=Path(root) if root else None, auto_start=auto_start, port=port)
    return client, runtime


def _fetch_current_digest(client: WorldMonitorClient):
    """Use the English digest only when the Chinese batch is unavailable or stale."""
    primary = None
    primary_error = None
    try:
        primary = client.fetch_digest(reuse_cycle=False)
    except WorldMonitorError as exc:
        primary_error = exc
    if primary is not None and primary.items and not primary.coverage.served_stale and primary.coverage.state != "stale":
        return primary
    try:
        alternate = client.fetch_digest(lang="en", reuse_cycle=False)
    except WorldMonitorError:
        if primary is not None:
            return primary
        raise primary_error
    if alternate.items and not alternate.coverage.served_stale and alternate.coverage.state in {"complete", "partial"}:
        return alternate
    if primary is not None:
        return primary
    return alternate


def build_global_map_preview(*, client, runtime, request: GlobalMapRequest) -> dict[str, object]:
    try:
        probe = runtime.ensure_ready()
        if not probe.ready:
            raise RuntimeError(f"{probe.error_code or 'WM_NOT_READY'}: {probe.message}")
        # The readiness probe performs a request to the same client.  Refresh
        # here so a stale probe response can never become the map snapshot.
        batch = _fetch_current_digest(client)
        snapshot = build_global_map_snapshot(
            batch,
            target_date=request.target_date,
            cutoff=request.cutoff_at,
            max_events=request.max_events,
            map_mode=request.map_mode,
        )
        return {
            "frozen_scope": request.to_dict(),
            "source_state": batch.coverage.state,
            "served_stale": batch.coverage.served_stale,
            "coverage": {
                "raw_items": snapshot.raw_item_count,
                "independent_events": snapshot.independent_event_count,
                "eligible_events": len(snapshot.events),
                "located_events": snapshot.located_event_count,
                "countries": snapshot.country_count,
                "publishers": snapshot.publisher_count,
            },
            "quality_state": snapshot.coverage_status,
            "upload_allowed": snapshot.upload_allowed,
            "warning": snapshot.warning,
            "events": snapshot.to_dict()["events"],
        }
    finally:
        runtime.release()


def preview_global_map_from_service(*, request: GlobalMapRequest) -> dict[str, object]:
    client, runtime = _service_client_and_runtime()
    return build_global_map_preview(client=client, runtime=runtime, request=request)


def create_global_map_post_from_service(
    *,
    output_dir: Path | None = None,
    target_date: str | None = None,
    request: GlobalMapRequest | None = None,
) :
    enabled = str(os.getenv("GLOBAL_MAP_ENABLED", "0")).lower() in {"1", "true", "yes", "on"}
    if not enabled:
        raise RuntimeError("GLOBAL_MAP_DISABLED: set GLOBAL_MAP_ENABLED=1 before using the map workflow")
    client, runtime = _service_client_and_runtime()
    try:
        probe = runtime.ensure_ready()
        if not probe.ready:
            raise RuntimeError(f"{probe.error_code}: {probe.message}")
        # Do not reuse the readiness probe's cached response for publication.
        batch = _fetch_current_digest(client)
        if request is None:
            cutoff = datetime.now(timezone.utc)
            beijing = cutoff.astimezone(timezone(timedelta(hours=8)))
            request = GlobalMapRequest.from_mapping({
                "target_date": target_date or beijing.date().isoformat(),
                "cutoff_at": cutoff,
            })
        return create_global_map_post(
            batch,
            target_date=request.target_date,
            cutoff=request.cutoff_at,
            max_events=request.max_events,
            map_mode=request.map_mode,
            output_dir=output_dir or Path("data") / "global_map",
            translate_events=translate_map_snapshot,
        )
    finally:
        runtime.release()
