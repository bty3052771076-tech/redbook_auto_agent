from dataclasses import dataclass

from src.agent import editorial_agent as agent


@dataclass
class Post:
    id: str


def test_repeated_source_concentration_stops_rebuilding_and_keeps_other_deliveries(tmp_path):
    calls = []
    delivered = []

    def review(job, posts, context):
        if job.kind == "daily_news":
            return []
        calls.append(len(calls) + 1)
        # Safety bound makes the pre-fix failure finite, not an infinite test.
        return {"errors": ["每日AI讯息信源过于集中：2 条仅来自 1 个已识别信源"],
                "approved_post_ids": [], "retryable": len(calls) < 6}

    result = agent.run_editorial_agent(
        [agent.AgentJob("daily_news", "News"), agent.AgentJob("daily_ai_digest", "AI")],
        tools=agent.EditorialAgentTools(lambda job: {}, lambda job, ctx: [Post(job.kind)],
                                      review, lambda job, post, ctx: (delivered.append(post.id) or True, "saved")),
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, retry_delay_s=0),
    )
    assert len(calls) <= 4
    assert result.status == "partial"
    assert delivered == ["daily_news"]
    checkpoint = agent.load_agent_checkpoint(result.checkpoint_path)
    assert checkpoint["job_states"]["0"]["status"] == "completed"
    assert checkpoint["job_states"]["1"]["post_ids"] == ["daily_ai_digest"]
    assert any("SOURCE_REFRESH_REQUIRED" in error for error in result.errors)


def test_repeated_high_impact_material_failure_does_not_retry_forever(tmp_path):
    attempts = []

    def generate(job, context):
        attempts.append(1)
        if len(attempts) > 4:
            raise RuntimeError("STAGE_PENDING_OR_UNKNOWN: safety stop for pre-fix behavior")
        raise RuntimeError("daily ai digest high-impact material insufficient: only 0 remain")

    result = agent.run_editorial_agent(
        [agent.AgentJob("daily_ai_digest", "AI")],
        tools=agent.EditorialAgentTools(lambda job: {}, generate, lambda *a: [], lambda *a: (True, "saved")),
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, retry_delay_s=0),
    )
    assert len(attempts) == 3
    assert result.status == "blocked"
    assert any("SOURCE_REFRESH_REQUIRED" in error for error in result.errors)


def test_explicit_resume_completes_missing_digest_without_regenerating_or_uploading_news(tmp_path):
    jobs = [agent.AgentJob("daily_news", "News"), agent.AgentJob("daily_ai_digest", "AI")]
    generated, delivered = [], []
    ready = [False]
    attempts = []

    def generate(job, context):
        generated.append(job.kind)
        return [Post(job.kind)]

    def review(job, posts, context):
        if job.kind == "daily_news" or ready[0]:
            return []
        attempts.append(1)
        return {"errors": ["SOURCE_DIVERSITY_REQUIRED: 信源过于集中"],
                "approved_post_ids": [], "retryable": len(attempts) < 6}

    tools = agent.EditorialAgentTools(
        lambda job: {}, generate, review,
        lambda job, post, context: (delivered.append(post.id) or True, "saved"),
        load_posts=lambda ids: [Post(post_id) for post_id in ids],
    )
    first = agent.run_editorial_agent(jobs, tools=tools,
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, retry_delay_s=0))
    assert first.status == "partial"
    ready[0] = True
    resumed = agent.run_editorial_agent(jobs, tools=tools,
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, retry_delay_s=0,
                                         resume_from=first.checkpoint_path))
    assert resumed.status == "completed"
    assert generated == ["daily_news", "daily_ai_digest"]
    assert delivered == ["daily_news", "daily_ai_digest"]


def test_first_source_failure_does_not_inherit_unrelated_retry_count(tmp_path):
    calls = []

    def review(job, posts, context):
        calls.append(1)
        if len(calls) <= 3:
            return ["review_error: temporary network timeout"]
        if len(calls) == 4:
            return ["SOURCE_DIVERSITY_REQUIRED: 信源过于集中"]
        return []

    result = agent.run_editorial_agent(
        [agent.AgentJob("daily_ai_digest", "AI")],
        tools=agent.EditorialAgentTools(lambda job: {}, lambda job, context: [Post("digest")],
                                      review, lambda *a: (True, "saved")),
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, retry_delay_s=0),
    )
    assert result.status == "completed"
    assert len(calls) == 5
