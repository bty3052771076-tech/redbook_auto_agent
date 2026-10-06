import json

import pytest

from src.ai_digest.collect import load_ai_digest_research_items
from src.ai_digest.sources import default_ai_digest_sources
from src.ai_digest.models import AIUpdateItem
from src.ai_digest.rank import dedupe_ai_updates


def test_official_claude_code_release_stream_is_available():
    source = next(s for s in default_ai_digest_sources() if s.name == 'anthropic-claude-code')
    assert source.url == 'https://api.github.com/repos/anthropics/claude-code/releases'
    assert source.vendor == 'Anthropic'
    assert source.kind == 'github'


@pytest.mark.parametrize('url,accepted', [
    ('https://github.com/anthropics/claude-code/releases/tag/v2.1.289', True),
    ('https://github.com/untrusted/claude-code/releases/tag/v2.1.289', False),
    ('https://github.com/anthropics-fake/claude-code/releases/tag/v2.1.289', False),
    ('https://github.com.example/anthropics/claude-code/releases/tag/v2.1.289', False),
])
def test_reviewed_github_evidence_requires_official_owner(tmp_path, url, accepted):
    path = tmp_path / 'items.json'
    path.write_text(json.dumps([dict(title='Claude Code adds agent.spawn', vendor='Anthropic',
        source_type='github', url=url, published_at='2026-10-03T23:07:17Z',
        raw_excerpt='Added agent.spawn for teammates', evidence_urls=[url])]))
    if accepted:
        assert len(load_ai_digest_research_items(path)) == 1
    else:
        with pytest.raises(ValueError, match='official host'):
            load_ai_digest_research_items(path)


def test_same_github_release_with_a_reviewed_title_is_one_event():
    original = AIUpdateItem(title='v2.1.289', vendor='Anthropic', source_type='github',
        url='https://github.com/anthropics/claude-code/releases/tag/v2.1.289',
        published_at='2026-10-03T23:07:17Z', raw_excerpt='Added agent.spawn for teammates')
    reviewed = original.model_copy(update={'title': 'Claude Code adds agent.spawn for teammates'})
    assert len(dedupe_ai_updates([original, reviewed])) == 1


def test_distinct_github_versions_are_not_merged_by_url():
    first = AIUpdateItem(title='v2.1.289', vendor='Anthropic', source_type='github',
        url='https://github.com/anthropics/claude-code/releases/tag/v2.1.289')
    second = first.model_copy(update={'title': 'v2.1.288',
        'url': 'https://github.com/anthropics/claude-code/releases/tag/v2.1.288'})
    assert len(dedupe_ai_updates([first, second])) == 2


def test_same_version_number_in_distinct_repositories_is_not_one_release():
    first = AIUpdateItem(title='v2.1.289', vendor='Anthropic', source_type='github',
        url='https://github.com/anthropics/claude-code/releases/tag/v2.1.289')
    second = first.model_copy(update={
        'url': 'https://github.com/anthropics/another-tool/releases/tag/v2.1.289'})
    assert len(dedupe_ai_updates([first, second])) == 2
