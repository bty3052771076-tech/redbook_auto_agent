from types import SimpleNamespace

import pytest

from src.publish import playwright_steps


class Page:
    def __init__(self, errors, *, cdp=True):
        self.errors = iter(errors)
        self.attempts = 0
        self.detached = False
        self.context = SimpleNamespace(new_cdp_session=self.new_session)
        self.cdp = cdp

    def new_session(self, page):
        if not self.cdp:
            raise RuntimeError("CDP unavailable")
        return SimpleNamespace(send=self.navigate, detach=self.detach)

    def detach(self):
        self.detached = True

    def navigate(self, *args, **kwargs):
        self.attempts += 1
        error = next(self.errors)
        return {"errorText": error} if error else {}

    def goto(self, *args, **kwargs):
        result = self.navigate()
        if result:
            raise RuntimeError(result["errorText"])


@pytest.mark.parametrize("cdp", [True, False])
def test_connection_closed_before_upload_is_retried(monkeypatch, cdp):
    monkeypatch.setattr(playwright_steps.time, "sleep", lambda seconds: None)
    page = Page(["net::ERR_CONNECTION_CLOSED", ""], cdp=cdp)
    playwright_steps._goto_xhs_page(page, "https://creator.xiaohongshu.com/", timeout_ms=60000)
    assert page.attempts == 2
    if cdp:
        assert page.detached


def test_navigation_retries_are_bounded(monkeypatch):
    monkeypatch.setattr(playwright_steps.time, "sleep", lambda seconds: None)
    page = Page(["net::ERR_CONNECTION_CLOSED"] * 10)
    with pytest.raises(RuntimeError, match="ERR_CONNECTION_CLOSED"):
        playwright_steps._goto_xhs_page(page, "https://creator.xiaohongshu.com/", timeout_ms=60000)
    assert page.attempts == 3


def test_certificate_failure_is_not_retried(monkeypatch):
    monkeypatch.setattr(playwright_steps.time, "sleep", lambda seconds: None)
    page = Page(["net::ERR_CERT_AUTHORITY_INVALID"])
    with pytest.raises(RuntimeError, match="ERR_CERT_AUTHORITY_INVALID"):
        playwright_steps._goto_xhs_page(page, "https://creator.xiaohongshu.com/", timeout_ms=60000)
    assert page.attempts == 1


def test_draft_inventory_uses_bounded_navigation(monkeypatch):
    page = SimpleNamespace(locator=lambda selector: SimpleNamespace(count=lambda: 1))
    calls = []
    monkeypatch.setattr(playwright_steps, "_goto_xhs_page",
                        lambda page, url, **kwargs: calls.append((url, kwargs)))
    monkeypatch.setattr(playwright_steps, "_wait_for_xhs_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr(playwright_steps, "_wait_for_any_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(playwright_steps, "_open_draft_box", lambda page: True)
    monkeypatch.setattr(playwright_steps, "_open_draft_tab", lambda page, dtype: True)
    playwright_steps._open_platform_draft_list(
        page, draft_type="image", login_hold=0, wait_timeout_ms=60000, headless=True,
    )
    assert calls == [(playwright_steps.TARGET_URL, {"timeout_ms": 60000})]
