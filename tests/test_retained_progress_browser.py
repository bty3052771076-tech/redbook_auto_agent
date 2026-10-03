"""Render the built frontend with all requests fulfilled locally."""

import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from backend.progress import build_activity


def test_resumed_progress_renders_retained_items_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    root_id, attempt_id, cid, pid = "a" * 32, "b" * 32, "c" * 32, "d" * 32
    plan = {"id": "e" * 32, "version": 1, "job_id": root_id, "resume_job_id": attempt_id,
            "executable": True, "jobs": [{"kind": "daily_news", "title": "每日新闻", "count": 10}],
            "status": "running", "delivery": "save_draft", "platform": "xhs"}
    conversation = {"id": cid, "title": "续跑测试", "messages": [], "plans": [plan], "runs": [root_id, attempt_id]}
    checkpoint = {"jobs": plan["jobs"], "job_index": 0, "post_ids": [pid], "reviewed_post_ids": [pid]}
    run = {"id": attempt_id, "agent_run_id": root_id, "status": "running", "retained_post_ids": [pid],
           "post_rows": [{"id": pid, "title": "保留下来的合格新闻", "images": 1, "status": "approved"}]}
    run["activity"] = build_activity(run, checkpoint)
    rows = {"/api/session": {}, "/api/conversations": {"rows": [conversation]},
            f"/api/conversations/{cid}": conversation, "/api/runs": {"rows": [run]},
            f"/api/runs/{attempt_id}": run, "/api/drafts": {"rows": []},
            "/api/connections": {"database": {"status": "ready"}, "providers": {"bindings": {}, "connections": []},
                                 "models": {"rows": []}, "profile_configured": True}}
    dist = Path(__file__).resolve().parents[1] / "frontend/dist"

    def route(request):
        path = urlsplit(request.request.url).path
        if path in rows:
            request.fulfill(content_type="application/json", body=json.dumps(rows[path], ensure_ascii=False))
        else:
            asset = (dist / path.lstrip("/")).resolve() if path != "/" else dist / "index.html"
            assert asset.is_relative_to(dist) and asset.is_file(), path
            request.fulfill(content_type=mimetypes.guess_type(str(asset))[0] or "application/octet-stream", body=asset.read_bytes())

    with sync_playwright() as playwright:
        chrome = os.getenv("REDBOOK_CHROME_EXECUTABLE") or r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        browser = playwright.chromium.launch(headless=True, executable_path=chrome)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", route)
        page.add_init_script(f"localStorage.setItem('agent-conversation', '{cid}')")
        page.goto("http://127.0.0.1:8786/")
        progress = page.locator(".live-message .run-progress")
        expect(progress.get_by_text("保留待上传 1", exact=False)).to_be_visible()
        expect(progress.get_by_text("保留下来的合格新闻", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="正在执行", exact=True)).to_be_disabled()
        page.screenshot(path=str(tmp_path / "retained-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_function("() => document.querySelector('.sidebar').getBoundingClientRect().right <= 0")
        expect(progress.get_by_text("保留下来的合格新闻", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / "retained-mobile.png"), full_page=True)
        assert not errors
        browser.close()
