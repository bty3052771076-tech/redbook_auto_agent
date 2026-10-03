import pytest

from src.ai_digest.generate import _format_ai_digest_body_published_at
from src.ai_digest.render import _format_published_at


@pytest.mark.parametrize("value,want", [
    ("2026-10-01T04:00:00Z", "2026-10-01 12:00"),
    ("2026-10-01T20:10:00+00:00", "2026-10-02 04:10"),
    ("2026-10-02T10:00:00+02:00", "2026-10-02 16:00"),
    ("2026-10-02T12:00:00+08:00", "2026-10-02 12:00"),
    ("2026-10-02T12:00:00", "2026-10-02 12:00"),
    ("2026-10-02", "2026-10-02"),
    ("", ""),
])
def test_card_and_body_show_the_same_beijing_publication_time(value, want):
    assert _format_published_at(value) == want
    assert _format_ai_digest_body_published_at(value) == want
