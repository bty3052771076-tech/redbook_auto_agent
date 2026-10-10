import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import socket
from threading import Thread
import time

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse, Response

from test_capability_store import store
from src.agent.capabilities.mcp_runtime import MCPRuntime
from src.agent.capabilities.mcp_service import MCPConnectionService
from src.agent.capabilities.models import CapabilityError
from src.agent.mcp_manager import MCPManager


@contextmanager
def http_server(label, *, fault='', expected_key='offline-http-key', legacy_version=None):
    server = MCPServer('test-' + label)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False))
    def search(query: str) -> dict:
        return {'server': label, 'query': query}

    app = server.streamable_http_app(json_response=True, stateless_http=True)
    received = []

    async def endpoint(scope, receive, send):
        if scope['type'] == 'http':
            headers = dict(scope['headers'])
            record = {'path': scope['path'], 'key_present': b'x-test-key' in headers}
            received.append(record)
            if fault == '401' or headers.get(b'x-test-key') != expected_key.encode():
                await Response('rejected', status_code=401)(scope, receive, send)
                return
            if fault == 'timeout':
                await asyncio.sleep(2)
                await Response('late response', media_type='application/json')(scope, receive, send)
                return
            elif fault == 'protocol':
                await Response('not JSON RPC', media_type='application/json')(scope, receive, send)
                return
            elif fault.startswith('redirect:'):
                await Response(status_code=307, headers={'location': fault.partition(':')[2]})(scope, receive, send)
                return
            if scope['method'] == 'POST':
                messages = []
                body = b''
                while True:
                    message = await receive()
                    messages.append(message)
                    body += message.get('body', b'')
                    if not message.get('more_body'):
                        break
                request = json.loads(body)
                record['method'] = request.get('method')
                original_receive = receive

                async def replay():
                    return messages.pop(0) if messages else await original_receive()

                receive = replay
                if legacy_version:
                    if 'id' not in request:
                        await Response(status_code=202)(scope, receive, send)
                        return
                    response = {'jsonrpc': '2.0', 'id': request['id']}
                    method = request['method']
                    if method == 'initialize':
                        response['result'] = {'protocolVersion': legacy_version,
                            'capabilities': {'tools': {}}, 'serverInfo': {'name': label, 'version': 'test'}}
                    elif method == 'tools/list':
                        response['result'] = {'tools': [{'name': 'search', 'description': 'Read-only test search',
                            'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}},
                                            'required': ['query']},
                            'annotations': {'readOnlyHint': True, 'destructiveHint': False}}]}
                    elif method == 'tools/call':
                        response['result'] = {'content': [{'type': 'text', 'text': json.dumps(
                            {'server': label, 'query': request['params']['arguments']['query']})}], 'isError': False}
                    else:
                        response['error'] = {'code': -32601, 'message': 'Method not found'}
                    await JSONResponse(response)(scope, receive, send)
                    return
            elif legacy_version:
                await Response(status_code=405)(scope, receive, send)
                return
        await app(scope, receive, send)

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    runner = uvicorn.Server(uvicorn.Config(endpoint, log_level='error', lifespan='on'))
    thread = Thread(target=runner.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not runner.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError('local MCP test server failed to start')
            time.sleep(.01)
        yield f'http://127.0.0.1:{listener.getsockname()[1]}/mcp', received
    finally:
        runner.should_exit = True
        thread.join(timeout=8)
        listener.close()
        assert not thread.is_alive(), 'test-owned HTTP server did not close'


def add(service, url, name='test', key='offline-http-key'):
    return service.save({'name': name, 'transport': 'streamable_http', 'url': url,
                         'headers': {'x-test-key': key}, 'startup_timeout_seconds': 1,
                         'timeout_seconds': 1, 'enabled': True})


def test_http_direct_ignores_environment_proxy_and_custom_does_not_fallback(store, tmp_path, monkeypatch):
    for name in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
        monkeypatch.setenv(name,'http://127.0.0.1:9')
    for name in ('NO_PROXY','no_proxy'):
        monkeypatch.setenv(name,'')
    service = MCPConnectionService(tmp_path,store)
    with http_server('network-policy') as (url,received):
        connection = add(service,url)
        row = service.save({'expected_revision':connection['revision'], 'network_policy':{'mode':'direct'}},connection['id'])
        result = service.discover(row['id'])
        assert result['health']['status'] == 'ready' and received
        count = len(received)
        service.save({'expected_revision':result['revision'],
                      'network_policy':{'mode':'custom','proxy_url':'http://127.0.0.1:9'}},row['id'])
        with pytest.raises(CapabilityError) as error:
            service.discover(row['id'])
        assert error.value.code in {'MCP_CONNECTION_FAILED','MCP_TIMEOUT'}
        assert len(received) == count, 'custom proxy failure silently fell back to direct'


@pytest.mark.parametrize('policy', [
    {'mode':'unexpected'}, {'mode':'custom'}, {'mode':'direct','proxy_url':'http://127.0.0.1:9'},
    {'mode':'custom','proxy_url':'http://user:password@127.0.0.1:9'},
    {'mode':'custom','proxy_url':'http://proxy.example.com'}, {'mode':'custom','proxy_url':'http://127.0.0.1:9?key=value'},
])
def test_network_policy_rejects_ambiguous_or_credential_urls(store,tmp_path,policy):
    service = MCPConnectionService(tmp_path,store)
    with pytest.raises(CapabilityError,match='MCP_NETWORK_INVALID'):
        service.save({'name':'network','transport':'streamable_http','url':'http://127.0.0.1:19800/mcp',
                      'network_policy':policy})
    assert service.list()['rows'][0]['id'] == 'mcp_local' and len(service.list()['rows']) == 1


@pytest.mark.parametrize('fault,code', [('401', 'MCP_AUTH_REJECTED'), ('timeout', 'MCP_TIMEOUT'),
                                        ('protocol', 'MCP_PROTOCOL_ERROR')])
def test_http_discovery_separates_auth_timeout_and_protocol_errors(store, tmp_path, fault, code):
    service = MCPConnectionService(tmp_path, store)
    with http_server(fault, fault=fault) as (url, received):
        connection = add(service, url)
        with pytest.raises(CapabilityError) as error:
            service.discover(connection['id'])
        assert error.value.code == code
        public = service.public(service.get(connection['id']))
        assert public['health']['error_code'] == code
        assert 'offline-http-key' not in str(public) + str(error.value)
        assert received and all(row['key_present'] for row in received)


def test_two_http_services_with_same_tool_name_do_not_share_call_or_credentials(store, tmp_path):
    service = MCPConnectionService(tmp_path, store)
    with http_server('first', expected_key='first-test-key') as (first_url, _), \
         http_server('second', expected_key='second-test-key') as (second_url, _):
        for name, url, key in [('first', first_url, 'first-test-key'), ('second', second_url, 'second-test-key')]:
            connection = service.discover(add(service, url, name, key)['id'])
            tool = connection['tools'][0]
            service.tool_policy(connection['id'], {'expected_revision': connection['revision'],
                'tool_name': tool['name'], 'schema_hash': tool['schema_hash'], 'stages': ['preparation'],
                'purpose': 'evidence', 'enabled': True,'read_only_confirmed':True})
        tools = service.tools()
        assert len({tool['id'] for tool in tools}) == 2
        with MCPRuntime(manager_factory=lambda connection: MCPManager(tmp_path, connection=connection,
                credentials=service.credentials, namespace=store.namespace)) as runtime:
            outputs = {tool['connection']['name']: runtime.call(tool, {'query': 'models'}) for tool in tools}
        assert outputs == {'first': {'server': 'first', 'query': 'models'},
                           'second': {'server': 'second', 'query': 'models'}}
        assert not runtime.running


@pytest.mark.parametrize('version', ['2026-07-28', '2025-06-18', '2024-11-05'])
def test_real_http_protocol_negotiation_and_legacy_text_result(store, tmp_path, version):
    service = MCPConnectionService(tmp_path, store)
    legacy = None if version == '2026-07-28' else version
    with http_server(version, legacy_version=legacy) as (url, received):
        connection = service.discover(add(service, url)['id'])
        tool = connection['tools'][0]
        assert tool['protocol_version'] == version
        service.tool_policy(connection['id'], {'expected_revision': connection['revision'],
            'tool_name': tool['name'], 'schema_hash': tool['schema_hash'], 'stages': ['preparation'],
            'purpose': 'evidence', 'enabled': True, 'read_only_confirmed': True})
        with MCPRuntime(manager_factory=lambda row: MCPManager(tmp_path, connection=row,
                credentials=service.credentials, namespace=store.namespace)) as runtime:
            assert runtime.call(service.tools()[0], {'query': 'models'}) == {'server': version, 'query': 'models'}
        methods = [row.get('method') for row in received]
        assert 'server/discover' in methods and 'tools/list' in methods and 'tools/call' in methods
        assert ('initialize' in methods) is bool(legacy)
        assert not runtime.running


def test_http_cross_origin_redirect_never_sends_auth_to_second_server(store, tmp_path):
    service = MCPConnectionService(tmp_path, store)
    with http_server('unapproved') as (target, target_requests), \
         http_server('redirect', fault='redirect:' + target) as (url, received):
        connection = add(service, url)
        with pytest.raises(CapabilityError):
            service.discover(connection['id'])
        assert received and target_requests == []


def test_revoked_secret_cannot_be_reused_by_an_already_open_http_session(store, tmp_path):
    service = MCPConnectionService(tmp_path, store)
    with http_server('credential') as (url, received):
        connection = service.discover(add(service, url)['id'])
        tool = connection['tools'][0]
        service.tool_policy(connection['id'], {'expected_revision': connection['revision'],
            'tool_name': tool['name'], 'schema_hash': tool['schema_hash'], 'stages': ['preparation'],
            'purpose': 'evidence', 'enabled': True,'read_only_confirmed':True})
        selected = service.tools()[0]
        with MCPRuntime(manager_factory=lambda connection: MCPManager(tmp_path, connection=connection,
                credentials=service.credentials, namespace=store.namespace)) as runtime:
            assert runtime.call(selected, {'query': 'first'})['query'] == 'first'
            reference = selected['connection']['header_refs']['x-test-key']
            path = service.credentials.directory / (reference.split(':', 1)[1] + '.bin')
            assert path.is_relative_to(tmp_path) and path.is_file()
            path.unlink()
            before = len(received)
            with pytest.raises(Exception, match='CREDENTIAL'):
                runtime.call(selected, {'query': 'after revocation'})
            assert len(received) == before


def test_constructor_failure_is_recorded_without_hiding_other_connections(store, tmp_path, monkeypatch):
    from src.agent.capabilities import mcp_service
    service = MCPConnectionService(tmp_path, store)
    first = add(service, 'http://127.0.0.1:19876/mcp', 'broken')
    second = add(service, 'http://127.0.0.1:19877/mcp', 'other')

    def broken(*args, **kwargs):
        raise CapabilityError('RUNTIME_PYTHON_MISSING', '应用解释器不存在', next_action='检查应用解释器路径')

    monkeypatch.setattr(mcp_service, 'MCPManager', broken)
    with pytest.raises(CapabilityError, match='RUNTIME_PYTHON_MISSING'):
        service.discover(first['id'])
    rows = {row['id']: row for row in service.list()['rows']}
    assert rows[first['id']]['health']['error_code'] == 'RUNTIME_PYTHON_MISSING'
    assert rows[second['id']]['health']['status'] == 'unknown'


def test_missing_builtin_paths_do_not_hide_custom_connections(store, tmp_path, monkeypatch):
    from src.agent.capabilities import mcp_service
    service = MCPConnectionService(tmp_path, store)
    custom = add(service, 'http://127.0.0.1:19876/mcp')

    def broken(*args):
        raise CapabilityError('RUNTIME_PYTHON_MISSING', '应用解释器不存在')

    monkeypatch.setattr(mcp_service.RuntimePaths, 'resolve', broken)
    rows = {row['id']: row for row in service.list()['rows']}
    assert rows['mcp_local']['health']['error_code'] == 'RUNTIME_PYTHON_MISSING'
    assert rows['mcp_local']['enabled'] is False
    assert rows[custom['id']]['health']['status'] == 'unknown'
    assert service.tools() == []


@pytest.mark.parametrize('url', ['http://127.0.0.1:1234/mcp?token=test-secret',
    'http://127.0.0.1:invalid/mcp', 'http://127.0.0.1:0/mcp',
    'http://127.0.0.1:1234/\\remote', 'http://127.0.0.1:1234/mcp\n'])
def test_http_configuration_rejects_credentials_and_ambiguous_urls(store, tmp_path, url):
    service = MCPConnectionService(tmp_path, store)
    before = len(store.resources('mcp'))
    with pytest.raises(CapabilityError, match='MCP_URL_INVALID'):
        add(service, url)
    assert len(store.resources('mcp')) == before
