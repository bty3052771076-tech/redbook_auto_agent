from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.global_map.models import MapSnapshot, VerifiedEvent
from src.global_map.translate import _translations_omitting_unsafe, _validated_translations
from src.llm.generate import _parse_json_text


def _snapshot() -> MapSnapshot:
    return MapSnapshot(
        target_date="2026-09-30",
        cutoff=datetime(2026, 9, 30, 4, tzinfo=timezone.utc).isoformat(),
        events=[
            VerifiedEvent(
                event_key="one", title="China's manufacturing PMI reaches 50.1",
                summary="Manufacturing activity expanded in September.",
                country="China", location_name="China", latitude=35.8, longitude=104.1,
            ),
            VerifiedEvent(
                event_key="two", title="Germany debates a new EU rule",
                summary="Officials are discussing a proposal.",
                country="Germany", location_name="Germany", latitude=51.1, longitude=10.4,
            ),
        ],
        coverage_status="limited", upload_allowed=True,
        located_event_count=2, country_count=2,
    )


def test_translated_titles_keep_source_evidence_untouched():
    original = _snapshot()
    result = _validated_translations(original, {
        "events": [
            {"index": 1, "title": "中国制造业采购经理指数达到50.1", "summary": "九月制造业活动扩张。"},
            {"index": 2, "title": "德国讨论新的欧盟规则", "summary": "官员正在讨论一项提案。"},
        ]
    })

    assert result.events[0].title.startswith("中国")
    assert original.events[0].title.startswith("China")
    assert result.events[0].latitude == original.events[0].latitude


@pytest.mark.parametrize("bad_rows", [
    [{"index": 1, "title": "中国制造业变化", "summary": ""}],
    [
        {"index": 2, "title": "中国制造业变化", "summary": ""},
        {"index": 1, "title": "德国讨论欧盟规则", "summary": ""},
    ],
    [
        {"index": 1, "title": "中国制造业增加到99", "summary": ""},
        {"index": 2, "title": "德国讨论欧盟规则", "summary": ""},
    ],
])
def test_translation_rejects_missing_reordered_or_invented_facts(bad_rows):
    with pytest.raises(RuntimeError, match="MAP_TRANSLATION_INVALID"):
        _validated_translations(_snapshot(), {"events": bad_rows})


def test_translation_removes_photo_attribution_from_body_text():
    result = _validated_translations(_snapshot(), {
        "events": [
            {"index": 1, "title": "中国制造业采购经理指数达到50.1", "summary": "九月制造业活动扩张。（图片来源：CNN）"},
            {"index": 2, "title": "德国讨论新的欧盟规则", "summary": "官员正在讨论一项提案。来源：reuters.com"},
        ]
    })

    assert "图片来源" not in result.events[0].summary
    assert "reuters.com" not in result.events[1].summary


def test_fenced_model_json_can_be_validated_without_accepting_extra_events():
    text = 'Translation:\n```json\n{"events":[{"index":1,"title":"中国制造业指数达到50.1","summary":"九月制造业活动扩张。"},{"index":2,"title":"德国讨论新的欧盟规则","summary":"官员讨论一项提案。"}]}\n```'
    payload = _parse_json_text(text)

    assert payload is not None
    assert len(_validated_translations(_snapshot(), payload).events) == 2


def test_unsafe_translation_is_dropped_and_coverage_is_recomputed():
    snapshot = _snapshot()
    result = _translations_omitting_unsafe(snapshot, {
        "events": [
            {"index": 1, "title": "中国制造业采购经理指数达到50.1", "summary": "九月制造业活动扩张。"},
            {"index": 2, "title": "德国讨论新增三项欧盟规则", "summary": "官员正在讨论一项提案。"},
        ]
    })

    assert len(result.events) == 1
    assert result.events[0].event_key == "one"
    assert not result.upload_allowed
    assert "已排除1条" in result.warning


def test_spelled_source_number_can_be_translated_without_being_invented():
    snapshot = _snapshot()
    snapshot.events[1].title = "Germany debates three new EU rules"
    result = _validated_translations(snapshot, {
        "events": [
            {"index": 1, "title": "中国制造业采购经理指数达到50.1", "summary": "九月制造业活动扩张。"},
            {"index": 2, "title": "德国讨论三项新的欧盟规则", "summary": "官员正在讨论一项提案。"},
        ]
    })

    assert len(result.events) == 2


def test_same_event_from_two_publishers_is_not_drawn_twice():
    snapshot = _snapshot()
    snapshot.events.extend([
        VerifiedEvent(
            event_key="morocco-france24",
            title="Fatima El Mansouri named Morocco first woman prime minister",
            summary="The king appointed her after the election.",
            country="Morocco", location_name="Morocco", latitude=31.8, longitude=-7.1,
        ),
        VerifiedEvent(
            event_key="morocco-premiumtimes",
            title="Morocco appoints first female prime minister",
            summary="The appointment was called historic.",
            country="Morocco", location_name="Morocco", latitude=31.8, longitude=-7.1,
        ),
    ])
    result = _translations_omitting_unsafe(snapshot, {"events": [
        {"index": 1, "title": "中国制造业采购经理指数上升", "summary": "制造业活动扩张。"},
        {"index": 2, "title": "德国讨论新的欧盟规则", "summary": "官员讨论提案。"},
        {"index": 3, "title": "摩洛哥任命首位女总理曼苏里", "summary": "国王任命曼苏里。"},
        {"index": 4, "title": "摩洛哥首位女总理正式获任命", "summary": "任命被称为历史性事件。"},
    ]})
    assert len(result.events) == 3
    assert len([event for event in result.events if event.country == "Morocco"]) == 1


def test_minimax_map_translation_retries_missing_events_array(monkeypatch):
    from types import SimpleNamespace

    import openai

    from src.global_map import translate

    responses = iter([
        '{"message":"翻译完成"}',
        '{"events":[{"index":1,"title":"中国制造业采购经理指数达到50.1","summary":"九月制造业活动扩张。"},'
        '{"index":2,"title":"德国讨论新的欧盟规则","summary":"官员讨论一项提案。"}]}',
    ])
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content=next(responses))
            )])

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    monkeypatch.setattr("src.config.load_llm_config", lambda: SimpleNamespace(
        provider="minimax", model="MiniMax-M3", base_url="https://example.invalid", api_key="test"
    ))
    result = translate.translate_map_snapshot(_snapshot())
    assert len(result.events) == 2
    assert len(calls) == 2
