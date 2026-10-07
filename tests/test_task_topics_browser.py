"""Show actual local parser output in the sidebar, without starting a worker."""

from playwright.sync_api import expect

from test_task_calibration import workbench
from test_task_calibration_browser import CalibrationScenario
from test_progress_browser import browser, prepare


def test_natural_topic_sidebar_uses_actual_parser_and_survives_reload(browser, workbench, tmp_path):
    text = "生成10条每日新闻，1条每日AI资讯，至少包含一条女性权益新闻"
    scenario = CalibrationScenario()
    scenario.plan.update(workbench._parse_agent_message(text))
    scenario.plan.update(status="ready", source_message_id="f" * 32)
    scenario.conversation["messages"][0]["content"] = text
    scenario.conversation["messages"][1]["content"] = scenario.plan["assistant_summary"]
    context, page, errors = prepare(browser, scenario)
    sidebar = page.locator(".plan-panel")
    if sidebar.count() == 0:
        sidebar = page.locator(".plan-list").first
    news = sidebar.locator("li").filter(has_text="每日新闻")
    ai = sidebar.locator("li").filter(has_text="每日AI讯息")
    expect(news.locator(".plan-keywords")).to_have_text("选题偏向女性权益")
    expect(news.locator(".plan-topic")).to_have_text("至少包含一条女性权益新闻")
    expect(ai.locator(".plan-keywords")).to_have_count(0)
    page.screenshot(path=str(tmp_path / "natural-topic-desktop.png"), full_page=True)
    page.reload()
    expect(page.locator(".plan-topic").first).to_have_text("至少包含一条女性权益新闻")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function("() => document.querySelector('.sidebar').getBoundingClientRect().right <= 0")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / "natural-topic-mobile.png"), full_page=True)
    assert not any("/confirm" in path for _, path in scenario.calls)
    assert not errors
    context.close()
