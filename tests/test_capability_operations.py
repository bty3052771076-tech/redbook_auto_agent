from uuid import uuid4

import pytest

from src.agent.capabilities.models import CapabilityError
from src.agent.capabilities.operations import OperationManager
from src.agent.capabilities.store import CapabilityStore


@pytest.fixture(autouse=True)
def configured_runtime():
    from backend.settings import configure_runtime
    configure_runtime()


def test_same_operation_key_does_not_collide_across_namespaces():
    stores=[CapabilityStore(namespace='operation_test_'+uuid4().hex) for _ in range(2)]
    managers=[]
    try:
        for store in stores:
            store.ensure_schema()
            managers.append(OperationManager(store))
        key=uuid4().hex
        first=managers[0].submit(lambda:{'owner':'first'},title='检测',key=key)
        second=managers[1].submit(lambda:{'owner':'second'},title='检测',key=key)
        assert first['operation_id']!=second['operation_id']
        for index,manager in enumerate(managers):
            manager.futures[[first,second][index]['operation_id']].result(timeout=5)
        assert managers[0].get(first['operation_id'])['results']=={'owner':'first'}
        assert managers[1].get(second['operation_id'])['results']=={'owner':'second'}
    finally:
        for manager in managers:manager.close()


def test_operation_replay_requires_same_request_not_just_same_key():
    store=CapabilityStore(namespace='operation_test_'+uuid4().hex)
    store.ensure_schema()
    manager=OperationManager(store)
    try:
        called=[]
        first=manager.submit(lambda:called.append(1) or [],title='检测',key='same',request={'tools':['first']})
        replay=manager.submit(lambda:pytest.fail('replay reran'),title='检测',key='same',request={'tools':['first']})
        assert replay['operation_id']==first['operation_id']
        with pytest.raises(CapabilityError,match='同一'):
            manager.submit(lambda:[],title='检测',key='same',request={'tools':['second']})
        manager.futures[first['operation_id']].result(timeout=5)
        assert called==[1]
    finally:manager.close()


def test_interrupted_operation_is_not_reported_running_forever():
    from psycopg.types.json import Jsonb
    store=CapabilityStore(namespace='operation_test_'+uuid4().hex)
    store.ensure_schema()
    manager=OperationManager(store)
    identity=uuid4().hex
    try:
        with store.knowledge_store.connection() as conn:
            conn.execute('INSERT INTO agent.capability_operations(id,namespace,status,payload) VALUES (%s,%s,%s,%s)',
                         (identity,store.namespace,'running',Jsonb({'title':'已中断的检测','lease_managed':True})))
        result=manager.get(identity)
        assert result['status']=='failed'
        assert result['error']['code']=='OPERATION_INTERRUPTED'
        assert result['error']['retryable'] is True
    finally:manager.close()


def test_shutdown_cancels_queued_operation_and_releases_lease(tmp_path):
    import threading
    import time
    from src.model_platforms.security import file_lock
    store=CapabilityStore(namespace='operation_test_'+uuid4().hex)
    store.ensure_schema()
    manager=OperationManager(store,root=tmp_path)
    release=threading.Event()
    entered=threading.Barrier(3)
    def blocked():
        entered.wait(timeout=5)
        assert release.wait(timeout=5)
        return []
    try:
        for i in range(2):manager.submit(blocked,title='在途检测',key=str(i))
        entered.wait(timeout=5)
        third=manager.submit(lambda:pytest.fail('cancelled operation ran'),title='排队检测',key='third')
        closer=threading.Thread(target=manager.close)
        closer.start()
        future=manager.futures[third['operation_id']]
        for _ in range(200):
            if future.cancelled():break
            time.sleep(.005)
        assert future.cancelled()
        release.set()
        closer.join(timeout=10)
        assert not closer.is_alive()
        with file_lock(manager.lease_directory/(third['operation_id']+'.lock'),timeout=0):pass
        result=manager.get(third['operation_id'])
        assert result['status']=='failed'
        assert result['error']['code']=='OPERATION_CANCELLED'
    finally:
        release.set()
        if 'closer' in locals():closer.join(timeout=10)
        manager.close()


def test_uncertain_trial_result_remains_uncertain_in_operation():
    store = CapabilityStore(namespace='operation_test_'+uuid4().hex)
    store.ensure_schema()
    manager = OperationManager(store)
    def uncertain():
        raise CapabilityError('TRIAL_RESULT_UNCERTAIN', '提交结果待核对',
                              next_action='先查看原运行 original-run；不要重新上传')
    try:
        result = manager.submit(uncertain, title='单次工具试运行', key='trial-result',
                                request={'origin':'diagnostic', 'trial_preview_id':'preview'})
        manager.futures[result['operation_id']].result(timeout=5)
        observed = manager.get(result['operation_id'])
        assert observed['status'] == 'uncertain'
        assert observed['error']['code'] == 'TRIAL_RESULT_UNCERTAIN'
        assert observed['error']['retryable'] is False
        assert 'original-run' in observed['error']['next_action']
    finally:
        manager.close()


def test_orphaned_trial_keeps_original_run_and_does_not_offer_resubmit(tmp_path):
    from psycopg.types.json import Jsonb
    from src.agent.capabilities.models import digest
    store = CapabilityStore(namespace='operation_test_'+uuid4().hex)
    store.ensure_schema()
    manager = OperationManager(store, root=tmp_path)
    preview_id = uuid4().hex
    request = {'origin':'diagnostic', 'trial_preview_id':preview_id,
               'resource_id':'builtin:news.generate'}
    identity = digest([store.namespace, preview_id])
    try:
        call = store.start_call({'resource_id':'builtin:news.generate', 'status':'running',
                                 'origin':'diagnostic', 'operation_key':preview_id})
        store.attach_call_run(call, 'original-run')
        with store.knowledge_store.connection() as conn:
            conn.execute('INSERT INTO agent.capability_operations(id,namespace,status,payload) VALUES (%s,%s,%s,%s)',
                         (identity, store.namespace, 'running', Jsonb({
                             'title':'单次工具试运行', 'lease_managed':True,
                             'request_hash':digest(['单次工具试运行', request]),
                             'recovery_context':request})))
        observed = manager.get(identity)
        assert observed['status'] == 'uncertain'
        assert observed['error']['code'] == 'TRIAL_RESULT_UNCERTAIN'
        assert observed['error']['retryable'] is False
        assert observed['run_id'] == 'original-run'
        assert 'original-run' in observed['error']['next_action']
        assert store.calls(origin='diagnostic')['rows'][0]['status'] == 'uncertain'
        replay = manager.submit(lambda:pytest.fail('orphaned trial was resubmitted'),
                                title='单次工具试运行', key=preview_id, request=request)
        assert replay['operation_id'] == identity and replay['status'] == 'uncertain'
    finally:
        manager.close()
