from __future__ import annotations

import time
from uuid import uuid4

import pytest

from backend.settings import configure_runtime
from test_plan_postgres_concurrency import pair

configure_runtime()


@pytest.fixture
def memory():
    from src.agent.capabilities.store import CapabilityStore
    from src.agent.memory_service import MemoryService
    store=CapabilityStore(namespace='test_memory_'+uuid4().hex)
    store.ensure_schema()
    return MemoryService(store)


def test_preference_scope_and_manual_confirmation_control_selection(memory):
    global_row=memory.save({'content':'新闻图片不含文字','key':'image_text','scope':'workspace','origin':'manual'})
    column_row=memory.save({'content':'AI卡片可含文字','key':'image_text','scope':'column','scope_id':'daily_ai_digest','origin':'manual'})
    inferred=memory.save({'content':'每天生成100条','scope':'workspace','origin':'inferred'})
    assert inferred['active'] is False
    chosen=memory.select(column='daily_ai_digest')
    assert chosen[0]['id']==column_row['id']
    assert global_row['id'] in chosen[0]['overridden_ids']
    overridden = chosen[0]['overridden_preferences'][0]
    assert overridden['content'] == global_row['content']
    assert overridden['source_ref'] == global_row['source_ref']
    assert overridden['revision'] == global_row['revision']
    assert overridden['reason'] == '更具体的作用范围优先'
    assert memory.select(column='daily_news')[0]['id']==global_row['id']


def test_forget_invalidates_derived_context_and_suppresses_reinjection(memory):
    row=memory.save({'content':'配图需要写大标题','key':'image_text','scope':'workspace'})
    result=memory.forget(row['id'],expected_revision=row['revision'])
    assert result['status']=='forgotten'
    assert memory.select()==[]
    filtered=memory.filter_forgotten('历史偏好：配图需要写大标题；保留新闻原文')
    assert '配图需要写大标题' not in filtered
    assert '保留新闻原文' in filtered


def test_forget_filter_only_applies_in_the_matching_scope(memory):
    row = memory.save({'content': '为新闻配图写大标题', 'scope': 'conversation', 'scope_id': 'target'})
    memory.forget(row['id'], expected_revision=row['revision'])
    assert memory.filter_forgotten('为新闻配图写大标题') == '为新闻配图写大标题'
    assert memory.filter_forgotten('为新闻配图写大标题', conversation='other') == '为新闻配图写大标题'
    assert '为新闻配图写大标题' not in memory.filter_forgotten('为新闻配图写大标题', conversation='target')


def test_forgotten_preference_is_not_reinjected_from_raw_history_or_recompaction(pair, monkeypatch):
    from backend.capabilities import manager
    from types import SimpleNamespace
    currents, cid, _ = pair
    current = currents[0]
    memory = manager(current).memory
    content = '新闻图片上必须写超大标题'
    row = memory.save({'content': content, 'scope': 'conversation', 'scope_id': cid})
    saved = current._read_agent_conversation(cid)
    saved['messages'].extend({'id': uuid4().hex, 'role': 'user', 'content': content + '；芯片新闻的背景材料' * 100}
                            for _ in range(4))
    current.conversation_store.save(saved)
    raw = current.conversation_store.context_messages(cid)
    current.conversation_store.save_snapshot(cid, expected_revision=current._read_agent_conversation(cid)['_revision'],
        snapshot={'through_seq': raw[-2]['seq'], 'summary': '历史偏好：' + content, 'constraints': [content],
                  'task_state': {'memory_refs': [{'id': row['id'], 'revision': row['revision']}]}, 'input_tokens': 3000, 'output_tokens': 50})
    memory.forget(row['id'], expected_revision=row['revision'])
    context = current._agent_memory_for_execution(cid)
    assert content not in str(context)
    assert content in str(current.conversation_store.context_messages(cid))
    sent = []
    monkeypatch.setattr(current, 'freeze_model_roles', lambda env, request: {})
    monkeypatch.setattr('src.model_platforms.integration.legacy_controller', lambda env: SimpleNamespace(provider='minimax', model='test-controller'))
    def summarize(payload, **kwargs):
        sent.append(payload)
        return {'summary': '保留芯片新闻材料；' + content, 'constraints': [content, '以官方来源核验芯片新闻']}
    monkeypatch.setattr('src.agent.compaction.minimax_summary', summarize)
    result = current.compact_agent_conversation(cid, policy={'soft_threshold': 256, 'keep_recent': 1})
    assert result['saved'] is True and len(sent) == 1
    assert content not in str(sent)
    snapshot = current.conversation_store.active_snapshot(cid)
    assert content not in snapshot['summary'] and content not in snapshot['constraints']
    assert snapshot['task_state']['memory_policy_revisions'][row['id']] == row['revision'] + 1
    assert content in str(current.conversation_store.context_messages(cid))


def test_forget_during_compaction_rejects_stale_memory_snapshot(pair, monkeypatch):
    from backend.capabilities import manager
    from types import SimpleNamespace
    currents, cid, _ = pair
    current = currents[0]
    memory = manager(current).memory
    row = memory.save({'content': '陈旧偏好需要遗忘', 'scope': 'workspace'})
    saved = current._read_agent_conversation(cid)
    saved['messages'].extend({'id': uuid4().hex, 'role': 'user', 'content': '待办材料' * 300} for _ in range(4))
    current.conversation_store.save(saved)
    monkeypatch.setattr(current, 'freeze_model_roles', lambda env, request: {})
    monkeypatch.setattr('src.model_platforms.integration.legacy_controller', lambda env: SimpleNamespace(provider='minimax', model='test-controller'))
    called = []
    def summarize(payload, **kwargs):
        called.append(1)
        memory.forget(row['id'], expected_revision=row['revision'])
        return {'summary': row['content'], 'constraints': [row['content']]}
    monkeypatch.setattr('src.agent.compaction.minimax_summary', summarize)
    result = current.compact_agent_conversation(cid, policy={'soft_threshold': 256, 'keep_recent': 1})
    assert result['status'] == 'conflict'
    assert called == [1]
    assert current.conversation_store.active_snapshot(cid) is None


def test_new_message_during_compaction_rejects_stale_summary_without_losing_messages(pair, monkeypatch):
    from types import SimpleNamespace
    currents, cid, _ = pair
    current, other = currents
    saved = current._read_agent_conversation(cid)
    saved['messages'].extend({'id': uuid4().hex, 'role': 'user', 'content': '旧事件核验材料' * 300}
                             for _ in range(4))
    current.conversation_store.save(saved)
    before = current.conversation_store.context_messages(cid)
    monkeypatch.setattr(current, 'freeze_model_roles', lambda env, request: {})
    monkeypatch.setattr('src.model_platforms.integration.legacy_controller', lambda env:
                        SimpleNamespace(provider='minimax', model='test-controller'))
    calls = []
    new_id = uuid4().hex

    def summarize(payload, **kwargs):
        calls.append(payload)
        concurrent = other._read_agent_conversation(cid)
        concurrent['messages'].append({'id': new_id, 'role': 'user', 'content': '追加新的官方来源核验要求'})
        other.conversation_store.save(concurrent)
        return {'summary': '已概括旧材料', 'constraints': ['保留官方来源']}

    monkeypatch.setattr('src.agent.compaction.minimax_summary', summarize)
    result = current.compact_agent_conversation(cid, policy={'soft_threshold': 256, 'keep_recent': 1})
    assert result['status'] == 'conflict'
    assert len(calls) == 1
    assert current.conversation_store.active_snapshot(cid) is None
    after = current.conversation_store.context_messages(cid)
    assert after[:len(before)] == before
    assert after[-1]['id'] == new_id
    assert after[-1]['content'] == '追加新的官方来源核验要求'


def test_forget_rolls_back_preference_if_snapshot_invalidation_fails(memory, monkeypatch):
    from contextlib import contextmanager
    row = memory.save({'content': '原子遗忘测试', 'scope': 'workspace'})
    original = memory.store.knowledge_store.connection

    class FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def transaction(self):
            return self.connection.transaction()

        def execute(self, query, *args, **kwargs):
            if str(query).startswith('UPDATE agent.compaction_snapshots'):
                raise RuntimeError('injected snapshot failure')
            return self.connection.execute(query, *args, **kwargs)

    @contextmanager
    def failing_connection(*args, **kwargs):
        with original(*args, **kwargs) as connection:
            yield FailingConnection(connection)

    with monkeypatch.context() as context:
        context.setattr(memory.store.knowledge_store, 'connection', failing_connection)
        with pytest.raises(RuntimeError, match='injected snapshot failure'):
            memory.forget(row['id'], expected_revision=row['revision'])
    latest = memory.get(row['id'])
    assert latest['forgotten'] is False
    assert latest['active'] is True
    assert latest['revision'] == row['revision']
    assert len(latest['versions']) == 1


@pytest.mark.parametrize('scope,scope_id', [('workspace',''), ('column','daily_news'), ('conversation','target')])
def test_forget_only_invalidates_eligible_namespace_and_scope(memory, scope, scope_id):
    from psycopg.types.json import Jsonb
    token=uuid4().hex
    identities={key:uuid4().hex for key in ('target','same_namespace_other_scope','other_namespace','known_unrelated')}
    value={'content':'独立偏好'+token,'scope':scope,'scope_id':identities['target'] if scope=='conversation' else scope_id}
    row=memory.save(value)
    store=memory.store.knowledge_store
    with store.connection() as conn,conn.transaction():
        for key,identity in identities.items():
            namespace=memory.store.namespace if key!='other_namespace' else memory.store.namespace+'_other'
            payload={'column':'daily_news' if key=='target' else 'daily_ai_digest'}
            refs={'memory_refs': ['memory_unrelated']} if key=='known_unrelated' else {}
            conn.execute('INSERT INTO agent.conversations(conversation_id,account_namespace,payload) VALUES (%s,%s,%s)',
                         (identity,namespace,Jsonb(payload)))
            conn.execute('''INSERT INTO agent.compaction_snapshots
                (conversation_id,version,through_seq,summary,constraints,evidence_refs,task_state,input_tokens,output_tokens,status)
                VALUES (%s,1,1,%s,'[]','[]',%s,100,10,'active')''',(identity,row['content'],Jsonb(refs)))
    try:
        memory.forget(row['id'],expected_revision=row['revision'])
        with store.connection() as conn:
            states={r['conversation_id']:r['status'] for r in conn.execute(
                'SELECT conversation_id,status FROM agent.compaction_snapshots WHERE conversation_id=ANY(%s)',(list(identities.values()),)).fetchall()}
        assert states[identities['target']]=='invalidated'
        assert states[identities['other_namespace']]=='active'
        assert states[identities['known_unrelated']]=='active'
        assert states[identities['same_namespace_other_scope']]==('invalidated' if scope=='workspace' else 'active')
    finally:
        with store.connection() as conn,conn.transaction():
            conn.execute('DELETE FROM agent.conversations WHERE conversation_id=ANY(%s)',(list(identities.values()),))


def test_no_summary_keeps_all_eligible_history():
    from src.agent.compaction import compacted_context
    class Conversations:
        def active_snapshot(self, identity): return None
        def context_messages(self, identity): return [{'seq':i,'role':'user','content':str(i)} for i in range(1,26)]
    result=compacted_context(Conversations(),'conversation')
    assert len(result['recent_messages'])==25
    assert result['recent_messages'][0]['seq']==1


def test_knowledge_exclusion_is_enforced_in_namespace_search(monkeypatch):
    from src.agent.capabilities.store import CapabilityStore
    from src.agent.capabilities.knowledge_service import KnowledgeManagement
    from src.knowledge.models import KnowledgeDocument
    from src.knowledge.store import KnowledgeStore
    from src.knowledge.embeddings import index_pending_documents
    token=uuid4().hex
    namespace='test_knowledge_'+token
    store=KnowledgeStore.from_env()
    store.ensure_schema()
    caps=CapabilityStore(store,namespace=namespace)
    caps.ensure_schema()
    knowledge=KnowledgeManagement(store,caps)
    vector=[1.0]+[0.0]*383
    class Embedder:
        def embed_query(self,value): return vector
        def embed_documents(self,values): return [vector for _ in values]
    monkeypatch.setattr('src.knowledge.embeddings.get_embedding_model',lambda:Embedder())
    for ns in [namespace,namespace+'_other']:
        store.upsert_document(KnowledgeDocument(record_id='model-release',record_type='news',title='测试模型发布',
            body='模型今日正式发布并开放权重',account_namespace=ns,allowed_purposes=['evidence','duplicate_reference']))
        index_pending_documents(store,account_namespace=ns)
    assert len(store.search('模型发布',purpose='evidence',account_namespace=namespace))==1
    policy=knowledge.policy('model-release',{'namespace':namespace,'expected_revision':0,'excluded_purposes':['evidence'],
                                        'annotation':'内容尚未完成官方核验','source_ref':'manual:test'})
    assert policy['revision']==1
    assert store.search('模型发布',purpose='evidence',account_namespace=namespace)==[]
    assert store.search('模型发布',purpose='duplicate_reference',account_namespace=namespace)[0]['record_id']=='model-release'
    assert store.search('模型发布',purpose='evidence',account_namespace=namespace+'_other')[0]['account_namespace']==namespace+'_other'


def test_knowledge_directory_filters_before_pagination_and_normalizes_fields():
    from src.agent.capabilities.store import CapabilityStore
    from src.agent.capabilities.knowledge_service import KnowledgeManagement
    from src.knowledge.models import KnowledgeDocument
    from src.knowledge.store import KnowledgeStore
    namespace='knowledge_directory_'+uuid4().hex
    store=KnowledgeStore.from_env()
    store.ensure_schema()
    caps=CapabilityStore(store,namespace=namespace)
    caps.ensure_schema()
    service=KnowledgeManagement(store,caps)
    for i in range(5):
        store.upsert_document(KnowledgeDocument(record_id='item-'+str(i),record_type='news',
            title='待索引原文' if i<3 else '',body='有效文本' if i<3 else '',
            account_namespace=namespace,allowed_purposes=('evidence','style_reference'),
            source_published_at='2026-10-09T08:00:00+08:00'))
    first=service.list(namespace=namespace,index_status='skipped_empty',limit=1)
    assert [row['id'] for row in first['rows']]==['item-3']
    row=first['rows'][0]
    assert row['namespace']==namespace and row['type']=='news'
    assert row['purposes']==['evidence','style_reference']
    assert row['published_at']=='2026-10-09T08:00:00+08:00'
    second=service.list(namespace=namespace,index_status='skipped_empty',limit=1,cursor=first['next_cursor'])
    assert [row['id'] for row in second['rows']]==['item-4'] and second['next_cursor'] is None
    assert first['index_progress']['empty_documents']==2


def test_knowledge_detail_exposes_the_requested_namespace():
    from src.agent.capabilities.store import CapabilityStore
    from src.agent.capabilities.knowledge_service import KnowledgeManagement
    from src.knowledge.models import KnowledgeDocument
    from src.knowledge.store import KnowledgeStore
    namespace='knowledge_detail_'+uuid4().hex
    store=KnowledgeStore.from_env()
    store.ensure_schema()
    caps=CapabilityStore(store,namespace=namespace)
    caps.ensure_schema()
    store.upsert_document(KnowledgeDocument(record_id='source',record_type='news',account_namespace=namespace,
        title='核验来源',body='原始文本'))
    assert KnowledgeManagement(store,caps).detail('source',namespace)['namespace']==namespace


def test_selection_does_not_silently_drop_the_fifty_first_preference(memory):
    for index in range(60):
        memory.save({'content':'已确认偏好'+str(index),'key':'preference_'+str(index),'scope':'workspace'})
    assert len(memory.select()) == 60


def test_oversized_preference_selection_requires_explicit_reduction(memory):
    from src.agent.capabilities.models import CapabilityError
    for index in range(101):
        memory.save({'content':'已确认偏好'+str(index),'key':'preference_'+str(index),'scope':'workspace'})
    with pytest.raises(CapabilityError,match='MEMORY_SELECTION_TOO_LARGE'):
        memory.select()


def test_large_knowledge_statistics_skip_empty_rows_in_real_postgres():
    from pgvector.psycopg import Vector
    from src.knowledge.store import KnowledgeStore, _MODEL_ID, _CHUNK_VERSION
    namespace = 'knowledge_counts_'+uuid4().hex
    store = KnowledgeStore.from_env()
    store.ensure_schema()
    try:
        with store.connection() as conn, conn.transaction():
            conn.execute('''INSERT INTO knowledge.documents
                (record_id, record_type, account_namespace, title, body, content_hash)
                SELECT 'item-'||n, 'news', %s, '', CASE WHEN n<=4372 THEN '核验材料' ELSE '  ' END,
                'fixture-'||n FROM generate_series(1,4390) n''', (namespace,))
            conn.execute('''INSERT INTO knowledge.chunks
                (chunk_id, document_id, document_version, chunk_index, content, char_start, char_end,
                 token_count, embedding, model_id, chunk_version, batch_id, index_status)
                SELECT %s||d.id, d.id, d.content_hash, 0, d.body, 0, 4, 4, %s, %s, %s, %s, 'ready'
                FROM knowledge.documents d WHERE d.account_namespace=%s AND d.body='核验材料' ''',
                (namespace, Vector([1.0]+[0.0]*383), _MODEL_ID, _CHUNK_VERSION, namespace, namespace))
        assert store.index_progress(account_namespace=namespace) == {
            'documents':4390, 'indexed_documents':4372, 'empty_documents':18, 'pending_documents':0}
        assert store.pending_documents(account_namespace=namespace) == []
    finally:
        with store.connection() as conn, conn.transaction():
            conn.execute('DELETE FROM knowledge.documents WHERE account_namespace=%s', (namespace,))


def test_namespace_list_index_detail_and_search_are_consistent(monkeypatch):
    from src.agent.capabilities.store import CapabilityStore
    from src.agent.capabilities.knowledge_service import KnowledgeManagement
    from src.knowledge.models import KnowledgeDocument
    from src.knowledge.store import KnowledgeStore
    from src.knowledge.embeddings import index_pending_documents
    namespace = 'knowledge_isolation_'+uuid4().hex
    other = namespace+'_other'
    store = KnowledgeStore.from_env()
    store.ensure_schema()
    caps = CapabilityStore(store, namespace=namespace)
    caps.ensure_schema()
    service = KnowledgeManagement(store, caps)
    vector = [1.0]+[0.0]*383

    class Embedder:
        def embed_query(self, value): return vector
        def embed_documents(self, values): return [vector for _ in values]

    monkeypatch.setattr('src.knowledge.embeddings.get_embedding_model', lambda: Embedder())
    try:
        for ns, body in ((namespace, '官方模型发布甲命名空间'), (other, '官方模型发布乙命名空间')):
            store.upsert_document(KnowledgeDocument(record_id='same-name', record_type='news',
                account_namespace=ns, title='官方模型发布', body=body, allowed_purposes=['evidence']))
        index_pending_documents(store, account_namespace=namespace)
        assert service.list(namespace=namespace)['rows'][0]['index_status'] == 'ready'
        assert service.list(namespace=other)['rows'][0]['index_status'] == 'pending'
        assert service.detail('same-name', namespace)['namespace'] == namespace
        assert service.detail('same-name', other)['chunks'] == []
        assert '甲命名空间' in str(service.search({'namespace':namespace, 'query':'官方模型', 'purpose':'evidence'})['rows'])
        with pytest.raises(RuntimeError, match='KNOWLEDGE_INDEX_NOT_READY'):
            service.search({'namespace':other, 'query':'官方模型', 'purpose':'evidence'})
        index_pending_documents(store, account_namespace=other)
        assert service.list(namespace=other)['rows'][0]['index_status'] == 'ready'
        assert '乙命名空间' in str(service.search({'namespace':other, 'query':'官方模型', 'purpose':'evidence'})['rows'])
        assert '乙命名空间' not in str(service.search({'namespace':namespace, 'query':'官方模型', 'purpose':'evidence'})['rows'])
    finally:
        with store.connection() as conn, conn.transaction():
            conn.execute('DELETE FROM knowledge.documents WHERE account_namespace=ANY(%s)', ([namespace, other],))
