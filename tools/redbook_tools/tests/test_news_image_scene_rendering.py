from src.images.auto_image import _build_aliyun_image_prompt


def test_scene_is_not_formatted_as_a_news_heading_or_explanatory_layout():
    scene = "美国职业棒球大联盟负责人阅读留白信件，示意说明取消办赛计划，背景为纯色。"
    prompt = _build_aliyun_image_prompt(title="MLB取消国家公园办赛计划", body="", topics=[], prompt_hint=scene)
    assert prompt.startswith(scene + "\n")
    assert "场景：" not in prompt
    assert "不分栏" in prompt and "不留文字区域" in prompt
    assert "MLB取消国家公园办赛计划" not in prompt
    assert "无文字或标识" in prompt
    for unrelated in ("武器", "伤亡", "灾难", "特定制服"):
        assert unrelated not in prompt


def test_rendering_keeps_verified_scene_and_negative_state_without_cutting():
    scene = "救援船在远海接应已获救乘客，原游艇沉没，不在画面中出现，背景是开阔海面。"
    prompt = _build_aliyun_image_prompt(title="远海救援", body="", topics=[], prompt_hint=scene)
    assert prompt.count(scene) == 1
    assert "声明或计划仅表现表达或讨论，不画成已实施结果" in prompt
    assert "未经证实的指控不画成事实" in prompt
    assert "角色、地点与状态以已证实事实为准" in prompt
    assert "仅画场景已列出的必要人物、物件与环境" in prompt
