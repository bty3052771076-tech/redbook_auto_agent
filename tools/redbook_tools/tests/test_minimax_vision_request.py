from __future__ import annotations

from types import SimpleNamespace

from PIL import Image

from src.config import LLMConfig
from src.workflow.vision_review import invoke_vision_review


def test_m3_vision_request_disables_thinking_without_changing_score_gate(monkeypatch, tmp_path):
    image_path = tmp_path / "sample.png"
    Image.new("RGB", (16, 16), "white").save(image_path)
    recorded = {}

    class FakeCompletions:
        def create(self, **kwargs):
            recorded.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content='{"ok":true,"score":80,"issues":[],"retry_prompt":""}'),
            )])

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    config = LLMConfig(model="MiniMax-M3", api_key="test-only", base_url="https://example.invalid/v1", provider="minimax")

    result = invoke_vision_review(config, prompt="Review this image", image_path=image_path)

    assert '"score":80' in result
    assert recorded["extra_body"]["thinking"] == {"type": "disabled"}
    assert recorded["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
