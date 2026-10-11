from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import os
import pytest
from src.knowledge.store import KnowledgeStore
from src.workflow.model_capacity import ModelCapacityStore


@pytest.fixture
def capacity():
    if os.getenv("REDBOOK_TEST_POSTGRES")!="1":
        pytest.skip("real PostgreSQL opt-in required")
    value=ModelCapacityStore(KnowledgeStore.from_env())
    value.ensure_schema()
    group="test-capacity-"+uuid4().hex
    yield value,group
    with value.store.connection() as conn:
        conn.execute("DELETE FROM agent.model_request_leases WHERE group_key=%s",(group,))


@pytest.mark.parametrize("modality,limit",[("text",5),("image",2)])
def test_independent_pools_share_limit(capacity,modality,limit):
    store,group=capacity
    with ThreadPoolExecutor(max_workers=12) as pool:
        results=list(pool.map(lambda i: ModelCapacityStore(store.store).acquire(group,str(i),modality=modality,limit=limit),range(12)))
    assert sum(results)==limit


def test_unknown_remote_request_keeps_slot(capacity):
    store,group=capacity
    assert store.acquire(group,"old",modality="text",limit=1)
    store.finish(group,"old",modality="text",uncertain=True)
    with pytest.raises(RuntimeError,match="MODEL_CAPACITY_UNCERTAIN"):
        store.acquire(group,"new",modality="text",limit=1)


def test_stricter_inflight_limit_is_respected(capacity):
    store,group=capacity
    assert store.acquire(group,"strict",modality="text",limit=1)
    assert not store.acquire(group,"loose",modality="text",limit=5)


def _process_admission(group, modality, limit, ready, release, output):
    store = ModelCapacityStore(KnowledgeStore.from_env())
    ready.wait(10)
    output.put(sum(store.acquire(group, uuid4().hex, modality=modality, limit=limit) for _ in range(8)))
    release.wait(10)


@pytest.mark.parametrize("modality,limit", [("text", 5), ("image", 2)])
def test_two_real_processes_share_postgresql_admission(capacity, modality, limit):
    import multiprocessing
    _, group = capacity
    ctx = multiprocessing.get_context("spawn")
    ready, release, output = ctx.Event(), ctx.Event(), ctx.Queue()
    workers = [ctx.Process(target=_process_admission, args=(group, modality, limit, ready, release, output)) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        ready.set()
        assert sum(output.get(timeout=15) for _ in workers) == limit
    finally:
        release.set()
        for worker in workers:
            worker.join(15)
            if worker.is_alive():
                worker.terminate()
                worker.join()
    assert all(worker.exitcode == 0 for worker in workers)
