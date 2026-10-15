"""Actual CLI controller preflight and one bounded context-overflow retry."""
import json
from types import SimpleNamespace
from uuid import uuid4

from typer.testing import CliRunner

from apps import cli
from src.agent import compaction
from src.agent.conversation_store import PostgresConversationStore
from src.agent.goal_store import GoalStore


def test_cli_controller_rebuilds_context_for_one_provider_overflow(monkeypatch, tmp_path):
    store = PostgresConversationStore(namespace='cli_context_' + uuid4().hex)
    cid = uuid4().hex
    messages = []
    for index in range(30):
        messages.extend([
            {'id': uuid4().hex, 'role': 'user', 'content': '已经完成的历史材料' * 3},
            {'id': uuid4().hex, 'role': 'assistant', 'content': '这一轮已经结束'}])
    store.save({'id': cid, 'context_schema_version': 2, 'messages': messages, 'plans': [], 'runs': []})
    goals = GoalStore(store.knowledge_store)
    goal = goals.create(store.namespace, cid, {'id': uuid4().hex, 'delivery': 'generate_only',
        'jobs': [{'job_id': 'news', 'kind': 'daily_news', 'count': 10}]})
    turn = goals.enqueue_turn(store.namespace, cid, messages[-2]['id'],
        {'kind': 'status', 'goal_id': goal['goal_id']}, 1, 'already-applied')
    goals.update_turn(store.namespace, turn['turn_id'], state='applied')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('MODEL_PLATFORMS_DIR', str(tmp_path / 'models'))
    monkeypatch.setenv('MODEL_PLATFORMS_NAMESPACE', 'agent')
    monkeypatch.setenv('MINIMAX_TOKEN_PLAN_API_KEY', 'offline-test-not-a-real-key')
    monkeypatch.setenv('MINIMAX_LLM_MODEL', 'MiniMax-M3')
    monkeypatch.setenv('AGENT_LLM_PROVIDER', 'minimax')
    plan = {'jobs': [{'job_id': 'news', 'kind': 'daily_news', 'title': '每日新闻', 'count': 10}],
            'delivery': 'generate_only', 'goal_context': {'namespace': store.namespace,
                'conversation_id': cid, 'goal_id': goal['goal_id']}}
    path = tmp_path / 'data/web_gui/conversations' / cid / 'plans/plan.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(plan), encoding='utf-8')
    monkeypatch.setattr(cli, 'prepare_local_knowledge_snapshot', lambda **kw: {'knowledge_status': 'ready'})
    summaries, requests, limits_seen = [], [], []
    from src.agent import context_builder
    real_prepare = context_builder.prepare_model_context
    def capture_limits(*args, **kwargs):
        limits_seen.append(args[5])
        return real_prepare(*args, **kwargs)
    monkeypatch.setattr(context_builder, 'prepare_model_context', capture_limits)
    def summarize(payload, **kwargs):
        summaries.append(payload['through_seq'])
        return {'summary': '历史材料已经处理，当前目标从宿主状态读取。'}
    monkeypatch.setattr(compaction, 'minimax_summary', summarize)
    def request(config, **kwargs):
        requests.append(json.loads(kwargs['user_prompt']))
        if len(requests) == 1:
            raise ValueError('context_length_exceeded')
        return {'job_order': [0], 'summary': '继续原任务', 'tool_calls': []}
    monkeypatch.setattr(cli, 'generate_json', request)
    def graph(jobs, *, tools, **kwargs):
        assert tools.plan(jobs, {})['job_order'] == [0]
        return SimpleNamespace(status='completed', completed_jobs=0, requested_jobs=0,
                               uploaded_posts=[], checkpoint_path='controlled', errors=[])
    monkeypatch.setattr(cli, 'run_editorial_agent', graph)
    result = CliRunner().invoke(cli.app, ['agent', '--job-plan-file', str(path), '--no-preflight',
                                         '--no-refresh-quotas', '--skill-mode', 'off'])
    assert result.exit_code == 0, result.output
    assert len(requests) == 2
    assert limits_seen[0]['provider'] == 'minimax'
    assert limits_seen[0]['model'] == 'MiniMax-M3'
    assert limits_seen[0]['request_format'] == 'openai_chat'
    assert summaries
    assert requests[0]['conversation_context']['history_summary'] is None
    assert requests[1]['conversation_context']['history_summary']['version'] >= 1
    assert requests[1]['conversation_context']['goal']['plan']['jobs'][0]['count'] == 10
    assert goals.snapshot(store.namespace, goal['goal_id'])['jobs'][0]['delivered_verified'] == 0
