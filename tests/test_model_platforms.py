import json
import time

import httpx
import pytest

from apps.web_service import Workbench
from backend.task_recognition import resolve_controller, call_model
from src.model_platforms import RuntimeClient, PlatformError
from test_task_calibration import calibration, workbench


def test_calibration_can_use_native_messages_connection(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_TEST_KEY', 'synthetic-canary-agent-key')
    current = Workbench(tmp_path, conversation_store=object())
    store = current.model_platforms()
    conn = store.add_connection({'name': 'Claude test', 'adapter': 'anthropic_messages',
        'base_url': 'http://127.0.0.1:11434/v1', 'network': 'local', 'auth_mode': 'x-api-key', 'credential_env': 'AGENT_TEST_KEY'})
    model = store.add_model({'connection_id': conn['connection_id'], 'upstream_model_id': 'org/claude:preview', 'enabled': True}, store.state()['revision'])
    store.authorize(conn['connection_id'], {'roles': ['agent', 'writer'], 'risk_accepted': True,
        'max_requests': 20, 'expires_at': time.time() + 3600}, store.state()['revision'])
    store.check(model['model_ref'], 'structured', RuntimeClient(store, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        'content': [{'type': 'text', 'text': '{"ok":true}'}], 'stop_reason': 'end_turn'}))))
    store.bind({'agent': model['model_ref']}, store.state()['revision'])
    config = resolve_controller(current)
    assert config.model == 'org/claude:preview'
    assert config.api_key == ''
    captured = []
    from src.model_platforms import integration
    def invoke(config, messages, **kwargs):
        captured.append(config.platform_snapshot)
        from types import SimpleNamespace
        return SimpleNamespace(content='{"schema_version":"task-recognition.v2"}')
    monkeypatch.setattr(integration, 'invoke', invoke)
    assert json.loads(call_model(config, {}))['schema_version'] == 'task-recognition.v2'
    assert captured[0]['adapter'] == 'anthropic_messages'


def test_agent_platform_api_requires_session_and_preserves_secret_projection(tmp_path, monkeypatch):
    from backend import app as module
    from fastapi.testclient import TestClient
    current = Workbench(tmp_path, conversation_store=object())
    monkeypatch.setattr(module.app.state, 'service', current)
    with TestClient(module.app, base_url='http://127.0.0.1:8786') as client:
        route = '/api/model-platforms/connections'
        assert client.get(route).status_code == 403
        client.post('/api/session', headers={'X-Workbench': '1'})
        response = client.post(route, headers={'X-Workbench': '1', 'Idempotency-Key': 'agent-connection'}, json={
            'expected_revision': 0, 'name': 'test', 'adapter': 'openai_chat', 'network': 'local',
            'auth_mode': 'none', 'base_url': 'http://127.0.0.1:11434/v1'})
        assert response.status_code == 200, response.text
        assert client.get(route).json()['revision'] == 1
        response = client.patch(route + '/' + response.json()['connection_id'], headers={'X-Workbench': '1'},
                                json={'expected_revision': 0, 'name': 'changed'})
        assert response.status_code == 409


def test_plan_role_override_is_versioned_and_does_not_change_defaults(calibration):
    current, client, cid, plan, body, calls = calibration
    route = f'/api/conversations/{cid}/plans/{plan["id"]}/models'
    roles = {'agent': 'minimax:MiniMax-M3', 'writer': 'minimax:MiniMax-M3', 'image': ''}
    response = client.put(route, headers={'X-Workbench': '1'}, json={'version': plan['version'], 'model_roles': roles})
    assert response.status_code == 200, response.text
    updated = response.json()['plan']
    assert updated['version'] == plan['version'] + 1
    assert updated['model_roles'] == roles
    assert current.providers()['bindings']['agent'] == ''
    assert calls == []
    assert client.put(route, headers={'X-Workbench': '1'}, json={'version': plan['version'], 'model_roles': roles}).status_code == 409
    current_route = f'/api/conversations/{cid}/plans/{updated["id"]}/models'
    response = client.put(current_route, headers={'X-Workbench': '1'}, json={'version': updated['version'], 'model_roles': {**roles, 'agent': 'm_missing'}})
    assert response.status_code == 200, response.text
    assert response.json()['plan']['executable'] is False
    assert any(row['code'] == 'MODEL_NOT_AVAILABLE' for row in response.json()['plan']['field_errors'])
    displayed = client.get(f'/api/conversations/{cid}').json()['plans'][-1]
    assert displayed['executable'] is False
    assert any(row['code'] == 'MODEL_NOT_AVAILABLE' for row in displayed['field_errors'])


def test_agent_cli_uses_runtime_directory_and_agent_namespace(tmp_path, monkeypatch):
    import os
    from unittest.mock import patch
    from pathlib import Path
    from types import SimpleNamespace
    from typer.testing import CliRunner
    from apps.cli import app
    from src.model_platforms import cli
    monkeypatch.delenv('MODEL_PLATFORMS_DIR', raising=False)
    monkeypatch.delenv('MODEL_PLATFORMS_NAMESPACE', raising=False)
    monkeypatch.setenv('REDBOOK_RUNTIME_ROOT', str(tmp_path / 'runtime'))
    captured = []
    def create(directory, namespace):
        captured.append((Path(directory), namespace))
        return SimpleNamespace(state=lambda: {'roles': {}})
    monkeypatch.setattr(cli, 'PlatformStore', create)
    previous = dict(os.environ)
    with patch.dict(os.environ):
        result = CliRunner().invoke(app, ['model-platforms', 'roles'])
    assert dict(os.environ) == previous
    assert result.exit_code == 0, result.output
    assert captured == [(tmp_path / 'runtime/data/model_platforms', 'agent')]
