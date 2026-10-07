from backend.task_recognition import recognition_payload
from test_task_calibration import workbench


def test_reader_preference_and_metrics_refresh_are_declared_host_capabilities(workbench):
    text = "生成10条每日新闻，刷新帖子数据根据用户偏好选择新闻"
    base = workbench._parse_agent_message(text)
    payload = recognition_payload(text, base, workbench)
    assert payload["capabilities"]["reader_preferences"] == "selection_soft_preference"
    assert payload["capabilities"]["published_metrics_sync"] == "preflight_freshness_check"
    assert all(row["quote"] in text for row in payload["user_evidence"])
    assert "prompt" not in payload["local_plan"]["jobs"][0]
