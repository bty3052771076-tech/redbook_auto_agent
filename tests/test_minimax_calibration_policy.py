import json

import httpx
import pytest

from backend import task_recognition
from src.config import LLMConfig


@pytest.mark.parametrize("model", ["MiniMax-M3.1-Flash", "MiniMax-M3.1-Flash-Preview", "MiniMax-M3"])
def test_calibration_uses_flash_medium_and_model_output_cap_without_changing_m3(monkeypatch, model):
    monkeypatch.delenv("MINIMAX_REASONING_EFFORT", raising=False)
    sent = []
    client_class = httpx.Client

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": '{"jobs":[]}'}}
        ]})

    monkeypatch.setattr(task_recognition.httpx, "Client",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs))
    result = task_recognition.call_model(
        LLMConfig(model, "test-key", base_url="https://example.com/v1", provider="minimax"),
        {"message": "generate news"},
    )
    assert result == '{"jobs":[]}'
    assert len(sent) == 1
    if model == "MiniMax-M3":
        assert sent[0]["max_tokens"] == 4096
        assert "max_completion_tokens" not in sent[0]
        assert "reasoning_effort" not in sent[0]
        assert sent[0]["thinking"] == {"type": "disabled"}
    else:
        assert sent[0]["max_completion_tokens"] == 131072
        assert "max_tokens" not in sent[0]
        assert sent[0].get("reasoning_effort") == "medium"
        assert sent[0].get("thinking") != {"type": "disabled"}
