from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_capability_store import store
from src.agent.capabilities.dispatcher import ToolDispatcher
from src.agent.capabilities.models import CapabilityError
from src.agent.capabilities.registry import builtin_catalog
from src.config import LLMConfig


def dispatcher(store, disabled=()):
    return ToolDispatcher(store, store.freeze(uuid4().hex, builtin_catalog(), disabled_tools=list(disabled)))


@pytest.mark.parametrize('function', ['generate_json', 'generate_draft'])
def test_real_writer_entry_rejects_disabled_before_model_request(store, monkeypatch, function):
    from src.llm import generate
    invoked = []
    monkeypatch.setattr(generate, 'init_chat_model', lambda *a, **k: invoked.append(k) or None)
    managed = dispatcher(store, ['builtin:writer.generate'])
    with managed.bound(), pytest.raises(CapabilityError, match='CAPABILITY_DISABLED'):
        if function == 'generate_json':
            generate.generate_json([], system_prompt='JSON', user_prompt='news')
        else:
            generate.generate_draft([], title_hint='news', prompt_hint='news', asset_paths=[])
    assert invoked == []
    assert store.calls(run_id=managed.snapshot['run_id'])['rows'][0]['status'] == 'denied'


def test_controller_json_is_not_misclassified_as_writer(store, monkeypatch):
    from src.llm import generate
    monkeypatch.setattr(generate, 'init_chat_model', lambda *a, **k: SimpleNamespace(invoke=lambda messages: SimpleNamespace(content='{"order":[0]}')))
    managed = dispatcher(store, ['builtin:writer.generate'])
    result = managed.call('builtin:controller.plan', lambda: generate.generate_json(
        LLMConfig(model='test', api_key='test', base_url='http://127.0.0.1'),
        system_prompt='JSON', user_prompt='news'), stage='preparation')
    assert result == {'order':[0]}
    assert [row['resource_id'] for row in store.calls(run_id=managed.snapshot['run_id'])['rows']] == ['builtin:controller.plan']


def test_saved_writer_timeout_reaches_actual_legacy_model_request(store,monkeypatch):
    from src.llm import generate
    requests=[]
    monkeypatch.setattr(generate,'init_chat_model',lambda *args,**kwargs:requests.append(kwargs) or
                        SimpleNamespace(invoke=lambda messages:SimpleNamespace(content='{"summary":"value"}')))
    store.put('builtin:writer.generate','tool',{'enabled':True,'timeout_seconds':17},expected_revision=0)
    managed=dispatcher(store)
    with managed.bound():
        generate.generate_json(LLMConfig(model='test',api_key='test',base_url='http://127.0.0.1'),
                               system_prompt='JSON',user_prompt='news')
    assert requests[0]['timeout']==17


@pytest.mark.parametrize('module,function', [
    ('minimax_images','generate_minimax_image'), ('opencodex_images','generate_subscription_image'),
    ('aliyun_images','generate_aliyun_image'), ('siliconflow_images','generate_siliconflow_image'),
    ('volcengine_images','generate_volcengine_image')])
def test_image_provider_entry_rejects_disabled_before_transport(store, tmp_path, monkeypatch, module, function):
    from importlib import import_module
    imported = import_module('src.images.'+module)
    entry = getattr(imported, function)
    def offline(*args, **kwargs):
        raise RuntimeError('offline test transport boundary')
    boundary = {'minimax_images':'load_minimax_image_config', 'opencodex_images':'_file_lock',
                'aliyun_images':'load_aliyun_image_config', 'siliconflow_images':'load_siliconflow_image_config',
                'volcengine_images':'load_volcengine_image_config'}[module]
    monkeypatch.setattr(imported,boundary,offline)
    monkeypatch.setenv('OPENCODEX_IMAGE_STATE_DIR',str(tmp_path/'state'))
    managed = dispatcher(store, ['builtin:image.generate'])
    with managed.bound(), pytest.raises(CapabilityError, match='CAPABILITY_DISABLED'):
        entry(post_id='test', prompt='scene', dest_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_model_queue_carries_real_policy_and_parent_into_worker(store, monkeypatch):
    from src.llm import generate
    from src.workflow.model_queues import ModelWorkQueues
    monkeypatch.setattr(generate, 'init_chat_model', lambda *a, **k: SimpleNamespace(invoke=lambda messages: SimpleNamespace(content='{"body":"test"}')))
    managed = dispatcher(store)
    cfg = LLMConfig(model='test', api_key='test', base_url='http://127.0.0.1')
    with ModelWorkQueues() as queues:
        managed.call('builtin:news.generate', lambda: queues.submit_llm(generate.generate_json,
            cfg, system_prompt='JSON', user_prompt='news').result(), stage='generate')
    calls = store.calls(run_id=managed.snapshot['run_id'])['rows']
    child = next(row for row in calls if row['resource_id']=='builtin:writer.generate')
    parent = next(row for row in calls if row['resource_id']=='builtin:news.generate')
    assert child['parent_call_id'] == parent['id'] and child['status']=='succeeded'


def test_model_queue_observes_revocation_before_network(store, monkeypatch):
    from src.llm import generate
    from src.workflow.model_queues import ModelWorkQueues
    requested = []
    monkeypatch.setattr(generate, 'init_chat_model', lambda *a, **k: requested.append(k) or None)
    store.put('builtin:writer.generate', 'tool', {'enabled':True}, expected_revision=0)
    managed = dispatcher(store)
    def work():
        store.revoke('builtin:writer.generate', expected_revision=1)
        with ModelWorkQueues() as queues:
            return queues.submit_llm(generate.generate_json, [], system_prompt='JSON', user_prompt='news').result()
    with pytest.raises(CapabilityError, match='CAPABILITY_REVOKED'):
        managed.call('builtin:news.generate', work, stage='generate')
    assert requested == []


def test_forgetting_frozen_preference_changes_next_actual_model_input_not_explicit_task(store,monkeypatch):
    import json
    from src.llm import generate
    from src.agent.execution_context import generation_hint
    from src.agent.memory_service import MemoryService
    memory=MemoryService(store).save({'content':'配图尽量无文字','key':'image.text'})
    managed=dispatcher(store)
    hints=generation_hint({'conversation_memory':{'confirmed_requirements':[{'count':1}], 'preferences':[memory]}})
    prompts=[]
    monkeypatch.setattr(generate,'init_chat_model',lambda *a,**k:SimpleNamespace(invoke=lambda messages:
        prompts.append(messages[-1].content) or SimpleNamespace(content='{"body":"test"}')))
    cfg=LLMConfig(model='test',api_key='test',base_url='http://127.0.0.1')
    with managed.bound():
        generate.generate_json(cfg,system_prompt='JSON',user_prompt=json.dumps({'prompt_hint':'本次新闻只写芯片'+hints},ensure_ascii=False))
        MemoryService(store).forget(memory['id'],expected_revision=memory['revision'])
        generate.generate_json(cfg,system_prompt='JSON',user_prompt=json.dumps({'prompt_hint':'本次新闻只写芯片'+hints},ensure_ascii=False))
    assert '配图尽量无文字' in prompts[0]
    assert '配图尽量无文字' not in prompts[1]
    assert '本次新闻只写芯片' in prompts[1]


@pytest.mark.parametrize('address',[
    'https://user:private-secret@api.example/v1',
    'https://api.example/v1?api_key=private-secret',
    'https://api.example/v1#private-secret',
    'https://api.example:invalid/v1',
])
def test_legacy_controller_rejects_sensitive_or_invalid_url_before_freeze(address):
    from src.model_platforms.integration import legacy_controller
    from src.model_platforms.security import PlatformError
    with pytest.raises(PlatformError) as error:
        legacy_controller({'MINIMAX_TOKEN_PLAN_API_KEY':'offline-test-key','MINIMAX_BASE_URL':address})
    assert error.value.code=='UNSAFE_ADDRESS'
    assert 'private-secret' not in str(error.value)


def test_restored_legacy_model_rechecks_frozen_address():
    import json
    from src.model_platforms.integration import frozen_legacy_config
    from src.model_platforms.security import PlatformError
    env={'MINIMAX_TOKEN_PLAN_API_KEY':'offline-test-key',
         'RUN_LEGACY_MODEL_ROLES':json.dumps({'agent':'minimax:test'}),
         'RUN_LEGACY_MODEL_CONFIGS':json.dumps({'agent':{'provider':'minimax','model':'test',
             'base_url':'https://api.example/v1?api_key=private-secret'}})}
    with pytest.raises(PlatformError,match='UNSAFE_ADDRESS'):
        frozen_legacy_config('agent',env=env)
