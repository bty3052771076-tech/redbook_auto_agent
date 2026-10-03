from src.images.auto_image import _build_aliyun_image_prompt


def test_conflict_statement_uses_briefing_scene_not_aircraft():
    prompt = _build_aliyun_image_prompt(
        title="总统就地区冲突发表声明",
        body="内容：总统就地区冲突发表声明。",
        topics=["每日新闻"],
        prompt_hint="总统就地区冲突发表强硬表态",
    )

    assert "总统就地区冲突发表强硬表态" in prompt
    assert "声明或计划仅表现表达或讨论，不画成已实施结果" in prompt
    assert "战机" not in prompt
    assert "飞机" not in prompt


def test_law_passage_uses_legislative_scene():
    prompt = _build_aliyun_image_prompt(
        title="参议院通过大学体育法案",
        body="内容：参议院通过大学体育法案。",
        topics=["每日新闻"],
        prompt_hint="参议院通过大学体育法案",
    )

    assert "参议院通过大学体育法案" in prompt
    assert "战机" not in prompt


def test_smart_ring_ipo_withdrawal_has_specific_finance_scene():
    prompt = _build_aliyun_image_prompt(
        title="Oura撤回百亿美元美国上市计划",
        body="内容：可穿戴设备公司Oura决定暂缓美国上市。",
        topics=["每日新闻"],
        prompt_hint="Oura撤回百亿美元美国上市计划",
    )

    assert "Oura撤回百亿美元美国上市计划" in prompt
    assert prompt.startswith("Oura撤回百亿美元美国上市计划\n")
    assert "声明或计划仅表现表达或讨论，不画成已实施结果" in prompt
    assert "交易所大厅" not in prompt
    assert "海边" not in prompt
    assert not prompt.endswith("或…")
