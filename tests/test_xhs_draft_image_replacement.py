from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.publish import playwright_steps as steps
from src.storage.models import AssetInfo, Post


@pytest.fixture
def editor(monkeypatch, tmp_path):
    page = SimpleNamespace(image_count=3, title="", body="", replacements=[])
    page.get_by_role = lambda *a, **kw: None
    page.locator = lambda *a, **kw: None
    context = SimpleNamespace(pages=[page], set_default_timeout=lambda *a: None, close=lambda: None)
    chromium = SimpleNamespace(launch_persistent_context=lambda *a, **kw: context)
    monkeypatch.setattr(steps, "sync_playwright", lambda: nullcontext(SimpleNamespace(chromium=chromium)))
    monkeypatch.setattr(steps, "_resolve_profile_config", lambda: (tmp_path / "profile", "", []))
    monkeypatch.setattr(steps, "_resolve_cdp_url", lambda: "")
    monkeypatch.setattr(steps, "_open_platform_draft_list", lambda *a, **kw: None)
    monkeypatch.setattr(steps, "_open_draft_editor_for_titles", lambda *a, **kw: None)
    monkeypatch.setattr(steps, "_wait_for_any_locator", lambda *a: None)
    monkeypatch.setattr(steps, "_assert_xhs_operable", lambda *a, **kw: None)
    monkeypatch.setattr(steps, "_count_editor_images", lambda page: page.image_count)
    monkeypatch.setattr(steps, "_click_first", lambda *a, **kw: False)
    monkeypatch.setattr(steps, "_click_draft", lambda page: (True, "saved"))
    monkeypatch.setattr(steps, "save_execution", lambda *a: None)
    monkeypatch.setenv("XHS_FILL_SETTLE_S", "0")

    def fill(page, title, body):
        page.title, page.body = title, body
        return True, True

    def replace(page, paths, **kwargs):
        page.replacements.append(paths)
        page.image_count = len(paths)

    monkeypatch.setattr(steps, "_fill_text_fields", fill)
    monkeypatch.setattr(steps, "_verify_title_body", lambda *a: (True, True))
    monkeypatch.setattr(steps, "_read_editor_draft_snapshot", lambda page: {
        "actual_title": page.title, "actual_body": page.body,
    })
    monkeypatch.setattr(steps, "_replace_editor_images", replace, raising=False)
    post = Post(id="revision", title="Current update", body="Verified current event", assets=[
        AssetInfo(path=str(tmp_path / "new-cover.png")), AssetInfo(path=str(tmp_path / "new-card.png")),
    ])
    return page, post


def test_replacement_updates_images_before_saving(editor):
    page, post = editor
    paths = [asset.path for asset in post.assets]
    execution = steps.run_update_draft_sync(post, image_paths=paths, headless=True)
    assert execution.result == "saved_draft", execution.error
    assert page.replacements == [paths] and page.image_count == 2
    assert any(s.name == "readback_saved_draft" and s.status == "success" for s in execution.steps)


def test_title_body_success_does_not_mask_old_image_count(editor):
    page, post = editor
    page.title, page.body = post.title, post.body
    execution = steps.run_update_draft_sync(post, headless=True)
    assert execution.result == "failed", "Three old cards cannot satisfy a two-card replacement"
    assert not page.replacements


def test_read_only_reconciliation_never_replaces_images(editor):
    page, post = editor
    page.title, page.body, page.image_count = post.title, post.body, 2
    execution = steps.run_update_draft_sync(post, image_paths=[a.path for a in post.assets], dry_run=True, headless=True)
    assert execution.result == "verified_draft"
    assert not page.replacements


@pytest.mark.parametrize("new_count", [1, 2, 18])
def test_image_replacement_removes_old_thumbnail_set(monkeypatch, tmp_path, new_count):
    from playwright.sync_api import sync_playwright

    paths = [str(tmp_path / f"card-{index}.png") for index in range(new_count)]
    for path in paths:
        Path(path).write_bytes(b"test fixture")
    with sync_playwright() as browser:
        with browser.chromium.launch_persistent_context(str(tmp_path / "chrome"), channel="chrome", headless=True) as context:
            page = context.new_page()
            page.set_content("""
                <style>.img-container {width:80px;height:80px}</style>
                <script>function removeImage(button) {if (document.querySelectorAll('.img-container').length > 1) button.parentElement.remove()}</script>
                <div id="images">
                  <div class="img-container" data-old="1"><button class="close-btn" onclick="removeImage(this)">x</button></div>
                  <div class="img-container" data-old="2"><button class="close-btn" onclick="removeImage(this)">x</button></div>
                  <div class="img-container" data-old="3"><button class="close-btn" onclick="removeImage(this)">x</button></div>
                </div>
            """)
            monkeypatch.setattr(steps, "_count_editor_images", lambda p: p.locator(".img-container").count())

            def upload(p, files):
                expected_old = 0 if p.locator("[data-file]").count() else 1
                assert p.locator("[data-old]").count() == expected_old
                assert p.locator(".img-container").count() + len(files) <= 18
                p.evaluate("files => files.forEach(file => {const e=document.createElement('div');e.className='img-container';e.dataset.file=file;document.querySelector('#images').append(e)})", files)
                return True, "input"

            monkeypatch.setattr(steps, "_try_upload_with_input", upload)
            monkeypatch.setattr(steps, "_wait_for_upload_ready", lambda p, n, **kw: p.locator(".img-container").count() == n)
            monkeypatch.setattr(steps, "_wait_for_processing_done", lambda p: True)
            monkeypatch.setattr(steps, "_wait_for_upload_settle", lambda p, **kw: True)
            steps._replace_editor_images(page, paths)
            assert page.locator(".img-container").count() == new_count
            assert page.locator("[data-old]").count() == 0


def test_missing_images_fail_before_mutating_editor():
    page = SimpleNamespace(locator=lambda *a: pytest.fail("must validate before deleting old images"))
    with pytest.raises(ValueError, match="missing"):
        steps._replace_editor_images(page, [])
