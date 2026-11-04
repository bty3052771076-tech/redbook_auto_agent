"""Regression cases for memory optimization. Added without executing them."""
from contextlib import nullcontext
import json
import os
from pathlib import Path

import pytest

from src.workflow import resources
from src.workflow.job_journal import JobJournal


@pytest.fixture
def simulated_resources(monkeypatch, tmp_path):
    monkeypatch.setenv("REDBOOK_MEMORY_ENABLED", "1")
    monkeypatch.setenv("REDBOOK_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("REDBOOK_RESOURCE_ROOT", str(tmp_path))
    monkeypatch.setattr(resources, "sample_memory",
                        lambda: resources.MemorySample(16 * resources.GIB, 8 * resources.GIB, 8 * resources.GIB))
    yield
    resources._observer_stop.set()
    if resources._observer is not None:
        resources._observer.join(timeout=6)
    resources._observer_stop.clear()


def test_pressure_has_hysteresis_and_delayed_recovery():
    state = {"pressure": "normal"}
    low = resources.MemorySample(16 * resources.GIB, resources.GIB)
    assert resources._pressure(state, low, 100) == "normal"
    assert resources._pressure(state, low, 106) == "normal"
    assert resources._pressure(state, low, 112) == "tight"
    high = resources.MemorySample(16 * resources.GIB, 4 * resources.GIB)
    assert resources._pressure(state, high, 118) == "tight"
    assert resources._pressure(state, high, 142) == "tight"
    assert resources._pressure(state, high, 148) == "normal"


def test_nested_admission_is_one_lease_and_is_released(simulated_resources):
    with resources.memory_slot("image", limit=2):
        with resources.memory_slot("image", limit=2):
            assert resources.resource_status()["active"]["image"] == 1
    assert resources.resource_status()["active"].get("image", 0) == 0


def test_unknown_measurement_is_not_reported_as_zero(monkeypatch, simulated_resources):
    monkeypatch.setattr(resources, "sample_memory", lambda: resources.MemorySample())
    status = resources.resource_status()
    assert status["pressure"] == "unknown"
    assert status["available"] is None
    with resources.memory_slot("text", limit=5):
        assert resources.resource_status()["active"]["text"] == 1
        state = resources.read_state(resources.coordination_root() / "data/runs/resource-admission/state.json")
        assert all(row["limit"] == 5 for row in state["leases"].values())


def test_embedding_is_serial_and_bounded(monkeypatch):
    from src.knowledge import embeddings

    calls = []
    class Vector:
        def tolist(self):
            return [0.0] * embeddings.MODEL_DIMENSIONS
    class Model:
        def embed(self, texts, *, batch_size, parallel):
            calls.append((len(texts), batch_size, parallel))
            yield from (Vector() for _ in texts)

    monkeypatch.setattr(embeddings, "memory_slot", lambda *a, **k: nullcontext())
    monkeypatch.setattr(embeddings, "embedding_batch_size", lambda: 16)
    result = embeddings.LocalEmbeddingModel(Model()).embed_documents(str(i) for i in range(67))
    assert len(result) == 67
    assert [row[0] for row in calls] == [16, 16, 16, 16, 3]
    assert all(row[2] is None for row in calls)


def test_manifest_hashing_does_not_read_entire_file(monkeypatch, tmp_path):
    from src.knowledge.embeddings import _asset_manifest
    import hashlib
    body = b"verified-model" * 1000
    (tmp_path / "model.onnx").write_bytes(body)
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("whole-file read"))
    assert _asset_manifest(tmp_path)["model.onnx"] == hashlib.sha256(body).hexdigest()


def test_job_journal_evicts_memory_not_history(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    for index in range(30):
        key = f"{index:032x}"
        (root / f"{key}.json").write_text(json.dumps({
            "id": key, "created_at": index, "status": "completed",
            "events": [{"id": 1, "message": "evidence"}],
        }), encoding="utf-8")
    journal = JobJournal(root, active_statuses={"running"}, max_details=3)
    assert len(journal) == 30
    first = journal.recent(limit=5)
    assert [row["created_at"] for row in first] == [29, 28, 27, 26, 25]
    assert len(journal.cache) <= 3
    assert journal[f"{0:032x}"]["events"][0]["message"] == "evidence"
    older = journal.recent(limit=5, before=first[-1]["id"])
    assert [row["created_at"] for row in older] == [24, 23, 22, 21, 20]
    assert len(list(root.glob("*.json"))) == 30


def test_ingestion_manifest_checks_database_versions(tmp_path):
    from src.knowledge.ingest import ingest_posts
    from src.knowledge.store import KnowledgeStore
    from src.storage.files import save_post
    from src.storage.models import Post
    store = KnowledgeStore.from_env()
    store._memory = True
    post = Post(title="Source subject", body="An independently verifiable event.")
    save_post(post, base=tmp_path)
    assert ingest_posts(store, data_root=tmp_path)["inserted"] == 1
    assert ingest_posts(store, data_root=tmp_path)["unchanged"] == 1
    store._memory_documents.clear()
    assert ingest_posts(store, data_root=tmp_path)["inserted"] == 1


def test_borrowed_service_never_launches_or_terminates(monkeypatch, tmp_path):
    from src.integrations import local_process
    monkeypatch.setattr(local_process, "coordination_root", lambda: tmp_path)
    monkeypatch.setattr(local_process.subprocess, "Popen", lambda *a, **k: pytest.fail("unexpected launch"))
    monkeypatch.setattr(local_process, "terminate_owned", lambda *_: pytest.fail("borrowed termination"))
    lease = local_process.LocalServiceLease("rsshub", port=1200)
    assert lease.acquire([], cwd=tmp_path, health=lambda: True, start_allowed=False)
    assert not lease.registered
    lease.release()


def test_owned_service_waits_for_last_lease(monkeypatch, tmp_path):
    from src.integrations import local_process
    monkeypatch.setattr(local_process, "coordination_root", lambda: tmp_path)
    monkeypatch.setattr(local_process, "process_alive", lambda *a: True)
    killed = []
    monkeypatch.setattr(local_process, "terminate_owned", lambda row: killed.append(row["pid"]) or True)
    first = local_process.LocalServiceLease("rsshub", port=1200)
    second = local_process.LocalServiceLease("rsshub", port=1200)
    record = {"pid": 12345, "identity": "owned-birth", "executable": "node",
              "members": {first.key: first.member, second.key: second.member}}
    resources.write_state(first.path, record)
    first.registered = second.registered = True
    first.release()
    assert killed == []
    second.release()
    assert killed == [12345]


def test_invalid_job_journal_is_ignored(tmp_path):
    (tmp_path / ("a" * 32 + ".json")).write_text("{broken", encoding="utf-8")
    journal = JobJournal(tmp_path, active_statuses={"running"})
    assert len(journal) == 0
    assert journal.recent() == []


def test_task_cancel_protects_shared_service_subtree(monkeypatch, tmp_path):
    from src.integrations import local_process
    monkeypatch.setattr(local_process, "coordination_root", lambda: tmp_path)
    monkeypatch.setattr(local_process, "process_identity", lambda pid: str(pid))
    monkeypatch.setattr(local_process, "process_alive", lambda *a: True)
    monkeypatch.setattr(resources, "process_tree", lambda: {100: 1, 201: 100, 202: 100, 203: 201})
    killed = []
    monkeypatch.setattr(local_process.subprocess, "run", lambda command, **kw: killed.append(int(command[2])))
    monkeypatch.setattr(local_process.os, "kill", lambda pid, signal: killed.append(pid))
    path = tmp_path / "data/runs/local-services/rsshub-1200.json"
    resources.write_state(path, {"pid": 201, "identity": "201", "members": {
        "other-task": {"pid": 999, "identity": "999"},
    }})
    local_process.terminate_task_tree(100, "100")
    assert set(killed) == {100, 202}


