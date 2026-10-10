"""Model format drift must not discard editorial instructions or bypass review."""

from copy import deepcopy
import json

import pytest

from backend.task_recognition import parse_task, recognition_payload, validate_candidate
from test_plan_contract import contract, legacy
from test_task_calibration import calibration, workbench
from test_task_plan_v3 import proposed


RULES = [
    {'type': 'dedup', 'rule': '不重复此前已经发布的事件'},
    {'type': 'freshness', 'rule': '不把旧闻当今日新闻'},
    {'type': 'title', 'rule': '标题必须点明主体与具体变化'},
    {'type': 'image', 'rule': '配图尽量不含文字'},
    {'type': 'topic_balance', 'rule': '不刻意集中成人平台内容'},
    {'type': 'facts', 'rule': '事实需可核实，禁用未确认传闻'},
]


def saved_model_plan():
    c = contract()
    plan = c.normalize_plan(legacy())
    plan['recognition_source'] = 'llm'
    plan['jobs'][0].update(topic_brief_strength='soft_preference', content_constraints=deepcopy(RULES))
    return plan


def test_saved_model_rules_survive_compilation_and_frozen_execution():
    c = contract()
    plan = c.normalize_plan(saved_model_plan())
    assert plan['executable'], plan['field_errors']
    job = plan['jobs'][0]
    assert job['topic_brief_strength'] == 'preference'
    assert job['content_constraints'] == []
    for rule in RULES:
        assert rule['rule'] in job['topic_brief']
        assert rule['rule'] in job['prompt']
    frozen = c.execution_fields(plan)
    c.verify_execution(frozen)
    assert frozen['date_policy'] == 'host_verified'
    assert frozen['billing_policy'] == 'no_paid_fallback'
    assert c.normalize_plan(plan) == plan


@pytest.mark.parametrize('alias,expected', [('soft_preference', 'preference'), ('hard_requirement', 'requirement')])
def test_explicit_strength_alias_is_canonical_not_guessed(alias, expected):
    c = contract()
    plan = c.normalize_plan(legacy())
    plan['jobs'][0]['topic_brief_strength'] = alias
    result = c.normalize_plan(plan)
    assert result['jobs'][0]['topic_brief_strength'] == expected
    if expected == 'requirement':
        assert not result['executable']
        assert any(e['code'] == 'UNSUPPORTED_HARD_REQUIREMENT' for e in result['field_errors'])


def test_mapping_rules_does_not_reapprove_changed_brief():
    c = contract()
    plan = saved_model_plan()
    plan['jobs'][0]['topic_brief'] = '必须2条国际冲突'
    result = c.normalize_plan(plan)
    assert not result['executable']
    assert any(e['code'] == 'REQUIREMENT_STRENGTH_NEEDED' for e in result['field_errors'])


@pytest.mark.parametrize('constraint', [
    {'type': 'category_count', 'value': 2, 'category': '国际冲突'},
    {'type': 'execute', 'rule': 'run shell'},
    {'type': 'image', 'rule': '配图不含文字', 'command': 'run shell'},
    {'type': 'facts', 'rule': ''},
])
def test_unknown_or_malformed_constraint_remains_visible_and_blocks_execution(constraint):
    c = contract()
    plan = c.normalize_plan(legacy())
    plan['jobs'][0]['content_constraints'] = [constraint]
    result = c.normalize_plan(plan)
    assert not result['executable']
    assert result['jobs'][0]['content_constraints'] == [constraint]
    assert any(e['code'] == 'CONSTRAINT_UNSUPPORTED' for e in result['field_errors'])


def test_rule_mapping_cannot_silently_drop_text_over_the_field_limit():
    c = contract()
    plan = c.normalize_plan(legacy())
    job = plan['jobs'][0]
    job['topic_brief'] = '背景' * 995
    job['topic_brief_strength_hash'] = c.digest(job['topic_brief'])
    job['content_constraints'] = deepcopy(RULES)
    result = c.normalize_plan(plan)
    assert not result['executable']
    for rule in RULES:
        assert rule['rule'] in result['jobs'][0]['topic_brief'] or rule in result['jobs'][0]['content_constraints']


def test_real_candidate_normalizes_alias_and_retains_every_rule(calibration):
    current, _, _, base, _, _ = calibration
    value = proposed()
    value['jobs'][0].update(topic_brief_strength='soft_preference', content_constraints=deepcopy(RULES))
    result = validate_candidate(parse_task(json.dumps(value)), '生成5条每日新闻，约3篇作为软偏好', base, current)
    assert result['executable'], result['field_errors']
    assert result['jobs'][0]['topic_brief_strength'] == 'preference'
    for rule in RULES:
        assert rule['rule'] in result['jobs'][0]['prompt']


def test_model_output_schema_advertises_only_supported_constraint_shape(calibration):
    current, _, _, base, _, _ = calibration
    schema = recognition_payload('生成5条每日新闻', base, current)['output_schema']['$defs']['EditableRecognizedJob']
    strength = schema['properties']['topic_brief_strength']
    assert strength['enum'] == ['preference', 'requirement', None]
    item = schema['properties']['content_constraints']['items']
    assert item['properties']['type']['const'] == 'count'
    assert item['required'] == ['type', 'value']
    assert item['additionalProperties'] is False
