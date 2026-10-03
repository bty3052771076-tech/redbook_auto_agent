"""Content-addressed visual review reuse; file timestamps are not evidence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from src.storage.models import Post


REVIEW_CACHE_VERSION = "news-vision-v3-full-input"


def reviewer_configuration() -> dict[str, str]:
    return {
        name: os.getenv(name, "").strip()
        for name in (
            "VLM_REVIEW_PROVIDER", "VLM_REVIEW_MODEL", "MINIMAX_VLM_MODEL",
            "MINIMAX_LLM_MODEL", "VOLCENGINE_VLM_MODEL", "ALIYUN_VLM_MODEL",
            "MINIMAX_BASE_URL", "MINIMAX_LLM_BASE_URL", "VOLCENGINE_LLM_BASE_URL",
            "ARK_BASE_URL", "ALIYUN_LLM_BASE_URL",
        )
    }


def visual_review_fingerprint(post: Post, viewpoint: str = "") -> str:
    images = []
    for asset in post.assets:
        if asset.kind != "image":
            continue
        path = Path(asset.path)
        if not path.is_file() or path.stat().st_size == 0:
            return ""
        images.append(hashlib.sha256(path.read_bytes()).hexdigest())
    if not images:
        return ""
    payload = {
        "version": REVIEW_CACHE_VERSION,
        "title": post.title,
        "body": post.body,
        "topics": post.topics,
        "viewpoint": viewpoint,
        "images": images,
        "event": (post.platform.get("news") or {}).get("picked"),
        "image_event": (post.platform.get("news") or {}).get("image_event"),
        "generation_prompt": (post.platform.get("image") or {}).get("prompt"),
        # Only non-secret configuration participates; changing reviewers must
        # invalidate their previous decision without storing any credential.
        "reviewer_config": reviewer_configuration(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def cached_vision_matches(post: Post, vision: object, viewpoint: str = "") -> bool:
    if not isinstance(vision, dict) or vision.get("cache_version") != REVIEW_CACHE_VERSION:
        return False
    fingerprint = visual_review_fingerprint(post, viewpoint)
    return bool(fingerprint and fingerprint == vision.get("content_fingerprint"))


def stamp_vision_cache(post: Post, vision: dict, viewpoint: str = "") -> dict:
    return {
        **vision,
        "cache_version": REVIEW_CACHE_VERSION,
        "content_fingerprint": visual_review_fingerprint(post, viewpoint),
    }
