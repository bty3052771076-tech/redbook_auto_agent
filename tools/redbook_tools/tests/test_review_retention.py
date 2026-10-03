from types import SimpleNamespace
from threading import Barrier, Lock

from apps import cli
from src.storage.models import AssetInfo, Post, PostStatus
from src.workflow.review_cache import cached_vision_matches, stamp_vision_cache


def _post(tmp_path, name="one", *, good=True):
    path = tmp_path / (name + ".png")
    path.write_bytes(b"image bytes")
    return Post(id=name, title=name, body="full content", assets=[AssetInfo(path=str(path))],
                platform={"news": {"picked": {"title": name, "url": "https://example.com/" + name}},
                          "quality_gate": {"deterministic_ok": True, "issues": [],
                                           "vision": {"ok": good, "score": 85 if good else 35}}})


def test_incomplete_batch_retains_qualified_post(tmp_path, monkeypatch):
    good = _post(tmp_path)
    bad = _post(tmp_path, "bad", good=False)
    monkeypatch.setattr(cli, "save_post", lambda post: None)
    cli._mark_visual_batch_incomplete([good, bad], requested_count=2, reason="one missing")
    assert good.status == PostStatus.draft
    assert good.platform["batch_selection"]["status"] == "retained_qualified"
    assert bad.status == PostStatus.failed


def test_visual_cache_invalidates_changed_text_and_actual_image(tmp_path):
    post = _post(tmp_path)
    cached = stamp_vision_cache(post, {"ok": True, "score": 85}, "neutral")
    assert cached_vision_matches(post, cached, "neutral")
    assert not cached_vision_matches(post, cached, "different")
    post.body += "changed"
    assert not cached_vision_matches(post, cached, "neutral")
    post.body = "full content"
    (tmp_path / "one.png").write_bytes(b"other image")
    assert not cached_vision_matches(post, cached, "neutral")


def test_one_bad_text_does_not_skip_review_of_other_posts(tmp_path, monkeypatch):
    good = _post(tmp_path)
    bad = _post(tmp_path, "bad")
    report = SimpleNamespace(issues=[SimpleNamespace(post_id="bad", code="thin_content", message="bad body")])
    monkeypatch.setattr(cli, "validate_post_batch", lambda *args, **kwargs: report)
    monkeypatch.setattr(cli, "list_posts", lambda: [])
    monkeypatch.setattr(cli, "save_post", lambda post: None)
    monkeypatch.setattr(cli, "configured_vision_review_model", lambda: True)
    monkeypatch.setattr(cli, "load_vision_review_config", lambda: SimpleNamespace(provider="test", model="vision"))
    reviewed = []
    def review(post, **kwargs):
        reviewed.append(post.id)
        return cli.VisionReviewResult(ok=True, score=85, issues=(), retry_prompt="", provider="test", model="vision")
    monkeypatch.setattr(cli, "review_post_image", review)
    errors = cli._run_auto_quality_gate([good, bad], expected_count=2, evaluation_viewpoint="neutral", require_vision=True)
    assert errors == ["bad body"]
    assert reviewed == ["one"]
    assert good.platform["quality_gate"]["vision"]["score"] == 85


def test_parallel_review_is_bounded_and_reused(tmp_path, monkeypatch):
    posts = [_post(tmp_path, str(i)) for i in range(4)]
    monkeypatch.setattr(cli, "validate_post_batch", lambda *args, **kwargs: SimpleNamespace(issues=[]))
    monkeypatch.setattr(cli, "list_posts", lambda: [])
    monkeypatch.setattr(cli, "save_post", lambda post: None)
    monkeypatch.setattr(cli, "configured_vision_review_model", lambda: True)
    monkeypatch.setattr(cli, "load_vision_review_config", lambda: SimpleNamespace(provider="test", model="vision"))
    barrier = Barrier(2)
    lock = Lock()
    active = [0]
    maximum = [0]
    calls = []
    def review(post, **kwargs):
        with lock:
            active[0] += 1
            maximum[0] = max(maximum[0], active[0])
            calls.append(post.id)
        barrier.wait(timeout=5)
        with lock:
            active[0] -= 1
        return cli.VisionReviewResult(ok=True, score=85, issues=(), retry_prompt="", provider="test", model="vision")
    monkeypatch.setattr(cli, "review_post_image", review)
    options = dict(expected_count=4, evaluation_viewpoint="neutral", require_vision=True, reuse_vision_results=True)
    assert cli._run_auto_quality_gate(posts, **options) == []
    assert maximum[0] == 2
    assert len(calls) == 4
    assert cli._run_auto_quality_gate(posts, **options) == []
    assert len(calls) == 4


def test_cached_two_failed_images_do_not_pass_or_call_model(tmp_path, monkeypatch):
    post = _post(tmp_path, good=False)
    post.platform["vision_selection"] = {
        "strategy": "best_of_two", "candidate_count": 2, "selected_index": 2,
        "selected_score": 45, "alternate_score": 35,
    }
    post.platform["quality_gate"]["vision"] = stamp_vision_cache(post, {
        "ok": False, "score": 45, "selection_mode": "best_of_two",
        "history": [{"ok": False, "score": 35}, {"ok": False, "score": 45}],
    }, "neutral")
    monkeypatch.setattr(cli, "validate_post_batch", lambda *args, **kwargs: SimpleNamespace(issues=[]))
    monkeypatch.setattr(cli, "list_posts", lambda: [])
    monkeypatch.setattr(cli, "save_post", lambda post: None)
    def forbidden(*args, **kwargs):
        raise AssertionError("completed candidates must not call model again")
    monkeypatch.setattr(cli, "review_post_image", forbidden)
    errors = cli._run_auto_quality_gate([post], expected_count=1, evaluation_viewpoint="neutral", require_vision=True, reuse_vision_results=True)
    assert errors
    assert "45" in errors[0]
