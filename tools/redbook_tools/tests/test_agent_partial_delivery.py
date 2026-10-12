from dataclasses import dataclass

import pytest

from src.agent import editorial_agent as agent


@dataclass
class Post:
    id: str


def test_qualified_subset_uploads_before_recovering_rejected_post(tmp_path):
    calls = []

    def review(job, posts, context):
        calls.append('review')
        if calls.count('review') == 1:
            return {'errors': ['bad source'], 'approved_post_ids': ['good'],
                    'rejected_post_ids': ['bad']}
        posts.append(Post('replacement'))
        return {'errors': [], 'approved_post_ids': ['good', 'replacement'],
                'rejected_post_ids': ['bad']}

    def upload_batch(job, posts, context):
        calls.append([p.id for p in posts])
        return {p.id: (True, 'saved') for p in posts}

    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_news', 'News', count=2)],
        tools=agent.EditorialAgentTools(lambda j: {}, lambda j,c: [Post('good'), Post('bad')],
            review, lambda *a: (True, 'saved'), upload_batch=upload_batch),
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path))
    assert calls == ['review', ['good'], 'review', ['replacement']]
    assert result.status == 'completed'
    checkpoint = agent.load_agent_checkpoint(result.checkpoint_path)
    summary = checkpoint['job_states']['0']['review_summary']
    assert summary == {'requested': 2, 'approved': 2, 'rejected': 1, 'pending': 0, 'missing': 0}


def test_non_retryable_rejection_still_delivers_good_posts(tmp_path):
    uploaded = []
    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_news', 'News', count=2)],
        tools=agent.EditorialAgentTools(lambda j: {}, lambda j,c: [Post('good'), Post('bad')],
            lambda *a: {'errors': ['invalid source'], 'approved_post_ids': ['good'],
                        'rejected_post_ids': ['bad'], 'retryable': False},
            lambda j,p,c: (uploaded.append(p.id) or True, 'saved')),
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path))
    assert uploaded == ['good']
    assert result.status == 'partial'
    assert result.completed_jobs == 0
    checkpoint = agent.load_agent_checkpoint(result.checkpoint_path)
    assert 'invalid source' in checkpoint['job_states']['0']['last_failure']


def test_upload_batch_count_is_for_current_job(tmp_path):
    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_ai_digest', 'AI'), agent.AgentJob('daily_news', 'News')],
        tools=agent.EditorialAgentTools(lambda j: {}, lambda j,c: [Post(j.kind)],
            lambda *a: [], lambda *a: (True,'saved'),
            upload_batch=lambda j,posts,c: {p.id: (True,'saved') for p in posts}),
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path))
    event = next(e for e in result.events if e['node'] == 'upload_batch'
                 and e['status'] == 'success' and e['detail'].startswith('daily_news'))
    assert 'uploaded=1' in event['detail']
    assert 'total_uploaded=2' in event['detail']


def test_explicit_resume_reconciles_uncertain_delivery_before_retry(tmp_path):
    post = Post('retained')
    calls = []

    def upload(job, post, context):
        calls.append('upload')
        return (False, 'XHS_WRITE_UNCERTAIN') if calls == ['upload'] else (True, 'saved')

    tools = agent.EditorialAgentTools(lambda j: {}, lambda j,c: [post], lambda *a: [], upload,
        load_posts=lambda ids: [post],
        reconcile_uploads=lambda j, ids: (calls.append('readonly_reconcile') or True, 'verified absent'))
    first = agent.run_editorial_agent([agent.AgentJob('daily_news', 'News')], tools=tools,
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path))
    assert first.status == 'partial'
    result = agent.run_editorial_agent([agent.AgentJob('daily_news', 'News')], tools=tools, config=agent.EditorialAgentConfig(
        checkpoint_dir=tmp_path, resume_from=first.checkpoint_path))
    assert result.status == 'completed'
    assert calls == ['upload', 'readonly_reconcile', 'upload']


@pytest.mark.parametrize('reconciled', [True, False])
def test_resume_reconciles_previous_job_after_later_generation_failure(tmp_path, reconciled):
    posts = {kind: Post(kind) for kind in ('daily_news', 'daily_ai_digest')}
    calls = []
    resumed = False

    def generate(job, context):
        if job.kind == 'daily_ai_digest' and not resumed:
            raise ValueError('daily ai digest official material insufficient')
        calls.append(('generate', job.kind))
        return [posts[job.kind]]

    def upload(job, post, context):
        calls.append(('upload', job.kind))
        return (True, 'saved') if resumed else (False, 'XHS_WRITE_UNCERTAIN')

    def reconcile(job, ids):
        calls.append(('reconcile', job.kind, ids))
        return reconciled, 'verified absent' if reconciled else 'still uncertain'

    jobs = [agent.AgentJob('daily_news', 'News'), agent.AgentJob('daily_ai_digest', 'AI')]
    tools = agent.EditorialAgentTools(lambda j: {}, generate, lambda *a: [], upload,
        load_posts=lambda ids: [posts[i] for i in ids], reconcile_uploads=reconcile)
    first = agent.run_editorial_agent(jobs, tools=tools,
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, retry_delay_s=0, no_progress_limit=1))
    checkpoint = agent.load_agent_checkpoint(first.checkpoint_path)
    assert checkpoint['platform_paused'] is True
    assert 'official material insufficient' in checkpoint['last_failure']
    assert checkpoint['job_states']['0']['status'] == 'blocked'
    resumed = True
    result = agent.run_editorial_agent(jobs, tools=tools,
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, resume_from=first.checkpoint_path,
                                          retry_delay_s=0, no_progress_limit=1))
    assert ('reconcile', 'daily_news', ['daily_news']) in calls
    assert calls.count(('generate', 'daily_news')) == 1
    assert result.status == ('completed' if reconciled else 'partial')
    assert calls.count(('upload', 'daily_news')) == (2 if reconciled else 1)
    assert calls.count(('upload', 'daily_ai_digest')) == (1 if reconciled else 0)


def test_resume_does_not_clear_platform_risk_block(tmp_path):
    post = Post('retained')
    calls = []
    tools = agent.EditorialAgentTools(lambda j: {}, lambda j, c: [post], lambda *a: [],
        lambda *a: (False, 'XHS_RISK_BLOCKED'), load_posts=lambda ids: [post],
        reconcile_uploads=lambda j, ids: (calls.append('reconcile') or True, 'absent'))
    jobs = [agent.AgentJob('daily_news', 'News')]
    first = agent.run_editorial_agent(jobs, tools=tools,
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path))
    result = agent.run_editorial_agent(jobs, tools=tools,
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, resume_from=first.checkpoint_path))
    assert result.status == 'partial'
    assert calls == []


@pytest.mark.parametrize('reconciled', [True, False])
def test_pending_ai_upload_reconciles_news_pause_without_rerunning_sync(tmp_path, reconciled):
    news, ai = Post('news'), Post('ai')
    calls = []
    jobs = [agent.AgentJob('daily_news', 'News'), agent.AgentJob('daily_ai_digest', 'AI')]
    config = agent.EditorialAgentConfig(checkpoint_dir=tmp_path,
        resume_from=tmp_path / 'checkpoint.json', retry_delay_s=0, no_progress_limit=1)
    tools = agent.EditorialAgentTools(lambda j: {}, lambda j,c: [ai], lambda *a: [],
        lambda j,p,c: (calls.append(('upload', p.id)) or True, 'saved'),
        load_posts=lambda ids: [news if i == 'news' else ai for i in ids],
        reconcile_uploads=lambda j, ids: (calls.append(('reconcile', j.kind, ids)) or reconciled, 'absent'))
    graph = agent._build_graph(config=config, tools=tools, progress=None)
    state = {'run_id': 'pending-ai', 'jobs': [j.__dict__ for j in jobs], 'job_index': 1,
             'current_job': jobs[1].__dict__, 'posts': [ai], 'post_ids': ['ai'],
             'reviewed_posts': [ai], 'reviewed_post_ids': ['ai'], 'review_complete': True,
             'platform_paused': True, 'last_failure': 'generation_error: official material insufficient',
             'job_states': {'0': {'status': 'blocked', 'reviewed_post_ids': ['news'],
                                 'last_failure': 'upload_error: XHS_WRITE_UNCERTAIN'}},
             'context': {}, 'status': 'running', 'errors': [], 'steps': 0}
    update = graph.nodes['upload'].invoke(state)
    assert ('reconcile', 'daily_news', ['news']) in calls
    assert update['platform_paused'] is not reconciled
    assert ('upload', 'ai') in calls if reconciled else ('upload', 'ai') not in calls
    assert update['job_states']['0']['status'] == ('pending' if reconciled else 'blocked')
