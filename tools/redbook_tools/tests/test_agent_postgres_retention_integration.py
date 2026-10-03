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
