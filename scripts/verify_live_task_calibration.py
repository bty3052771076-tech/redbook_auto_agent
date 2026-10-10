"""Opt-in, one-request calibration/edit acceptance; never confirm a content task."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from uuid import uuid4


REQUEST = (
    '生成10条今天的每日新闻，速度优先，保存到小红书创作者中心草稿箱，不公开发布。'
    '每日新闻优先关注知名平台禁令与解禁、隐私与年龄核验、账号规则变化、游戏订阅退款、'
    '消费者权益争议、真实且有意外转折的社会事件。'
    '约3条作为软偏好，不设硬配额，其余兼顾国际冲突、科技产业、社会民生、财经产业。'
)


def verify(runtime: Path, resume: Path | None = None) -> dict:
    application = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(application))
    sys.path.insert(0, str(application / 'tools/redbook_tools'))
    from apps import gui

    runtime = runtime.resolve()
    if runtime.drive.upper() != 'E:' or not runtime.is_dir():
        raise ValueError('RUNTIME_INVALID: use an existing runtime directory on E:')
    local = gui.load_env_file(runtime / '.env.gui')
    env = gui.build_subprocess_env(local)
    if not env.get('MINIMAX_TOKEN_PLAN_API_KEY'):
        raise ValueError('CREDENTIAL_UNAVAILABLE: configure the existing MiniMax subscription key locally')
    if env.get('MINIMAX_BILLING_MODE', 'subscription_only') not in {'subscription_only', 'subscription'}:
        raise ValueError('BILLING_NOT_AUTHORIZED: this diagnostic only accepts MiniMax subscription')
    if any(gui.env_flag_enabled(env.get(key)) for key in
           ('MINIMAX_ALLOW_PAYGO', 'MINIMAX_ALLOW_PAID_CREDITS', 'ALLOW_PAID_LLM_FALLBACK')):
        raise ValueError('BILLING_NOT_AUTHORIZED: disable paid API fallback before this diagnostic')

    previous = None
    if resume:
        output = resume.resolve()
        if output.parent != (runtime / 'data/tmp').resolve() or not output.name.startswith('live-task-calibration-'):
            raise ValueError('DIAGNOSTIC_PATH_INVALID: resume only an owned live calibration directory')
        previous = json.loads((output / 'result.json').read_text(encoding='utf-8'))
        identity = output.name.removeprefix('live-task-calibration-')
        if previous.get('namespace') != 'controls_live_' + identity or len(previous.get('model_calls', [])) != 1:
            raise ValueError('DIAGNOSTIC_IDENTITY_INVALID: resume requires exactly one recorded model call')
    else:
        identity = uuid4().hex
        output = runtime / 'data/tmp' / ('live-task-calibration-' + identity)
        output.mkdir(parents=True)
    original_load_env = gui.load_env_file

    def diagnostic_env(path):
        if Path(path).resolve() == (output / '.env.gui').resolve():
            return dict(local)
        return original_load_env(path)

    gui.load_env_file = diagnostic_env
    os.environ.update(env)
    os.environ.update(REDBOOK_RUNTIME_ROOT=str(runtime),
                      MODEL_PLATFORMS_DIR=str(output / 'model-platforms'),
                      MODEL_PLATFORMS_NAMESPACE='agent',
                      XHS_CHROME_USER_DATA_DIR=str(output / 'data/browser/chrome-profile'),
                      TOUTIAO_CHROME_USER_DATA_DIR=str(output / 'data/browser/chrome-profile'),
                      TEMP=str(output), TMP=str(output))
    for key in ('RUN_MODEL_SNAPSHOTS', 'RUN_LEGACY_MODEL_ROLES', 'RUN_LEGACY_MODEL_CONFIGS',
                'CONTROLLER_MODEL_REF', 'WRITER_MODEL_REF', 'IMAGE_MODEL_REF'):
        os.environ.pop(key, None)
    from backend.settings import configure_runtime
    configure_runtime()
    from backend import app as module
    from backend.task_recognition import call_model
    from apps.web_service import Workbench
    from src.agent.conversation_store import PostgresConversationStore
    import uvicorn
    from playwright.sync_api import expect, sync_playwright

    store = PostgresConversationStore(namespace='controls_live_' + identity)
    current = Workbench(output, conversation_store=store)
    calls = list(previous['model_calls']) if previous else []

    def once(config, payload):
        if calls or config.provider != 'minimax' or config.platform_snapshot:
            raise ValueError('MODEL_CALL_BLOCKED: only one legacy MiniMax subscription request is allowed')
        if config.cost_class != 'subscription_included':
            raise ValueError('BILLING_NOT_AUTHORIZED: model is not marked subscription_included')
        calls.append({'provider': config.provider, 'model': config.model,
                      'base_url': config.base_url, 'cost_class': config.cost_class})
        return call_model(config, payload)

    def no_content_task(*args, **kwargs):
        raise RuntimeError('DIAGNOSTIC_WRITE_DENIED: content workers and platform writes are forbidden')

    current.submit = no_content_task
    current._run = no_content_task
    previous_service = module.app.state.service
    previous_recognition_call = module.recognition_call
    module.app.state.service = current
    module.recognition_call = once
    sock = socket.socket()
    for port in range(49152, 65000):
        try:
            sock.bind(('127.0.0.1', port))
            break
        except OSError:
            continue
    else:
        sock.close()
        raise RuntimeError('PORT_UNAVAILABLE: no browser-safe owned port available')
    os.environ['REDBOOK_AGENT_PORT'] = str(port)
    server = uvicorn.Server(uvicorn.Config(module.app, log_level='error', lifespan='off'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    result = {'namespace': store.namespace, 'output_directory': str(output), 'status': 'failed',
              'stage': 'startup', 'resumed_existing_candidate': bool(previous), 'new_model_calls': 0}
    started = time.monotonic()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError('SERVER_UNAVAILABLE: diagnostic server did not start')
            time.sleep(.05)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page_errors = []
                page.on('pageerror', lambda error: page_errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{port}')
                result['stage'] = 'calibration'
                if previous:
                    rows = store.list()
                    assert len(rows) == 1, 'resume requires one isolated diagnostic conversation'
                    expect(page.locator('#conversation-select option')).to_have_count(2)
                    page.locator('#conversation-select').select_option(rows[0]['id'])
                else:
                    page.locator('#prompt').fill(REQUEST)
                    page.get_by_role('button', name='发送', exact=True).click()
                    expect(page.get_by_role('button', name='大模型校准', exact=True)).to_be_enabled()
                    model_started = time.monotonic()
                    page.get_by_role('button', name='大模型校准', exact=True).click()
                candidate = page.get_by_role('button', name='编辑候选', exact=True)
                expect(candidate).to_be_visible(timeout=75000)
                result['calibration_wall_seconds'] = previous.get('calibration_wall_seconds') if previous else round(time.monotonic() - model_started, 3)
                result['new_model_calls'] = 0 if previous else len(calls)
                page.screenshot(path=str(output / '01-calibration.png'), full_page=False)
                result['stage'] = 'editing'
                candidate.click()
                panel = page.get_by_role('dialog', name='编辑校准候选')
                panel.get_by_label('每日新闻篇数', exact=True).fill('5')
                names = panel.locator('.pe-tags button').evaluate_all(
                    "nodes => nodes.map(node => node.getAttribute('aria-label') || '')")
                for name in names:
                    if '隐私' in name or '年龄核验' in name:
                        panel.get_by_role('button', name=name, exact=True).first.click()
                field = panel.get_by_label('每日新闻选题偏向', exact=True)
                field.fill('游戏退款')
                field.press('Enter')
                panel.get_by_label('每日新闻补充要求', exact=True).fill('')
                panel.get_by_label('评价视角', exact=True).fill('从消费者权益角度简洁评价')
                panel.get_by_label('速度优先', exact=True).check()
                panel.get_by_label('仅生成本地稿', exact=True).check()
                result['stage'] = 'saving'
                save_started = time.monotonic()
                panel.get_by_role('button', name='保存并采用', exact=True).click()
                expect(panel).not_to_be_visible(timeout=20000)
                result['stage'] = 'reload'
                expect(page.locator('.plan-pane')).to_contain_text('5 条')
                page.reload()
                expect(page.locator('.plan-pane')).to_contain_text('游戏退款')
                result['save_and_reload_wall_seconds'] = round(time.monotonic() - save_started, 3)
                page.screenshot(path=str(output / '02-edited-reloaded.png'), full_page=False)
                assert not page_errors, 'browser emitted a JavaScript error'
            except Exception:
                page.screenshot(path=str(output / 'failure.png'), full_page=False)
                raise
            finally:
                browser.close()
        result['stage'] = 'postgres_readback'
        rows = store.list()
        assert len(rows) == 1
        saved = store.get(rows[0]['id'])
        plan = saved['plans'][-1]
        assert len(plan['jobs']) == 1 and plan['jobs'][0]['kind'] == 'daily_news'
        job = plan['jobs'][0]
        assert job['count'] == 5 and '游戏退款' in job['topic_preferences']
        assert not any('隐私' in term or '年龄核验' in term
                       for term in job['topic_preferences'] + job['search_keywords'])
        assert plan['delivery'] == 'generate_only'
        assert saved['runs'] == [] and current.jobs == {}
        assert len(calls) == 1 and len(saved['task_recognitions']) == 1
        record = saved['task_recognitions'][0]
        assert record['status'] == 'adopted'
        result.update(status='passed', stage='complete', conversation_id=saved['id'], plan_id=plan['id'],
                      plan_version=plan['version'], recognition_id=record['id'],
                      recognition_status=record['status'], model_calls=calls,
                      tool_count=job['count'], topic_preferences=job['topic_preferences'],
                      search_keywords=job['search_keywords'], delivery=plan['delivery'],
                      content_runs=0, platform_writes=0)
    except Exception as error:
        import traceback
        result['failure_locations'] = [{'file': str(Path(frame.filename).relative_to(application)),
                                        'line': frame.lineno, 'function': frame.name}
                                       for frame in traceback.extract_tb(error.__traceback__)
                                       if Path(frame.filename).is_relative_to(application)]
        result.update(error_type=type(error).__name__, model_calls=calls)
        raise
    finally:
        gui.load_env_file = original_load_env
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        module.app.state.service = previous_service
        module.recognition_call = previous_recognition_call
        result['wall_seconds'] = round(time.monotonic() - started, 3)
        (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, default=Path('E:/AI/codex/redbook_runtime'))
    parser.add_argument('--confirm-single-model-call', action='store_true')
    parser.add_argument('--resume-diagnostic', type=Path, help='Reuse an owned saved candidate without another model call')
    args = parser.parse_args()
    if not args.confirm_single_model_call and not args.resume_diagnostic:
        parser.error('explicit --confirm-single-model-call is required; this uses subscription quota')
    try:
        verify(args.runtime_root, args.resume_diagnostic)
    except Exception as error:
        print('diagnostic_failed=' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
