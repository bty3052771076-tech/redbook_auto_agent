from __future__ import annotations

from uuid import uuid4

import pytest

from backend.settings import configure_runtime

configure_runtime()


@pytest.fixture
def store():
    from src.agent.capabilities.store import CapabilityStore
    from src.knowledge.store import KnowledgeStore
    current = CapabilityStore(KnowledgeStore.from_env(), namespace='test_capabilities_' + uuid4().hex)
    current.ensure_schema()
    return current


def test_resource_revision_is_atomic_and_stale_save_is_rejected(store):
    from src.agent.capabilities.models import CapabilityError
    resource = store.put('builtin:test', 'tool', {'enabled': True}, expected_revision=0)
    assert resource['revision'] == 1
    updated = store.put('builtin:test', 'tool', {'enabled': False}, expected_revision=1)
    with pytest.raises(CapabilityError) as caught:
        store.put('builtin:test', 'tool', {'enabled': True}, expected_revision=1)
    assert caught.value.status == 409
    assert store.get('builtin:test')['enabled'] is False
    assert store.version('builtin:test', 1)['enabled'] is True
    assert updated['revision'] == 2
    assert len(store.changes()['rows']) == 2


def test_disabled_tool_is_denied_without_running_callback(store):
    from src.agent.capabilities.dispatcher import ToolDispatcher
    from src.agent.capabilities.models import CapabilityError
    from src.agent.capabilities.registry import builtin_catalog
    catalog = builtin_catalog()
    store.put('builtin:news.generate', 'tool', {'enabled': False}, expected_revision=0)
    snapshot = store.freeze('run_' + uuid4().hex, catalog)
    dispatcher = ToolDispatcher(store, snapshot)
    invoked = []
    with pytest.raises(CapabilityError, match='CAPABILITY_DISABLED'):
        dispatcher.call('builtin:news.generate', lambda: invoked.append(True), stage='generate')
    assert invoked == []
    assert store.calls(run_id=snapshot['run_id'])['rows'][0]['status'] == 'denied'


def test_frozen_task_uses_old_version_until_explicit_revocation(store):
    from src.agent.capabilities.dispatcher import ToolDispatcher
    from src.agent.capabilities.models import CapabilityError
    from src.agent.capabilities.registry import builtin_catalog
    store.put('builtin:news.generate', 'tool', {'enabled': True}, expected_revision=0)
    snapshot = store.freeze('run_' + uuid4().hex, builtin_catalog())
    store.put('builtin:news.generate', 'tool', {'enabled': False}, expected_revision=1)
    dispatcher = ToolDispatcher(store, snapshot)
    assert dispatcher.call('builtin:news.generate', lambda: 'retained', stage='generate') == 'retained'
    store.revoke('builtin:news.generate', expected_revision=2, reason='test stop')
    with pytest.raises(CapabilityError, match='CAPABILITY_REVOKED'):
        dispatcher.call('builtin:news.generate', lambda: 'unexpected', stage='generate')
    assert store.version('builtin:news.generate', 1)['enabled'] is True


def test_call_records_intent_before_effect_and_preserves_error(store):
    from src.agent.capabilities.dispatcher import ToolDispatcher
    from src.agent.capabilities.registry import builtin_catalog
    snapshot = store.freeze('run_' + uuid4().hex, builtin_catalog())
    dispatcher = ToolDispatcher(store, snapshot)
    def effect():
        row = store.calls(run_id=snapshot['run_id'])['rows'][0]
        assert row['status'] == 'running'
        raise RuntimeError('XHS_WRITE_UNCERTAIN')
    with pytest.raises(RuntimeError):
        dispatcher.call('builtin:xhs.drafts.save_batch', effect, stage='upload')
    row = store.calls(run_id=snapshot['run_id'])['rows'][0]
    assert row['status'] == 'uncertain'
    assert row['wall_ms'] >= 0


def test_resource_configuration_and_calls_survive_new_store_instance(store):
    from src.agent.capabilities.store import CapabilityStore
    store.put('builtin:test', 'tool', {'enabled': False}, expected_revision=0)
    fresh = CapabilityStore(store.knowledge_store, namespace=store.namespace)
    assert fresh.get('builtin:test')['enabled'] is False


def test_runtime_paths_use_application_interpreter_not_runtime_venv():
    import sys
    from pathlib import Path
    from src.agent.capabilities.runtime_paths import RuntimePaths
    runtime = configure_runtime()
    paths = RuntimePaths.resolve(runtime)
    assert paths.python_executable == Path(sys.executable).resolve()
    assert paths.application_root == Path(__file__).resolve().parents[1]
    assert paths.tool_package_root == paths.application_root / 'tools/redbook_tools'
    assert paths.runtime_root == runtime


def test_plan_disables_cannot_be_overwritten_by_global_enabled_policy(store):
    from src.agent.capabilities.registry import builtin_catalog
    store.put('builtin:news.generate', 'tool', {'enabled': True}, expected_revision=0)
    frozen = store.freeze('run_' + uuid4().hex, builtin_catalog(), disabled_tools=['builtin:news.generate'])
    assert frozen['tools']['builtin:news.generate']['enabled'] is False


def test_conversation_claim_and_capability_snapshot_roll_back_together(store):
    from src.agent.conversation_store import PostgresConversationStore
    from src.agent.capabilities.registry import builtin_catalog
    conversations = PostgresConversationStore(store.knowledge_store, namespace=store.namespace)
    cid, run_id = uuid4().hex, uuid4().hex
    saved = conversations.save({'id': cid, 'title': '原子冻结测试', 'messages': [], 'plans': [], 'runs': []})
    try:
        with pytest.raises(RuntimeError, match='fault-injected'):
            with store.knowledge_store.connection() as conn, conn.transaction():
                snapshot = store.freeze(run_id, builtin_catalog(), connection=conn)
                saved['claimed_run'] = run_id
                updated = conversations.save(saved, connection=conn)
                assert updated['claimed_run'] == run_id
                assert updated['_revision'] == 2
                assert snapshot['run_id'] == run_id
                raise RuntimeError('fault-injected')
        assert store.snapshot(run_id) is None
        assert 'claimed_run' not in conversations.get(cid)
        assert conversations.get(cid)['_revision'] == 1
    finally:
        with store.knowledge_store.connection() as conn:
            conn.execute('DELETE FROM agent.conversations WHERE conversation_id=%s AND account_namespace=%s', (cid, store.namespace))


def test_active_references_require_live_run_lease_and_preserve_history(store):
    from src.agent.artifact_store import AgentArtifactStore
    from src.agent.capabilities.registry import builtin_catalog
    identity='builtin:news.generate'
    run_id=uuid4().hex
    store.put(identity,'tool',{'enabled':True},expected_revision=0)
    snapshot=store.freeze(run_id,builtin_catalog())
    assert store.active_references(identity)==[]
    with AgentArtifactStore(store.knowledge_store).lease(run_id):
        call=store.start_call({'run_id':run_id,'resource_id':identity,'status':'running'})
        current=store.active_references(identity)
        assert len(current)==1 and current[0]['run_id']==run_id
        assert current[0]['version']==1 and current[0]['in_flight_calls']==1
        store.finish_call(call,'succeeded',{})
        assert store.active_references(identity)[0]['in_flight_calls']==0
    assert store.active_references(identity)==[]
    assert store.snapshot(run_id)==snapshot


def test_invalid_legacy_run_identity_cannot_break_active_reference_directory(store):
    from src.agent.capabilities.registry import builtin_catalog
    store.freeze('旧任务',builtin_catalog())
    assert store.active_references('builtin:news.generate')==[]
    assert store.active_references()==[]
