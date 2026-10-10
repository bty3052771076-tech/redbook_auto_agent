from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4

from src.model_platforms.security import CredentialStore, PlatformError
from src.agent.mcp_manager import MCPManager
from .models import CapabilityError, digest, safe
from .runtime_paths import RuntimePaths
from .mcp_effects import (is_read_only, require_read_only, connection_target,
                          validate_builtin_connection, validate_connection_containers, network_policy, PROXY_ENV)


def catalog_diff(old: list[dict], new: list[dict], *, complete: bool) -> dict:
    previous, current = {r['name']:r for r in old}, {r['name']:r for r in new}
    return {'added': sorted(current.keys()-previous.keys()),
            'removed': sorted(previous.keys()-current.keys()) if complete else [],
            'changed': sorted(name for name in previous.keys() & current.keys()
                              if digest([previous[name].get('input_schema'),previous[name].get('annotations')])
                              != digest([current[name].get('input_schema'),current[name].get('annotations')])),
            'complete': complete}


def discovery_error(exc: Exception, identity: str) -> CapabilityError:
    pending, causes, seen = [exc], [], set()
    while pending and len(seen) < 128:
        cause = pending.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        causes.append(cause)
        pending.extend(getattr(cause, 'exceptions', ()))
        pending.extend(child for child in (cause.__cause__, cause.__context__) if child is not None)
    for cause in causes:
        if isinstance(cause, CapabilityError):
            return CapabilityError(cause.code, str(safe(cause.message)), resource_id=identity,
                next_action=cause.next_action or '检查此连接的配置后再次检测', retryable=cause.retryable)
        if isinstance(cause, PlatformError):
            return CapabilityError(cause.code, str(safe(cause.action)), resource_id=identity,
                next_action='在此连接中重新配置凭据后再次检测', retryable=cause.retryable)
    statuses = {getattr(getattr(cause, 'response', None), 'status_code', None) for cause in causes}
    if statuses & {401, 403}:
        code, message, action = 'MCP_AUTH_REJECTED', 'MCP 服务拒绝认证', '检查此连接的认证请求头及账户权限'
    elif MCPManager._contains_timeout(exc) or any('timeout' in type(cause).__name__.lower() for cause in causes):
        code, message, action = 'MCP_TIMEOUT', 'MCP 连接或协议请求超时', '检查网络、代理和服务响应，再调整超时'
    elif any(status is not None and status >= 400 for status in statuses):
        code, message, action = 'MCP_HTTP_ERROR', 'MCP 服务返回 HTTP 错误', '检查服务地址、可用状态及 HTTP 日志'
    elif any(isinstance(cause, OSError) or type(cause).__name__ in {'ConnectError', 'NetworkError'} for cause in causes):
        code, message, action = 'MCP_CONNECTION_FAILED', 'MCP 传输连接失败', '检查路径、网络、代理和服务进程'
    else:
        code, message, action = 'MCP_PROTOCOL_ERROR', 'MCP 协议响应无法识别', '确认此地址或程序提供 MCP 协议，再检查服务日志'
    return CapabilityError(code, message, resource_id=identity, next_action=action, retryable=True)


class MCPConnectionService:
    def __init__(self, root: Path, store):
        self.root, self.store = Path(root), store
        self.credentials = CredentialStore(self.root / 'data/agent/credentials/mcp')

    def builtin(self) -> dict:
        health = {'status':'unknown','observed_at':None}
        command, enabled = '', False
        try:
            command = str(RuntimePaths.resolve(self.root).python_executable)
            enabled = True
        except CapabilityError as exc:
            health = {'status':'blocked','observed_at':datetime.now(timezone.utc).isoformat(),
                      'error_code':exc.code,'error':str(safe(exc.message)),
                      'next_action':exc.next_action or '检查运行区和应用解释器路径'}
        return {'id':'mcp_local','kind':'mcp','name':'本地只读工具','builtin':True,'transport':'stdio',
                'command':command,'args':['-m','src.agent.mcp_server'],'cwd':str(self.root),
                'enabled':enabled,'revision':0,'tools':[], 'allowed_tools':['runtime_status','knowledge_search','news_search'],
                'health':health,'process_status':'idle',
                'timeout_seconds':60,'startup_timeout_seconds':20,'network_policy':{'mode':'direct'}}

    def list(self) -> dict:
        rows = self.store.resources('mcp')
        if not any(row['id']=='mcp_local' for row in rows):
            rows.insert(0,self.builtin())
        return {'rows':[self.public(r) for r in rows if not r.get('retired')]}

    def get(self, identity: str) -> dict:
        row = self.store.get(identity) or (self.builtin() if identity=='mcp_local' else None)
        if row is None or row.get('kind')!='mcp':
            raise CapabilityError('MCP_NOT_FOUND','连接不存在',status=404,resource_id=identity)
        validate_connection_containers(row)
        validate_builtin_connection(row, self.root)
        return row

    def public(self, row: dict) -> dict:
        value = {**row}
        try:
            validate_connection_containers(row)
            validate_builtin_connection(row, self.root)
            value['network_policy'] = network_policy(row)
        except CapabilityError as exc:
            value.update(enabled=False, health={'status':'blocked','error_code':exc.code,
                'error':exc.message,'next_action':exc.next_action}, tools=[],tool_policies={},
                environment_refs={},header_refs={})
        row = value
        policies = value.get('tool_policies') or {}
        value['tools'] = [{**tool, **policies.get(tool['name'],{}),
                           'read_only_eligible':is_read_only(tool,row)} for tool in row.get('tools') or []]
        value['tool_count'] = len(value['tools'])
        value['allowed_tool_count'] = sum(bool(tool.get('enabled')) and tool['read_only_eligible']
                                         and (row.get('builtin') or tool.get('read_only_confirmed') is True)
                                         for tool in value['tools'])
        for key in ['environment_refs','header_refs']:
            public_key = 'environment' if key=='environment_refs' else 'headers'
            value[public_key] = {name:'已配置' for name in value.get(key) or {}}
        value.pop('environment_refs',None)
        value.pop('header_refs',None)
        return safe(value)

    def save(self, data: dict, identity: str = '') -> dict:
        editable = {'name','transport','command','args','cwd','url','timeout_seconds','startup_timeout_seconds',
                    'enabled','environment','headers','expected_revision','import_ref','network_policy'}
        if set(data) - editable:
            raise CapabilityError('MCP_FIELD_INVALID','连接配置包含只读或未知字段，请使用工具审批入口修改允许范围')
        old = self.get(identity) if identity else {}
        if old.get('builtin'):
            if (connection_target({**old, **data}, self.root)[:-1] != connection_target(old, self.root)[:-1]
                    or data.get('url') or data.get('environment') or data.get('headers')):
                raise CapabilityError('MCP_BUILTIN_LOCKED','内置连接的执行配置不可修改；请另建自定义连接并核对只读权限')
        imported = {}
        if data.get('import_ref'):
            imported = self.store.get('mcp_import:'+str(data['import_ref'])) or {}
            if identity or imported.get('kind') != 'mcp_import':
                raise CapabilityError('MCP_IMPORT_MISSING','导入预览不存在，请重新预览',status=409)
        transport = data.get('transport',old.get('transport','stdio'))
        if transport not in {'stdio','streamable_http'}:
            raise CapabilityError('MCP_TRANSPORT_UNSUPPORTED','只支持 stdio 和 Streamable HTTP')
        identity = identity or 'mcp_'+uuid4().hex
        value = {**old,**{k:v for k,v in data.items() if k not in {'environment','headers','expected_revision','import_ref'}}}
        value.update(id=identity,transport=transport,name=str(data.get('name',old.get('name',''))).strip()[:120])
        value['network_policy'] = network_policy(value)
        if not value['name']:
            raise CapabilityError('MCP_NAME_REQUIRED','请输入连接名称')
        if transport=='stdio':
            command = Path(value.get('command') or '').resolve()
            cwd = Path(value.get('cwd') or self.root).resolve()
            args = value.get('args',[])
            if command.drive.upper()!='E:' or not command.is_file() or cwd.drive.upper()!='E:' or not cwd.is_dir():
                raise CapabilityError('MCP_PATH_INVALID','可执行文件和工作目录必须存在且位于 E 盘')
            if not isinstance(args,list) or len(args)>80 or any(not isinstance(v,str) or len(v)>2000 for v in args):
                raise CapabilityError('MCP_ARGS_INVALID','参数须为不超过80项的字符串数组')
            if command.name.lower() in {'cmd.exe','powershell.exe','pwsh.exe','bash.exe'} or any(v in {'-y','--yes'} for v in args):
                raise CapabilityError('MCP_INSTALL_NOT_ALLOWED','请引用已经安装的程序，不执行 Shell 或自动安装命令')
            value.update(command=str(command),cwd=str(cwd),args=args)
        else:
            url = str(value.get('url',''))
            try:
                parsed = urlsplit(url)
                port = parsed.port
                valid = parsed.hostname and (port is None or 1 <= port <= 65535)
            except ValueError:
                valid = False
            if not valid or parsed.scheme not in {'https','http'} or parsed.username or parsed.password or parsed.query or parsed.fragment or '\\' in url or any(ord(char) < 33 for char in url):
                raise CapabilityError('MCP_URL_INVALID','请输入不含凭据的 HTTP 地址')
            if parsed.scheme!='https' and parsed.hostname not in {'localhost','127.0.0.1','::1'}:
                raise CapabilityError('MCP_HTTPS_REQUIRED','非本机服务必须使用 HTTPS')
        for field in ['timeout_seconds','startup_timeout_seconds']:
            number=float(value.get(field,60 if field=='timeout_seconds' else 20))
            if not 1<=number<=600:
                raise CapabilityError('MCP_TIMEOUT_INVALID','超时须在1至600秒内')
            value[field]=number
        for public_key,secret_key in [('environment','environment_refs'),('headers','header_refs')]:
            refs = dict(imported.get(secret_key) or old.get(secret_key) or {})
            entries = data.get(public_key) or {}
            if not isinstance(entries,dict) or len(entries)>40:
                raise CapabilityError('MCP_CREDENTIAL_INVALID','环境变量和请求头必须是有界对象')
            for name,secret in entries.items():
                if public_key=='environment' and name in PROXY_ENV:
                    raise CapabilityError('MCP_NETWORK_INVALID','代理变量请使用网络策略配置，不与凭据环境变量混用')
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,127}',name) or not isinstance(secret,str) or '\n' in secret or '\r' in secret:
                    raise CapabilityError('MCP_CREDENTIAL_INVALID','字段名或凭据格式无效')
                if secret and secret!='已配置':
                    refs[name]=self.credentials.save(secret)
            value[secret_key]=refs
        value['enabled']=bool(data.get('enabled',old.get('enabled',False)))
        if old and connection_target(value, self.root) != connection_target(old, self.root):
            value.update(tools=[],tool_policies={},enabled=False)
        value['health']={'status':'unknown','observed_at':None}
        return self.public(self.store.put(identity,'mcp',value,expected_revision=int(data.get('expected_revision',0)),reason='保存 MCP 连接'))

    def discover(self, identity: str) -> dict:
        row = self.get(identity)
        try:
            manager = MCPManager(self.root,connection=row,credentials=self.credentials,namespace=self.store.namespace)
            tools=manager.list_tools()
            result={'status':'ready' if manager.complete else 'degraded','observed_at':datetime.now(timezone.utc).isoformat(),
                    'probe':'protocol_and_catalog','protocol_version':manager.protocol_version}
            difference=catalog_diff(row.get('tools') or [],tools,complete=manager.complete)
            policies=dict(row.get('tool_policies') or {})
            for name in difference['changed']+difference['removed']:
                if name in policies:
                    policies[name]={**policies[name],'enabled':False,'schema_changed':True}
            for tool in tools:
                tool['schema_hash']=digest(tool['input_schema'])
            payload={**row,'tools':tools,'catalog_diff':difference,'tool_policies':policies,'health':result}
        except Exception as exc:
            error = discovery_error(exc, identity)
            payload={**row,'health':{'status':'blocked','observed_at':datetime.now(timezone.utc).isoformat(),
                                     'probe':'protocol_and_catalog','error_code':error.code,
                                     'error':error.message,'next_action':error.next_action}}
            self.store.put(identity,'mcp',payload,expected_revision=row['revision'],reason='MCP 检测失败')
            raise error from None
        updated=self.store.put(identity,'mcp',payload,expected_revision=row['revision'],reason='检测并发现工具')
        return self.public(updated)

    def tool_policy(self, identity: str, data: dict) -> dict:
        row=self.get(identity)
        name=str(data.get('tool_name',''))
        tool=next((t for t in row.get('tools') or [] if t['name']==name),None)
        if not tool or data.get('schema_hash')!=tool.get('schema_hash'):
            raise CapabilityError('MCP_SCHEMA_CONFLICT','工具目录已改变，请刷新并重新批准',status=409)
        if data.get('enabled', True):
            require_read_only(tool, row, confirmed=data.get('read_only_confirmed', False))
        stages=data.get('stages',[])
        if not stages or any(stage not in {'preparation','evidence'} for stage in stages):
            raise CapabilityError('MCP_STAGE_INVALID','自定义工具仅可绑定材料准备或证据核验阶段')
        if data.get('purpose') not in {'evidence','duplicate_reference','style_reference','operations'}:
            raise CapabilityError('MCP_PURPOSE_REQUIRED','请选择工具输出的用途')
        policies={**(row.get('tool_policies') or {}),name:{'schema_hash':tool['schema_hash'],'stages':stages,
                 'purpose':data['purpose'],'enabled':bool(data.get('enabled',True)),
                 'read_only_confirmed':row.get('builtin',False) or data.get('read_only_confirmed') is True,
                 'effect_hash':digest(tool.get('annotations') or {})}}
        return self.public(self.store.put(identity,'mcp',{**row,'tool_policies':policies},expected_revision=int(data['expected_revision']),reason='批准工具使用范围'))

    def tools(self) -> list[dict]:
        rows=[]
        connections = self.store.resources('mcp')
        if not any(row['id']=='mcp_local' for row in connections):
            connections.insert(0,self.builtin())
        for connection in connections:
            if connection.get('retired'):
                continue
            try:
                validate_connection_containers(connection)
                validate_builtin_connection(connection, self.root)
            except CapabilityError as exc:
                connection = {**connection, 'enabled':False, 'health':{'status':'blocked',
                    'error_code':exc.code,'error':exc.message,'next_action':exc.next_action},
                    'tools':[],'tool_policies':{}}
            for tool in connection.get('tools') or []:
                policy=(connection.get('tool_policies') or {}).get(tool['name'],{})
                rows.append({'id':f"mcp:{connection['id']}:{tool['name']}",'name':tool['name'],
                    'kind':'mcp','group':'选题与材料','description':tool.get('description',''),
                    'enabled':connection.get('enabled',False) and policy.get('enabled',False)
                        and is_read_only(tool,connection) and (connection.get('builtin') or policy.get('read_only_confirmed') is True),
                    'health':connection.get('health') or {'status':'unknown'},'revision':connection['revision'],
                    'binding':'agent_preparation' if policy.get('enabled') else 'unbound',
                    'stages':policy.get('stages',[]),'input_schema':tool['input_schema'],
                    'schema_hash':tool['schema_hash'],'purpose':policy.get('purpose',''),
                    'dependencies':['builtin:news.search'] if connection.get('builtin') and tool['name']=='news_search' else [],
                    'annotations':tool.get('annotations') or {},'read_only_confirmed':policy.get('read_only_confirmed',False),
                    'effect_hash':policy.get('effect_hash'),
                    'effects':['network_read'] if is_read_only(tool,connection) else ['unknown_effect'],'connection':connection})
        return rows

    def retire(self, identity: str, revision: int) -> dict:
        row = self.store.get(identity) or (self.builtin() if identity=='mcp_local' else None)
        if row is None or row.get('kind')!='mcp':
            raise CapabilityError('MCP_NOT_FOUND','连接不存在',status=404,resource_id=identity)
        return self.public(self.store.put(identity,'mcp',{**row,'retired':True,'enabled':False},expected_revision=revision,reason='退役连接'))

    def import_preview(self, config: dict) -> dict:
        servers=config.get('mcpServers')
        if not isinstance(servers,dict) or len(servers)>50:
            raise CapabilityError('MCP_IMPORT_INVALID','需要 mcpServers 对象且最多50个连接')
        rows=[]
        for name,value in servers.items():
            if not isinstance(value,dict):
                raise CapabilityError('MCP_IMPORT_INVALID','每个连接必须是对象')
            unknown=sorted(set(value)-{'command','args','cwd','env','url','headers','type','transport','timeout'})
            url = str(value.get('url',''))
            if url:
                parsed = urlsplit(url)
                if parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise CapabilityError('MCP_URL_INVALID','导入地址不能包含凭据、查询参数或片段')
            row = {'name':name,'transport':'streamable_http' if value.get('url') else 'stdio',
                         'command':value.get('command',''),'args':value.get('args',[]),'cwd':value.get('cwd',str(self.root)),
                         'url':url,'issues':unknown,'enabled':False}
            refs = {}
            for field, source in [('environment','env'),('headers','headers')]:
                entries = value.get(source) or {}
                if not isinstance(entries,dict) or len(entries)>40 or any(
                        not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,127}',key) or not isinstance(secret,str)
                        or '\n' in secret or '\r' in secret for key,secret in entries.items()):
                    raise CapabilityError('MCP_CREDENTIAL_INVALID','导入的敏感配置格式无效')
                private_field = 'environment_refs' if field=='environment' else 'header_refs'
                refs[private_field] = {key:self.credentials.save(secret) for key,secret in entries.items() if secret}
                row[field] = {key:'已配置' for key in refs[private_field]}
            token = uuid4().hex
            self.store.put('mcp_import:'+token,'mcp_import',refs,expected_revision=0,reason='预览导入敏感配置引用')
            row['import_ref'] = token
            rows.append(row)
        return {'rows':rows,'requires_save':True}
