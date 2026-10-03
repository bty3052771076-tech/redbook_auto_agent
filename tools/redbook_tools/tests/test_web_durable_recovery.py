"""Pending offline Web recovery cases; PostgreSQL and workers are test doubles."""

from contextlib import contextmanager
from copy import deepcopy
import json
import socket

from langgraph.checkpoint.base import CheckpointTuple
import pytest

from apps.web_service import Workbench
from src.agent import postgres_checkpoint


ROOT_ID = "a" * 32


@pytest.fixture(autouse=True)
def forbid_real_connections_and_workers(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline Web regression must not connect or start a real worker")

    monkeypatch.setattr(postgres_checkpoint, "postgres_checkpointer", forbidden)
    monkeypatch.setattr(postgres_checkpoint.psycopg, "connect", forbidden)
    monkeypatch.setattr(postgres_checkpoint.psycopg.Connection, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("apps.web_service.subprocess.Popen", forbidden)


@pytest.fixture
def workbench(monkeypatch, tmp_path, workbench_factory):
    service = workbench_factory(tmp_path)
    monkeypatch.setattr(service, "providers", lambda: {"bindings": {}})
    monkeypatch.setattr(service, "models", lambda: {"rows": []})
    monkeypatch.setattr(service, "environment", lambda: {})
    monkeypatch.setattr(service, "_run", lambda *args: None)
    commands = []
    real_plan = service.plan

    def capture(request, job_id):
        result = real_plan(request, job_id)
        commands.append(result[0])
        return result

    monkeypatch.setattr(service, "plan", capture)
    return service, commands


@pytest.fixture
def pg_state_stub(workbench, monkeypatch):
    service, _ = workbench
    record = {
        "state": {
            "run_id": ROOT_ID, "status": "blocked", "last_node": "finish",
            "jobs": [{"kind": "daily_news", "title": "News", "count": 1}],
            "job_index": 0, "completed_job_indices": [], "failed_jobs": [0],
            "post_ids": [], "reviewed_post_ids": [], "uploaded_post_ids": [],
            "item_status": {}, "job_states": {}, "events": [],
        },
        "read_ids": [],
    }

    def read_state(run_id):
        record["read_ids"].append(run_id)
        if run_id != ROOT_ID or record["state"] is None:
            raise RuntimeError("POSTGRES_CHECKPOINT_NOT_FOUND: offline test thread missing")
        return deepcopy(record["state"])

    monkeypatch.setattr(service, "_agent_checkpoint_state", read_state)
    return record


@pytest.fixture
def durable(monkeypatch):
    record = {
        "state": {
            "run_id": ROOT_ID, "status": "blocked", "last_node": "finish",
            "jobs": [{"kind": "daily_news", "title": "News", "count": 1}],
            "job_index": 0, "post_ids": [], "reviewed_post_ids": [],
            "uploaded_post_ids": [], "item_status": {}, "job_states": {},
            "completed_job_indices": [], "failed_jobs": [0], "events": [],
        },
        "error": None, "queries": [], "opened": 0,
    }

    class Saver:
        def get_tuple(self, config):
            record["queries"].append(deepcopy(config))
            if record["state"] is None:
                return None
            return CheckpointTuple(
                config={"configurable": {
                    "thread_id": ROOT_ID, "checkpoint_ns": "", "checkpoint_id": "checkpoint-1",
                }},
                checkpoint={
                    "v": 4, "id": "checkpoint-1", "ts": "2026-10-02T00:00:00+00:00",
                    "channel_values": deepcopy(record["state"]),
                    "channel_versions": {}, "versions_seen": {},
                },
                metadata={"source": "loop", "step": 1, "parents": {}},
                parent_config=None, pending_writes=[],
            )

    @contextmanager
    def checkpointer(store=None):
        record["opened"] += 1
        if record["error"]:
            raise record["error"]
        yield Saver()

    monkeypatch.setattr(postgres_checkpoint, "postgres_checkpointer", checkpointer)
    return record


@pytest.fixture
def resumable(workbench):
    service, _ = workbench
    conversation = service.create_agent_conversation("Offline recovery")
    cid = conversation["id"]
    planned = service.append_agent_message(cid, "生成1条每日新闻，只生成本地稿，不上传平台")
    saved = service._read_agent_conversation(cid)
    plan = saved["plans"][0]
    plan.update(job_id=ROOT_ID, agent_run_id=ROOT_ID, status="running", budget_minutes=120)
    saved["runs"] = [ROOT_ID]
    service._write_agent_conversation(saved)
    plan_file = service.directory / "conversations" / cid / "plans" / f"{planned['plan']['id']}.json"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text(json.dumps({"jobs": plan["jobs"], "delivery": plan["delivery"]}), encoding="utf-8")
    service.jobs[ROOT_ID] = {
        "id": ROOT_ID, "agent_run_id": ROOT_ID, "kind": "agent", "status": "interrupted",
        "title": "Offline recovery", "events": [], "post_ids": [],
    }
    service.persist(service.jobs[ROOT_ID])
    return cid, service.root / "data/runs/agent" / ROOT_ID / "checkpoint.json"


def write_audit(path, status):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": ROOT_ID, "status": status}), encoding="utf-8")


@pytest.mark.parametrize("audit", [None, "completed", "broken-json"])
def test_resume_uses_original_postgres_thread_not_audit(workbench, resumable, durable, audit):
    service, commands = workbench
    cid, path = resumable
    if audit:
        write_audit(path, audit)
        if audit == "broken-json":
            path.write_text("{", encoding="utf-8")
    result = service.resume_agent_run(cid, ROOT_ID, "offline-web-resume-0001")
    assert result["id"] != ROOT_ID
    assert result["agent_run_id"] == ROOT_ID
    assert result["resume_of"] == ROOT_ID
    assert durable["queries"] == [{"configurable": {"thread_id": ROOT_ID, "checkpoint_ns": ""}}]
    args = commands[-1]
    assert args[args.index("--run-id") + 1] == ROOT_ID
    assert args[args.index("--resume-from") + 1] == str(path.resolve())
    assert args[args.index("--budget-minutes") + 1] == "0.0"
    assert "--no-refresh-quotas" in args
    assert service.get_agent_conversation(cid)["plans"][0]["resume_job_id"] == result["id"]


def test_resume_repeated_attempts_keep_one_postgres_thread(workbench, resumable, durable):
    service, commands = workbench
    cid, _ = resumable
    previous = ROOT_ID
    for attempt in range(3):
        result = service.resume_agent_run(cid, previous, f"offline-web-resume-{attempt:04d}")
        assert result["agent_run_id"] == ROOT_ID
        assert result["resume_of"] == previous
        service.jobs[result["id"]]["status"] = "interrupted"
        previous = result["id"]
    assert len(commands) == 3
    assert all(query["configurable"]["thread_id"] == ROOT_ID for query in durable["queries"])


def test_legacy_attempt_identity_is_repaired_without_audit(workbench, resumable, durable):
    service, _ = workbench
    cid, _ = resumable
    legacy = "f" * 32
    service.jobs[legacy] = {**service.jobs[ROOT_ID], "id": legacy, "agent_run_id": legacy}
    saved = service._read_agent_conversation(cid)
    saved["runs"].append(legacy)
    saved["plans"][0]["resume_job_id"] = legacy
    service._write_agent_conversation(saved)
    result = service.resume_agent_run(cid, legacy, "offline-legacy-resume-0001")
    assert result["agent_run_id"] == ROOT_ID
    assert service.jobs[legacy]["agent_run_id"] == ROOT_ID
    assert durable["queries"][0]["configurable"]["thread_id"] == ROOT_ID


@pytest.mark.parametrize("failure", ["missing", "unavailable", "wrong-thread"])
def test_resume_fails_closed_even_with_valid_audit(workbench, resumable, durable, failure):
    service, commands = workbench
    cid, path = resumable
    write_audit(path, "blocked")
    if failure == "missing":
        durable["state"] = None
        error = "POSTGRES_CHECKPOINT_NOT_FOUND"
    elif failure == "unavailable":
        durable["error"] = OSError("database unavailable")
        error = "POSTGRES_CHECKPOINT_UNAVAILABLE"
    else:
        durable["state"]["run_id"] = "b" * 32
        error = "POSTGRES_CHECKPOINT_INVALID"
    with pytest.raises(RuntimeError, match=error):
        service.resume_agent_run(cid, ROOT_ID, "offline-invalid-resume-0001")
    assert not commands
    assert list(service.jobs) == [ROOT_ID]
    assert service.get_agent_conversation(cid)["runs"] == [ROOT_ID]


def test_postgres_completed_state_rejects_resume_without_audit(workbench, resumable, durable):
    service, commands = workbench
    cid, _ = resumable
    durable["state"]["status"] = "completed"
    with pytest.raises(ValueError, match="已经完成"):
        service.resume_agent_run(cid, ROOT_ID, "offline-completed-resume-0001")
    assert not commands


@pytest.mark.parametrize("guard", ["other-conversation", "active", "escaped-path", "missing-plan"])
def test_resume_guards_run_before_postgres_read(workbench, resumable, durable, monkeypatch, guard):
    service, commands = workbench
    cid, _ = resumable
    if guard == "other-conversation":
        cid = service.create_agent_conversation()["id"]
    elif guard == "active":
        service.jobs[ROOT_ID]["status"] = "running"
    elif guard == "escaped-path":
        monkeypatch.setattr(service, "agent_checkpoint_id", lambda *args: "../outside")
    else:
        saved = service._read_agent_conversation(cid)
        saved["plans"] = []
        service._write_agent_conversation(saved)
    with pytest.raises(ValueError):
        service.resume_agent_run(cid, ROOT_ID, "offline-guarded-resume-0001")
    assert durable["opened"] == 0
    assert not commands


@pytest.mark.parametrize("budget", [0, 15, 120, 240, "120"])
def test_legacy_nonnegative_budgets_emit_unlimited_worker_command(workbench, budget):
    service, _ = workbench
    args, _ = service.plan({"kind": "agent", "budget_minutes": budget}, ROOT_ID)
    assert args[args.index("--budget-minutes") + 1] == "0.0"


@pytest.mark.parametrize("budget", [-1, True, "nan", "inf", None])
def test_invalid_compatibility_budgets_still_rejected(workbench, budget):
    service, _ = workbench
    with pytest.raises(ValueError):
        service.plan({"kind": "agent", "budget_minutes": budget}, ROOT_ID)


@pytest.mark.parametrize("business_status, expected", [
    ("completed", "completed"), ("partial", "partial_success"),
    ("blocked", "failed"), ("running", "failed"),
])
def test_agent_terminal_status_uses_durable_result_not_historical_warning(
    workbench, resumable, durable, monkeypatch, business_status, expected,
):
    service, _ = workbench
    durable["state"]["status"] = business_status
    job = service.jobs[ROOT_ID]

    class Process:
        stdout = ["[agent] stage=generate | failed | temporary error\n",
                  "[agent] stage=generate | success | daily_news posts=1\n"]

        def wait(self):
            return 0

    monkeypatch.setattr("apps.web_service.subprocess.Popen", lambda *args, **kwargs: Process())
    Workbench._run(service, job, ["offline-worker"], {})
    assert job["exit_code"] == 0
    assert job["has_warnings"] is True
    assert job["status"] == expected
    assert job["agent_status"] == business_status
    assert service.process is None
    assert json.loads((service.directory / "jobs" / f"{ROOT_ID}.json").read_text(encoding="utf-8"))["status"] == expected


def test_zero_exit_without_postgres_result_is_not_reported_completed(workbench, resumable, durable, monkeypatch):
    service, _ = workbench
    durable["error"] = OSError("database unavailable")
    job = service.jobs[ROOT_ID]

    class Process:
        stdout = []

        def wait(self):
            return 0

    monkeypatch.setattr("apps.web_service.subprocess.Popen", lambda *args, **kwargs: Process())
    Workbench._run(service, job, ["offline-worker"], {})
    assert job["exit_code"] == 0
    assert job["status"] == "failed"
    assert "POSTGRES_CHECKPOINT_UNAVAILABLE" in job["message"]


@pytest.mark.parametrize("cancelled", [False, True])
def test_exit_failure_and_cancellation_take_precedence_over_durable_completed(
    workbench, resumable, durable, monkeypatch, cancelled,
):
    service, _ = workbench
    durable["state"]["status"] = "completed"
    job = service.jobs[ROOT_ID]

    class Process:
        stdout = []

        def wait(self):
            if cancelled:
                job["status"] = "stopping"
            return 1

    monkeypatch.setattr("apps.web_service.subprocess.Popen", lambda *args, **kwargs: Process())
    Workbench._run(service, job, ["offline-worker"], {})
    assert job["exit_code"] == 1
    assert job["status"] == ("cancelled" if cancelled else "failed")
    assert durable["opened"] == 0


def test_workbench_pg_state_stub_completes_despite_existing_warning(
    workbench, resumable, pg_state_stub, monkeypatch,
):
    service, _ = workbench
    post_id = "b" * 32
    pg_state_stub["state"].update(
        status="completed", job_index=1, completed_job_indices=[0], failed_jobs=[],
        post_ids=[post_id], reviewed_post_ids=[post_id],
        item_status={f"0:{post_id}:cafe": "skipped_local"},
    )
    job = service.jobs[ROOT_ID]
    job["has_warnings"] = True

    class Process:
        stdout = ["[agent] stage=finish | completed | uploaded=0\n"]

        def wait(self):
            return 0

    monkeypatch.setattr("apps.web_service.subprocess.Popen", lambda *args, **kwargs: Process())
    Workbench._run(service, job, ["offline-worker"], {})
    persisted = json.loads((service.directory / "jobs" / f"{ROOT_ID}.json").read_text(encoding="utf-8"))
    assert job["has_warnings"] is True
    assert persisted["status"] == "completed"
    assert persisted["agent_status"] == "completed"
    assert persisted["exit_code"] == 0
    assert pg_state_stub["read_ids"] == [ROOT_ID]
    assert service.process is None


def test_workbench_pg_state_stub_resumes_without_creating_audit_json(
    workbench, resumable, pg_state_stub,
):
    service, commands = workbench
    cid, path = resumable
    assert not path.exists()
    result = service.resume_agent_run(cid, ROOT_ID, "offline-state-resume-0001")
    assert result["id"] != ROOT_ID
    assert result["agent_run_id"] == ROOT_ID
    assert result["resume_of"] == ROOT_ID
    assert pg_state_stub["read_ids"] == [ROOT_ID]
    assert not path.exists()
    assert len(commands) == 1
    assert commands[0][commands[0].index("--run-id") + 1] == ROOT_ID
    assert commands[0][commands[0].index("--resume-from") + 1] == str(path.resolve())
    assert service.get_agent_conversation(cid)["runs"] == [ROOT_ID, result["id"]]


def test_workbench_pg_state_stub_missing_rejects_audit_fallback(
    workbench, resumable, pg_state_stub,
):
    service, commands = workbench
    cid, path = resumable
    write_audit(path, "blocked")
    pg_state_stub["state"] = None
    before = deepcopy(service.get_agent_conversation(cid))
    with pytest.raises(RuntimeError, match="POSTGRES_CHECKPOINT_NOT_FOUND"):
        service.resume_agent_run(cid, ROOT_ID, "offline-state-missing-0001")
    assert pg_state_stub["read_ids"] == [ROOT_ID]
    assert not commands
    assert list(service.jobs) == [ROOT_ID]
    assert service.get_agent_conversation(cid) == before


@pytest.mark.parametrize("budget", [15, 120, 240])
def test_workbench_submit_normalizes_positive_budget_without_pg_read(
    workbench, pg_state_stub, budget,
):
    service, commands = workbench
    result = service.submit(
        {"kind": "agent", "budget_minutes": budget}, "offline-positive-budget-0001",
    )
    assert result["status"] == "queued"
    assert len(commands) == 1
    assert commands[0][commands[0].index("--budget-minutes") + 1] == "0.0"
    assert "--no-refresh-quotas" in commands[0]
    assert pg_state_stub["read_ids"] == []
