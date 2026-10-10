from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from backend.settings import configure_runtime

RUNTIME = configure_runtime()


def test_real_local_mcp_catalog_and_status_use_independent_paths():
    from src.agent.mcp_manager import MCPManager
    manager = MCPManager(RUNTIME)
    names = {tool['name'] for tool in manager.list_tools()}
    assert names == {'runtime_status', 'knowledge_search', 'news_search'}
    response = manager.call_tool('runtime_status', {})
    assert response['status'] == 'ok'
    assert response['result']['status'] == 'ready'
    assert response['protocol_version']


def test_unknown_local_tool_never_runs():
    from src.agent.mcp_manager import MCPManager
    manager = MCPManager(RUNTIME)
    with pytest.raises(PermissionError):
        manager.call_tool('publish_everything', {})


def test_connection_secrets_are_not_returned_and_discovery_requires_explicit_action():
    from src.agent.capabilities.store import CapabilityStore
    from src.agent.capabilities.mcp_service import MCPConnectionService
    store = CapabilityStore(namespace='test_mcp_' + uuid4().hex)
    store.ensure_schema()
    service = MCPConnectionService(RUNTIME, store)
    secret = 'private-test-token-' + uuid4().hex
    connection = service.save({'name':'test', 'transport':'streamable_http',
                               'url':'http://127.0.0.1:19876/mcp', 'headers':{'Authorization':'Bearer ' + secret}})
    assert secret not in json.dumps(connection)
    assert connection['health']['status'] == 'unknown'
    assert connection['enabled'] is False
    persisted = store.get(connection['id'])
    assert secret not in json.dumps(persisted)
    assert persisted['header_refs']['Authorization'].startswith('dpapi:')


def test_two_services_tool_identity_and_schema_approval_are_separate():
    from src.agent.capabilities.mcp_service import catalog_diff
    from src.agent.capabilities.models import digest
    old = [{'name':'search','input_schema':{'type':'object'}}]
    new = [{'name':'search','input_schema':{'type':'object','required':['query']}},
           {'name':'new','input_schema':{'type':'object'}}]
    diff = catalog_diff(old,new,complete=True)
    assert diff['changed'] == ['search']
    assert diff['added'] == ['new']
    assert catalog_diff(old, [], complete=False)['removed'] == []


def test_builtin_namespace_is_explicit_and_custom_server_gets_no_unrequested_secrets(monkeypatch):
    from src.agent.mcp_manager import MCPManager
    monkeypatch.setenv('NEWS_API_KEY','do-not-leak')
    builtin = MCPManager(RUNTIME,namespace='scope-for-test')._parameters()
    assert builtin.env['AGENT_CAPABILITY_NAMESPACE'] == 'scope-for-test'
    custom = MCPManager(RUNTIME,connection={'id':'custom','builtin':False})._parameters()
    assert 'NEWS_API_KEY' not in custom.env
    assert 'KNOWLEDGE_DB_CREDENTIALS' not in custom.env


def test_stdio_proxy_environment_follows_connection_policy(monkeypatch):
    from src.agent.mcp_manager import MCPManager
    monkeypatch.setenv('HTTPS_PROXY','http://127.0.0.1:7890')
    monkeypatch.setenv('NO_PROXY','localhost,127.0.0.1')
    direct = MCPManager(RUNTIME,connection={'id':'custom','network_policy':{'mode':'direct'}})._parameters()
    assert 'HTTPS_PROXY' not in direct.env and 'NO_PROXY' not in direct.env
    inherited = MCPManager(RUNTIME,connection={'id':'custom','network_policy':{'mode':'inherit'}})._parameters()
    assert inherited.env['HTTPS_PROXY'] == 'http://127.0.0.1:7890'
    assert inherited.env['NO_PROXY'] == 'localhost,127.0.0.1'
    explicit = MCPManager(RUNTIME,connection={'id':'custom','network_policy':{
        'mode':'custom','proxy_url':'http://127.0.0.1:7891'}})._parameters()
    assert explicit.env['HTTPS_PROXY'] == 'http://127.0.0.1:7891' and 'NO_PROXY' not in explicit.env


def test_knowledge_mcp_search_uses_current_namespace(monkeypatch):
    import asyncio
    from src.agent import mcp_server
    recorded = []
    class Knowledge:
        def status(self): return {'status':'ready','index_ready':True}
        def search(self, query, *, purpose, limit, account_namespace):
            recorded.append({'account_namespace':account_namespace}); return []
    monkeypatch.setenv('AGENT_CAPABILITY_NAMESPACE','isolated-account')
    monkeypatch.setattr(mcp_server.KnowledgeStore,'from_env',lambda:Knowledge())
    assert asyncio.run(mcp_server.knowledge_search('test'))['status'] == 'ok_empty'
    assert recorded[0]['account_namespace'] == 'isolated-account'


def test_project_news_mcp_tool_cannot_bypass_disabled_builtin_search():
    from src.agent.capabilities.store import CapabilityStore
    from src.agent.capabilities.mcp_service import MCPConnectionService
    from src.agent.capabilities.registry import builtin_catalog
    from src.agent.capabilities.dispatcher import ToolDispatcher
    from src.agent.capabilities.models import digest, CapabilityError
    store=CapabilityStore(namespace='test_mcp_policy_'+uuid4().hex)
    store.ensure_schema()
    schema={'type':'object'}
    service=MCPConnectionService(RUNTIME,store)
    store.put('mcp_local','mcp',{**service.builtin(),'enabled':True,'health':{'status':'ready'},
        'tools':[{'name':'news_search','input_schema':schema,'schema_hash':digest(schema)}],
        'tool_policies':{'news_search':{'enabled':True,'stages':['preparation'],'purpose':'evidence'}}},expected_revision=0)
    row=service.tools()[0]
    assert row['enabled'] is True
    snapshot=store.freeze(uuid4().hex,builtin_catalog()+[row],disabled_tools=['builtin:news.search'])
    with pytest.raises(CapabilityError,match='CAPABILITY_DISABLED'):
        ToolDispatcher(store,snapshot).call(row['id'],lambda:pytest.fail('disabled news source was invoked'),stage='preparation')
