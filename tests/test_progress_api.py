from copy import deepcopy
from threading import RLock
import time
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend import app as module


CID = "d" * 32
RID = "a" * 32


class FakeWorkbench:
    def __init__(self):
        self.lock = RLock()
        self.conversation = {"id": CID, "messages": [], "plans": [{"id": "existing"}], "runs": [RID]}
        self.append_calls = 0

    def _read_agent_conversation(self, _id):
        return deepcopy(self.conversation)

    def _write_agent_conversation(self, value):
        self.conversation = deepcopy(value)
        return value

    def redact(self, value):
        return value

    def job_detail(self, _id):
        return {"id": RID, "status": "running", "started_at": 100, "events": []}

    def agent_checkpoint_id(self, run_id):
        return run_id

    def _parse_agent_message(self, _text):
        return {}

    def append_agent_message(self, _id, _text):
        self.append_calls += 1
        return {"plan": {"executable": True}}


def client_for(monkeypatch):
    fake = FakeWorkbench()
    monkeypatch.setattr(module.app.state, "service", fake)
    client = TestClient(module.app, base_url="http://127.0.0.1:8786")
    client.cookies.set("redbook_agent", module.app.state.token)
    return client, fake


def test_status_reply_preserves_plan_and_never_submits(monkeypatch):
    client, fake = client_for(monkeypatch)
    result = client.post(f"/api/conversations/{CID}/messages", headers={"X-Workbench": "1"}, json={"content": "现在进度如何？"})
    assert result.status_code == 200
    assert result.json()["plan"] is None
    assert result.json()["run"]["activity"]["active"]
    assert "用时" in result.json()["assistant"]["content"]
    assert len(fake.conversation["plans"]) == 1
    assert [m["role"] for m in fake.conversation["messages"]] == ["user", "assistant"]
    assert fake.append_calls == 0


def test_status_reply_without_run_does_not_create_task(monkeypatch):
    client, fake = client_for(monkeypatch)
    fake.conversation["runs"] = []
    response = client.post(f"/api/conversations/{CID}/messages", headers={"X-Workbench": "1"}, json={"content": "完成了吗？"})
    assert "还没有执行记录" in response.json()["assistant"]["content"]
    assert fake.append_calls == 0


def test_new_generation_request_still_uses_existing_confirmation(monkeypatch):
    client, fake = client_for(monkeypatch)
    result = client.post(f"/api/conversations/{CID}/messages", headers={"X-Workbench": "1"}, json={"content": "生成1条每日新闻，完成后告诉我进度"})
    assert result.json()["plan"]["executable"]
    assert fake.append_calls == 1


def test_run_detail_contains_readable_progress(monkeypatch):
    client, _ = client_for(monkeypatch)
    result = client.get(f"/api/runs/{RID}")
    assert result.status_code == 200
    assert result.json()["activity"]["status_label"] == "执行中"


@pytest.mark.skipif(os.getenv("REDBOOK_TEST_POSTGRES") != "1", reason="explicit local PostgreSQL integration test")
def test_progress_reply_persists_to_real_postgresql_without_starting_worker(monkeypatch):
    from src.agent.conversation_store import PostgresConversationStore

    client, fake = client_for(monkeypatch)
    store = PostgresConversationStore()
    conversation_id = uuid4().hex
    saved = {**fake.conversation, "id": conversation_id, "title": "进度问答数据库核验",
             "status": "idle", "created_at": time.time(), "updated_at": time.time(), "runs": [], "plans": []}
    store.save(saved)
    monkeypatch.setattr(fake, "_read_agent_conversation", store.get)
    monkeypatch.setattr(fake, "_write_agent_conversation", store.save)
    try:
        response = client.post(f"/api/conversations/{conversation_id}/messages", headers={"X-Workbench": "1"}, json={"content": "进度如何？"})
        assert response.status_code == 200
        persisted = store.get(conversation_id)
        assert len(persisted["messages"]) == 2
        assert persisted["messages"][-1]["content"] == response.json()["assistant"]["content"]
        assert persisted["plans"] == saved["plans"]
        assert not persisted["runs"]
        assert fake.append_calls == 0
    finally:
        with store.knowledge_store.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM agent.messages WHERE conversation_id=%s", (conversation_id,))
            conn.execute("DELETE FROM agent.conversations WHERE conversation_id=%s", (conversation_id,))
