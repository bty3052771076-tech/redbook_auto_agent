"""Stage restart acceptance against PostgreSQL, without vendor/platform writes."""
from dataclasses import replace
from pathlib import Path
import pytest

from test_news_stage_store_postgres import stage_store
from src.workflow.news_contracts import StageContext
from src.workflow.news_execution import NewsStageRuntime, StagePending


def runtime(store, key, execution="execution"):
    return NewsStageRuntime(StageContext(key.root_run_id, execution, key.job_key,
        key.candidate_id, key.namespace, "frozen", pipeline_version="staged_v2"), store)


def test_restart_after_text_and_image_makes_no_model_requests(stage_store, tmp_path):
    store, key = stage_store
    run = runtime(store, key)
    calls = []
    def writer():
        calls.append("writer")
        return {"body":"complete body"}
    path = tmp_path / "image.png"
    path.write_bytes(b"test-image-bytes")
    def image():
        calls.append("image")
        return [path], {"score":50}, False
    text = run.call("writer", {"source":"v1"}, writer)
    run.call("text_approved", {"body":text}, lambda:text, external=False)
    run.images({"scene":"v1"}, image)
    resumed = runtime(store, key, "new-process")
    assert resumed.call("writer", {"source":"v1"}, writer) == text
    assert resumed.images({"scene":"v1"}, image)[0] == [path]
    assert calls == ["writer", "image"]
    assert resumed.call("writer", {"source":"v2"}, writer) == text
    assert calls == ["writer", "image", "writer"]


def test_changed_image_file_is_not_silently_replaced(stage_store, tmp_path):
    store, key = stage_store
    run = runtime(store,key)
    path = tmp_path / "image.png"
    path.write_bytes(b"original")
    calls=[]
    def generate():
        calls.append(1)
        return [path], {}, False
    run.images({},generate)
    path.write_bytes(b"changed")
    with pytest.raises(ValueError,match="IMAGE_CACHE_INVALID"):
        runtime(store,key,"resume").images({},generate)
    assert len(calls)==1


def test_remote_timeout_is_reconciled_not_reissued(stage_store):
    store,key=stage_store
    calls=[]
    def timeout():
        calls.append(1)
        raise TimeoutError("remote still may be running")
    with pytest.raises(TimeoutError):
        runtime(store,key).call("image",{},timeout)
    with pytest.raises(StagePending):
        runtime(store,key,"resume").call("image",{},timeout)
    assert len(calls)==1


def test_failed_local_stage_is_retryable(stage_store):
    store,key=stage_store
    with pytest.raises(ValueError):
        runtime(store,key).call("validation",{},lambda: int("bad"),external=False)
    assert runtime(store,key,"resume").call("validation",{},lambda: "ok",external=False)=="ok"


def test_old_worker_from_other_root_cannot_commit_after_takeover(stage_store):
    store,key=stage_store
    old=store.claim_stage(key,owner="old",lease_seconds=60)
    with store.store.connection() as conn:
        conn.execute("UPDATE agent.news_work_items SET lease_until=now()-interval '1 second' WHERE namespace=%s",(key.namespace,))
    newer=store.claim_stage(replace(key,root_run_id="another-root"),owner="new",lease_seconds=60)
    assert newer
    assert not store.commit_stage(old,payload={"result":"stale"})
    assert store.commit_stage(newer,payload={"result":"fresh"})
