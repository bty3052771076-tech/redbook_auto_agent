"""Real PostgreSQL interruption test with deterministic tools and no platform IO."""

import os
from uuid import uuid4

import pytest

from src.agent.artifact_store import AgentArtifactStore
from src.agent.editorial_agent import AgentJob, EditorialAgentConfig, EditorialAgentTools, run_editorial_agent
from src.storage.models import Post


@pytest.mark.skipif(os.getenv("REDBOOK_TEST_POSTGRES") != "1", reason="explicit local PostgreSQL integration test")
def test_restart_inside_review_retains_items_in_real_postgres(tmp_path):
    artifacts = AgentArtifactStore()
    artifacts.ensure_schema()
    run_id = "test-resume-" + uuid4().hex
    rows = {}
    counts = {"generate": 0, "review": 0, "upload": 0}
    fail_once = [True]

    def generate(job, context):
        counts["generate"] += 1
        post = Post(title="first", body="factual content")
        rows[post.id] = post
        artifacts.save(run_id, context["agent_job_key"], post, phase="generated")
        return [post]

    def review(job, posts, context):
        counts["review"] += 1
        retained = {post.id: post for post in artifacts.load(run_id, context["agent_job_key"])}
        posts[:] = list(retained.values())
        if fail_once[0]:
            new_post = Post(title="second", body="another factual event")
            rows[new_post.id] = new_post
            artifacts.save(run_id, context["agent_job_key"], new_post, phase="approved")
            fail_once[0] = False
            raise KeyboardInterrupt("simulated process interruption after per-item commit")
        return {"errors": [], "approved_post_ids": [post.id for post in posts]}

    def upload(job, post, context):
        counts["upload"] += 1
        return True, "saved test receipt"

    tools = EditorialAgentTools(lambda job: {}, generate, review, upload, load_posts=lambda ids: [rows[pid] for pid in ids])
    config = dict(checkpoint_backend="postgres", checkpoint_dir=tmp_path, retry_delay_s=0)
    jobs = [AgentJob("daily_news", "news", count=2)]
    try:
        with pytest.raises(KeyboardInterrupt):
            run_editorial_agent(jobs, tools=tools, config=EditorialAgentConfig(**config), run_id=run_id)
        before_ids = {post.id for post in artifacts.load(run_id, "0")}
        assert len(before_ids) == 2
        result = run_editorial_agent(
            jobs, tools=tools,
            config=EditorialAgentConfig(**config, resume_from=tmp_path / run_id / "checkpoint.json"),
        )
        assert result.status == "completed"
        assert result.completed_jobs == 1
        assert {post.id for post in result.uploaded_posts} == before_ids
        assert counts == {"generate": 1, "review": 2, "upload": 2}
        again = run_editorial_agent(
            jobs, tools=tools,
            config=EditorialAgentConfig(**config, resume_from=result.checkpoint_path),
        )
        assert again.status == "completed"
        assert counts == {"generate": 1, "review": 2, "upload": 2}
    finally:
        with artifacts.store.connection() as conn:
            conn.execute("DELETE FROM agent.task_artifacts WHERE run_id=%s", (run_id,))
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                from psycopg import sql
                conn.execute(sql.SQL("DELETE FROM public.{} WHERE thread_id=%s").format(sql.Identifier(table)), (run_id,))


@pytest.mark.skipif(os.getenv("REDBOOK_TEST_POSTGRES") != "1", reason="explicit local PostgreSQL integration test")
def test_resume_pending_ai_generation_reconciles_previous_news_pause(tmp_path):
    run_id = "test-cross-job-" + uuid4().hex
    posts = {kind: Post(title=kind, body="verified test event")
             for kind in ("daily_news", "daily_ai_digest")}
    calls = []
    resumed = False
    attempts = 0

    def generate(job, context):
        nonlocal attempts
        calls.append(("generate", job.kind))
        if job.kind == "daily_ai_digest" and not resumed:
            attempts += 1
            if attempts == 1:
                raise ValueError("daily ai digest official material insufficient")
            raise KeyboardInterrupt("interrupted with later job failure retained")
        return [posts[job.kind]]

    def upload(job, post, context):
        calls.append(("upload", job.kind))
        return (True, "saved") if resumed else (False, "XHS_WRITE_UNCERTAIN")

    def reconcile(job, ids):
        calls.append(("reconcile", job.kind))
        assert ids == [posts["daily_news"].id]
        return True, "verified absent"

    tools = EditorialAgentTools(lambda job: {}, generate, lambda *a: [], upload,
        load_posts=lambda ids: [p for p in posts.values() if p.id in ids], reconcile_uploads=reconcile)
    jobs = [AgentJob("daily_news", "news"), AgentJob("daily_ai_digest", "AI")]
    config = dict(checkpoint_backend="postgres", checkpoint_dir=tmp_path, retry_delay_s=0)
    with pytest.raises(KeyboardInterrupt):
        run_editorial_agent(jobs, tools=tools, config=EditorialAgentConfig(**config), run_id=run_id)
    resumed = True
    result = run_editorial_agent(jobs, tools=tools, config=EditorialAgentConfig(
        **config, resume_from=tmp_path / run_id / "checkpoint.json"))
    assert result.status == "completed"
    assert result.completed_jobs == 2
    assert calls.count(("generate", "daily_news")) == 1
    assert calls.count(("reconcile", "daily_news")) == 1
    assert calls.count(("upload", "daily_news")) == 2
    assert calls.count(("upload", "daily_ai_digest")) == 1
    assert {p.id for p in result.uploaded_posts} == {p.id for p in posts.values()}
