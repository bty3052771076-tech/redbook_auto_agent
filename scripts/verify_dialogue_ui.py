"""Isolated browser contract check; every API request is intercepted, never proxied.

Run with an existing Playwright Python and a separate Vite server:
  python scripts/verify_dialogue_ui.py http://127.0.0.1:5187
Screenshots go to data/dialogue-ui-test, never to the production frontend bundle.
"""
import copy
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


def conversation(cid, title):
    return dict(id=cid, title=title, messages=[], plans=[], runs=[], status="idle")


def snapshot():
    return dict(engine="langchain", state="idle", revision=0, execution_id=None,
                events=[], requests=[], actions=[], messages=[])


class Fixture:
    def __init__(self):
        self.conversations = {"b": conversation("b", "Second conversation")}
        self.snapshots = {"b": snapshot()}
        self.snapshots["b"]["messages"] = [dict(id="b1", role="assistant", content="SECOND_ONLY", created_at=1794915000)]
        self.calls = []
        self.held = []
        self.hold_message = False
        self.hold_poll = False
        self.fail_message = False
        self.stale_request = False
        self.next_message_state = "starting"

    def bump(self, cid="a", **changes):
        value = self.snapshots[cid]
        value.update(changes)
        value["revision"] += 1
        return copy.deepcopy(value)

    def reply(self, route, value, status=200, headers=None):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value), headers=headers)

    def route(self, route):
        request = route.request
        path = urlparse(request.url).path
        body = request.post_data_json if request.post_data else None
        self.calls.append((request.method, path, body, request.headers, time.monotonic()))
        assert request.headers.get("x-workbench") == "1", ("missing workbench header", path)
        if path == "/api/session":
            self.reply(route, {"status": "ready"}, headers={"set-cookie": "redbook_agent=fixture; Path=/; HttpOnly; SameSite=Strict"})
            return
        assert "redbook_agent=fixture" in request.headers.get("cookie", ""), ("missing cookie", path)
        if path == "/api/connections":
            self.reply(route, dict(database={"status": "ready"}, providers={"bindings": {}, "connections": []},
                                   models={"rows": []}, profile_configured=False, profile_login=""))
        elif path == "/api/conversations":
            if request.method == "POST":
                self.conversations["a"] = conversation("a", body["title"])
                self.snapshots["a"] = snapshot()
                self.reply(route, self.conversations["a"])
            else:
                self.reply(route, {"rows": list(self.conversations.values())})
        elif path.startswith("/api/conversations/"):
            parts = path.split("/")
            cid = parts[3]
            if len(parts) == 4:
                self.reply(route, self.conversations[cid])
            elif path.endswith("/assistant"):
                if self.hold_poll and cid == "a":
                    self.hold_poll = False
                    self.held.append((route, copy.deepcopy(self.snapshots[cid])))
                else:
                    self.reply(route, self.snapshots[cid])
            elif path.endswith("/assistant/messages"):
                assert body["client_message_id"] and request.headers.get("idempotency-key")
                if self.fail_message:
                    self.fail_message = False
                    route.abort("failed")
                    return
                value = self.snapshots[cid]
                value["messages"].append(dict(id=body["client_message_id"], role="user", content=body["content"], created_at=1794915001))
                value = self.bump(cid, state=self.next_message_state, execution_id=f"exec-{cid}")
                self.next_message_state = "running"
                if self.hold_message:
                    self.hold_message = False
                    self.held.append((route, value))
                else:
                    self.reply(route, value, status=202)
            elif path.endswith("/assistant/control"):
                assert body["execution_id"] == f"exec-{cid}"
                self.reply(route, self.bump(cid, state="stopping" if body["action"] == "pause" else "cancelled"))
            elif "/assistant/requests/" in path:
                if self.stale_request:
                    self.stale_request = False
                    self.bump(cid, requests=[dict(self.snapshots[cid]["requests"][0], version=4)])
                    self.reply(route, {"detail": {"message": "Request version changed", "code": "stale"}}, status=409)
                    return
                assert body["version"] == self.snapshots[cid]["requests"][0]["version"]
                self.reply(route, self.bump(cid, requests=[], state="idle"))
            else:
                raise AssertionError(f"Unexpected conversation API: {request.method} {path}")
        else:
            raise AssertionError(f"Unexpected API: {request.method} {path}")

    def release(self):
        for route, value in self.held:
            self.reply(route, value, status=202 if route.request.method == "POST" else 200)
        self.held.clear()


def check_readable_results(page, fixture, artifacts):
    title = "今日模型发布：核验后的技术进展"
    post = {"id": "post-readable", "title": title, "body": "RAW_POST_BODY_SHOULD_STAY_COLLAPSED" * 300}
    actions = [
        dict(id="generated", tool="generate_news", status="succeeded", result={"ok": True, "posts": [post], "post_ids": [post["id"]], "gap": False}),
        dict(id="images", tool="create_image", status="succeeded", result={"ok": True, "post_id": post["id"], "assets": [{"path": "image1.png"}, {"path": "image2.png"}]}),
        dict(id="review", tool="review_draft", status="succeeded", result={"passed": False, "post_id": post["id"], "errors": ["原始来源缺少发布时间", {"message": "配图与正文不一致"}]}),
        dict(id="saved", tool="save_draft", status="succeeded", result={"post_ids": [post["id"]], "post_id": post["id"], "receipt": {"title": title}, "platform_receipt_confirmed": True}),
        dict(id="unverified", tool="save_draft", status="succeeded", result={"post_ids": [post["id"]], "post_id": post["id"]}),
        dict(id="uncertain", tool="save_draft", status="uncertain", result={"post_ids": [post["id"]], "error": {"message": "平台提交后未收到可靠回执"}}),
        dict(id="failed", tool="save_draft", status="missing_capability", result={"ok": False, "error": {"code": "PROFILE_NOT_CONFIGURED", "message": "请配置项目专用浏览器后再保存"}}),
        dict(id="empty", tool="generate_digest", status="succeeded", result={"posts": [], "post_ids": [], "gap": True}),
    ]
    events = [
        dict(id="readable-start", kind="tool/started", payload={"tool": "generate_news", "text": "开始调用 generate_news"}, created_at=1794915002),
        dict(id="readable-done", kind="tool/completed", payload={"tool": "generate_news", "status": "succeeded", "text": "generate_news：succeeded"}, created_at=1794915002),
        dict(id="readable-progress", kind="tool/progress", payload={"tool": "news_search", "text": "正在核验来源与日期"}, created_at=1794915002),
        dict(id="readable-json", kind="tool/progress", payload={"tool": "news_search", "text": json.dumps({"posts": [post]})}, created_at=1794915002),
        dict(id="readable-model", kind="model/status", payload={"status": "started"}, created_at=1794915002),
        dict(id="readable-model-done", kind="model/status", payload={"status": "completed", "tool_calls": 1}, created_at=1794915002),
        dict(id="readable-wait", kind="agent/waiting", payload={"text": "需要你确认下一步"}, created_at=1794915002),
        dict(id="readable-unknown", kind="future/unknown_event", payload={"text": "已保留本轮进展"}, created_at=1794915002),
    ]
    fixture.bump(actions=actions, events=events, messages=fixture.snapshots["a"]["messages"] + [dict(
        id="canonical-tool-result", role="tool", created_at=1794915002,
        content=json.dumps({"tool": "generate_news", "status": "succeeded", "posts": [post], "post_ids": [post["id"]]}))])
    page.get_by_role("button", name="工具与产物", exact=True).click()
    results = page.get_by_role("region", name="工具与产物")
    generated = results.locator("article[data-action-id=generated]")
    expect(generated).to_contain_text("生成单篇稿件", timeout=5000)
    expect(generated).to_contain_text("已生成 1 篇稿件")
    expect(generated.locator(".dialogue-result-titles")).to_contain_text(title)
    expect(generated.locator("pre")).not_to_be_visible()
    expect(results.locator("article[data-action-id=images]")).to_contain_text("配图完成 · 2 张")
    expect(results.locator("article[data-action-id=images] .dialogue-result-titles")).to_contain_text(title)
    expect(results.locator("article[data-action-id=review]")).to_contain_text("审查未通过 · 2 项问题")
    expect(results.locator("article[data-action-id=review]")).to_contain_text("原始来源缺少发布时间")
    expect(results.locator("article[data-action-id=review]")).to_contain_text("配图与正文不一致")
    expect(results.locator("article[data-action-id=saved]")).to_contain_text("平台草稿回执已核对 · 完成 1 篇")
    expect(results.locator("article[data-action-id=unverified]")).to_contain_text("暂不计入完成数")
    expect(results.locator("article[data-action-id=uncertain]")).to_contain_text("不计入完成数")
    expect(results.locator("article[data-action-id=uncertain]")).to_contain_text("平台提交后未收到可靠回执")
    expect(results.locator("article[data-action-id=failed]")).to_contain_text("请配置项目专用浏览器后再保存")
    expect(results.locator("article[data-action-id=empty]")).to_contain_text("未生成稿件 · 完成 0 篇")
    page.get_by_role("button", name="对话", exact=True).click()
    expect(page.locator(".message.tool .dialogue-result-summary")).to_have_text("已生成 1 篇稿件")
    expect(page.locator(".message.tool pre")).not_to_be_visible()
    page.get_by_role("button", name="执行动态", exact=True).click()
    timeline = page.get_by_role("region", name="实时事件")
    for label in ["工具开始", "工具完成", "工具进度", "模型调用开始", "模型响应完成", "等待你的确认", "执行状态更新"]:
        expect(timeline.get_by_text(label, exact=True).first).to_be_visible()
    expect(timeline.get_by_text("正在核验来源与日期", exact=True)).to_be_visible()
    visible = timeline.inner_text()
    for raw in ["tool/started", "tool/completed", "tool/progress", "model/status", "agent/waiting", "future/unknown_event", "generate_news", "succeeded", "RAW_POST_BODY_SHOULD_STAY_COLLAPSED"]:
        assert raw not in visible, raw
    page.get_by_role("button", name="工具与产物", exact=True).click()
    generated.scroll_into_view_if_needed()
    page.screenshot(path=str(artifacts / "desktop-readable-results.png"), full_page=True)
    generated.get_by_text("原始结果", exact=True).click()
    expect(generated.locator("pre")).to_contain_text("RAW_POST_BODY_SHOULD_STAY_COLLAPSED")
    generated.get_by_text("原始结果", exact=True).click()
    results.locator("article[data-action-id=review]").scroll_into_view_if_needed()
    page.screenshot(path=str(artifacts / "desktop-readable-errors.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator('.sidebar')).not_to_be_in_viewport()
    generated.scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(artifacts / "mobile-readable-results.png"), full_page=True)
    results.locator("article[data-action-id=review]").scroll_into_view_if_needed()
    page.screenshot(path=str(artifacts / "mobile-readable-errors.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.get_by_role("button", name="对话", exact=True).click()
    print("PASS readable draft titles/counts/errors, retained uncertain state, collapsed full JSON, Chinese event names, desktop/390px", flush=True)


def run(url):
    artifacts = Path(__file__).resolve().parents[1] / "data" / "dialogue-ui-test"
    artifacts.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe")
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        fixture = Fixture()
        context.route("**/api/**", fixture.route)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url)
        page.wait_for_load_state("networkidle")
        expect(page.get_by_role("heading", name="任务对话")).to_be_visible()
        assert not any(call[0] == "POST" and call[1] != "/api/session" for call in fixture.calls)
        page.get_by_label("消息", exact=True).fill("Discuss sources without executing a batch")
        page.get_by_role("button", name="发送", exact=True).click()
        expect(page.locator(".dialogue-feed .message p").filter(has_text="Discuss sources without executing a batch")).to_be_visible()
        expect(page.locator("#dialogue-conversation-select")).to_have_value("a")
        assert len([call for call in fixture.calls if call[:2] == ("POST", "/api/conversations")]) == 1
        print("PASS bootstrap identity, first-send creation, canonical messages, async 202", flush=True)

        expect(page.locator(".dialogue-statusbar")).to_contain_text("正在启动")
        expect(page.get_by_role("button", name="暂停对话", exact=True)).to_be_enabled()
        expect(page.get_by_role("button", name="取消执行", exact=True)).to_be_enabled()
        expect(page.get_by_label("补充要求", exact=True)).to_be_enabled()
        page.screenshot(path=str(artifacts / "desktop-starting.png"), full_page=True)
        fixture.bump(state="running")
        expect(page.locator(".dialogue-statusbar")).to_contain_text("执行中", timeout=5000)
        fixture.next_message_state = "stopping"
        page.get_by_label("补充要求", exact=True).fill("QUEUED_SUPPLEMENT")
        page.get_by_role("button", name="发送", exact=True).click()
        expect(page.locator(".dialogue-statusbar")).to_contain_text("正在停止")
        expect(page.get_by_label("补充要求", exact=True)).to_have_value("")
        expect(page.get_by_text("QUEUED_SUPPLEMENT", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="暂停对话", exact=True)).to_be_enabled()
        expect(page.get_by_role("button", name="取消执行", exact=True)).to_be_enabled()
        fixture.bump(state="running")
        expect(page.locator(".dialogue-statusbar")).to_contain_text("执行中", timeout=5000)
        queued = [call for call in fixture.calls if call[1].endswith("/assistant/messages") and call[2]["content"] == "QUEUED_SUPPLEMENT"]
        assert len(queued) == 1
        assert not any(call[1].endswith("/assistant/control") for call in fixture.calls)
        print("PASS starting active controls and composer, 202 stopping supplement accepted without resubmission", flush=True)

        check_readable_results(page, fixture, artifacts)

        request = dict(id="permission", kind="approval", tool="draft.generate", arguments={"source_id": "source-123", "draft_id": "draft-123"},
                       reason="Generate one verified draft", allowed_decisions=["approve_once", "approve_session", "deny", "configured"], version=3)
        fixture.bump(messages=fixture.snapshots["a"]["messages"] + [dict(id="model1", role="assistant", content="PUBLIC_MODEL_REPLY", created_at=1794915002)],
                     events=[dict(id="e1", kind="tool_started", payload={"tool": "source.read", "text": "PUBLIC_TOOL_EVENT", "reasoning": "PRIVATE_THOUGHT"}, created_at=1794915002),
                             dict(id="e2", kind="reasoning", payload={"message": "PRIVATE_THOUGHT"}, created_at=1794915002)],
                     actions=[dict(id="action1", tool="source.read", status="succeeded", result={"title": "VERIFIED_RESULT"})],
                     requests=[request], state="waiting_approval")
        expect(page.get_by_text("PUBLIC_MODEL_REPLY", exact=True)).to_be_visible(timeout=5000)
        page.get_by_role("button", name="执行动态", exact=True).click()
        expect(page.get_by_text("PUBLIC_TOOL_EVENT", exact=True)).to_be_visible()
        assert page.get_by_text("PRIVATE_THOUGHT").count() == 0
        page.get_by_role("button", name="工具与产物", exact=True).click()
        expect(page.get_by_text("VERIFIED_RESULT", exact=True)).to_be_visible()
        page.get_by_role("button", name="处理 1 项请求").click()
        expect(page.get_by_role("region", name="申请：draft.generate")).to_be_visible()
        fixture_posts = lambda: [call for call in fixture.calls if "/assistant/requests/" in call[1]]
        assert not fixture_posts()
        count = page.get_by_label("本会话最多调用次数")
        for invalid in ["0", "21", "1.5", ""]:
            count.fill(invalid)
            expect(page.get_by_role("button", name="授权本会话", exact=False)).to_be_disabled()
        count.fill("20")
        page.screenshot(path=str(artifacts / "desktop-approval.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator('.sidebar')).not_to_be_in_viewport()
        page.get_by_text("参数与操作范围").click()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "mobile horizontal overflow"
        page.screenshot(path=str(artifacts / "mobile-approval.png"), full_page=True)
        page.get_by_role("button", name="拒绝（0 次执行）", exact=True).click()
        expect(page.get_by_text("本次申请不授权执行", exact=False)).to_be_visible()
        assert fixture_posts()[-1][2] == {"decision": "deny", "version": 3}
        assert not fixture.snapshots["a"]["requests"]
        print("PASS public events, tool results, private-chain exclusion, bounded permission, deny", flush=True)

        fixture.bump(requests=[request], state="waiting_approval")
        expect(count).to_be_visible(timeout=5000)
        count.fill("20")
        page.get_by_role("button", name="授权本会话 20 次", exact=True).click()
        expect(count).to_have_count(0)
        assert fixture_posts()[-1][2] == {"decision": "approve_session", "version": 3, "max_calls": 20}
        fixture.bump(requests=[request], state="waiting_approval")
        expect(page.get_by_role("button", name="允许本次", exact=True)).to_be_visible(timeout=5000)
        fixture.stale_request = True
        page.get_by_role("button", name="允许本次", exact=True).click()
        expect(page.locator(".dialogue-alert").filter(has_text="Request version changed")).to_be_visible()
        page.get_by_role("button", name="允许本次", exact=True).click()
        expect(page.get_by_role("button", name="允许本次", exact=True)).to_have_count(0)
        assert fixture_posts()[-1][2] == {"decision": "approve_once", "version": 4}

        fixture.bump(requests=[dict(request, id="answer", kind="input", allowed_decisions=["answer"], version=5)], state="waiting_input")
        expect(page.get_by_role("button", name="提交回答", exact=True)).to_be_disabled(timeout=5000)
        page.get_by_label("补充信息", exact=True).fill("Only verified sources")
        page.get_by_role("button", name="提交回答", exact=True).click()
        expect(page.get_by_role("button", name="提交回答", exact=True)).to_have_count(0)
        assert fixture_posts()[-1][2] == {"decision": "answer", "version": 5, "answer": "Only verified sources"}
        fixture.bump(requests=[dict(request, id="configured", allowed_decisions=["configured"], version=6)], state="waiting_input")
        expect(page.get_by_role("button", name="已配置，重新检查", exact=True)).to_be_visible(timeout=5000)
        page.get_by_role("button", name="已配置，重新检查", exact=True).click()
        expect(page.get_by_role("button", name="已配置，重新检查", exact=True)).to_have_count(0)
        assert fixture_posts()[-1][2] == {"decision": "configured", "version": 6}
        print("PASS approve once/session, stale version recovery, answer and configured decisions", flush=True)

        fixture.hold_message = True
        page.get_by_label("消息", exact=True).fill("Supplement during execution")
        page.get_by_role("button", name="发送", exact=True).click()
        expect(page.get_by_role("button", name="发送", exact=True)).to_be_disabled()
        page.get_by_role("button", name="暂停对话", exact=True).click()
        expect(page.get_by_text("等待执行进程确认停止。", exact=True)).to_be_visible()
        fixture.bump(state="paused")
        expect(page.get_by_role("button", name="继续对话", exact=True)).to_be_visible(timeout=5000)
        fixture.release()
        expect(page.get_by_role("button", name="发送", exact=True)).to_be_disabled()  # accepted draft cleared
        expect(page.get_by_role("button", name="继续对话", exact=True)).to_be_enabled()
        page.get_by_role("button", name="继续对话", exact=True).click()
        expect(page.get_by_role("button", name="继续对话", exact=True)).to_have_count(0)
        fixture.hold_message = True
        page.get_by_label("补充要求", exact=True).fill("Another supplement")
        page.get_by_role("button", name="发送", exact=True).click()
        page.get_by_role("button", name="取消执行", exact=True).click()
        expect(page.get_by_text("本次执行已取消，已有稿件与回执保留。", exact=True)).to_be_visible()
        fixture.release()
        expect(page.get_by_role("button", name="继续对话", exact=True)).to_have_count(0)
        assert fixture.snapshots["a"]["state"] == "cancelled"
        print("PASS immediate independent pause/cancel during pending send, stopping, resume, stale revision", flush=True)

        fixture.fail_message = True
        page.get_by_label("消息", exact=True).fill("Retry with same identity")
        page.get_by_role("button", name="发送", exact=True).click()
        expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
        page.get_by_role("button", name="发送", exact=True).click()
        expect(page.get_by_role("button", name="发送", exact=True)).to_be_disabled()
        attempts = [call for call in fixture.calls if call[1].endswith("/assistant/messages") and call[2]["content"] == "Retry with same identity"]
        assert len(attempts) == 2 and attempts[0][2] == attempts[1][2]
        assert attempts[0][3]["idempotency-key"] == attempts[1][3]["idempotency-key"]

        fixture.hold_poll = True
        page.get_by_role("button", name="刷新状态", exact=True).click()
        fixture.hold_message = True
        page.get_by_label("补充要求", exact=True).fill("LATE_A_RESPONSE")
        page.get_by_role("button", name="发送", exact=True).click()
        page.locator("#dialogue-conversation-select").select_option("b")
        expect(page.get_by_text("SECOND_ONLY", exact=True)).to_be_visible()
        fixture.release()
        page.wait_for_timeout(1200)
        assert page.get_by_text("LATE_A_RESPONSE", exact=True).count() == 0
        assert page.get_by_text("PUBLIC_MODEL_REPLY", exact=True).count() == 0
        before = len([call for call in fixture.calls if call[0] == "POST" and call[1] != "/api/session"])
        page.reload()
        expect(page.get_by_text("SECOND_ONLY", exact=True)).to_be_visible()
        assert len([call for call in fixture.calls if call[0] == "POST" and call[1] != "/api/session"]) == before
        print("PASS message retry idempotency, late A poll/send isolation, refresh read-only", flush=True)

        page.set_viewport_size({"width": 1440, "height": 1000})
        page.get_by_role("button", name="人工计划", exact=True).last.click()
        expect(page.get_by_role("heading", name="人工计划", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="本次计划", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="连接与模型", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="能力中心", exact=True)).to_be_visible()
        page.get_by_role("button", name="对话任务", exact=True).click()
        fixture.bump("b", state="error", events=[dict(id="err", kind="error", payload={"message": "MODEL_CONFIGURATION_REQUIRED"}, created_at=1794915010)])
        page.get_by_role("button", name="执行动态", exact=True).click()
        expect(page.get_by_text("MODEL_CONFIGURATION_REQUIRED", exact=True)).to_be_visible(timeout=5000)
        expect(page.get_by_role("button", name="检查模型", exact=True)).to_be_visible()
        page.screenshot(path=str(artifacts / "desktop-error.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator('.sidebar')).not_to_be_in_viewport()
        page.screenshot(path=str(artifacts / "mobile-error.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        forbidden = [call for call in fixture.calls if call[0] == "POST" and not (
            call[1] in ["/api/session", "/api/conversations"] or "/assistant/" in call[1])]
        assert not forbidden, forbidden
        assert not errors, errors
        polls = [call[4] for call in fixture.calls if call[:2] == ("GET", "/api/conversations/b/assistant")]
        assert len(polls) >= 2
        print("PASS manual-plan preservation, error configuration links, desktop/390px, zero old-run writes, no JS errors", flush=True)
        print(f"Screenshots: {artifacts}", flush=True)
        browser.close()


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5187")
