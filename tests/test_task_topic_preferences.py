from copy import deepcopy

import pytest

from apps.web_service import Workbench
from backend.task_recognition import RecognizedTask, validate_candidate
from src.agent.task_intent import extract_job_topics


MESSAGE = """生成10条今天的每日新闻，速度优先，保存到小红书创作者中心草稿箱，不公开发布。
每日新闻优先关注知名平台禁令与解禁、隐私与年龄核验、账号规则变化、游戏订阅退款、消费者权益争议、真实且有意外转折的社会事件。
每日新闻优先筛选知名主体发生意外变化并涉及普通人规则或权益的事件，约3条作为软偏好，不设硬配额，其余兼顾国际冲突、科技产业、社会民生、财经产业。"""


class ParseOnlyWorkbench:
    _agent_job_enabled = staticmethod(Workbench._agent_job_enabled)
    _agent_count = staticmethod(Workbench._agent_count)

    def providers(self):
        return {"bindings": {"agent": "", "writer": "", "image": ""}}

    def settings(self):
        return {"performance_mode": "balanced"}

    def environment(self):
        return {}


def model_task(*, keywords=None, count=10, topic_brief="", ai_keywords=None):
    jobs = [{"kind": "daily_news", "count": count, "keywords": keywords or [],
             "topic_brief": topic_brief, "evaluation_viewpoint": None}]
    if ai_keywords is not None:
        jobs.append({"kind": "daily_ai_digest", "count": 1, "keywords": ai_keywords,
                     "topic_brief": "模型发布", "evaluation_viewpoint": None})
    return RecognizedTask.model_validate({
        "schema_version": "task-recognition.v2", "intent": "generate", "jobs": jobs,
        "options": {"delivery": "save_draft", "platform": "xhs", "performance_mode": "speed",
                    "image_score_required": None, "skip_quota_sync": None},
        "provider_requests": {"agent": None, "writer": None, "image": None},
        "requirements": [], "clarifications": [], "summary": "生成新闻并保存草稿",
    })


def test_soft_preferences_in_topic_brief_do_not_require_identical_keyword_array():
    current = ParseOnlyWorkbench()
    base = Workbench._parse_agent_message(current, MESSAGE)
    before = deepcopy(base)
    result = validate_candidate(model_task(topic_brief="偏向有反差的消费者权益事件"),
                                MESSAGE, base, current)
    assert result["executable"] is True
    assert result["jobs"][0]["count"] == 10
    assert result["jobs"][0]["keyword_mode"] == "default"
    assert result['jobs'][0]['topic_brief'] == '偏向有反差的消费者权益事件'
    assert '约3条作为软偏好' not in result['jobs'][0]['prompt']
    assert any(w['code'] == 'PLAN_DIFFERS' for w in result['warnings'])
    assert result["delivery"] == "save_draft"
    assert base == before


def test_ratio_and_instruction_clauses_are_not_displayed_as_topic_keywords():
    topic = extract_job_topics(MESSAGE, ["daily_news"])["daily_news"]
    assert "知名平台禁令与解禁" in topic["keywords"]
    assert "科技产业" in topic["keywords"]
    assert "约3条作为软偏好" not in topic["keywords"]
    assert "不设硬配额" not in topic["keywords"]
    assert "其余兼顾国际冲突" not in topic["keywords"]
    assert "国际冲突" in topic["keywords"]
    assert "约3条作为软偏好" in topic["topic_brief"]


@pytest.mark.parametrize("topics", [
    "关键词：伊朗、关税；每日新闻优先关注消费者权益、隐私",
    "优先关注消费者权益、隐私；每日新闻关键词：伊朗、关税",
])
def test_explicit_filter_keywords_are_not_polluted_by_soft_preferences(topics):
    text = "生成10条每日新闻；" + topics
    current = ParseOnlyWorkbench()
    base = Workbench._parse_agent_message(current, text)
    assert base["jobs"][0]["keywords"] == ["伊朗", "关税"]
    result = validate_candidate(model_task(keywords=["伊朗", "关税"]), text, base, current)
    assert result["executable"] is True
    assert result["jobs"][0]["keyword_mode"] == "filter"
    assert result['jobs'][0]['topic_brief'] == ''
    assert '消费者权益' not in result['jobs'][0]['prompt']


def test_actual_missing_explicit_keyword_reports_which_word_and_column():
    text = "生成10条每日新闻，关键词：伊朗、关税"
    current = ParseOnlyWorkbench()
    base = Workbench._parse_agent_message(current, text)
    result = validate_candidate(model_task(keywords=['伊朗']), text, base, current)
    assert result['jobs'][0]['search_keywords'] == ['伊朗']
    assert any(w['code'] == 'PLAN_DIFFERS' for w in result['warnings'])
    assert base['jobs'][0]['keywords'] == ['伊朗', '关税']


def test_soft_preference_allowance_does_not_weaken_another_columns_explicit_filter():
    text = "生成10条每日新闻，优先关注消费者权益；生成1条每日AI讯息，关键词：DeepSeek"
    current = ParseOnlyWorkbench()
    base = Workbench._parse_agent_message(current, text)
    result = validate_candidate(model_task(ai_keywords=["DeepSeek"]), text, base, current)
    assert result["jobs"][0]["keyword_mode"] == "default"
    assert result["jobs"][1]["prompt"] == "DeepSeek\n选题要求：模型发布"
    changed = validate_candidate(model_task(ai_keywords=[]), text, base, current)
    assert changed['jobs'][1]['search_keywords'] == []
    assert changed['executable'] is True
    assert result['jobs'][1]['search_keywords'] == ['DeepSeek']


def test_long_natural_preference_is_preserved_without_keyword_length_failure():
    detail = "优先筛选" + "有可靠原始信源并且能说明普通用户权益变化的事件" * 5
    topic = extract_job_topics("生成10条每日新闻；" + detail, ["daily_news"])["daily_news"]
    assert topic["keywords"] == []
    assert topic["keyword_mode"] == "preference"
    assert topic["topic_brief"] == detail


def test_task_ratio_is_not_confused_with_a_news_subject_starting_with_digits():
    topic = extract_job_topics("生成10条每日新闻，优先关注3D打印、5G通信，约3条作为偏好",
                               ["daily_news"])["daily_news"]
    assert topic["keywords"] == ["3D打印", "5G通信"]
