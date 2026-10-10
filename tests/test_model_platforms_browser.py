import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn
from playwright.sync_api import expect, sync_playwright

from apps.web_service import Workbench
from backend import app as module
from test_task_calibration import Conversations


def test_agent_model_management_and_per_plan_selection_with_real_backend(tmp_path, monkeypatch):
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body['model'] == 'org/claude:preview'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'content': [{'type': 'text', 'text': '{"ok":true}'}], 'stop_reason': 'end_turn'}).encode())
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    current = Workbench(tmp_path, conversation_store=Conversations())
    monkeypatch.setattr(module.app.state, 'service', current)
    monkeypatch.setattr(module, 'ensure_review_schema', lambda: None)
    monkeypatch.setattr(module.KnowledgeStore, 'status', lambda self: {'status':'ready'})
    sock = socket.socket()
    for candidate in range(49152, 65000):
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
    for _ in range(100):
        if server.started:
            break
        time.sleep(.01)
    artifacts = Path('data/tmp/model-platforms-browser')
    artifacts.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{port}')
            page.get_by_role('button', name='连接与模型', exact=True).click()
            page.get_by_role('button', name='添加供应商', exact=True).click()
            page.get_by_label('连接模板', exact=True).select_option('ollama')
            page.get_by_label('供应商名称', exact=True).fill('本地 Claude 协议测试')
            page.get_by_label('接口协议', exact=True).select_option('anthropic_messages')
            page.get_by_label('API 基址', exact=True).fill(f'http://127.0.0.1:{upstream.server_port}/v1')
            page.get_by_role('button', name='保存连接', exact=True).click()
            expect(page.get_by_role('dialog')).to_have_count(0)
            page.get_by_role('button', name='添加模型', exact=True).click()
            page.get_by_label('原生模型 ID', exact=True).fill('org/claude:preview')
            page.get_by_role('button', name='保存模型', exact=True).click()
            page.get_by_role('button', name='授权此连接', exact=True).click()
            page.get_by_label('确认承担此连接的费用风险', exact=True).check()
            page.get_by_role('button', name='保存授权', exact=True).click()
            page.get_by_role('tab', name='模型目录', exact=True).click()
            page.get_by_role('button', name='测试结构化', exact=True).click()
            expect(page.get_by_text('验证通过', exact=True)).to_be_visible(timeout=15000)
            page.get_by_role('button', name='对话任务', exact=True).click()
            page.locator('#prompt').fill('生成1条每日新闻，不上传')
            page.get_by_role('button', name='发送', exact=True).click()
            page.get_by_text('本次模型', exact=True).click()
            page.get_by_role('button', name='本次主控模型', exact=True).click()
            page.get_by_role('option', name='本地 Claude 协议测试 · org/claude:preview', exact=True).click()
            page.get_by_role('button', name='应用于本次计划', exact=True).click()
            expect(page.get_by_role('button', name='应用于本次计划', exact=True)).to_be_disabled()
            page.reload()
            page.get_by_text('本次模型', exact=True).click()
            expect(page.get_by_role('button', name='本次主控模型', exact=True)).to_contain_text('org/claude:preview')
            assert current.providers()['bindings']['agent'] == ''
            assert current.jobs == {}
            page.screenshot(path=str(artifacts / 'agent-desktop.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            page.wait_for_function("() => document.querySelector('.sidebar').getBoundingClientRect().right <= 0")
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.screenshot(path=str(artifacts / 'agent-mobile.png'), full_page=True)
            assert errors == []
            browser.close()
    finally:
        server.should_exit = True
        thread.join(5)
        upstream.shutdown()
        upstream.server_close()
        sock.close()
