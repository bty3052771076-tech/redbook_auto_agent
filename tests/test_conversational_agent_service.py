"""Host control and approval races against real disposable PostgreSQL."""

from threading import Event, Thread as RealThread
from time import monotonic
from types import SimpleNamespace

import psycopg
import pytest

from backend import dialogue
from backend.dialogue import DialogueService
from test_conversational_agent_cancellation import no_external_work
from test_conversational_agent_store import dialogue_store


@pytest.fixture
def host(dialogue_store, tmp_path):
    store, cid = dialogue_store
    current = SimpleNamespace(root=tmp_path, conversation_store=store.conversations,
                              environment=lambda: {}, redact=lambda value: value)
    service = DialogueService(current, store=store, directory=tmp_path / 'dialogue')
    store.session(cid)
    return service, store, cid


def set_execution(store, cid, execution='fixture-execution', state='running', **extra):
    return store.transition(cid, state=state, execution_id=execution, **extra)


def permission_request(store, cid, execution='fixture-execution'):
    return store.request(cid, execution, 'permission', {
        'tool': 'generate_news', 'arguments': {'material_id': 'fixture-source'},
        'allowed_decisions': ['allow', 'deny']}, key='permission:' + execution)


def test_wrong_execution_control_cannot_change_state_requests_or_permissions(host):
    service, store, cid = host
    set_execution(store, cid, permissions={'generate_news': {'remaining': 1}})
    pending = permission_request(store, cid)
    before = store.session(cid)
    with pytest.raises(ValueError, match='EXECUTION_CHANGED'):
        service.control(cid, 'cancel', 'older-execution')
    after = store.session(cid)
    assert after == before
    assert store.query('SELECT status FROM agent.dialogue_requests WHERE id=%s', (pending['id'],), one=True)['status'] == 'pending'


@pytest.mark.parametrize('steering,expected', [(False, 'paused'), (True, 'active')])
def test_explicit_stops_pause_goal_but_steering_preserves_it(host, monkeypatch, steering, expected):
    service, store, cid = host
    set_execution(store, cid, goal={'id': 'fixture', 'objective': 'A goal', 'version': 1, 'state': 'active',
        'started_at': 0, 'stage': 'local', 'target': 1, 'turns': 0, 'max_turns': 8})

    class NoWorkerThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(dialogue, 'Thread', NoWorkerThread)
    state = service.control(cid, 'pause', 'fixture-execution', steering=steering)
    assert state['goal']['state'] == expected
    assert state['state'] == 'stopping'


def test_cancel_rechecks_identity_before_cancelling_new_executions_request(host, monkeypatch):
    service, store, cid = host
    set_execution(store, cid, execution='old-execution')
    new_request = []

    class StartWinsGuard:
        def __enter__(self):
            set_execution(store, cid, execution='new-execution', state='waiting_input')
            new_request.append(permission_request(store, cid, 'new-execution'))

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(service, 'guard', lambda identity: StartWinsGuard())
    with pytest.raises(ValueError, match='EXECUTION_CHANGED'):
        service.control(cid, 'cancel', 'old-execution')
    assert store.session(cid)['execution_id'] == 'new-execution'
    row = store.query('SELECT status FROM agent.dialogue_requests WHERE id=%s', (new_request[0]['id'],), one=True)
    assert row['status'] == 'pending', 'A stale stop cancelled the new execution approval'


def test_answer_retry_after_start_failure_does_not_permanently_consume_approval(host, monkeypatch):
    service, store, cid = host
    set_execution(store, cid, state='waiting_input')
    request = permission_request(store, cid)
    attempts = []

    def start(identity, inputs):
        attempts.append(inputs)
        if len(attempts) == 1:
            raise RuntimeError('injected before execution launch')
        store.transition(identity, state='completed', execution_id='resumed-fixture', input=inputs)
        return store.snapshot(identity)

    monkeypatch.setattr(service, '_start', start)
    body = {'version': request['version'], 'decision': 'allow'}
    with pytest.raises(RuntimeError, match='before execution launch'):
        service.answer(cid, request['id'], body)
    service.answer(cid, request['id'], body)
    assert len(attempts) == 2, 'Identical approval was swallowed after launch failed'
    assert attempts[0] == attempts[1]
    service.answer(cid, request['id'], body)
    assert len(attempts) == 2, 'Successful retry launched another execution'


def test_message_retry_after_thread_start_failure_preserves_one_message_and_relaunches(host, monkeypatch):
    service, store, cid = host
    starts = []

    class FailThenAdmitThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            starts.append(self.kwargs)
            if len(starts) == 1:
                raise RuntimeError('injected Thread.start failure')

    monkeypatch.setattr(dialogue, 'Thread', FailThenAdmitThread)
    with pytest.raises(RuntimeError, match='Thread.start failure'):
        service.message(cid, 'Use only local fixture materials.', 'fixture-message')
    service.message(cid, 'Use only local fixture materials.', 'fixture-message')
    assert len(starts) == 2, 'Session remained starting forever after no worker was launched'
    assert [message['id'] for message in store.conversations.get(cid)['messages']] == ['fixture-message']
    service.message(cid, 'Use only local fixture materials.', 'fixture-message')
    assert len(starts) == 2


def test_old_execution_request_cannot_authorize_a_new_execution(host, monkeypatch):
    service, store, cid = host
    set_execution(store, cid, execution='old-execution', state='waiting_input')
    request = permission_request(store, cid, 'old-execution')
    set_execution(store, cid, execution='new-execution', state='waiting_input')
    starts = []
    monkeypatch.setattr(service, '_start', lambda *args: starts.append(args))
    with pytest.raises(ValueError, match='EXECUTION_CHANGED|REQUEST_EXECUTION'):
        service.answer(cid, request['id'], {'version': request['version'], 'decision': 'allow'})
    assert starts == []
    assert store.query('SELECT status FROM agent.dialogue_requests WHERE id=%s', (request['id'],), one=True)['status'] == 'pending'


def test_answer_can_retry_when_real_start_persists_then_thread_start_fails(host, monkeypatch):
    service, store, cid = host
    set_execution(store, cid, state='waiting_input')
    request = permission_request(store, cid)
    starts = []

    class FailThenAdmitThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            starts.append(self.kwargs)
            if len(starts) == 1:
                raise RuntimeError('injected approval Thread.start failure')

    monkeypatch.setattr(dialogue, 'Thread', FailThenAdmitThread)
    body = {'version': request['version'], 'decision': 'allow'}
    with pytest.raises(RuntimeError, match='Thread.start failure'):
        service.answer(cid, request['id'], body)
    service.answer(cid, request['id'], body)
    assert len(starts) == 2, 'Approved request was permanently consumed before worker launch'
    assert store.session(cid)['input']['request_id'] == request['id']


def test_confirmed_exit_from_existing_none_returning_terminator_becomes_paused(host, monkeypatch):
    service, store, cid = host
    set_execution(store, cid, state='stopping', input={'control': 'pause'}, pid=987654321, process_identity='fixture-birth')
    alive = [True]
    process = SimpleNamespace(pid=987654321)
    service.processes[cid] = ('fixture-execution', process, 'fixture-birth')
    monkeypatch.setattr(dialogue, 'process_alive', lambda *args: alive[0])

    def terminate(pid, birth):
        assert (pid, birth) == (987654321, 'fixture-birth')
        alive[0] = False
        # The existing local_process.terminate_task_tree returns None, not bool.

    monkeypatch.setattr(dialogue, 'terminate_task_tree', terminate)
    service._stop(cid, 'fixture-execution', 'pause')
    assert store.session(cid)['state'] == 'paused'


def test_unconfirmed_process_exit_never_claims_paused(host, monkeypatch):
    service, store, cid = host
    set_execution(store, cid, state='stopping', input={'control': 'pause'}, pid=987654321, process_identity='fixture-birth')
    service.processes[cid] = ('fixture-execution', SimpleNamespace(pid=987654321), 'fixture-birth')
    alive = [True]
    monkeypatch.setattr(dialogue, 'process_alive', lambda *args: alive[0])
    monkeypatch.setattr(dialogue, 'terminate_task_tree', lambda *args: None)
    service._stop(cid, 'fixture-execution', 'pause')
    assert store.session(cid)['state'] == 'stopping'


def test_repeated_control_returns_without_waiting_for_blocked_termination(host, monkeypatch):
    service, store, cid = host
    set_execution(store, cid, pid=987654321, process_identity='fixture-birth')
    service.processes[cid] = ('fixture-execution', SimpleNamespace(pid=987654321), 'fixture-birth')
    entered, release, repeated_returned = Event(), Event(), Event()
    children, errors = [], []
    alive = [True]
    monkeypatch.setattr(dialogue, 'process_alive', lambda *args: alive[0])

    def launch(**kwargs):
        thread = RealThread(**kwargs)
        children.append(thread)
        return thread

    def terminate(*args):
        entered.set()
        assert release.wait(5)
        alive[0] = False

    def repeat_control():
        try:
            service.control(cid, 'cancel', 'fixture-execution')
        except Exception as exc:
            errors.append(exc)
        finally:
            repeated_returned.set()

    monkeypatch.setattr(dialogue, 'Thread', launch)
    monkeypatch.setattr(dialogue, 'terminate_task_tree', terminate)
    repeater = None
    try:
        started = monotonic()
        service.control(cid, 'pause', 'fixture-execution')
        assert monotonic() - started < 1
        assert entered.wait(2)
        repeater = RealThread(target=repeat_control)
        repeater.start()
        assert repeated_returned.wait(.75), 'Control waited for slow termination while holding the per-conversation guard'
        assert errors == []
    finally:
        release.set()
        if repeater is not None:
            repeater.join(timeout=5)
            assert not repeater.is_alive()
        for thread in children:
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_database_unavailable_fails_startup_without_memory_or_old_agent_fallback(host, monkeypatch, tmp_path):
    service, store, _ = host

    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError('isolated injected database outage')

    monkeypatch.setattr(store.knowledge, 'connection', unavailable)
    with pytest.raises(psycopg.OperationalError, match='database outage'):
        DialogueService(service.current, store=store, directory=tmp_path / 'unavailable')
