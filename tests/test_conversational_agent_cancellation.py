"""Cancellation acceptance tests: local doubles, never actual child processes."""

from copy import deepcopy
from concurrent.futures import CancelledError
import json
import socket
import sys
from threading import Event, Thread
from time import monotonic
from uuid import uuid4

import pytest

from apps import web_service
from apps.web_service import Workbench
from test_execution_safety_regressions import Conversations
from test_conversation_continuity import pg_isolated
from test_run_resume_contract import isolated, start_plan


@pytest.fixture(autouse=True)
def no_external_work(monkeypatch):
    original_connect = socket.socket.connect

    def forbidden(*args, **kwargs):
        pytest.fail('Cancellation test attempted network or a real child process')

    def guarded_connect(sock, address):
        # Windows asyncio creates its self-pipe with socket.socketpair().
        caller = sys._getframe(1)
        if caller.f_code is getattr(socket.socketpair, '__code__', None):
            return original_connect(sock, address)
        return forbidden(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded_connect)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(web_service.subprocess, 'Popen', forbidden)


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    current = Workbench(tmp_path, conversation_store=Conversations())
    monkeypatch.setattr(current, 'plan', lambda *args: (['offline-worker'], {}))
    monkeypatch.setattr(current, '_agent_memory_for_execution', lambda *args: {})
    return current


def queued_job(current):
    job = {'id': uuid4().hex, 'kind': 'agent', 'status': 'queued',
           'events': [], 'post_ids': [], 'started_at': None, 'ended_at': None}
    current.jobs[job['id']] = job
    current.persist(job)
    return job


def test_stop_before_process_launch_persists_cancellation(workbench):
    job = queued_job(workbench)
    stopped = workbench.stop(job['id'])
    assert stopped['status'] == 'cancelled'
    assert stopped['ended_at'] is not None
    journal = workbench.directory / 'jobs' / (job['id'] + '.json')
    assert json.loads(journal.read_text(encoding='utf-8'))['status'] == 'cancelled'
    assert workbench.process is None


def test_cancelled_worker_cannot_launch_after_delayed_bootstrap(workbench, monkeypatch):
    pending = []
    with monkeypatch.context() as patch:
        patch.setattr(Thread, 'start', lambda thread: pending.append(thread))
        job = workbench.submit({'kind': 'agent'}, 'cancel-before-bootstrap-0001')
    assert len(pending) == 1
    workbench.stop(job['id'])
    pending[0].run()
    assert workbench.jobs[job['id']]['status'] == 'cancelled'
    assert workbench.jobs[job['id']]['started_at'] is None


class SleepingProcess:
    """An idle stdout reader is released only by a simulated stop or cleanup."""

    pid = 987654321

    def __init__(self):
        self.reading = Event()
        self.released = Event()
        self.returncode = None
        self.stdout = self
        self.terminate_calls = 0

    def __iter__(self):
        self.reading.set()
        assert self.released.wait(5), 'Test did not release its fake worker'
        return iter(())

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -15
        self.released.set()

    def wait(self, *args, **kwargs):
        assert self.released.wait(5)
        return self.returncode

    def poll(self):
        return self.returncode


def test_stop_interrupts_idle_worker_and_keeps_retained_posts(workbench, monkeypatch):
    from src.integrations import local_process
    from src.workflow import resources

    process = SleepingProcess()
    kills, releases = [], []
    monkeypatch.setattr(web_service.subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(resources, 'process_identity', lambda pid: 'fake-process-identity')

    def terminate_tree(pid, identity):
        assert (pid, identity) == (process.pid, 'fake-process-identity')
        kills.append(pid)
        process.terminate()

    monkeypatch.setattr(local_process, 'terminate_task_tree', terminate_tree)
    monkeypatch.setattr(local_process, 'release_process_services', lambda *args: releases.append(args))
    job = queued_job(workbench)
    job['post_ids'] = ['b' * 32]
    job['reviewed_post_ids'] = ['b' * 32]
    retained = deepcopy((job['post_ids'], job['reviewed_post_ids']))
    worker = Thread(target=workbench._run, args=(job, ['offline-worker'], {}))
    worker.start()
    try:
        assert process.reading.wait(3)
        started = monotonic()
        workbench.stop(job['id'])
        worker.join(timeout=1)
        assert not worker.is_alive(), 'Stop did not interrupt a silent worker promptly'
        assert monotonic() - started < 1.5
        assert job['status'] == 'cancelled'
        assert (job['post_ids'], job['reviewed_post_ids']) == retained
        assert process.terminate_calls == 1
        workbench.stop(job['id'])
        assert process.terminate_calls == 1
        assert releases and set(releases) == {(process.pid, 'fake-process-identity')}
    finally:
        process.terminate()
        worker.join(timeout=5)
        assert not worker.is_alive()


def test_stop_cannot_signal_another_jobs_process(workbench, monkeypatch):
    from src.integrations import local_process

    process = SleepingProcess()
    workbench.process = process
    workbench._process_job_id = uuid4().hex
    workbench._process_identity = 'other-owned-process'
    signals = []
    monkeypatch.setattr(local_process, 'terminate_task_tree', lambda *args: signals.append(args))
    job = queued_job(workbench)
    with pytest.raises(ValueError, match='JOB_PROCESS_OWNERSHIP_MISMATCH'):
        workbench.stop(job['id'])
    assert signals == []
    assert job['status'] == 'queued' and not job.get('stop_requested_at')
    assert workbench.process is process


def test_recycled_pid_is_not_signalled_and_stop_remains_unconfirmed(workbench, monkeypatch):
    from src.integrations import local_process

    process = SleepingProcess()
    job = queued_job(workbench)
    job['status'] = 'running'
    workbench.process = process
    workbench._process_job_id = job['id']
    workbench._process_identity = 'original-birth'
    monkeypatch.setattr(local_process, 'process_identity', lambda pid: 'replacement-birth')
    monkeypatch.setattr(local_process, 'process_alive', lambda *args: pytest.fail('Recycled PID reached process work'))
    threads = []
    real_start = Thread.start

    def capture(thread):
        threads.append(thread)
        real_start(thread)

    monkeypatch.setattr(Thread, 'start', capture)
    try:
        workbench.stop(job['id'])
    finally:
        for thread in threads:
            thread.join(timeout=3)
            assert not thread.is_alive()
    assert process.terminate_calls == 0
    assert job['status'] == 'stopping' and job['ended_at'] is None
    assert 'PROCESS_STOP_UNCONFIRMED' in job['stop_error']
    assert workbench.process is process


def test_slow_termination_returns_promptly_and_repeated_stop_dispatches_once(workbench, monkeypatch):
    from src.integrations import local_process

    process = SleepingProcess()
    job = queued_job(workbench)
    job['status'] = 'running'
    workbench.process = process
    workbench._process_job_id = job['id']
    workbench._process_identity = 'original-birth'
    entered, release = Event(), Event()
    calls, threads = [], []
    real_start = Thread.start

    def terminate(pid, identity):
        calls.append((pid, identity))
        entered.set()
        assert release.wait(5)

    def capture(thread):
        threads.append(thread)
        real_start(thread)

    monkeypatch.setattr(local_process, 'terminate_task_tree', terminate)
    monkeypatch.setattr(Thread, 'start', capture)
    try:
        started = monotonic()
        result = workbench.stop(job['id'])
        assert monotonic() - started < .5
        assert entered.wait(2)
        assert result['status'] == 'stopping'
        workbench.stop(job['id'])
        assert calls == [(process.pid, 'original-birth')] and len(threads) == 1
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=3)
            assert not thread.is_alive()
    assert job['status'] == 'stopping' and job['ended_at'] is None
    assert 'PROCESS_STOP_UNCONFIRMED' in job['stop_error']


def test_repeated_resource_release_preserves_other_workers_lease(tmp_path, monkeypatch):
    from src.integrations import local_process

    identity = 'fake-process-identity'
    directory = tmp_path / 'data/runs/local-services'
    directory.mkdir(parents=True)
    owned = directory / 'owned.json'
    shared = directory / 'shared.json'
    target = {'pid': SleepingProcess.pid, 'identity': identity}
    other = {'pid': 987654322, 'identity': 'other-worker-identity'}
    local_process.write_state(owned, {'name': 'owned-fixture-service', 'members': {'target': target}})
    local_process.write_state(shared, {'name': 'shared-fixture-service', 'members': {'target': target, 'other': other}})
    stopped = []
    monkeypatch.setattr(local_process, 'coordination_root', lambda: tmp_path)
    monkeypatch.setattr(local_process, 'process_alive', lambda pid, birth: (pid, birth) == (other['pid'], other['identity']))
    monkeypatch.setattr(local_process, 'terminate_owned', lambda row: stopped.append(row['name']) or True)
    local_process.release_process_services(SleepingProcess.pid, identity)
    local_process.release_process_services(SleepingProcess.pid, identity)
    assert stopped == ['owned-fixture-service']
    assert local_process.read_state(owned) == {}
    assert local_process.read_state(shared)['members'] == {'other': other}


def test_memory_admission_wait_stops_before_work_without_touching_shared_leases(tmp_path, monkeypatch):
    from src.workflow import resources

    monkeypatch.setenv('REDBOOK_MEMORY_ENABLED', '1')
    monkeypatch.setattr(resources, 'coordination_root', lambda: tmp_path)
    monkeypatch.setattr(resources, 'process_identity', lambda pid: 'fixture-worker')
    monkeypatch.setattr(resources, 'sample_memory', lambda: resources.MemorySample(
        total=8 * resources.GIB, available=64 * resources.MIB))
    monkeypatch.setattr(resources, '_record', lambda *args: None)
    waiting, stop = Event(), Event()
    work, errors = [], []

    def wait_for_memory():
        try:
            with resources.memory_slot('image', stop_event=stop, progress=lambda message: waiting.set()):
                work.append('must-not-start')
        except CancelledError as exc:
            errors.append(str(exc))

    worker = Thread(target=wait_for_memory)
    worker.start()
    try:
        assert waiting.wait(3)
        started = monotonic()
        stop.set()
        worker.join(timeout=1)
        assert not worker.is_alive(), 'Memory admission ignored stop_event'
        assert monotonic() - started < 1.5
        assert errors == ['stopped while waiting for memory admission']
        assert work == []
        state = json.loads((tmp_path / 'data/runs/resource-admission/state.json').read_text(encoding='utf-8'))
        assert state['leases'] == {}
    finally:
        stop.set()
        worker.join(timeout=5)
        assert not worker.is_alive()


@pytest.mark.parametrize('text,action', [
    ('\u6682\u505c', 'pause_goal'),
    ('\u53d6\u6d88\u4efb\u52a1', 'cancel_goal'),
])
def test_conversation_control_immediately_stops_active_attempt_once(pg_isolated, monkeypatch, text, action):
    current, client, commands = pg_isolated
    cid, _, run = start_plan(pg_isolated)
    before = client.get(f'/api/conversations/{cid}/goals').json()['rows'][0]
    current.jobs[run['id']]['status'] = 'running'
    stopped = []
    monkeypatch.setattr(current, 'stop', lambda identity: stopped.append(identity) or current.jobs[identity])
    headers = {'X-Workbench': '1', 'Idempotency-Key': 'immediate-control-' + action}
    response = client.post(f'/api/conversations/{cid}/messages', headers=headers, json={'content': text})
    assert response.status_code in {200, 202}, response.text
    assert response.json()['action'] == action
    assert stopped == [run['id']], 'Control was queued but did not reach the active worker'
    after = response.json()['goal']
    assert after['state'] == ('paused' if action == 'pause_goal' else 'cancelled')
    assert after['lease_epoch'] > before['lease_epoch'], 'Old worker retains write authority after stop'
    again = client.post(f'/api/conversations/{cid}/messages', headers=headers, json={'content': text})
    assert again.status_code in {200, 202}, again.text
    assert again.json()['turn']['turn_id'] == response.json()['turn']['turn_id']
    assert stopped == [run['id']]
    assert again.json()['goal']['lease_epoch'] == after['lease_epoch']
    assert len(commands) == 1


def test_cancel_fences_old_worker_writes_and_continue_cannot_revive_it(pg_isolated, monkeypatch):
    from backend.conversation_service import ConversationService

    current, client, commands = pg_isolated
    cid, _, run = start_plan(pg_isolated)
    coordinator = ConversationService(current)
    before = coordinator.ensure_goals(cid)[0]
    monkeypatch.setattr(current, 'stop', lambda identity: current.jobs[identity])
    response = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                           json={'content': '\u53d6\u6d88\u4efb\u52a1'})
    assert response.status_code in {200, 202}, response.text
    goal = response.json()['goal']
    assert goal['state'] == 'cancelled' and goal['lease_epoch'] > before['lease_epoch']
    with pytest.raises(ValueError, match='GOAL_LEASE_LOST'):
        coordinator.goals.set_state(coordinator.namespace, goal['goal_id'], 'running', expected_epoch=before['lease_epoch'])
    with pytest.raises(ValueError, match='GOAL_LEASE_LOST'):
        coordinator.goals.record_artifact(coordinator.namespace, goal['goal_id'], before['plan']['jobs'][0]['job_id'],
            'fixture-post', 'fixture-v1', state='generated', lease_epoch=before['lease_epoch'])
    continued = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                            json={'content': '\u7ee7\u7eed'})
    assert continued.status_code in {200, 202}, continued.text
    assert continued.json()['run'] is None
    after = coordinator.goals.snapshot(coordinator.namespace, goal['goal_id'])
    assert after['state'] == 'cancelled' and after['artifacts'] == before['artifacts']
    assert after['active_attempt_id'] == run['id']
    assert len(commands) == 1
