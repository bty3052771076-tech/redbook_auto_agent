"""Provider-internal reasoning is not public assistant text."""
import httpx
from langchain_core.messages import HumanMessage
import pytest

from src.model_platforms.security import PlatformError
from test_conversational_agent_cancellation import no_external_work
from test_conversational_agent_model import configured_model


@pytest.mark.parametrize('tool_call', [False, True])
def test_provider_reasoning_is_removed_without_dropping_tool_calls(tmp_path, tool_call):
    def respond(request):
        message = {'content': '<think>private-internal-fixture</think>' + ('' if tool_call else 'Public answer.')}
        if tool_call:
            message['tool_calls'] = [{'id': 'fixture-tool', 'type': 'function',
                'function': {'name': 'capabilities', 'arguments': '{}'}}]
        return httpx.Response(200, json={'choices': [{'message': message,
            'finish_reason': 'tool_calls' if tool_call else 'stop'}]})
    model, _ = configured_model(tmp_path, 'openai_chat', respond)
    result = model.invoke([HumanMessage(content='Read-only output fixture.')])
    assert result.content == ('' if tool_call else 'Public answer.')
    assert len(result.tool_calls) == int(tool_call)
    if tool_call:
        assert result.tool_calls[0]['id'] == 'fixture-tool'


@pytest.mark.parametrize('finish_reason,code', [('stop', 'MODEL_OUTPUT_INVALID'), ('length', 'OUTPUT_INCOMPLETE')])
def test_incomplete_reasoning_boundary_is_not_displayed_or_retried(tmp_path, finish_reason, code):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'content': '<think>private-incomplete-fixture'},
                                                    'finish_reason': finish_reason}]})
    model, _ = configured_model(tmp_path, 'openai_chat', respond)
    with pytest.raises(PlatformError, match=code):
        model.invoke([HumanMessage(content='Read-only output fixture.')])
    assert len(requests) == 1
