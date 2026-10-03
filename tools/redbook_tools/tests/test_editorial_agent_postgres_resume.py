from contextlib import contextmanager, nullcontext
from dataclasses import dataclass

from langgraph.checkpoint.memory import InMemorySaver

from src.agent import editorial_agent
from src.agent.editorial_agent import AgentJob, EditorialAgentConfig, EditorialAgentTools


@dataclass
class Post:
    id: str = "pending-post"


def test_postgres_resume_refreshes_attempt_clock_without_restarting_jobs(tmp_path, monkeypatch):
    saver = InMemorySaver()

    @contextmanager
    def checkpointer():
        yield saver

    monkeypatch.setattr("src.agent.postgres_checkpoint.postgres_checkpointer", checkpointer)
    monkeypatch.setattr("src.agent.artifact_store.AgentArtifactStore.lease", lambda self, run_id: nullcontext(lambda: None))
    build_graph = editorial_agent._build_graph
    first_invocation = [True]

    def interrupted_graph(**kwargs):
        graph = build_graph(**kwargs)
        invoke = graph.invoke

        def run(*args, **options):
            if first_invocation[0]:
                first_invocation[0] = False
                options["interrupt_before"] = ["review"]
            return invoke(*args, **options)

        graph.invoke = run
        return graph

    monkeypatch.setattr(editorial_agent, "_build_graph", interrupted_graph)
    clock = [1000.0]
    monkeypatch.setattr(editorial_agent.time, "time", lambda: clock[0])
    calls = []

    def review(job, posts, context):
        calls.append("review")
        assert [post.id for post in posts] == ["pending-post"]
        return []

    tools = EditorialAgentTools(
        lambda job: (calls.append("sync") or {}),
        lambda job, context: (calls.append("generate") or [Post()]),
        review,
        lambda job, post, context: (calls.append("upload") or (True, "saved")),
    )
    job = AgentJob("daily_news", "Daily news")
    editorial_agent.run_editorial_agent(
        [job], tools=tools,
        config=EditorialAgentConfig(checkpoint_dir=tmp_path, checkpoint_backend="postgres", max_elapsed_s=7200),
        run_id="interrupted-pg",
    )

    original = saver.get_tuple({"configurable": {"thread_id": "interrupted-pg"}})
    assert original.checkpoint["channel_values"]["started_at"] == 1000.0
    clock[0] = 100000.0
    result = editorial_agent.run_editorial_agent(
        [job], tools=tools,
        config=EditorialAgentConfig(
            checkpoint_dir=tmp_path, checkpoint_backend="postgres", max_elapsed_s=7200,
            resume_from=tmp_path / "interrupted-pg" / "checkpoint.json",
        ),
    )

    assert result.status == "completed"
    assert result.completed_jobs == 1
    assert calls == ["sync", "sync", "generate", "review", "upload"]
    checkpoint = editorial_agent.load_agent_checkpoint(result.checkpoint_path)
    assert checkpoint["started_at"] == 100000.0
    assert checkpoint["root_started_at"] == 1000.0
    assert checkpoint["resume_count"] == 1
    assert checkpoint["uploaded_post_ids"] == ["pending-post"]
