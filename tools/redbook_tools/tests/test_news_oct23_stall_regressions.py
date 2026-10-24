from types import SimpleNamespace

import pytest

from src.workflow import create_post as w


def source(text):
    return SimpleNamespace(title="Source report", description=text, content="")


@pytest.mark.parametrize("evidence,claim", [
    ("前三季度利润超过2025年全年水平。", "利润超过2025年全年水平。"),
    ("He hopes to race in the early 2030s.", "他希望在2030年代初参赛。"),
    ("Thousands have been evacuated as a typhoon approaches.", "台风接近时数千人撤离。"),
    ("A new public awareness campaign was launched.", "发起一项公众宣传活动。"),
    ("US regulator probes oil trading, official says.", "一名官员称监管机构调查石油交易。"),
])
def test_supported_translation_is_not_rejected_as_an_invented_quantity(evidence, claim):
    assert not w._daily_news_has_unsupported_numeric_claim(claim, source(evidence))


@pytest.mark.parametrize("evidence,claim", [
    ("He hopes to race in the early 2030s.", "他将在2030年参赛。"),
    ("Thousands have been evacuated.", "1000人已撤离。"),
    ("Thousands of files were deleted.", "数千人撤离。"),
    ("A public awareness campaign was launched.", "发起三项公众宣传活动。"),
    ("Officials denied the allegation.", "首位官员确认了指控。"),
    ("工程历时2025年。", "工程在2025年完成。"),
])
def test_numeric_repair_does_not_approve_unproven_precision(evidence, claim):
    assert w._daily_news_has_unsupported_numeric_claim(claim, source(evidence))


def test_scene_repair_uses_whole_accepted_fact_and_retains_original_rejection():
    facts = "近期，多家银行调整积存金收费情况，部分银行将手续费降至零。"
    body = f"内容：\n{facts}\n\n评价：\n消费者应核对优惠期限与后续费用。"
    draft = {"title": "银行下调积存金手续费", "body": body,
             "image_event": "银行员工在新闻发布会上宣布优惠。"}
    audit = {"reason": "unsupported_scene_setting", "accepted": False,
             "input": draft["image_event"]}
    result = w._repair_daily_news_scene_from_facts(draft, picked=source(facts), prompt_norm="", audit=audit)
    assert result["body"] == body
    assert result["image_event"].startswith(facts)
    assert "概念示意" in result["image_event"]
    assert "新闻发布会" not in result["image_event"]
    assert audit["accepted"]
    assert audit["fallback_from"]["reason"] == "unsupported_scene_setting"


def test_scene_repair_does_not_turn_a_plan_into_an_accomplished_event():
    facts = "市政府计划修建地铁，项目尚未开工。"
    body = f"内容：\n{facts}\n\n评价：\n沿线居民仍需了解具体规划。"
    result = w._repair_daily_news_scene_from_facts(
        {"title": "地铁建设计划公布", "body": body, "image_event": "地铁已经通车。"},
        picked=source(facts), prompt_norm="", audit={"reason": "unsupported_scene_state"})
    assert "已经通车" not in result["image_event"]
    assert facts in result["image_event"]


def test_scene_repair_requires_an_actual_complete_content_sentence():
    assert w._repair_daily_news_scene_from_facts(
        {"title": "新闻", "body": "评价：\n地铁已经通车。", "image_event": "旧场景"},
        picked=source(""), prompt_norm="", audit={}) is None
