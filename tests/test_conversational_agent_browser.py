"""Real desktop/mobile browser with all HTTP intercepted; no production server."""

from copy import deepcopy
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from test_conversation_continuity_browser import ContinuityScenario, CID, BASE
from test_progress_browser import browser


OTHER = '9' * 32


class AssistantScenario(ContinuityScenario):
    def __init__(self):
        super().__init__()
        self.conversation.update(plans=[], runs=[], goals=[], turns=[], messages=[])
        self.conversations = {CID: self.conversation, OTHER: {**deepcopy(self.conversation), 'id': OTHER, 'title': 'Other fixture conversation'}}
        self.snapshots = {}
        for identity in self.conversations:
            self.snapshots[identity] = {'engine': 'langchain', 'state': 'idle', 'revision': 1,
                'execution_id': 'a' * 32 if identity == CID else 'b' * 32,
                'events': [], 'requests': [], 'actions': [], 'messages': [
                    {'id': identity + '-assistant', 'role': 'assistant', 'content': 'Fixture conversation ' + identity[:4], 'created_at': 1}]}
        self.snapshots[CID].update(state='waiting_approval', requests=[{
            'id': 'fixture-approval', 'kind': 'approval', 'tool': 'generate_news',
            'arguments': {'material_id': 'fixture-source', 'column': 'daily_news'},
            'reason': 'Generation needs one explicit permission; previously retained drafts stay unchanged.',
            'allowed_decisions': ['approve_once', 'approve_session', 'deny'], 'version': 3}],
            events=[{'id': 'fixture-event', 'kind': 'tool/started', 'created_at': 1,
                     'payload': {'text': 'Read-only fixture inspection complete.', 'analysis': 'private-analysis-must-not-render'}}])
        self.sent, self.controls, self.decisions, self.accepted = [], [], [], set()
        self.goal_commands = []

    def route(self, route):
        req = route.request
        path = urlsplit(req.url).path
        parts = path.split('/')
        status = 200
        if path == '/api/conversations':
            data = {'rows': list(self.conversations.values())}
        elif len(parts) == 4 and parts[:3] == ['', 'api', 'conversations'] and parts[3] in self.conversations:
            data = self.conversations[parts[3]]
        elif len(parts) >= 5 and parts[4] == 'assistant':
            self.calls.append((req.method, path))
            identity = parts[3]
            snapshot = self.snapshots[identity]
            if len(parts) == 5:
                data = snapshot
            elif parts[5] == 'goal':
                body = req.post_data_json
                self.goal_commands.append((identity, body))
                action = body['action']
                if body['version'] != snapshot.get('goal_version', 0):
                    route.fulfill(status=409, content_type='application/json',
                                  body=json.dumps({'detail': 'GOAL_CHANGED: 目标已更新，请刷新'}))
                    return
                goal = dict(snapshot.get('goal') or {})
                if action in {'set', 'edit'}:
                    goal.update({k: body[k] for k in ('objective', 'stage', 'target', 'max_turns')})
                if action == 'set':
                    goal.update(id='new-fixture-goal', state='active', turns=0, reason='',
                                counts=dict(local=0, reviewed=0, saved=0))
                elif action in {'pause', 'resume'}:
                    goal['state'] = 'paused' if action == 'pause' else 'active'
                snapshot.update(goal=goal if action != 'clear' else None,
                                goal_version=body['version'] + 1, revision=snapshot['revision'] + 1)
                data = snapshot
            elif parts[5] == 'messages':
                body = req.post_data_json
                self.sent.append((identity, body, req.headers.get('idempotency-key')))
                if body['client_message_id'] not in self.accepted:
                    self.accepted.add(body['client_message_id'])
                    snapshot['messages'].append({'id': body['client_message_id'], 'role': 'user', 'content': body['content'], 'created_at': 2})
                    snapshot.update(state='running', revision=snapshot['revision'] + 1)
                if self.fail_message_once:
                    self.fail_message_once = False
                    route.abort('connectionfailed')
                    return
                data, status = snapshot, 202
            elif parts[5] == 'control':
                self.controls.append((identity, req.post_data_json))
                snapshot.update(state='stopping', revision=snapshot['revision'] + 1)
                data = snapshot
            elif parts[5] == 'requests':
                self.decisions.append((identity, req.post_data_json))
                snapshot.update(state='idle' if req.post_data_json['decision'] == 'deny' else 'running',
                                requests=[], revision=snapshot['revision'] + 1)
                data, status = snapshot, 202
            else:
                pytest.fail('Unexpected assistant route: ' + path)
        else:
            return super().route(route)
        route.fulfill(status=status, content_type='application/json', body=json.dumps(data))


def open_assistant(browser, scenario, viewport):
    context = browser.new_context(viewport=viewport, service_workers='block')
    dist = Path(__file__).resolve().parents[1] / 'frontend/dist'

    def frontend(route):
        path = urlsplit(route.request.url).path
        asset = (dist / path.lstrip('/')).resolve() if path != '/' else dist / 'index.html'
        assert asset.is_relative_to(dist) and asset.is_file(), path
        route.fulfill(content_type=mimetypes.guess_type(str(asset))[0] or 'application/octet-stream', body=asset.read_bytes())

    context.route('**/*', frontend)
    context.route('**/api/**', scenario.route)
    context.add_init_script(f"localStorage.setItem('agent-conversation', '{CID}')")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(BASE)
    expect(page.locator('#dialogue-prompt')).to_be_visible()
    expect(page.locator('#dialogue-conversation-select')).to_have_value(CID)
    return context, page, errors


@pytest.mark.parametrize('viewport', [{'width': 1366, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'mobile'])
def test_assistant_permission_controls_and_layout(browser, tmp_path, viewport):
    scenario = AssistantScenario()
    context, page, errors = open_assistant(browser, scenario, viewport)
    try:
        expect(page.get_by_label('\u7533\u8bf7\uff1agenerate_news')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        expect(page.locator('body')).not_to_contain_text('private-analysis-must-not-render')
        page.screenshot(path=str(tmp_path / f"assistant-approval-{viewport['width']}.png"), full_page=True)
        page.get_by_label('\u672c\u4f1a\u8bdd\u6700\u591a\u8c03\u7528\u6b21\u6570').fill('3')
        page.get_by_role('button', name='\u6388\u6743\u672c\u4f1a\u8bdd 3 \u6b21').click()
        expect(page.get_by_label('\u7533\u8bf7\uff1agenerate_news')).to_have_count(0)
        assert scenario.decisions == [(CID, {'decision': 'approve_session', 'version': 3, 'max_calls': 3})]
        page.get_by_role('button', name='\u6682\u505c\u5bf9\u8bdd', exact=True).click()
        expect(page.get_by_role('status').filter(has_text='\u7b49\u5f85\u6267\u884c\u8fdb\u7a0b\u786e\u8ba4\u505c\u6b62')).to_be_visible()
        scenario.snapshots[CID].update(state='paused', revision=10)
        page.get_by_role('button', name='\u5237\u65b0\u72b6\u6001', exact=True).click()
        page.get_by_role('button', name='\u7ee7\u7eed\u5bf9\u8bdd', exact=True).click()
        expect(page.get_by_role('button', name='\u7ee7\u7eed\u5bf9\u8bdd', exact=True)).to_have_count(0)
        assert scenario.sent[0][1]['content'] == '\u7ee7\u7eed'
        page.get_by_role('button', name='\u53d6\u6d88\u6267\u884c', exact=True).click()
        scenario.snapshots[CID].update(state='cancelled', revision=20)
        page.get_by_role('button', name='\u5237\u65b0\u72b6\u6001', exact=True).click()
        expect(page.get_by_role('button', name='\u53d6\u6d88\u6267\u884c', exact=True)).to_be_disabled()
        page.reload()
        expect(page.locator('#dialogue-prompt')).to_be_visible()
        assert len(scenario.sent) == 1
        assert [body['action'] for identity, body in scenario.controls] == ['pause', 'cancel']
        assert all(body['execution_id'] == 'a' * 32 for identity, body in scenario.controls)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / f"assistant-cancelled-{viewport['width']}.png"), full_page=True)
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize('width,height', [(1440, 900), (390, 844), (320, 740)])
def test_composer_focus_keeps_label_and_send_separate(browser, tmp_path, width, height):
    scenario = AssistantScenario()
    scenario.snapshots[CID].update(state='idle', requests=[])
    context, page, errors = open_assistant(browser, scenario, {'width': width, 'height': height})
    try:
        field = page.locator('#dialogue-prompt')
        button = page.get_by_role('button', name='\u53d1\u9001', exact=True)
        expect(button).to_be_disabled()
        field.fill('Check the original source.')
        field.press('Tab')
        expect(button).to_be_focused()
        button.press('Shift+Tab')
        expect(field).to_be_focused()
        for resized in (False, True):
            if resized:
                field.evaluate("(field) => { field.style.height = '180px'; }")
            geometry = field.evaluate("""(field) => {
                const style = getComputedStyle(field);
                const input = field.getBoundingClientRect();
                const label = field.labels[0].getBoundingClientRect();
                const button = field.form.querySelector('button').getBoundingClientRect();
                return {
                    outline: style.outlineStyle,
                    outset: parseFloat(style.outlineWidth) + parseFloat(style.outlineOffset),
                    labelGap: input.top - label.bottom,
                    buttonGap: button.top - input.bottom,
                    height: input.height,
                };
            }""")
            assert geometry['outline'] == 'solid' and geometry['outset'] <= 0, geometry
            assert geometry['labelGap'] >= 8 and geometry['buttonGap'] >= 8, geometry
            assert geometry['height'] >= (180 if resized else 88), geometry
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('.dialogue-composer').screenshot(path=str(tmp_path / f'composer-focus-{width}.png'))
        field.press('Enter')
        assert scenario.sent == []
        field.press('Control+Enter')
        expect(field).to_have_value('')
        assert len(scenario.sent) == 1
        assert errors == []
    finally:
        context.close()


def test_failed_message_retry_keeps_client_identity_and_refresh_is_readonly(browser):
    scenario = AssistantScenario()
    scenario.snapshots[CID].update(state='idle', requests=[])
    scenario.fail_message_once = True
    context, page, errors = open_assistant(browser, scenario, {'width': 1366, 'height': 900})
    try:
        page.locator('#dialogue-prompt').fill('Offline fixture message.')
        page.get_by_role('button', name='\u53d1\u9001', exact=True).click()
        expect(page.get_by_role('alert')).to_be_visible()
        page.get_by_role('button', name='\u53d1\u9001', exact=True).click()
        expect(page.locator('#dialogue-prompt')).to_have_value('')
        assert len(scenario.sent) == 2 and len(scenario.accepted) == 1
        assert scenario.sent[0][1]['client_message_id'] == scenario.sent[1][1]['client_message_id']
        assert scenario.sent[0][2] == scenario.sent[1][2]
        page.reload()
        expect(page.locator('#dialogue-prompt')).to_be_visible()
        assert len(scenario.sent) == 2
        assert errors == []
    finally:
        context.close()


def test_denial_and_conversation_switch_do_not_leak_pending_request(browser):
    scenario = AssistantScenario()
    context, page, errors = open_assistant(browser, scenario, {'width': 1366, 'height': 900})
    try:
        page.locator('#dialogue-conversation-select').select_option(OTHER)
        expect(page.get_by_label('\u7533\u8bf7\uff1agenerate_news')).to_have_count(0)
        expect(page.get_by_text('Fixture conversation 9999', exact=True)).to_be_visible()
        page.locator('#dialogue-conversation-select').select_option(CID)
        page.get_by_role('button', name='\u62d2\u7edd\uff080 \u6b21\u6267\u884c\uff09', exact=True).click()
        expect(page.get_by_label('\u7533\u8bf7\uff1agenerate_news')).to_have_count(0)
        assert scenario.decisions == [(CID, {'decision': 'deny', 'version': 3})]
        assert scenario.sent == [] and scenario.snapshots[CID]['actions'] == []
        assert errors == []
    finally:
        context.close()


def test_result_counts_request_subject_and_readonly_navigation(browser):
    scenario = AssistantScenario()
    source = '可核验的新闻选题'
    def action(identity, tool, **result):
        return dict(id=identity, tool=tool, status='succeeded', result=result)
    scenario.snapshots[CID]['actions'] = [
        action('sources', 'list_materials', materials=[dict(material_id='fixture-source', title=source)]),
        action('writer', 'generate_news', post_ids=['p1', 'p2']),
        action('duplicate', 'generate_news', post_ids=['p1']),
        action('stored', 'save_draft', post_id='p1', platform_receipt_confirmed=True),
        action('no-receipt', 'save_draft', post_id='p2', platform_receipt_confirmed=False),
    ]
    context, page, errors = open_assistant(browser, scenario, {'width': 390, 'height': 844})
    try:
        expect(page.get_by_text(source, exact=True)).to_be_visible()
        overview = page.locator('.dialogue-overview')
        expect(overview).to_contain_text('本地生成 2')
        expect(overview).to_contain_text('平台已确认 1')
        expect(page.locator('.dialogue-events li')).to_have_count(0)
        count = page.get_by_label('本会话最多调用次数')
        count.fill('4')
        page.get_by_role('button', name='工具与产物', exact=True).click()
        expect(page.locator('[data-action-id=no-receipt]')).to_contain_text('暂不计入完成数')
        expect(page.locator('[data-action-id=sources] pre')).to_have_count(0)
        page.locator('[data-action-id=sources] summary').click()
        expect(page.locator('[data-action-id=sources] pre')).to_contain_text(source)
        page.get_by_role('button', name='执行动态', exact=True).click()
        expect(page.get_by_role('region', name='实时事件')).to_be_visible()
        page.get_by_role('button', name='处理 1 项请求').click()
        expect(count).to_have_value('4')
        expect(page.get_by_label('申请：generate_news')).to_be_focused()
        assert not scenario.decisions and not scenario.sent and not scenario.controls
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize('width,height', [(1440, 900), (390, 844)])
def test_goal_controls_and_round_filter(browser, tmp_path, width, height):
    scenario = AssistantScenario()
    scenario.snapshots[CID].update(state='idle', requests=[], round_started_at=20, actions=[
        dict(id='second', tool='generate_news', status='succeeded', created_at=22,
             result=dict(post_ids=['draft-two'], post=dict(id='draft-two', title='本轮测试稿'))),
        dict(id='first', tool='generate_news', status='succeeded', created_at=10,
             result=dict(post_ids=['draft-one'], post=dict(id='draft-one', title='第一轮测试稿'))),
    ])
    context, page, errors = open_assistant(browser, scenario, {'width': width, 'height': height})
    try:
        panel = page.get_by_role('region', name='会话目标')
        page.get_by_role('button', name='设定目标', exact=True).click()
        dialog = page.get_by_role('dialog', name='设定会话目标')
        dialog.get_by_label('目标', exact=True).fill('生成三条新闻，以平台草稿回执验收')
        dialog.get_by_label('验收条件').select_option('saved')
        dialog.get_by_label('篇数').fill('3')
        page.screenshot(path=str(tmp_path / f'goal-editor-{width}.png'), full_page=True)
        dialog.get_by_role('button', name='开始目标').click()
        expect(panel.get_by_text('平台草稿已确认 0/3')).to_be_visible()
        expect(page.locator('.dialogue-round')).to_contain_text('本轮 · 本地 1')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / f'goal-workspace-{width}.png'), full_page=True)
        panel.get_by_role('button', name='暂停目标').click()
        expect(panel.get_by_text('已暂停', exact=True)).to_be_visible()
        page.reload()
        expect(panel.get_by_text('已暂停', exact=True)).to_be_visible()
        panel.get_by_role('button', name='编辑目标').click()
        editor = page.get_by_role('dialog', name='编辑会话目标')
        editor.get_by_label('目标', exact=True).fill('此编辑来自旧版本')
        snapshot = scenario.snapshots[CID]
        snapshot['goal']['objective'] = '另一标签页修改了目标'
        snapshot.update(goal_version=3, revision=snapshot['revision'] + 1)
        editor.get_by_role('button', name='保存修改').click()
        expect(editor.get_by_role('alert')).to_contain_text('GOAL_CHANGED')
        editor.get_by_role('button', name='关闭目标设置').click()
        expect(panel.get_by_title('另一标签页修改了目标', exact=True)).to_be_visible()
        panel.get_by_role('button', name='编辑目标').click()
        expect(editor.get_by_label('目标', exact=True)).to_have_value('另一标签页修改了目标')
        editor.get_by_label('目标', exact=True).fill('编辑目标后继续三篇')
        editor.get_by_role('button', name='保存修改').click()
        expect(panel.get_by_title('编辑目标后继续三篇', exact=True)).to_be_visible()
        panel.get_by_role('button', name='恢复目标').click()
        expect(panel.get_by_text('进行中', exact=True)).to_be_visible()
        page.get_by_role('button', name='工具与产物', exact=True).click()
        page.get_by_label('仅本轮').check()
        expect(page.get_by_text('本轮测试稿', exact=True)).to_be_visible()
        expect(page.get_by_text('第一轮测试稿', exact=True)).to_have_count(0)
        page.get_by_label('仅本轮').uncheck()
        expect(page.get_by_text('第一轮测试稿', exact=True)).to_be_visible()
        panel.get_by_role('button', name='清除目标', exact=True).click()
        page.get_by_role('dialog', name='清除此目标？').get_by_role('button', name='清除目标').click()
        expect(panel.get_by_text('未设置', exact=True)).to_be_visible()
        assert [command['action'] for cid, command in scenario.goal_commands] == ['set', 'pause', 'edit', 'edit', 'resume', 'clear']
        assert all(cid == CID for cid, command in scenario.goal_commands)
        assert not scenario.sent and not scenario.decisions and not errors
    finally:
        context.close()


@pytest.mark.parametrize(('tool', 'reason', 'summary', 'button'), [
    ('controller', 'OUTPUT_INCOMPLETE: finish_reason=length', '主控模型没有返回完整答复', '已检查模型，继续执行'),
    ('mcp_call', "CAPABILITY_UNKNOWN: {'resource_id': 'mcp:builtin:controller.plan'}",
     '智能体请求了尚未登记的能力', '已配置，重新检查'),
])
def test_blocked_request_explains_next_step_without_rerunning(browser, tool, reason, summary, button):
    scenario = AssistantScenario()
    scenario.snapshots[CID].update(state='waiting_input', requests=[dict(id='blocked', kind='error', tool=tool,
        arguments={}, reason=reason, version=1, allowed_decisions=['configured', 'answer'])])
    context, page, errors = open_assistant(browser, scenario, {'width': 1366, 'height': 900})
    try:
        card = page.get_by_label(f'申请：{tool}')
        expect(card.get_by_text(summary, exact=False)).to_be_visible()
        expect(card.get_by_role('button', name=button)).to_be_enabled()
        if tool == 'mcp_call':
            expect(card.get_by_text('mcp:builtin:controller.plan', exact=True)).to_be_visible()
        expect(card.get_by_text(reason, exact=True)).to_have_count(0)
        card.get_by_text('技术原因', exact=True).click()
        expect(card.get_by_text(reason, exact=True)).to_be_visible()
        assert not scenario.decisions and not scenario.sent and not errors
    finally:
        context.close()
