"""Read local draft references from a completed agent checkpoint."""

from __future__ import annotations

import json
import re
from pathlib import Path


_ITEM_KEY = re.compile(r"^\d+:([a-f0-9]{32}):[a-f0-9]+$")


def local_draft_ids(runtime_root: Path, run_id: str) -> list[str]:
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        return []
    path = runtime_root / "data/runs/agent" / run_id / "checkpoint.json"
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found: list[str] = []
    for key, status in (checkpoint.get("item_status") or {}).items():
        match = _ITEM_KEY.fullmatch(key)
        if status != "skipped_local" or match is None:
            continue
        post_id = match.group(1)
        if (runtime_root / "data/posts" / post_id / "post.json").is_file() and post_id not in found:
            found.append(post_id)
    return found
