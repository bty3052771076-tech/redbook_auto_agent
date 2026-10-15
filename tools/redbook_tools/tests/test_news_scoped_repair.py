from dataclasses import replace
import pytest
from src.workflow.news_contracts import ReviewIssue, StageContext, TextArtifact, SceneArtifact
from src.workflow.news_repairs import plan_repair, apply_repair, repair_draft

CONTEXT = StageContext("run", "exec", "news", "candidate", "account", "frozen", pipeline_version="staged_v2")
TEXT = TextArtifact("市政府调整公交", "市政府公布夜间公交计划，实施日期仍待确认。", "后续应关注末班车时刻与司机排班能否同步落实。")


def test_comment_patch_keeps_approved_content_byte_identical():
    request = plan_repair(TEXT, [ReviewIssue("comment_too_long", "comment", repairable=True)], context=CONTEXT)
    fixed = apply_repair(TEXT, {"comment":"末班车安排仍待交通部门正式确认。"}, request=request)
    assert fixed.content == TEXT.content
    assert fixed.title == TEXT.title


def test_scene_failure_can_only_change_scene():
    scene = SceneArtifact(TEXT.version, "evidence", "旧场景")
    request = plan_repair(scene, [ReviewIssue("image_scene_unverified", "scene", repairable=True)], context=CONTEXT)
    fixed = apply_repair(scene, {"scene_prompt":"公交车辆概念示意，无文字"}, request=request)
    assert fixed.text_hash == TEXT.version
    with pytest.raises(ValueError, match="unauthorized"):
        apply_repair(scene, {"text_hash":"different"}, request=request)


def test_patch_rejects_wrong_base_and_unrelated_fields():
    request = plan_repair(TEXT, [ReviewIssue("comment_too_long", "comment", repairable=True)], context=CONTEXT)
    with pytest.raises(ValueError, match="base"):
        apply_repair(replace(TEXT, content="changed"), {"comment":"ok"}, request=request)
    with pytest.raises(ValueError, match="unauthorized"):
        apply_repair(TEXT, {"content":"changed"}, request=request)


def test_scene_repair_does_not_call_full_writer_or_change_body():
    draft = {"title":TEXT.title,"body":"内容：\n原始正文。\n\n评价：\n原始评价。\n\n日期：2026-10-10", "image_event":"旧场景"}
    calls = []
    def model(**kwargs):
        calls.append(kwargs)
        return {"image_event":"新场景"}
    fixed = repair_draft(draft, "image_scene_unverified", source_prompt="原文", generate_patch=model)
    assert fixed["body"] == draft["body"]
    assert fixed["image_event"] == "新场景"
    assert len(calls) == 1
    assert '"image_event"' in calls[0]["user_prompt"]


def test_comment_only_patch_keeps_date_source_and_content():
    body = "内容：\n原始正文。\n\n评价：\n过长评价。\n\n日期：2026-10-10\n\n来源：官方"
    fixed = repair_draft({"title":"标题","body":body}, "comment_too_long", source_prompt="原文",
                         generate_patch=lambda **_: {"comment":"简洁评价。"})
    assert fixed["body"] == body.replace("过长评价。", "简洁评价。")


def test_content_resummary_is_complete_not_sliced():
    fixed = repair_draft({"title":"标题","body":"内容：\n旧正文。\n\n评价：\n评价。"}, "content_too_long",
        source_prompt="原文", generate_patch=lambda **_: {"content":"完整的新总结。"})
    assert fixed["body"] == "内容：\n完整的新总结。\n\n评价：\n评价。"


@pytest.mark.parametrize("repair", ["scene", "comment", "generic_comment", "waiting_comment", "missing_comment"])
def test_candidate_staged_mode_repairs_scene_without_second_writer(monkeypatch, repair):
    from src.workflow import create_post as w
    from src.news.daily_news import NewsItem
    from src.config import LLMConfig
    facts = ("市政府公布夜间公交调整计划，新增班次仍在研究，具体实施时间尚未确认。"
             "交通部门表示，换乘站将同步研究末班车衔接，运营公司需要根据客流调整车辆调度。"
             "乘客协会要求补充站点照明和候车安全措施，工会代表则提出司机轮班安排需要协商。"
             "财政部门解释，资金来源将使用既有交通专项预算，议会委员会将在审查报告中公开实施进展。")
    picked = NewsItem(title="市政府公布夜间公交计划",url="https://example.org/bus",description=facts,
                      content=facts*3,seendate="2026-10-10 08:00:00",source="test",domain="example.org",language="zh")
    calls=[]
    def writer(*a, **kw):
        calls.append("writer")
        comment = "夜间公交的便利不能以牺牲候车安全和司机休息为代价。"
        if repair == "waiting_comment":
            comment = "后续影响有待进一步观察，实际效果仍需后续信息明确。"
        elif repair == "missing_comment":
            comment = ""
        elif repair != "scene":
            comment *= 3
        return {"title":picked.title,"body":f"内容：\n{facts}\n\n评价：\n{comment}", "image_event":"bad" if repair == "scene" else "accepted", "topics":[]}
    def patch(*a, **kw):
        calls.append("patch")
        if repair == "scene":
            return {"image_event":"accepted"}
        return {"comment": "公交班次仍在研究，不是已经定论的结果，乘客暂需留意后续公告。" if repair == "generic_comment"
                else "夜间公交的便利不能以牺牲候车安全和司机休息为代价。"}
    def scene(value, **kw):
        kw["audit"].update(accepted=value=="accepted",reason="test")
        return value if value=="accepted" else ""
    monkeypatch.setattr(w,"generate_draft",writer)
    monkeypatch.setattr(w,"generate_json",patch)
    monkeypatch.setattr(w,"_normalize_daily_news_image_event",scene)
    monkeypatch.setattr(w,"save_post", lambda *a, **k: None)
    monkeypatch.setattr(w,"save_revision", lambda *a, **k: None)
    with w.ModelWorkQueues(llm_workers=1,image_workers=1) as queue:
        queue.pipeline_version="staged_v2"
        result=w._prepare_daily_news_candidate(candidate_index=1,picked=picked,cfgs=[LLMConfig(provider="fake",model="fake",api_key="test")],
            asset_paths=[],copy_assets=False,auto_image_enabled=False,prompt_norm="公交",viewpoint_norm="无视角评价",target_count=1,
            single_material_mode=False,base_meta={},progress_callback=None,model_queues=queue,post_quality_callback=None,
            prepared=(picked,{}, {},picked))
    if repair == "generic_comment":
        assert result.status == "skipped"
        assert result.reason == "generic_body"
    else:
        assert result.status=="success", (result.reason, calls, result.error, result.draft.get("_length_review"), w._daily_news_body_quality_fields(result.draft.get("body", "")))
    assert calls==["writer","patch"]
    assert facts in result.post.body
