from dataclasses import replace

import pytest

from src.config import LLMConfig
from src.news.daily_news import NewsItem
from src.workflow import create_post as w


FLIGHT_SOURCE = (
    "Israeli prime minister cautions against early conclusions when asked if there were any signs of Iranian involvement "
    "Benjamin Netanyahu has said it was too early to tell who was ultimately behind the apparent attempt to crash a "
    "Tel Aviv-bound flydubai flight, even as Israel's ambassador to the United Nations called the incident a terror attack, "
    "without apportioning blame. Danny Danon told Fox News on Wednesday night that Israel had no doubt it was a terror "
    "attack from a radical individual but could not link it to a terror state or organisation."
)
SETTLER_SOURCE = (
    "Settler violence in West Bank makes everyday life untenable for Palestinians, says ICRC after assault on family "
    "trying to return home in Jalud. More than 100 militant settlers overwhelmed Israeli security forces early on Tuesday "
    "to attack a Palestinian family as they attempted to reclaim their homes which had been seized by settlers in the "
    "West Bank village of Jalud. Three Israeli security officials were injured and houses were set on fire."
)
FLIGHT_TAIL = (
    "当被问及是否有伊朗方面介入的迹象时，内塔尼亚胡警告不要匆忙下结论。"
    "同日，阿联酋航空公司flydubai宣布暂停所有往返以色列的航班，这意味着该航司进出以色列的客运服务均已停止。"
)
SETTLER_TAIL = (
    "事件现场定居者人数众多，规模之大令以方到场人员难以控制局面。"
    '国际红十字委员会将这一袭击定性为"恐怖袭击"，并指出定居者暴力已使巴勒斯坦人在西岸的日常生活变得"难以维持"。'
)
SETTLER_LEAD = "超过100名定居者在约旦河西岸杰卢德村袭击试图返回被占住房的巴勒斯坦家庭，3名以色列安全人员受伤，房屋被纵火。"


def source(description=SETTLER_SOURCE):
    return NewsItem(
        title="Israeli forces overwhelmed by violent settlers in attack on Palestinian family",
        url="https://example.com/settlers", source="guardian", description=description,
        content="", seendate="2026-09-30T19:01:03+00:00", domain="example.com", language="en",
    )


def body(content):
    return f"内容：\n{content}\n\n评价：\n相关责任认定与后续处置仍待观察。\n\n日期：2026-09-30\n\n来源：guardian"


@pytest.mark.parametrize("title,tail", [
    ("内塔尼亚胡称航班事件责任方尚难判定", FLIGHT_TAIL),
    ("西岸定居者暴力袭击巴勒斯坦家庭", SETTLER_TAIL),
])
def test_real_run_contextless_bodies_fail_lead_gate(title, tail):
    # These bodies passed the old generic/thin gates despite missing the event.
    assert w._daily_news_quality_issue(title, body(tail)) == "missing_lead_event"


def test_real_english_source_keeps_supported_translated_numbers_and_lead():
    result = w._finalize_daily_news_body(body(SETTLER_LEAD + SETTLER_TAIL), source(), "国际新闻", title_hint="西岸定居者袭击返家家庭")
    assert result.startswith("内容：\n" + SETTLER_LEAD)
    assert "3名以色列安全人员受伤" in result


def test_real_flight_publication_date_does_not_prove_event_date_or_allow_silent_deletion():
    picked = replace(source(FLIGHT_SOURCE), seendate="2026-10-01T04:55:41+00:00")
    lead = "10月1日，内塔尼亚胡表示，一架飞往特拉维夫的flydubai航班疑遭人为坠机企图，目前尚无法判断事件最终责任方。"
    with pytest.raises(ValueError, match="unsupported_numeric_claim"):
        w._finalize_daily_news_body(body(lead + FLIGHT_TAIL), picked, "国际新闻", title_hint="内塔尼亚胡回应航班事件")


@pytest.mark.parametrize("evidence,claim", [
    ("Three Israeli security officials were injured.", "3名以色列安全人员受伤。"),
    ("Three officials were injured.", "三名官员受伤。"),
    ("Twenty-one passengers were rescued.", "21名乘客获救。"),
    ("More than 100 militant settlers attacked a family.", "超过100名定居者袭击一户家庭。"),
    ("At least three people were injured.", "至少3人受伤。"),
    ("The flight was delayed for three hours.", "航班延误3小时。"),
    ("三名人员受伤。", "3位人员受伤。"),
])
def test_supported_quantity_translations(evidence, claim):
    assert not w._daily_news_has_unsupported_numeric_claim(claim, source(evidence))


@pytest.mark.parametrize("evidence,claim", [
    ("Three officials were injured.", "30名官员受伤。"),
    ("The flight was delayed for three hours.", "3名乘客受伤。"),
    ("More than 100 settlers arrived.", "100名定居者抵达。"),
    ("30名人员受伤。", "3名人员受伤。"),
    ("1000名人员受伤。", "100名人员受伤。"),
    (SETTLER_SOURCE, "9000名定居者袭击家庭。"),
    ("10月1日公布消息。", "10月9日发生袭击。"),
    ("One hundred and three officials were injured.", "3名官员受伤。"),
    ("Nearly three hours elapsed.", "已过去3小时。"),
    ("Three hours delayed passengers.", "3名乘客被延误。"),
    ("二十名官员受伤。", "一二十名官员受伤。"),
    ("一万名人员参加。", "一万名人员参加。"),
])
def test_unverified_or_mismatched_quantities_are_not_accepted(evidence, claim):
    assert w._daily_news_has_unsupported_numeric_claim(claim, source(evidence))


def test_unverified_quantity_never_discards_whole_sentence_into_publishable_tail():
    with pytest.raises(ValueError, match="unsupported_numeric_claim"):
        w._finalize_daily_news_body(body("9000名定居者袭击巴勒斯坦家庭。" + SETTLER_TAIL), source(), "国际新闻")


def test_fact_dedupe_keeps_lead_despite_shared_characters():
    lead = "西岸定居者暴力袭击巴勒斯坦家庭。"
    result = w._dedupe_daily_news_fact_sentences(lead + SETTLER_TAIL)
    assert result == lead + SETTLER_TAIL


def test_fact_dedupe_keeps_distinct_actions_about_same_named_subject():
    text = "公司发布《交通计划》，公交将在周末加开班次。议会审议《交通计划》，要求补充预算来源。"
    assert w._dedupe_daily_news_fact_sentences(text) == text


def test_fact_dedupe_removes_exact_repeat_but_keeps_first_lead():
    lead = "西岸定居者暴力袭击巴勒斯坦家庭。"
    assert w._dedupe_daily_news_fact_sentences(lead + lead + "房屋被纵火。") == lead + "房屋被纵火。"


def test_lead_gate_accepts_complete_event_and_later_contextual_detail():
    complete = "内塔尼亚胡回应飞往特拉维夫航班疑遭人为坠机企图，表示责任方尚难判定。" + FLIGHT_TAIL
    assert not w._daily_news_content_lacks_lead_event(body(complete))
    assert not w._daily_news_content_lacks_lead_event(body(SETTLER_LEAD + SETTLER_TAIL))


@pytest.mark.parametrize("repair_valid", [True, False])
def test_candidate_rewrites_unverified_quantity_once_and_rechecks_it(monkeypatch, repair_valid):
    picked = source()
    good = SETTLER_LEAD + "红十字国际委员会在这起袭击后表示，定居者暴力已使巴勒斯坦人的日常生活难以维持；受袭家庭原本试图回到被定居者占据的住所，但到场的以色列安全人员因定居者人数众多未能控制局面。"
    invalid = good.replace("100名", "9000名")
    drafts = [invalid, good if repair_valid else invalid]
    calls = []

    def fake_model(*args, **kwargs):
        calls.append(kwargs["prompt_hint"])
        return {"title": "西岸定居者袭击返家家庭", "body": body(drafts.pop(0)), "topics": ["每日新闻"]}

    monkeypatch.setattr(w, "generate_draft", fake_model)
    queues = w.ModelWorkQueues(llm_workers=1, image_workers=1)
    try:
        result = w._prepare_daily_news_candidate(
            candidate_index=1, picked=picked, cfgs=[LLMConfig(provider="fake", model="fake", api_key="test")],
            asset_paths=[], copy_assets=False, auto_image_enabled=False, prompt_norm="国际新闻",
            viewpoint_norm="无视角评价", target_count=1, single_material_mode=False, base_meta={},
            progress_callback=None, model_queues=queues, post_quality_callback=None,
            prepared=(picked, {}, {}, picked),
        )
    finally:
        queues.close()
    assert len(calls) == 2
    assert "unsupported_numeric_claim" in calls[1]
    if repair_valid:
        assert result.status == "success", (result.reason, result.draft)
        assert result.post.body.startswith("内容：\n" + SETTLER_LEAD)
        assert "9000" not in result.post.body
    else:
        assert result.status == "skipped"
        assert result.reason == "unsupported_numeric_claim"
        assert result.failed_post_saved
        assert result.post.status == w.PostStatus.failed
        assert result.post.uploaded is False
        assert result.post.assets == []
        assert "9000名定居者" in result.post.body
        assert result.post.platform["batch_selection"]["status"] == "quality_rejected"
        diagnostics = result.post.platform["news"]["quality_rejection"]
        assert diagnostics["reason"] == "unsupported_numeric_claim"
        assert len(diagnostics["attempts"]) == 2
        assert all(attempt["raw_draft"]["body"] == body(invalid) for attempt in diagnostics["attempts"])
        assert any(token[0] == "9000" for token in diagnostics["attempts"][0]["numeric"]["difference"])
