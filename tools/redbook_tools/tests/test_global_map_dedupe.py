from src.storage.models import Post
from src.workflow.quality_gate import _same_event


def _map_post(day: str) -> Post:
    return Post(
        title=f"今日全球事件关注图｜{day}",
        body="以下是本轮信源中已核验、可定位的代表性事件。",
        platform={"global_map": {"target_date": day}},
    )


def test_global_map_dedupe_is_per_day_not_per_recurring_column_title():
    assert _same_event(_map_post("2026-09-30"), _map_post("2026-09-30"))
    assert not _same_event(_map_post("2026-09-30"), _map_post("2026-09-18"))
    assert not _same_event(_map_post("2026-09-30"), Post(title="今日全球事件关注图"))
