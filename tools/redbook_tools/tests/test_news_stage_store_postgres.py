"""Real PostgreSQL; every test owns a fresh namespace and removes only its rows."""
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4
import pytest
from src.knowledge.store import KnowledgeStore
from src.workflow.stage_store import NewsStageStore, StageKey


@pytest.fixture
def stage_store():
    if os.getenv("REDBOOK_TEST_POSTGRES") != "1":
        pytest.skip("real PostgreSQL opt-in required")
    store = NewsStageStore(KnowledgeStore.from_env())
    store.ensure_schema()
    namespace = "test-perf-" + uuid4().hex
    yield store, StageKey(namespace, "run", "news", "candidate", "text", "input")
    with store.store.connection() as conn:
        conn.execute("DELETE FROM agent.news_work_items WHERE namespace=%s", (namespace,))
        conn.execute("DELETE FROM agent.news_stage_cache WHERE namespace=%s", (namespace,))


def test_two_workers_only_one_claims(stage_store):
    store, key = stage_store
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda owner:store.claim_stage(key, owner=owner, lease_seconds=60), ("a", "b")))
    assert sum(result is not None for result in results) == 1


def test_completed_stage_is_reused_by_new_store_without_model_call(stage_store):
    store, key = stage_store
    lease = store.claim_stage(key, owner="a", lease_seconds=60)
    assert store.commit_stage(lease, payload={"body":"accepted"})
    fresh = NewsStageStore(store.store)
    assert fresh.get_cached(key)["body"] == "accepted"
    assert fresh.claim_stage(key, owner="b", lease_seconds=60) is None
    assert fresh.get_cached(replace(key, namespace="different-account")) is None


def test_old_epoch_cannot_overwrite_new_result(stage_store):
    store, key = stage_store
    old = store.claim_stage(key, owner="a", lease_seconds=60)
    with store.store.connection() as conn:
        conn.execute("UPDATE agent.news_work_items SET lease_until=now()-interval '1 second' WHERE namespace=%s",(key.namespace,))
    new = store.claim_stage(key, owner="b", lease_seconds=60)
    assert new.epoch > old.epoch
    assert not store.commit_stage(old, payload={"body":"old"})
    assert store.commit_stage(new, payload={"body":"new"})


def test_unknown_remote_result_is_not_retried_after_lease_expiry(stage_store):
    store, key = stage_store
    lease = store.claim_stage(key, owner="a", lease_seconds=60, external=True)
    with store.store.connection() as conn:
        conn.execute("UPDATE agent.news_work_items SET lease_until=now()-interval '1 second' WHERE namespace=%s",(key.namespace,))
    assert store.claim_stage(key, owner="b", lease_seconds=60, external=True) is None
    assert store.commit_stage(lease, payload={"body":"late-result"})


def test_cache_is_immutable_and_input_changes_invalidate(stage_store):
    store, key = stage_store
    lease = store.claim_stage(key, owner="a", lease_seconds=60)
    assert store.commit_stage(lease, payload={"body":"one"})
    with pytest.raises(ValueError, match="conflict"):
        store.put_cached(key, {"body":"two"})
    assert store.get_cached(replace(key, input_hash="updated-source")) is None


def test_database_failure_does_not_fallback(tmp_path):
    store = NewsStageStore(KnowledgeStore(credentials_path=tmp_path / "absent.json"))
    with pytest.raises(FileNotFoundError):
        store.get_cached(StageKey("account", "run", "news", "x", "text", "hash"))
