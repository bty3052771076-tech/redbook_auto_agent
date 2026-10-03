import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.ai_digest import generate as gen
from src.ai_digest.models import AIDigestBrief, AIUpdateItem
from src.ai_digest.rank import ai_update_category


@pytest.fixture
def saved_item():
    # Exact public item from artifact e26ff2b590d24c97b33ab4002d5f4e44.
    path = Path(__file__).parent / 'fixtures' / 'ai_digest_transformers_v5_18_0.json'
    return AIUpdateItem.model_validate(json.loads(path.read_text(encoding='utf-8')))


@pytest.fixture
def release_source(saved_item):
    # The saved artifact contains the corrupted title, not the original response.
    return saved_item.model_copy(update={'title': 'Release5.18.0', 'summary': saved_item.raw_excerpt[:220]})


def test_library_release_is_a_tool_update_even_with_background_models(release_source):
    assert ai_update_category(release_source) == 'technical_tool'


@pytest.mark.parametrize('prefix', ['tag', 'version', 'release', 'v'])
def test_url_version_is_not_a_model_identity(prefix):
    item = AIUpdateItem(title='Maintenance update', product='GitHub Release',
                        summary='Release with fixes, using an open-weight model for testing.',
                        url=f'https://github.com/example/project/releases/{prefix}/v5.18.0')
    assert ai_update_category(item) != 'model_release'


def test_real_model_release_is_still_prioritized():
    item = AIUpdateItem(title='Qwen3 released with open weights', product='Qwen3',
                        url='https://github.com/QwenLM/Qwen3/releases/tag/v3.0.0',
                        summary='Qwen3 released with open weights for local deployment.')
    assert ai_update_category(item) == 'model_release'


def test_saved_title_fallback_uses_repository_not_incidental_qwen(release_source):
    title = gen._fallback_chinese_title(release_source)
    assert 'Transformers' in title and 'v5.18.0' in title
    assert 'Qwen' not in title and 'GitHub Re' not in title
    assert '模型发布' not in title and '开放权重' not in title


def test_generic_github_product_is_not_appended_to_real_model():
    item = AIUpdateItem(title='Qwen3 released', product='GitHub Release',
                        summary='Qwen3 released with open weights.')
    assert gen._fallback_chinese_subject(item) == 'Qwen3'


def test_title_keeps_complete_long_model_name():
    subject = 'ExampleModel-International-Vision-Preview-2026.10'
    assert gen._title_with_action(subject, '开放权重', limit=28) == subject + '开放权重'


def test_subject_cleanup_cannot_cut_long_product_before_title_generation():
    product = 'ExampleModel-International-Multilingual-Vision-Preview-2026.10'
    item = AIUpdateItem(title='Open-weight release', product=product)
    assert gen._fallback_chinese_subject(item) == product


def test_existing_bad_artifact_fails_final_semantic_gate(saved_item):
    with pytest.raises(ValueError, match='事件关系'):
        gen.validate_ai_digest_concrete_content(AIDigestBrief(items=[saved_item]))


def test_library_named_title_still_cannot_claim_qwen_release(saved_item):
    bad = saved_item.model_copy(update={
        'title': 'Transformers v5.18.0发布Qwen2.5-VL新模型',
        'summary': 'Transformers v5.18.0发布Qwen2.5-VL新模型并开放权重。',
    })
    with pytest.raises(ValueError, match='事件关系'):
        gen.validate_ai_digest_concrete_content(AIDigestBrief(items=[bad]))


def test_library_release_accepts_accurate_support_summary(saved_item):
    good = saved_item.model_copy(update={
        'title': 'Transformers v5.18.0新增模型支持',
        'summary': 'Transformers v5.18.0新增对Nemotron 3 Diarization的支持，并修复Qwen2.5-VL的视频时间位置编码。',
    })
    gen.validate_ai_digest_concrete_content(AIDigestBrief(items=[good]))


@pytest.mark.parametrize('summary', [
    'Transformers v5.18.0新增对Nemotron 3 Diarization开源模型的支持。',
    'Transformers v5.18.0发布，新增对Qwen2.5-VL模型的支持。',
    'Transformers v5.18.0发布并修复Qwen2.5-VL的视频时间位置编码。',
])
def test_model_support_or_fix_does_not_become_a_release_claim(saved_item, summary):
    good = saved_item.model_copy(update={'title': 'Transformers v5.18.0版本发布', 'summary': summary})
    gen.validate_ai_digest_concrete_content(AIDigestBrief(items=[good]))


@pytest.mark.parametrize('claim', [
    'Transformers v5.18.0发布Qwen2.5-VL并新增API支持',
    'Transformers v5.18.0推出Qwen2.5-VL并提供推理支持',
    'Transformers v5.18.0支持Qwen2.5-VL，Qwen2.5-VL今日首发',
])
def test_support_keyword_does_not_hide_separate_false_release_claim(saved_item, claim):
    bad = saved_item.model_copy(update={'title': claim, 'summary': claim + '。'})
    with pytest.raises(ValueError, match='事件关系'):
        gen.validate_ai_digest_concrete_content(AIDigestBrief(items=[bad]))


def test_library_category_cannot_be_overridden_by_corrupted_generated_title(saved_item):
    assert ai_update_category(saved_item) == 'technical_tool'


def test_fallback_repair_preserves_verified_source_metadata(saved_item, release_source):
    brief = gen._restore_traceable_ai_digest_items(AIDigestBrief(items=[saved_item]), [release_source])
    repaired = brief.items[0]
    for field in ('url', 'source_type', 'verification_status', 'confidence_score', 'raw_excerpt', 'published_at'):
        assert getattr(repaired, field) == getattr(release_source, field)
    assert repaired.summary == saved_item.summary


def test_fallback_result_is_revalidated_not_silently_accepted(saved_item, release_source, monkeypatch):
    monkeypatch.setattr(gen, '_fallback_chinese_title', lambda item: saved_item.title)
    with pytest.raises(ValueError, match='事件关系'):
        gen._restore_traceable_ai_digest_items(AIDigestBrief(items=[saved_item]), [release_source])


def test_repair_keeps_unrelated_valid_item_and_provenance(saved_item, release_source):
    other = AIUpdateItem(title='示例平台新增导出功能', summary='示例平台新增数据导出功能，用户可以下载项目中的表格。',
                         url='https://example.com/export', source_name='示例平台', published_at='2026-10-01', tags=['AI动态'])
    brief = gen._restore_traceable_ai_digest_items(AIDigestBrief(items=[saved_item, other]), [release_source, other])
    assert brief.items[1] == other
    repaired = brief.items[0]
    assert 'Qwen2.5-VL' not in repaired.title
    assert repaired.url == saved_item.url
    assert repaired.published_at == saved_item.published_at
    gen.validate_ai_digest_concrete_content(brief)


def test_stored_digest_rejects_bad_fourth_item_despite_completed_receipts(saved_item):
    good = saved_item.model_copy(update={
        'title': 'Transformers v5.18.0新增模型支持',
        'summary': 'Transformers v5.18.0新增对Nemotron 3 Diarization的支持，并修复Qwen2.5-VL的视频时间位置编码。',
    })
    digest = {
        'items': [good.model_dump(), good.model_dump(), good.model_dump(), saved_item.model_dump()],
        'source_meta': {'selected_official_count': 3},
        'quality_gate': {'passed': True, 'score': 100},
        'status': 'completed',
        'upload_receipt': {'saved': True},
    }
    before = deepcopy(digest)
    issues = gen.stored_ai_digest_review_issues(digest)
    assert len(issues) == 1
    assert '第4条' in issues[0] and '事件关系错误' in issues[0]
    assert digest == before


def test_stored_digest_accepts_generated_corrected_counterexample(saved_item, release_source):
    corrected = gen._restore_traceable_ai_digest_items(AIDigestBrief(items=[saved_item]), [release_source])
    assert corrected.items[0].title == 'Transformers v5.18.0版本发布'
    assert corrected.items[0].summary == saved_item.summary
    digest = corrected.model_dump()
    before = deepcopy(digest)
    assert gen.stored_ai_digest_review_issues(digest) == []
    assert gen.stored_ai_digest_review_issues(corrected) == []
    assert digest == before


@pytest.mark.parametrize('digest', [None, '', [], {}, {'items': []}, {'items': None}, {'items': {}}])
def test_stored_digest_missing_or_invalid_items_fail_closed(digest):
    assert gen.stored_ai_digest_review_issues(digest)


def test_stored_digest_reports_invalid_rows_without_skipping_later_bad_item(saved_item):
    issues = gen.stored_ai_digest_review_issues({'items': [None, {}, saved_item.model_dump()]})
    assert len(issues) == 3
    assert '第1条' in issues[0]
    assert '第2条' in issues[1]
    assert '第3条' in issues[2] and '事件关系错误' in issues[2]


def test_stored_digest_does_not_silently_clean_html_in_existing_copy(saved_item):
    raw = saved_item.model_dump()
    raw.update(title='<p>Transformers v5.18.0新增模型支持</p>')
    issues = gen.stored_ai_digest_review_issues({'items': [raw]})
    assert issues and 'HTML' in issues[0]


def test_stored_digest_missing_source_url_cannot_bypass_relation_check(saved_item):
    raw = saved_item.model_dump()
    raw.pop('url')
    issues = gen.stored_ai_digest_review_issues({'items': [raw]})
    assert issues and 'URL' in issues[0]


def test_stored_digest_rejects_vague_non_library_summary():
    digest = {'items': [{
        'title': 'Anthropic披露AI产品变化', 'summary': 'Anthropic披露相关内容。',
        'url': 'https://www.anthropic.com/news/example',
    }]}
    issues = gen.stored_ai_digest_review_issues(digest)
    assert issues and '空泛' in issues[0]


@pytest.mark.parametrize('changed', [
    {'tags': 123}, {'source_type': 'invalid'}, {'title': {'text': '不能隐式转文本'}},
    {'url': 'https://['}, {'url': 'not-a-source'},
])
def test_stored_digest_malformed_fields_return_issues_not_exceptions(saved_item, changed):
    raw = saved_item.model_dump()
    raw.update(changed)
    assert gen.stored_ai_digest_review_issues({'items': [raw]})


def test_stored_digest_does_not_reject_a_real_model_release():
    digest = {'items': [{
        'title': 'Qwen3发布并开放模型权重',
        'summary': 'Qwen3发布并开放模型权重，开发者可以下载权重并在本地部署。',
        'url': 'https://github.com/QwenLM/Qwen3/releases/tag/v3.0.0',
    }]}
    assert gen.stored_ai_digest_review_issues(digest) == []


def test_real_oct1_digest_rejects_fourth_item_read_only(saved_item):
    path = Path('E:/AI/codex/redbook_runtime/data/posts/e26ff2b590d24c97b33ab4002d5f4e44/post.json')
    if not path.is_file():
        pytest.skip('runtime acceptance post is not present')
    before = path.read_bytes()
    digest = json.loads(before)['platform']['ai_digest']
    assert digest['items'][3] == saved_item.model_dump()
    issues = gen.stored_ai_digest_review_issues(digest)
    assert any('第4条' in issue and '事件关系错误' in issue for issue in issues)
    assert path.read_bytes() == before


def test_generation_with_offline_model_response_repairs_real_library_subject(saved_item, release_source, monkeypatch):
    from types import SimpleNamespace

    calls = []
    payload = AIDigestBrief(date='2026-10-01', items=[saved_item]).model_dump_json()
    class Model:
        def invoke(self, messages):
            calls.append(messages)
            return SimpleNamespace(content=payload)

    monkeypatch.setattr(gen, 'init_chat_model', lambda *args, **kwargs: Model())
    cfg = SimpleNamespace(provider='offline', model='offline', base_url='https://example.invalid', api_key='offline')
    result = gen.generate_ai_digest_brief_with_llm(
        [cfg], [release_source], target_count=1, date='2026-10-01',
    )
    assert len(calls) == 1
    assert result.items[0].title == 'Transformers v5.18.0版本发布'
    assert result.items[0].url == saved_item.url
    assert result.items[0].raw_excerpt == saved_item.raw_excerpt
    assert gen.stored_ai_digest_review_issues(result) == []
