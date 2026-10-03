from __future__ import annotations

import os
from pathlib import Path


def configure_runtime() -> Path:
    root = Path(os.getenv("REDBOOK_RUNTIME_ROOT") or r"E:\AI\codex\redbook_runtime").resolve()
    if root.drive.upper() != "E:" or not root.is_dir():
        raise RuntimeError("REDBOOK_RUNTIME_ROOT must be an existing directory on E:")
    credentials = root / "data/knowledge/postgresql-local/connection.json"
    if not credentials.is_file():
        raise RuntimeError("Independent PostgreSQL credentials are missing")
    os.environ["REDBOOK_RUNTIME_ROOT"] = str(root)
    os.environ["KNOWLEDGE_DB_CREDENTIALS"] = str(credentials)
    os.environ.setdefault("KNOWLEDGE_EMBEDDING_CACHE", str(root / "data/models/fastembed"))
    os.environ["ALLOW_PAID_LLM_FALLBACK"] = "0"
    os.environ.setdefault("GLOBAL_MAP_BASEMAP_PATH", r"E:\AI\codex\worldmonitor\public\data\countries.geojson")
    return root
