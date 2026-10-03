from types import SimpleNamespace

from src.config import LLMConfig
from src.news.daily_news import NewsItem
from src.workflow import create_post
from src.workflow.create_post import (
    _daily_news_content_lacks_lead_event,
    _daily_news_lead_lacks_headline_anchor,
    _daily_news_content_is_too_thin,
    _limit_daily_news_content,
)


def test_long_source_cannot_be_reduced_to_one_fact_fragment():
    source = SimpleNamespace(description="A" * 500, content="")
    body = "内容：\n法庭近日听取了控方证人的证词。\n\n评价：\n案件仍在审理。\n\n日期：2026-09-28\n\n来源：guardian"

    assert _daily_news_content_is_too_thin(body, source)


def test_short_source_does_not_force_unsupported_detail():
    source = SimpleNamespace(description="简短公告：产品今日开放。", content="")
    body = "内容：\n公司今日开放该产品。\n\n评价：\n需观察使用情况。\n\n日期：2026-09-29\n\n来源：公告"

    assert not _daily_news_content_is_too_thin(body, source)


def test_news_must_open_with_the_event_not_an_inference():
    bad = "内容：\n这意味着短期内贸易摩擦仍将继续，相关市场面临扰动。\n\n评价：\n后续待观察。"
    good = "内容：\n美国对加拿大酒类和乳制品实施的禁令今天生效。\n\n评价：\n后续待观察。"

    assert _daily_news_content_lacks_lead_event(bad)
    assert not _daily_news_content_lacks_lead_event(good)


def test_news_rejects_contextless_lead_and_missing_headline_action():
    company = "内容：\n该公司同时宣布，埃菲尔铁塔负责人将因此事辞职。\n\n评价：\n仍待观察。"
    ruling = "内容：\n最高法院此次裁定意味着她此前的救济途径已用尽。\n\n评价：\n仍待观察。"
    piracy = "内容：\n事件发生在索马里沿海，海盗活动近年来增加。\n\n评价：\n航运风险仍存。"

    assert _daily_news_content_lacks_lead_event(company)
    assert _daily_news_content_lacks_lead_event(ruling)
    assert _daily_news_content_lacks_lead_event(piracy)
    assert _daily_news_lead_lacks_headline_anchor("美最高法驳回暂缓行刑申请", ruling)


def test_lead_must_name_the_headline_event():
    title = "澳储行加息至15年最高水平"
    tail_only = "内容：\n澳行行长米歇尔表示，董事会清楚这一决定将让部分民众承压。\n\n评价：\n后续仍待观察。"
    event_first = "内容：\n澳大利亚储备银行宣布加息至15年来最高水平，相关贷款成本随之上升。\n\n评价：\n后续仍待观察。"

    assert _daily_news_lead_lacks_headline_anchor(title, tail_only)
    assert not _daily_news_lead_lacks_headline_anchor(title, event_first)


def test_suspension_headline_cannot_pass_with_only_background():
    title = "印尼五名监狱官员因豪华囚室被停职"
    background = "内容：\n调查发现监狱中有配备空调和电视的豪华囚室，长期存在囚犯特权争议。\n\n评价：\n后续需要关注。"
    event_first = "内容：\n印尼五名监狱官员因豪华囚室事件被停职，调查人员发现囚室配有空调和电视。\n\n评价：\n后续需要关注。"

    assert _daily_news_lead_lacks_headline_anchor(title, background)
    assert not _daily_news_lead_lacks_headline_anchor(title, event_first)


def test_news_content_keeps_supported_facts_beyond_old_limit():
    sentences = [
        "市政府公布新规，要求公交线路在周末增加夜间班次。",
        "交通部门解释，调整覆盖三条通勤线路和两个换乘站。",
        "运营公司表示，首批车辆将在下月完成设备调试。",
        "工会代表担心，司机轮班时间需要同步重新协商。",
        "乘客协会提出，站点照明和候车安全也应纳入计划。",
        "财政文件显示，这项措施的预算来自现有交通专项资金。",
        "议会委员会计划在月底审查实施进度与投诉数据。",
        "当地高校将提供客流调查，结果预计在年底公布。",
    ]
    text = "".join(sentences)

    result = _limit_daily_news_content(text)

    assert len(result) > 150
    assert len(result) <= 320
    assert sentences[4] in result


def test_candidate_rewrites_thin_copy_once_without_relaxing_review(monkeypatch):
    facts = (
        "市政府公布公交调整方案，要求三条通勤线路在周末增加夜间班次。"
        "交通部门说明两个换乘站将同步调整末班车时间，首批车辆下月调试。"
        "议会委员会计划月底审查实施进度，乘客协会要求补充站点照明。"
        "运营公司表示新增班次将先在中心城区试行，试行结果将在季度报告中公布。"
        "工会代表提出司机轮班安排需要协商，财政文件列出了现有交通专项资金来源。"
    )
    picked = NewsItem(
        title="市政府公布周末公交夜间班次调整方案",
        url="https://example.com/bus-plan",
        description=facts,
        content=facts * 3,
        seendate="2026-09-29 08:00:00",
        source="市政府",
        domain="example.com",
        language="zh",
    )
    generated = [
        {"title": "市政府调整周末公交班次", "body": "内容：\n市政府公布公交调整方案。\n\n评价：\n仍待落实。", "topics": ["每日新闻"]},
        {"title": "市政府公布周末公交调整方案", "body": f"内容：\n{facts}\n\n评价：\n末班车安排仍需观察。", "topics": ["每日新闻"]},
    ]
    prompts = []

    def fake_generate_draft(*_args, **kwargs):
        prompts.append(kwargs["prompt_hint"])
        return generated.pop(0)

    monkeypatch.setattr(create_post, "generate_draft", fake_generate_draft)
    monkeypatch.setattr(create_post, "_daily_news_quality_issue", lambda *_args: "")
    monkeypatch.setattr(create_post, "_daily_news_context_is_incomplete", lambda *_args: False)
    monkeypatch.setattr(create_post, "_daily_news_body_is_too_generic", lambda *_args: False)
    monkeypatch.setattr(create_post, "_finalize_daily_news_body", lambda body, *_args, **_kwargs: body)
    monkeypatch.setattr(create_post, "_repair_daily_news_mismatched_comment", lambda body, *_args, **_kwargs: body)
    queue = create_post.ModelWorkQueues(llm_workers=1, image_workers=1)
    try:
        result = create_post._prepare_daily_news_candidate(
            candidate_index=1, picked=picked,
            cfgs=[LLMConfig(provider="fake", model="fake", api_key="test")],
            asset_paths=[], copy_assets=False, auto_image_enabled=False,
            prompt_norm="公交", viewpoint_norm="无视角评价", target_count=1,
            single_material_mode=False, base_meta={}, progress_callback=None,
            model_queues=queue, post_quality_callback=None,
            prepared=(picked, {}, {}, picked),
        )
    finally:
        queue.close()

    assert result.status == "success"
    assert len(prompts) == 2
    assert "上一版未通过质量检查" in prompts[1]
    assert "两个换乘站" in result.post.body


def test_candidate_stops_after_one_failed_rewrite(monkeypatch):
    picked = NewsItem(
        title="市政府公布周末公交夜间班次调整方案",
        url="https://example.com/bus-plan",
        description=(
            "市政府公布公交调整方案，交通部门将调整三个公交站点。"
            "运营公司计划下月对首批车辆进行调试，议会月底审查实施进度。"
            "乘客协会要求补充照明和候车安全措施，工会代表提出轮班问题。"
            "财政文件列出资金来源，试行结果将在季度报告中公布。"
        ),
        content="市政府公布公交调整方案，交通部门将调整三个公交站点。" * 16,
        seendate="2026-09-29 08:00:00", source="市政府",
        domain="example.com", language="zh",
    )
    calls = []

    def fake_generate_draft(*_args, **kwargs):
        calls.append(kwargs["prompt_hint"])
        return {"title": "市政府调整周末公交班次", "body": "内容：\n市政府公布公交调整方案。\n\n评价：\n仍待落实。", "topics": ["每日新闻"]}

    monkeypatch.setattr(create_post, "generate_draft", fake_generate_draft)
    monkeypatch.setattr(create_post, "_daily_news_quality_issue", lambda *_args: "")
    monkeypatch.setattr(create_post, "_daily_news_context_is_incomplete", lambda *_args: False)
    monkeypatch.setattr(create_post, "_daily_news_body_is_too_generic", lambda *_args: False)
    monkeypatch.setattr(create_post, "_finalize_daily_news_body", lambda body, *_args, **_kwargs: body)
    monkeypatch.setattr(create_post, "_repair_daily_news_mismatched_comment", lambda body, *_args, **_kwargs: body)
    queue = create_post.ModelWorkQueues(llm_workers=1, image_workers=1)
    try:
        result = create_post._prepare_daily_news_candidate(
            candidate_index=1, picked=picked,
            cfgs=[LLMConfig(provider="fake", model="fake", api_key="test")],
            asset_paths=[], copy_assets=False, auto_image_enabled=False,
            prompt_norm="公交", viewpoint_norm="无视角评价", target_count=1,
            single_material_mode=False, base_meta={}, progress_callback=None,
            model_queues=queue, post_quality_callback=None,
            prepared=(picked, {}, {}, picked),
        )
    finally:
        queue.close()

    assert result.status == "skipped"
    assert result.reason == "thin_content"
    assert len(calls) == 2


def test_live_blog_roundup_is_not_a_single_news_candidate(monkeypatch):
    picked = NewsItem(
        title="Trump comments on Iran as talks continue – as it happened",
        url="https://example.com/us-news/live/2026/sep/29/updates",
        description=(
            "This live blog is now closed. The US Senate will vote on a college sports bill. "
            "The bill deals with university athletes and transfer eligibility."
        ),
        seendate="2026-09-29 08:00:00", source="Example",
        domain="example.com", language="en",
    )
    monkeypatch.setattr(create_post, "_daily_news_context_is_incomplete", lambda *_args: False)
    monkeypatch.setattr(create_post, "generate_draft", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not write a mixed story")))
    queue = create_post.ModelWorkQueues(llm_workers=1, image_workers=1)
    try:
        result = create_post._prepare_daily_news_candidate(
            candidate_index=1, picked=picked,
            cfgs=[LLMConfig(provider="fake", model="fake", api_key="test")],
            asset_paths=[], copy_assets=False, auto_image_enabled=False,
            prompt_norm="国际新闻", viewpoint_norm="无视角评价", target_count=10,
            single_material_mode=False, base_meta={}, progress_callback=None,
            model_queues=queue, post_quality_callback=None,
            prepared=(picked, {}, {}, picked),
        )
    finally:
        queue.close()

    assert result.status == "skipped"
    assert result.reason == "multi_story_source"
