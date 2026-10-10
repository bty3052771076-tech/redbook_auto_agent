"""Task-owned MCP sessions; async contexts are opened and closed by one owner."""
import asyncio
from concurrent.futures import Future
from contextlib import AsyncExitStack
import json
from queue import Queue, Empty
from threading import Lock, Thread, current_thread

from jsonschema import Draft202012Validator

from .models import CapabilityError, digest
from .mcp_effects import require_read_only


class MCPRuntime:
    def __init__(self, *, manager_factory, idle_seconds=60):
        self.factory, self.idle_seconds = manager_factory, idle_seconds
        self.queue, self.lock = Queue(), Lock()
        self.thread, self.closed = None, False

    @property
    def running(self):
        with self.lock:
            return self.thread is not None and self.thread.is_alive()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def call(self, tool, arguments):
        require_read_only(tool, tool['connection'], confirmed=tool.get('read_only_confirmed',False))
        schema = tool['input_schema']
        try:
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(arguments)
            if len(json.dumps(arguments,ensure_ascii=False))>20000:
                raise ValueError('arguments exceed the bound')
        except Exception as exc:
            raise CapabilityError('MCP_ARGUMENTS_INVALID','工具参数不符合已批准的schema') from exc
        future = Future()
        with self.lock:
            if self.closed:
                raise CapabilityError('MCP_RUNTIME_CLOSED','本次MCP运行已关闭')
            self.queue.put((tool, arguments, future))
            if self.thread is None or not self.thread.is_alive():
                self.thread = Thread(target=self._owner, name='agent-mcp-owner', daemon=True)
                self.thread.start()
        connection = tool['connection']
        return future.result(timeout=2*float(connection.get('startup_timeout_seconds',20))+
                             float(connection.get('timeout_seconds',60))+5)

    def close(self):
        with self.lock:
            self.closed = True
            thread = self.thread
            if thread and thread.is_alive():
                self.queue.put(None)
        if thread:
            thread.join(timeout=30)
            if thread.is_alive():
                raise TimeoutError('MCP_SHUTDOWN_PENDING: owned session is still closing')

    def _owner(self):
        try:
            asyncio.run(self._serve())
        finally:
            with self.lock:
                if self.thread is current_thread():
                    self.thread = None

    async def _serve(self):
        sessions = {}
        async with AsyncExitStack() as stack:
            while True:
                try:
                    request = await asyncio.to_thread(self.queue.get, True, self.idle_seconds)
                except Empty:
                    with self.lock:
                        if not self.queue.empty():
                            continue
                        if self.thread is current_thread():
                            self.thread = None
                    return
                if request is None:
                    return
                tool, arguments, future = request
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    connection = tool['connection']
                    key = digest(connection)
                    if key not in sessions:
                        manager = self.factory(connection)
                        session = await stack.enter_async_context(manager._session())
                        sessions[key] = (manager, session)
                    manager, session = sessions[key]
                    validate_credentials = getattr(manager, 'validate_credentials', None)
                    if validate_credentials:
                        validate_credentials()
                    catalog = await asyncio.wait_for(manager._list(session),timeout=float(connection.get('startup_timeout_seconds',20)))
                    name = tool['id'].split(':',2)[-1]
                    found = next((row for row in catalog if row['name']==name), None)
                    if not manager.complete or not found or digest(found['input_schema']) != tool['schema_hash']:
                        raise CapabilityError('MCP_SCHEMA_CHANGED','运行目录与冻结schema不一致，请重新检测并批准')
                    require_read_only(found, connection, confirmed=tool.get('read_only_confirmed',False))
                    if not connection.get('builtin') and digest(found.get('annotations') or {}) != tool.get('effect_hash'):
                        raise CapabilityError('MCP_SCHEMA_CHANGED','工具副作用声明已改变，请重新检测并批准')
                    timeout = float(connection.get('timeout_seconds',60))
                    if validate_credentials:
                        validate_credentials()
                    response = await asyncio.wait_for(session.call_tool(name, arguments, read_timeout_seconds=timeout),timeout=timeout+2)
                    if getattr(response,'is_error',getattr(response,'isError',False)):
                        raise CapabilityError('MCP_TOOL_ERROR','MCP工具返回错误')
                    result = getattr(response,'structured_content',getattr(response,'structuredContent',None))
                    if result is None:
                        parts = [item.text for item in response.content if getattr(item,'text',None)]
                        result = {'content':parts}
                        if len(parts) == 1 and len(parts[0]) <= 100000:
                            try:
                                result = json.loads(parts[0])
                            except (ValueError, TypeError):
                                pass
                    if len(json.dumps(result,ensure_ascii=False,default=str))>100000:
                        raise CapabilityError('MCP_RESULT_TOO_LARGE','MCP结果超过100000字符，请收窄查询')
                    future.set_result(result)
                except Exception as exc:
                    future.set_exception(exc)
