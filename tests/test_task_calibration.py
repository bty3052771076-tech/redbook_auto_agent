"""Task recognition contracts; provider and worker calls are isolated."""

from copy import deepcopy
import json
import time
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.web_service import Workbench
from backend import app as module
from apps.cli import _load_agent_job_plan


class Conversations:
    def __init__(self):
        self.rows = {}
        self.namespace = 'unit_plan_' + uuid4().hex

    def get(self, key):
        return deepcopy(self.rows[key])

    def save(self, row):
        self.rows[row["id"]] = deepcopy(row)
        return deepcopy(row)

    def list(self, *, limit=100):
        return list(deepcopy(self.rows).values())[:limit]


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    from backend.plan_service import PlanService
    monkeypatch.chdir(tmp_path)
    current = Workbench(tmp_path, conversation_store=Conversations())
    monkeypatch.setattr(current, "environment", lambda: {})
    monkeypatch.setattr(current, "providers", lambda: {"bindings": {"agent": "", "writer": "", "image": ""}})
    monkeypatch.setattr(current, "models", lambda: {"rows": []})
    monkeypatch.setattr(current, "_run", lambda *args: pytest.fail("Recognition started a content worker"))
    monkeypatch.setattr(module.app.state, "service", current)
    monkeypatch.setattr(module, "ensure_review_schema", lambda: None)
    monkeypatch.setattr(PlanService, '_model_runtime', lambda self, plan: {
        'snapshots':{}, 'legacy_roles':{}, 'legacy_configs':{}, 'namespace':'test'})
    return current


def candidate():
    return {
        "schema_version": "task-recognition.v2", "intent": "generate",
        "jobs": [{"kind": "daily_news", "count": 5, "keywords": ["伊朗", "关税"],
                  "topic_brief": "优先有新进展的国际争议", "evaluation_viewpoint": None}],
        "options": {"delivery": "generate_only", "platform": "xhs", "performance_mode": "speed",
                    "image_score_required": False, "skip_quota_sync": True},
        "provider_requests": {"agent": None, "writer": None, "image": None},
        "requirements": [{"id": "r1", "category": "topic", "scope": "job:daily_news",
                          "original_text": "伊朗、关税", "normalized_instruction": "关注伊朗、关税",
                          "strength": "preference", "status": "mapped", "target": "job.topic_brief",
                          "evidence_source": "user_message", "evidence_quote": "伊朗、关税"}],
        "clarifications": [], "summary": "生成5篇新闻，关注伊朗、关税，速度优先，仅生成本地稿",
    }


@pytest.mark.parametrize('address', [
    'https://private-secret@example.test/v1',
    'https://example.test/v1?api_key=private-secret',
    'https://example.test/v1#private-secret',
    'https://example.test:99999/v1',
])
def test_legacy_calibration_rejects_unsafe_base_before_creating_model_request(calibration, monkeypatch, address):
    from backend.task_recognition import resolve_controller
    from src.model_platforms import PlatformError
    current, _, _, _, _, calls = calibration
    env = current.environment()
    monkeypatch.setattr(current, 'environment', lambda: {**env, 'MINIMAX_BASE_URL': address})
    with pytest.raises(PlatformError) as error:
        resolve_controller(current)
    assert error.value.code == 'UNSAFE_ADDRESS'
    assert 'private-secret' not in str(error.value)
    assert calls == []


@pytest.fixture
def calibration(workbench, monkeypatch):
    calls = []
    env = {"MINIMAX_TOKEN_PLAN_API_KEY": "test-only-key", "MINIMAX_LLM_MODEL": "MiniMax-M3",
           "MINIMAX_BILLING_MODE": "subscription_only", "MINIMAX_ALLOW_PAYGO": "0", "MINIMAX_ALLOW_PAID_CREDITS": "0"}
    monkeypatch.setattr(workbench, "environment", lambda: env.copy())
    monkeypatch.setattr(workbench, "models", lambda: {"rows": [
        {"id": "minimax:MiniMax-M3", "provider": "minimax", "model": "MiniMax-M3", "kind": "llm",
         "selectable": True, "cost_class": "subscription_included"},
    ]})

    def fake_call(config, payload):
        calls.append((config, deepcopy(payload)))
        return json.dumps(candidate(), ensure_ascii=False)

    monkeypatch.setattr(module, "recognition_call", fake_call, raising=False)
    cid = workbench.create_agent_conversation()["id"]
    plan = workbench.append_agent_message(cid, "生成5条每日新闻，关键词：伊朗、关税；速度优先，不上传")["plan"]
    body = {"source_message_id": plan["source_message_id"], "base_plan_id": plan["id"], "base_plan_version": plan["version"]}
    with TestClient(module.app, base_url="http://127.0.0.1:8786") as client:
        client.post("/api/session", headers={"X-Workbench": "1"})
        yield workbench, client, cid, plan, body, calls


def wait_recognition(client, cid, rid):
    for _ in range(100):
        result = client.get(f"/api/conversations/{cid}/task-recognitions/{rid}")
        assert result.status_code == 200, result.text
        if result.json()["status"] != "running":
            return result.json()
        time.sleep(0.01)
    pytest.fail("Recognition did not finish")


def test_calibration_adoption_is_idempotent_and_does_not_execute(calibration):
    workbench, client, cid, plan, body, calls = calibration
    route = f"/api/conversations/{cid}/task-recognitions"
    headers = {"X-Workbench": "1", "Idempotency-Key": "recognition-1"}
    response = client.post(route, headers=headers, json=body)
    assert response.status_code == 200, response.text
    rid = response.json()["id"]
    result = wait_recognition(client, cid, rid)
    assert result["status"] == "ready", result
    assert len(calls) == 1
    assert calls[0][0].provider == "minimax"
    assert calls[0][1]["user_message"].startswith("生成5条每日新闻")
    assert "test-only-key" not in json.dumps(calls[0][1])
    assert client.post(route, headers=headers, json=body).json()["id"] == rid
    assert len(calls) == 1
    adopted = client.post(f"{route}/{rid}/adopt", headers={"X-Workbench": "1"}, json={"base_plan_version": plan["version"]})
    assert adopted.status_code == 200, adopted.text
    new = adopted.json()["plan"]
    assert new["recognition_source"] == "llm"
    assert new["jobs"][0]["keywords"] == ["伊朗", "关税"]
    assert "伊朗 关税" in new["jobs"][0]["prompt"]
    assert "国际争议" in new["jobs"][0]["prompt"]
    assert new["performance_mode"] == "speed"
    assert new["image_score_required"] is False
    again = client.post(f"{route}/{rid}/adopt", headers={"X-Workbench": "1"}, json={"base_plan_version": plan["version"]})
    assert again.json()["plan"]["id"] == new["id"]
    saved = workbench.get_agent_conversation(cid)
    assert len(saved["plans"]) == 2
    assert saved["runs"] == []


def test_keyword_requirement_maps_to_actual_keyword_field(calibration, monkeypatch):
    _, client, cid, _, body, _ = calibration
    value = candidate()
    value["requirements"][0]["target"] = "job.keywords"
    monkeypatch.setattr(module, "recognition_call", lambda config, payload: json.dumps(value, ensure_ascii=False))
    response = client.post(f"/api/conversations/{cid}/task-recognitions", headers={"X-Workbench": "1"}, json=body)
    result = wait_recognition(client, cid, response.json()["id"])
    assert result["status"] == "ready", result
    assert result["candidate"]["requirements"][0]["target"] == "job.keywords"


def test_soft_topic_calibration_can_be_adopted_without_executing_a_worker(calibration, monkeypatch):
    workbench, client, _, _, _, _ = calibration
    message = ("生成10条每日新闻，优先关注知名平台禁令与解禁、消费者权益争议；"
               "每日新闻优先筛选有意外变化的事件，约3条作为软偏好，不设硬配额")
    cid = workbench.create_agent_conversation()["id"]
    plan = workbench.append_agent_message(cid, message)["plan"]
    value = candidate()
    value["jobs"][0].update(count=10, keywords=[], topic_brief="偏向有反差的权益事件")
    value["options"]["delivery"] = "save_draft"
    value["requirements"] = []
    monkeypatch.setattr(module, "recognition_call", lambda config, payload: json.dumps(value, ensure_ascii=False))
    route = f"/api/conversations/{cid}/task-recognitions"
    body = {"source_message_id": plan["source_message_id"], "base_plan_id": plan["id"],
            "base_plan_version": plan["version"]}
    response = client.post(route, headers={"X-Workbench": "1"}, json=body)
    assert response.status_code == 200, response.text
    rid = response.json()["id"]
    result = wait_recognition(client, cid, rid)
    assert result["status"] == "ready", result
    adopted = client.post(f"{route}/{rid}/adopt", headers={"X-Workbench": "1"},
                          json={"base_plan_version": plan["version"]})
    assert adopted.status_code == 200, adopted.text
    job = adopted.json()["plan"]["jobs"][0]
    assert job["keyword_mode"] == "default"
    assert job["count"] == 10
    assert job['topic_brief'] == '偏向有反差的权益事件'
    assert '约3条作为软偏好' not in job['prompt']
    assert '知名平台禁令与解禁' not in job['prompt']
    assert workbench.get_agent_conversation(cid)["runs"] == []


def test_requirement_scope_schema_exposes_allowed_columns():
    from backend.task_recognition import RecognizedTask

    scope = RecognizedTask.model_json_schema()["$defs"]["Requirement"]["properties"]["scope"]
    assert "job:daily_news" in scope["enum"]
    assert "plan" in scope["enum"]


def test_calibration_rejects_stale_candidate_without_replacing_new_plan(calibration):
    workbench, client, cid, plan, body, _ = calibration
    route = f"/api/conversations/{cid}/task-recognitions"
    result = client.post(route, headers={"X-Workbench": "1"}, json=body)
    rid = result.json()["id"]
    wait_recognition(client, cid, rid)
    new = workbench.append_agent_message(cid, "仅生成1篇每日AI讯息")["plan"]
    adopted = client.post(f"{route}/{rid}/adopt", headers={"X-Workbench": "1"}, json={"base_plan_version": plan["version"]})
    assert adopted.status_code == 400
    assert "变化" in adopted.json()["error"]
    assert workbench.get_agent_conversation(cid)["plans"][-1]["id"] == new["id"]


@pytest.mark.parametrize("bad", ["invalid_json", "unknown_field", "duplicate_key", "invented_evidence", "bad_count", "public_publish", "lost_keywords", "changed_count", "changed_delivery"])
def test_bad_model_output_preserves_local_plan(calibration, monkeypatch, bad):
    workbench, client, cid, plan, body, _ = calibration
    value = candidate()
    if bad == "unknown_field":
        value["executable"] = True
    elif bad == "invented_evidence":
        value["requirements"][0]["evidence_quote"] = "不存在的用户要求"
    elif bad == "bad_count":
        value["jobs"][0]["count"] = 21
    elif bad == "public_publish":
        value["options"]["delivery"] = "publish"
    elif bad == "lost_keywords":
        value["jobs"][0]["keywords"] = []
    elif bad == "changed_count":
        value["jobs"][0]["count"] = 3
    elif bad == "changed_delivery":
        value["options"]["delivery"] = "save_draft"
    raw = json.dumps(value, ensure_ascii=False)
    if bad == "invalid_json":
        raw = '{"jobs":'
    elif bad == "duplicate_key":
        raw = raw.replace('"intent": "generate"', '"intent": "generate", "intent": "unknown"')
    monkeypatch.setattr(module, "recognition_call", lambda config, payload: raw, raising=False)
    route = f"/api/conversations/{cid}/task-recognitions"
    result = client.post(route, headers={"X-Workbench": "1"}, json=body)
    outcome = wait_recognition(client, cid, result.json()["id"])
    expected = 'failed' if bad in {'invalid_json', 'unknown_field', 'duplicate_key'} else 'needs_input' if bad in {'bad_count', 'public_publish'} else 'ready'
    assert outcome["status"] == expected, outcome
    if bad == 'invented_evidence':
        assert outcome['candidate']['requirements'][0]['verification'] == 'unverified'
    if bad in {'lost_keywords', 'changed_count', 'changed_delivery'}:
        assert any(w['code'] == 'PLAN_DIFFERS' for w in outcome['candidate']['warnings'])
    assert workbench.get_agent_conversation(cid)["plans"][-1]["id"] == plan["id"]
    assert workbench.get_agent_conversation(cid)["runs"] == []


def test_running_calibration_blocks_execute_and_duplicate_calls(calibration, monkeypatch):
    workbench, client, cid, plan, body, _ = calibration
    entered, release = threading.Event(), threading.Event()
    attempts = []

    def waiting(config, payload):
        attempts.append(payload)
        entered.set()
        assert release.wait(5)
        return json.dumps(candidate(), ensure_ascii=False)

    monkeypatch.setattr(module, "recognition_call", waiting)
    route = f"/api/conversations/{cid}/task-recognitions"
    headers = {"X-Workbench": "1", "Idempotency-Key": "same-request"}
    try:
        started = client.post(route, headers=headers, json=body)
        assert started.status_code == 200
        rid = started.json()["id"]
        assert entered.wait(2)
        duplicate = client.post(route, headers=headers, json=body)
        assert duplicate.json()["id"] == rid
        assert len(attempts) == 1
        busy = client.post(route, headers={**headers, "Idempotency-Key": "second-request"}, json=body)
        assert busy.status_code == 400 and "BUSY" in busy.json()["error"]
        confirmed = client.post(f"/api/plans/{plan['id']}/confirm", headers=headers,
                                json={"conversation_id": cid, "version": plan["version"]})
        assert confirmed.status_code == 400 and "BUSY" in confirmed.json()["error"]
    finally:
        release.set()
    assert wait_recognition(client, cid, rid)["status"] == "ready"
    kept = client.post(f"{route}/{rid}/discard", headers=headers)
    assert kept.json()["status"] == "discarded"
    assert workbench.get_agent_conversation(cid)["plans"][-1]["id"] == plan["id"]


def test_changed_model_configuration_cannot_adopt_old_candidate(calibration, monkeypatch):
    workbench, client, cid, plan, body, _ = calibration
    route = f"/api/conversations/{cid}/task-recognitions"
    started = client.post(route, headers={"X-Workbench": "1"}, json=body)
    rid = started.json()["id"]
    assert wait_recognition(client, cid, rid)["status"] == "ready"
    settings = workbench.settings()
    monkeypatch.setattr(workbench, "settings", lambda: {**settings, "performance_mode": "speed"})
    response = client.post(f"{route}/{rid}/adopt", headers={"X-Workbench": "1"}, json={"base_plan_version": plan["version"]})
    assert response.status_code == 400 and "设置已变化" in response.json()["error"]
    assert len(workbench.get_agent_conversation(cid)["plans"]) == 1


def test_server_restart_marks_running_recognition_interrupted(calibration):
    workbench, client, cid, _, _, calls = calibration
    saved = workbench._read_agent_conversation(cid)
    record = {"id": "c" * 32, "status": "running", "owner": "previous-server"}
    saved["task_recognitions"] = [record]
    workbench._write_agent_conversation(saved)
    response = client.get(f"/api/conversations/{cid}/task-recognitions/{record['id']}")
    assert response.status_code == 200
    assert response.json()["status"] == "interrupted"
    assert calls == []


def test_record_cannot_be_read_or_adopted_from_another_conversation(calibration):
    workbench, client, cid, _, body, _ = calibration
    route = f"/api/conversations/{cid}/task-recognitions"
    started = client.post(route, headers={"X-Workbench": "1"}, json=body)
    rid = started.json()["id"]
    wait_recognition(client, cid, rid)
    other = workbench.create_agent_conversation()["id"]
    assert client.get(f"/api/conversations/{other}/task-recognitions/{rid}").status_code == 400
    assert client.post(f"/api/conversations/{other}/task-recognitions/{rid}/adopt", headers={"X-Workbench": "1"},
                       json={"base_plan_version": 1}).status_code == 400


def test_http_client_has_single_call_and_bounded_output(monkeypatch):
    from backend.task_recognition import call_model
    from src.config import LLMConfig
    import httpx

    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    result = call_model(LLMConfig("MiniMax-M3", "isolated", "https://example.com/v1", provider="minimax"), {"user_message": "测试"})
    assert result == "{}"
    assert len(requests) == 1
    assert requests[0]["max_tokens"] == 4096
    assert not requests[0]["stream"]


@pytest.mark.parametrize("failure", ["rate_limit", "timeout", "truncated"])
def test_http_failures_do_not_retry(monkeypatch, failure):
    from backend.task_recognition import call_model
    from src.config import LLMConfig
    import httpx

    attempts = []

    def handle(request):
        attempts.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("isolated")
        if failure == "rate_limit":
            return httpx.Response(429)
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    with pytest.raises(ValueError, match="TASK_LLM_"):
        call_model(LLMConfig("test", "isolated", "https://example.com/v1"), {"user_message": "测试"})
    assert len(attempts) == 1


def test_local_news_keywords_reach_executable_job(workbench, monkeypatch):
    cid = workbench.create_agent_conversation()["id"]
    result = workbench.append_agent_message(cid, "生成5条每日新闻，关键词：伊朗、关税、芯片；速度优先，不上传")
    plan = result["plan"]
    assert plan["jobs"][0]["keywords"] == ["伊朗", "关税", "芯片"]
    assert plan["jobs"][0]["prompt"] == "伊朗 关税 芯片"
    assert plan["source_message_id"] == result["message"]["id"]
    assert plan["recognition_source"] == "rules"
    assert plan["performance_mode"] == "speed"
    requests = []
    monkeypatch.setattr(workbench, "submit", lambda request, key: requests.append(request) or {"id": "a" * 32})
    workbench.execute_agent_plan(cid, plan["id"], plan["version"], "keyword-test")
    frozen = json.loads(__import__("pathlib").Path(requests[0]["agent_jobs_file"]).read_text(encoding="utf-8"))
    jobs = _load_agent_job_plan(requests[0]["agent_jobs_file"], "auto", "无视角评价")
    assert frozen["jobs"][0]["keywords"] == ["伊朗", "关税", "芯片"]
    assert jobs[0].prompt == "伊朗 关税 芯片"
    assert requests[0]["performance_mode"] == "speed"
    assert requests[0]["delivery"] == "generate_only"


@pytest.mark.parametrize("text,want", [
    ("生成3条关于半导体的每日新闻，不上传", ["半导体"]),
    ("围绕伊朗、关税生成3条每日新闻，不上传", ["伊朗", "关税"]),
    ("生成3条每日新闻，关键词：‘人工智能芯片’、DeepSeek；不上传", ["人工智能芯片", "DeepSeek"]),
])
def test_common_news_topic_phrases_are_preserved(workbench, text, want):
    plan = workbench._parse_agent_message(text)
    assert plan["jobs"][0]["keywords"] == want


def test_job_keywords_are_not_shared_across_columns(workbench):
    plan = workbench._parse_agent_message("生成2条每日新闻，关键词：伊朗、关税；生成1条每日AI讯息，关键词：DeepSeek、MiniMax")
    assert plan["jobs"][0]["keywords"] == ["伊朗", "关税"]
    assert plan["jobs"][1]["keywords"] == ["DeepSeek", "MiniMax"]
    assert "伊朗" not in plan["jobs"][1]["prompt"]


def test_local_parse_error_preserves_source_for_calibration(workbench):
    cid = workbench.create_agent_conversation()["id"]
    result = workbench.append_agent_message(cid, "生成21条每日新闻，关键词：能源")
    assert result["plan"]["executable"] is False
    assert result["plan"]["source_message_id"] == result["message"]["id"]
    assert "1至20" in result["assistant"]["content"]
    assert workbench.get_agent_conversation(cid)["runs"] == []


def test_calibration_entry_is_authenticated_and_reports_missing_model(workbench):
    cid = workbench.create_agent_conversation()["id"]
    plan = workbench.append_agent_message(cid, "生成1条每日新闻，关键词：能源")["plan"]
    route = f"/api/conversations/{cid}/task-recognitions"
    body = {"source_message_id": plan["source_message_id"], "base_plan_id": plan["id"], "base_plan_version": plan["version"]}
    with TestClient(module.app, base_url="http://127.0.0.1:8786") as client:
        assert client.post(route, json=body).status_code == 403
        client.post("/api/session", headers={"X-Workbench": "1"})
        response = client.post(route, headers={"X-Workbench": "1", "Idempotency-Key": "missing-model"}, json=body)
        assert response.status_code == 400
        assert "主控" in response.json()["error"]
        assert workbench.get_agent_conversation(cid)["runs"] == []
