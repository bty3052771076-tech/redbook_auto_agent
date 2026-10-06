import json
from datetime import datetime, timezone

import pytest

from src.ai_digest.fetchers import parse_codex_reset_html
from src.wool.collect import extract_wool_offers


def history_page():
    history = [
        {'recordKind': 'confirmed_global', 'resetAt': '2026-10-02T21:18:48Z',
         'details': {'resetMethod': 'Hard Reset', 'note': 'Reset all propagated. Enjoy.'},
         'source': 'https://x.com/thsottiaux/status/111'},
        {'recordKind': 'banked_distribution', 'resetAt': '2026-09-29T18:41:59Z',
         'details': {'resetMethod': 'Banked Reset distribution', 'note': 'A BANKED Reset distribution was observed.'},
         'source': 'https://x.com/thsottiaux/status/222'},
    ]
    payload = '1:' + json.dumps({'recentHistory': history}, separators=(',', ':'))
    return '<script>self.__next_f.push(' + json.dumps([1, payload]) + ')</script>'


def test_reset_date_note_and_evidence_are_bound_to_the_same_record():
    items = parse_codex_reset_html(history_page(), source_name='Tracker', vendor='Codex', base_url='https://tracker.example')
    assert len(items) == 2
    hard = next(i for i in items if i.published_at == '2026-10-02T21:18:48Z')
    banked = next(i for i in items if i.published_at == '2026-09-29T18:41:59Z')
    assert 'banked_reset' not in hard.tags
    assert 'BANKED' not in hard.raw_excerpt
    assert 'https://x.com/thsottiaux/status/111' in hard.evidence_urls
    assert 'banked_reset' in banked.tags
    assert 'https://x.com/thsottiaux/status/222' in banked.evidence_urls
    assert extract_wool_offers(items, now=datetime(2026, 10, 5, 6, tzinfo=timezone.utc), max_age_days=3) == []


def test_tracker_markup_change_is_not_reported_as_a_successful_empty_search():
    with pytest.raises(ValueError, match='history'):
        parse_codex_reset_html('<html>Loading</html>', source_name='Tracker', vendor='Codex')
