"""CC Switch image selection uses existing UI and never submits a generation task."""
import json

from playwright.sync_api import expect

from apps import gui
from apps.web_service import Workbench
from backend import app as module
from test_model_selection_persistence import selection_frontend, selection_page, selection_store


def test_ccswitch_image_default_survives_reload_and_enters_plan(selection_page, selection_store, monkeypatch, tmp_path):
    page = selection_page
    current, _, conversations = selection_store
    config = {"CCSWITCH_IMAGE_ENABLED": "1"}
    monkeypatch.setattr(gui, "load_env_file", lambda path: dict(config))
    monkeypatch.setattr(gui, "build_subprocess_env", lambda overrides: {**config, **overrides})
    before = current.providers()["bindings"]
    page.reload()
    page.get_by_role("button", name="连接与模型", exact=True).click()
    page.get_by_role("tab", name="默认角色", exact=True).click()
    page.get_by_role("button", name="生图模型", exact=True).click()
    page.get_by_role("option", name="CC Switch / ChatGPT订阅 · gpt-image-2", exact=True).click()
    expect(page.get_by_role("button", name="生图模型", exact=True)).to_contain_text("CC Switch")
    expect(page.get_by_text("默认角色已保存", exact=True)).to_be_visible()
    roles = current.providers()["bindings"]
    assert roles["image"] == "ccswitch:gpt-image-2"
    assert all(roles[k] == before[k] for k in ("agent", "writer"))
    page.wait_for_load_state("networkidle")
    page.screenshot(path=str(tmp_path / "ccswitch-desktop.png"), full_page=True)
    reopened = Workbench(current.root, conversation_store=conversations)
    monkeypatch.setattr(module.app.state, "service", reopened)
    page.reload()
    page.get_by_role("button", name="连接与模型", exact=True).click()
    page.get_by_role("tab", name="默认角色", exact=True).click()
    expect(page.get_by_role("button", name="生图模型", exact=True)).to_contain_text("CC Switch")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function("() => document.querySelector('.sidebar').getBoundingClientRect().right <= 0")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.screenshot(path=str(tmp_path / "ccswitch-mobile.png"), full_page=True)
    plan = reopened.append_agent_message(reopened.create_agent_conversation()["id"], "生成1条每日新闻")['plan']
    assert plan["model_roles"]["image"] == "ccswitch:gpt-image-2"
    frozen = json.loads(reopened.freeze_model_roles({}, {"kind": "agent"})["RUN_LEGACY_MODEL_ROLES"])
    assert frozen["image"] == "ccswitch:gpt-image-2"
    assert reopened.jobs == {}
