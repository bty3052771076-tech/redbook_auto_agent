from datetime import date

import pytest
from PIL import Image

from src.wool import image_edit, workflow


def test_empty_successful_search_notifies_before_image_and_persists_notice(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('REDBOOK_RUNTIME_ROOT', str(tmp_path))
    monkeypatch.setenv('WOOL_IMAGE_MODE', 'reference_edit')
    monkeypatch.setenv('WOOL_LLM_COPY', '0')
    monkeypatch.setattr(workflow, 'collect_daily_wool_offers', lambda **kw: ([], {
        'source_meta': {'errors': [], 'source_health': {'attempts': [
            {'source_name': 'openai', 'status': 'empty'}]}}}))
    events = []
    image_path = tmp_path / 'cover.png'
    Image.new('RGB', (128, 128), 'white').save(image_path)

    def generate(**kw):
        assert any(stage == 'wool_result' and 'no_verified_offers' in detail for stage, detail in events)
        return image_path, {'asset_mode': 'no_offer_neutral_ai_illustration', 'provider': 'opencodex', 'elapsed_s': 1}

    monkeypatch.setattr(image_edit, 'create_wool_image', generate)
    post = workflow.create_daily_wool_posts(now=date(2026, 10, 5), progress=lambda s, d: events.append((s, d)))[0]
    meta = post.platform['daily_wool']
    assert meta['availability_status'] == 'no_verified_offers'
    assert '2026-10-05' in meta['user_notice']
    assert '暂未发现' in meta['user_notice']
    assert meta['offer_count'] == 0
    assert '暂无可核验福利' in post.title


@pytest.mark.parametrize('source_meta', [
    {'errors': ['openai: timeout'], 'source_health': {'attempts': [{'status': 'timeout'}]}},
    {'errors': [], 'source_health': {'attempts': [{'status': 'cooldown'}]}},
    {'errors': ['x-openai: 429'], 'source_health': {'attempts': [{'status': 'success'}, {'status': 'http_error'}]}},
    {},
])
def test_failed_verification_does_not_claim_no_benefits_or_generate_cover(tmp_path, monkeypatch, source_meta):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(workflow, 'collect_daily_wool_offers', lambda **kw: ([], {'source_meta': source_meta}))
    monkeypatch.setattr(image_edit, 'create_wool_image', lambda **kw: pytest.fail('no image request for unknown benefits'))
    events = []
    with pytest.raises(RuntimeError, match='WOOL_VERIFICATION_INCOMPLETE'):
        workflow.create_daily_wool_posts(now=date(2026, 10, 5), progress=lambda s, d: events.append((s, d)))
    assert any(s == 'wool_result' and '核验未完成' in d for s, d in events)
    assert not list(tmp_path.glob('data/posts/*/post.json'))
