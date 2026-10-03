from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.agent import editorial_agent as agent


@dataclass
class Post:
    id: str
    title: str = 'original'


@pytest.fixture(params=['json', 'postgres'])
def harness(request, tmp_path, monkeypatch):
    saver = InMemorySaver()

    @contextmanager
    def memory():
        yield saver

    monkeypatch.setattr('src.agent.postgres_checkpoint.postgres_checkpointer', memory)
    monkeypatch.setattr('src.agent.artifact_store.AgentArtifactStore.lease', lambda self, run_id: nullcontext(lambda: None))
    trace = []
    identifier = 'test-revalidate-' + uuid4().hex
    jobs = [agent.AgentJob(kind, kind) for kind in ('daily_news', 'daily_ai_digest', 'daily_global_map')]

    def save(post):
        (tmp_path / (post.id + '.json')).write_text(json.dumps(vars(post)), encoding='utf-8')

    def load(ids):
        trace.append(('load', tuple(ids)))
        return [Post(**json.loads((tmp_path / (pid + '.json')).read_text(encoding='utf-8'))) for pid in ids]

    def generate(job, context):
        trace.append(('generate', job.kind))
        post = Post(job.kind)
        save(post)
        return [post]

    def review(job, posts, context):
        trace.append(('review', job.kind))
        return []

    def upload(job, post, context):
        trace.append(('upload', post.id))
        return True, 'saved'

    tools = agent.EditorialAgentTools(
        lambda job: trace.append(('sync', job.kind)) or {}, generate, review, upload, load_posts=load,
    )
    path = tmp_path / 'runs' / identifier / 'checkpoint.json'

    def run(resume=False):
        return agent.run_editorial_agent(jobs, tools=tools, run_id=identifier,
            config=agent.EditorialAgentConfig(
                checkpoint_dir=tmp_path / 'runs', checkpoint_backend=request.param,
                resume_from=path if resume else None, no_progress_limit=2, retry_delay_s=0,
            ))

    def state():
        if request.param == 'postgres':
            return saver.get_tuple({'configurable': {'thread_id': identifier}}).checkpoint['channel_values']
        return agent.load_agent_checkpoint(path)

    return SimpleNamespace(run=run, state=state, tools=tools, trace=trace, save=save,
                           backend=request.param, identifier=identifier, path=path, saver=saver)


def test_completed_valid_jobs_are_loaded_once_without_regeneration_or_upload(harness):
    h = harness
    h.run()
    h.trace.clear()
    h.save(Post('daily_ai_digest', 'new file revision'))
    observed = []

    def revalidate(job, posts, context):
        observed.append((job.kind, posts[0].title, context['agent_run_id'], context['agent_job_key']))
        return []

    h.tools.revalidate_completed = revalidate
    result = h.run(resume=True)
    assert result.status == 'completed'
    assert result.completed_jobs == 3
    assert [row for row in observed if row[0] == 'daily_ai_digest'] == [
        ('daily_ai_digest', 'new file revision', h.identifier, '1')]
    assert len(observed) == 3
    assert h.trace == [('load', ('daily_news',)), ('load', ('daily_ai_digest',)), ('load', ('daily_global_map',))]


def test_only_invalid_completed_job_returns_to_review_with_original_posts(harness):
    h = harness
    h.run()
    previous = h.state()['job_states']['0'].copy()
    old_version = h.state()['job_states']['1']['approved_versions']['daily_ai_digest']
    old_receipt_key = f'1:daily_ai_digest:{old_version}'
    assert h.state()['item_status'][old_receipt_key] == 'saved'
    h.trace.clear()
    h.tools.revalidate_completed = lambda job, posts, context: ['wrong release'] if job.kind == 'daily_ai_digest' else []

    def repair(job, posts, context):
        h.trace.append(('review', job.kind))
        assert job.kind == 'daily_ai_digest'
        assert [p.id for p in posts] == ['daily_ai_digest']
        assert context['agent_approved_post_ids'] == []
        posts[0].title = 'corrected release'
        h.save(posts[0])
        return []

    h.tools.review = repair
    result = h.run(resume=True)
    assert result.status == 'completed'
    assert result.completed_jobs == 3
    assert [row for row in h.trace if row[0] in {'generate', 'review', 'upload'}] == [
        ('review', 'daily_ai_digest'), ('upload', 'daily_ai_digest')]
    assert h.state()['job_states']['0']['approved_versions'] == previous['approved_versions']
    assert h.state()['job_states']['1']['post_ids'] == ['daily_ai_digest']
    new_version = agent._content_version(Post('daily_ai_digest', 'corrected release'))
    assert new_version != old_version
    assert h.state()['job_states']['1']['approved_versions'] == {'daily_ai_digest': new_version}
    assert h.state()['item_status'][f'1:daily_ai_digest:{new_version}'] == 'saved'
    assert h.state()['item_status'][old_receipt_key] == 'saved'
    # Updating content reuses the post identity; it does not regenerate a new draft.
    assert h.state()['uploaded_post_ids'].count('daily_ai_digest') == 1
    h.trace.clear()
    h.tools.revalidate_completed = lambda *args: []
    assert h.run(resume=True).status == 'completed'
    assert not any(row[0] in {'generate', 'review', 'upload'} for row in h.trace)


def test_revalidation_does_not_lower_quality_when_repair_still_fails(harness):
    h = harness
    h.run()
    h.trace.clear()
    h.tools.revalidate_completed = lambda job, posts, context: ['bad fact'] if job.kind == 'daily_ai_digest' else []
    h.tools.review = lambda job, posts, context: {'errors': ['bad fact remains'], 'approved_post_ids': [], 'retryable': False}
    result = h.run(resume=True)
    assert result.status != 'completed'
    assert result.completed_jobs == 2
    state = h.state()
    assert state['completed_job_indices'] == [0, 2]
    assert state['job_states']['1']['post_ids'] == ['daily_ai_digest']
    assert state['job_states']['1']['approved_versions'] == {}
    assert state['job_states']['1']['review_complete'] is False
    assert not any(row[0] in {'generate', 'upload'} for row in h.trace)


@pytest.mark.parametrize('failure', ['raises', 'missing', 'mismatch', 'no_loader'])
def test_completed_post_load_failure_cannot_remain_completed(harness, failure):
    h = harness
    h.run()
    h.trace.clear()
    validations = []
    h.tools.revalidate_completed = lambda job, posts, context: validations.append(job.kind) or []
    normal_load = h.tools.load_posts

    def load(ids):
        if ids != ['daily_ai_digest']:
            return normal_load(ids)
        if failure == 'raises':
            raise OSError('artifact unavailable')
        return [] if failure == 'missing' else [Post('wrong-id')]

    h.tools.load_posts = None if failure == 'no_loader' else load
    result = h.run(resume=True)
    assert result.status != 'completed'
    assert result.completed_jobs == (0 if failure == 'no_loader' else 2)
    assert 'daily_ai_digest' not in validations
    assert any('RETAINED_POST_UNAVAILABLE' in error for error in result.errors)
    assert h.state()['job_states']['1']['post_ids'] == ['daily_ai_digest']
    assert not any(row[0] in {'generate', 'review', 'upload'} for row in h.trace)


def test_revalidation_platform_risk_still_blocks(harness):
    h = harness
    h.run()
    h.trace.clear()
    h.tools.revalidate_completed = lambda job, posts, context: ['XHS_RISK_BLOCKED'] if job.kind == 'daily_ai_digest' else []
    result = h.run(resume=True)
    assert result.status != 'completed'
    assert result.completed_jobs == 2
    assert h.state()['platform_paused'] is True
    assert not any(row[0] in {'generate', 'upload'} for row in h.trace)


@pytest.mark.parametrize('failure', ['exception', 'invalid_return'])
def test_revalidation_callback_failure_fails_closed(harness, failure):
    h = harness
    h.run()

    def revalidate(job, posts, context):
        if failure == 'exception':
            raise RuntimeError('validation service unavailable')
        return None

    h.tools.revalidate_completed = revalidate
    h.trace.clear()
    result = h.run(resume=True)
    assert result.status != 'completed'
    assert result.completed_jobs == 0
    assert not any(row[0] in {'generate', 'review', 'upload'} for row in h.trace)


@pytest.mark.parametrize('pending_node', ['generate', 'upload', 'finish'])
def test_postgres_pending_node_preserved_while_completed_job_is_reopened(harness, monkeypatch, pending_node):
    h = harness
    if h.backend != 'postgres':
        pytest.skip('JSON has no graph pending-node metadata')
    build = agent._build_graph
    paused = False

    class Pause(BaseException):
        pass

    def builder(**kwargs):
        graph = build(**kwargs)
        invoke = graph.invoke

        def once(value, config, **options):
            nonlocal paused
            before = graph.get_state(config)
            index = before.values.get('job_index', 0) if before.values else 0
            target_index = 3 if pending_node == 'finish' else 2
            if not paused and index == target_index:
                paused = True
                if before.next != (pending_node,):
                    invoke(value, config, interrupt_before=[pending_node], **options)
                assert graph.get_state(config).next == (pending_node,)
                raise Pause()
            return invoke(value, config, **options)

        graph.invoke = once
        return graph

    monkeypatch.setattr(agent, '_build_graph', builder)
    with pytest.raises(Pause):
        h.run()
    h.trace.clear()
    h.tools.revalidate_completed = lambda job, posts, context: ['bad release'] if job.kind == 'daily_ai_digest' else []

    def review(job, posts, context):
        h.trace.append(('review', job.kind))
        return []

    h.tools.review = review
    # Deliberately wrong JSON projection must not replace PostgreSQL jobs.
    h.path.write_text(json.dumps({'run_id': h.identifier, 'jobs': [], 'completed_job_indices': []}), encoding='utf-8')
    result = h.run(resume=True)
    assert result.status == 'completed'
    assert result.completed_jobs == 3
    actions = [row for row in h.trace if row[0] != 'load']
    if pending_node == 'generate':
        assert actions[0] == ('generate', 'daily_global_map')
    elif pending_node == 'upload':
        assert actions[0] == ('upload', 'daily_global_map')
    assert ('review', 'daily_ai_digest') in actions
    assert ('generate', 'daily_ai_digest') not in actions
    assert ('upload', 'daily_ai_digest') not in actions


def test_tools_accept_optional_completed_revalidation():
    callback = lambda job, posts, context: []
    tools = agent.EditorialAgentTools(lambda job: {}, lambda job, ctx: [], lambda *args: [],
                                     lambda *args: (True, 'saved'), revalidate_completed=callback)
    assert tools.revalidate_completed is callback
