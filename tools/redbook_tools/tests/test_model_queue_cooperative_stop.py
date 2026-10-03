"""Cooperative-stop regression cases. Written without executing them."""

from concurrent.futures import CancelledError
import json
import socket
from threading import Barrier, Event, Thread

import pytest

from src.news.daily_news import NewsItem
from src.storage.files import save_post, save_revision
from src.storage.models import AssetInfo, Post
from src.workflow import create_post
from src.workflow.model_queues import ModelWorkQueues
from src.workflow.performance import PerformancePolicy


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("cooperative-stop tests must not use the network")

    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.mark.parametrize("lane", ["llm", "image"])
def test_stop_rejects_new_and_queued_calls_but_keeps_running_result(lane):
    queues = ModelWorkQueues(llm_workers=1, image_workers=1)
    submit = getattr(queues, "submit_" + lane)
    started, release = Event(), Event()
    calls = []

    def running():
        started.set()
        assert release.wait(10)
        return "completed artifact"

    try:
        first = submit(running)
        assert started.wait(10)
        pending = submit(calls.append, "must not start")
        queues.request_stop()
        with pytest.raises(CancelledError):
            submit(calls.append, "must not enqueue")
        release.set()
        assert first.result(timeout=10) == "completed artifact"
        with pytest.raises(CancelledError):
            pending.result(timeout=10)
        assert calls == []
    finally:
        release.set()
        queues.request_stop()
        queues.close()


def test_stopped_close_cancels_both_lanes_before_waiting_for_running_calls():
    queues = ModelWorkQueues(llm_workers=1, image_workers=1)
    release = Event()
    started = [Event(), Event()]
    cancelled = [Event(), Event()]
    calls = []

    def running(index):
        started[index].set()
        assert release.wait(10)
        return index

    closer = None
    try:
        first = [queues.submit_llm(running, 0), queues.submit_image(running, 1)]
        assert all(event.wait(10) for event in started)
        pending = [queues.submit_llm(calls.append, "llm"),
                   queues.submit_image(calls.append, "image")]
        for future, event in zip(pending, cancelled):
            future.add_done_callback(lambda _, event=event: event.set())
        queues.request_stop()
        closer = Thread(target=queues.close)
        closer.start()
        assert all(event.wait(10) for event in cancelled)
        assert all(future.cancelled() for future in pending)
        assert closer.is_alive()
        release.set()
        closer.join(timeout=10)
        assert not closer.is_alive()
        assert [future.result(timeout=10) for future in first] == [0, 1]
        assert calls == []
    finally:
        release.set()
        queues.request_stop()
        if closer is not None:
            closer.join(timeout=10)
        queues.close()


def test_normal_close_drains_queued_calls_without_requesting_stop():
    queues = ModelWorkQueues(llm_workers=1, image_workers=1)
    started, release = Event(), Event()
    calls = []

    def first():
        started.set()
        assert release.wait(10)
        return "first"

    try:
        current = queues.submit_llm(first)
        assert started.wait(10)
        pending = queues.submit_llm(calls.append, "queued result")
        release.set()
        queues.close()
        assert current.result() == "first"
        assert pending.result() is None
        assert calls == ["queued result"]
        assert not queues.stopped
    finally:
        release.set()
        queues.close()


def _runner(monkeypatch, tmp_path, *, size, mode="speed", target=1):
    picks = [NewsItem(title=f"candidate {index}", url=f"https://example.invalid/{index}",
                      description=f"candidate {index}") for index in range(1, size + 1)]
    data = tmp_path / "data"
    queues_created = []
    stopped = Event()
    release = Event()

    class ObservedQueues(ModelWorkQueues):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            queues_created.append(self)

        def request_stop(self):
            super().request_stop()
            stopped.set()
            release.set()

    monkeypatch.setattr(create_post, "ModelWorkQueues", ObservedQueues)
    monkeypatch.setattr(create_post, "_daily_news_coordinator_workers", lambda: max(2, size))
    monkeypatch.setattr(create_post, "_daily_news_candidate_batch_indices",
                        lambda indices, **kwargs: list(indices[:kwargs.get("batch_size", size)]))
    monkeypatch.setattr(create_post, "_prefetch_daily_news_context", lambda items, **kwargs: {
        index: (item, {}, {}, None) for index, item in enumerate(items, 1)
    })
    monkeypatch.setattr(create_post, "_daily_news_body_fact_key", lambda _: "")
    monkeypatch.setattr(create_post, "save_post", lambda post: save_post(post, data))
    monkeypatch.setattr(create_post, "save_revision", lambda revision: save_revision(revision, data))

    def run(callback=None):
        return create_post._run_parallel_daily_news_candidates(
            picks=picks, cfgs=[], asset_paths=[], copy_assets=False,
            auto_image_enabled=False, prompt_norm="", viewpoint_norm="",
            target_count=target, single_material_mode=True, base_meta={},
            progress_callback=None, post_quality_callback=None,
            required_china_count=0, required_international_conflict_count=0,
            performance_policy=PerformancePolicy.from_value(mode),
            post_saved_callback=callback,
        )

    return run, data, queues_created, stopped, release


def _success(kwargs, post=None):
    return create_post._DailyNewsCandidateResult(
        candidate_index=kwargs["candidate_index"], status="success",
        picked=kwargs["picked"], post=post or Post(title=kwargs["picked"].title, body="factual body"),
        asset_paths=[], draft={"title": kwargs["picked"].title, "body": "factual body"},
    )


def test_target_stop_retains_completed_surplus_without_acceptance_callbacks(monkeypatch, tmp_path):
    run, data, queues, stopped, _ = _runner(monkeypatch, tmp_path, size=4)
    barrier = Barrier(4)
    accepted = []

    def prepare(**kwargs):
        post = kwargs["model_queues"].submit_llm(
            Post, title=kwargs["picked"].title, body="factual body",
        ).result()
        barrier.wait(timeout=10)
        return _success(kwargs, post)

    monkeypatch.setattr(create_post, "_prepare_daily_news_candidate", prepare)
    posts = run(accepted.append)
    snapshots = [json.loads(path.read_text(encoding="utf-8"))
                 for path in (data / "posts").glob("*/post.json")]
    assert len(posts) == len(accepted) == 1
    assert len(snapshots) == 4
    assert stopped.is_set() and queues[0].stopped
    surplus = [row for row in snapshots if row["id"] != posts[0].id]
    assert all(row["platform"]["batch_selection"]["status"] == "retained_after_stop" for row in surplus)
    assert all(row["platform"]["batch_selection"]["reason"] == "target_reached" for row in surplus)


def test_cooperative_retention_never_runs_vlm_rejection_diagnostics(monkeypatch, tmp_path):
    run, data, _, stopped, _ = _runner(monkeypatch, tmp_path, size=2)
    barrier = Barrier(2)
    accepted = []

    def prepare(**kwargs):
        result = _success(kwargs)
        barrier.wait(timeout=10)
        return result

    def unexpected_diagnostics(**kwargs):
        raise AssertionError("retaining an unselected draft is not a VLM rejection")

    monkeypatch.setattr(create_post, "_prepare_daily_news_candidate", prepare)
    monkeypatch.setattr(create_post, "_daily_news_reject_diagnostics", unexpected_diagnostics)
    posts = run(accepted.append)
    assert len(posts) == len(accepted) == 1
    assert stopped.is_set()
    rows = [json.loads(path.read_text(encoding="utf-8"))
            for path in (data / "posts").glob("*/post.json")]
    retained = [row for row in rows if row["id"] != posts[0].id]
    assert len(retained) == 1
    assert retained[0]["platform"]["batch_selection"]["status"] == "retained_after_stop"
    assert "quality_rejection" not in retained[0]["platform"].get("news", {})
    assert retained[0]["uploaded"] is False
    assert len(list((data / "posts" / retained[0]["id"] / "revisions").glob("*.json"))) == 1


@pytest.mark.parametrize("mode", ["balanced", "speed"])
def test_target_stop_does_not_wait_for_other_candidates_to_enter_next_lane(monkeypatch, tmp_path, mode):
    run, data, queues, stopped, release = _runner(monkeypatch, tmp_path, size=2, mode=mode)
    running = Event()
    calls = []

    def prepare(**kwargs):
        if kwargs["candidate_index"] == 1:
            assert running.wait(10)
            return _success(kwargs)
        running.set()
        assert release.wait(10)
        try:
            kwargs["model_queues"].submit_llm(calls.append, "late request").result()
        except CancelledError:
            return create_post._DailyNewsCandidateResult(
                candidate_index=2, status="cancelled", picked=kwargs["picked"],
                draft={"title": "finished text", "body": "completed before stop"},
                reason="model_work_stopped",
            )
        raise AssertionError("a later model stage must not start after acceptance")

    monkeypatch.setattr(create_post, "_prepare_daily_news_candidate", prepare)
    try:
        posts = run()
        assert len(posts) == 1 and stopped.is_set() and queues[0].stopped
        assert calls == []
        assert len(list((data / "posts").glob("*/post.json"))) == 2
    finally:
        release.set()


@pytest.mark.parametrize("target", [1, 2])
def test_worker_saved_final_image_survives_retention_and_acceptance(monkeypatch, tmp_path, target):
    run, data, _, _, _ = _runner(monkeypatch, tmp_path, size=2, mode="balanced", target=target)
    barrier = Barrier(2)
    final_path = tmp_path / "final-image.png"
    final_path.write_bytes(b"completed image bytes")
    image_post_ids = []
    callbacks = []

    def prepare(**kwargs):
        result = _success(kwargs)
        if kwargs["candidate_index"] == 2:
            post = result.post
            post.assets = [AssetInfo(path=str(final_path), validated=True, sha256="final-image-identity")]
            post.platform["image_lineage"] = {"first_image": "original-image-identity"}
            create_post.save_post(post)
            image_post_ids.append(post.id)
            result.asset_paths = [tmp_path / "superseded-image.png"]
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(create_post, "_prepare_daily_news_candidate", prepare)
    posts = run(callbacks.append)
    assert len(posts) == len(callbacks) == target
    snapshot = json.loads((data / "posts" / image_post_ids[0] / "post.json").read_text(encoding="utf-8"))
    assert snapshot["assets"][0]["path"] == str(final_path)
    assert snapshot["assets"][0]["validated"] is True
    assert snapshot["assets"][0]["sha256"] == "final-image-identity"
    assert snapshot["platform"]["image_lineage"]["first_image"] == "original-image-identity"
    if target == 1:
        assert snapshot["platform"]["batch_selection"]["status"] == "retained_after_stop"
        assert image_post_ids[0] not in {post.id for post in posts}
    else:
        assert image_post_ids[0] in {post.id for post in posts}


@pytest.mark.parametrize("mode", ["balanced", "speed"])
def test_capacity_stop_prevents_next_lane_and_retains_finished_text(monkeypatch, tmp_path, mode):
    run, data, queues, stopped, release = _runner(monkeypatch, tmp_path, size=2, mode=mode, target=2)
    running = Event()
    image_calls = []

    def prepare(**kwargs):
        if kwargs["candidate_index"] == 2:
            assert running.wait(10)
            return create_post._DailyNewsCandidateResult(
                candidate_index=2, status="failed", reason="llm_request_failed",
                error="free quota exhausted",
            )

        def draft_request():
            running.set()
            assert release.wait(10)
            return {"title": "completed text", "body": "retained factual body"}

        draft = kwargs["model_queues"].submit_llm(draft_request).result()
        try:
            kwargs["model_queues"].submit_image(image_calls.append, "must not start").result()
        except CancelledError:
            return create_post._DailyNewsCandidateResult(
                candidate_index=1, status="cancelled", picked=kwargs["picked"],
                draft=draft, reason="model_work_stopped",
            )
        raise AssertionError("image call should have been stopped")

    monkeypatch.setattr(create_post, "_prepare_daily_news_candidate", prepare)
    try:
        with pytest.raises(RuntimeError, match="created only"):
            run()
        rows = [json.loads(path.read_text(encoding="utf-8"))
                for path in (data / "posts").glob("*/post.json")]
        assert stopped.is_set() and queues[0].stopped
        assert image_calls == []
        assert len(rows) == 1
        assert rows[0]["title"] == "completed text"
        assert rows[0]["body"] == "retained factual body"
        assert rows[0]["assets"] == []
        assert rows[0]["platform"]["batch_selection"]["reason"] == "provider_capacity_exhausted"
    finally:
        release.set()


def test_late_worker_exception_after_capacity_stop_is_not_counted_or_retried(monkeypatch, tmp_path):
    run, _, queues, stopped, release = _runner(monkeypatch, tmp_path, size=3, mode="balanced", target=4)
    running = Event()
    retries = []

    def prepare(**kwargs):
        index = kwargs["candidate_index"]
        if index == 1:
            return _success(kwargs)
        if index == 2:
            assert running.wait(10)
            return create_post._DailyNewsCandidateResult(
                candidate_index=2, status="failed", reason="llm_request_failed", error="quota exhausted",
            )
        running.set()
        assert release.wait(10)
        raise RuntimeError("secondary exception after stop")

    monkeypatch.setattr(create_post, "_prepare_daily_news_candidate", prepare)
    monkeypatch.setattr(create_post, "_schedule_daily_news_candidate_retry",
                        lambda *args: retries.append(args) or False)
    try:
        with pytest.raises(create_post.PartialDailyNewsError) as caught:
            run()
        assert len(caught.value.posts) == 1
        assert caught.value.failed_count == 1
        assert retries == []
        assert stopped.is_set() and queues[0].stopped
    finally:
        release.set()


@pytest.mark.parametrize("error", ["HTTP 429 retry later", "Token Plan api_key missing", "connection reset"])
def test_non_capacity_failures_preserve_normal_exit_semantics(monkeypatch, tmp_path, error):
    run, data, queues, stopped, _ = _runner(monkeypatch, tmp_path, size=2, mode="balanced", target=3)

    def prepare(**kwargs):
        if kwargs["candidate_index"] == 1:
            return create_post._DailyNewsCandidateResult(
                candidate_index=1, status="failed", reason="permanent_input_error", error=error,
            )
        return _success(kwargs)

    monkeypatch.setattr(create_post, "_prepare_daily_news_candidate", prepare)
    with pytest.raises(create_post.PartialDailyNewsError) as caught:
        run()
    assert len(caught.value.posts) == 1
    assert not stopped.is_set() and not queues[0].stopped
    assert len(list((data / "posts").glob("*/post.json"))) == 1


def test_coordinator_callback_exception_stops_and_preserves_other_completed_posts(monkeypatch, tmp_path):
    run, data, queues, stopped, _ = _runner(monkeypatch, tmp_path, size=2, mode="balanced", target=2)
    barrier = Barrier(2)
    callbacks = []

    def prepare(**kwargs):
        result = _success(kwargs)
        barrier.wait(timeout=10)
        return result

    def fail(post):
        callbacks.append(post.id)
        raise RuntimeError("artifact ledger unavailable")

    monkeypatch.setattr(create_post, "_prepare_daily_news_candidate", prepare)
    with pytest.raises(create_post.PartialDailyNewsError, match="completion recording failed") as caught:
        run(fail)
    assert stopped.is_set() and queues[0].stopped
    assert len(caught.value.posts) == len(callbacks) == 1
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert len(list((data / "posts").glob("*/post.json"))) == 2
