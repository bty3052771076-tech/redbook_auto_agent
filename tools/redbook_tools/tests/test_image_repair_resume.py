"""Checkpoint-window cases; intentionally not executed in this delivery."""

from types import SimpleNamespace

import pytest

from apps import cli
from src.storage.models import AssetInfo, Post
from src.workflow.image_repair import merge_pending_local_image, retained_image_repair
from src.workflow.review_cache import cached_vision_matches, stamp_vision_cache
from src.workflow.vision_review import VisionReviewResult


def _post(tmp_path):
    image = tmp_path / "first.png"
    image.write_bytes(b"first-image")
    return Post(id="repair-one", title="A source event", body="Complete factual content",
                assets=[AssetInfo(path=str(image))],
                platform={"news": {"picked": {"title": "A source event", "url": "https://example.com/one"},
                                   "image_event": "One supported scene"},
                          "image": {"prompt": "One scene without text"}})


def _review(score):
    return VisionReviewResult(ok=score >= 70, score=score, issues=() if score >= 70 else ("bad scene",),
                              retry_prompt="fix scene", provider="test", model="vision")


def _options(review, redraw, checkpoint=None):
    return dict(config=SimpleNamespace(provider="test", model="vision"), viewpoint="neutral",
                max_repairs=5, review_fn=review, regenerate_fn=redraw, checkpoint_fn=checkpoint)


def test_resume_after_redraw_saved_only_reviews_second_image(tmp_path, monkeypatch):
    post = _post(tmp_path)
    saved = []
    monkeypatch.setattr(cli, "save_post", lambda value: saved.append(value.model_copy(deep=True)))
    calls = {"redraw": 0, "review": 0}

    def review(value, **kwargs):
        calls["review"] += 1
        if calls["review"] == 2:
            raise KeyboardInterrupt("stop during second review")
        return _review(40)

    def redraw(value, prompt):
        calls["redraw"] += 1
        path = tmp_path / "second.png"
        path.write_bytes(b"second-image")
        value.assets = [AssetInfo(path=str(path))]
        return True

    with pytest.raises(KeyboardInterrupt):
        cli._review_with_bounded_image_repair(post, **_options(review, redraw))
    restored = saved[-1]
    assert restored.platform["image_repair"]["phase"] == "redraw_saved"
    result, repairs, errors, history = cli._review_with_bounded_image_repair(
        restored, **_options(lambda *args, **kwargs: _review(88), redraw))
    assert calls["redraw"] == 1
    assert result.score == 88 and repairs == 1 and len(history) == 2 and not errors


def test_uncertain_redraw_is_not_resubmitted(tmp_path, monkeypatch):
    post = _post(tmp_path)
    monkeypatch.setattr(cli, "save_post", lambda value: None)
    calls = []

    def redraw(value, prompt):
        calls.append("redraw")
        raise KeyboardInterrupt("remote result not committed")

    with pytest.raises(KeyboardInterrupt):
        cli._review_with_bounded_image_repair(post, **_options(lambda *args, **kwargs: _review(40), redraw))
    result, repairs, errors, history = cli._review_with_bounded_image_repair(
        post, **_options(lambda *args, **kwargs: pytest.fail("first verdict is retained"), redraw))
    assert calls == ["redraw"]
    assert result.score == 40 and repairs == 1 and len(history) == 1
    assert any("IMAGE_REDRAW_UNCERTAIN" in error for error in errors)


def test_first_verdict_commits_before_remote_redraw(tmp_path, monkeypatch):
    post = _post(tmp_path)
    checkpoints = []
    monkeypatch.setattr(cli, "save_post", lambda value: None)

    def checkpoint(value):
        checkpoints.append(value.model_copy(deep=True))

    def redraw(value, prompt):
        assert checkpoints[-1].platform["image_repair"]["phase"] == "redraw_requested"
        assert checkpoints[-1].platform["image_repair"]["first"]["review"]["score"] == 40
        return False

    cli._review_with_bounded_image_repair(
        post, **_options(lambda *args, **kwargs: _review(40), redraw, checkpoint))
    assert checkpoints[0].platform["image_repair"]["phase"] == "first_reviewed"


def test_recovery_merges_only_reserved_local_second_image(tmp_path, monkeypatch):
    post = _post(tmp_path)
    saved = []
    monkeypatch.setattr(cli, "save_post", lambda value: saved.append(value.model_copy(deep=True)))

    def redraw(value, prompt):
        path = tmp_path / "second.png"
        path.write_bytes(b"second-image")
        value.assets = [AssetInfo(path=str(path))]
        raise KeyboardInterrupt("after local save before artifact commit")

    with pytest.raises(KeyboardInterrupt):
        cli._review_with_bounded_image_repair(post, **_options(lambda *args, **kwargs: _review(40), redraw))
    authoritative = saved[-1]
    assert merge_pending_local_image(authoritative, post)
    assert authoritative.assets[0].path.endswith("second.png")
    changed = post.model_copy(deep=True)
    changed.body = "unrelated revision"
    assert not merge_pending_local_image(saved[-1], changed)


def test_better_first_candidate_preserves_delivery_fields(tmp_path, monkeypatch):
    post = _post(tmp_path)
    monkeypatch.setattr(cli, "save_post", lambda value: None)
    results = iter([_review(60), _review(30)])

    def redraw(value, prompt):
        path = tmp_path / "second.png"
        path.write_bytes(b"second-image")
        value.assets = [AssetInfo(path=str(path))]
        value.platform["xhs_draft"] = {"execution_id": "preserve-receipt"}
        return True

    result, _, _, _ = cli._review_with_bounded_image_repair(
        post, **_options(lambda *args, **kwargs: next(results), redraw))
    assert result.score == 60 and not result.ok
    assert post.assets[0].path.endswith("first.png")
    assert post.platform["xhs_draft"]["execution_id"] == "preserve-receipt"


def test_completed_repair_does_not_undo_changed_scene(tmp_path, monkeypatch):
    post = _post(tmp_path)
    monkeypatch.setattr(cli, "save_post", lambda value: None)
    cli._review_with_bounded_image_repair(
        post, **_options(lambda *args, **kwargs: _review(88), lambda *args: False))
    assert retained_image_repair(post, "neutral") is not None
    post.platform["news"]["image_event"] = "The manually corrected scene"
    assert retained_image_repair(post, "neutral") is None


@pytest.mark.parametrize("field", ["image_event", "generation_prompt", "reviewer"])
def test_cache_invalidates_complete_review_inputs(tmp_path, monkeypatch, field):
    post = _post(tmp_path)
    cached = stamp_vision_cache(post, {"ok": True, "score": 88}, "neutral")
    assert cached_vision_matches(post, cached, "neutral")
    if field == "image_event":
        post.platform["news"]["image_event"] = "A different intended scene"
    elif field == "generation_prompt":
        post.platform["image"]["prompt"] = "A different actual prompt"
    else:
        monkeypatch.setenv("VLM_REVIEW_MODEL", "another-reviewer")
    assert not cached_vision_matches(post, cached, "neutral")
