"""Rebase known local metadata paths in copied post records only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _slot(document: dict, *keys: str) -> tuple[dict, str] | None:
    node = document
    for key in keys[:-1]:
        node = node.get(key)
        if not isinstance(node, dict):
            return None
    return (node, keys[-1]) if keys[-1] in node else None


SLOTS = (
    ("platform", "news", "source_api", "file_path"),
    ("platform", "news", "manual_materials", "file_path"),
    ("platform", "ai_digest", "source_meta", "research_materials", "path"),
    ("platform", "xhs_draft", "profile"),
)


def rebase_post(path: Path, old_root: Path, runtime_root: Path) -> int:
    document = json.loads(path.read_text(encoding="utf-8"))
    changed = 0
    for keys in SLOTS:
        found = _slot(document, *keys)
        if not found:
            continue
        node, key = found
        value = node[key]
        if not isinstance(value, str):
            continue
        candidate = Path(value)
        if not candidate.is_absolute() or not candidate.is_relative_to(old_root):
            continue
        node[key] = str(runtime_root / candidate.relative_to(old_root))
        changed += 1
    if changed:
        temporary = path.with_name(path.name + ".rebase-tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args()
    old_root = args.old_root.resolve()
    runtime_root = args.runtime_root.resolve()
    if old_root == runtime_root or not (runtime_root / "data/posts").is_dir():
        raise ValueError("Separate existing runtime data/posts is required")
    changed_files = 0
    changed_fields = 0
    for path in (runtime_root / "data/posts").rglob("post.json"):
        count = rebase_post(path, old_root, runtime_root)
        changed_files += count > 0
        changed_fields += count
    print(f"Rebased {changed_fields} fields in {changed_files} copied post records")


if __name__ == "__main__":
    main()
