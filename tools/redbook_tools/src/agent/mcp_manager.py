from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager, AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
from threading import Thread
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client


@dataclass(frozen=True)
class LocalMCPServer:
    server_id: str
    command: str
    args: tuple[str, ...]
    cwd: Path
    allowed_tools: frozenset[str]
    timeout_seconds: float = 60.0
    startup_timeout_seconds: float = 20.0
    transport: str = 'stdio'
    url: str = ''
    environment: dict | None = None
    headers: dict | None = None
    builtin: bool = True
    network_policy: dict | None = None


class MCPManager:
    """On-demand stdio MCP client. Only the reviewed project server is enabled by default."""

    SOURCE_ENV_ALLOWLIST = frozenset({
        "NEWS_PROVIDER", "NEWS_TIMEOUT_S", "NEWS_MAX_RECORDS", "NEWS_FETCH_WINDOW_DAYS",
        "UNIFIED_NEWS_SOURCES", "UNIFIED_WORLDMONITOR", "WORLDMONITOR_BASE_URL",
        "WORLDMONITOR_DIR", "WORLDMONITOR_AUTO_START", "WORLDMONITOR_TIMEOUT_S",
        "NEWS_API_KEY", "GNEWS_API_KEY", "NEWSDATA_API_KEY", "THENEWSAPI_TOKEN",
        "ALPHAVANTAGE_API_KEY", "FINNHUB_API_KEY", "JUHE_NEWS_APPKEY",
        "JUHE_FINANCE_NEWS_APPKEY", "TIANAPI_API_KEY", "TIANAPI_KEY",
        "NEWSAPI_API_KEY", "NEWSAPI_KEY",
    })
    PROCESS_ENV_ALLOWLIST = frozenset({
        "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "APPDATA", "LOCALAPPDATA",
    })
    _TOOL_REQUIRED = {
        "runtime_status": set(),
        "knowledge_search": {"query"},
        "news_search": {"query"},
    }

    def __init__(self, workspace_root: Path, *, connection: dict | None = None, credentials=None, namespace: str | None = None):
        from src.agent.capabilities.runtime_paths import RuntimePaths
        self.root = workspace_root.resolve()
        self.paths = RuntimePaths.resolve(self.root)
        self.python = self.paths.python_executable
        self.protocol_version = ''
        self.complete = True
        self.credentials = credentials
        self._credential_fingerprints = None
        self.last_http_status = None
        self.namespace = namespace or os.getenv('AGENT_CAPABILITY_NAMESPACE','local')
        configured = connection or {}
        from src.agent.capabilities.mcp_effects import network_policy
        if connection:
            from src.agent.capabilities.mcp_effects import validate_builtin_connection, validate_connection_containers
            validate_connection_containers(configured)
            validate_builtin_connection(configured, self.root)
        self.server = LocalMCPServer(
            server_id=str(configured.get('id') or 'redbook-local'),
            command=str(configured.get('command') or self.python),
            args=tuple(configured.get('args', ('-m', 'src.agent.mcp_server'))),
            cwd=Path(configured.get('cwd') or self.root),
            allowed_tools=frozenset(configured.get('allowed_tools', self._TOOL_REQUIRED if not connection else [])),
            timeout_seconds=float(configured.get('timeout_seconds', 60)),
            startup_timeout_seconds=float(configured.get('startup_timeout_seconds', 20)),
            transport=str(configured.get('transport', 'stdio')), url=str(configured.get('url') or ''),
            environment=configured.get('environment_refs') or {}, headers=configured.get('header_refs') or {},
            builtin=not connection or bool(configured.get('builtin')),
            network_policy=network_policy(configured),
        )

    def validate_credentials(self) -> None:
        from src.model_platforms.security import PlatformError
        refs = set((self.server.environment or {}).values()) | set((self.server.headers or {}).values())
        if refs and self.credentials is None:
            raise PlatformError('CREDENTIAL_UNAVAILABLE', '未配置此 MCP 的凭据存储')
        current = {ref: self.credentials.fingerprint(ref) for ref in refs}
        if self._credential_fingerprints is not None and current != self._credential_fingerprints:
            raise PlatformError('CREDENTIAL_CHANGED', '运行中的 MCP 凭据已改变，请重新确认任务')
        self._credential_fingerprints = current

    def _parameters(self) -> StdioServerParameters:
        inherited = self.PROCESS_ENV_ALLOWLIST | (self.SOURCE_ENV_ALLOWLIST if self.server.builtin else frozenset())
        env = {
            key: value for key in inherited
            if (value := os.environ.get(key)) is not None
        }
        env_file = self.root / ".env.gui"
        if self.server.builtin and env_file.is_file():
            from apps.gui import load_env_file
            configured = load_env_file(env_file)
            env.update({key: configured[key] for key in self.SOURCE_ENV_ALLOWLIST if configured.get(key)})
        env["PYTHONIOENCODING"] = "utf-8"
        env['PYTHONPATH'] = str(self.paths.tool_package_root)
        if self.server.builtin:
            env['KNOWLEDGE_DB_CREDENTIALS'] = str(self.root / 'data/knowledge/postgresql-local/connection.json')
            env['KNOWLEDGE_EMBEDDING_CACHE'] = str(self.root / 'data/models/fastembed')
            env['AGENT_CAPABILITY_NAMESPACE'] = self.namespace
        env['REDBOOK_RUNTIME_ROOT'] = str(self.root)
        env.update({key: self.credentials.read(ref) for key, ref in (self.server.environment or {}).items()})
        from src.agent.capabilities.mcp_effects import PROXY_ENV
        for key in PROXY_ENV:
            env.pop(key,None)
        policy = self.server.network_policy
        if policy['mode'] == 'inherit':
            env.update({key:os.environ[key] for key in PROXY_ENV if key in os.environ})
        elif policy['mode'] == 'custom':
            env.update(HTTP_PROXY=policy['proxy_url'],HTTPS_PROXY=policy['proxy_url'],
                       ALL_PROXY=policy['proxy_url'])
        env["TEMP"] = str(self.root / "data" / "tmp" / "mcp")
        env["TMP"] = env["TEMP"]
        Path(env["TEMP"]).mkdir(parents=True, exist_ok=True)
        return StdioServerParameters(
            command=self.server.command,
            args=list(self.server.args),
            cwd=str(self.server.cwd),
            env=env,
            encoding="utf-8",
            encoding_error_handler="replace",
        )

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        self.validate_credentials()
        async with AsyncExitStack() as stack:
            if self.server.transport == 'stdio':
                parameters = self._parameters()
                log = stack.enter_context(self._stderr_log(parameters))
                read, write = await stack.enter_async_context(stdio_client(parameters, errlog=log))
            elif self.server.transport == 'streamable_http':
                from mcp.client.streamable_http import streamable_http_client
                import httpx2
                headers = {key: self.credentials.read(ref) for key, ref in (self.server.headers or {}).items()}
                policy = self.server.network_policy
                http = await stack.enter_async_context(httpx2.AsyncClient(headers=headers,timeout=self.server.timeout_seconds,
                    trust_env=policy['mode']=='inherit',proxy=policy.get('proxy_url') if policy['mode']=='custom' else None))
                async def observe_status(response):
                    self.last_http_status = response.status_code
                http.event_hooks.setdefault('response', []).append(observe_status)
                streams = await stack.enter_async_context(streamable_http_client(self.server.url, http_client=http))
                read, write = streams[0], streams[1]
            else:
                raise ValueError('MCP_TRANSPORT_UNSUPPORTED')
            session = await stack.enter_async_context(ClientSession(read, write))
            try:
                if hasattr(session, 'discover'):
                    try:
                        await asyncio.wait_for(session.discover(), timeout=self.server.startup_timeout_seconds)
                    except Exception as exc:
                        if getattr(exc, 'code', None) != -32601:
                            raise
                        await asyncio.wait_for(session.initialize(), timeout=self.server.startup_timeout_seconds)
                else:
                    await asyncio.wait_for(session.initialize(), timeout=self.server.startup_timeout_seconds)
                self.protocol_version = str(getattr(session, '_negotiated_version', '') or 'legacy')
                yield session
            except Exception:
                from src.agent.capabilities.models import CapabilityError
                if self.last_http_status in {401, 403}:
                    raise CapabilityError('MCP_AUTH_REJECTED', 'MCP 服务拒绝认证',
                        next_action='检查此连接的认证请求头及账户权限') from None
                if self.last_http_status is not None and self.last_http_status >= 400:
                    raise CapabilityError('MCP_HTTP_ERROR', 'MCP 服务返回 HTTP 错误',
                        next_action='检查服务地址和 HTTP 日志') from None
                raise

    @contextmanager
    def _stderr_log(self, parameters):
        from src.agent.capabilities.models import safe
        secret_names = set(self.server.environment or {})
        secrets = {value for key,value in (parameters.env or {}).items() if value and
                   (key in secret_names or any(word in key.upper() for word in ('KEY','TOKEN','SECRET','PASSWORD','CREDENTIAL','AUTHORIZATION')))}
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, 'r', encoding='utf-8', errors='replace')
        writer = os.fdopen(write_fd, 'w', encoding='utf-8')
        path = self.root/'data/tmp/mcp-stderr.log'
        path.parent.mkdir(parents=True, exist_ok=True)

        def drain():
            with reader, path.open('a', encoding='utf-8') as output:
                while line := reader.readline(8193):
                    if len(line) > 8192:
                        while line and not line.endswith('\n'):
                            line = reader.readline(8193)
                        output.write('[oversized stderr omitted]\n')
                        continue
                    for secret in sorted(secrets, key=len, reverse=True):
                        line = line.replace(secret, '[redacted]')
                    output.write(str(safe(line)))
                    output.flush()

        thread = Thread(target=drain, name='mcp-redacted-stderr', daemon=True)
        thread.start()
        try:
            yield writer
        finally:
            writer.close()
            thread.join(timeout=5)
            if thread.is_alive():
                raise TimeoutError('MCP_STDERR_SHUTDOWN_PENDING')

    def _validate_tools(self, tools: list[Any]) -> None:
        names = {str(tool.name) for tool in tools}
        if names != set(self.server.allowed_tools):
            raise RuntimeError("MCP_TOOL_CATALOG_MISMATCH: local MCP tools changed; review before use")
        for tool in tools:
            schema = getattr(tool, 'input_schema', getattr(tool, 'inputSchema', {})) or {}
            properties = set((schema.get("properties") or {}).keys())
            required = set(schema.get("required") or [])
            if not self._TOOL_REQUIRED[tool.name].issubset(properties | required):
                raise RuntimeError(f"MCP_TOOL_SCHEMA_INVALID: {tool.name}")

    @staticmethod
    def _contains_timeout(exc: BaseException, seen: set[int] | None = None) -> bool:
        seen = seen or set()
        if id(exc) in seen:
            return False
        seen.add(id(exc))
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return True
        if "timed out while waiting for response" in str(exc).casefold():
            return True
        children = getattr(exc, "exceptions", ())
        linked = [*children]
        if exc.__cause__ is not None:
            linked.append(exc.__cause__)
        if exc.__context__ is not None:
            linked.append(exc.__context__)
        return any(MCPManager._contains_timeout(child, seen) for child in linked)

    async def alist_tools(self) -> list[dict[str, Any]]:
        async with self._session() as session:
            return await self._list(session)

    async def _list(self, session) -> list[dict]:
        collected, cursor, seen = [], None, set()
        self.complete = False
        for _ in range(20):
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            result = await asyncio.wait_for(session.list_tools(params=params), timeout=self.server.startup_timeout_seconds)
            collected.extend(result.tools)
            cursor = getattr(result, 'next_cursor', getattr(result, 'nextCursor', None))
            if not cursor:
                self.complete = True
                break
            if cursor in seen:
                break
            seen.add(cursor)
        if self.server.builtin:
            if not self.complete:
                raise RuntimeError('MCP_CATALOG_INCOMPLETE')
            self._validate_tools(collected)
        return [{'name': tool.name, 'description': tool.description or '',
                 'input_schema': getattr(tool,'input_schema',getattr(tool,'inputSchema',{})),
                 'output_schema': getattr(tool,'output_schema',getattr(tool,'outputSchema',{})),
                 'annotations': tool.annotations.model_dump(by_alias=True, exclude_none=True)
                    if getattr(tool,'annotations',None) is not None else {},
                 'server_id': self.server.server_id, 'protocol_version': self.protocol_version}
                for tool in collected]

    async def acall_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.server.allowed_tools:
            raise PermissionError("MCP_TOOL_NOT_ALLOWED")
        try:
            async with self._session() as session:
                catalog = await self._list(session)
                if name not in {item['name'] for item in catalog}:
                    raise RuntimeError('MCP_TOOL_REMOVED')
                self.validate_credentials()
                result = await asyncio.wait_for(
                    session.call_tool(name, arguments, read_timeout_seconds=self.server.timeout_seconds),
                    timeout=self.server.timeout_seconds + 2,
                )
                if getattr(result,'is_error',getattr(result,'isError',False)):
                    raise RuntimeError(f"MCP_TOOL_ERROR: {name}")
                structured = getattr(result,'structured_content',getattr(result,'structuredContent',None))
                if structured is not None:
                    return {"status": "ok", "server_id": self.server.server_id, "tool": name, "result": structured, 'protocol_version':self.protocol_version}
                text_parts = [str(item.text) for item in result.content if getattr(item, "text", None)]
                if len(text_parts) == 1:
                    try:
                        decoded = json.loads(text_parts[0])
                    except (json.JSONDecodeError, TypeError):
                        pass
                    else:
                        if isinstance(decoded, dict):
                            return {"status": "ok", "server_id": self.server.server_id, "tool": name, "result": decoded, 'protocol_version':self.protocol_version}
                return {"status": "ok", "server_id": self.server.server_id, "tool": name, "content": text_parts, 'protocol_version':self.protocol_version}
        except Exception as exc:
            if self._contains_timeout(exc):
                raise TimeoutError(f"MCP_TOOL_TIMEOUT: {name} exceeded {self.server.timeout_seconds:g}s") from None
            raise

    def list_tools(self) -> list[dict[str, Any]]:
        return asyncio.run(self.alist_tools())

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("MCP arguments must be a JSON object")
        return asyncio.run(self.acall_tool(name, arguments))


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Reviewed local MCP utilities")
    parser.add_argument("action", choices=("list", "call"))
    parser.add_argument("tool", nargs="?")
    parser.add_argument("--args", default="{}", help="JSON object; never interpreted as shell")
    args = parser.parse_args(argv)
    manager = MCPManager(Path(__file__).resolve().parents[2])
    if args.action == "list":
        payload = {"status": "ready", "tools": manager.list_tools()}
    else:
        if not args.tool:
            parser.error("call requires a tool name")
        payload = manager.call_tool(args.tool, json.loads(args.args))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
