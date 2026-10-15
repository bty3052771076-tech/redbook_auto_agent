from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.agent.compaction import compact_conversation
from src.agent.conversation_store import PostgresConversationStore


@pytest.fixture
def conversations():
    store = PostgresConversationStore(namespace='compaction_v2_' + uuid4().hex)
    cid = uuid4().hex
    store.save({'id': cid, 'messages': [], 'plans': [], 'created_at': 1})
    yield store, cid
    with store.knowledge_store.connection() as conn:
        conn.execute('DELETE FROM agent.conversations WHERE account_namespace=%s', (store.namespace,))


def append(store, cid, count=24):
    value = store.get(cid)
    for i in range(count):
        value['messages'].extend([
            {'id': uuid4().hex, 'role': 'user', 'content': 'verified history ' * 80},
            {'id': uuid4().hex, 'role': 'assistant', 'content': 'answer ' * 30},
        ])
    store.save(value)


def compact(store, cid, summarize, **kwargs):
    return compact_conversation(store, cid, summarize=summarize, purpose='controller',
                                model_limits={'context_window': 16000, 'max_output_tokens': 1000},
                                soft_limit_tokens=256, keep_recent_messages=4, **kwargs)


def test_tail_append_does_not_invalidate_prefix_snapshot(conversations):
    store, cid = conversations
    append(store, cid)
    before = store.context_messages(cid)
    def summarize(source):
        value = store.get(cid)
        value['messages'].append({'id': 'tail', 'role': 'user', 'content': 'new unconfirmed requirement'})
        store.save(value)
        return {'summary': 'Historical decisions only', 'evidence_refs': []}
    result = compact(store, cid, summarize)
    assert result['saved'] is True, str(result)
    snapshot = store.active_snapshot(cid)
    assert snapshot['through_seq'] == 44
    assert store.context_messages(cid)[:len(before)] == before
    assert store.context_messages(cid)[-1]['id'] == 'tail'
    assert snapshot['task_state']['schema_version'] == 'conversation-summary.v2'


def test_confirmed_plan_change_rejects_snapshot_candidate(conversations):
    store, cid = conversations
    append(store, cid)
    def summarize(source):
        value = store.get(cid)
        value['plans'] = [{'id': 'new', 'version': 2, 'delivery': 'save_draft'}]
        store.save(value)
        return {'summary': 'obsolete summary'}
    result = compact(store, cid, summarize)
    assert result['status'] == 'conflict' and not result['saved']
    assert store.active_snapshot(cid) is None


def test_summary_failure_keeps_old_snapshot_and_all_messages(conversations):
    store, cid = conversations
    append(store, cid)
    before = store.context_messages(cid)
    calls = []
    def fail(source):
        calls.append(1)
        raise RuntimeError('offline failure')
    result = compact(store, cid, fail)
    assert result['attempts'] == len(calls) == 2
    assert result['saved'] is False
    assert result['can_continue'] is True
    assert store.active_snapshot(cid) is None and store.context_messages(cid) == before


def test_cancelled_requirement_stays_cancelled_after_two_compactions(conversations):
    store, cid = conversations
    append(store, cid)
    state = {'superseded_requirements': [{'text': 'old image heading', 'message_id': 'old'}],
             'confirmed_requirements': ['save drafts only']}
    sources = []
    def summarize(source):
        sources.append(source)
        return {'summary': 'Keep draft delivery', 'decisions': [], 'evidence_refs': [],
                'superseded_requirements': []}
    first = compact(store, cid, summarize, task_state=state)
    assert first['saved'], str(first)
    append(store, cid, 6)
    assert compact(store, cid, summarize, task_state=state)['saved']
    snapshot = store.active_snapshot(cid)
    assert snapshot['task_state']['superseded_requirements'] == state['superseded_requirements']
    assert snapshot['constraints'] == []
    assert sources[-1]['superseded_requirements'] == state['superseded_requirements']


def test_invalid_summary_references_are_rejected_not_silently_accepted(conversations):
    store, cid = conversations
    append(store, cid)
    result = compact(store, cid, lambda source: {'summary': 'ok', 'decisions': [
        {'text': 'invented instruction', 'message_id': 'not-in-prefix'}]})
    assert result['status'] == 'blocked' and result['attempts'] == 2
    assert store.active_snapshot(cid) is None


def test_large_prefix_is_chunked_at_exchange_boundaries(conversations):
    from src.agent.compaction import estimate_tokens
    store, cid = conversations
    append(store, cid, 80)
    sizes = []
    def summarize(source):
        sizes.append(estimate_tokens(source))
        assert source['new_messages'][0]['role'] == 'user'
        assert source['new_messages'][-1]['role'] == 'assistant'
        return {'summary': 'bounded historical summary'}
    result = compact(store, cid, summarize)
    assert result['saved'] is True and len(sizes) > 1, str(result)
    assert max(sizes) <= 12952


def test_new_conversations_auto_and_legacy_manual_preserved(conversations):
    from src.agent.capabilities.context_service import ContextService
    store, cid = conversations
    resources = {}
    service = ContextService(SimpleNamespace(
        workbench=SimpleNamespace(_read_agent_conversation=store.get),
        store=SimpleNamespace(get=resources.get)))
    assert service.policy(cid)[0]['mode'] == 'auto'
    resources['policy:context:' + cid] = {'mode': 'manual', 'revision': 2}
    assert service.policy(cid)[0]['mode'] == 'manual'
    legacy = uuid4().hex
    store.import_legacy({'id': legacy, 'messages': []})
    assert service.policy(legacy)[0]['mode'] == 'manual'


def test_auto_prepare_returns_operation_without_waiting_for_summary(conversations, tmp_path, monkeypatch):
    from src.agent.capabilities.context_service import ContextService
    from src.agent.capabilities.operations import OperationManager
    from src.agent.capabilities.store import CapabilityStore
    store, cid = conversations
    caps = CapabilityStore(store.knowledge_store, namespace=store.namespace)
    caps.ensure_schema()
    operations = OperationManager(caps, root=tmp_path)
    entered, release = Event(), Event()
    def summarize(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'status': 'compacted', 'saved': True}
    service = ContextService(SimpleNamespace(store=caps, operations=operations,
        workbench=SimpleNamespace(_read_agent_conversation=store.get, compact_agent_conversation=summarize)))
    monkeypatch.setattr(service, 'overview', lambda cid: {
        'policy': {'mode': 'auto', 'soft_threshold': 256, 'keep_recent': 4},
        'policy_revision': 0, 'threshold_exceeded': True,
        'coverage': {'summary_through_seq': 0, 'retained_sequences': [1, 2]}})
    try:
        result = service.prepare(cid)
        assert result['operation_id']
        assert result['status'] in {'queued', 'running'}
        assert entered.wait(2)
    finally:
        release.set()
        operations.close()


def test_long_tool_result_is_persisted_as_readable_artifact_before_summary(conversations):
    store, cid = conversations
    value = store.get(cid)
    huge = 'external source material ' * 20000
    value['messages'] = [
        {'id': 'u', 'role': 'user', 'content': 'read source'},
        {'id': 'a', 'role': 'assistant', 'tool_calls': [{'id': 't', 'name': 'read', 'args': {}}]},
        {'id': 't', 'role': 'tool', 'tool_call_id': 't', 'content': huge, 'error_code': 'SOURCE_PARTIAL'},
    ]
    store.save(value)
    append(store, cid, 4)
    captured = []
    def summarize(source):
        captured.append(source)
        return {'summary': 'Source reference retained', 'artifact_refs': source['allowed_artifact_refs']}
    result = compact(store, cid, summarize)
    assert result['saved'], str(result)
    tool = next(m for source in captured for m in source['new_messages'] if m.get('tool_call_id') == 't')
    assert tool['artifact_ref'] and tool['error_code'] == 'SOURCE_PARTIAL'
    assert len(tool['content']) < 1000
    assert store.read_context_artifact(tool['artifact_ref']) == huge
    assert store.context_messages(cid)[2]['content'] == huge


def test_summary_failure_over_window_blocks_only_model_input(conversations):
    store, cid = conversations
    append(store, cid, 80)
    result = compact(store, cid, lambda source: (_ for _ in ()).throw(ValueError('summary failure')))
    assert result['status'] == 'blocked' and result['attempts'] == 2
    assert result['can_continue'] is False
    assert len(store.context_messages(cid)) == 160


def test_provider_context_length_error_has_one_emergency_compaction_retry():
    import src.agent.compaction as module
    assert hasattr(module, 'invoke_with_compaction'), 'bounded emergency retry is missing'
    calls, compacted = [], []
    def invoke():
        calls.append(1)
        raise ValueError('context_length_exceeded')
    with pytest.raises(ValueError, match='context_length_exceeded'):
        module.invoke_with_compaction(invoke, lambda: compacted.append(1) or {'saved': True, 'can_continue': True})
    assert len(calls) == 2 and len(compacted) == 1


def test_emergency_compaction_does_not_retry_without_a_committed_improvement():
    from src.agent.compaction import invoke_with_compaction
    calls = []

    def invoke():
        calls.append(1)
        raise ValueError('context_length_exceeded')

    with pytest.raises(ValueError, match='context_length_exceeded'):
        invoke_with_compaction(invoke, lambda: {'saved': False, 'can_continue': True, 'status': 'in_progress'})
    assert len(calls) == 1


def test_dispatcher_external_summary_is_audited_without_prompt_or_secret(conversations, monkeypatch):
    from src.agent.capabilities.context_usage import observe_context_requests
    from src.agent.capabilities.store import CapabilityStore
    from src.config import LLMConfig
    from src.llm import generate
    store, cid = conversations
    caps = CapabilityStore(store.knowledge_store, namespace=store.namespace)
    caps.ensure_schema()
    monkeypatch.setattr(generate, 'init_chat_model', lambda *a, **k: SimpleNamespace(invoke=lambda messages:
        SimpleNamespace(content='{"summary":"ok"}', usage_metadata={'input_tokens': 10, 'output_tokens': 2})))
    with observe_context_requests(caps, cid, 'compaction'):
        generate.generate_json(LLMConfig(provider='minimax', model='offline', api_key='private-test-key',
                                        base_url='http://127.0.0.1'), system_prompt='JSON', user_prompt='private-prompt')
    request = caps.calls()['rows'][0]['context_requests'][0]
    assert request['actual_tokens']['input_tokens'] == 10
    assert request['usage_kind'] == 'actual'
    assert 'private-prompt' not in str(request) and 'private-test-key' not in str(request)


def test_forgotten_preference_is_filtered_by_context_builder(conversations):
    from src.agent.capabilities.store import CapabilityStore
    from src.agent.memory_service import MemoryService
    from src.agent.context_builder import ContextBuilder
    store, cid = conversations
    caps = CapabilityStore(store.knowledge_store, namespace=store.namespace)
    caps.ensure_schema()
    memory = MemoryService(caps)
    row = memory.save({'content': 'withdrawn-preference', 'scope': 'conversation', 'scope_id': cid})
    value = store.get(cid)
    value['messages'] = [{'id': 'u', 'role': 'user', 'content': 'withdrawn-preference'}]
    store.save(value)
    memory.forget(row['id'], expected_revision=row['revision'])
    result = ContextBuilder(store).build(cid, None, 'controller', {})
    assert 'withdrawn-preference' not in str(result['envelope'])
    assert store.context_messages(cid)[0]['content'] == 'withdrawn-preference'


def test_explicit_empty_tombstones_do_not_resurrect_previous_cancelled_requirement(conversations):
    store, cid = conversations
    append(store, cid)
    tombstone = {'text': 'cancelled preference', 'message_id': 'old'}
    assert compact(store, cid, lambda source: {'summary': 'history'},
                   task_state={'superseded_requirements': [tombstone]})['saved']
    append(store, cid, 4)
    assert compact(store, cid, lambda source: {'summary': 'new history'},
                   task_state={'superseded_requirements': []})['saved']
    assert store.active_snapshot(cid)['task_state']['superseded_requirements'] == [tombstone]


def test_context_service_respects_model_window_without_calling_model_on_read(conversations):
    from src.agent.capabilities.context_service import ContextService
    store, cid = conversations
    resources = {}
    service = ContextService(SimpleNamespace(
        workbench=SimpleNamespace(_read_agent_conversation=store.get,
            models=lambda: {'rows': [{'id': 'm_small', 'model': 'small', 'provider': 'offline',
                                     'available': True, 'context_window': 4096}]}),
        store=SimpleNamespace(get=resources.get)))
    assert service.model_selection('m_small')['context_window'] == 4096


def test_recompaction_after_memory_invalidation_uses_raw_filtered_prefix(conversations):
    from src.agent.capabilities.store import CapabilityStore
    from src.agent.memory_service import MemoryService
    store, cid = conversations
    caps = CapabilityStore(store.knowledge_store, namespace=store.namespace)
    caps.ensure_schema()
    memory = MemoryService(caps)
    row = memory.save({'content': 'cancelled instruction', 'scope': 'conversation', 'scope_id': cid})
    append(store, cid)
    state = {'memory_refs': [{'id': row['id'], 'revision': row['revision']}]}
    assert compact(store, cid, lambda source: {'summary': 'cancelled instruction'}, task_state=state)['saved']
    memory.forget(row['id'], expected_revision=row['revision'])
    assert store.active_snapshot(cid) is None
    result = compact(store, cid, lambda source: {'summary': 'clean history'})
    assert result['saved'], str(result)
    assert store.active_snapshot(cid)['version'] == 2


def test_revoked_preference_cannot_leak_through_structured_decision_text(conversations):
    from src.agent.capabilities.store import CapabilityStore
    from src.agent.memory_service import MemoryService
    from src.agent.context_builder import ContextBuilder
    store, cid = conversations
    caps = CapabilityStore(store.knowledge_store, namespace=store.namespace)
    caps.ensure_schema()
    memory = MemoryService(caps)
    row = memory.save({'content': 'secret-old-preference', 'scope': 'conversation', 'scope_id': cid})
    memory.forget(row['id'], expected_revision=row['revision'])
    append(store, cid)
    result = compact(store, cid, lambda source: {'summary': 'history', 'decisions': [
        {'text': 'secret-old-preference', 'message_id': source['new_messages'][0]['id']}]})
    assert result['saved'], str(result)
    assert 'secret-old-preference' not in str(ContextBuilder(store).build(cid, None, 'controller', {})['envelope'])
