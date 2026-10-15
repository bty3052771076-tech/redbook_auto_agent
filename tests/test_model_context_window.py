import json
import time

import httpx
import pytest

from src.model_platforms import PlatformError, PlatformStore, RuntimeClient
from src.model_platforms.service import platform_request


@pytest.fixture
def platform(tmp_path, request):
    store = PlatformStore(tmp_path, namespace='agent', env={'CONTEXT_TEST_KEY': 'synthetic-context-key'})
    connection = store.add_connection({
        'name': 'Context test', 'adapter': getattr(request, 'param', 'openai_chat'), 'network': 'local',
        'base_url': 'http://127.0.0.1:11434/v1', 'auth_mode': 'bearer',
        'credential_env': 'CONTEXT_TEST_KEY', 'billing': 'unknown',
    })
    store.authorize(connection['connection_id'], {
        'roles': ['agent', 'writer'], 'risk_accepted': True,
        'max_requests': 20, 'expires_at': time.time() + 3600,
    }, store.state()['revision'])
    return store, connection


def add_model(platform, **fields):
    store, connection = platform
    return store.add_model({
        'connection_id': connection['connection_id'], 'upstream_model_id': 'org/model:preview',
        'enabled': True, **fields,
    }, store.state()['revision'])


@pytest.mark.parametrize('window', [None, 4096, 131072, 4000000])
def test_context_window_round_trips_as_top_level_metadata(platform, window):
    store, _ = platform
    model = add_model(platform, context_window=window, parameters={'max_output_tokens': 512})
    assert model['context_window'] == window
    reopened = PlatformStore(store.directory, namespace='agent', env={'CONTEXT_TEST_KEY': 'synthetic-context-key'})
    assert reopened.state()['schema_version'] == 2
    assert reopened.state()['models'][0]['context_window'] == window
    assert reopened.catalog_rows()['models'][0]['context_window'] == window
    snapshot = reopened.resolve('agent', model['model_ref'], require_verified=False)
    assert snapshot['context_window'] == window
    assert snapshot['parameters'] == {'max_output_tokens': 512}


@pytest.mark.parametrize('operation', ['add', 'edit'])
@pytest.mark.parametrize('invalid', [True, False, '8192', '', 4096.0, 4095, 4000001, 0, -1, [], {}])
def test_context_window_rejects_invalid_values_without_saving(platform, operation, invalid):
    store, _ = platform
    model = add_model(platform)
    before = (store.directory / 'registry.json').read_bytes()
    with pytest.raises(PlatformError, match='INVALID_CONTEXT_WINDOW'):
        if operation == 'add':
            add_model(platform, upstream_model_id='another/model', context_window=invalid)
        else:
            store.edit_model(model['model_ref'], {'context_window': invalid}, store.state()['revision'])
    assert (store.directory / 'registry.json').read_bytes() == before


@pytest.mark.parametrize('window', [None, 4096, 4000000])
def test_context_window_edits_preserve_bindings_credentials_and_authorization(platform, window):
    store, connection = platform
    model = add_model(platform, context_window=65536, parameters={'max_output_tokens': 512})
    client = RuntimeClient(store, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}],
    })))
    store.check(model['model_ref'], 'structured', client)
    store.bind({'agent': model['model_ref'], 'writer': model['model_ref']}, store.state()['revision'])
    before = store.state()
    frozen = store.resolve('agent')
    updated = store.edit_model(model['model_ref'], {'context_window': window}, before['revision'])
    assert updated['context_window'] == window
    assert updated['parameters'] == {'max_output_tokens': 512}
    assert updated['capabilities'] == before['models'][0]['capabilities']
    after = store.state()
    assert after['connections'] == before['connections']
    assert after['namespaces'] == before['namespaces']
    assert after['revision'] == before['revision'] + 1
    assert updated['revision'] == before['models'][0]['revision'] + 1
    assert store.resolve('agent')['context_window'] == window
    assert frozen['context_window'] == 65536
    assert store.secrets.read(connection['credential_ref']) == 'synthetic-context-key'
    store.edit_model(model['model_ref'], {'favorite': True}, after['revision'])
    assert store.resolve('agent')['context_window'] == window


@pytest.mark.parametrize('metadata, expected', [({}, None), ({'context_window': None}, None), ({'context_window': 8192}, 8192)])
def test_old_and_new_registries_expose_unknown_without_guessing(platform, metadata, expected):
    store, _ = platform
    model = add_model(platform, upstream_model_id='gpt-4o-128k')
    path = store.directory / 'registry.json'
    registry = json.loads(path.read_text(encoding='utf-8'))
    registry['models'][0].pop('context_window', None)
    registry['models'][0].update(metadata)
    path.write_text(json.dumps(registry), encoding='utf-8')
    before = path.read_bytes()
    assert store.catalog_rows()['models'][0]['context_window'] == expected
    assert store.resolve('writer', model['model_ref'], require_verified=False)['context_window'] == expected
    assert path.read_bytes() == before


def test_context_window_remains_unknown_for_discovered_models(platform):
    store, connection = platform
    model = add_model(platform, context_window=65536)
    client = RuntimeClient(store, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        'data': [{'id': 'org/model:preview'}, {'id': 'another/model-128k'}],
    })))
    store.discover(connection['connection_id'], client)
    rows = {row['upstream_model_id']: row for row in store.catalog_rows()['models']}
    assert rows['org/model:preview']['context_window'] == 65536
    assert rows['another/model-128k']['context_window'] is None
    assert rows['org/model:preview']['model_ref'] == model['model_ref']


@pytest.mark.parametrize('platform', ['openai_chat', 'openai_responses', 'anthropic_messages'], indirect=True)
def test_context_window_never_enters_upstream_request(platform):
    store, _ = platform
    model = add_model(platform, context_window=131072, parameters={'max_output_tokens': 512})
    snapshot = store.resolve('writer', model['model_ref'], require_verified=False)
    adapter = snapshot['adapter']
    response = {
        'openai_chat': {'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}]},
        'openai_responses': {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'OK'}]}]},
        'anthropic_messages': {'content': [{'type': 'text', 'text': 'OK'}], 'stop_reason': 'end_turn'},
    }[adapter]
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=response)

    client = RuntimeClient(store, transport=httpx.MockTransport(respond))
    assert client.call(snapshot, [{'role': 'user', 'content': 'hello'}]).text == 'OK'
    assert snapshot['context_window'] == 131072
    assert 'context_window' not in requests[0].content.decode()
    assert json.loads(requests[0].content)['model'] == 'org/model:preview'
    with pytest.raises(PlatformError, match='UNSUPPORTED_PARAMETER'):
        store.edit_model(model['model_ref'], {'parameters': {'context_window': 131072}}, store.state()['revision'])


def test_context_window_does_not_bypass_verification_or_billing(platform):
    store, connection = platform
    model = add_model(platform, context_window=131072)
    with pytest.raises(PlatformError, match='CAPABILITY_UNVERIFIED'):
        store.resolve('agent', model['model_ref'])
    store.credential(connection['connection_id'], {'credential_env': 'CONTEXT_TEST_KEY'}, store.state()['revision'])
    with pytest.raises(PlatformError, match='BILLING_NOT_AUTHORIZED'):
        store.resolve('agent', model['model_ref'], require_verified=False)


def test_model_platform_api_accepts_and_returns_top_level_context_window(platform):
    from types import SimpleNamespace

    store, connection = platform
    service = SimpleNamespace(model_platforms=lambda: store)
    model = platform_request(service, 'POST', '/api/model-platforms/models', {
        'expected_revision': store.state()['revision'], 'connection_id': connection['connection_id'],
        'upstream_model_id': 'api/model', 'enabled': True, 'context_window': 131072,
    })
    result = platform_request(service, 'GET', '/api/model-platforms/models')
    assert result['models'][0]['context_window'] == 131072
    updated = platform_request(service, 'PATCH', '/api/model-platforms/models/' + model['model_ref'], {
        'expected_revision': result['revision'], 'context_window': None,
    })
    assert updated['context_window'] is None


def test_model_window_survives_checkpoint_and_resume_without_upstream_parameter(platform):
    from src.model_platforms.integration import checkpoint_models, resume_model_environment

    store, _ = platform
    model = add_model(platform, context_window=131072)
    snapshot = store.resolve('writer', model['model_ref'], require_verified=False)
    runtime = checkpoint_models({}, saved={'snapshots': {'writer': snapshot}})
    assert runtime['snapshots']['writer']['context_window'] == 131072
    restored = resume_model_environment({'model_runtime': runtime}, {})
    assert json.loads(restored['RUN_MODEL_SNAPSHOTS'])['writer']['context_window'] == 131072
    assert 'context_window' not in runtime['snapshots']['writer']['parameters']
