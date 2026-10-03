from __future__ import annotations

import importlib
import json
import socket
from types import SimpleNamespace

import pytest

from src.config import LLMConfig
from src.news.daily_news import NewsItem
from src.workflow import create_post

generate = importlib.import_module("src.llm.generate")
TITLE = "内塔尼亚胡称航班事件责任方尚难判定"
FACTS = "内塔尼亚胡警告不要匆忙下结论，航班事件责任方尚难判定。"
SCENE = FACTS + "内塔尼亚胡警告不要匆忙下结论的概念示意，以非写实侧影表达立场，背景为简洁纯色色面，人物与物件表面留白。"


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    def denied(*args, **kwargs):
        raise AssertionError("scene tests must not use the network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(create_post, "post_dir", lambda pid: tmp_path / pid)
    monkeypatch.setattr(create_post, "save_post", lambda post: None)
    monkeypatch.setattr(create_post, "save_revision", lambda revision: None)


def normalize(scene, *, title=TITLE, facts=FACTS, opinion="仍待核实。", audit=None):
    kwargs = dict(picked=SimpleNamespace(title="Unrelated scraped recommendation", content="气候行动"),
                  title=title, body=f"内容：\n{facts}\n\n评价：\n{opinion}", prompt_norm="国际新闻")
    if audit is not None:
        kwargs["audit"] = audit
    return create_post._normalize_daily_news_image_event(scene, **kwargs)


def test_named_speaker_scene_survives_short_headline_mismatch():
    assert not create_post._daily_news_text_matches_context(SCENE[len(FACTS):], TITLE)
    assert normalize(SCENE) == SCENE


def test_body_supported_scene_survives_different_headline_focus():
    scene = "沙特达曼机场停机坪上，一架客机已停稳，起落架轮胎接触地面。"
    facts = "涉事客机备降沙特达曼机场，客机停稳后接受检查。"
    assert normalize(scene, title="航班事件调查仍在进行", facts=facts) == scene


@pytest.mark.parametrize("scene", [
    "气候行动者在海边举牌讨论气候政策。",
    "英国央行行长公开回应市场风险，背景留白。",
    "内塔尼亚胡驾驶赛车参加比赛，场边观众欢呼。",
])
def test_unrelated_scene_is_not_saved_even_if_opinion_or_scraped_page_mentions_it(scene):
    assert normalize(scene, opinion=scene) == ""


@pytest.mark.parametrize("scene,reason", [("", "missing_scene"), ("气候行动者在海边举牌。", "unrelated_scene")])
def test_fallback_is_traceable(scene, reason):
    audit = {}
    assert normalize(scene, audit=audit) == ""
    assert audit["input"] == scene
    assert audit["normalized"] == ""
    assert audit["reason"] == reason
    assert audit["accepted"] is False


def test_supported_scene_audit_preserves_full_input_and_reason():
    audit = {}
    assert normalize(SCENE, audit=audit) == SCENE
    assert audit["input"] == audit["normalized"] == SCENE
    assert audit["reason"] in {"body_supported", "entity_supported", "editorial_source_supported"}
    assert audit["accepted"] is True


@pytest.mark.parametrize("column", ["daily_news", "daily_wow"])
def test_actual_column_prompt_requires_scene_without_optional_contract(column):
    picked = NewsItem(title=TITLE, url="https://example.test/news", description=FACTS,
                      content=FACTS, seendate="2026-10-01 08:00:00", source="test", domain="example.test")
    prompt = create_post._daily_news_prompt(picked, "国际新闻", column=column)
    assert "必填 keys: title, body, topics, image_event" in prompt
    assert "可选 key: image_event" not in prompt
    assert "必须在本次写稿调用中一同生成" in prompt


@pytest.mark.parametrize("news", [True, False])
def test_writer_contract_and_parser_preserve_scene_in_same_call(monkeypatch, news):
    calls = []
    response = dict(title=TITLE, body=f"内容：\n{FACTS}\n\n评价：\n仍待核实。", topics=[], image_event=SCENE)
    def invoke(messages):
        calls.append(messages)
        return SimpleNamespace(content=json.dumps(response, ensure_ascii=False))
    monkeypatch.setattr(generate, "init_chat_model", lambda *a, **kw: SimpleNamespace(invoke=invoke))
    result = generate.generate_draft(LLMConfig(provider="fake", model="fake", api_key="test"),
                                    title_hint="每日新闻" if news else "生活记录",
                                    prompt_hint="每日新闻：描述真实单场景" if news else "生活记录",
                                    asset_paths=[])
    assert len(calls) == 1
    assert result["image_event"] == SCENE
    assert result["body"] == response["body"]
    system, user = [m.content for m in calls[0]]
    if news:
        assert "image_event is required" in system
        assert "single visible scene" in system
        assert "optionally image_event" not in user
        assert "Optional JSON key: image_event" not in system
    else:
        assert "Optional JSON key: image_event" in system


@pytest.mark.parametrize("rewrite", [False, True])
def test_candidate_persists_writer_and_rewrite_scene_audit(monkeypatch, rewrite):
    picked = NewsItem(title=TITLE, url="https://example.test/news", description=FACTS,
                      content=FACTS, seendate="2026-10-01 08:00:00", source="test", domain="example.test", language="zh")
    initial_scene = "气候行动者在海边举牌。" if rewrite else SCENE
    drafts = [dict(title=TITLE, body=f"内容：\n{FACTS}\n\n评价：\n仍待核实。", topics=[], image_event=initial_scene)]
    if rewrite:
        drafts.append(dict(drafts[0], image_event=SCENE))
    monkeypatch.setattr(create_post, "generate_draft", lambda *a, **kw: drafts.pop(0))
    checks = iter(["thin_content", ""] if rewrite else [""])
    monkeypatch.setattr(create_post, "_daily_news_quality_issue", lambda *a: next(checks))
    monkeypatch.setattr(create_post, "_daily_news_context_is_incomplete", lambda *a: False)
    monkeypatch.setattr(create_post, "_daily_news_body_is_too_generic", lambda *a: False)
    monkeypatch.setattr(create_post, "_daily_news_lead_lacks_headline_anchor", lambda *a: False)
    monkeypatch.setattr(create_post, "_daily_news_content_is_too_thin", lambda *a: False)
    monkeypatch.setattr(create_post, "_finalize_daily_news_body", lambda body, *a, **kw: body)
    monkeypatch.setattr(create_post, "_repair_daily_news_mismatched_comment", lambda body, *a, **kw: body)
    queue = create_post.ModelWorkQueues(llm_workers=1, image_workers=1)
    try:
        result = create_post._prepare_daily_news_candidate(
            candidate_index=1, picked=picked, cfgs=[LLMConfig(provider="fake", model="fake", api_key="test")],
            asset_paths=[], copy_assets=False, auto_image_enabled=False, prompt_norm="国际新闻",
            viewpoint_norm="无视角评价", target_count=1, single_material_mode=False, base_meta={},
            progress_callback=None, model_queues=queue, post_quality_callback=None, prepared=(picked, {}, {}, picked),
        )
    finally:
        queue.close()
    assert result.status == "success"
    assert result.post.platform["news"]["image_event"] == SCENE
    audit = result.post.platform["news"]["image_event_audit"]
    assert audit["writer_value"] == initial_scene
    assert audit.get("rewrite_value") == (SCENE if rewrite else None)
    assert audit["input"] == audit["normalized"] == SCENE
    assert audit["accepted"] is True
    assert result.draft["image_event_audit"] == audit
