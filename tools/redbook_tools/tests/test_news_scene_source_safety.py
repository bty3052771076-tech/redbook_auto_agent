"""Offline scene-source regressions from the three rejected acceptance posts."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.config import LLMConfig
from src.news.daily_news import NewsItem
from src.workflow import create_post as w


@pytest.fixture(autouse=True)
def isolated_scene_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "post_dir", lambda pid: tmp_path / pid)
    monkeypatch.setattr(w, "save_post", lambda post: None)
    monkeypatch.setattr(w, "save_revision", lambda revision: None)


# Public source excerpts and factual paragraphs copied from stored post.json;
# no generated replacement facts, image URLs, credentials or production writes.
SAMPLES = [
    dict(
        id="84435072079f41929164bd8158b6f27d",
        title="巴西总检察长外国干预不容容忍",
        facts='巴西联邦总检察长豪尔赫·梅西西亚（Jorge Messias）表示，外国势力对巴西国家机构及民主制度的干预"不能被容忍"。他是在回应《卫报》一篇关于美国特朗普政府资助计划的报道时作出这一表态的。据介绍，相关报道披露了美国特朗普政府相关资助安排方面的内容，梅西西亚将该报道称为"对巴西主权的重要警示"。',
        source="Brazil attorney general says meddling ‘cannot be tolerated’ after Trump funding plans revealed",
        description='Writing on X, Jorge Messias called the report “an important alert for Brazilian sovereignty”.',
        bad="在官方办公场所内，一名身着正装的中年男性官员站立于发言台后方，身体微微前倾作发言状，身前架设着麦克风，画面以侧影呈现其发表正式表态的瞬间，背景为素净墙面。",
        good="巴西联邦总检察长豪尔赫·梅西西亚表示外国干预不能被容忍的概念示意，以非写实侧影表达在X平台发声的立场，背景为纯色色面，所有物面留白。",
    ),
    dict(
        id="348b56267fbb4f46a3c5b2ed355cc468",
        title="MLB取消国家公园办赛计划",
        facts="美国职业棒球大联盟（MLB）已不再考虑在国家公园内举办比赛。联盟总裁罗伯·曼弗雷德在致怀俄明州提顿县官员的信件中确认了这一决定。提顿县紧邻大提顿国家公园，而大提顿国家公园正是MLB此前考虑的办赛地点。",
        source="Swing-and-a-miss: MLB rescinds idea to host baseball game in national park after backlash",
        description="Major League Baseball (MLB) leadership is no longer considering holding a game inside a national park, according to a letter commissioner Robert Manfred sent to officials in Teton county, Wyoming, on Thursday",
        bad="一封已拆开的白色信封平放在深色木质办公桌面上，信纸抽出后并排搁在信封旁边，桌面右侧放置着一支深色钢笔，画面聚焦于桌面中部的信件与文具，背景虚化为办公室环境，整体构图简洁。",
        good="联盟总裁罗伯·曼弗雷德在致怀俄明州提顿县官员的信件中确认了这一决定的概念示意，一张无字信纸置于纯色色面中，信纸作为取消办赛计划的表达载体，所有物面留白。",
    ),
    dict(
        id="96f8865fd73e437abec66e9e506f63c9",
        title="澳洲工党打击留学生跳签证行为",
        facts='澳大利亚工党政府将"跳签证"的国际学生列为新一轮移民管控的目标，同时关注被指"向希望留在澳大利亚的人兜售虚假希望"的移民代理与律师。报道指出，部长们的目标是减少海外入境人数，针对的是长期处于"永久临时"状态的居留群体。',
        source="Labor targets ‘visa hopping’ international students in crackdown on ‘permanently temporary’ migration",
        description="“Visa hopping” international students, migration agents and lawyers trying to “game” the migration system will be targeted by the federal government, as Labor unveils a further crackdown.",
        bad="澳大利亚政府发言人在新闻发布厅面对媒体阐述针对国际学生签证的新管控措施，台前架设话筒，背景为简洁的政府会议室内景。",
        good='澳大利亚工党政府将"跳签证"的国际学生列为新一轮移民管控的目标的概念示意，以非写实人物侧影表达政府政策立场，背景为纯色色面，所有物面留白。',
    ),
]

# Corrected editorial candidates follow the new writer contract; frozen source
# paragraphs and the actually rejected writer values above remain unchanged.
for sample in SAMPLES:
    sample["good"] = sample["facts"].split("。", 1)[0] + "。" + sample["good"]


# Frozen factual paragraphs and public source excerpt from this live rejection.
CAMPUS_PROTEST = dict(
    id="7af80e8cecda4f5b9df3735eced56f01",
    title="法高中生抗议升级",
    facts="法国内政部门通报，全法范围内各地高中校园外目前约有900场示威、封锁及相关事件正在进行。这场由高中生发起的抗议诉求涉及学习时间过长、师资短缺以及校园设施破旧，并已从最初的小范围活动迅速扩大至全国。",
    source="France school protests escalate into ‘urban violence’ as PM calls crisis cabinet meeting",
    description="About 900 demonstrations, blockades and related incidents under way outside high schools around the country",
)
CAMPUS_COMPOSITION = "前景以非写实侧影和背影表现参与者，衣物采用简洁色块处理，背景为纯色色面，整体构图以人物为视觉中心，前后层次清晰，光照均匀，轮廓线条清楚，配色克制，人物分布疏密有序，所有物面留白，画面单幅铺满。"
CAMPUS_SCENE = "法国高中生在高中校园外正在进行示威与封锁抗议，" + CAMPUS_COMPOSITION


def normalize(sample, scene, *, facts=None, description=None):
    audit = {}
    result = w._normalize_daily_news_image_event(
        scene, picked=SimpleNamespace(title=sample["source"], description=sample["description"] if description is None else description),
        title=sample["title"], body=f"内容：\n{sample['facts'] if facts is None else facts}\n\n评价：\n仍待核实。",
        prompt_norm="国际新闻", audit=audit,
    )
    return result, audit


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: s["id"])
def test_real_rejected_scene_cannot_become_title_prompt(sample):
    result, audit = normalize(sample, sample["bad"])
    assert result == ""
    assert audit["accepted"] is False
    assert audit["normalized"] == ""
    assert audit["input"] == sample["bad"]


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: s["id"])
def test_source_named_editorial_scene_is_not_diluted_by_composition(sample):
    result, audit = normalize(sample, sample["good"])
    assert result == sample["good"]
    assert audit["accepted"] is True
    assert audit["evidence_sentence"]
    assert audit["supported_anchors"]


@pytest.mark.parametrize("scene", [
    "美国职业棒球大联盟（MLB）在大提顿国家公园举办棒球比赛，球员挥棒击球，观众欢呼。",
    "美国职业棒球大联盟（MLB）取消国家公园办赛计划的概念示意，球员在公园内挥棒击球，观众欢呼。",
])
def test_cancelled_game_must_not_be_drawn_as_played(scene):
    result, audit = normalize(SAMPLES[1], scene)
    assert result == ""
    assert audit["accepted"] is False
    assert audit["reason"] == "unsupported_scene_state"


def test_matching_named_speaker_does_not_invent_real_podium():
    scene = SAMPLES[0]["good"].replace("概念示意", "现场").replace("纯色色面", "发言台与麦克风")
    result, audit = normalize(SAMPLES[0], scene)
    assert result == ""
    assert audit["reason"] == "unsupported_scene_setting"


def test_channel_is_allowed_only_with_exact_source_support():
    result, audit = normalize(SAMPLES[0], SAMPLES[0]["good"], description="A report about Brazilian sovereignty.")
    assert result == ""
    assert audit["reason"] == "unsupported_scene_channel"


def test_source_on_x_does_not_require_scene_to_depict_x():
    scene = SAMPLES[0]["good"].replace("在X平台发声的立场", "反对外国干预的立场")
    result, audit = normalize(SAMPLES[0], scene)
    assert result == scene
    assert audit["accepted"] is True


def test_missing_facts_never_approve_headline_alone():
    result, audit = normalize(SAMPLES[0], SAMPLES[0]["title"], facts="")
    assert result == ""
    assert audit["accepted"] is False


def test_matching_actor_and_speech_verb_do_not_license_unrelated_editorial_topic():
    scene = "巴西联邦总检察长豪尔赫·梅西西亚表示关注气候行动的概念示意，以侧影表达在X平台发声的立场，背景为纯色色面。"
    result, audit = normalize(SAMPLES[0], scene)
    assert result == ""
    assert audit["accepted"] is False


def test_real_cancelled_source_cannot_license_completion_under_another_sport_word():
    scene = "美国职业棒球大联盟（MLB）在大提顿国家公园举办赛事，球员上场奔跑，观众正在看球。"
    result, audit = normalize(SAMPLES[1], scene)
    assert result == ""
    assert audit["accepted"] is False


def test_channel_from_different_source_actor_is_not_borrowed():
    result, audit = normalize(SAMPLES[0], SAMPLES[0]["good"], description="Writing on X, another official discussed climate action.")
    assert result == ""
    assert audit["accepted"] is False


@pytest.mark.parametrize("action", ["正在进行示威", "正在抗议", "正在进行封锁抗议"])
def test_live_campus_protest_core_state_survives_long_neutral_composition(action):
    scene = f"法国高中生在高中校园外{action}，{CAMPUS_COMPOSITION}"
    facts = CAMPUS_PROTEST["facts"]
    assert not any(w._daily_news_scene_matches_text(scene, sentence) for sentence in facts.split("。") if sentence)
    result, audit = normalize(CAMPUS_PROTEST, scene)
    assert result == scene
    assert audit["accepted"] is True
    assert {"state": "demonstration", "supported": True} in audit["state_checks"]
    assert audit["evidence_sentence"] in facts
    assert any("高中" in anchor for anchor in audit["supported_anchors"])


@pytest.mark.parametrize("scene", [
    "法国高中生在高中校园外踢足球，" + CAMPUS_COMPOSITION,
    "法国高中生在高中校园外庆祝，" + CAMPUS_COMPOSITION,
    "法国高中生在高中校园外庆祝抗议取得胜利，" + CAMPUS_COMPOSITION,
])
def test_same_campus_does_not_license_sport_or_celebration(scene):
    result, audit = normalize(CAMPUS_PROTEST, scene)
    assert result == ""
    assert audit["accepted"] is False


@pytest.mark.parametrize("facts", [
    "法国高中生计划在高中校园外举行示威和封锁抗议，活动尚未开始。",
    "法国高中生明天将在高中校园外举行示威和封锁抗议。",
    "法国高中生没有在高中校园外举行示威，也未发生封锁抗议。",
    "法国高中生在高中校园外的示威和封锁抗议已经结束。",
    "法国高中生在高中校园外上课。另一座机场目前正在进行示威与封锁抗议。",
    "法国高中生在高中校园外正在举行示威。",
])
def test_campus_demonstration_needs_same_fact_sentence_and_current_asserted_state(facts):
    result, audit = normalize(CAMPUS_PROTEST, CAMPUS_SCENE, facts=facts)
    assert result == ""
    assert audit["accepted"] is False
    assert audit["reason"] == "unsupported_scene_state"


def test_campus_protest_only_in_opinion_never_supplies_state_evidence():
    audit = {}
    result = w._normalize_daily_news_image_event(
        CAMPUS_SCENE, picked=SimpleNamespace(title=CAMPUS_PROTEST["source"], description=CAMPUS_PROTEST["description"]),
        title=CAMPUS_PROTEST["title"], body=f"内容：\n法国高中生在高中校园外上课。\n\n评价：\n{CAMPUS_PROTEST['facts']}",
        prompt_norm="国际新闻", audit=audit,
    )
    assert result == ""
    assert audit["accepted"] is False
    assert audit["reason"] == "unsupported_scene_state"


@pytest.mark.parametrize("detail", ["街道上", "校门前", "设置路障", "举着标语牌", "进行示威游行", "身穿校服"])
def test_campus_core_state_does_not_verify_unreported_setting_or_props(detail):
    scene = f"法国高中生在高中校园外正在示威，参与者{detail}，{CAMPUS_COMPOSITION}"
    result, audit = normalize(CAMPUS_PROTEST, scene)
    assert result == ""
    assert audit["accepted"] is False


@pytest.mark.parametrize("scene", [
    "一名官员表示外国干预不能被容忍的概念示意，背景为纯色色面。",
    "一张信纸表达取消办赛计划的概念示意，背景为纯色色面。",
])
def test_editorial_objects_or_roles_cannot_erase_named_subject(scene):
    sample = SAMPLES[1] if "信纸" in scene else SAMPLES[0]
    result, audit = normalize(sample, scene)
    assert result == ""
    assert audit["accepted"] is False


def prepare(monkeypatch, tmp_path, sample, scenes, *, issues=(), auto_image_enabled=True):
    calls, images, facts_checked, saved = [], [], [], []
    drafts = [dict(title=sample["title"], body=f"内容：\n{sample['facts']}\n\n评价：\n仍待核实。\n\n日期：2026-10-02\n\n来源：卫报", topics=["每日新闻"], image_event=s) for s in scenes]
    for draft in drafts:
        if draft["image_event"] is None:
            del draft["image_event"]
    def writer(*args, **kwargs):
        calls.append(kwargs["prompt_hint"])
        return deepcopy(drafts[len(calls) - 1])
    def gate(body, *args, **kwargs):
        facts_checked.append(body)
        return body
    def image(**kwargs):
        images.append(kwargs["prompt_hint"])
        return [], [], None
    checks = iter(issues)
    monkeypatch.setattr(w, "generate_draft", writer)
    monkeypatch.setattr(w, "_fetch_daily_news_related_images", image)
    monkeypatch.setattr(w, "_finalize_daily_news_body", gate)
    monkeypatch.setattr(w, "_repair_daily_news_mismatched_comment", lambda body, *a, **kw: body)
    monkeypatch.setattr(w, "_daily_news_quality_issue", lambda *a: next(checks, ""))
    for name in ("_daily_news_context_is_incomplete", "_daily_news_source_is_multi_story", "_daily_news_body_is_too_generic", "_daily_news_lead_lacks_headline_anchor", "_daily_news_content_is_too_thin"):
        monkeypatch.setattr(w, name, lambda *a: False)
    monkeypatch.setattr(w, "post_dir", lambda pid: tmp_path / pid)
    monkeypatch.setattr(w, "save_post", lambda post: saved.append(deepcopy(post)))
    monkeypatch.setattr(w, "save_revision", lambda revision: None)
    picked = NewsItem(title=sample["source"], url="https://example.test/frozen-source", description=sample["description"], content=sample["facts"], source="guardian", domain="theguardian.com", language="en", seendate="2026-10-02 01:36:50")
    queue = w.ModelWorkQueues(llm_workers=1, image_workers=1)
    try:
        result = w._prepare_daily_news_candidate(
            candidate_index=1, picked=picked, cfgs=[LLMConfig(provider="fake", model="fake", api_key="test")],
            asset_paths=[], copy_assets=False, auto_image_enabled=auto_image_enabled, prompt_norm="国际新闻", viewpoint_norm="无视角评价",
            target_count=1, single_material_mode=False, base_meta={}, progress_callback=None, model_queues=queue,
            post_quality_callback=None, prepared=(picked, {}, {}, picked),
        )
    finally:
        queue.close()
    return result, calls, images, facts_checked, saved


@pytest.mark.parametrize("scene", [None, ""])
def test_text_only_candidate_without_scene_does_not_rewrite_or_generate_image(monkeypatch, tmp_path, scene):
    result, calls, images, checked, saved = prepare(
        monkeypatch, tmp_path, SAMPLES[0], [scene], auto_image_enabled=False,
    )
    assert result.status == "success"
    assert result.post.platform["news"]["image_event"] == ""
    assert len(calls) == len(checked) == 1
    assert images == []
    assert not result.failed_post_saved


def test_text_only_body_rewrite_still_uses_one_allowance_without_scene(monkeypatch, tmp_path):
    result, calls, images, checked, saved = prepare(
        monkeypatch, tmp_path, SAMPLES[0], [None, None], issues=["thin_content", ""], auto_image_enabled=False,
    )
    assert result.status == "success"
    assert len(calls) == len(checked) == 2
    assert images == []
    assert result.post.platform["news"]["image_event"] == ""


def test_auto_image_without_scene_stays_strict_and_never_submits_headline(monkeypatch, tmp_path):
    result, calls, images, checked, saved = prepare(monkeypatch, tmp_path, SAMPLES[0], [None, None])
    assert result.status == "skipped"
    assert result.reason == "image_scene_unverified"
    assert len(calls) == len(checked) == 2
    assert images == []
    assert result.draft["image_event"] == ""
    assert result.draft["image_event_audit"]["accepted"] is False


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: s["id"])
def test_one_same_writer_repair_rechecks_body_before_only_supported_scene_is_sent(monkeypatch, tmp_path, sample):
    result, calls, images, checked, saved = prepare(monkeypatch, tmp_path, sample, [sample["bad"], sample["good"]])
    assert len(calls) == len(checked) == 2
    assert images == [sample["good"]]
    assert result.post.platform["news"]["image_event_audit"]["accepted"] is True
    assert result.draft["image_event_audit"]["attempts"][0]["accepted"] is False
    assert "body" in calls[1] and "image_event" in calls[1]


def test_source_supported_scene_with_new_composition_needs_no_rewrite(monkeypatch, tmp_path):
    sample = SAMPLES[0]
    scene = sample["good"].replace("在X平台发声的立场", "反对外国干预的立场").replace("背景为纯色色面，所有物面留白", "背景采用绿与灰的平涂色面，侧影放在画面左侧，构图简洁，所有物面留白")
    result, calls, images, checked, saved = prepare(monkeypatch, tmp_path, sample, [scene])
    assert len(calls) == len(checked) == 1
    assert images == [scene]
    assert result.post.platform["news"]["image_event_audit"]["accepted"] is True


def test_second_scene_failure_skips_with_durable_audit_and_zero_images(monkeypatch, tmp_path):
    sample = SAMPLES[1]
    result, calls, images, checked, saved = prepare(monkeypatch, tmp_path, sample, [sample["bad"], sample["bad"]])
    assert result.status == "skipped"
    assert len(calls) == len(checked) == 2
    assert images == []
    assert result.reason == "image_scene_unverified"
    assert result.draft["image_event"] == ""
    assert result.draft["image_event_audit"]["accepted"] is False
    assert saved and saved[-1].platform["news"]["image_event_audit"]["accepted"] is False
    assert result.failed_post_saved is True


def test_body_rewrite_and_scene_rewrite_share_one_allowance(monkeypatch, tmp_path):
    sample = SAMPLES[0]
    result, calls, images, checked, saved = prepare(monkeypatch, tmp_path, sample, [sample["good"], sample["bad"]], issues=["thin_content", ""])
    assert result.status == "skipped"
    assert len(calls) == len(checked) == 2
    assert images == []


def test_scene_rewrite_must_not_bypass_rewritten_body_fact_failure(monkeypatch, tmp_path):
    sample = SAMPLES[0]
    result, calls, images, checked, saved = prepare(monkeypatch, tmp_path, sample, [sample["bad"], sample["good"]], issues=["", "unsupported_numeric_claim"])
    assert result.status == "skipped"
    assert result.reason == "unsupported_numeric_claim"
    assert len(calls) == len(checked) == 2
    assert images == []
