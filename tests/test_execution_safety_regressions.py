"""Offline execution-boundary regressions; workers and PostgreSQL are doubles."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import socket
from threading import Event, Lock, Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apps import web_service
from apps.web_service import Workbench
from backend import app as api, capabilities
from backend.plan_service import PlanService


class Conversations:
    def __init__(self):
        self.rows = {}

    def get(self, identity):
        return deepcopy(self.rows[identity])

    def save(self, row):
        self.rows[row['id']] = deepcopy(row)
        return deepcopy(row)

    def list(self, *, limit=100):
        return list(deepcopy(self.rows).values())[:limit]


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail('Execution safety test attempted real external work')

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(web_service.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(api.KnowledgeStore, 'from_env', forbidden)
    monkeypatch.setattr(api, 'ensure_review_schema', forbidden)


@pytest.fixture
def current(tmp_path, monkeypatch):
    instance = Workbench(tmp_path, conversation_store=Conversations())
    monkeypatch.setattr(instance, 'environment', lambda: {})
    monkeypatch.setattr(instance, 'providers', lambda: {'bindings': {}})
    monkeypatch.setattr(instance, 'models', lambda: {'rows': []})
    monkeypatch.setattr(instance, 'plan', lambda *args: (['offline-worker'], {}))
    monkeypatch.setattr(instance, '_agent_memory_for_execution', lambda *args: {})
    return instance


@pytest.mark.parametrize('parent_secret', ['fixture-parent-XYZ789', 'fixture-local-ABC123-extra'])
def test_conflicting_environment_secrets_are_redacted_before_persistence(current, monkeypatch, parent_secret):
    local_secret = 'fixture-local-ABC123'
    monkeypatch.setenv('NEWS_API_KEY', parent_secret)
    monkeypatch.setattr(web_service.gui, 'load_env_file', lambda _: {'NEWS_API_KEY': local_secret})
    child = web_service.gui.build_subprocess_env({'NEWS_API_KEY': local_secret})
    assert child['NEWS_API_KEY'] == local_secret
    job = {'id': uuid4().hex, 'events': [], 'post_ids': []}
    current.event(job, f'echo {child["NEWS_API_KEY"]} {parent_secret}')
    hidden = '\u5df2\u9690\u85cf'
    assert job['message'] == f'echo [{hidden}] [{hidden}]'
    for path in (current.directory / 'jobs' / (job['id'] + '.json'),
                 current.directory / (job['id'] + '.log')):
        saved = path.read_text(encoding='utf-8')
        assert local_secret not in saved and parent_secret not in saved


def test_database_recovery_initializes_one_workbench_for_concurrent_requests(tmp_path, monkeypatch):
    first_constructing, contender, release = Event(), Event(), Event()
    created, migrations = [], []
    attempts = Lock()

    class ObservedLock:
        def __enter__(self):
            if first_constructing.is_set():
                contender.set()
            attempts.acquire()

        def __exit__(self, *args):
            attempts.release()

    def construct(*, root):
        assert root == tmp_path
        if first_constructing.is_set():
            contender.set()
        first_constructing.set()
        assert release.wait(5)
        instance = Workbench(root, conversation_store=Conversations())
        created.append(instance)
        return instance

    monkeypatch.setattr(api, 'RUNTIME', tmp_path)
    monkeypatch.setattr(api.app.state, 'service', None)
    monkeypatch.setattr(api.app.state, 'review_schema_ready', False)
    monkeypatch.setattr(api, '_service_lock', ObservedLock(), raising=False)
    monkeypatch.setattr(api, 'Workbench', construct)
    available = [False]
    monkeypatch.setattr(api.KnowledgeStore, 'from_env', lambda: SimpleNamespace(
        status=lambda: {'status': 'ready' if available[0] else 'unavailable'}))
    monkeypatch.setattr(api, 'ensure_review_schema', lambda: migrations.append('migration'))
    with pytest.raises(ValueError, match='POSTGRES_UNAVAILABLE'):
        api.service()
    assert api.app.state.service is None
    available[0] = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(api.service)
        try:
            assert first_constructing.wait(5)
            second = pool.submit(api.service)
            assert contender.wait(5)
        finally:
            release.set()
        refs = [first.result(timeout=5), second.result(timeout=5)]
    assert len(created) == 1
    assert refs[0] is refs[1] is api.app.state.service
    assert migrations == ['migration']


def test_failed_service_construction_releases_initialization_lock(current, monkeypatch):
    monkeypatch.setattr(api.app.state, 'service', None)
    monkeypatch.setattr(api.app.state, 'review_schema_ready', True)
    monkeypatch.setattr(api.KnowledgeStore, 'from_env', lambda: SimpleNamespace(status=lambda: {'status': 'ready'}))

    def failed(**kwargs):
        raise OSError('injected constructor failure')

    monkeypatch.setattr(api, 'Workbench', failed)
    with pytest.raises(OSError, match='constructor failure'):
        api.service()
    assert api.app.state.service is None
    monkeypatch.setattr(api, 'Workbench', lambda **kwargs: current)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(api.service).result(timeout=5) is current


@pytest.fixture
def confirmed_plan(current, monkeypatch):
    manager = SimpleNamespace(
        store=SimpleNamespace(namespace='offline-execution'),
        context=SimpleNamespace(policy=lambda *args: ({'mode': 'manual'}, 0)),
        plan_capabilities=lambda *args: {'memory': [], 'skills': []},
        freeze_plan=lambda *args, **kwargs: {'snapshot_id': 'offline'},
    )
    monkeypatch.setattr(capabilities, 'manager', lambda *args: manager)
    monkeypatch.setattr(PlanService, 'configuration_fingerprint', lambda self: 'offline-config')
    monkeypatch.setattr(PlanService, '_model_runtime', lambda *args: {
        'snapshots': {}, 'legacy_roles': {}, 'legacy_configs': {}, 'namespace': 'offline'})
    cid = current.create_agent_conversation()['id']
    plan = current.append_agent_message(cid, '\u751f\u62101\u6761\u6bcf\u65e5\u65b0\u95fb\uff0c\u4e0d\u4e0a\u4f20')['plan']
    return cid, plan['id'], {'version': plan['version'], 'skill_mode': 'off', 'skill_names': []}


@pytest.mark.parametrize('restart', [False, True])
def test_confirm_retries_unstarted_worker_with_original_execution_identity(current, confirmed_plan, monkeypatch, restart):
    cid, pid, body = confirmed_plan
    ps = PlanService(current)

    def failed_start(self):
        raise RuntimeError('injected before thread launch')

    with monkeypatch.context() as patch:
        patch.setattr(Thread, 'start', failed_start)
        with pytest.raises(RuntimeError, match='before thread launch'):
            ps.confirm(cid, pid, body, 'offline-confirm')
    saved = current._read_agent_conversation(cid)['plans'][-1]
    identity = saved['execution_request_id']
    assert saved['submission_state'] == 'starting'
    assert identity not in current.jobs
    assert not (current.directory / 'jobs' / (identity + '.json')).exists()
    if restart:
        current = Workbench(current.root, conversation_store=current.conversation_store)
        monkeypatch.setattr(current, 'plan', lambda *args: (['offline-worker'], {}))
        ps = PlanService(current)
    started = Event()
    calls = []

    def worker(job, args, env):
        calls.append(job['id'])
        started.set()

    monkeypatch.setattr(current, '_run', worker)
    retried = ps.confirm(cid, pid, body, 'offline-confirm')
    assert retried['id'] == identity and retried['agent_run_id'] == identity
    assert started.wait(5)
    assert ps.confirm(cid, pid, body, 'offline-confirm')['id'] == identity
    assert calls == [identity]
    assert current._read_agent_conversation(cid)['runs'] == [identity]


def test_started_thread_error_keeps_journal_and_does_not_replay(current, confirmed_plan, monkeypatch):
    cid, pid, body = confirmed_plan
    started, release = Event(), Event()
    threads, calls = [], []
    real_start = Thread.start

    def worker(job, args, env):
        calls.append(job['id'])
        started.set()
        release.wait(5)

    def start_then_raise(thread):
        real_start(thread)
        threads.append(thread)
        raise RuntimeError('injected after thread launch')

    monkeypatch.setattr(current, '_run', worker)
    ps = PlanService(current)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Thread, 'start', start_then_raise)
            with pytest.raises(RuntimeError, match='after thread launch'):
                ps.confirm(cid, pid, body, 'offline-confirm')
        assert started.wait(5)
        identity = current._read_agent_conversation(cid)['plans'][-1]['execution_request_id']
        assert (current.directory / 'jobs' / (identity + '.json')).is_file()
        assert ps.confirm(cid, pid, body, 'offline-confirm')['id'] == identity
        assert calls == [identity]
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_late_thread_bootstrap_cannot_execute_a_rolled_back_submission(current, monkeypatch):
    identity = uuid4().hex
    pending, admitted, calls = [], [], []
    started, release = Event(), Event()
    real_start = Thread.start
    request = {'kind': 'agent', 'reserved_job_id': identity}

    def delayed_start(thread):
        pending.append(thread)
        raise RuntimeError('injected before bootstrap acknowledgement')

    def worker(job, *args):
        calls.append(job['id'])
        started.set()
        release.wait(5)

    def admitted_start(thread):
        admitted.append(thread)
        real_start(thread)

    monkeypatch.setattr(current, '_run', worker)
    with monkeypatch.context() as patch:
        patch.setattr(Thread, 'start', delayed_start)
        with pytest.raises(RuntimeError, match='bootstrap acknowledgement'):
            current.submit(request, identity)
    assert identity not in current.jobs
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Thread, 'start', admitted_start)
            retried = current.submit(request, identity)
        assert retried['id'] == identity and started.wait(5)
        real_start(pending[0])
        pending[0].join(timeout=5)
        assert not pending[0].is_alive()
        assert calls == [identity]
        assert current.submit(request, identity)['id'] == identity
        journal = current.directory / 'jobs' / (identity + '.json')
        assert json.loads(journal.read_text(encoding='utf-8'))['id'] == identity
    finally:
        release.set()
        for thread in admitted:
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.parametrize('status', ['queued', 'running', 'interrupted', 'failed'])
def test_existing_journal_without_start_failure_proof_never_replays(current, confirmed_plan, monkeypatch, status):
    cid, pid, body = confirmed_plan
    ps = PlanService(current)
    claim = ps.claim(cid, pid, body, 'offline-confirm')
    identity = claim['plan']['execution_request_id']
    current.persist({'id': identity, 'agent_run_id': identity, 'kind': 'agent', 'status': status})
    monkeypatch.setattr(current, 'submit', lambda *args: pytest.fail('Uncertain start was replayed'))
    result = ps.confirm(cid, pid, body, 'offline-confirm')
    assert result['id'] == identity and result['status'] == status


def test_failed_journal_cleanup_retains_idempotency_guard(current, monkeypatch):
    identity = uuid4().hex
    request = {'kind': 'agent', 'reserved_job_id': identity}
    journal = current.directory / 'jobs' / (identity + '.json')

    def failed_start(self):
        raise RuntimeError('injected before thread launch')

    original_unlink = Path.unlink

    def failed_unlink(path, *args, **kwargs):
        if path == journal:
            raise OSError('injected journal cleanup failure')
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Thread, 'start', failed_start)
        patch.setattr(Path, 'unlink', failed_unlink)
        with pytest.raises((RuntimeError, OSError)):
            current.submit(request, identity)
    assert identity in current.jobs and journal.is_file()
    monkeypatch.setattr(Thread, 'start', lambda self: pytest.fail('Uncertain journal was replayed'))
    assert current.submit(request, identity)['id'] == identity
    assert json.loads(journal.read_text(encoding='utf-8'))['id'] == identity
