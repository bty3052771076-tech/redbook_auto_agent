"""Only reviewed read-only MCP tools may enter the preparation/evidence stages."""
import re
from pathlib import Path
from urllib.parse import urlsplit

from .models import CapabilityError
from .runtime_paths import RuntimePaths


BUILTIN_READ_ONLY = {'runtime_status', 'knowledge_search', 'news_search'}
PROXY_ENV = frozenset({'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY',
                      'http_proxy','https_proxy','all_proxy','no_proxy'})


def network_policy(connection):
    policy = connection.get('network_policy')
    if policy is None:
        return {'mode':'direct' if connection.get('transport','stdio') == 'stdio' else 'inherit'}
    if not isinstance(policy,dict) or set(policy)-{'mode','proxy_url'} or policy.get('mode') not in {'direct','inherit','custom'}:
        raise CapabilityError('MCP_NETWORK_INVALID','请选择直连、继承代理或指定代理')
    mode, url = policy['mode'], policy.get('proxy_url','')
    if mode != 'custom':
        if url:
            raise CapabilityError('MCP_NETWORK_INVALID','直连和继承模式不能同时指定代理地址')
        return {'mode':mode}
    try:
        parsed = urlsplit(url) if isinstance(url,str) and len(url)<=2048 else None
        valid = parsed and parsed.hostname and (parsed.port is None or 1<=parsed.port<=65535)
    except ValueError:
        valid = False
    if (not valid or parsed.scheme not in {'http','https'} or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {'','/'} or '\\' in url
            or any(ord(c)<33 for c in url)
            or (parsed.scheme=='http' and parsed.hostname not in {'localhost','127.0.0.1','::1'})):
        raise CapabilityError('MCP_NETWORK_INVALID','代理须为无凭据的 HTTPS 地址或本机 HTTP 地址',
                              next_action='例如 http://127.0.0.1:7890；不填写用户名、密码、路径或查询参数')
    return {'mode':mode,'proxy_url':url.rstrip('/')}


def connection_target(connection, root):
    transport = connection.get('transport', 'stdio')
    if transport == 'stdio':
        target = (str(Path(connection.get('command') or '').resolve()),
                  tuple(connection.get('args') or []), str(Path(connection.get('cwd') or root).resolve()))
    else:
        target = (connection.get('url') or '',)
    return (transport, target, connection.get('environment_refs') or {}, connection.get('header_refs') or {},network_policy(connection))


def validate_connection_containers(connection):
    network_policy(connection)
    invalid = False
    for key in ('environment_refs', 'header_refs', 'tool_policies'):
        value = connection.get(key)
        if value is None:
            continue
        invalid = invalid or not isinstance(value, dict)
        if isinstance(value, dict):
            expected = dict if key == 'tool_policies' else str
            invalid = invalid or any(not isinstance(name,str) or not isinstance(entry,expected)
                                     for name,entry in value.items())
    tools = connection.get('tools')
    if tools is not None:
        invalid = invalid or not isinstance(tools, list)
        if isinstance(tools, list):
            invalid = invalid or any(not isinstance(tool,dict) or not isinstance(tool.get('name'),str)
                or not tool['name'] or not isinstance(tool.get('input_schema'),dict)
                or not isinstance(tool.get('schema_hash'),str) for tool in tools)
    if invalid:
        raise CapabilityError('MCP_CONFIG_INVALID', '此连接的历史配置结构损坏，已隔离',
            resource_id=connection.get('id',''), next_action='退役损坏记录并重新添加连接；其他连接仍可使用')


def validate_builtin_connection(connection, root):
    if not connection.get('builtin') and connection.get('id') != 'mcp_local':
        return
    paths = RuntimePaths.resolve(root)
    expected = {'transport':'stdio', 'command':str(paths.python_executable),
                'args':['-m','src.agent.mcp_server'], 'cwd':str(paths.runtime_root)}
    args = connection.get('args')
    allowed = connection.get('allowed_tools', BUILTIN_READ_ONLY)
    typed = (isinstance(args, (list, tuple)) and all(isinstance(arg,str) for arg in args)
             and isinstance(allowed, (list, tuple, set, frozenset))
             and all(isinstance(name,str) for name in allowed)
             and isinstance(connection.get('command'), str)
             and isinstance(connection.get('cwd'), str))
    matches = False
    if typed:
        try:
            matches = connection_target(connection, root)[:-1] == connection_target(expected, root)[:-1]
        except (TypeError, ValueError, OSError):
            pass
    if (connection.get('id') != 'mcp_local' or connection.get('builtin') is not True
            or not matches
            or connection.get('url') or connection.get('environment') or connection.get('headers')
            or set(allowed) != BUILTIN_READ_ONLY):
        raise CapabilityError('MCP_BUILTIN_MISMATCH', '内置连接与项目受控目标不一致，已阻止运行',
            resource_id=connection.get('id',''), next_action='退役旧配置；外部服务请另建自定义连接并重新批准')


def is_read_only(tool, connection):
    name = tool.get('name') or str(tool.get('id', '')).split(':', 2)[-1]
    if connection.get('builtin'):
        return name in BUILTIN_READ_ONLY
    annotations = tool.get('annotations') or {}
    return (isinstance(annotations, dict) and annotations.get('readOnlyHint') is True
            and annotations.get('destructiveHint') is not True
            and not re.match(r'^(?:delete|remove|publish|post|upload|write|create|update|execute|run)(?:_|$)', name, re.I))


def require_read_only(tool, connection, *, confirmed=False):
    if not is_read_only(tool, connection) or not (connection.get('builtin') or confirmed is True):
        raise CapabilityError('MCP_READ_ONLY_REQUIRED',
            '此阶段仅允许经人工确认的只读工具；写入或用途不明的工具不能批准',
            resource_id=tool.get('id', tool.get('name', '')),
            next_action='使用可信只读服务和只读凭据，检测后重新确认；平台写入使用已有业务入口')
