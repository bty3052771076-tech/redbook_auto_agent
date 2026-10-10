from uuid import uuid4

import pytest

from test_capability_store import store
from src.agent.editorial_agent import AgentJob, EditorialAgentTools, EditorialAgentConfig
from src.agent.capabilities.registry import builtin_catalog
from src.agent.capabilities.dispatcher import ToolDispatcher, managed_call
from src.agent.capabilities.models import CapabilityError


def callbacks(invoked):
    return EditorialAgentTools(sync_context=lambda job: {'kind': job.kind},
        generate=lambda job, context: invoked.append('generated') or ['post'],
        review=lambda job, posts, context: [], upload=lambda job, post, context: (True, 'saved'),
        upload_batch=lambda job, posts, context: invoked.append('uploaded') or {'post': (True, 'saved')})


def test_generation_adapter_enforces_real_policy_before_effect(store):
    from src.agent.capabilities.execution import wrap_tools
    snapshot = store.freeze(uuid4().hex, builtin_catalog(), disabled_tools=['builtin:news.generate'])
    invoked = []
    wrapped = wrap_tools(callbacks(invoked), ToolDispatcher(store, snapshot))
    with pytest.raises(CapabilityError, match='CAPABILITY_DISABLED'):
        wrapped.generate(AgentJob(kind='daily_news', title='每日新闻', count=1), {})
    assert invoked == []
    assert store.calls(run_id=snapshot['run_id'])['rows'][0]['status'] == 'denied'


def test_nested_calls_share_run_and_parent_identity(store):
    from src.agent.capabilities.execution import wrap_tools
    snapshot = store.freeze(uuid4().hex, builtin_catalog())
    tools = callbacks([])
    tools.generate = lambda job, context: managed_call('builtin:writer.generate', 'generate', lambda: ['written'])
    wrapped = wrap_tools(tools, ToolDispatcher(store, snapshot))
    assert wrapped.generate(AgentJob(kind='daily_news', title='每日新闻', count=1), {}) == ['written']
    rows = store.calls(run_id=snapshot['run_id'])['rows']
    parent = next(row for row in rows if row['resource_id'] == 'builtin:news.generate')
    child = next(row for row in rows if row['resource_id'] == 'builtin:writer.generate')
    assert child['parent_call_id'] == parent['id']
    assert all(row['status'] == 'succeeded' for row in rows)


def test_batch_adapter_uses_one_policy_call_and_retains_serial_adapter(store):
    from src.agent.capabilities.execution import wrap_tools
    snapshot = store.freeze(uuid4().hex, builtin_catalog())
    invoked = []
    original = callbacks(invoked)
    wrapped = wrap_tools(original, ToolDispatcher(store, snapshot))
    assert wrapped.upload_batch(AgentJob(kind='daily_news', title='每日新闻', count=1), ['post'], {}) == {'post': (True, 'saved')}
    assert invoked == ['uploaded']
    assert [row['resource_id'] for row in store.calls(run_id=snapshot['run_id'])['rows']] == ['builtin:xhs.drafts.save_batch']


@pytest.mark.parametrize('detail,status',[('XHS_WRITE_UNCERTAIN: read original draft','uncertain'),('image upload failed','failed')])
@pytest.mark.parametrize('batch',[True,False])
def test_returned_upload_failure_does_not_appear_as_successful_call(store,detail,status,batch):
    from src.agent.capabilities.execution import wrap_tools
    snapshot=store.freeze(uuid4().hex,builtin_catalog())
    tools=callbacks([])
    expected={'post':(False,detail)} if batch else (False,detail)
    tools.upload_batch=lambda *args:expected
    tools.upload=lambda *args:expected
    wrapped=wrap_tools(tools,ToolDispatcher(store,snapshot))
    callback=wrapped.upload_batch if batch else wrapped.upload
    assert callback(AgentJob('daily_news','每日新闻',1),['post'] if batch else 'post',{})==expected
    call=store.calls(run_id=snapshot['run_id'])['rows'][0]
    assert call['status']==status
    assert detail in call['result_summary']


def test_nested_uncertainty_is_retained_by_all_parent_calls(store):
    snapshot=store.freeze(uuid4().hex,builtin_catalog())
    dispatcher=ToolDispatcher(store,snapshot)
    def unknown():
        raise CapabilityError('TRIAL_RESULT_UNCERTAIN','original operation needs readback')
    with pytest.raises(CapabilityError):
        dispatcher.call('builtin:news.generate',lambda:dispatcher.call('builtin:xhs.drafts.save_batch',unknown,stage='upload'),stage='generate')
    assert {call['status'] for call in store.calls(run_id=snapshot['run_id'])['rows']}=={'uncertain'}


def test_runtime_configuration_preserves_capability_management_flag():
    assert EditorialAgentConfig(capability_management=True).validate().capability_management is True


def test_actual_agent_stops_on_denied_tool_without_retry_loop(store, tmp_path, monkeypatch):
    from src.agent.editorial_agent import run_editorial_agent
    monkeypatch.setenv('AGENT_CAPABILITY_NAMESPACE', store.namespace)
    store.put('builtin:news.generate', 'tool', {'enabled': False}, expected_revision=0)
    invoked = []
    run_id = uuid4().hex
    result = run_editorial_agent([AgentJob(kind='daily_news', title='每日新闻', count=1)], tools=callbacks(invoked),
        config=EditorialAgentConfig(capability_management=True, checkpoint_dir=tmp_path, retry_delay_s=0), run_id=run_id)
    assert result.status == 'blocked'
    assert invoked == []
    calls = store.calls(run_id=run_id)['rows']
    denied = [row for row in calls if row['resource_id'] == 'builtin:news.generate']
    assert len(denied) == 1
    assert denied[0]['status'] == 'denied'


def test_selected_skill_resource_is_read_from_frozen_version_and_audited(store):
    from src.agent.capabilities.execution import wrap_tools
    from src.agent.capabilities.skill_runtime import resource_tools
    skill={'id':'skill_test','name':'news-style','version_hash':'original','resources':{'references/style.md':'原始附件'}}
    snapshot=store.freeze(uuid4().hex,builtin_catalog()+resource_tools([skill]))
    store.put('skill_test','skill',{**skill,'resources':{'references/style.md':'新版附件'}},expected_revision=0)
    tools=callbacks([])
    tools.plan=lambda jobs,context:{'tool_calls':[{'tool_id':'skill_resource:skill_test','arguments':{'path':'references/style.md'}}]}
    wrapped=wrap_tools(tools,ToolDispatcher(store,snapshot),selected_skills=[skill])
    result=wrapped.plan([AgentJob(kind='daily_news',title='每日新闻',count=1)],{})
    assert result['skill_preparation'][0]['output']['content']=='原始附件'
    calls=store.calls(run_id=snapshot['run_id'])['rows']
    read=next(row for row in calls if row['resource_id']=='skill_resource:skill_test')
    assert read['status']=='succeeded' and read['action']=='resource_read'
    assert read['loaded_resources']==1


def test_skill_resource_does_not_permit_path_escape_or_script_execution():
    from src.agent.capabilities.skill_runtime import read_resource
    skill={'id':'skill_test','version_hash':'v1','resources':{'scripts/command.ps1':'Remove-Item forbidden'}}
    assert read_resource(skill,{'path':'scripts/command.ps1'})['content']=='Remove-Item forbidden'
    with pytest.raises(CapabilityError,match='SKILL_RESOURCE_INVALID'):
        read_resource(skill,{'path':'../other'})


def readonly_tool(identity, stages):
    return {'id':identity,'name':'lookup','kind':'mcp','enabled':True,'binding':'agent_preparation',
            'stages':stages,'purpose':'evidence','input_schema':{'type':'object'},
            'annotations':{'readOnlyHint':True,'destructiveHint':False},'read_only_confirmed':True,
            'dependencies':[], 'revision':1, 'connection':{'id':identity.split(':')[1]}}


def test_evidence_stage_selects_only_approved_tools_and_passes_results_to_review(store, monkeypatch, tmp_path):
    from src.agent.capabilities.execution import wrap_tools
    monkeypatch.setenv('REDBOOK_RUNTIME_ROOT', str(tmp_path))
    evidence = readonly_tool('mcp:test:evidence', ['evidence'])
    preparation = readonly_tool('mcp:test:preparation', ['preparation'])
    snapshot = store.freeze(uuid4().hex, builtin_catalog()+[evidence, preparation])
    observed = []
    tools = callbacks([])
    def choose(jobs, context):
        assert context['tool_stage'] == 'evidence'
        assert [item['id'] for item in context['preparation_tools']] == [evidence['id']]
        assert context['artifacts'][0]['title'] == '官方新模型发布'
        return {'tool_calls':[{'tool_id':evidence['id'],'arguments':{'query':'模型发布日期'}}]}
    tools.plan = choose
    tools.review = lambda job, posts, context: observed.append(context['mcp_evidence']) or []
    class Runtime:
        def call(self, row, arguments):
            return {'date':'2026-10-09','source':'https://vendor.example/release'}
        def close(self): pass
    wrapped = wrap_tools(tools, ToolDispatcher(store,snapshot),mcp_runtime=Runtime())
    context = {}
    assert wrapped.review(AgentJob('daily_news','每日新闻'), [{'id':'post1','title':'官方新模型发布','body':'事实内容'}], context) == []
    assert observed[0][0]['tool_id'] == evidence['id'] and observed[0][0]['status'] == 'succeeded'
    assert observed[0][0]['output']['source'] == 'https://vendor.example/release'
    rows = store.calls(run_id=snapshot['run_id'])['rows']
    child = next(row for row in rows if row['resource_id'] == evidence['id'])
    parent = next(row for row in rows if row['resource_id'] == 'builtin:content.review')
    selector = next(row for row in rows if row['resource_id'] == 'builtin:controller.plan')
    assert child['stage'] == selector['stage'] == 'evidence'
    assert child['parent_call_id'] == selector['id'] and selector['parent_call_id'] == parent['id']
    assert not any(row['resource_id'] == preparation['id'] for row in rows)


def test_review_without_bound_evidence_tools_does_not_call_controller(store):
    from src.agent.capabilities.execution import wrap_tools
    tools = callbacks([])
    tools.plan = lambda *args: pytest.fail('unbound review called a model')
    snapshot = store.freeze(uuid4().hex,builtin_catalog())
    wrapped = wrap_tools(tools,ToolDispatcher(store,snapshot))
    assert wrapped.review(AgentJob('daily_news','每日新闻'),[],{}) == []


def test_preparation_catalog_does_not_silently_hide_twenty_first_approved_tool(store, monkeypatch, tmp_path):
    from src.agent.capabilities.execution import wrap_tools
    monkeypatch.setenv('REDBOOK_RUNTIME_ROOT',str(tmp_path))
    rows = [readonly_tool(f'mcp:test:{number}', ['preparation']) for number in range(25)]
    snapshot = store.freeze(uuid4().hex,builtin_catalog()+rows)
    tools = callbacks([])
    def choose(jobs,context):
        assert len(context['preparation_tools']) == 25
        return {'tool_calls':[]}
    tools.plan = choose
    assert wrap_tools(tools,ToolDispatcher(store,snapshot)).plan([],{})['tool_calls'] == []
