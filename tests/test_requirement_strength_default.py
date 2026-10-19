import json

import pytest
from playwright.sync_api import expect

from backend.task_recognition import parse_task, validate_candidate
from src.agent.plan_contract import apply_edits, digest, execution_fields, normalize_plan, verify_execution
from test_plan_contract import legacy
from test_plan_editor_browser import EditableScenario
from test_progress_browser import browser, prepare
from test_task_calibration import calibration, module, wait_recognition, workbench
from test_task_plan_v3 import proposed


@pytest.mark.parametrize('strength', [None, '', 'omitted'])
@pytest.mark.parametrize('kind', ['daily_news', 'daily_ai_digest', 'daily_wow', 'daily_wool'])
def test_missing_strength_defaults_to_executable_preference(kind, strength):
    plan = normalize_plan(legacy())
    job = plan['jobs'][0]
    job.update(kind=kind, count=1, search_keywords=['OpenAI'], topic_preferences=['免费试用'],
               topic_brief='优先关注无需信用卡的免费额度', content_constraints=[])
    job.pop('topic_brief_strength_hash', None)
    if strength == 'omitted':
        job.pop('topic_brief_strength', None)
    else:
        job['topic_brief_strength'] = strength
    result = normalize_plan(plan)
    assert result['executable'], result['field_errors']
    assert result['jobs'][0]['topic_brief_strength'] == 'preference'
    assert '优先关注无需信用卡的免费额度' in result['jobs'][0]['prompt']
    frozen = execution_fields(result)
    verify_execution(frozen)
    assert frozen['jobs'][0]['topic_brief_strength'] == 'preference'
    assert normalize_plan(result) == result


@pytest.mark.parametrize('kind', ['daily_news', 'daily_wool'])
def test_calibration_null_strength_is_defaulted_before_adoption(calibration, kind):
    current, _, _, base, _, _ = calibration
    value = proposed()
    value['jobs'][0].update(kind=kind, count=1, topic_brief='优先关注无需信用卡的免费额度',
                             topic_brief_strength=None)
    result = validate_candidate(parse_task(json.dumps(value)), '生成一篇，优先关注免费额度', base, current)
    assert result['executable'], result['field_errors']
    assert result['jobs'][0]['topic_brief_strength'] == 'preference'
    assert '优先关注无需信用卡的免费额度' in execution_fields(result)['jobs'][0]['prompt']


def test_default_never_softens_explicit_hard_requirement_or_unknown_strength():
    plan = normalize_plan(legacy())
    job = plan['jobs'][0]
    job.update(topic_brief='必须两篇国际冲突', topic_brief_strength='requirement',
               topic_brief_strength_hash=digest('必须两篇国际冲突'))
    hard = normalize_plan(plan)
    assert not hard['executable']
    assert hard['jobs'][0]['topic_brief_strength'] == 'requirement'
    assert any(issue['code'] == 'UNSUPPORTED_HARD_REQUIREMENT' for issue in hard['field_errors'])
    job['topic_brief_strength'] = 'unrecognized'
    invalid = normalize_plan(plan)
    assert not invalid['executable']
    assert invalid['jobs'][0]['topic_brief_strength'] == 'unrecognized'


@pytest.mark.parametrize('kind', ['daily_news', 'daily_wool'])
def test_calibration_adopt_reload_needs_no_manual_strength_click(calibration, monkeypatch, kind):
    current, client, cid, base, body, _ = calibration
    value = proposed()
    value['jobs'][0].update(kind=kind, count=1, topic_brief_strength=None,
                             topic_brief='优先关注OpenAI免费额度')
    monkeypatch.setattr(module, 'recognition_call', lambda config, payload: json.dumps(value, ensure_ascii=False))
    route = f'/api/conversations/{cid}/task-recognitions'
    response = client.post(route, headers={'X-Workbench': '1'}, json=body)
    assert response.status_code == 200, response.text
    rid = response.json()['id']
    record = wait_recognition(client, cid, rid)
    assert record['status'] == 'ready', record
    adopted = client.post(f'{route}/{rid}/adopt', headers={'X-Workbench': '1'},
                          json={'base_plan_version': base['version']})
    assert adopted.status_code == 200, adopted.text
    plan = adopted.json()['plan']
    assert plan['jobs'][0]['topic_brief_strength'] == 'preference'
    assert plan['executable'], plan['field_errors']
    verify_execution(execution_fields(plan))
    saved = current.get_agent_conversation(cid)
    assert saved['plans'][-1]['jobs'][0]['topic_brief_strength'] == 'preference'
    assert saved['runs'] == []


@pytest.mark.parametrize('kind', ['daily_news', 'daily_wool'])
def test_editor_defaults_and_keeps_strength_when_text_changes(browser, kind, tmp_path):
    scenario = EditableScenario()
    scenario.plan['jobs'][0].update(kind=kind, count=1, title='每日新闻' if kind == 'daily_news' else '每日羊毛',
        search_keywords=[], topic_preferences=[], topic_brief='', legacy_extra_requirements='')
    scenario.plan = normalize_plan(scenario.plan)
    scenario.conversation['plans'] = [scenario.plan]
    context, page, errors = prepare(browser, scenario)
    try:
        page.get_by_role('button', name='编辑计划', exact=True).click()
        panel = page.get_by_role('dialog', name='编辑本次计划')
        title = scenario.plan['jobs'][0]['title']
        panel.get_by_label(title + '补充要求', exact=True).fill('优先关注免费额度')
        preference = panel.get_by_role('radio', name='选题偏好', exact=True)
        required = panel.get_by_role('radio', name='必须满足', exact=True)
        expect(preference).to_be_checked()
        required.check()
        panel.get_by_label(title + '补充要求', exact=True).fill('优先关注无需信用卡的免费额度')
        expect(required).to_be_checked()
        preference.check()
        panel.get_by_label(title + '补充要求', exact=True).fill('优先关注OpenAI免费额度')
        expect(preference).to_be_checked()
        page.screenshot(path=str(tmp_path / (kind + '-default-preference.png')), full_page=True)
        panel.get_by_role('button', name='保存计划', exact=True).click()
        expect(panel).not_to_be_visible()
        assert scenario.saved_payload['editable_fields']['jobs'][0]['topic_brief_strength'] == 'preference'
        assert scenario.plan['executable'], scenario.plan['field_errors']
        page.reload()
        page.get_by_role('button', name='编辑计划', exact=True).click()
        expect(page.get_by_role('dialog').get_by_role('radio', name='选题偏好', exact=True)).to_be_checked()
        assert not any('/confirm' in path for _, path in scenario.calls)
        assert not errors
    finally:
        context.close()


def test_old_null_calibration_candidate_shows_selected_default(browser):
    scenario = EditableScenario()
    scenario.finish()
    scenario.record['candidate']['jobs'][0]['topic_brief_strength'] = None
    scenario.conversation['task_recognitions'] = [scenario.record]
    context, page, errors = prepare(browser, scenario)
    try:
        page.get_by_role('button', name='编辑候选', exact=True).click()
        expect(page.get_by_role('dialog').get_by_role('radio', name='选题偏好', exact=True)).to_be_checked()
        assert not errors
    finally:
        context.close()
