"""Public assistant routes: session checks and read-only refresh contracts."""

from copy import deepcopy
from threading import Event, Thread
from time import monotonic
from uuid import uuid4

import pytest

from test_conversation_continuity import pg_isolated
from test_conversational_agent_cancellation import no_external_work
from test_run_resume_contract import isolated


@pytest.fixture
def assistant_api(pg_isolated, monkeypatch, tmp_path):
    from backend import app as api
    from backend.dialogue import DialogueService

    current, client, _ = pg_isolated
    service = DialogueService(current, directory=tmp_path / 'assistant-host')
    monkeypatch.setattr(api.app.state, 'dialogue', service)
    cid = current.create_agent_conversation()['id']
    return service, client, cid


def test_assistant_refresh_does_not_create_messages_plans_or_workers(pg_isolated):
    current, client, commands = pg_isolated
    cid = current.create_agent_conversation('isolated assistant refresh')['id']
    before = deepcopy(current._read_agent_conversation(cid))
    states = []
    for _ in range(3):
        response = client.get(f'/api/conversations/{cid}/assistant')
        assert response.status_code == 200, response.text
        assert response.headers['Cache-Control'] == 'no-store'
        assert isinstance(response.json().get('engine'), str)
        states.append(response.json())
    after = current._read_agent_conversation(cid)
    for field in ('messages', 'plans', 'runs'):
        assert after[field] == before[field], f'Refresh changed {field}'
    assert len(current.jobs) == 0 and commands == []


@pytest.mark.parametrize('method,suffix,body', [
    ('GET', '', None),
    ('POST', '/messages', {'content': 'offline request'}),
    ('POST', '/control', {'action': 'pause'}),
    ('POST', '/goal', {'action': 'set', 'version': 0, 'objective': 'Fixture goal'}),
    ('POST', '/requests/fixture-request', {'decision': 'deny'}),
])
def test_assistant_routes_reject_missing_session(pg_isolated, method, suffix, body):
    current, client, commands = pg_isolated
    cid = current.create_agent_conversation()['id']
    client.cookies.clear()
    response = client.request(method, f'/api/conversations/{cid}/assistant{suffix}',
                              headers={'X-Workbench': '1'}, json=body)
    assert response.status_code == 403, response.text
    assert commands == [] and len(current.jobs) == 0


@pytest.mark.parametrize('suffix,body', [
    ('/messages', {'content': 'offline request'}),
    ('/control', {'action': 'cancel'}),
    ('/goal', {'action': 'set', 'version': 0, 'objective': 'Fixture goal'}),
    ('/requests/fixture-request', {'decision': 'allow'}),
])
def test_assistant_writes_require_workbench_header(pg_isolated, suffix, body):
    current, client, commands = pg_isolated
    cid = current.create_agent_conversation()['id']
    response = client.post(f'/api/conversations/{cid}/assistant{suffix}', json=body)
    assert response.status_code == 403, response.text
    assert commands == [] and len(current.jobs) == 0


def test_assistant_rejects_external_origin_before_any_work(pg_isolated):
    current, client, commands = pg_isolated
    cid = current.create_agent_conversation()['id']
    response = client.post(f'/api/conversations/{cid}/assistant/messages',
        headers={'X-Workbench': '1', 'Origin': 'https://untrusted.invalid'}, json={'content': 'offline request'})
    assert response.status_code == 403, response.text
    assert commands == [] and len(current.jobs) == 0


def test_assistant_unknown_conversation_is_not_created_implicitly(pg_isolated):
    current, client, commands = pg_isolated
    before = current.conversation_store.list()
    response = client.get(f'/api/conversations/{uuid4().hex}/assistant')
    assert response.status_code in {400, 404}, response.text
    assert current.conversation_store.list() == before
    assert commands == [] and len(current.jobs) == 0


def test_goal_lifecycle_and_stale_write_protection(assistant_api, monkeypatch):
    service, client, cid = assistant_api
    path = f'/api/conversations/{cid}/assistant'
    launches = []
    def start(cid, inputs, *, goal_version=None):
        launches.append((inputs, goal_version))
        return service.snapshot(cid)
    monkeypatch.setattr(service, '_start', start)
    def goal(action, version, **fields):
        return client.post(path + '/goal', headers={'X-Workbench': '1'},
                           json=dict(action=action, version=version, **fields))
    assert goal('set', 0, objective=' ').status_code == 409
    first = goal('set', 0, objective='Create three local fixture drafts', stage='local', target=3)
    assert first.status_code == 200, first.text
    original = first.json()['goal']
    assert original['state'] == 'active' and original['target'] == 3
    assert goal('set', 0, objective='Duplicate request').status_code == 409
    assert goal('complete', 1).status_code == 409
    assert goal('pause', 1).json()['goal']['state'] == 'paused'
    assert goal('edit', 2, objective='Two local fixture drafts', target=2).json()['goal']['id'] == original['id']
    assert client.get(path).json()['goal']['target'] == 2
    assert goal('resume', 3).json()['goal']['state'] == 'active'
    assert goal('clear', 4).json()['goal'] is None
    assert launches == [({'kind': 'goal'}, 1), ({'kind': 'goal'}, 4)]
    assert client.get(path).json()['goal_version'] == 5


def test_message_api_is_async_and_retries_or_refresh_do_not_start_another_worker(assistant_api, monkeypatch):
    from backend import dialogue

    service, client, cid = assistant_api
    entered, release = Event(), Event()
    threads, calls = [], []

    def drive(identity, execution):
        calls.append(execution)
        entered.set()
        assert release.wait(5)

    def launch(**kwargs):
        thread = Thread(**kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(service, '_drive', drive)
    monkeypatch.setattr(dialogue, 'Thread', launch)
    body = {'content': 'Offline fixture only.', 'client_message_id': uuid4().hex}
    headers = {'X-Workbench': '1'}
    try:
        started = monotonic()
        first = client.post(f'/api/conversations/{cid}/assistant/messages', headers=headers, json=body)
        assert first.status_code == 202, first.text
        assert monotonic() - started < 1
        assert entered.wait(2)
        again = client.post(f'/api/conversations/{cid}/assistant/messages', headers=headers, json=body)
        assert again.status_code == 202, again.text
        for _ in range(3):
            assert client.get(f'/api/conversations/{cid}/assistant').status_code == 200
        conflict = client.post(f'/api/conversations/{cid}/assistant/messages', headers=headers,
            json={**body, 'content': 'Different content cannot reuse that identity.'})
        assert conflict.status_code == 409, conflict.text
        assert len(threads) == len(calls) == 1
        messages = service.store.conversations.get(cid)['messages']
        assert [message['id'] for message in messages] == [body['client_message_id']]
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_approval_api_retries_after_thread_start_failure_without_losing_confirmation(assistant_api, monkeypatch):
    from backend import dialogue

    service, client, cid = assistant_api
    service.store.session(cid)
    execution = uuid4().hex
    service.store.transition(cid, state='waiting_approval', execution_id=execution)
    request = service.store.request(cid, execution, 'approval', {'tool': 'generate_news',
        'arguments': {'material_id': 'fixture-source', 'column': 'daily_news'},
        'allowed_decisions': ['approve_once', 'deny']}, key='api-launch-failure')
    starts = []

    class FailThenAdmitThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            starts.append(self.kwargs)
            if len(starts) == 1:
                raise RuntimeError('injected approval API launch failure')

    monkeypatch.setattr(dialogue, 'Thread', FailThenAdmitThread)
    route = f"/api/conversations/{cid}/assistant/requests/{request['id']}"
    body = {'decision': 'approve_once', 'version': request['version']}
    first = client.post(route, headers={'X-Workbench': '1'}, json=body)
    assert first.status_code >= 400, first.text
    retried = client.post(route, headers={'X-Workbench': '1'}, json=body)
    assert retried.status_code == 202, retried.text
    assert len(starts) == 2
    assert service.store.session(cid)['input']['request_id'] == request['id']


@pytest.mark.parametrize('suffix,body', [
    ('/messages', {'content': 'fixture', 'client_message_id': 'short'}),
    ('/messages', {'content': 'fixture', 'client_message_id': 'x' * 81}),
    ('/messages', {'content': 'fixture', 'client_message_id': 'x' * 16, 'model': 'unapproved-model'}),
    ('/requests/fixture', {'decision': 'approve_session', 'version': 1, 'max_calls': 21}),
    ('/requests/fixture', {'decision': 'approve_once', 'version': True}),
    ('/requests/fixture', {'decision': 'approve_once', 'version': 1, 'tool': 'save_draft'}),
])
def test_assistant_rejects_invalid_id_or_permission_escalation_fields(assistant_api, suffix, body):
    service, client, cid = assistant_api
    response = client.post(f'/api/conversations/{cid}/assistant{suffix}', headers={'X-Workbench': '1'}, json=body)
    assert response.status_code == 422, response.text
    assert service.processes == {}
