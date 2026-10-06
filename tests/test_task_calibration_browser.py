"""Calibration UI contracts in an isolated headless browser, with no model or platform writes."""

from copy import deepcopy
import json
import time

from playwright.sync_api import expect

from test_progress_browser import CID, Scenario, browser, prepare


class CalibrationScenario(Scenario):
    def __init__(self):
        super().__init__()
        self.phase = "running"
        self.plan.update(status="ready", source_message_id="f" * 32, recognition_source="rules",
                         delivery="generate_only", performance_mode="speed", image_score_required=False,
                         model_roles={"agent": "minimax:MiniMax-M3", "writer": "minimax:MiniMax-M3", "image": "opencodex:gpt-image-2"})
        self.plan.pop("job_id")
        self.plan["jobs"][0].update(keywords=["伊朗", "关税", "芯片"], prompt="伊朗 关税 芯片")
        self.conversation.update(title="任务校准测试", status="planned", runs=[], task_recognitions=[])
        self.conversation["messages"][0]["id"] = "f" * 32
        self.conversation["messages"][1]["plan_id"] = self.plan["id"]
        self.record = {"id": "1" * 32, "base_plan_id": self.plan["id"], "base_plan_version": 1,
                       "source_message_id": "f" * 32, "status": "running", "started_at": time.time(),
                       "provider": "minimax", "model": "MiniMax-M3", "error": "", "candidate": None, "changes": []}

    def finish(self, status="ready"):
        self.record.update(status=status, elapsed_seconds=6.1)
        if status == "failed":
            self.record["error"] = "TASK_LLM_TIMEOUT：当前计划保留，可稍后手动重试"
            return
        candidate = deepcopy(self.plan)
        candidate.update(recognition_source="llm", assistant_summary="保留伊朗、关税、芯片关键词，仅生成本地稿",
                         unresolved_requirements=[], executable=True)
        candidate["jobs"][0]["topic_brief"] = "优先筛选有新进展的国际争议"
        self.record["candidate"] = candidate

    def route(self, route):
        request = route.request
        path = request.url.split("8786", 1)[1]
        root = f"/api/conversations/{CID}/task-recognitions"
        if path.startswith(root):
            self.calls.append((request.method, path))
            if path == root:
                assert request.method == "POST"
                assert request.headers["idempotency-key"]
                assert request.post_data_json["source_message_id"] == "f" * 32
                self.conversation["task_recognitions"] = [self.record]
                data = self.record
            elif path.endswith("/adopt"):
                adopted = deepcopy(self.record["candidate"])
                adopted.update(id="2" * 32, version=2)
                self.conversation["plans"].append(adopted)
                self.record["status"] = "adopted"
                data = {"plan": adopted}
            elif path.endswith("/discard"):
                self.record["status"] = "discarded"
                data = self.record
            else:
                data = self.record
            route.fulfill(status=200, content_type="application/json", body=json.dumps(data, ensure_ascii=False))
            return
        if path == "/api/runs":
            self.calls.append((request.method, path))
            route.fulfill(status=200, content_type="application/json", body='{"rows":[]}')
            return
        super().route(route)


def test_calibration_click_compare_adopt_reload_and_responsive_layout(browser, tmp_path):
    scenario = CalibrationScenario()
    context, page, errors = prepare(browser, scenario)
    expect(page.locator(".plan-keywords")).to_contain_text("伊朗、关税、芯片")
    expect(page.get_by_text("识别来源：本地规则", exact=True)).to_be_visible()
    page.get_by_role("button", name="大模型校准", exact=True).click()
    expect(page.get_by_role("button", name="校准中", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="确认并执行", exact=True)).to_be_disabled()
    root = f"/api/conversations/{CID}/task-recognitions"
    assert scenario.calls.count(("POST", root)) == 1
    scenario.finish()
    expect(page.get_by_role("heading", name="校准候选 6.1 秒")).to_be_visible()
    expect(page.locator(".calibration-comparison")).to_contain_text("优先筛选有新进展的国际争议")
    expect(page.locator(".calibration-options")).to_contain_text("主控模型")
    page.screenshot(path=str(tmp_path / "calibration-desktop.png"), full_page=True)
    for width in (390, 760):
        page.set_viewport_size({"width": width, "height": 844})
        page.wait_for_function("() => document.querySelector('.sidebar').getBoundingClientRect().right <= 0")
        page.screenshot(path=str(tmp_path / f"calibration-mobile-{width}.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
    page.get_by_role("button", name="采用校准计划", exact=True).click()
    expect(page.get_by_text("识别来源：大模型校准", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="确认并执行", exact=True)).to_be_enabled()
    page.reload()
    expect(page.get_by_text("识别来源：大模型校准", exact=True)).to_be_visible()
    assert scenario.calls.count(("POST", root)) == 1
    assert not any("/confirm" in path for _, path in scenario.calls)
    assert not errors
    context.close()


def test_restored_candidate_is_not_called_again_and_can_be_discarded(browser):
    scenario = CalibrationScenario()
    scenario.finish()
    scenario.conversation["task_recognitions"] = [scenario.record]
    context, page, errors = prepare(browser, scenario)
    expect(page.get_by_role("button", name="确认并执行", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="采用校准计划", exact=True)).to_be_enabled()
    page.reload()
    expect(page.get_by_role("button", name="保留当前计划", exact=True)).to_be_visible()
    page.get_by_role("button", name="保留当前计划", exact=True).click()
    expect(page.get_by_role("button", name="确认并执行", exact=True)).to_be_enabled()
    expect(page.get_by_text("识别来源：本地规则", exact=True)).to_be_visible()
    assert not any(method == "POST" and path.endswith("task-recognitions") for method, path in scenario.calls)
    assert len(scenario.conversation["plans"]) == 1
    assert not errors
    context.close()


def test_failed_or_unresolved_calibration_keeps_original_plan(browser):
    scenario = CalibrationScenario()
    scenario.finish("failed")
    scenario.conversation["task_recognitions"] = [scenario.record]
    context, page, errors = prepare(browser, scenario)
    expect(page.get_by_role("alert")).to_contain_text("当前计划保留")
    expect(page.get_by_role("button", name="大模型校准", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="确认并执行", exact=True)).to_be_enabled()
    scenario.finish("needs_input")
    scenario.record["error"] = ""
    scenario.record["candidate"].update(executable=False, unresolved_requirements=["需要明确平台"])
    page.reload()
    expect(page.get_by_role("button", name="采用校准计划", exact=True)).to_be_disabled()
    expect(page.locator(".calibration-issues")).to_contain_text("需要明确平台")
    assert not errors
    context.close()
