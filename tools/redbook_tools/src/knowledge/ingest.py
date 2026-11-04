from __future__ import annotations

from datetime import datetime, timezone
import json
import hashlib
from itertools import islice
from pathlib import Path
from typing import Any

from src.workflow.resources import read_state, shared_lock, write_state
from src.storage.models import Post

from .models import KnowledgeDocument
from .store import KnowledgeStore


def _purposes(post: Post) -> tuple[str, ...]:
    visibility = str((post.platform or {}).get("publish", {}).get("visibility") or "unknown").lower()
    status = str(post.status.value if hasattr(post.status, "value") else post.status)
    purposes = ["duplicate_reference", "operational_case"]
    if status in {"published", "saved_as_draft"} and visibility not in {"private", "unknown"}:
        purposes.append("performance")
    if status not in {"failed", "canceled"}:
        purposes.append("style_example")
    # Generated copy is useful for duplicate/style checks, never factual evidence.
    return tuple(purposes)


def document_from_post(post: Post) -> KnowledgeDocument:
    status = str(post.status.value if hasattr(post.status, "value") else post.status)
    visibility = str((post.platform or {}).get("publish", {}).get("visibility") or "unknown")
    metadata = {
        "post_type": str(post.type.value if hasattr(post.type, "value") else post.type),
        "topics": list(post.topics or []),
        "asset_count": len(post.assets or []),
        "platform_keys": sorted(str(key) for key in (post.platform or {}).keys()),
    }
    return KnowledgeDocument(
        record_id=f"post-{post.id}",
        record_type="post",
        title=post.title,
        body=post.body,
        source_url=str((post.platform or {}).get("news", {}).get("source_url") or ""),
        source_published_at=str((post.platform or {}).get("news", {}).get("published_at") or ""),
        observed_at=post.updated_at or post.created_at,
        status=status,
        visibility=visibility,
        allowed_purposes=_purposes(post),
        metadata=metadata,
    )


def ingest_posts(store: KnowledgeStore, *, data_root: Path = Path("data")) -> dict[str, Any]:
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    total = 0
    manifest_path = data_root / "knowledge/ingest-manifest.json"
    paths = (path / "post.json" for path in (data_root / "posts").iterdir()) if (data_root / "posts").is_dir() else iter(())
    with shared_lock(manifest_path.with_suffix(".lock")):
        manifest = read_state(manifest_path)
        cached = manifest.get("files", {}) if manifest.get("format") == 1 else {}
        current = {}
        while batch := list(islice(paths, 32)):
            candidates = []
            for path in batch:
                if not path.is_file():
                    continue
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                candidates.append((path, digest.hexdigest(), cached.get(path.parent.name, {})))
            versions = store.current_versions([item[2]["record_id"] for item in candidates if item[2].get("record_id")])
            documents = []
            for path, digest, previous in candidates:
                if previous.get("file_hash") == digest and versions.get(previous.get("record_id")) == previous.get("content_hash") and previous.get("record_id"):
                    current[path.parent.name] = previous
                    counts["unchanged"] += 1
                    total += 1
                    continue
                try:
                    raw = path.read_bytes()
                    document = document_from_post(Post.model_validate_json(raw))
                    digest = hashlib.sha256(raw).hexdigest()
                    del raw
                except (OSError, ValueError):
                    continue
                documents.append(document)
                record = document.to_record()
                current[path.parent.name] = {"file_hash": digest, "record_id": record["record_id"],
                                             "content_hash": record["content_hash"]}
                total += 1
            if documents:
                result = store.upsert_documents(documents)
                for key in counts:
                    counts[key] += result[key]
        write_state(manifest_path, {"format": 1, "files": current})
    return {
        "domain": "posts",
        "input_count": total,
        "documents": total,
        **counts,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
