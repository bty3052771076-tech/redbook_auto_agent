"""Real PostgreSQL restart tests; no model or platform calls are made."""

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import json
import os
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.agent import editorial_agent as agent
from src.agent.postgres_checkpoint import postgres_checkpointer


@dataclass
class Post:
    id: str


@pytest.fixture(params=['memory', 'postgres'])
def durable_saver(request, monkeypatch):
    if request.param == 'postgres':
        if os.getenv('REDBOOK_TEST_POSTGRES') != '1':
            pytest.skip('set REDBOOK_TEST_POSTGRES=1 for the real local PostgreSQL test')
        yield postgres_checkpointer
    else:
        saver = InMemorySaver()

        @contextmanager
        def memory():
            yield saver

        monkeypatch.setattr('src.agent.postgres_checkpoint.postgres_checkpointer', memory)
        monkeypatch.setattr('src.agent.artifact_store.AgentArtifactStore.lease', lambda self, run_id: nullcontext(lambda: None))
        yield memory


def test_postgres_retains_partial_review_across_new_connection_and_ignores_audit_json(
    tmp_path, monkeypatch, durable_saver,
):
    identifier = 'test-retention-' + uuid4().hex
    build = agent._build_graph
    interrupted = [False]
    generated = []
    reviews = []
    sent = []

    def builder(**kwargs):
        graph = build(**kwargs)
        invoke = graph.invoke

        def invoke_once(*args, **options):
            if not interrupted[0]:
                interrupted[0] = True
                options['interrupt_after'] = ['review']
            return invoke(*args, **options)

        graph.invoke = invoke_once
        return graph

    monkeypatch.setattr(agent, '_build_graph', builder)

    def generate(job, context):
        generated.append((job.kind, context['agent_run_id'], context['agent_job_key']))
        return [Post('a' if job.kind == 'daily_news' else 'ai')]

    def review(job, posts, context):
        reviews.append((job.kind, [p.id for p in posts], context['agent_job_key']))
        if job.kind != 'daily_news':
            return []
        if len(posts) == 1:
            posts.append(Post('b'))
            return {'errors': ['one missing'], 'approved_post_ids': ['a', 'b']}
        assert context['agent_approved_post_ids'] == ['a', 'b']
        posts.append(Post('c'))
        return {'errors': [], 'approved_post_ids': ['a', 'b', 'c']}

    tools = agent.EditorialAgentTools(
        lambda j: {}, generate, review,
        lambda j, p, c: (sent.append(p.id) or True, 'saved'),
    )
    jobs = [agent.AgentJob('daily_news', 'News', count=3), agent.AgentJob('daily_ai_digest', 'AI')]
    try:
        first = agent.run_editorial_agent(jobs, tools=tools, run_id=identifier,
            config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, checkpoint_backend='postgres'))
        assert first.status == 'running'
        assert sent == []
        # The file is only an audit projection. Its altered content must never
        # replace the authoritative pending graph checkpoint in PostgreSQL.
        first.checkpoint_path.write_text(json.dumps({'run_id': identifier, 'jobs': [], 'post_ids': ['wrong']}), encoding='utf-8')
        resumed = agent.run_editorial_agent([], tools=tools,
            config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, checkpoint_backend='postgres', resume_from=first.checkpoint_path))
        assert resumed.status == 'completed'
        assert resumed.completed_jobs == 2
        assert generated == [('daily_news', identifier, '0'), ('daily_ai_digest', identifier, '1')]
        assert sent == ['a', 'b', 'ai', 'c']
        assert reviews[-1] == ('daily_news', ['a', 'b'], '0')
        again = agent.run_editorial_agent([], tools=tools,
            config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, checkpoint_backend='postgres', resume_from=first.checkpoint_path))
        assert again.status == 'completed'
        assert sent == ['a', 'b', 'ai', 'c']
        with durable_saver() as saver:
            checkpoint = saver.get_tuple({'configurable': {'thread_id': identifier}})
            values = checkpoint.checkpoint['channel_values']
            assert values['completed_job_indices'] == [0, 1]
            assert values['job_states']['0']['post_ids'] == ['a', 'b', 'c']
    finally:
        with durable_saver() as saver:
            saver.delete_thread(identifier)


def test_postgres_reopens_blocked_job_without_replaying_completed_job(tmp_path, durable_saver):
    identifier = 'test-reopen-' + uuid4().hex
    available = [False]
    generated = []
    sent = []

    def generate(job, context):
        generated.append(job.kind)
        return [Post(job.kind)]

    def review(job, posts, context):
        if job.kind == 'daily_news' and not available[0]:
            return {'errors': ['EXTERNAL_CONFIG_REQUIRED'], 'retryable': False}
        return []

    tools = agent.EditorialAgentTools(lambda j: {}, generate, review, lambda j, p, c: (sent.append(p.id) or True, 'saved'))
    jobs = [agent.AgentJob('daily_news', 'News'), agent.AgentJob('daily_ai_digest', 'AI')]
    try:
        first = agent.run_editorial_agent(jobs, tools=tools, run_id=identifier,
            config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, checkpoint_backend='postgres'))
        assert first.status == 'partial'
        assert first.completed_jobs == 1
        available[0] = True
        resumed = agent.run_editorial_agent([], tools=tools,
            config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, checkpoint_backend='postgres', resume_from=first.checkpoint_path))
        assert resumed.status == 'completed'
        assert generated == ['daily_news', 'daily_ai_digest']
        assert sent == ['daily_ai_digest', 'daily_news']
    finally:
        with durable_saver() as saver:
            saver.delete_thread(identifier)


def test_postgres_can_resume_when_audit_projection_is_missing(tmp_path, durable_saver):
    identifier = 'test-missing-audit-' + uuid4().hex
    calls = []
    tools = agent.EditorialAgentTools(lambda j: {},
        lambda j, c: (calls.append('generate') or [Post('a')]),
        lambda *a: [], lambda *a: (True, 'saved'))
    try:
        first = agent.run_editorial_agent([agent.AgentJob('daily_news', 'News')], tools=tools, run_id=identifier,
            config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, checkpoint_backend='postgres'))
        first.checkpoint_path.unlink()
        resumed = agent.run_editorial_agent([], tools=tools,
            config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, checkpoint_backend='postgres', resume_from=first.checkpoint_path))
        assert resumed.status == 'completed'
        assert resumed.run_id == identifier
        assert calls == ['generate']
    finally:
        with durable_saver() as saver:
            saver.delete_thread(identifier)
