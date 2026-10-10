import os

import pytest
from playwright.sync_api import sync_playwright

from src.publish.playwright_steps import _click_draft


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=os.getenv(
            'REDBOOK_CHROME_EXECUTABLE', r'C:\Program Files\Google\Chrome\Application\chrome.exe'))
        page = browser.new_page(viewport={'width': 1280, 'height': 720})
        page.set_default_timeout(1000)
        yield page
        browser.close()


def test_closed_shadow_save_targets_real_button_not_host(page):
    page.set_content('''<xhs-publish-btn save-text="暂存离开"></xhs-publish-btn>
        <script>
        window.saved=0; window.published=0;
        const host=document.querySelector('xhs-publish-btn');
        const root=host.attachShadow({mode:'closed'});
        root.innerHTML='<button id="save">暂存离开</button><button id="publish">发布</button>';
        root.querySelector('#save').onclick=()=>window.saved++;
        root.querySelector('#publish').onclick=()=>window.published++;
        </script>''')
    ok, detail = _click_draft(page)
    assert ok, detail
    assert page.evaluate('window.saved') == 1
    assert page.evaluate('window.published') == 0


def test_no_save_button_never_clicks_publish_or_coordinates(page):
    page.set_content('''<style>button {position:fixed; bottom:0; left:0; width:100%; height:100px}</style>
        <button onclick="window.published++">发布</button><script>window.published=0</script>''')
    ok, detail = _click_draft(page)
    assert not ok
    assert page.evaluate('window.published') == 0


def test_disabled_save_does_not_fall_through_to_publish(page):
    page.set_content('''<xhs-publish-btn save-text="暂存离开" save-disabled="true"></xhs-publish-btn>
        <script>window.saved=0; window.published=0;
        const root=document.querySelector('xhs-publish-btn').attachShadow({mode:'closed'});
        root.innerHTML='<button disabled>暂存离开</button><button>发布</button>';
        root.querySelectorAll('button')[0].onclick=()=>window.saved++;
        root.querySelectorAll('button')[1].onclick=()=>window.published++;</script>''')
    assert _click_draft(page)[0] is False
    assert page.evaluate('window.saved+window.published') == 0
