"""New model adapter over real framework and RuntimeClient, mocked HTTP only."""

import json
import time

import httpx
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
import pytest

from backend.dialogue_model import RuntimeChatModel
from src.model_platforms import PlatformStore, RuntimeClient, PlatformError
from test_conversational_agent_cancellation import no_external_work
from test_conversational_agent_framework import pg_agent_checkpointer


def configured_model(tmp_path, adapter, responder):
    store = PlatformStore(tmp_path / 'model-platforms', namespace='agent', env={})
    connection = store.add_connection({'name': 'Offline fixture', 'adapter': adapter,
        'base_url': 'http://127.0.0.1:11434/v1', 'network': 'local', 'auth_mode': 'none'})
    registered = store.add_model({'connection_id': connection['connection_id'],
        'upstream_model_id': 'fixture-controller', 'enabled': True}, store.state()['revision'])
    store.authorize(connection['connection_id'], {'roles': ['agent'], 'risk_accepted': True,
        'max_requests': 10, 'expires_at': time.time() + 60}, store.state()['revision'])
    snapshot = store.resolve('agent', registered['model_ref'], require_verified=False)
    model = RuntimeChatModel()
    model._client = RuntimeClient(store, transport=httpx.MockTransport(responder))
    model._snapshot = snapshot
    return model, store


@pytest.mark.parametrize('adapter', ['openai_chat', 'openai_responses', 'anthropic_messages'])
def test_registered_adapter_preserves_tool_call_and_result_in_real_create_agent(tmp_path, pg_agent_checkpointer, adapter):
    from langchain.agents import create_agent

    requests, executed = [], []

    @tool
    def inspect_fixture() -> str:
        """Read an offline fixture."""
        executed.append('inspected')
        return 'verified-fixture-result'

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        first = len(requests) == 1
        assert len(requests) <= 2, 'Adapter retried or framework failed to consume a tool result'
        if adapter == 'openai_chat':
            message = {'content': '', 'tool_calls': [{'id': 'fixture-call', 'type': 'function',
                'function': {'name': 'inspect_fixture', 'arguments': '{}'}}]} if first else {'content': 'Fixture inspected.'}
            payload = {'choices': [{'message': message, 'finish_reason': 'tool_calls' if first else 'stop'}]}
        elif adapter == 'openai_responses':
            output = [{'type': 'function_call', 'call_id': 'fixture-call', 'name': 'inspect_fixture', 'arguments': '{}'}] if first else [
                {'type': 'message', 'content': [{'type': 'output_text', 'text': 'Fixture inspected.'}]}]
            payload = {'status': 'completed', 'output': output}
        else:
            content = [{'type': 'tool_use', 'id': 'fixture-call', 'name': 'inspect_fixture', 'input': {}}] if first else [
                {'type': 'text', 'text': 'Fixture inspected.'}]
            payload = {'content': content, 'stop_reason': 'tool_use' if first else 'end_turn'}
        return httpx.Response(200, json=payload)

    model, store = configured_model(tmp_path, adapter, respond)
    before_roles = store.state()['roles']
    events = []
    model._emit = events.append
    _, open_saver = pg_agent_checkpointer
    with open_saver() as (saver, _):
        agent = create_agent(model=model, tools=[inspect_fixture], checkpointer=saver)
        result = agent.invoke({'messages': [HumanMessage(content='Inspect the fixture.')]},
                              {'configurable': {'thread_id': 'adapter-fixture'}})
    assert result['messages'][-1].content == 'Fixture inspected.'
    assert executed == ['inspected'] and len(requests) == 2
    if adapter == 'openai_chat':
        assert next(row for row in requests[1]['messages'] if row['role'] == 'tool') == {
            'role': 'tool', 'tool_call_id': 'fixture-call', 'content': 'verified-fixture-result'}
    elif adapter == 'openai_responses':
        assert {'type': 'function_call_output', 'call_id': 'fixture-call', 'output': 'verified-fixture-result'} in requests[1]['input']
    else:
        assert any({'type': 'tool_result', 'tool_use_id': 'fixture-call', 'content': 'verified-fixture-result'} in row['content']
                   for row in requests[1]['messages'] if row['role'] == 'user')
    assert [event['status'] for event in events] == ['started', 'completed', 'started', 'completed']
    assert store.state()['roles'] == before_roles


@pytest.mark.parametrize('status,code', [(429, 'RATE_LIMITED'), (403, 'CREDENTIAL_REJECTED'), (503, 'UPSTREAM_HTTP_ERROR')])
def test_adapter_does_not_retry_or_switch_model_after_upstream_error(tmp_path, status, code):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={'error': 'offline injected failure'})

    model, store = configured_model(tmp_path, 'openai_chat', respond)
    snapshot = dict(model._snapshot)
    with pytest.raises(PlatformError) as error:
        model.invoke([HumanMessage(content='Fixture only.')])
    assert error.value.code == code
    assert len(calls) == 1
    assert model._snapshot == snapshot
    assert json.loads(calls[0].content)['model'] == 'fixture-controller'
    assert store.state()['roles'] == {}


def test_adapter_dump_and_identity_do_not_expose_runtime_or_secrets(tmp_path):
    model, _ = configured_model(tmp_path, 'openai_chat', lambda request: pytest.fail('No model call was requested'))
    model._snapshot['credential_canary'] = 'private-fixture-secret'
    visible = model.model_dump_json() + json.dumps(model._identifying_params)
    assert 'private-fixture-secret' not in visible
    assert '_client' not in visible and '_snapshot' not in visible
    assert model._identifying_params['role'] == 'agent'
