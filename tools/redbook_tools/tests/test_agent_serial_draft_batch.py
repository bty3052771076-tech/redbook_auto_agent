import pytest

from apps import cli
from src.agent.editorial_agent import AgentJob
from src.storage.models import Post


@pytest.mark.parametrize("reason", ["XHS_WRITE_UNCERTAIN", "XHS_LOGIN_REQUIRED", "XHS_RISK_BLOCKED"])
def test_serial_draft_batch_stops_before_next_external_write(reason):
    posts = [Post(title=str(i)) for i in range(3)]
    called = []

    def upload(job, post, context):
        called.append(post.id)
        return (True, "saved") if len(called) == 1 else (False, reason)

    result = cli._upload_agent_drafts_serially(AgentJob("daily_news", "news"), posts, {}, upload)
    assert called == [post.id for post in posts[:2]]
    assert result[posts[0].id] == (True, "saved")
    assert result[posts[2].id][0] is False
    assert reason in result[posts[2].id][1]


def test_serial_draft_batch_keeps_independent_nonterminal_results():
    posts = [Post(title=str(i)) for i in range(3)]
    called = []

    def upload(job, post, context):
        called.append(post.id)
        return post is not posts[1], "local validation failed" if post is posts[1] else "saved"

    result = cli._upload_agent_drafts_serially(AgentJob("daily_news", "news"), posts, {}, upload)
    assert called == [post.id for post in posts]
    assert [result[post.id][0] for post in posts] == [True, False, True]


def test_serial_draft_batch_exception_is_not_blindly_retried():
    posts = [Post(title=str(i)) for i in range(2)]
    called = []

    def upload(job, post, context):
        called.append(post.id)
        raise ConnectionError("browser response lost")

    result = cli._upload_agent_drafts_serially(AgentJob("daily_news", "news"), posts, {}, upload)
    assert called == [posts[0].id]
    assert all("XHS_WRITE_UNCERTAIN" in value[1] for value in result.values())
