import importlib
import json
from types import SimpleNamespace

from src.config import LLMConfig
from src.workflow.create_post import _normalize_daily_news_image_event
import pytest


def test_writer_requests_subject_action_before_conceptual_composition(monkeypatch):
    generate = importlib.import_module("src.llm.generate")
    fact = "英国央行行长警告人工智能热潮可能引发市场冲击。"
    scene = fact + "英国央行行长警告人工智能热潮的概念示意，以非写实侧影表达立场，背景为纯色色面，物面无字。"
    answer = {"title": "英国央行警告AI市场风险", "body": "内容：\n" + fact + "\n\n评价：\n仍需关注风险。",
              "topics": [], "image_event": scene}
    calls = []

    def invoke(messages):
        calls.append(messages)
        return SimpleNamespace(content=json.dumps(answer, ensure_ascii=False))

    monkeypatch.setattr(generate, "init_chat_model", lambda *args, **kwargs: SimpleNamespace(invoke=invoke))
    draft = generate.generate_draft(LLMConfig(provider="fake", model="fake", api_key="test"),
                                    title_hint="每日新闻", prompt_hint=fact, asset_paths=[])
    system = calls[0][0].content
    assert "second sentence MUST start with the concrete source subject and action clause" in system
    assert "Do not start the second sentence with composition, background or a generic concept label" in system
    assert "Thursday means 周四, not a calendar date derived from metadata" in system
    assert draft["body"] == answer["body"]
    assert _normalize_daily_news_image_event(scene, picked=SimpleNamespace(title=fact, description=fact),
                                            title=draft["title"], body=draft["body"], prompt_norm="每日新闻") == scene


def test_single_sentence_editorial_scene_keeps_verified_subject_and_action():
    fact = "英国央行行长警告人工智能热潮可能引发市场冲击。"
    scene = "英国央行行长警告人工智能热潮的概念示意，以非写实侧影表达立场，背景为纯色色面。"
    audit = {}
    assert _normalize_daily_news_image_event(scene, picked=SimpleNamespace(title=fact, description=fact),
                                            title="英国央行警告AI市场风险", body="内容：\n" + fact,
                                            prompt_norm="每日新闻", audit=audit) == scene
    assert audit["accepted"] is True
    assert audit["evidence_sentence"] in fact
    assert audit["supported_anchors"]


@pytest.mark.parametrize("visual", [
    "英国央行行长的概念示意，以非写实侧影表达立场。",
    "英国央行行长驾驶赛车的概念示意，背景为纯色色面。",
    "概念示意图采用纯色背景，以无名人物侧影表达立场。",
])
def test_editorial_scene_still_needs_source_action_not_only_copied_provenance(visual):
    fact = "英国央行行长警告人工智能热潮可能引发市场冲击。"
    for scene in (visual, fact + visual):
        assert _normalize_daily_news_image_event(scene, picked=SimpleNamespace(title=fact, description=fact),
                                                title="英国央行警告AI市场风险", body="内容：\n" + fact,
                                                prompt_norm="每日新闻") == ""
