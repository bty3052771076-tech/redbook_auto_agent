"""Offline contracts: exact factual lead and durable quality rejections."""

from copy import deepcopy

import pytest

from src.config import LLMConfig
from src.news.daily_news import NewsItem
from src.workflow import create_post as w
from test_news_scene_source_safety import SAMPLES, normalize, prepare

PostStatus = w.PostStatus


# Frozen factual paragraphs/source excerpts from the two actual rejected posts.
SHIPPING = dict(
    id="876d27dd35da426294fa5514092142b5", title="霍尔木兹原油运输恢复",
    facts="霍尔木兹海峡的原油出口已大体恢复至伊朗战争爆发前的水平。据报道，石油生产商与航运业通过替代方式将燃料运出海湾地区，使原油运输基本回到战前状态；但柴油等成品油的运输流通仍受到制约，恢复进度落后于原油。",
    source="Crude oil exports from strait of Hormuz largely return to pre-war levels",
    description="Alternative ways being used to move fuel out of Gulf region, but flows of refined products such as diesel still constrained",
)
HEARING = dict(
    id="e5476d6be4ee4b6e9f5116367c6f89d1", title="康奈尔学生公听会表不满",
    facts="康奈尔大学学生会就一起强奸案相关议题主持举行公开听证会，多名学生在听证会上就案件本身、校园性暴力与安全问题表达不满。BBC记者娜达·塔菲克到场出席并报道此次听证会过程。听证会上，康奈尔大学学生会主导讨论环节，围绕校园安全性问题与性暴力议题展开公开讨论，多名学生参与发言，就案件处理、校园安全治理等具体议题表达不满并提出相关意见。",
    source="Cornell students voice frustration at public hearing over rape case",
    description="The BBC's Nada Tawfik was at the hearing, where the university's Student Assembly led a discussion around safety concerns and sexual violence on campus.",
)
COMPOSITION = "背景为纯色色面，非写实轮廓居中，前后层次清晰，光照均匀，配色克制，所有物面留白，单幅铺满。"


def exact_scene(sample, visual):
    return sample["facts"].split("。", 1)[0] + "。" + visual + "，" + COMPOSITION


@pytest.mark.parametrize("sample,visual", [
    (SHIPPING, "霍尔木兹海峡的原油出口恢复至战前水平的概念示意，以原油运输为主体的非实景构图"),
    (HEARING, "康奈尔大学学生会主持公开听证会的editorial概念示意，以非写实学生侧影表达讨论校园性暴力与安全问题"),
])
def test_real_non_speech_news_complete_lead_accepts_grounded_editorial(sample, visual):
    scene = exact_scene(sample, visual)
    result, audit = normalize(sample, scene)
    assert result == scene
    assert audit["accepted"] is True
    assert audit["factual_lead"] == sample["facts"].split("。", 1)[0] + "。"
    assert audit["visual_scene"].startswith(visual)


@pytest.mark.parametrize("lead", [
    "霍尔木兹海峡原油出口。",  # Shared name is not a whole factual sentence.
    SHIPPING["facts"].split("。", 1)[0].replace("大体", "全面") + "。",
    SHIPPING["facts"].split("。", 1)[0] + "，",
])
def test_editorial_requires_verbatim_complete_first_fact(lead):
    scene = lead + "霍尔木兹海峡原油出口恢复的概念示意，" + COMPOSITION
    result, audit = normalize(SHIPPING, scene)
    assert result == ""
    assert audit["accepted"] is False


@pytest.mark.parametrize("visual", [
    "巴西联邦总检察长豪尔赫·梅西西亚关注气候行动的概念示意",
    "一名官员表示外国干预不能被容忍的概念示意",
    "巴西联邦总检察长豪尔赫·梅西西亚在发布会讲台讲话的概念示意",
    "巴西联邦总检察长豪尔赫·梅西西亚正在踢足球的概念示意",
])
def test_copied_fact_does_not_license_unrelated_or_anonymous_added_scene(visual):
    result, audit = normalize(SAMPLES[0], exact_scene(SAMPLES[0], visual))
    assert result == ""
    assert audit["accepted"] is False


def test_copied_cancelled_fact_is_not_itself_a_depicted_completed_game():
    sample = SAMPLES[1]
    visual = "联盟总裁罗伯·曼弗雷德在信件中确认不再考虑国家公园办赛的概念示意，以无字信纸为表达载体"
    result, audit = normalize(sample, exact_scene(sample, visual))
    assert result
    assert audit["accepted"] is True
    result, audit = normalize(sample, exact_scene(sample, "美国职业棒球大联盟举办棒球比赛，球员挥棒击球的概念示意"))
    assert result == ""
    assert audit["reason"] == "unsupported_scene_state"


def test_copied_fact_does_not_license_unsupported_channel():
    scene = exact_scene(SHIPPING, "霍尔木兹海峡原油出口恢复的概念示意，在X平台发布运输消息")
    result, audit = normalize(SHIPPING, scene)
    assert result == ""
    assert audit["reason"] == "unsupported_scene_channel"


@pytest.mark.parametrize("issue", ["thin_content", "missing_lead_event", "generic_title", "generic_body", "bad_body_language", "unsupported_numeric_claim", "image_scene_unverified", "custom_quality_failure"])
def test_every_final_body_gate_rejection_keeps_raw_attempts_and_failed_post(monkeypatch, tmp_path, issue):
    result, calls, images, checked, saved = prepare(
        monkeypatch, tmp_path, SAMPLES[0], [None, None], issues=[issue, issue], auto_image_enabled=False,
    )
    assert result.status == "skipped"
    assert result.reason == issue
    assert images == []
    assert result.failed_post_saved and saved
    post = saved[-1]
    assert post.status == PostStatus.failed
    assert not post.platform.get("uploaded")
    diag = post.platform["news"]["quality_rejection"]
    assert diag["reason"] == issue
    assert diag["candidate_index"] == 1
    assert diag["source_snapshot"]["title"] == SAMPLES[0]["source"]
    assert len(diag["attempts"]) == len(calls) <= 2
    assert all("raw_draft" in attempt and "body" in attempt["raw_draft"] for attempt in diag["attempts"])
    assert all(attempt["reason"] == issue for attempt in diag["attempts"])


def test_numeric_reject_persists_real_helper_difference_and_revision_on_disk(monkeypatch, tmp_path):
    from src.storage.files import save_post, save_revision

    picked = NewsItem(title="Three students spoke at a hearing", url="https://example.test/frozen-hearing",
                      description="Three students spoke at the hearing.", content="三名学生在听证会上发言。",
                      source="BBC", domain="bbc.com", seendate="2026-10-02 01:00:00")
    raw = dict(title="学生听证会发言", body="内容：\n999名学生在听证会上发言。\n\n评价：\n后续处理仍待观察。\n\n日期：2026-10-02\n\n来源：BBC", topics=["每日新闻"])
    calls = []
    def writer(*a, **kw):
        calls.append(kw)
        return deepcopy(raw)
    def gate(body, *a, **kw):
        return w._daily_news_remove_unsupported_numeric_sentences(body, picked)
    monkeypatch.setattr(w, "generate_draft", writer)
    monkeypatch.setattr(w, "_finalize_daily_news_body", gate)
    monkeypatch.setattr(w, "_daily_news_body_is_too_generic", lambda *a: False)
    monkeypatch.setattr(w, "_daily_news_source_is_multi_story", lambda *a: False)
    monkeypatch.setattr(w, "_daily_news_context_is_incomplete", lambda *a: False)
    monkeypatch.setattr(w, "save_post", lambda post: save_post(post, base=tmp_path))
    monkeypatch.setattr(w, "save_revision", lambda rev: save_revision(rev, base=tmp_path))
    queue = w.ModelWorkQueues(llm_workers=1, image_workers=1)
    try:
        result = w._prepare_daily_news_candidate(candidate_index=7, picked=picked, cfgs=[LLMConfig(provider="fake", model="fake", api_key="test")],
            asset_paths=[], copy_assets=False, auto_image_enabled=False, prompt_norm="新闻", viewpoint_norm="无视角评价", target_count=1,
            single_material_mode=False, base_meta={}, progress_callback=None, model_queues=queue, post_quality_callback=None, prepared=(picked, {}, {}, picked))
    finally:
        queue.close()
    assert result.reason == "unsupported_numeric_claim"
    assert len(calls) == 2
    assert result.failed_post_saved and result.post.status == PostStatus.failed
    diag = result.post.platform["news"]["quality_rejection"]
    assert diag["attempts"][0]["raw_draft"] == raw
    assert diag["attempts"][1]["raw_draft"] == raw
    numeric = diag["attempts"][0]["numeric"]
    assert ["999", "person", "exact"] in numeric["difference"]
    assert ["3", "person", "exact"] in numeric["source_tokens"]
    assert all(token[1] != "date" for token in numeric["candidate_tokens"])
    assert diag["attempts"][0]["error"]
    import json
    directory = tmp_path / "posts" / result.post.id
    stored = json.loads((directory / "post.json").read_text(encoding="utf-8"))
    assert stored["status"] == "failed"
    revisions = list((directory / "revisions").glob("*.json"))
    assert revisions
    revision = json.loads(revisions[0].read_text(encoding="utf-8"))
    assert revision["content"]["quality_rejection"] == stored["platform"]["news"]["quality_rejection"]


@pytest.mark.parametrize("ok,score", [(False, 85), (True, 69)])
def test_actual_vlm_rejection_persists_failed_diagnostics_and_revision(monkeypatch, tmp_path, ok, score):
    import json
    from apps.cli import _vision_review_passes
    from src.storage.files import save_post, save_revision
    from src.workflow.vision_review import VisionReviewResult

    picked = NewsItem(title=SHIPPING["source"], url="https://example.test/frozen-oil-source",
                      description=SHIPPING["description"], content=SHIPPING["facts"],
                      source="guardian", domain="theguardian.com", seendate="2026-10-01 12:00:00")
    raw = dict(title=SHIPPING["title"], body=f"内容：\n{SHIPPING['facts']}\n\n评价：\n全面恢复仍待观察。\n\n日期：2026-10-01\n\n来源：guardian",
               topics=["每日新闻"])
    image = tmp_path / "provided.png"
    image.write_bytes(b"offline provided image fixture")
    calls, reviewed = [], []

    def writer(*a, **kw):
        calls.append(kw)
        return deepcopy(raw)

    def quality(post):
        reviewed.append(post.id)
        verdict = VisionReviewResult(ok=ok, score=score, issues=("主体动作不符",), retry_prompt="")
        assert not _vision_review_passes(verdict)
        return ["VLM主体动作不符"]

    monkeypatch.setattr(w, "generate_draft", writer)
    monkeypatch.setattr(w, "_finalize_daily_news_body", lambda body, *a, **kw: body)
    monkeypatch.setattr(w, "_repair_daily_news_mismatched_comment", lambda body, *a, **kw: body)
    monkeypatch.setattr(w, "_daily_news_quality_issue", lambda *a: "")
    for name in ("_daily_news_context_is_incomplete", "_daily_news_source_is_multi_story", "_daily_news_body_is_too_generic", "_daily_news_lead_lacks_headline_anchor", "_daily_news_content_is_too_thin"):
        monkeypatch.setattr(w, name, lambda *a: False)
    monkeypatch.setattr(w, "save_post", lambda post: save_post(post, base=tmp_path))
    monkeypatch.setattr(w, "save_revision", lambda revision: save_revision(revision, base=tmp_path))
    queue = w.ModelWorkQueues(llm_workers=1, image_workers=1)
    try:
        result = w._prepare_daily_news_candidate(
            candidate_index=9, picked=picked, cfgs=[LLMConfig(provider="fake", model="fake", api_key="test")],
            asset_paths=[str(image)], copy_assets=False, auto_image_enabled=False,
            prompt_norm="国际新闻", viewpoint_norm="无视角评价", target_count=1, single_material_mode=False,
            base_meta={"run_id": "4a041b7eee5f4fbbb15f0ef470c3ba27"}, progress_callback=None,
            model_queues=queue, post_quality_callback=quality, prepared=(picked, {}, {}, picked),
        )
    finally:
        queue.close()
    assert len(calls) == len(reviewed) == 1
    assert result.status == "skipped" and result.reason == "visual_quality_failed"
    assert result.failed_post_saved and result.post.status == PostStatus.failed
    assert result.post.uploaded is False
    assert result.post.platform["batch_selection"]["status"] == "visual_quality_failed"
    diagnostics = result.post.platform["news"]["quality_rejection"]
    assert diagnostics["reason"] == "visual_quality_failed"
    assert diagnostics["error"] == "VLM主体动作不符"
    assert diagnostics["candidate_index"] == 9
    assert diagnostics["source_trace"]["run_id"] == "4a041b7eee5f4fbbb15f0ef470c3ba27"
    assert diagnostics["attempts"][0]["raw_draft"] == raw
    directory = tmp_path / "posts" / result.post.id
    stored = json.loads((directory / "post.json").read_text(encoding="utf-8"))
    revision = json.loads(next((directory / "revisions").glob("*.json")).read_text(encoding="utf-8"))
    assert stored["status"] == "failed" and stored["uploaded"] is False
    assert revision["content"]["quality_rejection"] == diagnostics
