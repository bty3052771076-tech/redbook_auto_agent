"""Offline API/worker contract tests; no provider, PostgreSQL or platform writes."""

from copy import deepcopy
import json
import socket
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from apps.web_service import Workbench
from backend import app as module
from backend.progress import build_activity


class MemoryConversations:
    def __init__(self):
        self.rows = {}

    def get(self, key):
        return deepcopy(self.rows[key])

    def save(self, row):
        self.rows[row["id"]] = deepcopy(row)
        return deepcopy(row)

    def list(self, *, limit=100):
        return list(deepcopy(self.rows).values())[:limit]


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    from backend.plan_service import PlanService
    from backend import capabilities
    service = Workbench(tmp_path, conversation_store=MemoryConversations())
    namespace = 'resume_unit_' + uuid4().hex
    snapshot = {'snapshot_id':'offline-test'}
    manager = SimpleNamespace(store=SimpleNamespace(namespace=namespace,resources=lambda: []),
        context=SimpleNamespace(policy=lambda cid: ({'mode':'manual'},0),
                                prepare=lambda cid: pytest.fail('manual policy invoked compaction')),
        plan_capabilities=lambda *args: {'memory':[],'skills':[]}, freeze_plan=lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(capabilities, 'manager', lambda current: manager)
    monkeypatch.setattr(PlanService, '_model_runtime', lambda self, plan: {
        'snapshots':{}, 'legacy_roles':{}, 'legacy_configs':{}, 'namespace':'test'})
    service._test_checkpoint_states = {}

    def checkpoint_state(run_id):
        if run_id not in service._test_checkpoint_states:
            raise RuntimeError("POSTGRES_CHECKPOINT_NOT_FOUND: isolated checkpoint is missing")
        return deepcopy(service._test_checkpoint_states[run_id])

    monkeypatch.setattr(service, "_agent_checkpoint_state", checkpoint_state)
    monkeypatch.setattr(service, "providers", lambda: {"bindings": {}})
    monkeypatch.setattr(service, "models", lambda: {"rows": []})
    monkeypatch.setattr(service, "environment", lambda: {})
    monkeypatch.setattr(service, "_run", lambda *args: None)
    monkeypatch.setattr(module.app.state, "service", service)
    monkeypatch.setattr(module, "RUNTIME", tmp_path)
    commands = []
    real_plan = service.plan

    def capture(request, job_id):
        result = real_plan(request, job_id)
        commands.append(result[0])
        return result

    monkeypatch.setattr(service, "plan", capture)
    client = TestClient(module.app, base_url="http://127.0.0.1:8786")
    assert client.post("/api/session", headers={"X-Workbench": "1"}).status_code == 200
    yield service, client, commands
    client.close()


def start_plan(isolated):
    service, client, commands = isolated
    headers = {"X-Workbench": "1", "Idempotency-Key": "test-confirm-once-0001"}
    conversation = client.post("/api/conversations", headers=headers, json={"title": "resume contract"}).json()
    cid = conversation["id"]
    plan = client.post(f"/api/conversations/{cid}/messages", headers=headers, json={
        "content": "生成10条每日新闻、1条每日AI讯息、1条每日全球事件关注图，保存到小红书创作者中心草稿箱",
    }).json()["plan"]
    result = client.post(f"/api/plans/{plan['id']}/confirm", headers=headers,
                         json={"conversation_id": cid, "version": plan["version"]})
    assert result.status_code == 200, result.text
    return cid, plan, result.json()


def checkpoint(service, run_id, **extra):
    path = service.root / "data/runs/agent" / run_id / "checkpoint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"run_id": run_id, "status": "blocked", **extra}
    if "jobs" not in payload:
        payload["jobs"] = next(
            plan["jobs"] for row in service.conversation_store.rows.values() for plan in row["plans"]
            if run_id in {plan.get("agent_run_id"), plan.get("job_id")}
        )
    service._test_checkpoint_states[run_id] = deepcopy(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_new_plan_is_unlimited_and_confirm_is_idempotent(isolated):
    service, client, commands = isolated
    cid, plan, job = start_plan(isolated)
    assert plan["budget_minutes"] == 0
    assert commands[-1][commands[-1].index("--budget-minutes") + 1] == "0.0"
    assert commands[-1][commands[-1].index("--run-id") + 1] == job["id"] == job["agent_run_id"]
    again = client.post(f"/api/plans/{plan['id']}/confirm", headers={"X-Workbench": "1"},
                        json={"conversation_id": cid, "version": plan["version"]})
    assert again.status_code == 200
    assert again.json()["id"] == job["id"]
    assert len(commands) == 1


@pytest.mark.parametrize("budget", [0, 15, 240])
def test_command_budget_accepts_zero_and_explicit_nonnegative_values(isolated, budget):
    service, _, _ = isolated
    args, _ = service.plan({"kind": "agent", "budget_minutes": budget}, "a" * 32)
    assert float(args[args.index("--budget-minutes") + 1]) == 0.0


@pytest.mark.parametrize("budget", [-1, True, "nan", "inf", None])
def test_command_budget_rejects_invalid_values(isolated, budget):
    service, _, _ = isolated
    with pytest.raises(ValueError):
        service.plan({"kind": "agent", "budget_minutes": budget}, "a" * 32)


def test_repeat_resume_uses_original_checkpoint_and_ignores_legacy_budget(isolated):
    service, client, commands = isolated
    cid, plan, original = start_plan(isolated)
    root_id = original["id"]
    saved = service._read_agent_conversation(cid)
    saved["plans"][0]["budget_minutes"] = 120
    service._write_agent_conversation(saved)
    path = checkpoint(service, root_id)
    previous = root_id
    for attempt in range(2):
        service.jobs[previous]["status"] = "interrupted"
        response = client.post(f"/api/runs/{previous}/resume",
                               headers={"X-Workbench": "1", "Idempotency-Key": f"test-resume-once-{attempt:04d}"},
                               json={"conversation_id": cid})
        assert response.status_code == 200, response.text
        job = response.json()
        assert job["id"] != previous
        assert job["agent_run_id"] == root_id
        assert job["resume_of"] == previous
        args = commands[-1]
        assert args[args.index("--run-id") + 1] == root_id
        assert args[args.index("--resume-from") + 1] == str(path.resolve())
        assert args[args.index("--budget-minutes") + 1] == "0.0"
        assert "--no-refresh-quotas" in args and "--headless" in args
        previous = job["id"]
    assert len(service.jobs) == 3
    assert service.get_agent_conversation(cid)["plans"][0]["agent_run_id"] == root_id


def test_legacy_resume_record_is_resolved_using_original_plan(isolated):
    service, client, _ = isolated
    cid, plan, original = start_plan(isolated)
    root_id, legacy_id = original["id"], "f" * 32
    service.jobs[root_id]["status"] = "interrupted"
    service.jobs[legacy_id] = {**original, "id": legacy_id, "agent_run_id": legacy_id, "status": "failed"}
    saved = service._read_agent_conversation(cid)
    saved["runs"].append(legacy_id)
    saved["plans"][0]["resume_job_id"] = legacy_id
    service._write_agent_conversation(saved)
    checkpoint(service, root_id, jobs=plan["jobs"], reviewed_post_ids=["b" * 32], post_ids=["b" * 32])
    detail = client.get(f"/api/runs/{legacy_id}")
    assert detail.status_code == 200
    assert detail.json()["agent_run_id"] == root_id
    response = client.post(f"/api/runs/{legacy_id}/resume", headers={"X-Workbench": "1"}, json={"conversation_id": cid})
    assert response.status_code == 200, response.text
    assert response.json()["agent_run_id"] == root_id
    assert client.get(f"/api/runs/{legacy_id}").json()["agent_run_id"] == root_id


def test_resumed_detail_shows_preserved_reviewed_and_uploaded_items(isolated, monkeypatch):
    service, client, _ = isolated
    cid, plan, original = start_plan(isolated)
    root_id = original["id"]
    service.jobs[root_id]["status"] = "interrupted"
    retained, uploaded = "b" * 32, "c" * 32
    checkpoint(service, root_id, jobs=plan["jobs"], job_index=0,
               post_ids=[retained, uploaded], reviewed_post_ids=[retained, uploaded],
               uploaded_post_ids=[uploaded], item_status={f"0:{uploaded}:cafe": "saved"})
    monkeypatch.setattr(service, "post", lambda pid: {"id": pid, "title": "保留新闻", "body": "正文",
        "assets": ["image.png"], "status": "saved_as_draft" if pid == uploaded else "approved",
        "readback": "verified" if pid == uploaded else "unverified"})
    response = client.post(f"/api/runs/{root_id}/resume", headers={"X-Workbench": "1"}, json={"conversation_id": cid})
    detail = client.get(f"/api/runs/{response.json()['id']}").json()
    assert detail["agent_run_id"] == root_id
    assert detail["retained_post_ids"] == [retained]
    assert {row["id"] for row in detail["post_rows"]} == {retained, uploaded}
    assert detail["activity"]["jobs"][0]["reviewed"] == 2
    assert detail["activity"]["jobs"][0]["retained"] == 1
    assert detail["activity"]["counts"]["saved"] == detail["activity"]["counts"]["verified"] == 1


def test_checkpoint_counts_override_old_review_events():
    cp = {"jobs": [{"kind": "daily_news", "count": 10}], "job_index": 0,
          "post_ids": ["b" * 32, "c" * 32], "reviewed_post_ids": ["b" * 32],
          "events": [{"at": 1, "node": "review", "status": "success", "detail": "daily_news posts=8"}]}
    detail = build_activity({"status": "running"}, cp)
    assert detail["jobs"][0]["reviewed"] == 1
    assert detail["jobs"][0]["retained"] == 1


def test_resume_rejects_completed_checkpoint_and_cross_conversation(isolated):
    service, client, commands = isolated
    cid, _, original = start_plan(isolated)
    rid = original["id"]
    service.jobs[rid]["status"] = "interrupted"
    checkpoint(service, rid, status="completed")
    result = client.post(f"/api/runs/{rid}/resume", headers={"X-Workbench": "1"}, json={"conversation_id": cid})
    assert result.status_code == 400 and "已经完成" in result.json()["error"]
    other = service.create_agent_conversation()
    result = client.post(f"/api/runs/{rid}/resume", headers={"X-Workbench": "1"}, json={"conversation_id": other["id"]})
    assert result.status_code == 400 and "不属于" in result.json()["error"]
    assert len(commands) == 1


def test_resume_progress_retains_other_jobs_while_current_job_changes(isolated, monkeypatch):
    service, client, _ = isolated
    _, plan, original = start_plan(isolated)
    news, ai = "b" * 32, "c" * 32
    checkpoint(service, original["id"], jobs=plan["jobs"], job_index=1,
               post_ids=[ai], reviewed_post_ids=[ai], job_states={
                   "0": {"post_ids": [news], "reviewed_post_ids": [news], "status": "pending"},
                   "1": {"post_ids": [], "reviewed_post_ids": []},
               })
    monkeypatch.setattr(service, "post", lambda pid: {"id": pid, "title": "已保留", "assets": ["image.png"],
                                                    "status": "approved", "readback": "unverified"})
    response = client.get(f"/api/runs/{original['id']}")
    assert response.status_code == 200
    detail = response.json()
    assert set(detail["retained_post_ids"]) == {news, ai}
    assert detail["activity"]["counts"]["retained"] == 2
    assert [j["reviewed"] for j in detail["activity"]["jobs"]] == [1, 1, None]


def test_real_http_start_and_resume_contract_without_external_writes(isolated, monkeypatch):
    service, _, commands = isolated
    migrations = []
    monkeypatch.setattr(module, "ensure_review_schema", lambda: migrations.append("startup"))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    monkeypatch.setenv("REDBOOK_AGENT_PORT", str(port))
    server = uvicorn.Server(uvicorn.Config(module.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
            headers = {"X-Workbench": "1", "Idempotency-Key": "http-contract-start-0001"}
            assert client.get("/api/conversations").status_code == 403
            assert client.post("/api/session", headers=headers).status_code == 200
            cid = client.post("/api/conversations", headers=headers, json={"title": "HTTP contract"}).json()["id"]
            plan = client.post(f"/api/conversations/{cid}/messages", headers=headers, json={
                "content": "生成10条每日新闻、1条每日AI讯息、1条每日全球事件关注图，保存到小红书草稿箱",
            }).json()["plan"]
            started = client.post(f"/api/plans/{plan['id']}/confirm", headers=headers,
                                  json={"conversation_id": cid, "version": plan["version"]})
            assert started.status_code == 200, started.text
            original = started.json()["id"]
            service.jobs[original]["status"] = "interrupted"
            checkpoint(service, original, jobs=plan["jobs"], post_ids=[], reviewed_post_ids=[])
            resumed = client.post(f"/api/runs/{original}/resume", headers={**headers, "Idempotency-Key": "http-contract-resume-0001"},
                                  json={"conversation_id": cid})
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["agent_run_id"] == original
            detail = client.get(f"/api/runs/{resumed.json()['id']}").json()
            assert detail["activity"]["requested"] == 12
            assert detail["activity"]["counts"]["saved"] == 0
            assert len(commands) == 2
            assert all(args[args.index("--budget-minutes") + 1] == "0.0" for args in commands)
        assert migrations == ["startup"]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()
