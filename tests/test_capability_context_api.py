from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from test_plan_postgres_concurrency import pair


@pytest.fixture
def context_client(pair,monkeypatch):
    from backend import app as module
    currents,cid,plan=pair
    monkeypatch.setattr(module.app.state,'service',currents[0])
    monkeypatch.setattr(module,'ensure_review_schema',lambda:None)
    with TestClient(module.app,base_url='http://127.0.0.1:8786') as client:
        client.post('/api/session',headers={'x-workbench':'1'})
        yield client,currents[0],cid


def test_context_read_reports_actual_range_without_model_calls(context_client,monkeypatch):
    client,current,cid=context_client
    monkeypatch.setattr(current,'compact_agent_conversation',lambda *a,**k:pytest.fail('GET called a model'))
    response=client.get('/api/conversations/'+cid+'/context')
    assert response.status_code==200,response.text
    data=response.json()
    assert data['policy']=={'mode':'manual','soft_threshold':12000,'keep_recent':16}
    assert data['context']['recent_messages'][0]['content']=='生成10条每日新闻，不上传'
    assert data['tokens_estimate']>0 and data['snapshots']==[]


def test_context_policy_persists_and_rejects_stale_or_invalid_values(context_client):
    client,current,cid=context_client
    url='/api/conversations/'+cid+'/context-policy'
    body={'expected_revision':0,'mode':'auto','soft_threshold':3000,'keep_recent':20}
    assert client.put(url,json=body).status_code==403
    response=client.put(url,json=body,headers={'x-workbench':'1'})
    assert response.status_code==200,response.text
    assert client.get('/api/conversations/'+cid+'/context').json()['policy']['mode']=='auto'
    assert client.put(url,json=body,headers={'x-workbench':'1'}).status_code==409
    for bad in ({**body,'expected_revision':1,'keep_recent':0},{**body,'expected_revision':1,'mode':'magic'}):
        assert client.put(url,json=bad,headers={'x-workbench':'1'}).status_code==422


def test_manual_compaction_is_an_explicit_observable_operation(context_client,monkeypatch):
    import time
    client,current,cid=context_client
    invoked=[]
    monkeypatch.setattr(current,'compact_agent_conversation',lambda *args,**kwargs:invoked.append(args) or {'status':'not_needed','saved':False})
    response=client.post('/api/conversations/'+cid+'/compact',json={},headers={'x-workbench':'1','idempotency-key':uuid4().hex})
    assert response.status_code==200,response.text
    identity=response.json()['operation_id']
    for _ in range(100):
        result=client.get('/api/capabilities/checks/'+identity).json()
        if result['status'] not in {'queued','running'}:break
        time.sleep(.01)
    assert result['status']=='succeeded'
    assert len(invoked)==1 and current.jobs=={}


def test_auto_policy_compacts_only_during_valid_new_confirmation(context_client,monkeypatch):
    from backend.capabilities import manager
    from backend.plan_service import PlanService
    client,current,cid=context_client
    capabilities=manager(current)
    capabilities.context.save_policy(cid,{'expected_revision':0,'mode':'auto','soft_threshold':256,'keep_recent':1})
    saved=current._read_agent_conversation(cid)
    saved['messages'].append({'id':uuid4().hex,'role':'user','content':'已有的上下文材料'*300})
    current.conversation_store.save(saved)
    invoked=[]
    monkeypatch.setattr(current,'compact_agent_conversation',lambda identity,**kwargs:invoked.append(kwargs['policy']) or {'status':'not_needed'})
    monkeypatch.setattr(current,'submit',lambda req,key:{'id':key,'status':'queued'})
    service=PlanService(current)
    plan=service.current_plan(cid)
    request={'version':plan['version'],'semantic_hash':plan['semantic_hash'],'skill_mode':'off','skill_names':[]}
    assert not invoked
    service.confirm(cid,plan['id'],request,'auto-confirm')
    service.confirm(cid,plan['id'],request,'auto-confirm')
    assert invoked==[{'mode':'auto','soft_threshold':256,'keep_recent':1}]


def test_invalid_confirmation_does_not_trigger_auto_model(context_client,monkeypatch):
    from backend.capabilities import manager
    from backend.plan_service import PlanService
    from src.agent.plan_contract import PlanContractError
    _,current,cid=context_client
    manager(current).context.save_policy(cid,{'expected_revision':0,'mode':'auto','soft_threshold':256,'keep_recent':1})
    monkeypatch.setattr(current,'compact_agent_conversation',lambda *a,**k:pytest.fail('invalid confirmation called model'))
    service=PlanService(current)
    plan=service.current_plan(cid)
    with pytest.raises(PlanContractError):
        service.confirm(cid,plan['id'],{'version':0},'invalid-auto')


def test_reused_confirmation_key_is_rejected_before_auto_model(context_client,monkeypatch):
    from backend.capabilities import manager
    from backend.plan_service import PlanService
    from src.agent.plan_contract import PlanContractError
    _,current,cid=context_client
    service=PlanService(current)
    original=service.current_plan(cid)
    service.claim(cid,original['id'],{'version':original['version']},'used-confirm-key')
    current.append_agent_message(cid,'生成1条每日新闻，不上传')
    manager(current).context.save_policy(cid,{'expected_revision':0,'mode':'auto','soft_threshold':256,'keep_recent':1})
    saved=current._read_agent_conversation(cid)
    saved['messages'].append({'id':uuid4().hex,'role':'user','content':'待压缩上下文材料'*200})
    current.conversation_store.save(saved)
    called=[]
    monkeypatch.setattr(current,'compact_agent_conversation',lambda *a,**k:called.append(1) or {'status':'not_needed'})
    plan=service.current_plan(cid)
    with pytest.raises(PlanContractError) as error:
        service.confirm(cid,plan['id'],{'version':plan['version']},'used-confirm-key')
    assert error.value.code=='CONFIRM_REQUEST_CONFLICT'
    assert called==[]


def test_concurrent_auto_compaction_uses_one_persistent_operation(pair,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event, Lock
    from backend.capabilities import manager
    currents,cid,_=pair
    services=[manager(current).context for current in currents]
    barrier=Barrier(2)
    release=Event()
    entered=Event()
    lock=Lock()
    called=[]
    def overview(identity):
        barrier.wait(timeout=5)
        return {'policy':{'mode':'auto','soft_threshold':256,'keep_recent':1},
                'policy_revision':0,'threshold_exceeded':True,
                'coverage':{'summary_through_seq':0,'retained_sequences':[1,2]},'raw_message_count':2}
    def compact(identity,**kwargs):
        with lock: called.append(identity)
        entered.set()
        assert release.wait(timeout=5)
        return {'status':'not_needed','saved':False}
    for service in services:
        monkeypatch.setattr(service,'overview',overview)
        monkeypatch.setattr(service.workbench,'compact_agent_conversation',compact)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures=[executor.submit(service.prepare,cid) for service in services]
        try:
            assert entered.wait(timeout=5)
            release.set()
            results=[future.result(timeout=10) for future in futures]
        finally:release.set()
    assert called==[cid]
    assert all(result['status']=='not_needed' for result in results)
    with services[0].store.knowledge_store.connection() as conn:
        rows=conn.execute('SELECT id,status FROM agent.capability_operations WHERE namespace=%s',
                         (services[0].store.namespace,)).fetchall()
    assert len(rows)==1 and rows[0]['status']=='succeeded'
