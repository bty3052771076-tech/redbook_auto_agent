"""Write-once first-image evidence; persistence belongs to the caller."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.storage.models import Post, now_iso


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lineage(post: Post) -> dict[str, Any]:
    value = post.platform.get("image_lineage")
    return value if isinstance(value, dict) else {}


def get_first_image_identity(post: Post) -> dict[str, Any] | None:
    """Return a detached snapshot, never reconstruct history from current metadata."""
    value = _lineage(post).get("first_image")
    return deepcopy(value) if isinstance(value, dict) else None


def record_initial_news_image(
    post: Post,
    *,
    image_path: str | Path,
    image_meta: Mapping[str, Any],
) -> bool:
    """Call only at new-post creation, with the initial generation result.

    Never call on loaded/resumed posts to backfill missing history. Read errors
    propagate before any mutation; an existing identity is never overwritten.
    """
    lineage = _lineage(post)
    if "first_image" in lineage:
        return False
    if "image_lineage" in post.platform and not isinstance(
        post.platform["image_lineage"], dict
    ):
        return False
    prompt = image_meta.get("prompt")
    news = post.platform.get("news")
    audit = news.get("image_event_audit") if isinstance(news, dict) else None
    identity = {
        "post_id": post.id,
        "image_path": str(image_path),
        "image_sha256": _file_sha256(image_path),
        "prompt_version": image_meta.get("prompt_version"),
        "prompt_hash": (
            hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            if isinstance(prompt, str) and prompt
            else None
        ),
        "scene_audit_version": audit.get("version") if isinstance(audit, dict) else None,
        "provider": image_meta.get("provider"),
        "model": image_meta.get("model"),
        "captured_at": now_iso(),
    }
    post.platform.setdefault("image_lineage", {"schema_version": 1})[
        "first_image"
    ] = deepcopy(identity)
    return True


def record_first_image_review(
    post: Post,
    *,
    ok: bool,
    score: int | float | None,
    provider: str | None,
    model: str | None,
    issues: Iterable[str] | None,
) -> bool:
    """Capture the original verdict before repair, only for matching file bytes.

    Does not save the post, infer a verdict, or change any quality threshold.
    Missing/unreadable/replaced images and already-recorded reviews return False.
    """
    lineage = _lineage(post)
    identity = lineage.get("first_image")
    if not isinstance(identity, dict) or "first_review" in lineage:
        return False
    if identity.get("post_id") != post.id or not identity.get("image_sha256"):
        return False
    if not post.assets or post.assets[0].kind != "image":
        return False
    if not isinstance(ok, bool):
        return False
    if score is not None and (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
    ):
        return False
    try:
        current_hash = _file_sha256(post.assets[0].path)
    except (OSError, ValueError):
        return False
    if current_hash != identity["image_sha256"]:
        return False
    lineage["first_review"] = {
        "image_sha256": current_hash,
        "ok": ok,
        "score": score,
        "provider": provider,
        "model": model,
        "issues": [issues] if isinstance(issues, str) else list(issues or ()),
        "recorded_at": now_iso(),
    }
    return True


def first_image_pass_summary(posts: Iterable[Post]) -> dict[str, Any]:
    """Summarize all candidates, not only accepted posts; no disk reads/writes.

    Supply one snapshot per post ID, including rejected candidates and legacy
    posts. Unknown identity/version stays unknown; current image/quality_gate
    metadata is never used to invent first-image evidence.
    Passing requires both ok=True and a finite numeric score of at least 70.
    """
    def counters() -> dict[str, Any]:
        return dict(candidates=0, reviewed=0, passed=0, failed=0, pending=0, identity_missing=0)

    def rates(counts: dict[str, Any]) -> dict[str, Any]:
        candidates, reviewed = counts["candidates"], counts["reviewed"]
        return {
            **counts,
            "pass_rate": counts["passed"] / candidates if candidates else None,
            "reviewed_pass_rate": counts["passed"] / reviewed if reviewed else None,
            "complete": counts["pending"] == 0,
            "above_50_percent": (
                counts["passed"] * 2 > candidates
                if candidates and counts["pending"] == 0
                else None
            ),
        }

    total = counters()
    groups: dict[str, dict[str, Any]] = {"unknown": counters()}
    seen: set[str] = set()
    for post in posts:
        if post.id in seen:
            raise ValueError("first_image_pass_summary requires unique post IDs")
        seen.add(post.id)
        lineage = _lineage(post)
        identity = lineage.get("first_image")
        known = (
            isinstance(identity, dict)
            and identity.get("post_id") == post.id
            and bool(identity.get("image_sha256"))
        )
        version = identity.get("prompt_version") if known else None
        version = version.strip() if isinstance(version, str) and version.strip() else "unknown"
        review = lineage.get("first_review")
        reviewed = (
            known
            and isinstance(review, dict)
            and review.get("image_sha256") == identity["image_sha256"]
            and isinstance(review.get("ok"), bool)
        )
        score = review.get("score") if reviewed else None
        passed = (
            reviewed
            and review["ok"]
            and not isinstance(score, bool)
            and isinstance(score, (int, float))
            and (not isinstance(score, float) or math.isfinite(score))
            and score >= 70
        )
        group = groups.setdefault(version, counters())
        for counts in (total, group):
            counts["candidates"] += 1
            counts["identity_missing"] += int(not known)
            if reviewed:
                counts["reviewed"] += 1
                counts["passed" if passed else "failed"] += 1
            else:
                counts["pending"] += 1
    return {
        "total": rates(total),
        "by_prompt_version": {version: rates(counts) for version, counts in groups.items()},
    }
