"""Opt-in real PostgreSQL process exclusion; written but not run for this change."""

import multiprocessing
import os
import hashlib
import json
from uuid import uuid4

import pytest

from src.agent.artifact_store import AgentArtifactStore


def hold_run_lease(run_id, ready, release):
    try:
        with AgentArtifactStore().lease(run_id):
            ready.put(("acquired", os.getpid()))
            if not release.wait(30):
                raise RuntimeError("test parent did not release lease")
    except BaseException as exc:
        ready.put(("failed", type(exc).__name__))
        raise


@pytest.mark.skipif(os.getenv("REDBOOK_TEST_POSTGRES") != "1", reason="requires explicit local test PG opt-in")
def test_real_postgres_session_lease_excludes_another_process_and_releases_on_exit():
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Event()
    run_id = "test-lease-" + uuid4().hex
    child = context.Process(target=hold_run_lease, args=(run_id, ready, release))
    child.start()
    try:
        status, child_pid = ready.get(timeout=15)
        assert status == "acquired"
        assert child_pid != os.getpid()
        store = AgentArtifactStore()
        key = int.from_bytes(hashlib.sha256(f'redbook:agent-run:{run_id}'.encode('ascii')).digest()[:8], 'big', signed=True)
        bits = key & ((1 << 64) - 1)
        with store.store.connection() as conn:
            locks = conn.execute("""
                SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted
                  AND classid::bigint=%s AND objid::bigint=%s AND objsubid=1
            """, (bits >> 32, bits & ((1 << 32) - 1))).fetchall()
            observer = conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
        assert len(locks) == 1
        assert locks[0]['pid'] != observer
        print(json.dumps({'run_id': run_id, 'holder_process_pid': child_pid,
                          'observer_process_pid': os.getpid(), 'holder_pg_pid': locks[0]['pid'],
                          'observer_pg_pid': observer, 'lock_kind': 'session advisory bigint'}))
        with pytest.raises(RuntimeError, match="AGENT_RUN_ALREADY_ACTIVE"):
            with store.lease(run_id):
                pytest.fail("another process entered an active run")
        with store.lease(run_id + "-other"):
            pass
        release.set()
        child.join(timeout=15)
        assert child.exitcode == 0
        with store.lease(run_id):
            pass
    finally:
        release.set()
        child.join(timeout=5)
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
        ready.close()
        ready.join_thread()
