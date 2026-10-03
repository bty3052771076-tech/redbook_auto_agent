from __future__ import annotations

from types import SimpleNamespace

from apps.cli import _visual_replenishment_is_terminal
from src.workflow.create_post import _normalize_daily_news_image_event


def test_image_event_cannot_follow_unrelated_page_text():
    picked = SimpleNamespace(
        title="US prosecutors reopen case at Cornell University",
        description="A related article discusses climate action.",
        content="Climate action appears in the source page's related stories.",
    )
    result = _normalize_daily_news_image_event(
        "气候行动分歧受关注",
        picked=picked,
        title="美检方重启康奈尔大学刑事调查",
        body="内容：美检方重启康奈尔大学刑事调查。",
        prompt_norm="国际新闻",
    )

    assert "气候" not in result
    assert result == ""


def test_one_unscorable_image_does_not_stop_news_replenishment():
    assert not _visual_replenishment_is_terminal(
        "VISION_BEST_OF_TWO_ZERO: both image candidates were unscorable"
    )
    assert not _visual_replenishment_is_terminal("provider HTTP 429 rate_limit_error")
    assert _visual_replenishment_is_terminal("provider HTTP 429 insufficient_quota")
    assert _visual_replenishment_is_terminal("MiniMax Token Plan 用量上限")
