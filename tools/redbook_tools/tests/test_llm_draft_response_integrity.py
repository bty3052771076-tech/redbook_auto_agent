import json
from types import SimpleNamespace

import pytest

from src.config import LLMConfig
from src.llm import generate as g


ANSWER = json.dumps({"title": "test", "body": "confirmed content", "topics": [], "image_event": "scene"})


def call_draft(monkeypatch, content=ANSWER, *, finish="stop", provider="minimax", model="MiniMax-M3", usage=None):
    calls = []

    def factory(name, **kwargs):
        calls.append({"model": name, **kwargs})
        return SimpleNamespace(invoke=lambda messages: SimpleNamespace(
            content=content,
            response_metadata={"finish_reason": finish, "private": "never retain"},
            usage_metadata=usage or {},
        ))

    monkeypatch.setenv("LLM_TRANSIENT_RETRY_MAX", "0")
    monkeypatch.setattr(g, "init_chat_model", factory)
    cfg = LLMConfig(model=model, api_key="test-key", base_url="https://example.invalid/v1", provider=provider)
    result = g.generate_draft(cfg, title_hint="test", prompt_hint="facts", asset_paths=[])
    return result, calls


def test_plain_complete_response_remains_usable(monkeypatch):
    result, _ = call_draft(monkeypatch)
    assert result["body"] == "confirmed content"
    assert not result.get("_fallback_error")


def test_closed_thinking_is_removed_before_json_selection(monkeypatch):
    text = '<think>{"title":"wrong", "body":"private reasoning"}</think>' + ANSWER
    result, _ = call_draft(monkeypatch, text)
    assert result["body"] == "confirmed content"
    assert "private reasoning" not in json.dumps(result)


@pytest.mark.parametrize("text", [
    '<think>{"title":"wrong","body":"private reasoning"}</think>',
    '<think>analysis without final answer',
    '<think>{"body":"analysis only"}',
    'analysis</think>',
])
def test_thinking_without_final_answer_is_not_publishable(monkeypatch, text):
    result, calls = call_draft(monkeypatch, text)
    assert result.get("_fallback_error")
    assert result["body"] == ""
    assert len(calls) == 1


@pytest.mark.parametrize("finish", ["length", "content_filter"])
def test_incomplete_response_cannot_be_published_even_if_json_parses(monkeypatch, finish):
    result, calls = call_draft(monkeypatch, finish=finish)
    assert result.get("_fallback_error")
    assert result["body"] == ""
    assert len(calls) == 1
    assert result["_response_diagnostics"]["finish_reason"] == finish


def test_m3_draft_request_disables_thinking_and_splits_reasoning(monkeypatch):
    _, calls = call_draft(monkeypatch)
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}, "reasoning_split": True}


def test_m31_never_requests_unsupported_disabled_thinking(monkeypatch):
    _, calls = call_draft(monkeypatch, model="MiniMax-M3.1-Flash-Preview")
    assert calls[0]["extra_body"] == {"reasoning_split": True}


def test_other_providers_do_not_receive_minimax_parameters(monkeypatch):
    _, calls = call_draft(monkeypatch, provider="custom", model="other")
    assert "extra_body" not in calls[0]


def test_response_diagnostics_preserve_safe_usage_without_private_fields(monkeypatch):
    result, _ = call_draft(monkeypatch, usage={
        "input_tokens": 100, "output_tokens": 250, "total_tokens": 350,
        "output_token_details": {"reasoning": 200}, "private": "never retain",
    })
    diag = result["_response_diagnostics"]
    assert diag["input_tokens"] == 100
    assert diag["output_tokens"] == 250
    assert diag["reasoning_tokens"] == 200
    assert diag["requested_max_tokens"] == 5024
    assert "private" not in diag and "test-key" not in json.dumps(diag)


def test_parser_never_recovers_a_json_object_from_thinking():
    assert g._parse_json_text('<think>{"body":"private reasoning"}</think>') is None


def test_structured_reasoning_blocks_are_not_publishable_content(monkeypatch):
    result, _ = call_draft(monkeypatch, [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": '{"body":"PRIVATE"}'}]},
        {"type": "text", "text": "confirmed content"},
    ])
    assert result["body"] == "confirmed content"


def test_json_parser_preserves_literal_tags_inside_valid_string():
    assert g._parse_json_text('{"body":"<think>visible text</think>"}')["body"] == "<think>visible text</think>"


def test_json_summary_discards_reasoning(monkeypatch):
    calls = []
    def factory(name, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(invoke=lambda messages: SimpleNamespace(
            content='<think>{"wrong":true}</think>{"answer":"confirmed"}',
            response_metadata={"finish_reason": "stop"}, usage_metadata={},
        ))
    monkeypatch.setattr(g, "init_chat_model", factory)
    result = g.generate_json(LLMConfig("MiniMax-M3", "test-key", provider="minimax"), system_prompt="facts", user_prompt="summarize")
    assert result["answer"] == "confirmed"
    assert calls[0]["extra_body"] == {"reasoning_split": True}


def test_json_summary_rejects_truncated_response(monkeypatch):
    monkeypatch.setattr(g, "init_chat_model", lambda *args, **kwargs: SimpleNamespace(invoke=lambda messages: SimpleNamespace(
        content='{"answer":"incomplete"}', response_metadata={"finish_reason": "length"}, usage_metadata={},
    )))
    with pytest.raises(RuntimeError, match="length"):
        g.generate_json(LLMConfig("MiniMax-M3", "test-key", provider="minimax"), system_prompt="facts", user_prompt="summarize")
