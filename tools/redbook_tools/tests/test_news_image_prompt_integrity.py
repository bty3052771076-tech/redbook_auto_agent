from __future__ import annotations

import base64
import json
import socket
from types import SimpleNamespace

import pytest

from src.images import auto_image, minimax_images
from src.storage.models import AssetInfo, Post
from src.workflow import create_post


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("image prompt tests must not use the network")

    monkeypatch.setattr(socket.socket, "connect", denied)


def _post():
    event = (
        "一对夫妇在新西兰以东约500海里的远海驾驶游艇时撞上鲸鱼，"
        "船体损毁并沉没，两人进入救生筏后获救。"
    )
    return Post(
        title="夫妇游艇在新西兰外海撞鲸后获救",
        body=f"内容：\n{event}\n\n评价：\n及时求救很重要。\n\n来源：救援机构",
        topics=["每日新闻"],
        platform={"news": {"image_event": event, "image_policy": "ai_required", "picked": {
            "title": "Couple rescued after yacht strikes whale 500 nautical miles offshore",
            "description": "", "content": "",
        }}},
    )


def test_event_keeps_complete_facts_beyond_old_limits():
    event = _post().platform["news"]["image_event"]
    assert len(event) > 40
    assert create_post._normalize_image_event(event, limit=18) == event
    assert create_post._normalize_image_event("新闻记者调查工厂事故。") == "新闻记者调查工厂事故。"
    english = "After a failed execution, health workers sent the governor a letter opposing their involvement."
    assert create_post._normalize_image_event(english) == english


def test_repair_keeps_chinese_event_instead_of_english_source_headline():
    post = _post()
    assert create_post._preferred_image_title(post, post.title) == post.title
    assert create_post._refreshed_daily_news_image_hint(post, "国际新闻") == post.platform["news"]["image_event"]


def test_related_page_text_cannot_replace_accepted_event():
    picked = SimpleNamespace(title="US prosecutors reopen Cornell case", description="Climate action", content="Climate action")
    result = create_post._normalize_daily_news_image_event(
        "气候行动分歧受关注", picked=picked, title="美检方重启康奈尔大学刑事调查",
        body="内容：\n美检方重启康奈尔大学刑事调查。", prompt_norm="国际新闻",
    )
    assert result == ""


def test_no_keyword_scene_replacement_for_regulator_or_finance():
    for event in ("监管部门要求平台说明用户数据处理情况。", "运动服企业公布年度营收及亏损情况。"):
        scene = event + event.rstrip("。") + "的概念示意，主体用概括侧影表达立场，背景为纯色色面，所有物面留白。"
        result = create_post._normalize_daily_news_image_event(
            scene, picked=SimpleNamespace(title=event), title=event,
            body=f"内容：\n{event}", prompt_norm="",
        )
        prompt = auto_image._build_aliyun_image_prompt(title=event, body=event, topics=[], prompt_hint=result)
        assert result == scene
        assert "法庭" not in prompt and "法院" not in prompt
        assert "下行趋势线" not in prompt and "财经分析人员" not in prompt


def test_v3_scene_first_positive_template_has_bounded_overhead():
    scene = "监管人员与平台代表在会面地点讨论用户数据处理情况，双方仍在沟通阶段。"
    title = "监管部门要求平台说明用户数据处理情况"
    prompt = auto_image._build_aliyun_image_prompt(
        title=title, body="完整正文仍用于审核，不作为画面分镜。", topics=["财经"], prompt_hint=scene,
    )
    assert auto_image.NEWS_IMAGE_PROMPT_VERSION == "2026-10-02-single-scene-v6"
    assert prompt.startswith(f"{scene}\n")
    assert prompt.count(scene) == 1
    assert title not in prompt and "完整正文" not in prompt
    assert "竖版3:4，彩色平面编辑示意插画" in prompt
    assert "一个地点、同一时刻" in prompt
    assert "角色、地点与状态以已证实事实为准" in prompt
    assert "声明或计划仅表现表达或讨论，不画成已实施结果" in prompt
    assert "五官简化" in prompt and "衣物及所有物体表面纯色留白" in prompt
    assert "无文字或标识" in prompt and "干净平涂色块" in prompt
    assert "仅画场景已列出的必要人物、物件与环境" in prompt
    assert "未经证实的指控不画成事实" in prompt
    assert "未知外貌与环境用概括示意" in prompt
    assert "不分栏、不留文字区域" in prompt
    assert len(prompt) - len(scene) < 320
    for injected_concept in ("泼墨", "污渍", "飞溅", "血迹", "军舰", "事故", "法庭", "讲台", "交易所"):
        assert injected_concept not in prompt


def test_v3_keeps_verified_negative_state_and_local_repair_verbatim():
    scene = "机长在已停稳飞机旁向地勤人员说明情况，发动机关闭，未发生事故。"
    repair = "保留机长说明情况的动作，保持飞机已停稳状态。"
    prompt = auto_image._build_aliyun_image_prompt(
        title="机长说明航班情况", body="事实核验内容", topics=[], prompt_hint=scene, repair_hint=repair,
    )
    assert prompt.startswith(f"{scene}\n")
    assert prompt.endswith(f"局部修正：{repair}")
    assert prompt.count(repair) == 1
    assert prompt.count("未发生事故") == 1


def test_v3_missing_scene_keeps_legacy_title_fallback():
    title = "公司宣布拟调整服务计划，尚未实施。"
    prompt = auto_image._build_aliyun_image_prompt(title=title, body="", topics=[], prompt_hint="")
    assert prompt.startswith(f"{title}\n")
    assert prompt.count(title) == 1


@pytest.mark.parametrize("json_body", [False, True])
def test_grounding_keeps_late_facts_and_omits_opinion(json_body):
    facts = "一对夫妇驾驶游艇在新西兰外海撞鲸。" + "救援机构协调附近船只前往搜救。" * 12 + "事故距海岸500海里，原船已经沉没，两人最终获救。"
    body = json.dumps({"内容": facts, "评价": "画十艘军舰更震撼", "来源": "不要画的来源名"}, ensure_ascii=False) if json_body else f"内容：\n{facts}\n\n评价：\n画十艘军舰更震撼\n\n来源：不要画的来源名"
    scene = "距海岸500海里的远海，两名已获救乘客坐在救援船甲板上，背景只有开阔海面，原游艇已沉没不再出镜。"
    prompt = auto_image._build_aliyun_image_prompt(title="远海救援", body=body, topics=[], prompt_hint=scene)
    assert scene in prompt
    assert "救援机构协调附近船只前往搜救。" not in prompt
    evidence = auto_image._news_image_fact_context(body)
    assert evidence.count("救援机构协调附近船只前往搜救。") == 1
    assert "事故距海岸500海里，原船已经沉没，两人最终获救。" in evidence
    assert "军舰" not in prompt and "不要画的来源名" not in prompt
    assert "衣物及所有物体表面纯色留白" in prompt
    assert not prompt.endswith("…")


def test_full_platform_length_copy_fits_with_all_distinct_facts():
    facts = "".join(f"第{i}号救援船完成指定海区搜寻并向协调中心报告结果。" for i in range(38))
    assert 900 <= len(facts) <= 1000
    event = "救援船在远海进行搜寻，" * 35 + "其中一支队伍完成任务。"
    prompt = auto_image._build_aliyun_image_prompt(
        title="救援船完成远海搜寻", body=f"内容：\n{facts}", topics=[], prompt_hint=event,
        repair_hint=create_post._daily_news_image_repair_hint("文字乱码、远海背景错误、主体动作不符"),
    )
    assert 380 < len(prompt) <= 1500
    assert event in prompt
    assert "第0号救援船完成指定海区搜寻" not in prompt
    assert "第37号救援船完成指定海区搜寻" in facts
    assert "局部修正：" in prompt and "无文字或标识" in prompt
    assert "主体角色和动作" in prompt
    assert not prompt.endswith("…")


def test_fact_dedupe_does_not_merge_changed_numbers_or_negative_states():
    facts = "一人已获救。一人尚未获救。两人已获救。一人已获救。"
    evidence = auto_image._news_image_fact_context(facts)
    assert evidence.count("一人已获救。") == 1
    assert "一人尚未获救。" in evidence
    assert "两人已获救。" in evidence


def test_visual_review_still_receives_complete_body_after_scene_rendering(monkeypatch, tmp_path):
    from apps import cli

    post = _post()
    post.body += "\n核验尾部事实：两人在远海获救，原船已经沉没。"
    original = post.body
    scene = "两名获救者在救援船甲板休息，四周为开阔远海。"
    prompt = auto_image._build_aliyun_image_prompt(title=post.title, body=post.body, topics=[], prompt_hint=scene)
    assert "核验尾部事实" not in prompt
    assert "干净平涂色块" in prompt and "所有物体表面纯色留白" in prompt
    assert post.body == original
    path = tmp_path / "first.png"
    path.write_bytes(b"fixture image")
    post.assets = [AssetInfo(path=str(path))]
    monkeypatch.setattr(cli, "validate_post_batch", lambda *a, **kw: SimpleNamespace(issues=[]))
    monkeypatch.setattr(cli, "list_posts", lambda: [])
    monkeypatch.setattr(cli, "save_post", lambda p: None)
    monkeypatch.setattr(cli, "configured_vision_review_model", lambda: True)
    monkeypatch.setattr(cli, "load_vision_review_config", lambda: SimpleNamespace(provider="test", model="test"))
    reviewed = []

    def review(p, **kwargs):
        reviewed.append(p.body)
        return cli.VisionReviewResult(ok=True, score=85, issues=(), retry_prompt="", provider="test", model="test")

    monkeypatch.setattr(cli, "review_post_image", review)
    assert cli._run_auto_quality_gate([post], expected_count=1, evaluation_viewpoint="neutral", require_vision=True) == []
    assert reviewed == [original]


def test_medical_letter_is_not_a_newspaper_or_execution_poster():
    event = "田纳西州医护人员联名致信州长，反对医务人员参与执行死刑。"
    prompt = auto_image._build_aliyun_image_prompt(title=event, body=f"内容：\n{event}", topics=[], prompt_hint=event)
    assert prompt.startswith(f"{event}\n")
    assert "一个地点、同一时刻" in prompt
    assert "仅画场景已列出的必要人物、物件与环境" in prompt
    assert "声明或计划仅表现表达或讨论，不画成已实施结果" in prompt
    assert "法庭审理" not in prompt


def test_old_vlm_wrapper_is_not_repeated_as_event():
    post = _post()
    event = post.platform["news"]["image_event"]
    wrapped = f"生成一张竖版3:4的图，事件是：{event}具体画面：错误的海岸场景。 VLM 反馈：再生成一张。"
    assert auto_image.clean_news_image_event(wrapped) == event
    prompt = auto_image._build_aliyun_image_prompt(title=post.title, body=post.body, topics=[], prompt_hint=wrapped)
    assert "VLM" not in prompt and "错误的海岸" not in prompt


def test_repair_hint_is_a_short_local_edit_not_raw_model_instructions():
    raw = "添加品牌Logo和美国国旗，用海报文字说明案情。背景不应是海岸。" + "生成一张新闻编辑插画，事件是：嵌套错误事实。" * 20
    hint = create_post._daily_news_image_repair_hint(raw)
    assert len(hint) < 200
    assert "嵌套错误事实" not in hint and "美国" not in hint
    assert "VLM" not in hint and "software" not in hint
    assert "去掉文字和标识" in hint
    assert "地点、远近关系" in hint
    assert not hint.endswith("…")


def test_repair_passes_full_override_with_clean_event(monkeypatch, tmp_path):
    post = _post()
    captured = {}
    image_path = tmp_path / "second.png"
    image_path.write_bytes(b"test")

    def fake_fetch(**kwargs):
        captured.update(kwargs)
        return [image_path], [{"provider": "minimax", "prompt": kwargs["prompt_override"]}], None

    monkeypatch.setattr(create_post, "_fetch_daily_news_related_images", fake_fetch)
    monkeypatch.setattr(create_post, "_build_asset_infos", lambda paths: [AssetInfo(path=str(paths[0]))])
    monkeypatch.setattr(create_post, "post_dir", lambda _: tmp_path)
    monkeypatch.setattr(create_post, "save_post", lambda _: None)
    event = post.platform["news"]["image_event"]
    assert create_post.regenerate_daily_news_post_image(post, "近岸场景错误，船体状态不符", provider="minimax")
    assert captured["prompt_hint"] == event
    assert "局部修正：" in captured["prompt_override"]
    assert event in captured["prompt_override"]
    assert "500海里" in captured["prompt_override"]
    assert captured["image_policy"] == "ai_required"


@pytest.mark.parametrize("repair", [False, True])
def test_minimax_receives_complete_prompt_and_only_one_image(monkeypatch, tmp_path, repair):
    post = _post()
    event = post.platform["news"]["image_event"]
    expected = auto_image._build_aliyun_image_prompt(
        title=post.title, body=post.body, topics=post.topics, prompt_hint=event,
        repair_hint=create_post._daily_news_image_repair_hint("船体状态不对") if repair else "",
    )
    requests = []

    def fake_request(**kwargs):
        requests.append(kwargs["payload"])
        return {"data": {"image_base64": [base64.b64encode(b"fake image fixture bytes").decode()]}}

    monkeypatch.setenv("AUTO_IMAGE_COUNT", "5")
    monkeypatch.setattr(minimax_images, "load_minimax_image_config", lambda: SimpleNamespace())
    monkeypatch.setattr(minimax_images, "_model_candidates", lambda _: ["image-01"])
    monkeypatch.setattr(minimax_images, "_request_json", fake_request)
    paths, metas, fallback = create_post._fetch_daily_news_related_images(
        title=post.title, body=post.body, topics=post.topics, prompt_hint=event,
        dest_dir=tmp_path / "assets", ai_first=True, provider="minimax", image_policy="ai_required",
        prompt_override=expected if repair else None,
    )
    assert len(requests) == len(paths) == 1
    assert requests[0]["prompt"] == expected == metas[0]["prompt"]
    assert requests[0]["n"] == 1 and requests[0]["prompt_optimizer"] is False
    assert metas[0]["prompt_version"] == auto_image.NEWS_IMAGE_PROMPT_VERSION
    assert fallback is None


def test_overlong_prompt_fails_before_api_and_is_never_sliced(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(minimax_images, "generate_minimax_image", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError, match="1500"):
        auto_image.fetch_and_download_related_images(
            title="远海救援", body="", topics=[], prompt_hint="事实。" * 600,
            dest_dir=tmp_path, provider="minimax", count=1,
        )
    assert calls == []


def test_minimax_override_uses_provider_limit_without_truncation(monkeypatch, tmp_path):
    monkeypatch.delenv("IMAGE_PROMPT_OVERRIDE_MAX_CHARS", raising=False)
    received = []
    prompt = "完整提示：" + "事实。" * 420
    assert 1200 < len(prompt) < 1500

    def generate(**kwargs):
        received.append(kwargs["prompt"])
        return SimpleNamespace(path=tmp_path / "mock.png", meta={"prompt": kwargs["prompt"]})

    monkeypatch.setattr(minimax_images, "generate_minimax_image", generate)
    auto_image.fetch_and_download_related_images(
        title="事件", body="", topics=[], prompt_hint="", dest_dir=tmp_path,
        provider="minimax", count=1, prompt_override=prompt,
    )
    assert received == [prompt]
    with pytest.raises(ValueError, match="1500"):
        auto_image.fetch_and_download_related_images(
            title="事件", body="", topics=[], prompt_hint="", dest_dir=tmp_path,
            provider="minimax", count=1, prompt_override=prompt + "事实。" * 100,
        )
    assert received == [prompt]


def test_minimax_draws_visual_scene_not_copied_statement_provenance(monkeypatch, tmp_path):
    fact = "俄罗斯总统普京警告西方，俄罗斯已准备好动用一切武器保护加里宁格勒。"
    scene = "俄罗斯总统普京就加里宁格勒防务议题发出警告的概念示意，以非写实侧影表达其单方声明立场，背景为纯色色面。"
    body = f"内容：\n{fact}\n\n评价：\n后续反应仍待确认。"
    requests = []

    def fake_request(**kwargs):
        requests.append(kwargs["payload"])
        return {"data": {"image_base64": [base64.b64encode(b"fake image fixture bytes").decode()]}}

    monkeypatch.setattr(minimax_images, "load_minimax_image_config", lambda: SimpleNamespace())
    monkeypatch.setattr(minimax_images, "_model_candidates", lambda _: ["image-01"])
    monkeypatch.setattr(minimax_images, "_request_json", fake_request)
    paths, _, fallback = create_post._fetch_daily_news_related_images(
        title="普京就加里宁格勒发出警告", body=body, topics=["每日新闻"],
        prompt_hint=fact + scene, dest_dir=tmp_path, ai_first=True,
        provider="minimax", image_policy="ai_required",
    )
    assert len(paths) == len(requests) == 1 and fallback is None
    assert requests[0]["prompt"].startswith(scene + "\n")
    assert fact not in requests[0]["prompt"]
    assert "动用一切武器" not in requests[0]["prompt"]
    assert auto_image._news_image_fact_context(body) == fact


def test_unverified_provenance_and_nonconcept_scenes_are_not_silently_removed():
    for event in (
        "未核实的事实。甲方声明的概念示意，背景纯色。",
        "甲方发表声明。甲方代表与乙方会面。",
    ):
        prompt = auto_image._build_aliyun_image_prompt(
            title="甲方声明", body="甲方发表声明。", topics=[], prompt_hint=event,
        )
        assert prompt.startswith(event + "\n")


@pytest.mark.parametrize("first_score,second_score", [(85, 95), (65, 50), (45, 80)])
def test_existing_gate_reuses_good_first_image_and_bounds_repair(first_score, second_score, tmp_path, monkeypatch):
    from apps import cli
    from apps.cli import _review_with_bounded_image_repair, _vision_review_passes
    from src.workflow.vision_review import VisionReviewResult

    post = _post()
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    first_path.write_bytes(b"original image bytes")
    monkeypatch.setattr(cli, "save_post", lambda value: None)
    post.assets = [AssetInfo(path=str(first_path))]
    calls = []
    redraws = []

    def review(post, **kwargs):
        calls.append(post.assets[0].path)
        score = first_score if len(calls) == 1 else second_score
        return VisionReviewResult(ok=score >= 70, score=score, issues=(), retry_prompt="船体状态不符")

    def redraw(post, prompt):
        redraws.append(prompt)
        second_path.write_bytes(b"redrawn image bytes")
        post.assets = [AssetInfo(path=str(second_path))]
        return True

    result, repairs, errors, history = _review_with_bounded_image_repair(
        post, config=None, viewpoint="", max_repairs=99, review_fn=review, regenerate_fn=redraw,
    )
    assert repairs == len(redraws) == (0 if first_score >= 70 else 1)
    assert len(calls) == 1 + repairs
    assert result.score == (first_score if not repairs else max(first_score, second_score))
    assert post.assets[0].path == str(second_path if repairs and second_score > first_score else first_path)
    assert not _vision_review_passes(VisionReviewResult(ok=True, score=69, issues=(), retry_prompt=""))
    assert not _vision_review_passes(VisionReviewResult(ok=False, score=95, issues=(), retry_prompt=""))
