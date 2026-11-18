"""Internal policy IDs must never be advertised as callable MCP tools."""
import json

from langchain_core.messages import AIMessage, ToolMessage
from jsonschema import Draft202012Validator

from backend.dialogue_tools import TOOL_CATALOG, capability_view
from test_conversational_agent_cancellation import no_external_work
from test_conversational_agent_framework import pg_agent_checkpointer
from test_conversational_agent_store import dialogue_store
from test_conversational_agent_worker import worker, call
from test_conversational_agent_domain_execute import action


def test_capability_discovery_only_advertises_actual_callable_tools(action):
    result = action.execute('capabilities', {})
    assert result['ok'] is True
    assert {row['name'] for row in result['tools']} == set(TOOL_CATALOG)
    assert result['count'] == len(TOOL_CATALOG)
    assert result['mcp_tools'] == []
    assert 'builtin:controller.plan' not in json.dumps(result)
    assert 'builtin:agent.orchestrate' not in json.dumps(result)
    news = next(row for row in result['tools'] if row['name'] == 'generate_news')
    assert news['input_schema'] == TOOL_CATALOG['generate_news']['parameters']
    assert {row['column'] for row in news['policies']} == {'daily_news', 'daily_wow'}


def test_mcp_schema_rejects_synthesized_builtin_namespace():
    validator = Draft202012Validator(TOOL_CATALOG['mcp_call']['parameters'])
    assert not validator.is_valid({'tool_id': 'mcp:builtin:controller.plan', 'arguments': {}})
    assert validator.is_valid({'tool_id': 'mcp:mcp_local:news_search', 'arguments': {'query': 'fixture'}})


def test_real_mcp_ids_are_separate_from_callable_names_and_keep_disabled_state():
    schema = {'type': 'object', 'properties': {'query': {'type': 'string'}}}
    view = capability_view([{'id': 'mcp:mcp_fixture:lookup', 'kind': 'mcp', 'name': 'lookup',
        'enabled': False, 'input_schema': schema}, {'id': 'builtin:controller.plan', 'kind': 'builtin',
        'name': 'Legacy batch planner', 'enabled': True}])
    assert view['mcp_tools'][0]['id'] == 'mcp:mcp_fixture:lookup'
    assert view['mcp_tools'][0]['input_schema'] == schema
    assert view['mcp_tools'][0]['enabled'] is False
    assert not next(row for row in view['tools'] if row['name'] == 'mcp_call')['enabled']
    assert 'Legacy batch planner' not in json.dumps(view)


def test_discovery_preserves_column_specific_disabled_policy(action):
    action.caps.put('builtin:news.generate', 'tool', {'enabled': False}, expected_revision=0)
    result = action.execute('list_capabilities', {})
    news = next(row for row in result['tools'] if row['name'] == 'generate_news')
    policy = next(row for row in news['policies'] if row['column'] == 'daily_news')
    assert policy['enabled'] is False


def test_builtin_mcp_mistake_is_corrected_before_permission_and_execution(worker):
    _, _, executed, _, invoke, _ = worker
    state, model = invoke([
        call('mcp_call', {'tool_id': 'mcp:builtin:controller.plan', 'arguments': {'jobs': []}}, 'wrong-mcp'),
        call('capabilities', {}, 'correct-discovery'),
        AIMessage(content='Use generate_news directly after selecting materials.')])
    assert state['state'] == 'idle' and state['requests'] == []
    assert [name for name, _, _ in executed] == ['capabilities']
    assert any(isinstance(m, ToolMessage) and 'BUILTIN_IS_NOT_MCP' in str(m.content) for m in model.requests[1])


def test_repeated_builtin_mcp_errors_ask_without_fake_configuration(worker):
    _, _, executed, _, invoke, _ = worker
    state, model = invoke([
        call('mcp_call', {'tool_id': 'mcp:builtin:controller.plan', 'arguments': {}}, 'bad-one'),
        call('mcp_call', {'tool_id': 'mcp:builtin:agent.orchestrate', 'arguments': {}}, 'bad-two')])
    assert len(model.requests) == 2 and executed == []
    assert state['state'] == 'waiting_input'
    assert state['requests'][0]['kind'] == 'question'
    assert 'configured' not in state['requests'][0]['allowed_decisions']
