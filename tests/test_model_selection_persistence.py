"""Model selection round trips without credentials, external calls or workers."""

from copy import deepcopy
import json
from pathlib import Path
import socket
import subprocess
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from starlette.routing import Mount

from apps import gui
from apps.web_service import Workbench
from backend import app as module
from src.model_platforms import PlatformStore, RuntimeClient
from test_task_calibration import Conversations


@pytest.fixture
def selection_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gui, 'build_subprocess_env', lambda overrides: {})
    conversations = Conversations()
    current = Workbench(tmp_path, conversation_store=conversations)
    current._capability_manager = SimpleNamespace(
        store=SimpleNamespace(resources=lambda: []),
        plan_capabilities=lambda cid, pid: {
            'version': 1, 'skill_mode': 'off', 'skill_names': [], 'disabled_tools': [],
            'tools': [], 'skills': [], 'memory': [], 'readiness': {'ready': True},
        },
    )
    store = current.model_platforms()
    connection = store.add_connection({
        'name': 'Local test', 'adapter': 'openai_chat',
        'base_url': 'http://127.0.0.1:11434/v1', 'network': 'local', 'auth_mode': 'none',
    })
    store.authorize(connection['connection_id'], {
        'roles': ['agent', 'writer'], 'risk_accepted': True,
        'max_requests': 20, 'expires_at': time.time() + 3600,
    }, store.state()['revision'])
    refs = []
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}],
    }))
    for name in ('first', 'second'):
        model = store.add_model({
            'connection_id': connection['connection_id'], 'upstream_model_id': name, 'enabled': True,
        }, store.state()['revision'])
        for purpose in ('structured', 'text'):
            store.check(model['model_ref'], purpose, RuntimeClient(store, transport=transport))
        refs.append(model['model_ref'])
    monkeypatch.setattr(module.app.state, 'service', current)
    monkeypatch.setattr(module, 'ensure_review_schema', lambda: None)
    monkeypatch.setattr(module.KnowledgeStore, 'from_env', lambda: SimpleNamespace(status=lambda: {'status': 'ready'}))
    monkeypatch.setattr(Workbench, '_run', lambda *args: pytest.fail('selection started a worker'))
    return current, refs, conversations


def test_saved_defaults_survive_restart_without_changing_frozen_roles(selection_store, monkeypatch):
    current, (first, second), conversations = selection_store
    with TestClient(module.app, base_url='http://127.0.0.1:8786') as client:
        client.post('/api/session', headers={'X-Workbench': '1'})
        route = '/api/model-platforms/roles'
        revision = client.get(route).json()['revision']
        saved = client.put(route, headers={'X-Workbench': '1'}, json={
            'expected_revision': revision, 'agent': first, 'writer': second,
        })
        assert saved.status_code == 200, saved.text
        frozen = current.freeze_model_roles({}, {'kind': 'agent'})
        current.jobs['frozen-test'] = {'model_snapshots': json.loads(frozen['RUN_MODEL_SNAPSHOTS'])}
        before = deepcopy(current.jobs)
        saved = client.put(route, headers={'X-Workbench': '1'}, json={
            'expected_revision': saved.json()['revision'], 'agent': second,
        })
        assert saved.status_code == 200, saved.text
        assert current.jobs == before
        resumed = current.freeze_model_roles({}, {'kind': 'agent', 'resume_of': 'frozen-test'})
        assert resumed['RUN_MODEL_SNAPSHOTS'] == frozen['RUN_MODEL_SNAPSHOTS']
        reopened = Workbench(current.root, conversation_store=conversations)
        monkeypatch.setattr(module.app.state, 'service', reopened)
        assert client.get(route).json()['roles'] == {'agent': second, 'writer': second, 'image': ''}
        assert PlatformStore(reopened.model_platforms().directory, namespace='workflow').state()['roles'] == {}


@pytest.fixture(scope='module')
def selection_frontend(tmp_path_factory):
    frontend = Path(__file__).resolve().parents[1] / 'frontend'
    output = tmp_path_factory.mktemp('selection-frontend')
    subprocess.run(['node', str(frontend / 'node_modules/vite/bin/vite.js'), 'build',
                    '--outDir', str(output)], cwd=frontend, check=True, capture_output=True, text=True)
    return output


@pytest.fixture
def selection_page(selection_store, selection_frontend, monkeypatch):
    monkeypatch.setattr(module.app.router, 'routes', [route for route in module.app.routes if not isinstance(route, Mount)] + [
        Mount('/', app=StaticFiles(directory=selection_frontend, html=True)),
    ])
    sock = socket.socket()
    for candidate in range(52000, 65000):
        try:
            sock.bind(('127.0.0.1', candidate))
            break
        except OSError:
            continue
    else:
        sock.close()
        raise RuntimeError('no browser-safe test port available')
    port = sock.getsockname()[1]
    monkeypatch.setenv('REDBOOK_AGENT_PORT', str(port))
    server = uvicorn.Server(uvicorn.Config(module.app, log_level='error', lifespan='off'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(.01)
        assert server.started
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{port}')
                page.get_by_role('button', name='连接与模型', exact=True).click()
                page.get_by_role('tab', name='默认角色', exact=True).click()
                expect(page.get_by_role('button', name='智能体主控模型', exact=True)).to_be_enabled()
                yield page
                assert errors == []
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()


def test_default_choice_persists_on_selection_and_reopen(selection_page, selection_store, monkeypatch, tmp_path):
    page = selection_page
    current, (first, second), conversations = selection_store
    for label, name in (('智能体主控模型', 'first'), ('写稿模型', 'second')):
        page.get_by_role('button', name=label, exact=True).click()
        page.get_by_role('option', name=f'Local test · {name}', exact=True).click()
        expect(page.get_by_text('默认角色已保存', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name=label, exact=True)).to_contain_text(name)
    assert current.providers()['bindings'] == {'agent': first, 'writer': second, 'image': ''}
    page.get_by_role('button', name='对话任务', exact=True).click()
    page.get_by_role('button', name='连接与模型', exact=True).click()
    page.get_by_role('tab', name='默认角色', exact=True).click()
    expect(page.get_by_role('button', name='智能体主控模型', exact=True)).to_contain_text('first')
    monkeypatch.setattr(module.app.state, 'service', Workbench(current.root, conversation_store=conversations))
    page.reload()
    page.get_by_role('button', name='连接与模型', exact=True).click()
    page.get_by_role('tab', name='默认角色', exact=True).click()
    expect(page.get_by_role('button', name='智能体主控模型', exact=True)).to_contain_text('first')
    expect(page.get_by_role('button', name='写稿模型', exact=True)).to_contain_text('second')
    page.screenshot(path=str(tmp_path / 'defaults-desktop.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.wait_for_function("() => document.querySelector('.sidebar').getBoundingClientRect().right <= 0")
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    page.screenshot(path=str(tmp_path / 'defaults-mobile.png'), full_page=True)


def test_failed_default_save_keeps_persisted_selection_and_can_retry(selection_page, selection_store):
    page = selection_page
    current, (first, _), _ = selection_store
    route = '**/api/model-platforms/roles'
    page.route(route, lambda request: request.fulfill(status=409, json={'error': 'REVISION_CONFLICT'})
               if request.request.method == 'PUT' else request.continue_())
    page.get_by_role('button', name='智能体主控模型', exact=True).click()
    page.get_by_role('option', name='Local test · first', exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('REVISION_CONFLICT')
    expect(page.get_by_role('button', name='智能体主控模型', exact=True)).to_contain_text('继承原有配置')
    assert current.providers()['bindings']['agent'] == ''
    page.unroute(route)
    page.get_by_role('button', name='智能体主控模型', exact=True).click()
    page.get_by_role('option', name='Local test · first', exact=True).click()
    expect(page.get_by_text('默认角色已保存', exact=True)).to_be_visible()
    assert current.providers()['bindings']['agent'] == first


@pytest.mark.parametrize('response', ['pending', 'failed'])
def test_default_roles_do_not_look_reset_while_refresh_is_pending_or_failed(selection_page, selection_store, response):
    page = selection_page
    current, (first, second), _ = selection_store
    store = current.model_platforms()
    store.bind({'agent': first, 'writer': second}, store.state()['revision'])
    pending = []
    route = '**/api/model-platforms/roles'
    def intercept(request):
        if response == 'pending':
            pending.append(request)
        else:
            request.fulfill(status=503, json={'error': '默认角色读取暂时失败'})
    page.route(route, intercept)
    page.reload()
    page.get_by_role('button', name='连接与模型', exact=True).click()
    page.get_by_role('tab', name='默认角色', exact=True).click()
    agent = page.get_by_role('button', name='智能体主控模型', exact=True)
    writer = page.get_by_role('button', name='写稿模型', exact=True)
    expect(agent).to_contain_text('Local test · first')
    expect(writer).to_contain_text('Local test · second')
    expect(agent).to_be_disabled()
    assert current.providers()['bindings']['agent'] == first
    if response == 'pending':
        assert pending
        for request in pending:
            request.continue_()
    else:
        expect(page.get_by_role('alert')).to_contain_text('默认角色读取暂时失败')
        page.unroute(route)
        page.get_by_role('button', name='重新读取默认角色', exact=True).click()
    expect(agent).to_be_enabled()
    expect(agent).to_contain_text('Local test · first')
    expect(writer).to_contain_text('Local test · second')
    assert current.jobs == {}


def test_pending_default_save_blocks_other_choices_and_clear_is_persisted(selection_page, selection_store):
    page = selection_page
    current, (first, _), _ = selection_store
    pending = []
    route = '**/api/model-platforms/roles'
    page.route(route, lambda request: pending.append(request)
               if request.request.method == 'PUT' else request.continue_())
    page.get_by_role('button', name='智能体主控模型', exact=True).click()
    with page.expect_request(lambda request: request.method == 'PUT' and request.url.endswith('/roles')):
        page.get_by_role('option', name='Local test · first', exact=True).click()
    for label in ('智能体主控模型', '写稿模型', '生图模型'):
        expect(page.get_by_role('button', name=label, exact=True)).to_be_disabled()
    assert len(pending) == 1
    assert set(pending[0].request.post_data_json) == {'agent', 'expected_revision'}
    assert current.providers()['bindings']['agent'] == ''
    pending[0].continue_()
    expect(page.get_by_text('默认角色已保存', exact=True)).to_be_visible()
    assert current.providers()['bindings']['agent'] == first
    page.unroute(route)
    page.get_by_role('button', name='智能体主控模型', exact=True).click()
    page.get_by_role('option', name='继承原有配置', exact=True).click()
    expect(page.get_by_text('默认角色已保存', exact=True)).to_be_visible()
    assert current.providers()['bindings']['agent'] == ''
    page.reload()
    page.get_by_role('button', name='连接与模型', exact=True).click()
    page.get_by_role('tab', name='默认角色', exact=True).click()
    expect(page.get_by_role('button', name='智能体主控模型', exact=True)).to_contain_text('继承原有配置')


def test_task_override_requires_apply_and_never_changes_defaults(selection_page, selection_store):
    page = selection_page
    current, (first, second), conversations = selection_store
    page.get_by_role('button', name='智能体主控模型', exact=True).click()
    page.get_by_role('option', name='Local test · first', exact=True).click()
    expect(page.get_by_text('默认角色已保存', exact=True)).to_be_visible()
    page.get_by_role('button', name='对话任务', exact=True).click()
    page.locator('#prompt').fill('生成1条每日新闻，不上传')
    page.get_by_role('button', name='发送', exact=True).click()
    page.get_by_text('本次模型', exact=True).click()
    expect(page.get_by_role('button', name='本次主控模型', exact=True)).to_contain_text('first')
    page.get_by_role('button', name='本次主控模型', exact=True).click()
    page.get_by_role('option', name='Local test · second', exact=True).click()
    page.reload()
    page.get_by_text('本次模型', exact=True).click()
    expect(page.get_by_role('button', name='本次主控模型', exact=True)).to_contain_text('first')
    page.get_by_role('button', name='本次主控模型', exact=True).click()
    page.get_by_role('option', name='Local test · second', exact=True).click()
    page.get_by_role('button', name='应用于本次计划', exact=True).click()
    expect(page.get_by_role('button', name='应用于本次计划', exact=True)).to_be_disabled()
    page.reload()
    page.get_by_text('本次模型', exact=True).click()
    expect(page.get_by_role('button', name='本次主控模型', exact=True)).to_contain_text('second')
    assert current.providers()['bindings']['agent'] == first
    saved = conversations.list()[0]
    assert saved['plans'][-1]['model_roles']['agent'] == second
    assert saved['runs'] == []
    assert current.jobs == {}


def test_task_picker_can_save_defaults_for_new_tasks_after_restart(selection_page, selection_store, monkeypatch, tmp_path):
    page = selection_page
    current, (first, second), conversations = selection_store
    page.get_by_role('button', name='对话任务', exact=True).click()
    page.locator('#prompt').fill('生成1条每日新闻，不上传')
    page.get_by_role('button', name='发送', exact=True).click()
    page.get_by_text('本次模型', exact=True).click()
    for role, model in [('本次主控模型', 'first'), ('本次写稿模型', 'second')]:
        page.get_by_role('button', name=role, exact=True).click()
        page.get_by_role('option', name=f'Local test · {model}', exact=True).click()
    original = deepcopy(conversations.list()[0]['plans'][-1])
    save = page.get_by_role('button', name='保存为默认模型', exact=True)
    expect(save).to_be_enabled()
    save.click()
    expect(page.get_by_role('status').filter(has_text='默认模型已保存')).to_be_visible()
    page.screenshot(path=str(tmp_path / 'task-defaults-desktop.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    expect(page.locator('.sidebar')).to_have_css('transform', 'matrix(1, 0, 0, 1, -224, 0)')
    save.scroll_into_view_if_needed()
    expect(save).to_be_in_viewport()
    page.screenshot(path=str(tmp_path / 'task-defaults-mobile.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.set_viewport_size({'width': 1440, 'height': 1000})
    assert current.providers()['bindings'] == {'agent': first, 'writer': second, 'image': ''}
    assert conversations.list()[0]['plans'][-1] == original
    reopened = Workbench(current.root, conversation_store=conversations)
    reopened._capability_manager = current._capability_manager
    monkeypatch.setattr(module.app.state, 'service', reopened)
    page.reload()
    page.get_by_role('button', name='新对话', exact=True).click()
    page.locator('#prompt').fill('生成1条每日新闻，不上传')
    page.get_by_role('button', name='发送', exact=True).click()
    page.get_by_text('本次模型', exact=True).click()
    expect(page.get_by_role('button', name='本次主控模型', exact=True)).to_contain_text('Local test · first')
    expect(page.get_by_role('button', name='本次写稿模型', exact=True)).to_contain_text('Local test · second')
    assert current.jobs == {}


def test_task_default_save_failure_preserves_existing_defaults(selection_page, selection_store):
    page = selection_page
    current, (first, _), _ = selection_store
    page.get_by_role('button', name='对话任务', exact=True).click()
    page.locator('#prompt').fill('生成1条每日新闻，不上传')
    page.get_by_role('button', name='发送', exact=True).click()
    page.get_by_text('本次模型', exact=True).click()
    page.get_by_role('button', name='本次主控模型', exact=True).click()
    page.get_by_role('option', name='Local test · first', exact=True).click()
    page.route('**/api/model-platforms/roles', lambda route: route.fulfill(
        status=409, content_type='application/json', body='{"error":"REVISION_CONFLICT"}',
    ) if route.request.method == 'PUT' else route.continue_())
    page.get_by_role('button', name='保存为默认模型', exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('REVISION_CONFLICT')
    assert not current.providers()['bindings']['agent']
    expect(page.get_by_role('button', name='本次主控模型', exact=True)).to_contain_text('Local test · first')
    page.unroute('**/api/model-platforms/roles')
    page.get_by_role('button', name='保存为默认模型', exact=True).click()
    expect(page.get_by_role('status').filter(has_text='默认模型已保存')).to_be_visible()
    assert current.providers()['bindings']['agent'] == first


def test_builtin_defaults_restore_in_browser_and_new_plan(selection_page, selection_store, monkeypatch):
    page = selection_page
    current, _, conversations = selection_store
    config = {
        'MINIMAX_TOKEN_PLAN_API_KEY': 'synthetic-subscription-key',
        'MINIMAX_LLM_MODEL': 'MiniMax-M3', 'MINIMAX_USE_SUBSCRIPTION': '1',
        'MINIMAX_BILLING_MODE': 'subscription_only',
        'MINIMAX_ALLOW_PAID_CREDITS': '0', 'MINIMAX_ALLOW_PAYGO': '0',
    }
    monkeypatch.setattr(gui, 'load_env_file', lambda path: dict(config))
    monkeypatch.setattr(gui, 'build_subprocess_env', lambda overrides: {**config, **overrides})
    from src.images import opencodex_images
    monkeypatch.setattr(opencodex_images, 'image_connection_status', lambda env: {'enabled': True})
    page.reload()
    page.get_by_role('button', name='连接与模型', exact=True).click()
    page.get_by_role('tab', name='默认角色', exact=True).click()
    for label, choice in [
        ('智能体主控模型', 'MiniMax · MiniMax-M3'),
        ('写稿模型', 'MiniMax · MiniMax-M3'),
        ('生图模型', 'OpenCodex / ChatGPT订阅 · gpt-image-2'),
    ]:
        page.get_by_role('button', name=label, exact=True).click()
        page.get_by_role('option', name=choice, exact=True).click()
        expect(page.get_by_role('button', name=label, exact=True)).to_contain_text(choice)
        expect(page.get_by_role('button', name=label, exact=True)).to_be_enabled()
    expected = {'agent': 'minimax:MiniMax-M3', 'writer': 'minimax:MiniMax-M3', 'image': 'opencodex:gpt-image-2'}
    assert current.providers()['bindings'] == expected
    reopened = Workbench(current.root, conversation_store=conversations)
    monkeypatch.setattr(module.app.state, 'service', reopened)
    page.reload()
    page.get_by_role('button', name='连接与模型', exact=True).click()
    page.get_by_role('tab', name='默认角色', exact=True).click()
    expect(page.get_by_role('button', name='智能体主控模型', exact=True)).to_contain_text('MiniMax-M3')
    expect(page.get_by_role('button', name='写稿模型', exact=True)).to_contain_text('MiniMax-M3')
    expect(page.get_by_role('button', name='生图模型', exact=True)).to_contain_text('gpt-image-2')
    parsed = reopened.append_agent_message(reopened.create_agent_conversation()['id'], '生成1条每日新闻')['plan']
    assert parsed['model_roles'] == expected
    assert json.loads(reopened.freeze_model_roles({}, {'kind': 'agent'})['RUN_LEGACY_MODEL_ROLES']) == expected
    assert reopened.jobs == {}


@pytest.mark.parametrize('instruction', ['图片门槛只做参考', '不需要对图片进行评分硬门槛'])
def test_chat_api_parses_advisory_policy_without_model_calibration(selection_store, monkeypatch, instruction):
    current, _, _ = selection_store
    monkeypatch.setattr(module, 'call_model', lambda *a, **kw: pytest.fail('local parsing called a model'))
    with TestClient(module.app, base_url='http://127.0.0.1:8786') as client:
        client.post('/api/session', headers={'X-Workbench': '1'})
        cid = client.post('/api/conversations', headers={'X-Workbench': '1'}, json={'title': 'policy test'}).json()['id']
        result = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'}, json={
            'content': '生成10条每日新闻，不上传，' + instruction,
        })
        assert result.status_code == 200, result.text
        plan = client.get(f'/api/conversations/{cid}').json()['plans'][-1]
        assert plan['image_score_required'] is False
        assert plan['recognition_source'] == 'rules'
        assert plan['jobs'][0]['count'] == 10
        assert current.jobs == {}


@pytest.mark.parametrize('origin', ['task', 'global'])
def test_saving_task_defaults_refreshes_confirmation_fingerprint(selection_page, selection_store, origin):
    from backend.plan_service import PlanService
    page = selection_page
    current, _, _ = selection_store
    page.get_by_role('button', name='对话任务', exact=True).click()
    page.locator('#prompt').fill('生成1条每日新闻，不上传')
    page.get_by_role('button', name='发送', exact=True).click()
    if origin == 'global':
        page.get_by_role('button', name='连接与模型', exact=True).click()
        page.get_by_role('tab', name='默认角色', exact=True).click()
        page.get_by_role('button', name='智能体主控模型', exact=True).click()
    else:
        page.get_by_text('本次模型', exact=True).click()
        page.get_by_role('button', name='本次主控模型', exact=True).click()
    page.get_by_role('option', name='Local test · first', exact=True).click()
    if origin == 'global':
        expect(page.get_by_text('默认角色已保存', exact=True)).to_be_visible()
        page.get_by_role('button', name='对话任务', exact=True).click()
    else:
        page.get_by_role('button', name='保存为默认模型', exact=True).click()
        expect(page.get_by_role('status').filter(has_text='默认模型已保存')).to_be_visible()
    expected = PlanService(current).configuration_fingerprint()
    requests = []
    def capture(route):
        requests.append(route.request.post_data_json)
        route.fulfill(status=503, content_type='application/json', body='{"error":"offline-only"}')
    page.route('**/api/plans/*/confirm', capture)
    page.get_by_role('button', name='确认并执行', exact=True).click()
    expect(page.get_by_text('offline-only', exact=True)).to_be_visible()
    assert requests[0]['configuration_fingerprint'] == expected
    assert current.jobs == {}
