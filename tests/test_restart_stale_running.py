"""Offline restart contracts; run with --noconftest and an E: --basetemp.

PostgreSQL, the goal-control thread and execution workers are fenced off.
Startup, journal recovery, API projection and resume admission remain real.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi.testclient import TestClient


RUN = "5652fe3b58a04804afb386f0480b6b86"
CID = "a" * 32
PID = "b" * 32
GID = "c" * 32
UPLOADED = [f"{i:032x}" for i in range(1, 9)]
BASE = "http://127.0.0.1:8786"


class MemoryConversations:
    namespace = "restart-test"
    knowledge_store = None

    def __init__(self):
        self.rows = {}

    def get(self, identity):
        return deepcopy(self.rows[identity])

    def save(self, row):
        self.rows[row["id"]] = deepcopy(row)
        return deepcopy(row)

    def list(self, *, limit=100):
        return list(deepcopy(self.rows).values())[:limit]


class DeferredThread:
    """Record scheduling without executing control or generation work."""

    ident = None

    def __init__(self, **kwargs):
        self.target = kwargs["target"]

    def start(self):
        pass

    def join(self, **kwargs):
        pass


@pytest.fixture
def offline(tmp_path, monkeypatch):
    assert tmp_path.drive.upper() == "E:"
    monkeypatch.chdir(tmp_path)
    credentials = tmp_path / "data/knowledge/postgresql-local/connection.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("REDBOOK_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("KNOWLEDGE_DB_CREDENTIALS", str(credentials))
    monkeypatch.setenv("MODEL_PLATFORMS_DIR", str(tmp_path / "model-platforms"))
    monkeypatch.setenv("REDBOOK_AGENT_PORT", "8786")

    def forbidden(*args, **kwargs):
        pytest.fail("Offline restart test attempted a connection or worker")

    import psycopg
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(psycopg.Connection, "connect", forbidden)
    web = importlib.import_module("apps.web_service")
    monkeypatch.setattr(web.subprocess.Popen, "__init__", forbidden)
    monkeypatch.setattr(web.Workbench, "_run", forbidden)
    module = importlib.import_module("backend.app")
    monkeypatch.setattr(module, "RUNTIME", tmp_path)
    monkeypatch.setattr(module.app.state, "service", None)
    monkeypatch.setattr(module.app.state, "review_schema_ready", False)
    monkeypatch.setattr(module.app.state, "goal_control_stop", None, raising=False)
    monkeypatch.setattr(module.app.state, "goal_control_thread", None, raising=False)
    monkeypatch.setattr(module, "Thread", DeferredThread)
    database = {"status": "ready"}
    monkeypatch.setattr(module.KnowledgeStore, "from_env", lambda: SimpleNamespace(status=lambda: database))
    monkeypatch.setattr(module, "ensure_review_schema", lambda: None)
    conversations = MemoryConversations()
    monkeypatch.setattr(web, "PostgresConversationStore", lambda: conversations)

    # Replace only this adapter's thread API, not HTTP's own portal threads.
    monkeypatch.setattr(web, "threading", SimpleNamespace(RLock=web.threading.RLock, Thread=DeferredThread))
    with TestClient(module.app, base_url=BASE) as client:
        # Windows asyncio needs its internal socketpair before network fencing.
        monkeypatch.setattr(socket.socket, "connect", forbidden)
        monkeypatch.setattr(socket, "create_connection", forbidden)
        assert client.post("/api/session", headers={"X-Workbench": "1"}).status_code == 200
        yield SimpleNamespace(root=tmp_path, web=web, module=module, client=client,
                              conversations=conversations, database=database)


def seed_interrupted_host(offline, status="running"):
    from src.agent.plan_contract import digest, execution_fields, normalize_plan

    plan = normalize_plan({"id": PID, "version": 1, "jobs": [
        {"kind": "daily_news", "count": 10},
        {"kind": "daily_ai_digest", "count": 1},
    ]})
    frozen = execution_fields(plan)
    frozen.update(plan_id=PID, plan_version=1, host_environment={}, model_runtime={},
                  capability_namespace="restart-test", goal_context={
                      "goal_id": GID, "namespace": "restart-test", "conversation_id": CID,
                      "goal_revision": 1, "platform": "xhs"})
    plan.update(status="running", job_id=RUN, agent_run_id=RUN,
                execution_request_id=RUN, frozen_execution=frozen, frozen_execution_hash=digest(frozen))
    offline.conversations.save({"id": CID, "title": "Restart", "status": "running",
                                "messages": [], "plans": [plan], "runs": [RUN]})
    job = {"id": RUN, "key": RUN, "kind": "agent", "title": "Confirmed plan",
           "status": status, "created_at": 100, "started_at": 101, "ended_at": None,
           "agent_run_id": RUN, "post_ids": UPLOADED, "exit_code": None,
           "events": [{"id": 2470, "at": 150, "message": "[agent] stage=generate | in_progress | AI"}]}
    checkpoint = {"run_id": RUN, "status": "running", "jobs": plan["jobs"], "job_index": 1,
                  "uploaded_post_ids": UPLOADED, "reviewed_post_ids": [], "post_ids": [],
                  "item_status": {f"0:{pid}:abcd": "saved" for pid in UPLOADED}, "events": []}
    paths = [offline.root / "data/web_gui/jobs" / f"{RUN}.json",
             offline.root / "data/runs/agent" / RUN / "checkpoint.json",
             offline.root / "data/web_gui/conversations" / CID / "plans" / f"{PID}.json"]
    for path, value in zip(paths, (job, checkpoint, {**frozen, "frozen_execution_hash": digest(frozen)})):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    return SimpleNamespace(job=job, checkpoint=checkpoint, plan=plan, paths=paths)


@pytest.mark.parametrize("status", ["queued", "running", "waiting_user", "stopping"])
def test_restart_reconciles_active_journal_without_touching_checkpoint_or_plan(offline, status):
    seeded = seed_interrupted_host(offline, status)
    before = [path.read_bytes() for path in seeded.paths[1:]]
    conversation = offline.conversations.get(CID)
    # Simulate a fresh process, not an in-process second startup reusing its cache.
    offline.module.app.state.service = None
    offline.module.startup()
    current = offline.module.app.state.service
    job = current.jobs[RUN]
    assert job["status"] == "interrupted"
    assert job["ended_at"] is not None
    assert {k: v for k, v in job.items() if k not in {"status", "ended_at", "message"}} == {
        k: v for k, v in seeded.job.items() if k not in {"status", "ended_at", "message"}}
    assert json.loads(seeded.paths[0].read_text(encoding="utf-8")) == job
    assert [path.read_bytes() for path in seeded.paths[1:]] == before
    assert offline.conversations.get(CID) == conversation
    assert current.process is None
    current.assert_idle()

    rows = offline.client.get("/api/runs").json()["rows"]
    assert [(r["id"], r["status"]) for r in rows] == [(RUN, "interrupted")]
    detail = offline.client.get(f"/api/runs/{RUN}").json()
    assert detail["agent_run_id"] == RUN
    assert detail["activity"]["active"] is False
    assert detail["activity"]["status"] == "interrupted"
    assert detail["activity"]["counts"]["saved"] == 8
    assert all(j["status"] != "running" for j in detail["activity"]["jobs"])

    recovered_bytes = seeded.paths[0].read_bytes()
    offline.module.app.state.service = None
    offline.module.startup()
    assert seeded.paths[0].read_bytes() == recovered_bytes


@pytest.mark.parametrize("status", ["interrupted", "completed", "failed", "paused", "cancelled", "partial_success"])
def test_restart_leaves_nonactive_journals_unchanged(offline, status):
    seeded = seed_interrupted_host(offline, status)
    before = [path.read_bytes() for path in seeded.paths]
    offline.module.app.state.service = None
    offline.module.startup()
    assert [path.read_bytes() for path in seeded.paths] == before


def test_database_unavailable_fails_closed_then_recovers_on_first_read(offline, monkeypatch):
    seeded = seed_interrupted_host(offline)
    before = seeded.paths[0].read_bytes()
    offline.module.app.state.service = None
    offline.database["status"] = "unavailable"

    def unavailable():
        raise offline.module.OperationalError("offline database")

    monkeypatch.setattr(offline.module, "ensure_review_schema", unavailable)
    offline.module.startup()
    assert offline.module.app.state.service is None
    response = offline.client.get("/api/runs")
    assert response.status_code == 503
    assert seeded.paths[0].read_bytes() == before

    offline.database["status"] = "ready"
    monkeypatch.setattr(offline.module, "ensure_review_schema", lambda: None)
    response = offline.client.get("/api/runs")
    assert response.status_code == 200
    assert response.json()["rows"][0]["status"] == "interrupted"


def test_resume_api_keeps_original_checkpoint_plan_and_goal_after_restart(offline, monkeypatch):
    seeded = seed_interrupted_host(offline)
    before = [path.read_bytes() for path in seeded.paths[1:]]
    offline.module.app.state.service = None
    offline.module.startup()
    current = offline.module.app.state.service
    goal = {"goal_id": GID, "conversation_id": CID, "goal_revision": 1,
            "active_attempt_id": RUN, "state": "running",
            "target_date": datetime.now(timezone(timedelta(hours=8))).date().isoformat()}

    def bind(namespace, identity, attempt, *, expected_attempt_id, expected_revision):
        assert (namespace, identity, expected_attempt_id, expected_revision) == ("restart-test", GID, RUN, 1)
        goal["active_attempt_id"] = attempt

    from src.agent import goal_store, postgres_checkpoint
    from contextlib import contextmanager
    from langgraph.checkpoint.base import CheckpointTuple

    @contextmanager
    def checkpointer(store=None):
        def get_tuple(config):
            assert config == {"configurable": {"thread_id": RUN, "checkpoint_ns": ""}}
            return CheckpointTuple(config=config, checkpoint={"channel_values": deepcopy(seeded.checkpoint)}, metadata={})
        yield SimpleNamespace(get_tuple=get_tuple)

    monkeypatch.setattr(postgres_checkpoint, "postgres_checkpointer", checkpointer)
    monkeypatch.setattr(goal_store, "GoalStore", lambda store: SimpleNamespace(
        snapshot=lambda ns, identity: deepcopy(goal), turns=lambda ns, cid: [], bind_attempt=bind))
    submitted = []

    def plan_worker(request, job_id):
        submitted.append(deepcopy(request))
        return ["offline-worker"], {}

    monkeypatch.setattr(current, "plan", plan_worker)
    headers = {"X-Workbench": "1", "Idempotency-Key": "restart-resume-original-0001"}
    response = offline.client.post(f"/api/runs/{RUN}/resume", headers=headers, json={"conversation_id": CID})
    assert response.status_code == 200, response.text
    resumed = response.json()
    expected_attempt = uuid5(NAMESPACE_URL, CID + ":" + headers["Idempotency-Key"]).hex
    assert resumed["id"] == expected_attempt != RUN
    assert resumed["agent_run_id"] == RUN
    assert resumed["resume_of"] == RUN
    assert goal["goal_id"] == GID and goal["active_attempt_id"] == expected_attempt
    assert goal["goal_revision"] == 1
    assert submitted[0]["run_id"] == RUN
    assert Path(submitted[0]["resume_from"]) == seeded.paths[1]
    assert Path(submitted[0]["agent_jobs_file"]) == seeded.paths[2]
    saved = offline.conversations.get(CID)
    assert saved["runs"] == [RUN, expected_attempt]
    assert len(saved["plans"]) == 1
    assert saved["plans"][0]["id"] == PID
    assert saved["plans"][0]["frozen_execution"] == seeded.plan["frozen_execution"]
    assert [path.read_bytes() for path in seeded.paths[1:]] == before
    again = offline.client.post(f"/api/runs/{RUN}/resume", headers=headers, json={"conversation_id": CID})
    assert again.status_code == 200 and again.json()["id"] == expected_attempt
    assert len(submitted) == 1
