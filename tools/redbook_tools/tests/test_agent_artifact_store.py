import os
from uuid import uuid4

import pytest

from src.agent.artifact_store import AgentArtifactStore
from src.storage.models import Post


@pytest.mark.skipif(os.getenv("REDBOOK_TEST_POSTGRES") != "1", reason="explicit local PostgreSQL integration test")
def test_item_artifacts_survive_new_store_and_are_run_scoped():
    first = AgentArtifactStore()
    first.ensure_schema()
    run_id = "test-artifact-" + uuid4().hex
    post = Post(title="保留的新闻", body="完整正文", platform={"quality_gate": {"deterministic_ok": True}})
    try:
        first.save(run_id, "0", post, phase="generated")
        second = AgentArtifactStore()
        loaded = second.load(run_id, "0")
        assert [item.id for item in loaded] == [post.id]
        assert loaded[0].body == post.body
        assert second.load(run_id, "1") == []
        post.platform["quality_gate"]["vision"] = {"ok": True, "score": 90}
        second.save(run_id, "0", post, phase="approved")
        assert first.load(run_id, "0")[0].platform["quality_gate"]["vision"]["score"] == 90
        assert first.summary(run_id) == [{"job_key": "0", "phase": "approved", "count": 1}]
    finally:
        with first.store.connection() as conn:
            conn.execute("DELETE FROM agent.task_artifacts WHERE run_id=%s", (run_id,))
