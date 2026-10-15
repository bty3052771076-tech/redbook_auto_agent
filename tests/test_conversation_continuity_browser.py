"""Continuity GUI contracts. All HTTP is intercepted; no live service or model."""

from copy import deepcopy
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from test_progress_browser import browser  # Existing headless Chrome and E: TEMP.


CID, RID, GID, TID = "d" * 32, "a" * 32, "g" * 32, "t" * 32
BASE = "http://127.0.0.1:19876"


class ContinuityScenario:
    def __init__(self):
        self.calls = []
        self.messages = []
        self.confirmations = []
        self.revisions = []
        self.fail_message_once = False
        self.conflict = False
        self.plan = {
            "id": "e" * 32, "version": 1, "status": "running", "executable": True,
            "job_id": RID, "semantic_hash": "original-hash", "source_message_id": "u",
            "jobs": [{"job_id": "news", "kind": "daily_news", "title": "每日新闻", "count": 10,
                      "search_keywords": ["国际"], "topic_preferences": []}],
            "delivery": "save_draft", "platform": "xhs", "assistant_summary": "每日新闻10条",
        }
        self.goal = {
            "goal_id": GID, "revision": 3, "ledger_revision": 8, "state": "paused",
            "active_plan_id": self.plan["id"], "active_attempt_id": RID, "plan": self.plan,
            "jobs": [{"job_id": "news", "title": "每日新闻", "requested": 10,
                      "delivered_verified": 7, "approved_current": 2, "retained_ready": 2,
                      "uncertain": 1, "remaining": 3, "new_generation_needed": 1}],
        }
        self.conversation = {
            "id": CID, "title": "持续采编测试", "status": "running", "conversation_revision": 4,
            "plans": [self.plan], "runs": [RID], "goals": [self.goal], "turns": [],
            "messages": [{"id": "u", "role": "user", "content": "生成10条每日新闻", "created_at": 1}],
        }
        self.context = {
            "status": "ready", "model": "offline-controller", "raw_message_count": 28,
            "tokens_estimate": 1500, "policy": {"mode": "auto"}, "context": {},
            "coverage": {"summary_through_seq": 20, "retained_sequences": [21, 22]},
            "snapshots": [], "actual_usage": None,
        }

    def turn(self, kind="continue_goal", state="accepted"):
        turn = {"turn_id": TID, "state": state, "kind": kind, "goal_id": GID,
                "proposal_plan_id": None, "command_payload": {}, "result": {}}
        self.conversation["turns"] = [turn]
        return turn

    def propose(self):
        candidate = deepcopy(self.plan)
        candidate.pop("job_id")
        candidate.update(id="f" * 32, version=2, status="draft", semantic_hash="candidate-hash")
        candidate["jobs"][0]["search_keywords"] = ["科技"]
        candidate["image_score_required"] = False
        candidate["continuation"] = {
            "goal_id": GID, "turn_id": TID, "expected_goal_revision": 3,
            "base_plan_id": self.plan["id"], "scope": "remaining",
            "diff": [{"field": "jobs.news.search_keywords", "before": ["国际"], "after": ["科技"]},
                     {"field": "image_score_required", "before": True, "after": False}],
        }
        self.conversation["plans"].append(candidate)
        self.turn("amend_goal", "awaiting_confirmation")["proposal_plan_id"] = candidate["id"]
        return candidate

    def route(self, route):
        req = route.request
        path = urlsplit(req.url).path
        self.calls.append((req.method, path))
        status = 200
        if path == "/api/session":
            data = {"status": "ready"}
        elif path == "/api/conversations":
            data = {"rows": [self.conversation]}
        elif path == f"/api/conversations/{CID}":
            data = self.conversation
        elif path.endswith("/goals"):
            data = {"rows": self.conversation["goals"]}
        elif path.endswith("/context"):
            data = self.context
        elif path.endswith("/messages"):
            body = req.post_data_json
            self.messages.append((body, req.headers.get("idempotency-key")))
            if self.fail_message_once:
                self.fail_message_once = False
                route.abort("connectionfailed")
                return
            kind = "status" if "进度" in body["content"] else "continue_goal"
            if "暂停" in body["content"]:
                kind = "pause_goal"
            if "取消" in body["content"]:
                kind = "cancel_goal"
            if "修改" in body["content"]:
                self.propose()
                kind = "amend_goal"
            elif len(self.conversation["goals"]) > 1 and not body.get("goal_id"):
                kind = "clarify"
                self.turn(kind, "needs_input")["goal_id"] = None
            elif kind != "status":
                self.turn(kind)
            assistant = {"id": f"reply-{len(self.messages)}", "role": "assistant", "content": "请求已收到", "created_at": 2}
            message = {"id": body.get("client_message_id", "user"), "role": "user", "content": body["content"], "created_at": 2}
            self.conversation["messages"].extend([message, assistant])
            data = {"message": message, "assistant": assistant, "plan": self.conversation["plans"][-1] if kind == "amend_goal" else None,
                    "run": None, "action": kind, "goal": self.goal if kind != "clarify" else None,
                    "turn": self.conversation["turns"][-1] if kind != "status" else None}
            status = 202
        elif path.endswith(f"/turns/{TID}/confirm"):
            self.confirmations.append((req.post_data_json, req.headers.get("idempotency-key")))
            if self.conflict:
                status, data = 409, {"detail": {"code": "GOAL_REVISION_CONFLICT", "message": "目标修订已变化，请核对候选"}}
            else:
                data = {"turn": {**self.conversation["turns"][-1], "state": "queued"},
                        "action": "amend_goal", "goal": self.goal, "plan": None, "run": None}
                self.conversation["turns"] = [data["turn"]]
                status = 202
        elif path.endswith(f"/turns/{TID}/discard"):
            turn = {**self.conversation['turns'][-1], 'state': 'cancelled'}
            self.conversation['turns'] = [turn]
            data = {'turn': turn, 'goal': self.goal, 'plan': None, 'run': None, 'action': 'discard_amendment'}
        elif path.endswith("/revisions"):
            self.revisions.append(req.post_data_json)
            candidate = deepcopy(self.conversation["plans"][-1])
            fields = req.post_data_json["editable_fields"]
            for patch in fields.get("jobs", []):
                candidate["jobs"][0].update({k: v for k, v in patch.items() if k != "target_job_id"})
            candidate.update(id="9" * 32, version=3, semantic_hash="edited-candidate-hash")
            self.conversation["plans"].append(candidate)
            data = {"plan": candidate}
        elif path == "/api/runs" or path == "/api/drafts":
            data = {"rows": []}
        elif path in {f"/api/runs/{RID}", "/api/runs/other-attempt"}:
            data = {"id": path.rsplit('/', 1)[-1], "status": "running", "events": [], "stage": "generate"}
        elif path == "/api/connections":
            data = {"database": {"status": "ready"}, "providers": {"bindings": {}, "connections": []}, "models": {"rows": []}}
        elif path.endswith("/capabilities"):
            data = {"version": 1, "skill_mode": "off", "skill_names": [], "disabled_tools": [],
                    "tools": [], "skills": [], "memory": {}, "readiness": {"ready": True}, "calls": []}
        elif path == "/api/capabilities/overview":
            data = {}
        else:
            pytest.fail(f"Unexpected API call: {req.method} {path}")
        route.fulfill(status=status, content_type="application/json", body=json.dumps(data, ensure_ascii=False))


def prepare(browser, scenario):
    context = browser.new_context(viewport={"width": 1366, "height": 900})
    dist = Path(__file__).resolve().parents[1] / "frontend/dist"

    def frontend(route):
        path = urlsplit(route.request.url).path
        asset = (dist / path.lstrip("/")).resolve() if path != "/" else dist / "index.html"
        assert asset.is_relative_to(dist) and asset.is_file(), path
        route.fulfill(content_type=mimetypes.guess_type(str(asset))[0] or "application/octet-stream", body=asset.read_bytes())

    context.route("**/*", frontend)
    context.route("**/api/**", scenario.route)
    context.add_init_script(f"localStorage.setItem('agent-conversation', '{CID}')")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE)
    page.wait_for_load_state("networkidle")
    expect(page.locator("#conversation-select")).to_have_value(CID)
    return context, page, errors


def test_continue_keeps_plan_counts_and_pending_turn_survive_reopen(browser, tmp_path):
    scenario = ContinuityScenario()
    context, page, errors = prepare(browser, scenario)
    expect(page.get_by_label("目标实际进度")).to_contain_text("已交付 7 / 10")
    expect(page.get_by_label("目标实际进度")).to_contain_text("需新生成 1")
    expect(page.get_by_label("目标实际进度")).to_contain_text("结果待核验 1")
    page.get_by_role("button", name="继续目标", exact=True).dblclick()
    expect(page.get_by_label("对话指令状态")).to_contain_text("已接收")
    expect(page.get_by_role("button", name="继续目标", exact=True)).to_be_disabled()
    expect(page.locator(".plan-pane .plan-list")).to_contain_text("10 条")
    assert scenario.messages[0][0]["expected_goal_revision"] == 3
    assert scenario.messages[0][0]["expected_attempt_id"] == RID
    assert scenario.messages[0][0]["goal_id"] == GID
    assert scenario.messages[0][0]["client_message_id"]
    page.get_by_role("button", name="询问进度", exact=True).click()
    expect(page.locator("#prompt")).to_be_editable()
    scenario.conversation["turns"][-1]["state"] = "queued"
    page.reload()
    expect(page.get_by_label("对话指令状态")).to_contain_text("待生效")
    page.close()
    page = context.new_page()
    page.goto(BASE)
    expect(page.get_by_label("对话指令状态")).to_contain_text("待生效")
    scenario.conversation["turns"][-1].update(state="applied", result={"revision": 4, "applied_at": 1792022400})
    expect(page.get_by_label("对话指令状态")).to_contain_text("已应用", timeout=8000)
    expect(page.get_by_label("对话指令状态")).to_contain_text("修订 4")
    assert sum(b["content"] == "继续" for b, _ in scenario.messages) == 1
    assert not any("/confirm" in path or "/resume" in path for _, path in scenario.calls)
    page.screenshot(path=str(tmp_path / "continuity-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(tmp_path / "continuity-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors
    context.close()


def test_generate_only_goal_counts_approvals_not_platform_deliveries(browser):
    scenario = ContinuityScenario()
    scenario.plan['delivery'] = 'generate_only'
    scenario.goal['state'] = 'completed'
    scenario.goal['jobs'][0].update(approved_current=10, delivered_verified=0,
        retained_ready=10, remaining=0, new_generation_needed=0, uncertain=0)
    context, page, errors = prepare(browser, scenario)
    progress = page.get_by_label('目标实际进度')
    expect(progress).to_contain_text('已完成 10 / 10')
    expect(progress).not_to_contain_text('已交付 0 / 10')
    expect(progress).to_contain_text('保留可复用 10')
    assert not errors
    context.close()


def test_edit_remaining_candidate_confirm_once_with_current_hash(browser):
    scenario = ContinuityScenario()
    scenario.propose()
    context, page, errors = prepare(browser, scenario)
    diff = page.get_by_label("剩余任务修改")
    expect(diff).to_contain_text("仅剩余任务")
    expect(diff).to_contain_text("国际")
    expect(diff).to_contain_text("科技")
    diff.get_by_role("button", name="编辑候选", exact=True).click()
    panel = page.get_by_role("dialog", name="编辑剩余任务候选")
    panel.get_by_label("每日新闻篇数").fill("9")
    panel.get_by_role("button", name="保存候选", exact=True).click()
    expect(panel).not_to_be_visible()
    assert not scenario.confirmations
    page.reload()
    expect(page.locator('.plan-pane .plan-list')).to_contain_text('9 条')
    diff.get_by_role("button", name="确认修改并继续", exact=True).dblclick()
    expect(page.get_by_label("对话指令状态")).to_contain_text("待生效")
    expect(diff.get_by_role("button", name="确认修改并继续", exact=True)).to_be_disabled()
    page.reload()
    expect(page.get_by_role("button", name="确认修改并继续", exact=True)).to_be_disabled()
    assert len(scenario.confirmations) == 1
    body, key = scenario.confirmations[0]
    assert body == {"expected_goal_revision": 3, "proposal_hash": "edited-candidate-hash"}
    assert key
    assert not any(path.startswith("/api/plans/") and path.endswith("/confirm") for _, path in scenario.calls)
    assert not errors
    context.close()


def test_unstarted_goal_omits_empty_attempt_in_messages(browser):
    scenario = ContinuityScenario()
    scenario.goal.update(state='planned', active_attempt_id='')
    scenario.plan.pop('job_id')
    scenario.plan['status'] = 'draft'
    scenario.conversation['runs'] = []
    context, page, errors = prepare(browser, scenario)
    page.get_by_role('button', name='询问进度', exact=True).click()
    expect(page.locator('#prompt')).to_be_editable()
    assert len(scenario.messages) == 1
    assert 'expected_attempt_id' not in scenario.messages[0][0]
    assert not errors
    context.close()


def test_discard_candidate_keeps_active_plan_without_execution(browser):
    scenario = ContinuityScenario()
    scenario.propose()
    context, page, errors = prepare(browser, scenario)
    page.get_by_role("button", name="放弃候选", exact=True).click()
    expect(page.get_by_label("剩余任务修改")).not_to_be_visible()
    expect(page.locator(".plan-pane .plan-list")).to_contain_text("国际")
    assert not scenario.confirmations
    assert not errors
    context.close()


def test_multiple_goals_require_explicit_target_and_never_guess(browser):
    scenario = ContinuityScenario()
    other = deepcopy(scenario.goal)
    other.update(goal_id="h" * 32, active_attempt_id="other-attempt", revision=5)
    other["jobs"][0]["title"] = "每日AI讯息"
    scenario.conversation["goals"].append(other)
    context, page, errors = prepare(browser, scenario)
    expect(page.get_by_label("当前目标", exact=True)).to_have_value("")
    page.locator("#prompt").fill("继续")
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page.get_by_label("对话指令状态")).to_contain_text("需补充")
    assert scenario.messages[-1][0]["goal_id"] is None
    page.get_by_label("当前目标", exact=True).select_option(other["goal_id"])
    page.get_by_role("button", name="继续目标", exact=True).click()
    expect(page.locator("#prompt")).to_have_value("")
    assert scenario.messages[-1][0]["goal_id"] == other["goal_id"]
    assert scenario.messages[-1][0]["expected_goal_revision"] == 5
    assert scenario.messages[-1][0]["expected_attempt_id"] == "other-attempt"
    assert not errors
    context.close()


def test_message_retry_reuses_identity_and_confirmation_conflict_never_executes(browser):
    scenario = ContinuityScenario()
    scenario.fail_message_once = True
    context, page, errors = prepare(browser, scenario)
    page.locator("#prompt").fill("继续")
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page.get_by_role("alert")).to_be_visible()
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page.get_by_label("对话指令状态")).to_contain_text("已接收")
    assert scenario.messages[0] == scenario.messages[1]
    scenario.propose()
    scenario.conflict = True
    page.reload()
    page.get_by_role("button", name="确认修改并继续", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("目标修订已变化")
    expect(page.get_by_label("剩余任务修改")).to_be_visible()
    assert len(scenario.confirmations) == 1
    assert not errors
    context.close()


@pytest.mark.parametrize("state,label", [("running", "压缩中"), ("succeeded", "压缩完成"), ("failed", "压缩失败")])
def test_compaction_events_come_from_context_with_coverage_and_token_provenance(browser, state, label):
    scenario = ContinuityScenario()
    scenario.context["compaction_events"] = [{"operation_id": "compact-1", "status": state, "through_seq": 20,
                                              "input_tokens": 3200, "output_tokens": 600, "estimated": True}]
    context, page, errors = prepare(browser, scenario)
    expect(page.get_by_label("上下文压缩事件")).to_contain_text(label)
    if state != "succeeded":
        expect(page.get_by_label("上下文压缩事件")).not_to_contain_text("压缩完成")
    page.get_by_role("link", name="查看上下文", exact=True).click()
    expect(page.get_by_label("上下文压缩事件")).to_contain_text("20")
    expect(page.get_by_label("上下文压缩事件")).to_contain_text("估算")
    expect(page.get_by_label("上下文压缩事件")).to_contain_text("3200")
    scenario.context["actual_usage"] = {"requests": [{"request_id": "real-request-1", "input_tokens": 2900, "estimated": False}]}
    page.get_by_role("button", name="刷新状态", exact=True).click()
    page.get_by_text("查看本次实际上下文", exact=True).click()
    expect(page.locator(".cap-context-columns").locator("..")).to_contain_text("real-request-1")
    assert not any(method == "POST" and path not in {"/api/session"} for method, path in scenario.calls)
    assert not errors
    context.close()
