"""Durable, revision-bound evidence for the one permitted image redraw."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.storage.models import Post
from src.workflow.review_cache import (
    REVIEW_CACHE_VERSION, reviewer_configuration, visual_review_fingerprint,
)


IMAGE_REPAIR_VERSION = "bounded-image-repair-v1"


def image_asset_key(post: Post) -> str:
    images = []
    for asset in post.assets:
        if asset.kind != "image":
            continue
        path = Path(asset.path)
        if not path.is_file() or path.stat().st_size == 0:
            return ""
        images.append((str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(images).encode("utf-8")).hexdigest() if images else ""


def image_repair_content_key(post: Post, viewpoint: str = "") -> str:
    news = post.platform.get("news")
    payload = {
        "version": IMAGE_REPAIR_VERSION,
        "review_version": REVIEW_CACHE_VERSION,
        "post_id": post.id,
        "title": post.title,
        "body": post.body,
        "topics": post.topics,
        "event": news.get("picked") if isinstance(news, dict) else None,
        "viewpoint": viewpoint,
        "reviewer_config": reviewer_configuration(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def image_candidate_snapshot(post: Post, viewpoint: str = "") -> dict[str, Any]:
    fingerprint = visual_review_fingerprint(post, viewpoint)
    if not fingerprint:
        raise ValueError("IMAGE_REPAIR_ASSET_MISSING: cannot retain an unreadable image")
    payload = post.model_dump(mode="json")
    payload["platform"].pop("image_repair", None)
    return {"fingerprint": fingerprint, "asset_key": image_asset_key(post), "post": payload}


def restore_image_candidate(post: Post, candidate: dict[str, Any], viewpoint: str = "") -> None:
    retained = Post.model_validate(candidate["post"])
    if (retained.id != post.id
            or image_repair_content_key(retained, viewpoint) != image_repair_content_key(post, viewpoint)
            or not candidate.get("fingerprint")
            or visual_review_fingerprint(retained, viewpoint) != candidate["fingerprint"]):
        raise ValueError("IMAGE_REPAIR_ASSET_CHANGED: retained candidate is not valid for this revision")
    # Only restore picture fields. Never roll back delivery receipts or post status.
    post.assets = deepcopy(retained.assets)
    for key in ("images", "image", "image_fallback"):
        if key in retained.platform:
            post.platform[key] = deepcopy(retained.platform[key])
        else:
            post.platform.pop(key, None)
    retained_news = retained.platform.get("news")
    if isinstance(retained_news, dict) and isinstance(post.platform.get("news"), dict):
        for key in ("image_event", "image_event_audit"):
            if key in retained_news:
                post.platform["news"][key] = deepcopy(retained_news[key])
            else:
                post.platform["news"].pop(key, None)


def retained_image_repair(post: Post, viewpoint: str = "") -> dict[str, Any] | None:
    value = post.platform.get("image_repair")
    if not isinstance(value, dict) or value.get("version") != IMAGE_REPAIR_VERSION:
        return None
    if value.get("content_key") != image_repair_content_key(post, viewpoint):
        return None
    if not isinstance(value.get("first"), dict) or not value["first"].get("fingerprint"):
        raise ValueError("IMAGE_REPAIR_EVIDENCE_INVALID: missing first candidate")
    phase = value.get("phase")
    if phase == "complete":
        selected = value.get("selected_index", 1)
        candidate = value.get("second") if selected == 2 else value["first"]
        if not isinstance(candidate, dict):
            raise ValueError("IMAGE_REPAIR_EVIDENCE_INVALID: missing selected candidate")
        if visual_review_fingerprint(post, viewpoint) != candidate.get("fingerprint"):
            # A changed scene/prompt/file is not the old completed review, even
            # when title/body are untouched. Never restore over a user's edit.
            return None
    elif phase == "redraw_requested":
        if (visual_review_fingerprint(post, viewpoint) != value["first"]["fingerprint"]
                and image_asset_key(post) == value["first"].get("asset_key")):
            raise ValueError("IMAGE_REPAIR_EVIDENCE_CHANGED: metadata edit is not proof of a saved redraw")
    return deepcopy(value)


def merge_pending_local_image(post: Post, local: Post) -> bool:
    """Recover the narrow file-save/PG-commit window, not arbitrary local edits."""
    value = post.platform.get("image_repair")
    local_value = local.platform.get("image_repair")
    if not isinstance(value, dict) or not isinstance(local_value, dict):
        return False
    if value.get("phase") != "redraw_requested" or value.get("version") != IMAGE_REPAIR_VERSION:
        return False
    viewpoint = str(value.get("viewpoint") or "")
    key = image_repair_content_key(post, viewpoint)
    if (post.id != local.id or value.get("content_key") != key
            or local_value.get("content_key") != key
            or image_repair_content_key(local, viewpoint) != key
            or value.get("first") != local_value.get("first")
            or local_value.get("phase") not in {"redraw_requested", "redraw_saved", "second_reviewed", "complete"}):
        return False
    current = visual_review_fingerprint(local, viewpoint)
    if (not current or current == value["first"].get("fingerprint")
            or image_asset_key(local) == value["first"].get("asset_key")):
        return False
    candidate = image_candidate_snapshot(local, viewpoint)
    restore_image_candidate(post, candidate, viewpoint)
    post.platform["image_repair"] = deepcopy(local_value)
    return True
