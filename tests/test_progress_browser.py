"""Browser contract tests with simulated events, never generation or platform writes."""

import json
import os
import time
import mimetypes
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from backend.progress import activity_reply, build_activity


BASE = "http://127.0.0.1:8786"
CID, RID, P1, P2 = "d" * 32, "a" * 32, "b" * 32, "c" * 32
ARTIFACTS = None


class Scenario:
    def __init__(self):
        self.phase = "generate"
        self.offline = False
        self.legacy = False
        self.calls = []
        self.started = time.time() - 70
        self.plan = {"id": "e" * 32, "version": 1, "status": "running", "executable": True,
                     "job_id": RID, "jobs": [{"kind": "daily_news", "title": "每日新闻", "count": 2}],
                     "delivery": "save_draft", "platform": "xhs", "assistant_summary": "已识别每日新闻2条，确认后执行。"}
        self.conversation = {"id": CID, "title": "实时进度测试", "status": "running", "plans": [self.plan], "runs": [RID],
                             "messages": [{"id": "u", "role": "user", "content": "生成2条每日新闻", "created_at": self.started},
                                          {"id": "v", "role": "assistant", "content": self.plan["assistant_summary"], "created_at": self.started}]}

    def run(self):
        at = time.time()
        event = {"id": 1, "at": at, "message": "[agent] stage=generate | success | daily_news posts=2 attempt=1"}
        run = {"id": RID, "title": "实时进度测试", "status": "running", "started_at": self.started,
               "created_at": self.started, "stage": "生成配图", "events": [event], "post_rows": []}
        cp = {"jobs": self.plan["jobs"], "job_index": 0}
        if self.phase == "review":
            run["events"].append({"id": 2, "at": at + .01, "message": "[agent] stage=review | in_progress | daily_news"})
            run["stage"] = "review"
        if self.phase == "completed":
            run.update(status="completed", ended_at=at, stage="finish", post_rows=[{"id": P1, "readback": "verified"}, {"id": P2, "readback": "unverified"}])
            cp.update(job_index=1, item_status={f"0:{P1}:cafe": "saved", f"0:{P2}:cafe": "saved"})
            run["events"].append({"id": 3, "at": at + .01, "message": "[agent] stage=finish | success | uploaded=2"})
        if not self.legacy:
            run["activity"] = build_activity(run, cp, now=at)
        return run

    def route(self, route):
        request = route.request
        path = request.url.split("8786", 1)[1]
        self.calls.append((request.method, path))
        if path == f"/api/runs/{RID}" and self.offline:
            route.abort("connectionfailed")
            return
        if path == "/api/session":
            data = {"status": "ready"}
        elif path == "/api/conversations":
            data = {"rows": [self.conversation]}
        elif path == f"/api/conversations/{CID}":
            data = self.conversation
        elif path == f"/api/conversations/{CID}/messages":
            content = request.post_data_json["content"]
            assistant = {"id": "reply", "role": "assistant", "content": activity_reply(self.run()["activity"]), "created_at": time.time()}
            self.conversation["messages"].extend([{"id": "question", "role": "user", "content": content, "created_at": time.time()}, assistant])
            data = {"plan": None, "assistant": assistant, "run": self.run()}
        elif path == "/api/runs":
            data = {"rows": [{**self.run(), "status_label": "执行中", "display_message": "生成内容与配图"}]}
        elif path == f"/api/runs/{RID}":
            data = self.run()
        elif path == "/api/drafts":
            data = {"rows": []}
        elif path == "/api/connections":
            data = {"database": {"status": "ready", "documents": 10}, "providers": {"bindings": {}, "connections": []},
                    "models": {"rows": []}, "profile_configured": True, "profile_login": "未验证"}
        elif path.endswith('/capabilities') and request.method == 'GET':
            data = {'version': self.plan.get('version', 1), 'skill_mode': 'off', 'skill_names': [],
                    'disabled_tools': [], 'tools': [], 'skills': [], 'memory': {}, 'profile': '测试隔离',
                    'readiness': {'ready': True}, 'calls': []}
        else:
            pytest.fail(f"Unexpected API call: {request.method} {path}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(deepcopy(data), ensure_ascii=False))


@pytest.fixture
def browser(tmp_path, monkeypatch):
    global ARTIFACTS
    ARTIFACTS = tmp_path
    temp = ARTIFACTS / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    original = os.environ.get("TEMP")
    os.environ["TEMP"] = str(temp)
    with sync_playwright() as p:
        chrome = os.getenv("REDBOOK_CHROME_EXECUTABLE") or r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        browser = p.chromium.launch(headless=True, executable_path=chrome)
        yield browser
        browser.close()
    if original:
        os.environ["TEMP"] = original
    else:
        os.environ.pop("TEMP", None)


def prepare(browser, scenario, *, restore=True):
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    dist = Path(__file__).resolve().parents[1] / "frontend/dist"

    def local_frontend(route):
        path = urlsplit(route.request.url).path
        asset = (dist / path.lstrip("/")).resolve() if path != "/" else dist / "index.html"
        assert asset.is_relative_to(dist) and asset.is_file(), path
        route.fulfill(content_type=mimetypes.guess_type(str(asset))[0] or "application/octet-stream",
                      body=asset.read_bytes())

    context.route("**/*", local_frontend)
    context.route("**/api/**", scenario.route)
    if restore:
        context.add_init_script(f"localStorage.setItem('agent-conversation', '{CID}')")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE)
    page.wait_for_load_state("networkidle")
    expect(page.get_by_role("heading", name="任务对话", exact=True)).to_be_visible()
    return context, page, errors


def test_chat_live_progress_reconnect_question_reload_and_completion(browser):
    scenario = Scenario()
    context, page, errors = prepare(browser, scenario)
    progress = page.locator(".live-message .run-progress")
    expect(progress.get_by_text("每日新闻 · 生成内容与配图", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="正在执行", exact=True)).to_be_disabled()
    assert not page.locator(".live-message .technical-log").evaluate("e => e.open")
    page.screenshot(path=str(ARTIFACTS / "desktop-running.png"), full_page=True)
    scenario.phase = "review"
    expect(progress.get_by_text("每日新闻 · 审查内容与配图", exact=True)).to_be_visible(timeout=8000)
    scenario.offline = True
    expect(progress.get_by_text("进度连接中断，正在重连", exact=True)).to_be_visible(timeout=8000)
    scenario.offline = False
    expect(progress.get_by_text("进度连接中断，正在重连", exact=True)).not_to_be_visible(timeout=8000)
    page.get_by_role("button", name="询问进度", exact=True).click()
    expect(page.locator(".message.assistant").filter(has_text="预计剩余时间尚无可靠依据")).to_have_count(1)
    assert len(scenario.conversation["plans"]) == 1
    assert not any("confirm" in path for _, path in scenario.calls)
    page.reload()
    expect(page.locator("#conversation-select")).to_have_value(CID)
    expect(page.locator(".live-message .progress-current")).to_have_text("每日新闻 · 审查内容与配图")
    scenario.phase = "completed"
    expect(page.get_by_role("button", name="计划已执行", exact=True)).to_be_disabled(timeout=8000)
    expect(progress.locator(".progress-summary")).to_contain_text("其中 1 条的回读未确认")
    page.screenshot(path=str(ARTIFACTS / "desktop-completed.png"), full_page=True)
    for width in (390, 760):
        page.set_viewport_size({"width": width, "height": 844})
        page.wait_for_function("() => document.querySelector('.sidebar').getBoundingClientRect().right <= 0")
        page.screenshot(path=str(ARTIFACTS / f"mobile-{width}.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Overflow at {width}px"
    page.locator(".live-message .technical-log summary").click()
    expect(page.locator(".live-message .technical-log pre")).to_be_visible()
    assert not errors
    context.close()


def test_run_records_poll_even_without_selected_conversation(browser):
    scenario = Scenario()
    context, page, errors = prepare(browser, scenario, restore=False)
    page.get_by_role("button", name="运行记录", exact=True).click()
    page.locator(".run-table-row").click()
    expect(page.locator(".run-detail .progress-current")).to_contain_text("生成内容与配图")
    scenario.phase = "review"
    expect(page.locator(".run-detail .progress-current")).to_contain_text("审查内容与配图", timeout=8000)
    assert not errors
    context.close()


def test_older_running_backend_remains_usable_without_restarting_worker(browser):
    scenario = Scenario()
    scenario.legacy = True
    context, page, errors = prepare(browser, scenario)
    expect(page.locator(".live-message .progress-current")).to_have_text("生成配图")
    page.get_by_role("button", name="询问进度", exact=True).click()
    page.locator("#prompt").fill("进度如何？")
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page.locator("#prompt")).to_have_value("")
    assert not any(path.endswith("/messages") for _, path in scenario.calls)
    assert not errors
    context.close()
