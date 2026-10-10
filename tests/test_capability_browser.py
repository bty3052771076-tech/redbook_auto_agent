"""Browser/API boundary contracts. Fixtures never enter the shipped frontend."""

import json
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import unquote
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import expect, sync_playwright


ARTIFACTS = Path("E:/AI/codex/redbook_runtime/data/tmp/capability-browser")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def frontend_url():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TEMP=str(ARTIFACTS), TMP=str(ARTIFACTS),
               npm_config_cache=str(ARTIFACTS / "npm-cache"))
    result = subprocess.run(["npm.cmd", "run", "build", "--", "--outDir", str(ARTIFACTS / "dist")],
                            cwd=ROOT / "frontend", env=env, capture_output=True, text=True)
    (ARTIFACTS / "build.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr

    class Static(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ARTIFACTS / "dist"), **kwargs)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Static)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.fixture
def ui(frontend_url):
    tool = {"id": "builtin:news.search", "name": "检索新闻", "description": "检索候选来源",
            "kind": "builtin", "group": "news", "enabled": True, "revision": 2,
            "version": "v2", "health": {"status": "ready", "observed_at": 1791420000},
            "stages": ["preparation"], "dependencies": [], "effects": ["read_only"],
            "input_schema": {"properties": {"query": {"type": "string"}}},
            "output_schema": {}, "active_runs": [{"id": "run_old", "version": "v1"}]}
    requests = []
    state = {"tool": tool, "connections": [], "memory": [], "skills": [], "documents": [],
             "conversations": [], "runs": [], "default_mode": "off", "conflict": False, "polls": 0,
             "preview_valid": True, "next_cursor": None, "skill_body_reads": 0}

    def contract(route):
        request = route.request
        path = unquote(request.url.split("/api", 1)[1].split("?", 1)[0])
        body = request.post_data_json if request.post_data else None
        requests.append((request.method, path, body, request.url))
        payload, status = {}, 200
        if path == "/session":
            payload = {"status": "ready"}
        elif path == "/connections":
            payload = {"database": {"status": "ready"}, "providers": {"bindings": {}}, "models": {"rows": []}}
        elif path == "/capabilities":
            payload = {"rows": [tool], "next_cursor": state["next_cursor"], "database": {"status": "offline", "documents": 4390,
                       "indexed_documents": 4372, "empty_documents": 18, "pending_documents": 0},
                       "environment": {"python": "E:/AI/codex/redbook_agent/.venv/Scripts/python.exe"},
                       "issues": [{"resource_id": "mcp_bad", "message": "Python 路径不存在"}], "recent_calls": []}
            if 'catalog_counts' in state:
                payload.update(counts=state['catalog_counts'],total=state['catalog_counts']['registered'])
        elif path == "/capabilities/builtin:news.search":
            if request.method == "PATCH":
                if state["conflict"]:
                    payload, status = {"code": "REVISION_CONFLICT", "message": "配置已更新，请刷新后重试",
                                       "next_action": "refresh", "revision": 3}, 409
                else:
                    tool.update(enabled=body["enabled"], revision=3)
                    payload = tool
            else:
                payload = tool
        elif path == "/capabilities/builtin:news.generate":
            payload = {**tool,'id':'builtin:news.generate','name':'生成每日新闻',
                       'trial':{'supported':True,'template':{'query':'','delivery':'generate_only'},
                                'description':'只生成一篇，不公开发布'}}
        elif path == "/capabilities/builtin:news.generate/trial-preview":
            payload = {'preview_id':'trial_1','preview_hash':'exact-input-hash','expires_at':time.time()+600,
                       'input':{'kind':'daily_news','count':1,**{k:v for k,v in body.items() if k!='expected_revision'}},
                       'effects':['model','local_write'],'models':{'writer':'test-model'},'profile':{'headless':True}}
        elif path == '/capability-trials/trial_1/confirm':
            assert body == {'preview_hash':'exact-input-hash','acknowledge_effects':True}
            payload = {'operation_id':'check_1','status':'running'}
        elif path in ("/capabilities/checks", "/mcp/checks", "/mcp/connections/mcp_new/discover"):
            if path == '/mcp/connections/mcp_new/discover' and state.get('reset_policy_on_discover'):
                connection = state['connections'][0]
                connection['revision'] += 1
                connection['tools'][0]['schema_hash'] = 'changed-schema'
                connection['tool_policies']['search']['enabled'] = False
            payload = {"operation_id": "check_1", "status": "running"}
        elif path == "/capabilities/checks/check_1":
            state["polls"] += 1
            payload = {"status": "running" if state.get('hold_check') else "succeeded",
                       "stages": [{"name": "配置", "status": "succeeded"}], "results": []}
        elif path == "/mcp/connections":
            if request.method == "POST":
                state["connections"].append({**body, "id": "mcp_new", "revision": 1,
                                             "environment": {"TOKEN": {"configured": True}}, "headers": {}})
                payload = state["connections"][-1]
            else:
                payload = {"rows": state["connections"]}
        elif path == "/mcp/connections/mcp_new":
            connection = state["connections"][0]
            if request.method == 'PATCH':
                if body['expected_revision'] != connection['revision']:
                    payload, status = {'code':'REVISION_CONFLICT','message':'配置已更新，请重新核对'}, 409
                else:
                    connection.update({k:v for k,v in body.items() if k!='expected_revision'})
                    connection['revision'] += 1
                    payload = connection
            else:
                payload = connection
        elif path == "/mcp/import-preview":
            payload = {'rows':[{'name':'imported','transport':'streamable_http','url':'http://127.0.0.1:19876/mcp',
                'headers':{'Authorization':'已配置'},'import_ref':'owned-preview-ref','enabled':False,'issues':[]}]}
        elif path == "/skills":
            payload = {"rows": [{k: v for k, v in row.items() if k != "body"} for row in state["skills"]],
                       "default_mode": state["default_mode"], "policy_revision": 4, "directories": ["E:/skills/runtime"]}
        elif path == "/skills/defaults":
            state["default_mode"] = body["default_mode"]
            payload = {"mode": state["default_mode"], "revision": 5}
        elif path == "/skills/import-preview":
            payload = {"preview_id": "preview_1", "hash": "sha256-file-version", "valid": state["preview_valid"],
                       "issues": [] if state["preview_valid"] else ["非法 frontmatter"], "name": "发布核验",
                       "files": ["SKILL.md", "references/check.md"], "total_bytes": 480, "collision": False}
        elif path == "/skills/import-commit":
            payload = {"id": "skill_1", "name": "发布核验", "description": "核对发布来源", "source": "user_import",
                       "version": body["hash"], "revision": 1, "enabled": True,
                       "body": '# 来源核验\n<script>window.skillScriptExecuted=true</script>\n**核对来源**',
                       "files": ["SKILL.md", "references/check.md"], "versions": [], "recent_calls": []}
            state["skills"].append(payload)
        elif path == "/skills/skill_1/copy":
            original = state["skills"][0]
            assert body["expected_revision"] == original["revision"]
            payload = {**original, "id":"skill_copy", "name":body["name"], "source":"user_copy",
                       "enabled":False, "revision":1, "version":"copy-version",
                       "copied_from":{"id":original["id"], "revision":original["revision"], "version":original["version"]}}
            state["skills"].append(payload)
        elif path == "/skills/skill_copy":
            payload = state["skills"][1]
            if request.method == "PATCH":
                assert body["expected_revision"] == payload["revision"]
                payload.update(body=body["body"], revision=2, version="edited-copy-version")
        elif path == "/skills/skill_1":
            state["skill_body_reads"] += 1
            payload = state["skills"][0]
        elif path == "/skills/skill_1/resources":
            payload = {"content": "引用附件实际内容"}
        elif path == "/memory/items":
            if request.method == "POST":
                state["memory"].append({**body, "id": "memory_1", "revision": 1})
                payload = state["memory"][-1]
            else:
                payload = {"rows": state["memory"]}
        elif path == "/memory/items/memory_1":
            state["memory"][0].update(body)
            payload = state["memory"][0]
        elif path == "/memory/items/memory_1/forget":
            payload = {"operation_id": "check_1", "status": "running"}
        elif path == "/knowledge/documents":
            payload = {"rows": state["documents"], "namespaces": ["test-ui", "other-ui"], "next_cursor": None}
        elif path == "/knowledge/documents/doc_1":
            payload = state["documents"][0]
        elif path == "/knowledge/documents/doc_1/policy":
            state["documents"][0]["policy"].update(body)
            payload = state["documents"][0]
        elif path == "/knowledge/search":
            payload = {"rows": [{"document_id": "doc_1", "title": "测试原文", "content": "实际测试命中片段",
                                 "score": 0.42, "match_reason": "关键词与向量匹配"}],
                       "elapsed_ms": 12, "ranking": "hybrid", "embedding_model": "test-embedding", "embedding_dimensions": 384}
        elif path == "/conversations":
            payload = {"rows": state["conversations"]}
        elif path == "/conversations/conversation_1":
            payload = state["conversations"][0]
        elif path == "/conversations/conversation_1/plans/plan_1/capabilities":
            plan = state["conversations"][0]["plans"][0]
            if request.method == "PUT":
                assert body["version"] == plan["version"]
                plan.update(version=plan["version"] + 1, skill_mode=body["skill_mode"], skill_names=body["skill_names"])
            payload = {"plan": plan, "tools": [tool], "skills": [{"id": "skill_1", "name": "发布核验", "enabled": True}],
                       "skill_mode": plan["skill_mode"], "skill_names": plan["skill_names"], "memory": {"history": "执行时检索"},
                       "models": {"agent": "test-controller"}, "profile": {"path": "E:/test-profile", "verified": False},
                       "readiness": {"ready": True}, "frozen": False, "version": plan["version"]}
            if state.get('plan_memory'):
                payload['memory'] = state['plan_memory']
        elif path == "/plans/plan_1/confirm":
            plan = state["conversations"][0]["plans"][0]
            assert body["version"] == plan["version"]
            assert body["skill_mode"] == plan["skill_mode"]
            assert body["skill_names"] == plan["skill_names"]
            plan.update(job_id="run_1")
            state["conversations"][0]["runs"] = ["run_1"]
            state["runs"] = [{"id": "run_1", "status": "completed", "post_rows": []}]
            payload = state["runs"][0]
        elif path == "/runs":
            payload = {"rows": state["runs"]}
        elif path == "/runs/run_1":
            payload = state["runs"][0]
        elif path == "/runs/run_1/capabilities":
            payload = {"status": "not_recorded", "not_recorded": True}
        elif path in ("/drafts", "/capability-calls", "/resource-changes"):
            payload = {"rows": [], "namespaces": ["test-ui"], "next_cursor": None}
        else:
            payload, status = {"message": "未实现的测试接口 " + path}, 404
        if state.get('database_unavailable') and path == '/capabilities':
            payload.update(read_only=True, database={'status':'offline'}, recent_calls_available=False,
                error={'code':'POSTGRES_UNAVAILABLE','message':'PostgreSQL 暂不可用，仅显示静态内置目录'})
            payload['rows'] = [{**tool, 'enabled':False, 'configuration_available':False,
                                'health':{'status':'blocked'}}]
        if state.get('business_unavailable') and path in ['/connections','conversations','runs','drafts']:
            payload, status = {'code':'POSTGRES_UNAVAILABLE','error':'PostgreSQL 暂不可用'}, 503
        if state.get('skills_unavailable') and path == '/skills':
            payload, status = {'code':'POSTGRES_UNAVAILABLE','message':'PostgreSQL 暂不可用'}, 503
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True,
                                    env={**os.environ, "TEMP": str(ARTIFACTS), "TMP": str(ARTIFACTS)})
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/**", contract)
        page.goto(frontend_url)
        yield page, requests, state, errors, frontend_url
        browser.close()


def test_capability_center_readonly_navigation_and_deep_link(ui):
    page, requests, state, errors, url = ui
    page.get_by_role("button", name="能力中心", exact=True).click(timeout=3000)
    for name in ["概览", "工具", "MCP", "SKILLS", "记忆与知识库", "调用与变更"]:
        expect(page.get_by_role("tab", name=name, exact=True)).to_be_visible()
    expect(page.get_by_text("Python 路径不存在", exact=True)).to_be_visible()
    page.get_by_role("tab", name="工具", exact=True).click()
    page.get_by_role("button", name="检索新闻", exact=True).click()
    expect(page.get_by_role("dialog")).to_contain_text("检索候选来源")
    page.reload()
    expect(page.get_by_role("dialog")).to_contain_text("检索候选来源")
    assert "#capabilities/tools/builtin%3Anews.search" in page.url
    assert all(method == "GET" or path == "/session" for method, path, _, _ in requests)
    assert errors == []


def test_offline_static_directory_cannot_change_configuration(ui):
    page, requests, state, errors, url = ui
    state['database_unavailable'] = True
    page.goto(url+'#capabilities/tools')
    expect(page.get_by_role('button',name='检索新闻',exact=True)).to_be_visible()
    expect(page.get_by_text('PostgreSQL 暂不可用，仅显示静态内置目录',exact=False)).to_be_visible()
    expect(page.get_by_role('switch',name='启用检索新闻',exact=True)).to_be_disabled()
    expect(page.get_by_label('选择检索新闻',exact=True)).to_be_disabled()
    assert all(method=='GET' or path=='/session' for method,path,_,_ in requests)
    state['database_unavailable'] = False
    page.get_by_role('button',name='刷新状态',exact=True).click()
    expect(page.get_by_role('switch',name='启用检索新闻',exact=True)).to_be_enabled()
    assert errors == []


def test_cold_browser_can_enter_readonly_capabilities_when_business_data_is_unavailable(ui):
    page, requests, state, errors, url = ui
    state.update(database_unavailable=True, business_unavailable=True)
    page.goto(url+'#capabilities/tools')
    expect(page.get_by_role('button',name='检索新闻',exact=True)).to_be_visible()
    expect(page.get_by_role('switch',name='启用检索新闻',exact=True)).to_be_disabled()
    page.get_by_role('button',name='对话任务',exact=True).click()
    expect(page.get_by_label('任务要求',exact=True)).to_have_count(0)
    state.update(database_unavailable=False, business_unavailable=False)
    page.get_by_role('button',name='重新读取业务数据',exact=True).click()
    expect(page.get_by_label('任务要求',exact=True)).to_be_visible()
    assert all(method=='GET' or path=='/session' for method,path,_,_ in requests)
    assert errors == []


def test_skill_import_preview_mode_and_safe_lazy_body(ui):
    page, requests, state, errors, url = ui
    page.goto(url + "#capabilities/skills")
    page.get_by_role("button", name="导入技能", exact=True).first.click()
    source = page.get_by_label("文件夹或 ZIP 路径", exact=True)
    source.fill("E:/fixtures/skill.zip")
    state["preview_valid"] = False
    page.get_by_role("button", name="预览技能", exact=True).click()
    expect(page.get_by_role("button", name="确认导入", exact=True)).to_be_disabled()
    expect(page.get_by_text("非法 frontmatter", exact=False)).to_be_visible()
    source.fill("E:/fixtures/valid.zip")
    state["preview_valid"] = True
    page.get_by_role("button", name="预览技能", exact=True).click()
    page.get_by_role("button", name="确认导入", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert state["default_mode"] == "off"
    assert state["skill_body_reads"] == 0
    page.get_by_role("button", name="自动", exact=True).click()
    assert any(path == "/skills/defaults" and body == {"expected_revision": 4, "default_mode": "auto"}
               for _, path, body, _ in requests)
    page.get_by_role("button", name="发布核验", exact=True).click()
    expect(page.get_by_role("dialog")).to_contain_text("来源核验")
    assert page.evaluate("window.skillScriptExecuted === undefined")
    page.get_by_role("tab", name="引用文件", exact=True).click()
    page.get_by_role("button", name="references/check.md", exact=True).click()
    expect(page.get_by_role("dialog")).to_contain_text("引用附件实际内容")
    assert errors == []


def test_preference_write_and_forget_are_separate_explicit_actions(ui):
    page, requests, state, errors, url = ui
    page.goto(url + "#capabilities/memory")
    page.get_by_role("button", name="添加偏好", exact=True).click()
    page.get_by_label("偏好标识", exact=True).fill("writing-style")
    page.get_by_label("偏好内容", exact=True).fill("引用原始来源，不使用夸张标题")
    page.get_by_label("变更原因", exact=True).fill("人工确认")
    page.get_by_role("button", name="保存偏好", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    page.get_by_role("switch", name="启用偏好writing-style", exact=True).click()
    expect(page.get_by_role("switch", name="启用偏好writing-style", exact=True)).not_to_be_checked()
    assert not any(path.endswith("/forget") for _, path, _, _ in requests)
    page.get_by_role("button", name="引用原始来源，不使用夸张标题", exact=True).click()
    page.get_by_role("group").filter(has=page.get_by_text("忘记偏好", exact=True)).locator("summary").click()
    page.get_by_label("变更原因", exact=True).fill("用户要求忘记")
    page.get_by_role("button", name="确认忘记", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=10000)
    assert any(path == "/memory/items/memory_1/forget" and body["expected_revision"] == 1 for _, path, body, _ in requests)
    assert errors == []


def test_knowledge_namespace_search_and_policy_do_not_overwrite_original(ui):
    page, requests, state, errors, url = ui
    state["documents"] = [{"id": "doc_1", "title": "测试原文", "namespace": "test-ui", "body": "来源正文保持不变",
                           "index_status": "ready", "revision": 2, "policy": {"revision": 3, "excluded_purposes": []}}]
    page.goto(url + "#capabilities/memory?memory.view=documents&memory.namespace=test-ui")
    page.get_by_role("button", name="测试原文", exact=True).click()
    expect(page.get_by_role("dialog")).to_contain_text("来源正文保持不变")
    page.get_by_label("注释", exact=True).fill("人工核对注释")
    page.get_by_label("证据", exact=True).check()
    page.get_by_role("button", name="保存用途与注释", exact=True).click()
    expect(page.get_by_role("button", name="保存用途与注释", exact=True)).to_be_disabled()
    assert state["documents"][0]["body"] == "来源正文保持不变"
    assert state["documents"][0]["policy"]["namespace"] == "test-ui"
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    page.get_by_role("button", name="测试检索", exact=True).click()
    page.get_by_label("查询词", exact=True).fill("来源")
    page.get_by_role("button", name="执行检索", exact=True).click()
    expect(page.get_by_role("dialog")).to_contain_text("实际测试命中片段")
    expect(page.get_by_role("dialog")).to_contain_text("检索排序分数")
    assert any(path == "/knowledge/search" and body["namespace"] == "test-ui" for _, path, body, _ in requests)
    assert errors == []


@pytest.mark.parametrize('label,purpose', [('风格参考','style_reference'),('历史查重','duplicate_reference')])
def test_document_filter_policy_and_search_share_backend_purpose_values(ui,label,purpose):
    from urllib.parse import parse_qs,urlsplit
    page,requests,state,errors,url=ui
    state['documents']=[{'id':'doc_1','title':'可核验来源','namespace':'test-ui','body':'保持原文',
        'revision':2,'policy':{'revision':3,'excluded_purposes':[]}}]
    page.goto(url+'#capabilities/memory?memory.view=documents&memory.namespace=test-ui')
    page.get_by_label('文档用途',exact=True).select_option(label=label)
    page.get_by_role('button',name='可核验来源',exact=True).click()
    page.get_by_label(label,exact=True).check()
    page.get_by_role('button',name='保存用途与注释',exact=True).click()
    expect(page.get_by_role('button',name='保存用途与注释',exact=True)).to_be_disabled()
    assert state['documents'][0]['policy']['excluded_purposes']==[purpose]
    assert any(path=='/knowledge/documents' and parse_qs(urlsplit(request_url).query).get('purpose')==[purpose]
               for method,path,body,request_url in requests)
    page.keyboard.press('Escape')
    page.get_by_role('button',name='测试检索',exact=True).click()
    page.get_by_label('查询词',exact=True).fill('来源')
    page.get_by_role('dialog',name='测试检索',exact=True).get_by_role('combobox').select_option(label=label)
    page.get_by_role('button',name='执行检索',exact=True).click()
    expect(page.get_by_role('dialog')).to_contain_text('实际测试命中片段')
    assert any(path=='/knowledge/search' and body['purpose']==purpose for method,path,body,request_url in requests)
    assert errors==[]


def test_plan_capability_version_and_confirmation_preserve_defaults(ui):
    page, requests, state, errors, url = ui
    plan = {"id": "plan_1", "version": 7, "status": "pending", "executable": True,
            "jobs": [{"kind": "daily_news", "title": "每日新闻", "count": 1, "keywords": ["芯片"]}],
            "delivery": "generate_only", "platform": "xhs", "assistant_summary": "测试计划",
            "skill_mode": "auto", "skill_names": []}
    state["conversations"] = [{"id": "conversation_1", "title": "保留关键词的计划", "messages": [], "plans": [plan], "runs": [], "status": "pending"}]
    page.reload()
    page.get_by_label("当前对话", exact=True).select_option("conversation_1")
    page.get_by_text("本次能力", exact=True).click()
    page.get_by_role("button", name="调整本次能力", exact=True).click()
    page.get_by_role("button", name="手动", exact=True).click()
    page.get_by_label("本次选择发布核验", exact=True).check()
    expect(page.get_by_role("button", name="确认并执行", exact=True)).to_be_disabled()
    page.get_by_role("button", name="应用本次能力", exact=True).click()
    expect(page.get_by_text("计划版本 8", exact=True)).to_be_visible()
    assert plan["jobs"][0]["keywords"] == ["芯片"]
    page.get_by_role("button", name="确认并执行", exact=True).click()
    expect(page.get_by_role("button", name="计划已执行", exact=True)).to_be_visible()
    expect(page.get_by_text("该版本未采集", exact=True)).to_be_visible()
    assert any(path == "/plans/plan_1/confirm" and body["version"] == 8
               and body["skill_mode"] == "manual" and body["skill_names"] == ["skill_1"]
               for _, path, body, _ in requests)
    assert errors == []


def test_filters_pagination_restore_and_drawer_focus(ui):
    page, requests, state, errors, url = ui
    state["next_cursor"] = "cursor-next"
    page.goto(url + "#capabilities/tools")
    page.get_by_label("搜索名称或用途", exact=True).fill("检索")
    page.get_by_role("button", name="下一页", exact=True).click()
    page.get_by_role("tab", name="MCP", exact=True).click()
    page.get_by_role("tab", name="工具", exact=True).click()
    expect(page.get_by_label("搜索名称或用途", exact=True)).to_have_value("检索")
    assert "tools.cursor=cursor-next" in page.url
    assert any("query=%E6%A3%80%E7%B4%A2" in url and "limit=50" in url and "cursor=cursor-next" in url
               for _, path, _, url in requests if path == "/capabilities")
    opener = page.get_by_role("button", name="检索新闻", exact=True)
    opener.click()
    expect(page.get_by_role("dialog")).to_contain_text("检索候选来源")
    page.keyboard.press("Tab")
    assert page.get_by_role("dialog").evaluate("el => el.contains(document.activeElement)")
    page.keyboard.press("Shift+Tab")
    assert page.get_by_role("dialog").evaluate("el => el.contains(document.activeElement)")
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(opener).to_be_focused()
    assert errors == []


def test_tool_policy_conflict_and_check_operation(ui):
    page, requests, state, errors, url = ui
    page.goto(url + "#capabilities/tools")
    toggle = page.get_by_role("switch", name="启用检索新闻", exact=True)
    expect(toggle).to_be_checked()
    state["conflict"] = True
    toggle.click()
    expect(page.get_by_role("alert")).to_contain_text("配置已更新，请刷新后重试")
    expect(toggle).to_be_checked()
    state["conflict"] = False
    toggle.click()
    expect(toggle).not_to_be_checked()
    page.get_by_label("选择检索新闻", exact=True).check()
    page.get_by_role("button", name="检查所选", exact=True).click()
    expect(page.get_by_text("检测完成", exact=True)).to_be_visible(timeout=10000)
    page.wait_for_timeout(1800)
    assert state["polls"] == 1
    assert any(method == "PATCH" and body == {"expected_revision": 2, "enabled": False}
               for method, path, body, _ in requests)
    assert any(body == {"resource_ids": ["builtin:news.search"]} for _, _, body, _ in requests)
    assert errors == []


def test_mcp_form_dirty_focus_and_responsive(ui):
    page, requests, state, errors, url = ui
    page.goto(url + "#capabilities/mcp")
    page.get_by_role("button", name="添加连接", exact=True).click()
    page.get_by_label("连接名称", exact=True).fill("只读测试连接")
    page.keyboard.press("Escape")
    expect(page.get_by_text("有未保存修改", exact=True)).to_be_visible()
    page.get_by_role("button", name="保留修改", exact=True).click()
    page.get_by_label("执行文件", exact=True).fill("E:/python.exe")
    page.get_by_label("环境变量（JSON，只写）", exact=True).fill('{"TOKEN":"test-secret-never-echo"}')
    page.get_by_role("button", name="保存连接", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert state["connections"][0]["enabled"] is False
    assert not any("discover" in path for _, path, _, _ in requests)
    page.get_by_role("button", name="只读测试连接", exact=True).click()
    expect(page.get_by_role("dialog")).not_to_contain_text("test-secret-never-echo")
    for width in (1440, 1024, 390):
        page.set_viewport_size({"width": width, "height": 900})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert page.get_by_role("dialog").bounding_box()["width"] <= width
        page.screenshot(path=str(ARTIFACTS / f"mcp-{width}.png"), full_page=True)
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert errors == []


def test_mcp_checks_use_connection_routes_and_loaded_policy_matches_saved_state(ui):
    page, requests, state, errors, url = ui
    state['connections'] = [{'id':'mcp_new','name':'只读连接','revision':2,'enabled':True,
        'health':{'status':'ready'},'transport':'streamable_http','tools':[{'name':'search','schema_hash':'approved',
        'read_only_eligible':True}], 'tool_policies':{'search':{'enabled':True,'stages':['evidence'],
        'purpose':'evidence','read_only_confirmed':True}}}]
    page.goto(url+'#capabilities/mcp')
    page.get_by_label('选择只读连接', exact=True).check()
    page.get_by_role('button',name='检查所选',exact=True).click()
    expect(page.get_by_text('检测完成',exact=True)).to_be_visible(timeout=10000)
    assert any(path=='/mcp/checks' and body=={'connection_ids':['mcp_new']} for _,path,body,_ in requests)
    page.get_by_role('button',name='只读连接',exact=True).click()
    expect(page.get_by_role('switch',name='允许search',exact=True)).to_be_checked()
    expect(page.get_by_label('证据阶段',exact=True)).to_be_checked()
    expect(page.get_by_label('使用目的',exact=True)).to_have_value('evidence')
    expect(page.get_by_label('已核对可信服务及只读凭据',exact=True)).to_be_checked()
    page.get_by_role('button',name='检测连接',exact=True).click()
    expect(page.get_by_role('dialog').get_by_text('检测完成',exact=True)).to_be_visible(timeout=10000)
    assert any(path=='/mcp/connections/mcp_new/discover' for _,path,_,_ in requests)
    assert not any(path=='/capabilities/checks' for _,path,_,_ in requests)
    page.reload()
    expect(page.get_by_role('switch',name='允许search',exact=True)).to_be_checked()
    assert errors == []


def test_mcp_schema_change_does_not_display_old_approval(ui):
    page, requests, state, errors, url = ui
    state['connections'] = [{'id':'mcp_new','name':'核验连接','revision':2,'enabled':True,
        'health':{'status':'ready'},'transport':'streamable_http','tools':[{'name':'search','schema_hash':'approved',
        'read_only_eligible':True}], 'tool_policies':{'search':{'enabled':True,'stages':['evidence'],
        'purpose':'evidence','read_only_confirmed':True}}}]
    state['reset_policy_on_discover'] = True
    page.goto(url+'#capabilities/mcp/mcp_new')
    expect(page.get_by_role('switch',name='允许search',exact=True)).to_be_checked()
    page.get_by_role('button',name='检测并发现工具',exact=True).click()
    expect(page.get_by_role('dialog').get_by_text('检测完成',exact=True)).to_be_visible(timeout=10000)
    expect(page.get_by_role('switch',name='允许search',exact=True)).not_to_be_checked()
    assert errors == []


def test_mcp_edit_retains_original_revision_during_background_discovery(ui):
    page, requests, state, errors, url = ui
    state['connections'] = [{'id':'mcp_new','name':'原连接','revision':2,'enabled':True,
        'health':{'status':'ready'},'transport':'streamable_http','url':'http://127.0.0.1:19800/mcp',
        'tools':[{'name':'search','schema_hash':'approved','read_only_eligible':True}],
        'tool_policies':{'search':{'enabled':True}}}]
    state['reset_policy_on_discover'] = True
    state['hold_check'] = True
    page.goto(url+'#capabilities/mcp/mcp_new')
    page.get_by_role('button',name='检测连接',exact=True).click()
    page.get_by_role('button',name='编辑连接',exact=True).click()
    page.get_by_label('连接名称',exact=True).fill('我的未保存修改')
    state['connections'][0]['url'] = 'http://127.0.0.1:19801/mcp'
    with page.expect_response(lambda response: response.url.endswith('/api/mcp/connections')
                              and response.request.method=='GET'):
        state['hold_check'] = False
    page.get_by_role('button',name='保存连接',exact=True).click()
    assert state['connections'][0]['url'] == 'http://127.0.0.1:19801/mcp'
    expect(page.get_by_role('alert').filter(has_text='配置已更新')).to_be_visible()
    expect(page.get_by_text('连接配置已更新',exact=False)).to_be_visible()
    expect(page.get_by_label('连接名称',exact=True)).to_have_value('我的未保存修改')
    submitted = [body for method,path,body,_ in requests if method=='PATCH' and path=='/mcp/connections/mcp_new']
    assert len(submitted) == 1 and submitted[0]['expected_revision'] == 2
    assert errors == []


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_every_capability_page_fits_supported_viewports(ui, width):
    page, requests, state, errors, url = ui
    state['memory'] = [{'id':'memory_1','key':'long','content':'当前指令优先并核对来源'*12,
        'scope':'workspace','active':True,'revision':1}]
    state['skills'] = [{'id':'skill_1','name':'来源核验技能','body':'核验来源正文',
        'description':'针对官方模型发布核验日期与具体变化','revision':1,'enabled':True}]
    state['connections'] = [{'id':'mcp_new','name':'只读新闻连接','revision':2,'enabled':False,
        'health':{'status':'blocked','error':'本地路径缺失，请检查应用虚拟环境'},
        'transport':'stdio','command':'E:/'+('long_path_segment/'*12)+'python.exe','tools':[]}]
    state['documents'] = [{'id':'doc_1','title':'官方发布与原文索引记录','body':'可核验原文',
        'namespace':'test-ui','policy':{'excluded_purposes':[]}}]
    page.set_viewport_size({'width':width,'height':900})
    routes = [('overview','概览'),('tools','工具'),('mcp','MCP'),('skills','SKILLS'),
        ('memory','记忆与知识库'),('memory?memory.view=documents','记忆与知识库'),
        ('memory?memory.view=context','记忆与知识库'),('memory?memory.view=checkpoints','记忆与知识库'),
        ('calls','调用与变更'),('mcp/mcp_new','MCP')]
    for index, (route, label) in enumerate(routes):
        page.goto(url+'#capabilities/'+route)
        expect(page.get_by_role('tabpanel',name=label,exact=True)).to_be_visible()
        expect(page.locator('.cap-loading')).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), route
        if route == 'mcp/mcp_new':
            box = page.get_by_role('dialog').bounding_box()
            assert 0 <= box['x'] and box['x']+box['width'] <= width+1
        page.screenshot(path=str(ARTIFACTS/f'pages-{width}-{index}.png'))
    assert all(method=='GET' or path=='/session' for method,path,_,_ in requests)
    assert errors == []


def test_mcp_import_form_preserves_safe_preview_reference(ui):
    page, requests, state, errors, url = ui
    page.goto(url+'#capabilities/mcp/import')
    page.get_by_label('MCP JSON 配置',exact=True).fill(json.dumps({'mcpServers':{'imported':{
        'url':'http://127.0.0.1:19876/mcp','headers':{'Authorization':'synthetic-import-secret'}}}}))
    page.get_by_role('button',name='预览配置',exact=True).click()
    page.get_by_role('button',name='填入 imported',exact=True).click()
    expect(page.get_by_label('Header（JSON，只写）',exact=True)).to_have_value('')
    page.get_by_role('button',name='保存连接',exact=True).click()
    expect(page.get_by_role('dialog')).to_have_count(0)
    assert any(path=='/mcp/connections' and body.get('import_ref')=='owned-preview-ref' for _,path,body,_ in requests if isinstance(body,dict))
    assert errors == []


def test_skill_version_adoption_submits_revision_not_metadata_hash(ui):
    page, requests, state, errors, url = ui
    state['skills'] = [{'id':'skill_1','name':'核验技能','body':'正文','revision':2,'enabled':True,
        'versions':[{'revision':1,'hash':'configuration-hash-not-content-version'}]}]
    page.goto(url+'#capabilities/skills/skill_1?skills.detailTab=versions')
    page.get_by_role('button',name='采用此版本',exact=True).click()
    assert any(method=='PATCH' and path=='/skills/skill_1' and body.get('version_revision')==1
        and 'version' not in body for method,path,body,_ in requests)
    assert errors == []


def test_builtin_skill_copy_opens_editable_personal_version(ui):
    page, requests, state, errors, url = ui
    original = {'id':'skill_1','name':'release-check','body':'核验官方原文','revision':2,
                'version':'original-version','source':'builtin','enabled':True,'description':'核验来源'}
    state['skills'] = [dict(original)]
    page.goto(url+'#capabilities/skills/skill_1')
    expect(page.get_by_role('button',name='编辑正文',exact=True)).to_have_count(0)
    page.get_by_role('button',name='创建个人副本',exact=True).click()
    page.get_by_label('副本名称',exact=True).fill('personal-release')
    page.get_by_role('button',name='确认创建副本',exact=True).click()
    expect(page.get_by_role('dialog')).to_contain_text('personal-release')
    page.get_by_role('button',name='编辑正文',exact=True).click()
    page.screenshot(path=str(ARTIFACTS/'skill-copy-edit.png'))
    (ARTIFACTS/'skill-copy-edit.html').write_text(page.content(),encoding='utf-8')
    page.get_by_label('技能正文',exact=True).fill('人工补充核验日期')
    page.get_by_role('button',name='保存新版本',exact=True).click()
    expect(page.get_by_role('dialog')).to_contain_text('人工补充核验日期')
    assert state['skills'][0] == original
    assert state['skills'][1]['enabled'] is False
    assert len([r for r in requests if r[0]=='POST' and r[1].endswith('/copy')]) == 1
    assert errors == []


def test_plan_shows_overridden_memory_content_source_and_reason(ui):
    page, requests, state, errors, url = ui
    plan = {'id':'plan_1','version':1,'status':'pending','executable':True,
            'jobs':[{'kind':'daily_news','title':'每日新闻','count':1}], 'delivery':'generate_only',
            'platform':'xhs','skill_mode':'off','skill_names':[]}
    state['conversations'] = [{'id':'conversation_1','title':'偏好核验','messages':[],
                              'plans':[plan],'runs':[],'status':'pending'}]
    state['plan_memory'] = [{'id':'memory_current','content':'AI卡片可含文字','scope':'column',
        'source_ref':'manual:local_user','revision':2,'applies_to':['daily_ai_digest'],
        'overridden_preferences':[{'id':'memory_old','content':'所有配图不含文字',
            'source_ref':'message:original-choice','revision':1,'reason':'更具体的作用范围优先'}]}]
    page.reload()
    page.get_by_label('当前对话',exact=True).select_option('conversation_1')
    page.get_by_text('本次能力',exact=True).click()
    page.get_by_text('被覆盖的旧偏好（1）',exact=True).click()
    expect(page.get_by_text('所有配图不含文字',exact=True)).to_be_visible()
    expect(page.get_by_text('message:original-choice',exact=True)).to_be_visible()
    expect(page.get_by_text('更具体的作用范围优先',exact=True)).to_be_visible()
    assert all(method=='GET' or path=='/session' for method,path,_,_ in requests)
    assert errors == []


def test_failed_refresh_keeps_only_non_executable_last_known_preview(ui):
    page, requests, state, errors, url = ui
    state['skills'] = [{'id':'skill_1','name':'known-skill','description':'上次保存的技能','enabled':True,'revision':1}]
    page.goto(url+'#capabilities/skills')
    expect(page.get_by_role('switch',name='启用known-skill',exact=True)).to_be_visible()
    state['skills_unavailable'] = True
    page.get_by_role('button',name='刷新状态',exact=True).click()
    expect(page.get_by_role('switch',name='启用known-skill',exact=True)).to_have_count(0)
    page.get_by_text('上次读取的只读记录',exact=True).click()
    expect(page.get_by_text('上次保存的技能',exact=False)).to_be_visible()
    expect(page.get_by_text('不代表当前状态，不能据此执行或修改配置',exact=False)).to_be_visible()
    assert all(method=='GET' or path=='/session' for method,path,_,_ in requests)
    assert errors == []


def test_mcp_form_saves_per_connection_proxy_policy(ui):
    page, requests, state, errors, url = ui
    page.goto(url+'#capabilities/mcp/new')
    page.get_by_label('连接名称',exact=True).fill('local-http')
    page.get_by_label('传输方式',exact=True).select_option('streamable_http')
    page.get_by_label('HTTP 地址',exact=True).fill('http://127.0.0.1:19876/mcp')
    page.get_by_label('网络策略',exact=True).select_option('custom')
    page.get_by_label('代理地址',exact=True).fill('http://127.0.0.1:7890')
    page.get_by_role('button',name='保存连接',exact=True).click()
    expect(page.get_by_role('dialog')).to_have_count(0)
    assert state['connections'][0]['network_policy'] == {'mode':'custom','proxy_url':'http://127.0.0.1:7890'}
    assert errors == []


def test_tool_trial_previews_exact_input_before_single_confirmed_operation(ui):
    page, requests, state, errors, url = ui
    page.goto(url+'#capabilities/tools/builtin%3Anews.generate')
    expect(page.get_by_role('button',name='检查可用性',exact=True)).to_be_visible()
    page.get_by_role('button',name='试运行',exact=True).click()
    page.get_by_label('试运行输入（JSON）',exact=True).fill(json.dumps({'query':'芯片','delivery':'generate_only'}))
    page.get_by_role('button',name='核对本次输入',exact=True).click()
    expect(page.get_by_role('button',name='确认本次试运行',exact=True)).to_be_disabled()
    assert not [r for r in requests if r[1].endswith('/confirm')]
    page.get_by_label('已核对输入及额度、文件和平台写入影响',exact=True).check()
    page.get_by_role('button',name='确认本次试运行',exact=True).click()
    expect(page.get_by_text('检测完成',exact=True)).to_be_visible()
    assert len([r for r in requests if r[1].endswith('/confirm')]) == 1
    assert len([r for r in requests if r[1].endswith('/trial-preview')]) == 1
    assert errors == []


def test_builtin_tool_detail_renders_array_outputs_and_workflow_concurrency(ui):
    from src.agent.capabilities.registry import builtin_catalog
    page,requests,state,errors,url=ui
    definition=next(row for row in builtin_catalog() if row['id']=='builtin:news.generate')
    state['tool'].update(definition)
    page.goto(url+'#capabilities/tools/builtin%3Anews.generate')
    drawer=page.get_by_role('dialog')
    expect(drawer.get_by_text('title',exact=True)).to_be_visible()
    expect(drawer.get_by_text('body',exact=True)).to_be_visible()
    expect(drawer.get_by_text('job',exact=True)).to_be_visible()
    expect(drawer.get_by_text('由工作流并发队列控制',exact=True)).to_be_visible()
    assert not [request for request in requests if request[0]!='GET' and request[1]!='/session']
    assert errors==[]


def test_tool_dependency_opens_registered_resource_without_write(ui):
    page,requests,state,errors,url=ui
    state['tool']['dependencies']=['builtin:news.search']
    state['tool']['dependency_details']=[{'resource_id':'builtin:news.search','name':'检索新闻','kind':'builtin'}]
    page.goto(url+'#capabilities/tools/builtin%3Anews.generate')
    page.get_by_role('dialog').get_by_role('button',name='检索新闻',exact=True).click()
    expect(page.get_by_role('dialog')).to_have_attribute('aria-label','检索新闻')
    assert '#capabilities/tools/builtin%3Anews.search' in page.url
    assert all(method=='GET' or path=='/session' for method,path,_,_ in requests)
    assert errors==[]


def test_overview_uses_all_registered_counts_instead_of_page_size(ui):
    page,requests,state,errors,url=ui
    state['next_cursor']='next'
    state['catalog_counts']={'available':27,'registered':60,'enabled':30}
    page.goto(url+'#capabilities/overview')
    expect(page.get_by_text('27 / 60',exact=True)).to_be_visible()
    expect(page.get_by_text('当前页可用数 / 登记总数',exact=True)).to_have_count(0)
    assert all(method=='GET' or path=='/session' for method,path,_,_ in requests)
    assert errors==[]
