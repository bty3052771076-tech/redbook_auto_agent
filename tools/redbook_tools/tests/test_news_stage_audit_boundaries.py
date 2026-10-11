from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest

from test_news_stage_store_postgres import stage_store
from test_shared_model_capacity import capacity
from test_news_pipeline_resume import runtime
from src.workflow.news_contracts import StageContext
from src.workflow.news_execution import NewsStageRuntime, build_stage_runtime
from src.workflow.model_queues import ModelWorkQueues


def test_confirmed_external_failure_is_retryable(stage_store):
    store, key = stage_store
    with pytest.raises(ValueError):
        runtime(store, key).call("writer", {}, lambda: int("bad JSON"))
    assert runtime(store, key, "resume").call("writer", {}, lambda: "valid") == "valid"


def test_parse_runtime_error_after_model_return_is_not_remote_unknown(stage_store):
    from src.agent.capabilities.context_usage import invoke_observed
    store, key = stage_store
    def parse_failure():
        invoke_observed(SimpleNamespace(provider="fake", model="test"), [], lambda: SimpleNamespace())
        raise RuntimeError("could not parse returned JSON")
    with pytest.raises(RuntimeError, match="could not parse"):
        runtime(store, key).call("writer", {}, parse_failure)
    assert runtime(store, key, "resume").call("writer", {}, lambda: "valid") == "valid"


def test_post_identity_is_root_scoped_even_when_text_cache_is_shared(stage_store):
    store, key = stage_store
    first = runtime(store, key)
    second = runtime(store, replace(key, root_run_id="another-run"))
    assert first.key("writer", {}) == replace(second.key("writer", {}), root_run_id=key.root_run_id)
    assert first.key("post_identity", {}).cache_args() != second.key("post_identity", {}).cache_args()


def test_runtime_binds_platform_and_permissions(monkeypatch, tmp_path):
    from src.workflow import news_execution as module
    from src.workflow.model_capacity import ModelCapacityStore
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module.NewsStageStore, "ensure_schema", lambda self: None)
    monkeypatch.setattr(module.NewsStageStore, "__init__", lambda self: setattr(self, "store", None))
    monkeypatch.setattr(ModelCapacityStore, "ensure_schema", lambda self: None)
    context = StageContext("root", "attempt", "news", "item", "account", "config")
    def build():
        return build_stage_runtime([], job_key="news", options={}, context=context).context
    first = build()
    monkeypatch.setenv("TOUTIAO_CHROME_USER_DATA_DIR", "E:/profiles/another")
    second = build()
    monkeypatch.setenv("AGENT_CAPABILITY_NAMESPACE", "other-user")
    third = build()
    assert len({first.namespace, second.namespace, third.namespace}) == 3
    assert first.capability_hash


def test_orphaned_capacity_raises_instead_of_waiting_forever(capacity):
    store, group = capacity
    assert store.acquire(group, "crashed", modality="text", limit=1)
    with store.store.connection() as conn:
        conn.execute("UPDATE agent.model_request_leases SET heartbeat_at=now()-interval '5 minutes' WHERE group_key=%s", (group,))
    with pytest.raises(RuntimeError, match="MODEL_CAPACITY_UNCERTAIN"):
        store.acquire(group, "new", modality="text", limit=1)
    with store.store.connection() as conn:
        assert conn.execute("SELECT status FROM agent.model_request_leases WHERE group_key=%s AND request_id='crashed'", (group,)).fetchone()["status"] == "unknown"


def test_confirmed_model_failure_releases_capacity(capacity):
    store, group = capacity
    with pytest.raises(ValueError):
        with store.slot(group, modality="text", limit=1, stop_event=Event()):
            raise ValueError("invalid local response")
    assert store.acquire(group, "next", modality="text", limit=1)


@pytest.mark.parametrize("remote_timeout", [False, True])
def test_queue_capacity_uses_observed_remote_completion(capacity, stage_store, remote_timeout):
    from src.agent.capabilities.context_usage import invoke_observed
    store, group = capacity
    stages, key = stage_store
    config = SimpleNamespace(provider="fake", model="test")
    def generate():
        invoke_observed(config, [], lambda: SimpleNamespace())
        if remote_timeout:
            def timeout():
                raise TimeoutError("remote outcome unknown")
            invoke_observed(config, [], timeout)
        raise RuntimeError("could not parse returned JSON")
    isolated = SimpleNamespace(slot=lambda ignored, **kwargs: store.slot(group, **kwargs))
    with ModelWorkQueues(capacity=isolated) as queues:
        with pytest.raises((RuntimeError, TimeoutError)):
            runtime(stages, key).call("writer", {}, lambda: queues.submit_llm(generate).result())
    with store.store.connection() as conn:
        status = conn.execute("SELECT status FROM agent.model_request_leases WHERE group_key=%s", (group,)).fetchone()["status"]
    assert status == ("unknown" if remote_timeout else "finished")


def test_mixed_and_minimax_queues_share_conservative_group():
    from contextlib import contextmanager
    groups = []
    @contextmanager
    def slot(group, **kwargs):
        groups.append(group)
        yield "request"
    for provider in ("minimax", None):
        with ModelWorkQueues(llm_provider=provider, capacity=SimpleNamespace(slot=slot)) as queues:
            assert queues.submit_llm(lambda: 1).result() == 1
    assert groups[0] == groups[1]


def test_local_stages_are_not_model_requests(stage_store, tmp_path):
    from src.workflow.performance import RunContext, summarize_performance
    import json
    store, key = stage_store
    context = runtime(store, key).context
    telemetry = RunContext.create("speed", run_id="metrics", telemetry_dir=tmp_path)
    runner = NewsStageRuntime(context, store, telemetry)
    runner.call("text_approved", {}, lambda: {"text": "ok"}, external=False)
    events = [json.loads(line) for line in telemetry.telemetry_path.read_text(encoding="utf8").splitlines()]
    assert len(events) >= 3
    assert summarize_performance(events)["requests"] == 0


def test_adapter_attempts_are_counted_without_counting_local_stages(stage_store, tmp_path):
    from src.agent.capabilities.context_usage import invoke_observed
    from src.workflow.performance import RunContext, summarize_performance
    import json
    store, key = stage_store
    telemetry = RunContext.create("speed", run_id="actual", telemetry_dir=tmp_path)
    runner = NewsStageRuntime(runtime(store, key).context, store, telemetry)
    config = SimpleNamespace(provider="fake", model="test")
    def generate():
        Event().wait(0.03)
        for _ in range(2):
            invoke_observed(config, [], lambda: SimpleNamespace(usage_metadata={"input_tokens": 10, "output_tokens": 5}))
        return "done"
    with ModelWorkQueues() as queues:
        runner.call("writer", {}, lambda: queues.submit_llm(generate).result())
    events = [json.loads(line) for line in telemetry.telemetry_path.read_text(encoding="utf8").splitlines()]
    result = summarize_performance(events)
    assert result["requests"] == 2
    assert result["retries"] == 0
    assert result["retry_identity_unknown"] == 2
    finished = [row["details"] for row in events if row["status"] == "success" and row["details"].get("request_id")]
    assert all(row["input_tokens"] == 10 for row in finished)
    assert finished[0]["queue_ms"] > 0
