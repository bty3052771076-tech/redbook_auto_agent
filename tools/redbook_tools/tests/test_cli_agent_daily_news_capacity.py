"""CLI Partial capacity propagation with a detached ledger and no service IO."""

import socket
from types import SimpleNamespace

import psycopg
import pytest
from typer.testing import CliRunner

from apps import cli
from src.agent.editorial_agent import AgentJob
from src.storage.models import Post


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("capacity adapter tests must not contact services or run preflight")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(cli, "_prepare_auto_pipeline", forbidden)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('MODEL_PLATFORMS_DIR', str(tmp_path / 'model-platforms'))
    monkeypatch.setenv('MODEL_PLATFORMS_NAMESPACE', 'agent')
    monkeypatch.setenv('MINIMAX_TOKEN_PLAN_API_KEY', 'offline-test-not-a-real-key')
    monkeypatch.setenv('AGENT_LLM_PROVIDER', 'minimax')
    retained = {}
    saves = []

    class Ledger:
        def ensure_schema(self):
            pass

        def save(self, run_id, job_key, post, *, phase):
            saves.append((run_id, job_key, post.id, phase))
            retained[post.id] = post.model_copy(deep=True)

        def load(self, run_id, job_key):
            return [post.model_copy(deep=True) for post in retained.values()]

    captured = {}
    monkeypatch.setattr(cli.KnowledgeStore, "from_env", lambda: object())
    monkeypatch.setattr(cli, "AgentArtifactStore", lambda store: Ledger())
    monkeypatch.setattr(cli, "prepare_local_knowledge_snapshot", lambda **kw: {"knowledge_status": "ready"})

    def capture(jobs, *, tools, **kwargs):
        captured["tools"] = tools
        return SimpleNamespace(status="completed", completed_jobs=0, requested_jobs=0,
                               uploaded_posts=[], checkpoint_path="unused", errors=[])

    monkeypatch.setattr(cli, "run_editorial_agent", capture)
    result = CliRunner().invoke(cli.app, [
        "agent", "--run-id", "test-partial-capacity", "--no-preflight", "--no-refresh-quotas", "--skill-mode", "off",
    ])
    assert result.exit_code == 0, result.output
    context = {"agent_run_id": "test-partial-capacity", "agent_job_key": "0"}
    return SimpleNamespace(tools=captured["tools"], retained=retained, saves=saves, context=context)


@pytest.mark.parametrize("reason", [
    "模型免费额度已耗尽",
    "模型额度不足",
    "模型订阅 Token Plan 用量上限已达到，未切换付费模型",
    "insufficient_quota",
    "INSUFFICIENT BALANCE",
])
def test_partial_capacity_retains_all_posts_before_reraising_original_cause(adapter, monkeypatch, reason):
    posts = [Post(id=f"retained-{index}", title=f"Event {index}", body="Retained factual content") for index in range(2)]
    message = f"daily news generated=2/10; {reason}"
    original = cli.PartialDailyNewsError(message, posts=posts, requested_count=10, failed_count=8)
    calls = []

    def partial(**kwargs):
        calls.append(kwargs["count"])
        kwargs["post_saved_callback"](posts[0])
        raise original

    monkeypatch.setattr(cli, "create_daily_news_posts", partial)
    with pytest.raises(cli.PartialDailyNewsError) as caught:
        adapter.tools.generate(AgentJob("daily_news", "News", count=10), adapter.context)

    assert caught.value is original
    assert str(caught.value) == message
    assert caught.value.requested_count == 10
    assert caught.value.failed_count == 8
    assert [post.id for post in caught.value.posts] == [post.id for post in posts]
    assert adapter.retained == {post.id: post for post in posts}
    assert {saved[2] for saved in adapter.saves} == {post.id for post in posts}
    assert all(saved[:2] == ("test-partial-capacity", "0") and saved[3] == "generated" for saved in adapter.saves)
    assert calls == [10]


def test_non_capacity_partial_keeps_existing_return_and_reuse_behavior(adapter, monkeypatch):
    post = Post(id="ordinary-partial", title="Available event", body="Retained factual content")
    original = cli.PartialDailyNewsError(
        "daily news candidates exhausted after quality filtering", posts=[post], requested_count=10, skipped_quality_count=9,
    )
    calls = []

    def partial(**kwargs):
        calls.append(kwargs["count"])
        raise original

    monkeypatch.setattr(cli, "create_daily_news_posts", partial)
    job = AgentJob("daily_news", "News", count=10)
    assert adapter.tools.generate(job, adapter.context) == [post]
    assert adapter.retained == {post.id: post}
    assert adapter.saves == [("test-partial-capacity", "0", post.id, "generated")]
    assert adapter.tools.generate(job, adapter.context) == [post]
    assert calls == [10]
