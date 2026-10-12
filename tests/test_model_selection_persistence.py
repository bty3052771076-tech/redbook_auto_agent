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
