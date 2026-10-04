from __future__ import annotations

import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import app as module
from backend.app import app
from backend.evidence import source_evidence
from backend.runs import local_draft_ids


BASE = "http://127.0.0.1:8786"
TOOLS = Path(__file__).resolve().parents[1] / "tools/redbook_tools"
OLD = Path(r"E:\AI\codex\redbook_workflow")


def test_wool_library_routes_use_isolated_assets_and_require_human_confirmation(tmp_path):
    import json
    from PIL import Image
    batch = tmp_path / "assets/wool/候选原图/test"
    batch.mkdir(parents=True)
    Image.new("RGB", (96, 128), "coral").save(batch / "sample.png")
    (batch / "manifest.json").write_text(json.dumps({"images": [{"filename": "sample.png", "rating": "s"}]}), encoding="utf-8")
    with TestClient(app, base_url=BASE) as client:
        assert client.get("/api/wool-library").status_code == 403
        client.post("/api/session", headers={"X-Workbench": "1"})
        identity = client.get("/api/wool-library").json()["rows"][0]["id"]
        assert client.post("/api/wool-library/review", json={"id": identity, "decision": "approve"}).status_code == 403
        response = client.post("/api/wool-library/review", headers={"X-Workbench": "1"}, json={"id": identity, "decision": "approve"})
        assert response.status_code == 400
        response = client.post("/api/wool-library/review", headers={"X-Workbench": "1"}, json={"id": identity, "decision": "approve", "adult_confirmed": True, "rights_confirmed": True, "non_explicit_confirmed": True})
        assert response.json()["status"] == "approved"
        assert client.get(f"/api/wool-library/images/{identity}").content.startswith(b"\x89PNG")


@pytest.fixture(autouse=True)
def isolate_api_writes(tmp_path, monkeypatch):
    from apps.web_service import Workbench

    class Conversations:
        def __init__(self):
            self.rows = {}

        def get(self, key):
            return deepcopy(self.rows[key])

        def save(self, row):
            self.rows[row["id"]] = deepcopy(row)
            return deepcopy(row)

        def list(self, *, limit=100):
            return list(deepcopy(self.rows).values())[:limit]

    current = Workbench(tmp_path, conversation_store=Conversations())
    # Do not construct Workbench on the live root: its recovery rewrites active jobs.
    read_only = Workbench.__new__(Workbench)
    read_only.root = module.RUNTIME
    read_only.directory = module.RUNTIME / "data/web_gui"
    for name in ("posts", "post", "models", "providers", "environment"):
        monkeypatch.setattr(current, name, getattr(read_only, name))
    monkeypatch.setattr(current, "_run", lambda *args: pytest.fail("API regression started a worker"))
    monkeypatch.setattr(module.app.state, "service", current)
    monkeypatch.setattr(module, "ensure_review_schema", lambda: None)


def test_api_session_and_browser_origin_guard():
    with TestClient(app, base_url=BASE) as client:
        navigation = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        assert client.get("/", headers=navigation).status_code == 200
        assert client.get("/api/health", headers=navigation).status_code == 403
        assert client.get("/api/conversations").status_code == 403
        assert client.post("/api/session", headers={"X-Workbench": "1", "Origin": "https://example.com"}).status_code == 403
        assert client.post("/api/session", headers={"X-Workbench": "1", **navigation}).status_code == 403
        assert client.post("/api/session", headers={"X-Workbench": "1"}).status_code == 200
        assert client.get("/api/conversations").status_code == 200
        assert client.post("/api/conversations", json={"title": "blocked"}).status_code == 403


def test_conversation_plan_requires_explicit_confirmation():
    with TestClient(app, base_url=BASE) as client:
        client.post("/api/session", headers={"X-Workbench": "1"})
        created = client.post("/api/conversations", headers={"X-Workbench": "1"}, json={"title": "独立智能体测试"})
        assert created.status_code == 200
        conversation_id = created.json()["id"]
        result = client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers={"X-Workbench": "1"},
            json={"content": "生成1条每日新闻并保存到小红书草稿箱，不公开发布"},
        )
        assert result.status_code == 200
        plan = result.json()["plan"]
        assert plan["executable"] is True
        assert plan["delivery"] == "save_draft"
        assert plan["jobs"][0]["count"] == 1
        assert client.get(f"/api/conversations/{conversation_id}").json()["runs"] == []


def test_public_publish_request_is_rejected_before_execution():
    with TestClient(app, base_url=BASE) as client:
        client.post("/api/session", headers={"X-Workbench": "1"})
        created = client.post("/api/conversations", headers={"X-Workbench": "1"}, json={"title": "发布边界测试"})
        conversation_id = created.json()["id"]
        result = client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers={"X-Workbench": "1"},
            json={"content": "查看小红书创作者中心的已有草稿并公开发布"},
        )
        assert result.status_code == 400
        assert "人工确认" in result.json()["error"]
        assert client.get(f"/api/conversations/{conversation_id}").json()["runs"] == []


def test_independent_database_models_and_copied_drafts():
    with TestClient(app, base_url=BASE) as client:
        client.post("/api/session", headers={"X-Workbench": "1"})
        health = client.get("/api/health").json()
        assert health["database"]["documents"] >= 3344
        connections = client.get("/api/connections").json()
        assert connections["database"]["status"] == "ready"
        assert any(row["provider"] == "minimax" and row["selectable"] for row in connections["models"]["rows"])
        rows = client.get("/api/drafts").json()["rows"]
        assert rows
        assert all(row["status"] in {"draft", "saved_as_draft", "approved"} for row in rows)
        chosen = next(row for row in rows if row["asset_count"])
        detail = client.get(f"/api/drafts/{chosen['post_id']}")
        assert detail.status_code == 200
        assert detail.json()["assets"]
        assert isinstance(detail.json()["evidence"], list)
        rejected = client.post(
            f"/api/drafts/{chosen['post_id']}/review",
            headers={"X-Workbench": "1"},
            json={"updated_at": detail.json()["updated_at"], "checks": {"source": True, "date": False, "body": True, "image": True}},
        )
        assert rejected.status_code == 400


def test_agent_imports_only_separate_tools_installation():
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["REDBOOK_RUNTIME_ROOT"] = r"E:\AI\codex\redbook_runtime"
    result = subprocess.run(
        [sys.executable, "-c", "import apps.cli, src.agent.editorial_agent; print(apps.cli.__file__); print(src.agent.editorial_agent.__file__)"],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, check=True,
    )
    paths = [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    assert len(paths) == 2
    assert all(path.is_relative_to(TOOLS) for path in paths)
    assert not any(path.is_relative_to(OLD) for path in paths)


def test_source_evidence_removes_query_tokens_and_unsafe_schemes():
    rows = source_evidence({
        "news": {"picked": {"title": "News", "url": "https://example.com/story?api_key=secret", "seendate": "2026-09-28"}},
        "ai_digest": {"items": [
            {"title": "AI", "url": "javascript:alert(1)"},
            {"title": "Launch", "url": "https://vendor.example/launch?token=hidden", "published_at": "2026-09-28"},
        ]},
    })
    assert [row["url"] for row in rows] == ["https://example.com/story", "https://vendor.example/launch"]


def test_news_draft_displays_original_source():
    with TestClient(app, base_url=BASE) as client:
        client.post("/api/session", headers={"X-Workbench": "1"})
        detail = client.get("/api/drafts/0083f5cab27940fb9ad17fe2c8557c5c")
        assert detail.status_code == 200
        assert detail.json()["evidence"][0]["published_at"] == "2026-08-12T00:23:00Z"


def test_local_drafts_are_linked_from_checkpoint(tmp_path):
    run_id = "a" * 32
    post_id = "b" * 32
    path = tmp_path / "data/runs/agent" / run_id / "checkpoint.json"
    path.parent.mkdir(parents=True)
    (tmp_path / "data/posts" / post_id).mkdir(parents=True)
    (tmp_path / "data/posts" / post_id / "post.json").write_text("{}", encoding="utf-8")
    path.write_text(__import__("json").dumps({"item_status": {f"0:{post_id}:cafe": "skipped_local"}}), encoding="utf-8")
    assert local_draft_ids(tmp_path, run_id) == [post_id]
    assert local_draft_ids(tmp_path, "../bad") == []
