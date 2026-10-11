from playwright.sync_api import expect
from test_progress_browser import browser, prepare
from test_plan_editor_browser import EditableScenario
from test_progress_browser import Scenario


def test_pipeline_choice_can_be_saved_without_starting_or_overflow(browser,tmp_path):
    scenario=EditableScenario()
    context,page,errors=prepare(browser,scenario)
    page.get_by_role("button",name="编辑计划",exact=True).click()
    panel=page.get_by_role("dialog",name="编辑本次计划")
    panel.get_by_role("radio",name="阶段复用（试用）",exact=True).check()
    panel.get_by_role("radio",name="阶段复用（试用）",exact=True).scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path/"pipeline-desktop.png"),full_page=True)
    page.set_viewport_size({"width":390,"height":844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    expect(panel.get_by_role("radio",name="阶段复用（试用）",exact=True)).to_be_checked()
    panel.get_by_role("radio",name="阶段复用（试用）",exact=True).scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path/"pipeline-mobile.png"),full_page=True)
    panel.get_by_role("button",name="保存计划",exact=True).click()
    expect(panel).not_to_be_visible()
    assert scenario.saved_payload["editable_fields"]["generation_pipeline_version"]=="staged_v2"
    assert not any("/confirm" in path for _,path in scenario.calls)
    assert not errors
    context.close()


def test_pipeline_counts_fit_desktop_and_mobile(browser, tmp_path):
    class PipelineScenario(Scenario):
        def run(self):
            value = super().run()
            value["activity"]["pipeline"] = {"text_approved": 10, "image_ready": 7, "repairing": 2, "rejected": 1}
            return value
    context, page, errors = prepare(browser, PipelineScenario())
    page.get_by_role("button", name="运行记录", exact=True).click()
    counts = page.get_by_label("候选处理进度").first
    expect(counts).to_be_visible()
    expect(counts).to_contain_text("候选正文通过")
    for width, name in ((1280, "desktop"), (390, "mobile")):
        page.set_viewport_size({"width": width, "height": 900})
        counts.scroll_into_view_if_needed()
        if width < 760:
            page.wait_for_function("document.querySelector('.sidebar').getBoundingClientRect().right <= 0")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(tmp_path / f"pipeline-counts-{name}.png"), full_page=True, animations="disabled")
    assert not errors
    context.close()
