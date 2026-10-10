"""Opt-in capability acceptance: one new draft, then one readonly readback."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from urllib.parse import quote
from uuid import uuid4


def verify(runtime: Path, query: str) -> dict:
    application = Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(application))
    sys.path.insert(0,str(application/'tools/redbook_tools'))
    runtime = runtime.resolve()
    if runtime.drive.upper() != 'E:' or not runtime.is_dir():
        raise ValueError('RUNTIME_INVALID: use an existing runtime directory on E:')
    from apps import gui
    local = gui.load_env_file(runtime/'.env.gui')
    env = gui.build_subprocess_env(local)
    if not env.get('MINIMAX_TOKEN_PLAN_API_KEY'):
        raise ValueError('CREDENTIAL_UNAVAILABLE: configure the existing subscription key locally')
    if env.get('MINIMAX_BILLING_MODE','subscription_only') not in {'subscription_only','subscription'} or any(
        gui.env_flag_enabled(env.get(key)) for key in ('MINIMAX_ALLOW_PAYGO','MINIMAX_ALLOW_PAID_CREDITS','ALLOW_PAID_LLM_FALLBACK')):
        raise ValueError('BILLING_NOT_AUTHORIZED: paid fallback is forbidden in this diagnostic')
    identity = uuid4().hex
    output = runtime/'data/tmp'/('live-capability-trial-'+identity)
    output.mkdir(parents=True)
    os.environ.update(env)
    os.environ.update(REDBOOK_RUNTIME_ROOT=str(runtime),TEMP=str(output),TMP=str(output),
                      AGENT_CAPABILITY_NAMESPACE='controls_trial_live_'+identity)
    for key in ('RUN_MODEL_SNAPSHOTS','RUN_LEGACY_MODEL_ROLES','RUN_LEGACY_MODEL_CONFIGS',
                'CONTROLLER_MODEL_REF','WRITER_MODEL_REF','IMAGE_MODEL_REF'):
        os.environ.pop(key,None)
    from backend.settings import configure_runtime
    configure_runtime()
    from backend import app as module
    from backend.capabilities import manager
    from apps.web_service import Workbench
    from src.agent.conversation_store import PostgresConversationStore
    from src.agent.capabilities.models import safe
    import uvicorn
    from playwright.sync_api import expect,sync_playwright

    store = PostgresConversationStore(namespace='controls_trial_live_'+identity)
    current = Workbench(runtime,conversation_store=store)
    result = {'namespace':store.namespace,'output_directory':str(output),'status':'failed','stage':'startup',
              'public_publishes':0,'quota_syncs':0,'operations':[]}
    started = time.monotonic()
    capabilities = None
    server = thread = sock = None
    previous_service = module.app.state.service

    def record(stage):
        result['stage'] = stage
        result['wall_seconds'] = round(time.monotonic()-started,3)
        (output/'result.json').write_text(json.dumps(safe(result),ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        print(json.dumps({'stage':stage,'wall_seconds':result['wall_seconds']},ensure_ascii=False),flush=True)

    def trial(page, tool, values, label):
        record(label+'_preview')
        page.goto(f'http://127.0.0.1:{port}/#capabilities/tools/{quote(tool,safe="")}')
        page.get_by_role('button',name='试运行',exact=True).click()
        page.get_by_label('试运行输入（JSON）',exact=True).fill(json.dumps(values,ensure_ascii=False))
        with page.expect_response(lambda response:response.request.method=='POST' and response.url.endswith('/trial-preview')) as response:
            page.get_by_role('button',name='核对本次输入',exact=True).click()
        preview_response = response.value
        if not preview_response.ok:
            raise RuntimeError('TRIAL_PREVIEW_FAILED: '+str(safe(preview_response.json())))
        preview = preview_response.json()
        result[label+'_preview'] = preview
        assert preview['profile']['headless'] is True
        assert Path(preview['profile']['user_data_dir']).resolve().is_relative_to((runtime/'data/browser').resolve())
        if label=='generation':
            assert preview['input']['count']==1 and preview['input']['delivery']=='save_draft'
        page.screenshot(path=str(output/(label+'-preview.png')),full_page=False)
        page.get_by_role('checkbox',name='已核对输入及额度、文件和平台写入影响',exact=True).check()
        with page.expect_response(lambda response:response.request.method=='POST' and '/capability-trials/' in response.url) as response:
            page.get_by_role('button',name='确认本次试运行',exact=True).click()
        confirmed = response.value
        if not confirmed.ok:
            raise RuntimeError('TRIAL_CONFIRM_FAILED: '+str(safe(confirmed.json())))
        operation_id = confirmed.json()['operation_id']
        result['operations'].append({'label':label,'operation_id':operation_id,'preview_id':preview['preview_id']})
        record(label+'_running')
        last_report = time.monotonic()
        while True:
            response = page.request.get(f'http://127.0.0.1:{port}/api/capabilities/checks/{operation_id}')
            if not response.ok:
                raise RuntimeError('TRIAL_OBSERVATION_FAILED: do not resubmit '+operation_id)
            operation = response.json()
            if operation['status'] not in {'running','queued','pending'}:
                break
            if time.monotonic()-last_report>20:
                calls = capabilities.store.calls(resource_id=tool,origin='diagnostic')['rows']
                run_id = next((row['run_id'] for row in calls if row.get('run_id')),None)
                if run_id:
                    run = current.job_detail(run_id)
                    result['current_run'] = {k:run.get(k) for k in ('id','status','message','post_ids')}
                record(label+'_running')
                last_report = time.monotonic()
            time.sleep(1)
        result[label+'_operation'] = operation
        page.screenshot(path=str(output/(label+'-result.png')),full_page=False)
        record(label+'_terminal')
        if operation['status'] != 'succeeded':
            raise RuntimeError('TRIAL_NOT_COMPLETED: '+str(safe(operation.get('error',operation))))
        return operation['results']

    try:
        capabilities = manager(current)
        bindings = current.providers()['bindings']
        if not all(str(bindings.get(role,'')).startswith('minimax:') for role in ('agent','writer')):
            raise ValueError('MODEL_NOT_AUTHORIZED: configure MiniMax subscription for agent and writer first')
        result['model_bindings'] = bindings
        record('readonly_capability_preflight')
        from src.agent.capabilities.execution import runtime_tools
        from src.agent.capabilities.skill_runtime import resource_tools
        from src.agent.editorial_agent import EditorialAgentTools,AgentJob
        source = output/'diagnostic-skill'
        (source/'references').mkdir(parents=True)
        (source/'SKILL.md').write_text('---\nname: controls-diagnostic\ndescription: Verify news sources and dates\n---\nRead references/check.md before using a source.\n',encoding='utf-8')
        (source/'references/check.md').write_text('Keep the original source URL and publication date. Do not invent missing facts.\n',encoding='utf-8')
        preview = capabilities.skills.preview(str(source))
        skill = capabilities.skills.commit(preview['preview_id'],preview['hash'])
        selected = capabilities.skills.select('',mode='manual',names=[skill['id']])
        connection = capabilities.mcp.discover('mcp_local')
        tool = next(row for row in connection['tools'] if row['name']=='runtime_status')
        capabilities.mcp.tool_policy('mcp_local',{'expected_revision':connection['revision'],'tool_name':'runtime_status',
            'schema_hash':tool['schema_hash'],'stages':['preparation'],'purpose':'operations'})
        preflight_id = uuid4().hex
        capabilities.store.freeze(preflight_id,capabilities.all_tools()+resource_tools(selected),metadata={'origin':'diagnostic'})

        def no_content(*args,**kwargs):
            raise RuntimeError('PREFLIGHT_SIDE_EFFECT_DENIED')

        adapters = EditorialAgentTools(sync_context=no_content,generate=no_content,review=no_content,upload=no_content,
            plan=lambda jobs,context:{'job_order':[0],'tool_calls':[
                {'tool_id':'skill_resource:'+skill['id'],'arguments':{'path':'references/check.md'},'reason':'explicit diagnostic selection'},
                {'tool_id':'mcp:mcp_local:runtime_status','arguments':{},'reason':'explicit readonly diagnostic'}]})
        wrapped = runtime_tools(adapters,preflight_id,conversation_context={'skills':selected},require_frozen=True)
        try:
            preflight = wrapped.plan([AgentJob(kind='daily_news',title='每日新闻')],{})
            assert preflight['mcp_preparation'][0]['status']=='succeeded',preflight['mcp_preparation']
            assert preflight['mcp_preparation'][0]['output']['status']=='ready'
            assert preflight['skill_preparation'][0]['output']['loaded_resources']==1
        finally:
            wrapped.capability_cleanup()
        result['preflight'] = {'run_id':preflight_id,'skill_id':skill['id'],'skill_version':skill['version'],
                              'results':preflight,'calls':capabilities.store.calls(run_id=preflight_id)['rows'],
                              'selection':'explicit diagnostic; not an LLM tool-selection test'}
        module.app.state.service = current
        sock = socket.socket()
        for port in range(49152,65000):
            try:
                sock.bind(('127.0.0.1',port))
                break
            except OSError:
                continue
        else:
            raise RuntimeError('PORT_UNAVAILABLE')
        os.environ['REDBOOK_AGENT_PORT'] = str(port)
        server = uvicorn.Server(uvicorn.Config(module.app,log_level='error',lifespan='off'))
        thread = threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True)
        thread.start()
        deadline = time.monotonic()+20
        while not server.started:
            if not thread.is_alive() or time.monotonic()>deadline:
                raise RuntimeError('SERVER_UNAVAILABLE')
            time.sleep(.05)
        record('gui_ready')
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome',headless=True)
            try:
                page = browser.new_page(viewport={'width':1440,'height':1000})
                page_errors=[]
                page.on('pageerror',lambda error:page_errors.append(str(error)))
                run = trial(page,'builtin:news.generate',{'query':query,'delivery':'save_draft'},'generation')
                assert len(run['post_ids'])==1
                post_id = run['post_ids'][0]
                result['post_id']=post_id
                trial(page,'builtin:xhs.drafts.read',{'post_id':post_id},'readback')
                post = current.post(post_id)
                assert post['readback']=='verified' and post['status']=='saved_as_draft'
                proof = next(step for step in post['steps'] if step['name']=='readback_saved_draft' and step['status']=='success')
                result['platform_readback']=json.loads(proof['detail'])
                assert result['platform_readback']['actual_title']==post['title']
                assert result['platform_readback']['actual_image_count']==len(post['assets'])
                result['post']=post
                result['generation_calls']=capabilities.store.calls(run_id=run['run_id'],limit=200)['rows']
                assert not page_errors,page_errors
                result.update(status='passed',browser_errors=page_errors)
                record('complete')
            except Exception:
                page.screenshot(path=str(output/'failure.png'),full_page=False)
                raise
            finally:
                browser.close()
    except Exception as error:
        result['error_type']=type(error).__name__
        result['error']=safe(str(error))
        raise
    finally:
        if capabilities:
            capabilities.operations.close()
        if server:
            server.should_exit=True
        if thread:
            thread.join(10)
        if sock:
            sock.close()
        module.app.state.service=previous_service
        record(result['stage'])
        print(json.dumps(safe(result),ensure_ascii=False,default=str),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root',type=Path,default=Path('E:/AI/codex/redbook_runtime'))
    parser.add_argument('--query',default='科技产业 社会民生')
    parser.add_argument('--execute-one-draft',action='store_true')
    args=parser.parse_args()
    if not args.execute_one_draft:
        parser.error('explicit --execute-one-draft is required; this generates and saves one platform draft')
    try:
        verify(args.runtime_root,args.query)
    except Exception as error:
        print('diagnostic_failed='+type(error).__name__,file=sys.stderr)
        raise SystemExit(1) from None
