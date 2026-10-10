from dataclasses import dataclass

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
