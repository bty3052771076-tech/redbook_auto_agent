from copy import deepcopy
import json
import pytest

from playwright.sync_api import expect

from test_progress_browser import CID, browser, prepare
from test_task_calibration_browser import CalibrationScenario
from src.agent.plan_contract import normalize_plan, apply_edits


class EditableScenario(CalibrationScenario):
    def __init__(self):
        super().__init__()
        self.plan = normalize_plan(self.plan)
        self.plan['jobs'][0].update(count=10, search_keywords=['隐私'], topic_preferences=['年龄核验'])
        self.plan = normalize_plan(self.plan)
        self.conversation['plans'] = [self.plan]
        self.conversation['conversation_revision'] = 4
        self.saved_payload = None
        self.conflict = False
        self.restored = None
        self.calibration_payload = None

    def route(self, route):
        request = route.request
        path = request.url.split('8786', 1)[1]
        if path.endswith('/task-recognitions') and request.method == 'POST':
            self.calibration_payload = request.post_data_json
            self.record.update(base_plan_id=self.plan['id'], base_plan_version=self.plan['version'])
        if path.endswith('/copy'):
            self.calls.append((request.method,path))
            revised=deepcopy(self.plan)
            for key in ('execution_request_id','job_id','submission_state','frozen_execution'):
                revised.pop(key,None)
            revised.update(id='6'*32,version=self.plan['version']+1,parent_plan_id=self.plan['id'],last_editor='user')
            self.plan=revised
            self.conversation['plans'].append(revised)
            self.conversation['conversation_revision']+=1
            route.fulfill(status=200,content_type='application/json',body=json.dumps({'plan':revised},ensure_ascii=False))
            return
        if path.endswith('/restore'):
            self.calls.append((request.method, path))
            self.restored = request.post_data_json
            target = next(row for row in self.conversation['plans'] if row['id'] == self.restored['restore_plan_id'])
            revised = deepcopy(target)
            revised.update(id='8'*32, version=self.plan['version'] + 1, last_editor='user', parent_plan_id=self.plan['id'])
            self.plan = revised
            self.conversation['plans'].append(revised)
            self.conversation['conversation_revision'] += 1
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'plan': revised}, ensure_ascii=False))
            return
        if path.endswith('/revisions'):
            self.calls.append((request.method, path))
            self.saved_payload = request.post_data_json
            assert request.headers['idempotency-key']
            if self.conflict:
                route.fulfill(status=409, content_type='application/json', body=json.dumps({'error': '计划已更新，编辑内容已保留'}))
                return
            revised = apply_edits(self.plan, self.saved_payload['editable_fields'])
            revised.update(id='9'*32, version=2, last_editor='user')
            self.plan = revised
            self.conversation['plans'].append(revised)
            self.conversation['conversation_revision'] += 1
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'plan': revised}, ensure_ascii=False))
            return
        super().route(route)


def test_unsupported_constraint_can_be_removed_without_starting_task(browser):
    scenario = EditableScenario()
    scenario.plan['jobs'][0]['content_constraints'] = [
        {'type': 'category_count', 'value': 2, 'category': '国际冲突'},
    ]
    scenario.plan = normalize_plan(scenario.plan)
    scenario.conversation['plans'] = [scenario.plan]
    context, page, errors = prepare(browser, scenario)
    expect(page.get_by_role('button', name='确认并执行', exact=True)).to_be_disabled()
    page.get_by_role('button', name='编辑计划', exact=True).click()
    panel = page.get_by_role('dialog', name='编辑本次计划')
    expect(panel.get_by_text('category_count', exact=True)).to_be_visible()
    panel.get_by_role('button', name='移除约束1', exact=True).click()
    panel.get_by_role('button', name='保存计划', exact=True).click()
    expect(panel).not_to_be_visible()
    expect(page.get_by_role('button', name='确认并执行', exact=True)).to_be_enabled()
    assert scenario.saved_payload['editable_fields']['jobs'][0]['content_constraints'] == []
    assert not any('/confirm' in path for _, path in scenario.calls)
    assert not errors
    context.close()


def test_frozen_plan_copy_requires_confirmation_and_creates_editable_revision(browser):
    scenario=EditableScenario()
    scenario.plan['execution_request_id']='a'*32
    scenario.plan['submission_state']='submitted'
    context,page,errors=prepare(browser,scenario)
    expect(page.get_by_role('button',name='编辑计划',exact=True)).to_be_disabled()
    page.get_by_role('button',name='复制为新计划',exact=True).click()
    assert not any(path.endswith('/copy') for _,path in scenario.calls)
    page.get_by_role('button',name='确认复制为新计划',exact=True).click()
    expect(page.get_by_role('button',name='编辑计划',exact=True)).to_be_enabled()
    page.get_by_role('button',name='编辑计划',exact=True).click()
    expect(page.get_by_role('dialog',name='编辑本次计划')).to_be_visible()
    assert sum(path.endswith('/copy') for _,path in scenario.calls)==1
    assert not any('/confirm' in path for _,path in scenario.calls)
    assert not errors
    context.close()


def test_edit_count_topics_save_refresh_does_not_execute(browser, tmp_path):
    scenario = EditableScenario()
    context, page, errors = prepare(browser, scenario)
    page.get_by_role('button', name='编辑计划', exact=True).click()
    panel = page.get_by_role('dialog', name='编辑本次计划')
    expect(panel).to_be_visible()
    panel.get_by_label('每日新闻篇数').fill('5')
    expect(panel.get_by_text('本次人工修改', exact=True).first).to_be_visible()
    panel.get_by_role('button', name='移除年龄核验', exact=True).click()
    panel.get_by_label('每日新闻选题偏向', exact=True).fill('游戏退款')
    panel.get_by_label('每日新闻选题偏向', exact=True).press('Enter')
    panel.get_by_role('button', name='移除隐私', exact=True).click()
    page.screenshot(path=str(tmp_path / 'plan-editor-desktop.png'), full_page=True)
    panel.get_by_role('button', name='保存计划', exact=True).click()
    expect(panel).not_to_be_visible()
    expect(page.locator('.plan-pane')).to_contain_text('5 条')
    expect(page.locator('.plan-pane')).to_contain_text('游戏退款')
    expect(page.locator('.plan-pane')).not_to_contain_text('年龄核验')
    page.reload()
    expect(page.locator('.plan-pane')).to_contain_text('游戏退款')
    assert scenario.saved_payload['conversation_revision'] == 4
    assert not any('/confirm' in path for _, path in scenario.calls)
    assert not errors
    context.close()


def test_history_restore_requires_confirmation_and_does_not_execute(browser):
    scenario = EditableScenario()
    original = deepcopy(scenario.plan)
    changed = apply_edits(original, {'jobs': [{'target_job_id': original['jobs'][0]['job_id'], 'count': 5}]})
    changed.update(id='9'*32, version=2, parent_plan_id=original['id'], last_editor='user')
    scenario.plan = changed
    scenario.conversation['plans'].append(changed)
    context, page, errors = prepare(browser, scenario)
    page.get_by_text('计划修订记录', exact=True).click()
    page.get_by_role('button', name='恢复此版本', exact=True).click()
    assert scenario.restored is None
    page.get_by_role('button', name='确认恢复为新修订', exact=True).click()
    expect(page.locator('.plan-pane')).to_contain_text('10 条')
    assert scenario.restored['restore_plan_id'] == original['id']
    assert not any('/confirm' in path for _, path in scenario.calls)
    page.reload()
    expect(page.locator('.plan-pane')).to_contain_text('10 条')
    assert not errors
    context.close()


def test_conflict_retains_input_escape_requires_discard_and_mobile_fit(browser, tmp_path):
    scenario = EditableScenario()
    scenario.conflict = True
    context, page, errors = prepare(browser, scenario)
    page.get_by_role('button', name='编辑计划', exact=True).click()
    panel = page.get_by_role('dialog', name='编辑本次计划')
    panel.get_by_label('每日新闻篇数').fill('6')
    panel.get_by_role('button', name='保存计划', exact=True).click()
    expect(panel.get_by_role('alert')).to_contain_text('编辑内容已保留')
    expect(panel.get_by_label('每日新闻篇数')).to_have_value('6')
    panel.get_by_label('每日新闻篇数').press('Escape')
    expect(panel.get_by_role('button', name='继续编辑')).to_be_visible()
    panel.get_by_role('button', name='继续编辑').click()
    page.set_viewport_size({'width': 390, 'height': 844})
    page.screenshot(path=str(tmp_path / 'plan-editor-mobile.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    panel.get_by_label('每日新闻篇数').press('Escape')
    panel.get_by_role('button', name='放弃修改').click()
    expect(panel).not_to_be_visible()
    expect(page.get_by_role('button', name='编辑计划', exact=True)).to_be_focused()
    assert not errors
    context.close()


@pytest.mark.parametrize('choice', ['return', 'save_failure', 'save', 'discard'])
def test_dirty_plan_calibration_requires_explicit_saved_revision_choice(browser, choice):
    scenario = EditableScenario()
    scenario.conflict = choice == 'save_failure'
    original_id = scenario.plan['id']
    context, page, errors = prepare(browser, scenario)
    page.get_by_role('button', name='编辑计划', exact=True).click()
    panel = page.get_by_role('dialog', name='编辑本次计划')
    panel.get_by_label('每日新闻篇数').fill('6')
    panel.get_by_role('button', name='大模型校准', exact=True).click()
    expect(panel.get_by_role('button', name='返回编辑', exact=True)).to_be_focused()
    assert scenario.calibration_payload is None
    if choice == 'return':
        panel.get_by_role('button', name='返回编辑', exact=True).click()
        expect(panel.get_by_label('每日新闻篇数')).to_have_value('6')
        assert scenario.saved_payload is None and scenario.calibration_payload is None
    elif choice == 'save_failure':
        panel.get_by_role('button', name='保存后校准', exact=True).click()
        expect(panel.get_by_role('alert')).to_contain_text('编辑内容已保留')
        expect(panel.get_by_label('每日新闻篇数')).to_have_value('6')
        assert scenario.calibration_payload is None
    else:
        panel.get_by_role('button', name='保存后校准' if choice == 'save' else '放弃修改后校准', exact=True).click()
        expect(panel).not_to_be_visible()
        expect(page.get_by_role('button', name='校准中', exact=True)).to_be_disabled()
        assert scenario.calibration_payload['base_plan_id'] == ('9' * 32 if choice == 'save' else original_id)
        assert scenario.calibration_payload['base_plan_version'] == (2 if choice == 'save' else 1)
        assert scenario.plan['jobs'][0]['count'] == (6 if choice == 'save' else 10)
        assert sum(method == 'POST' and path.endswith('/task-recognitions') for method, path in scenario.calls) == 1
        assert (scenario.saved_payload is None) == (choice == 'discard')
    assert not any('/confirm' in path for _, path in scenario.calls)
    assert not errors
    context.close()


def test_chinese_composition_commit_does_not_submit_tag_until_explicit_enter(browser):
    scenario=EditableScenario()
    context,page,errors=prepare(browser,scenario)
    page.get_by_role('button',name='编辑计划',exact=True).click()
    panel=page.get_by_role('dialog',name='编辑本次计划')
    field=panel.get_by_label('每日新闻选题偏向',exact=True)
    field.evaluate("node => node.dispatchEvent(new CompositionEvent('compositionstart', {bubbles:true}))")
    field.fill('消费者退款')
    field.evaluate("node => node.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter',keyCode:229,isComposing:true,bubbles:true}))")
    field.evaluate("node => node.dispatchEvent(new CompositionEvent('compositionend', {data:'消费者退款',bubbles:true}))")
    field.evaluate("node => node.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter',keyCode:229,isComposing:false,bubbles:true}))")
    expect(panel.get_by_role('button',name='移除消费者退款',exact=True)).to_have_count(0)
    expect(field).to_have_value('消费者退款')
    field.press('Enter')
    field.press('Enter')
    expect(panel.get_by_role('button',name='移除消费者退款',exact=True)).to_have_count(1)
    expect(field).to_have_value('')
    assert not any('/confirm' in path for _,path in scenario.calls) and not errors
    context.close()


@pytest.mark.parametrize('width',[1440,1024,768,390])
def test_long_tag_and_dirty_calibration_choice_fit_each_viewport(browser,tmp_path,width):
    scenario=EditableScenario()
    context,page,errors=prepare(browser,scenario)
    page.set_viewport_size({'width':width,'height':1000 if width>390 else 844})
    page.get_by_role('button',name='编辑计划',exact=True).click()
    panel=page.get_by_role('dialog',name='编辑本次计划')
    field=panel.get_by_label('每日新闻选题偏向',exact=True)
    field.fill('LongContinuousTopic'*4)
    field.press('Enter')
    panel.get_by_label('每日新闻补充要求',exact=True).fill('具体事件背景和规则变化。'*100)
    panel.get_by_role('button',name='大模型校准',exact=True).click()
    expect(panel.get_by_role('button',name='返回编辑',exact=True)).to_be_focused()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert panel.evaluate('node => node.scrollWidth <= node.clientWidth')
    bounds=panel.bounding_box()
    assert bounds['y'] >= 0 and bounds['y']+bounds['height'] <= page.viewport_size['height']+1
    footer=panel.locator('.pe-footer')
    buttons=footer.locator('button')
    assert buttons.count()==3
    assert footer.evaluate('node => Array.from(node.querySelectorAll("button")).every(button => { const r=button.getBoundingClientRect(); const f=node.getBoundingClientRect(); return r.left>=f.left && r.right<=f.right && r.top>=f.top && r.bottom<=f.bottom; })')
    page.screenshot(path=str(tmp_path/f'plan-calibration-{width}.png'),full_page=False)
    assert scenario.calibration_payload is None and not any('/confirm' in path for _,path in scenario.calls)
    assert not errors
    context.close()
