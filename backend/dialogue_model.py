"""LangChain model boundary over the existing, role-frozen model adapters."""
from __future__ import annotations

import ipaddress
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlsplit

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr

from src.model_platforms.integration import (
    freeze_run_environment, frozen_legacy_config, legacy_controller, platform_config,
)
from src.model_platforms.runtime import RuntimeClient, encode, decode
from src.model_platforms.security import PlatformError
from src.model_platforms.store import PlatformStore
from src.llm.generate import _strip_reasoning_text, _summary_provider_kwargs
from src.llm.request_policy import minimax_output_tokens


def _messages(messages):
    output = []
    for message in messages:
        role = {"human": "user", "ai": "assistant"}.get(message.type, message.type)
        if role not in {"system", "user", "assistant", "tool"}:
            raise PlatformError("MESSAGE_ROLE_INVALID", "Unsupported conversation message role")
        row = {"role": role, "content": message.content}
        if isinstance(message, AIMessage) and message.tool_calls:
            row["tool_calls"] = [{"id": call["id"], "name": call["name"], "arguments": call["args"]}
                                 for call in message.tool_calls]
        if isinstance(message, ToolMessage):
            row["tool_call_id"] = message.tool_call_id
        output.append(row)
    return output


class _LegacyRuntimeClient(RuntimeClient):
    """Legacy subscriptions have no registered per-connection usage authorization."""
    def __init__(self, store, env, provider):
        super().__init__(store)
        self.env, self.provider = env, provider

    def call(self, snapshot, messages, *, max_tokens=4096, tools=None):
        from src.workflow.resources import memory_slot
        # Revalidate the legacy subscription restriction before every request.
        config = frozen_legacy_config("agent", env=self.env)
        path, body = encode(snapshot, messages, max_tokens, tools)
        body.update(_summary_provider_kwargs(config, disable_thinking=snapshot.get('purpose') == 'compaction').get('extra_body', {}))
        with memory_slot("text", limit=5 if self.provider == "minimax" else 2):
            started = time.monotonic()
            data = self._request(snapshot, path, body=body)
            try:
                return decode(snapshot["adapter"], data, time.monotonic() - started)
            except PlatformError as exc:
                if exc.code != 'OUTPUT_INCOMPLETE':
                    raise
                reason = (data.get('choices') or [{}])[0].get('finish_reason')
                reason = reason if reason in {'length', 'content_filter', 'error'} else 'unexpected'
                maximum = body.get('max_completion_tokens', body.get('max_tokens'))
                raise PlatformError('OUTPUT_INCOMPLETE',
                    f'模型未完整返回：finish_reason={reason}，max_output_tokens={maximum}；'
                    '未执行残缺工具调用，原历史和产物保留，未自动重试') from None


class RuntimeChatModel(BaseChatModel):
    """Keep credentials and transport objects out of Pydantic dumps and traces."""
    max_retries: int = 0
    _client: Any = PrivateAttr()
    _snapshot: dict = PrivateAttr()
    _emit: Any = PrivateAttr(default=None)

    @property
    def _llm_type(self):
        return "registered-runtime-chat"

    @property
    def _identifying_params(self):
        return {"model_ref": self._snapshot.get("model_ref"), "role": "agent"}

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        if tool_choice not in (None, "auto"):
            raise PlatformError("TOOL_CHOICE_UNSUPPORTED", "This adapter supports automatic tool selection only")
        if set(kwargs) - {"parallel_tool_calls", "strict"}:
            raise PlatformError("MODEL_OPTIONS_UNSUPPORTED", "Unsupported tool binding options")
        if kwargs.get("strict") or kwargs.get("parallel_tool_calls"):
            raise PlatformError("MODEL_OPTIONS_UNSUPPORTED", "Strict/parallel tool guarantees are not configured on this route")
        converted = [convert_to_openai_tool(tool)["function"] for tool in tools]
        return self.bind(tools=converted)

    def for_summary(self):
        summary = self.model_copy()
        summary._snapshot = {**self._snapshot, 'purpose': 'compaction',
                             'parameters': dict(self._snapshot.get('parameters') or {})}
        if (isinstance(self._client, _LegacyRuntimeClient) and self._client.provider == 'minimax'
                and self._snapshot['upstream_model_id'].lower() == 'minimax-m3'):
            summary._snapshot['parameters']['max_output_tokens'] = 8192
        return summary

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if stop:
            raise PlatformError("MODEL_OPTIONS_UNSUPPORTED", "Stop sequences are not configured for this route")
        maximum = int(self._snapshot.get('parameters', {}).get('max_output_tokens', 4096))
        if self._emit:
            self._emit({'type': 'model', 'status': 'started', 'purpose': self._snapshot.get('purpose', 'agent'),
                        'max_output_tokens': maximum})
        result = self._client.call(self._snapshot, _messages(messages),
            max_tokens=maximum,
            tools=kwargs.get("tools"))
        try:
            visible = _strip_reasoning_text(result.text)
        except ValueError:
            raise PlatformError('MODEL_OUTPUT_INVALID', '模型返回了未完整分隔的内部内容，未展示原文，请检查模型输出。') from None
        message = AIMessage(content=visible, tool_calls=[
            {"id": call["id"], "name": call["name"], "args": call["arguments"], "type": "tool_call"}
            for call in result.tool_calls],
            response_metadata={"finish_reason": result.finish_reason, "request_id": result.request_id,
                               "usage": result.usage})
        if self._emit:
            self._emit({"type": "model", "status": "completed", "tool_calls": len(result.tool_calls)})
        return ChatResult(generations=[ChatGeneration(message=message)])


def build_model(env: dict, emit=None) -> BaseChatModel:
    frozen = dict(env)
    frozen.update(freeze_run_environment(frozen, roles=("agent",)))
    config = platform_config("agent", env=frozen)
    model = RuntimeChatModel()
    if config is not None:
        snapshot = config.platform_snapshot
        store = PlatformStore(config.platform_directory, namespace=snapshot["namespace"], env=frozen)
        model._client, model._snapshot, model._emit = RuntimeClient(store), snapshot, emit
        return model
    config = frozen_legacy_config("agent", env=frozen) or legacy_controller(frozen)
    host = urlsplit(config.base_url).hostname
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    snapshot = {"adapter": "openai_chat", "base_url": config.base_url, "network": "local" if local else "public",
        "upstream_model_id": config.model, "model_ref": config.provider + ":" + config.model, "role": "agent",
        "parameters": {"max_output_tokens": minimax_output_tokens(config.model, 16384)} if config.provider == 'minimax' else {},
        "auth_mode": "bearer", "credential_ref": "env:DIALOGUE_LEGACY_API_KEY"}
    directory = Path(frozen.get("MODEL_PLATFORMS_DIR") or Path.cwd() / "data/model_platforms")
    store = PlatformStore(directory, namespace=frozen.get("MODEL_PLATFORMS_NAMESPACE", "workflow"),
        env={**frozen, "DIALOGUE_LEGACY_API_KEY": config.api_key})
    model._client = _LegacyRuntimeClient(store, frozen, config.provider)
    model._snapshot, model._emit = snapshot, emit
    return model
