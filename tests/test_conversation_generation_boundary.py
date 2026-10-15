import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from apps import cli
from src.agent.artifact_store import AgentArtifactStore
from src.agent.conversation_control import ConversationPaused
from src.storage.models import Post
from test_conversation_goals import goal_store


def test_cli_saved_post_boundary_retains_artifact_then_honors_pause(monkeypatch, tmp_path):
    goals, ns, cid, goal = goal_store()
    goals.bind_attempt(ns, goal['goal_id'], 'saved-boundary')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('MODEL_PLATFORMS_DIR', str(tmp_path / 'models'))
    monkeypatch.setenv('MODEL_PLATFORMS_NAMESPACE', 'agent')
    monkeypatch.setenv('MINIMAX_TOKEN_PLAN_API_KEY', 'offline-test-not-a-real-key')
    monkeypatch.delenv('AGENT_WEB_ATTEMPT_ID', raising=False)
    plan = {**goal['plan'], 'goal_context': {'namespace': ns, 'goal_id': goal['goal_id'],
                'goal_revision': 1, 'attempt_id': 'saved-boundary'}}
    path = tmp_path / 'data/web_gui/conversations' / cid / 'plans/plan.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(plan), encoding='utf-8')
    monkeypatch.setattr(cli, 'prepare_local_knowledge_snapshot', lambda **kw: {'knowledge_status': 'ready'})
    first = Post(title='已生成并保存的事件', body='受控材料')
    def generate(**kwargs):
        goals.enqueue_turn(ns, cid, 'pause-source', {'kind': 'pause_goal', 'goal_id': goal['goal_id']}, 1, 'pause')
        try:
            kwargs['post_saved_callback'](first)
        except ConversationPaused as exc:
            raise cli.PartialDailyNewsError(str(exc), posts=[first], requested_count=10) from exc
        return [first, Post(title='不应再接纳第二篇', body='受控材料')]
    monkeypatch.setattr(cli, 'create_daily_news_posts', generate)
    def graph(jobs, *, tools, **kwargs):
        with pytest.raises(ConversationPaused):
            tools.generate(jobs[0], {'agent_run_id': 'saved-boundary', 'agent_job_key': '0'})
        assert [post.id for post in AgentArtifactStore(goals.knowledge_store).load('saved-boundary', '0')] == [first.id]
        assert goals.snapshot(ns, goal['goal_id'])['state'] == 'paused'
        return SimpleNamespace(status='completed', completed_jobs=0, requested_jobs=0,
                               uploaded_posts=[], checkpoint_path='controlled', errors=[])
    monkeypatch.setattr(cli, 'run_editorial_agent', graph)
    result = CliRunner().invoke(cli.app, ['agent', '--job-plan-file', str(path), '--no-preflight',
                                         '--no-refresh-quotas', '--skill-mode', 'off'])
    assert result.exit_code == 0, result.output
