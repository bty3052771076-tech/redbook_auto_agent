from __future__ import annotations

import socket
import threading

import pytest

from src.news.daily_news import NewsItem
from src.storage.models import Post
from src.workflow import create_post
from src.workflow.performance import PerformancePolicy


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("callback unit tests must not use the network")

    monkeypatch.setattr(socket.socket, "connect", denied)


def _setup_runner(monkeypatch):
    titles = ["海上船员获救", "海上船员获救", "监管机构调查平台", "应被淘汰的候选"]
    picks = [NewsItem(title=title, url=f"https://example.invalid/{i}", description=title) for i, title in enumerate(titles)]
    saved, revisions, worker_threads = [], [], []
    monkeypatch.setattr(create_post, "_prefetch_daily_news_context", lambda items, **kw: {
        i: (item, {}, {}, None) for i, item in enumerate(items, 1)
    })
    monkeypatch.setattr(create_post, "infer_llm_provider", lambda _: "test")
    monkeypatch.setattr(create_post, "save_post", lambda post: saved.append(post.model_copy(deep=True)))
    monkeypatch.setattr(create_post, "save_revision", lambda revision: revisions.append(revision))

    def prepare(**kw):
        worker_threads.append(threading.get_ident())
        if kw["candidate_index"] == 4:
            return create_post._DailyNewsCandidateResult(candidate_index=4, status="skipped", reason="bad_copy")
        item = kw["picked"]
        return create_post._DailyNewsCandidateResult(
            candidate_index=kw["candidate_index"], status="success", picked=item,
            post=Post(title=item.title, body=f"内容：\n{item.title}。"), asset_paths=[],
        )

    monkeypatch.setattr(create_post, "_prepare_daily_news_candidate", prepare)
    return picks, saved, revisions, worker_threads


def _run(picks, callback=None, *, mode="balanced", target=3):
    return create_post._run_parallel_daily_news_candidates(
        picks=picks, cfgs=[], asset_paths=[], copy_assets=False, auto_image_enabled=False,
        prompt_norm="", viewpoint_norm="", target_count=target, single_material_mode=True,
        base_meta={}, progress_callback=None, post_quality_callback=None,
        required_china_count=0, required_international_conflict_count=0,
        performance_policy=PerformancePolicy.from_value(mode), post_saved_callback=callback,
    )


@pytest.mark.parametrize("mode", ["balanced", "speed"])
def test_callback_runs_on_coordinator_after_acceptance_and_save(monkeypatch, mode):
    picks, saved, revisions, worker_threads = _setup_runner(monkeypatch)
    accepted = []
    coordinator = threading.get_ident()

    def on_saved(post):
        assert threading.get_ident() == coordinator
        assert saved[-1].id == post.id
        assert len(revisions) == len(accepted)  # callback precedes revision save
        assert post.platform["news"]["pick_index"] == len(accepted) + 1
        accepted.append(post)

    with pytest.raises(create_post.PartialDailyNewsError) as caught:
        _run(picks, on_saved, mode=mode)

    assert len(saved) == len(accepted) == len(revisions) == 2
    assert {post.title for post in accepted} == {"海上船员获救", "监管机构调查平台"}
    assert [post.id for post in caught.value.posts] == [post.id for post in accepted]
    assert caught.value.requested_count == 3
    assert worker_threads and all(thread != coordinator for thread in worker_threads)


def test_callback_failure_retains_the_just_saved_post(monkeypatch):
    picks, saved, revisions, _ = _setup_runner(monkeypatch)

    def fail(post):
        raise RuntimeError("ledger temporarily unavailable")

    with pytest.raises(create_post.PartialDailyNewsError, match="completion recording failed") as caught:
        _run(picks, fail)

    assert len(caught.value.posts) == 1
    assert len(saved) >= 1
    assert caught.value.posts[0].id == saved[0].id
    retained = saved[1:]
    assert all(post.platform["batch_selection"]["status"] == "retained_after_stop" for post in retained)
    assert {revision.post_id for revision in revisions} == {post.id for post in retained}
    assert caught.value.posts[0].id not in {revision.post_id for revision in revisions}
    assert isinstance(caught.value.__cause__, RuntimeError)


def test_default_callback_remains_optional(monkeypatch):
    picks, saved, revisions, _ = _setup_runner(monkeypatch)
    posts = _run(picks, target=2)
    assert len(posts) == 2
    selected_ids = {post.id for post in posts}
    assert selected_ids <= {post.id for post in saved}
    assert selected_ids <= {revision.post_id for revision in revisions}
    assert len(saved) == len(revisions)
    assert all(post.platform["batch_selection"]["status"] == "retained_after_stop"
               for post in saved if post.id not in selected_ids)


def test_public_entry_forwards_callback_only_to_final_acceptance(monkeypatch):
    picks, _, _, _ = _setup_runner(monkeypatch)
    recorded = {}
    callback = lambda post: None
    monkeypatch.setattr(create_post, "load_llm_configs", lambda: [])
    monkeypatch.setattr(create_post, "daily_news_soft_preferences_enabled", lambda: True)

    def fetch(*args, **kwargs):
        assert "post_saved_callback" not in kwargs
        return picks, {}

    def run(**kwargs):
        recorded.update(kwargs)
        return []

    monkeypatch.setattr(create_post, "_fetch_daily_news_candidates_for_upload", fetch)
    monkeypatch.setattr(create_post, "_supervise_daily_news_candidates", lambda items, **kwargs: (items, {"status": "success"}))
    monkeypatch.setattr(create_post, "_run_parallel_daily_news_candidates", run)
    create_post.create_daily_news_posts(asset_paths=[], count=1, auto_image=False, post_saved_callback=callback)
    assert recorded["post_saved_callback"] is callback
