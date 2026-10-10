import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_capability_store import store
from test_capability_context_api import context_client
from test_plan_postgres_concurrency import pair
from src.agent.capabilities.dispatcher import ToolDispatcher
from src.agent.capabilities.registry import builtin_catalog
from src.config import LLMConfig


def managed(store, **metadata):
    return ToolDispatcher(store, store.freeze(uuid4().hex, builtin_catalog(), metadata=metadata))


@pytest.mark.parametrize('entry', ['generate_json', 'generate_draft'])
def test_actual_model_boundary_records_refs_and_reported_tokens_without_prompt_or_key(store, monkeypatch, entry):
    from src.llm import generate
    memory = {'id': 'memory_test', 'revision': 2, 'content': '配图无文字', 'active': True}
    store.put(memory['id'], 'memory', memory, expected_revision=0)
    store.put(memory['id'], 'memory', memory, expected_revision=1)
    current = managed(store, memory=[memory])
    captured = []

    def request(messages):
        captured.append(messages)
        return SimpleNamespace(content='{"title":"事件","body":"报道正文","summary":"结果"}',
                               usage_metadata={'input_tokens': 41, 'output_tokens': 12, 'total_tokens': 53})

    monkeypatch.setattr(generate, 'init_chat_model', lambda *a, **k: SimpleNamespace(invoke=request))
    cfg = LLMConfig(provider='minimax', model='test-model', api_key='private-offline-secret', base_url='http://127.0.0.1')
    prompt = '当前确认只写芯片 <agent_preferences>' + json.dumps([memory], ensure_ascii=False) + '</agent_preferences>'
    with current.bound():
        if entry == 'generate_json':
            generate.generate_json(cfg, system_prompt='JSON', user_prompt=prompt)
        else:
            generate.generate_draft(cfg, title_hint='事件', prompt_hint=prompt, asset_paths=[])
    call = store.calls(run_id=current.snapshot['run_id'])['rows'][0]
    requests = call.get('context_requests') or []
    assert len(requests) == len(captured) == 1
    request = requests[0]
    assert request['status'] == 'response_received'
    assert request['model'] == 'test-model' and request['provider'] == 'minimax'
    assert request['memory_refs'] == [{'id': 'memory_test', 'revision': 2}]
    assert request['actual_tokens'] == {'input_tokens': 41, 'output_tokens': 12, 'total_tokens': 53}
    assert request['input_characters'] > len(prompt) and len(request['input_hash']) == 64
    serialized = json.dumps(call, ensure_ascii=False)
    assert '配图无文字' not in serialized and '当前确认只写芯片' not in serialized
    assert 'private-offline-secret' not in serialized


def test_network_failure_records_attempt_not_success_and_does_not_guess_usage(store, monkeypatch):
    from src.llm import generate
    current = managed(store)
    monkeypatch.setenv('LLM_RATE_LIMIT_MAX_RETRIES', '0')
    monkeypatch.setattr(generate, 'init_chat_model', lambda *a, **k: SimpleNamespace(
        invoke=lambda messages: (_ for _ in ()).throw(RuntimeError('network private-offline-secret'))))
    with current.bound(), pytest.raises(RuntimeError):
        generate.generate_json(LLMConfig(model='test-model', api_key='offline', base_url='http://127.0.0.1'),
                               system_prompt='JSON', user_prompt='材料')
    requests = store.calls(run_id=current.snapshot['run_id'])['rows'][0].get('context_requests') or []
    assert len(requests) == 1
    assert requests[0]['status'] == 'request_failed'
    assert requests[0]['actual_tokens'] is None
    assert 'private-offline-secret' not in json.dumps(requests)


def test_revoked_request_never_acquires_a_sent_context_record(store, monkeypatch):
    from src.llm import generate
    current = ToolDispatcher(store, store.freeze(uuid4().hex, builtin_catalog(), disabled_tools=['builtin:writer.generate']))
    monkeypatch.setattr(generate, 'init_chat_model', lambda *a, **k: pytest.fail('denied request reached transport'))
    with current.bound(), pytest.raises(Exception, match='CAPABILITY_DISABLED'):
        generate.generate_json([], system_prompt='JSON', user_prompt='材料')
    assert not store.calls(run_id=current.snapshot['run_id'])['rows'][0].get('context_requests')


def test_context_api_reads_actual_request_history_not_new_model_defaults(context_client, monkeypatch):
    from backend.capabilities import manager
    from src.llm import generate
    client, workbench, cid = context_client
    service = manager(workbench)
    current = managed(service.store, conversation_id=cid)
    monkeypatch.setattr(generate, 'init_chat_model', lambda *a, **k: SimpleNamespace(
        invoke=lambda messages: SimpleNamespace(content='{"summary":"测试结果"}')))
    cfg = LLMConfig(provider='minimax', model='previous-model', api_key='offline', base_url='http://127.0.0.1')
    with current.bound():
        generate.generate_json(cfg, system_prompt='JSON', user_prompt=json.dumps({'conversation_context': {
            'summary': '旧对话摘要', 'snapshot_version': 3, 'through_seq': 10,
            'recent_messages': [{'seq': 11, 'content': '近期消息'}],
            'skills': [{'id': 'skill_test', 'version_hash': 'a'*64, 'body': '核验来源'}]}}, ensure_ascii=False))
    unrelated = managed(service.store, conversation_id=uuid4().hex)
    with unrelated.bound():
        generate.generate_json(cfg, system_prompt='JSON', user_prompt='其他会话')
    monkeypatch.setattr(generate, 'init_chat_model', lambda *a, **k: pytest.fail('GET started a model'))
    response = client.get('/api/conversations/' + cid + '/context')
    assert response.status_code == 200, response.text
    usage = response.json()['actual_usage']
    assert usage is not None and len(usage['requests']) == 1
    request = usage['requests'][0]
    assert request['model'] == 'previous-model' and request['actual_tokens'] is None
    assert request['snapshot_version'] == 3 and request['summary_through_seq'] == 10
    assert request['retained_sequences'] == [11]
    assert request['skill_refs'] == [{'id': 'skill_test', 'version_hash': 'a'*64}]
    assert '旧对话摘要' not in json.dumps(usage, ensure_ascii=False)


def test_context_api_displays_resolved_compaction_model_without_sending_request(context_client, monkeypatch):
    from src.llm import generate
    client, workbench, cid = context_client
    monkeypatch.setattr(workbench, 'environment', lambda: {
        'AGENT_LLM_PROVIDER': 'minimax', 'MINIMAX_LLM_MODEL': 'next-model',
        'MINIMAX_TOKEN_PLAN_API_KEY': 'offline-test-key'})
    monkeypatch.setattr(generate, 'init_chat_model', lambda *a, **k: pytest.fail('GET started a model'))
    response = client.get('/api/conversations/' + cid + '/context')
    assert response.status_code == 200, response.text
    assert response.json()['model'] == 'next-model'
    assert response.json()['model_selection']['source'] == 'current_binding'
    assert response.json()['actual_usage'] is None
