from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from src.agent.capabilities.models import digest


SCHEMA = {'type':'object','properties':{'query':{'type':'string'}},'required':['query'],'additionalProperties':False}


class Manager:
    def __init__(self):
        self.started = self.closed = self.called = 0
        self.schema = SCHEMA
        self.complete = True
    @asynccontextmanager
    async def _session(self):
        self.started += 1
        try: yield self
        finally: self.closed += 1
    async def _list(self, session): return [{'name':'search','input_schema':self.schema,
        'annotations':{'readOnlyHint':True,'destructiveHint':False}}]
    async def call_tool(self, name, arguments, **kwargs):
        self.called += 1
        return SimpleNamespace(is_error=False,structured_content={'items':[]})


def row():
    return {'id':'mcp:test:search','schema_hash':digest(SCHEMA),'input_schema':SCHEMA,
            'annotations':{'readOnlyHint':True,'destructiveHint':False},'read_only_confirmed':True,
            'effect_hash':digest({'readOnlyHint':True,'destructiveHint':False}),
            'connection':{'id':'test','revision':1,'timeout_seconds':1,'startup_timeout_seconds':1}}


def test_runtime_reuses_one_owned_session_then_closes():
    from src.agent.capabilities.mcp_runtime import MCPRuntime
    manager = Manager()
    with MCPRuntime(manager_factory=lambda connection: manager) as runtime:
        assert runtime.call(row(),{'query':'news'})['items'] == []
        assert runtime.call(row(),{'query':'models'})['items'] == []
        assert manager.started == 1 and manager.closed == 0 and manager.called == 2
    assert manager.closed == 1
    assert not runtime.running


def test_runtime_rejects_invalid_arguments_before_launch_and_changed_schema_before_call():
    from src.agent.capabilities.mcp_runtime import MCPRuntime
    manager = Manager()
    with MCPRuntime(manager_factory=lambda connection: manager) as runtime:
        with pytest.raises(ValueError, match='MCP_ARGUMENTS_INVALID'):
            runtime.call(row(),{'unknown':'value'})
        assert manager.started == 0
        manager.schema = {'type':'object','required':['another']}
        with pytest.raises(ValueError, match='MCP_SCHEMA_CHANGED'):
            runtime.call(row(),{'query':'news'})
        assert manager.called == 0
    assert manager.closed == 1


def test_idle_runtime_closes_and_restart_is_explicitly_owned():
    import time
    from src.agent.capabilities.mcp_runtime import MCPRuntime
    manager = Manager()
    with MCPRuntime(manager_factory=lambda connection: manager, idle_seconds=.05) as runtime:
        runtime.call(row(),{'query':'news'})
        for _ in range(50):
            if not runtime.running: break
            time.sleep(.01)
        assert manager.closed == 1 and not runtime.running
        runtime.call(row(),{'query':'models'})
        assert manager.started == 2
    assert manager.closed == 2


def test_v1_text_json_response_is_decoded_without_claiming_plain_text_is_json():
    from src.agent.capabilities.mcp_runtime import MCPRuntime
    manager = Manager()
    async def call(*args, **kwargs):
        return SimpleNamespace(is_error=False, content=[SimpleNamespace(text='{"status":"ready"}')])
    manager.call_tool = call
    with MCPRuntime(manager_factory=lambda connection: manager) as runtime:
        assert runtime.call(row(),{'query':'status'}) == {'status':'ready'}


def test_real_protocol_selection_is_called_and_session_closed(tmp_path, monkeypatch):
    from uuid import uuid4
    from backend.settings import configure_runtime
    from src.agent.capabilities.store import CapabilityStore
    from src.agent.capabilities.mcp_service import MCPConnectionService
    from src.agent.capabilities.mcp_runtime import MCPRuntime
    from src.agent.capabilities.dispatcher import ToolDispatcher
    from src.agent.capabilities.execution import wrap_tools
    from src.agent.capabilities.registry import builtin_catalog
    from src.agent.editorial_agent import AgentJob, EditorialAgentTools
    from src.agent.mcp_manager import MCPManager
    root = configure_runtime()
    store = CapabilityStore(namespace='mcp_protocol_'+uuid4().hex)
    store.ensure_schema()
    service = MCPConnectionService(root,store)
    connection = service.discover('mcp_local')
    tool = next(row for row in connection['tools'] if row['name']=='runtime_status')
    service.tool_policy('mcp_local',{'expected_revision':connection['revision'],'tool_name':'runtime_status',
        'schema_hash':tool['schema_hash'],'stages':['preparation'],'purpose':'operations'})
    snapshot = store.freeze(uuid4().hex,builtin_catalog()+service.tools())
    monkeypatch.setenv('REDBOOK_RUNTIME_ROOT',str(tmp_path))
    tools = EditorialAgentTools(sync_context=lambda job:{},generate=lambda *args:[],review=lambda *args:[],upload=lambda *args:(True,''),
        plan=lambda jobs,context:{'job_order':[0],'tool_calls':[{'tool_id':'mcp:mcp_local:runtime_status','arguments':{},'reason':'检查知识库'}]})
    with MCPRuntime(manager_factory=lambda connection:MCPManager(root,connection=connection)) as runtime:
        wrapped = wrap_tools(tools,ToolDispatcher(store,snapshot),mcp_runtime=runtime)
        result = wrapped.plan([AgentJob(kind='daily_news',title='每日新闻',count=1)],{})
        assert result['mcp_preparation'][0]['status'] == 'succeeded'
        assert result['mcp_preparation'][0]['output']['status'] == 'ready'
        assert runtime.running
    assert not runtime.running
    calls = store.calls(run_id=snapshot['run_id'])['rows']
    child = next(row for row in calls if row['resource_id']=='mcp:mcp_local:runtime_status')
    parent = next(row for row in calls if row['resource_id']=='builtin:controller.plan')
    assert child['parent_call_id'] == parent['id'] and child['status']=='succeeded'
