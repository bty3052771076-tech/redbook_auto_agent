from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend import app as module
from backend.app import app
from src.agent.capabilities.service import CapabilityService
from src.agent.capabilities.store import CapabilityStore


@pytest.fixture
def client(monkeypatch):
    store=CapabilityStore(namespace='test_api_'+uuid4().hex)
    store.ensure_schema()
    current=SimpleNamespace(root=module.RUNTIME,environment=lambda:{},providers=lambda:{'bindings':{}},redact=lambda value:value)
    manager=CapabilityService(module.RUNTIME,store=store,workbench=current)
    current._capability_manager=manager
    monkeypatch.setattr(module.app.state,'service',current)
    monkeypatch.setattr(module,'ensure_review_schema',lambda:None)
    with TestClient(app,base_url='http://127.0.0.1:8786') as test:
        test.post('/api/session',headers={'x-workbench':'1'})
        yield test,manager
    manager.operations.close()


def test_capability_reads_do_not_start_external_tools(client,monkeypatch):
    from src.agent.mcp_manager import MCPManager
    monkeypatch.setattr(MCPManager,'list_tools',lambda self:pytest.fail('GET started MCP'))
    test,_=client
    result=test.get('/api/capabilities')
    assert result.status_code==200
    value=result.json()
    assert value['rows']
    assert value['database']['status']=='ready'
    assert value['environment']['python_executable'].endswith('python.exe')
    assert test.get('/api/mcp/connections').json()['rows'][0]['health']['status']=='unknown'
    assert test.get('/api/skills').json()['default_mode']=='off'


def test_catalog_counts_and_total_describe_all_filtered_rows_not_current_page(client):
    test,service=client
    service.store.put('builtin:news.generate','tool',{'enabled':True,'health':{'status':'ready'}},expected_revision=0)
    all_rows=service.all_tools()
    first=test.get('/api/capabilities?limit=1').json()
    assert len(first['rows'])==1
    assert first['counts']['available']==sum(row.get('enabled',False) and row.get('health',{}).get('status')=='ready' for row in all_rows)
    assert first['counts']['registered']==len(all_rows)==first['total']
    second=test.get('/api/capabilities',params={'limit':1,'cursor':first['next_cursor']}).json()
    assert second['counts']==first['counts'] and second['total']==first['total']
    empty=test.get('/api/capabilities?query=definitely-unmatched-resource').json()
    assert empty['rows']==[] and empty['counts']=={'registered':0,'available':0,'enabled':0}


def test_builtin_details_describe_real_workflow_contract_even_after_old_policy_save(client):
    test,service=client
    service.store.put('builtin:news.generate','tool',{'enabled':False,'input_schema':{'type':'object'},
        'output_schema':{'type':'object'}},expected_revision=0)
    details=test.get('/api/capabilities/builtin:news.generate').json()
    assert details['enabled'] is False
    assert details['input_schema']['properties']['job']['properties']['count']['type']=='integer'
    assert details['input_schema']['properties']['context']['type']=='object'
    assert details['output_schema']['type']=='array'
    assert details['output_schema']['items']['properties']['title']['type']=='string'
    assert '工作流' in details['concurrency_policy']
    upload=test.get('/api/capabilities/builtin:xhs.drafts.save_batch').json()
    assert upload['concurrency']==1
    assert '串行' in upload['concurrency_policy']


def test_dependency_links_are_readonly_projection_not_runtime_policy(client):
    test,service=client
    identity='builtin:news.generate'
    detail=test.get('/api/capabilities/'+identity).json()
    assert detail['dependency_details'][0]['resource_id']=='builtin:writer.generate'
    assert detail['dependency_details'][0]['name']=='内容写稿'
    saved=test.patch('/api/capabilities/'+identity,headers={'x-workbench':'1'},
                     json={'expected_revision':0,'enabled':False})
    assert saved.status_code==200
    stored=service.store.get(identity)
    assert 'dependency_details' not in stored
    assert stored['dependencies']==['builtin:writer.generate','builtin:image.generate','builtin:news.search']


def test_tool_mutation_uses_revision_and_session_guard(client):
    test,_=client
    identity='builtin:news.generate'
    assert test.patch('/api/capabilities/'+identity,json={'expected_revision':0,'enabled':False}).status_code==403
    response=test.patch('/api/capabilities/'+identity,headers={'x-workbench':'1'},json={'expected_revision':0,'enabled':False})
    assert response.status_code==200
    assert response.json()['enabled'] is False
    conflict=test.patch('/api/capabilities/'+identity,headers={'x-workbench':'1'},json={'expected_revision':0,'enabled':True})
    assert conflict.status_code==409
    assert conflict.json()['code']=='REVISION_CONFLICT'
    assert test.get('/api/capabilities/'+identity).json()['revision']==1


def test_async_check_has_observable_operation_and_no_generation(client):
    import time
    test,_=client
    response=test.post('/api/capabilities/checks',headers={'x-workbench':'1'},json={'resource_ids':['builtin:artifacts.read']})
    assert response.status_code==200
    operation=response.json()['operation_id']
    for _ in range(100):
        result=test.get('/api/capabilities/checks/'+operation).json()
        if result['status'] not in {'queued','running'}:
            break
        time.sleep(.02)
    assert result['status']=='succeeded'
    assert result['results'][0]['health']['probe']=='configuration'


def test_trial_api_requires_session_exact_preview_and_acknowledgement(client,monkeypatch):
    import time
    test, capabilities = client
    post_id = 'a'*32
    requests = []
    monkeypatch.setattr(capabilities.workbench,'post',lambda identity:{'id':identity,'body':'审核内容'},raising=False)

    def submit(request,key):
        requests.append((request,key))
        return {'id':'test-run','status':'completed','message':'local validation','post_ids':[]}

    monkeypatch.setattr(capabilities.workbench,'submit',submit,raising=False)
    endpoint = '/api/capabilities/builtin:content.review/trial-preview'
    assert test.post(endpoint,json={'expected_revision':0,'post_id':post_id}).status_code == 403
    preview = test.post(endpoint,headers={'x-workbench':'1'},json={'expected_revision':0,'post_id':post_id})
    assert preview.status_code == 200,preview.text
    assert requests == []
    value = preview.json()
    confirm = '/api/capability-trials/'+value['preview_id']+'/confirm'
    assert test.post(confirm,headers={'x-workbench':'1'},json={
        'preview_hash':value['preview_hash'],'acknowledge_effects':False}).status_code == 422
    assert requests == []
    body = {'preview_hash':value['preview_hash'],'acknowledge_effects':True}
    operation = test.post(confirm,headers={'x-workbench':'1'},json=body).json()['operation_id']
    assert test.post(confirm,headers={'x-workbench':'1'},json=body).json()['operation_id'] == operation
    for _ in range(100):
        result = test.get('/api/capabilities/checks/'+operation).json()
        if result['status'] not in {'queued','running'}:
            break
        time.sleep(.02)
    assert result['status'] == 'succeeded',result
    assert requests == [({'kind':'validate','post_id':post_id,'platform':'xhs'},value['preview_id'])]
    assert capabilities.store.calls(origin='diagnostic')['rows'][0]['status'] == 'succeeded'
    assert capabilities.store.calls(origin='diagnostic')['rows'][0]['run_id'] == 'test-run'


def test_memory_api_is_persistent_and_no_secret_fields_in_catalog(client):
    test,manager=client
    response=test.post('/api/memory/items',headers={'x-workbench':'1'},json={'content':'配图尽量不含文字','scope':'column','scope_id':'daily_news'})
    assert response.status_code==200
    identity=response.json()['id']
    assert test.get('/api/memory/items').json()['rows'][0]['id']==identity
    assert test.get('/api/resource-changes').json()['rows']


def test_unsupported_upload_timeout_is_not_accepted_as_working_configuration(client):
    test,_=client
    result=test.patch('/api/capabilities/builtin:xhs.drafts.save_batch',headers={'x-workbench':'1'},
                      json={'expected_revision':0,'timeout_seconds':10})
    assert result.status_code==422
    assert result.json()['code']=='CAPABILITY_TIMEOUT_UNSUPPORTED'


def test_document_update_and_async_reindex_excludes_previous_chunks_in_real_postgres(client, monkeypatch):
    import time
    from src.knowledge.models import KnowledgeDocument
    test, manager = client
    namespace = manager.store.namespace
    store = manager.store.knowledge_store
    vector = [1.0]+[0.0]*383
    class Embedder:
        def embed_query(self, value): return vector
        def embed_documents(self, values): return [vector for _ in values]
    monkeypatch.setattr('src.knowledge.embeddings.get_embedding_model', lambda: Embedder())

    def index_and_wait():
        result = test.post('/api/knowledge/index-jobs', json={'namespace':namespace},
            headers={'x-workbench':'1','idempotency-key':uuid4().hex})
        assert result.status_code == 200, result.text
        identity = result.json()['operation_id']
        for _ in range(200):
            operation = test.get('/api/capabilities/checks/'+identity).json()
            if operation['status'] not in {'queued','running'}:
                break
            time.sleep(.01)
        assert operation['status'] == 'succeeded', operation

    document = KnowledgeDocument(record_id='release',record_type='news',account_namespace=namespace,
        title='官方模型发布',body='原版片段测试',allowed_purposes=['evidence'])
    store.upsert_document(document)
    index_and_wait()
    detail = test.get('/api/knowledge/documents/release',params={'namespace':namespace}).json()
    old_hash = detail['content_hash']
    assert detail['chunks'] and all(row['document_version']==old_hash for row in detail['chunks'])
    store.upsert_document(KnowledgeDocument(record_id='release',record_type='news',account_namespace=namespace,
        title='官方模型发布',body='新版片段测试',allowed_purposes=['evidence']))
    pending = test.get('/api/knowledge/documents',params={'namespace':namespace,'index_status':'pending'}).json()
    assert [row['id'] for row in pending['rows']] == ['release']
    index_and_wait()
    current = test.get('/api/knowledge/documents/release',params={'namespace':namespace}).json()
    assert current['content_hash'] != old_hash
    assert current['versions'][0]['content_hash'] == old_hash
    assert all(row['document_version'] == current['content_hash'] and '原版片段' not in row['content'] for row in current['chunks'])
    result = test.post('/api/knowledge/search', json={'namespace':namespace,'query':'官方模型','purpose':'evidence'},headers={'x-workbench':'1'})
    assert result.status_code == 200, result.text
    assert result.json()['rows'] and '新版片段' in str(result.json()['rows'])
    assert '原版片段' not in str(result.json()['rows'])


def test_real_postgres_connection_failure_is_readonly_and_recoverable(client, tmp_path, monkeypatch):
    import socket
    from psycopg.conninfo import make_conninfo
    from psycopg_pool import ConnectionPool
    test, manager = client
    knowledge = manager.store.knowledge_store
    artifact = tmp_path/'retained-draft.txt'
    artifact.write_text('keep completed content', encoding='utf-8')
    created = test.post('/api/memory/items', headers={'x-workbench':'1'},
                        json={'content':'保留偏好','scope':'workspace'})
    assert created.status_code == 200, created.text
    memory = created.json()
    before = manager.store.resources()
    previous = knowledge._pool_by_role.copy()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    config = knowledge._credentials()
    config.update(host='127.0.0.1', port=listener.getsockname()[1])
    pool = ConnectionPool(make_conninfo(**config, connect_timeout=1), min_size=0, max_size=1,
                          timeout=1, open=True)
    try:
        monkeypatch.setattr(knowledge, '_pool_by_role', {**previous, 'app':pool})
        catalog = test.get('/api/capabilities')
        assert catalog.status_code == 200
        data = catalog.json()
        assert data['database']['status'] == 'offline' and data['read_only'] is True
        assert data['rows'] and all(row['kind']=='builtin' and row['configuration_available'] is False
                                    for row in data['rows'])
        for result in [test.post('/api/memory/items', headers={'x-workbench':'1'},
                                  json={'content':'must not save','scope':'workspace'}),
                       test.patch('/api/capabilities/builtin:news.generate', headers={'x-workbench':'1'},
                                  json={'expected_revision':0,'enabled':False})]:
            assert result.status_code == 503 and result.json()['code'] == 'POSTGRES_UNAVAILABLE'
            assert config['password'] not in result.text
        assert artifact.read_text(encoding='utf-8') == 'keep completed content'
    finally:
        monkeypatch.setattr(knowledge, '_pool_by_role', previous)
        pool.close()
        listener.close()
    assert manager.store.resources() == before
    assert [row['id'] for row in test.get('/api/memory/items').json()['rows']] == [memory['id']]
    assert test.get('/api/capabilities').json()['database']['status'] == 'ready'


def test_builtin_skill_copy_api_uses_session_and_source_revision(client, tmp_path):
    from src.agent.capabilities.skill_service import SkillService
    from test_capability_skills import builtin
    test, manager = client
    manager.skills = SkillService(tmp_path, manager.store)
    original = builtin(manager.skills, tmp_path)
    body = {'expected_revision':original['revision'], 'name':'api-personal-release'}
    url = '/api/skills/'+original['id']+'/copy'
    assert test.post(url, json=body).status_code == 403
    result = test.post(url, json=body, headers={'x-workbench':'1'})
    assert result.status_code == 200, result.text
    assert result.json()['id'] != original['id'] and result.json()['enabled'] is False
    assert manager.skills.get(original['id'])['body'] == original['body']
