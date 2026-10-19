import json

from playwright.sync_api import expect

from apps import gui
from apps.web_service import Workbench
from backend import app as module
from test_model_selection_persistence import selection_frontend, selection_store, selection_page


def test_flash_default_roles_persist_in_browser_and_new_tasks(selection_page, selection_store, monkeypatch, tmp_path):
    page = selection_page
    current, _, conversations = selection_store
    env = {
        "MINIMAX_TOKEN_PLAN_API_KEY": "synthetic-flash-key", "MINIMAX_LLM_MODEL": "MiniMax-M3",
        "MINIMAX_BILLING_MODE": "subscription_only", "MINIMAX_ALLOW_PAID_CREDITS": "0",
        "MINIMAX_ALLOW_PAYGO": "0",
    }
    monkeypatch.setattr(gui, "load_env_file", lambda path: dict(env))
    monkeypatch.setattr(gui, "build_subprocess_env", lambda overrides: {**env, **overrides})
    page.reload()
    page.get_by_role("button", name="连接与模型", exact=True).click()
    page.get_by_role("tab", name="默认角色", exact=True).click()
    for label in ("智能体主控模型", "写稿模型"):
        page.get_by_role("button", name=label, exact=True).click()
        page.get_by_role("option", name="MiniMax · MiniMax-M3.1-Flash", exact=True).click()
        expect(page.get_by_role("button", name=label, exact=True)).to_contain_text("MiniMax-M3.1-Flash")
        expect(page.get_by_role("button", name=label, exact=True)).to_be_enabled()
    reopened = Workbench(current.root, conversation_store=conversations)
    monkeypatch.setattr(module.app.state, "service", reopened)
    page.reload()
    page.get_by_role("button", name="连接与模型", exact=True).click()
    page.get_by_role("tab", name="默认角色", exact=True).click()
    expect(page.get_by_role("button", name="智能体主控模型", exact=True)).to_contain_text("MiniMax-M3.1-Flash")
    expect(page.get_by_role("button", name="写稿模型", exact=True)).to_contain_text("MiniMax-M3.1-Flash")
    page.screenshot(path=str(tmp_path / "minimax-flash-defaults.png"), full_page=True)
    roles = reopened.providers()["bindings"]
    assert roles["agent"] == "minimax:MiniMax-M3.1-Flash"
    assert roles["writer"] == "minimax:MiniMax-M3.1-Flash"
    assert json.loads(reopened.freeze_model_roles({}, {"kind": "agent"})["RUN_LEGACY_MODEL_ROLES"])["writer"] == roles["writer"]
    assert reopened.jobs == {}
