from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.storage.models import AssetInfo, Execution, Post


@pytest.mark.parametrize('actual_body,image_count,success', [('完整事件正文',1,True), ('正文缺失',1,False), ('完整事件正文',0,False)])
def test_readonly_draft_verifies_full_content_and_image_count(tmp_path, monkeypatch, actual_body, image_count, success):
    from src.publish import playwright_steps as steps
    post = Post(id='a'*32, title='具体新闻标题', body='完整事件正文', assets=[AssetInfo(path='picture.png')])
    owned = SimpleNamespace(pages=[object()], closed=False)
    owned.set_default_timeout = lambda value:None
    owned.close = lambda:setattr(owned,'closed',True)

    @contextmanager
    def browser():
        yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda *a,**kw:owned))

    monkeypatch.setattr(steps,'sync_playwright',browser)
    monkeypatch.setattr(steps,'_resolve_profile_config',lambda:(tmp_path,None,[]))
    monkeypatch.setattr(steps,'_resolve_cdp_url',lambda:'')
    monkeypatch.setattr(steps,'_open_platform_draft_list',lambda *a,**kw:None)
    monkeypatch.setattr(steps,'_open_draft_editor_for_titles',lambda *a,**kw:None)
    monkeypatch.setattr(steps,'_wait_for_any_locator',lambda *a,**kw:None)
    monkeypatch.setattr(steps,'_read_editor_draft_snapshot',lambda page:{'actual_title':post.title,'actual_body':actual_body})
    monkeypatch.setattr(steps,'_count_editor_images',lambda page:image_count)
    monkeypatch.setattr(steps,'save_execution',lambda execution:None)
    writes=[]
    monkeypatch.setattr(steps,'_fill_text_fields',lambda *a:writes.append('fill'))
    monkeypatch.setattr(steps,'_click_draft',lambda *a:writes.append('save'))
    result=steps._run_update_draft_sync_unlocked(post,dry_run=True,headless=True)
    assert result.result == ('verified_draft' if success else 'failed')
    readback=next(step for step in result.steps if step.name=='readback_saved_draft')
    assert readback.status == ('success' if success else 'failed')
    import json
    proof=json.loads(readback.detail)
    assert proof['actual_title']=='具体新闻标题' and proof['actual_body']==actual_body
    assert proof['actual_image_count']==image_count and proof['expected_image_count']==1
    assert writes == [] and owned.closed


def test_verify_draft_command_reports_readback_failure_with_nonzero_exit(monkeypatch):
    import typer
    from apps import cli
    post = Post(id='b'*32, title='具体新闻标题', body='完整事件正文')
    monkeypatch.setattr(cli,'load_post',lambda identity:post)
    monkeypatch.setattr(cli,'_next_attempt',lambda identity:1)
    monkeypatch.setattr(cli,'run_update_draft_sync',lambda *args,**kwargs:Execution(post_id=post.id,result='failed',error={'message':'readback mismatch'}))
    with pytest.raises(typer.Exit) as error:
        cli.update_draft(post.id,draft_type='image',dry_run=True,headless=True,login_hold=0,wait_timeout=30)
    assert error.value.exit_code == 1
