from dataclasses import dataclass

import pytest

from src.agent import editorial_agent as agent


@dataclass
class Post:
    id: str


def config(tmp_path, **kwargs):
    return agent.EditorialAgentConfig(checkpoint_dir=tmp_path, **kwargs)


def test_partial_review_keeps_appended_posts_and_only_reviews_deficit(tmp_path):
    generated = []
    reviewed = []
    uploaded = []

    def generate(job, context):
        generated.append((context['agent_run_id'], context['agent_job_key']))
        return [Post('a')]

    def review(job, posts, context):
        reviewed.append([p.id for p in posts])
        if len(reviewed) == 1:
            posts.append(Post('b'))
            return {'errors': ['one missing'], 'approved_post_ids': ['a', 'b'], 'retryable': True}
        assert context['agent_approved_post_ids'] == ['a', 'b']
        posts.append(Post('c'))
        return {'errors': [], 'approved_post_ids': ['a', 'b', 'c']}

    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_news', 'News', count=3)],
        tools=agent.EditorialAgentTools(lambda j: {}, generate, review,
                                      lambda j, p, c: (uploaded.append(p.id) or True, 'saved')),
        config=config(tmp_path), run_id='retain',
    )
    assert result.status == 'completed'
    assert generated == [('retain', '0')]
    assert reviewed == [['a'], ['a', 'b']]
    assert uploaded == ['a', 'b', 'c']


def test_old_wall_clock_budget_does_not_end_any_job(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(agent.time, 'time', lambda: now[0])

    def generate(job, context):
        now[0] += 100000.0
        return [Post(job.kind)]

    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_news', 'News'), agent.AgentJob('daily_ai_digest', 'AI')],
        tools=agent.EditorialAgentTools(lambda j: {}, generate, lambda *a: [], lambda *a: (True, 'saved')),
        config=config(tmp_path, max_elapsed_s=1),
    )
    assert result.status == 'completed'
    assert result.completed_jobs == 2


def test_recovery_rotates_jobs_and_outlives_graph_recursion_window(tmp_path):
    order = []
    calls = [0]

    def review(job, posts, context):
        order.append(job.kind)
        if job.kind == 'daily_news':
            calls[0] += 1
            if calls[0] > 1:
                posts.append(Post(str(calls[0])))
            ids = [p.id for p in posts]
            return {'errors': ['deficit'] if len(ids) < 70 else [], 'approved_post_ids': ids}
        return []

    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_news', 'News', count=70), agent.AgentJob('daily_ai_digest', 'AI')],
        tools=agent.EditorialAgentTools(lambda j: {}, lambda j, c: [Post('1' if j.kind == 'daily_news' else 'ai')],
                                      review, lambda *a: (True, 'saved')),
        config=config(tmp_path, max_attempts_per_job=2, max_steps=8),
    )
    assert result.status == 'completed'
    assert order[:3] == ['daily_news', 'daily_ai_digest', 'daily_news']
    assert len(result.uploaded_posts) == 71


def test_repeated_no_progress_is_resumable_and_does_not_claim_completion(tmp_path, monkeypatch):
    delays = []
    monkeypatch.setattr(agent.time, 'sleep', delays.append)
    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_news', 'News'), agent.AgentJob('daily_ai_digest', 'AI')],
        tools=agent.EditorialAgentTools(lambda j: {}, lambda j, c: [Post(j.kind)],
            lambda j, p, c: ['source unavailable'] if j.kind == 'daily_news' else [],
            lambda *a: (True, 'saved')),
        config=config(tmp_path, no_progress_limit=3),
    )
    assert result.status == 'partial'
    assert result.completed_jobs == 1
    checkpoint = agent.load_agent_checkpoint(result.checkpoint_path)
    assert checkpoint['job_states']['0']['post_ids'] == ['daily_news']
    assert checkpoint['job_states']['0']['status'] == 'blocked'
    assert any('NO_PROGRESS' in e for e in result.errors)
    assert delays and all(0 < d <= 60 for d in delays)


def test_default_continues_after_more_than_six_no_progress_rounds(tmp_path, monkeypatch):
    monkeypatch.setattr(agent.time, 'sleep', lambda _: None)
    calls = [0]

    def review(job, posts, context):
        calls[0] += 1
        return ['source temporarily unavailable'] if calls[0] <= 8 else []

    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_news', 'News')],
        tools=agent.EditorialAgentTools(lambda j: {}, lambda j, c: [Post('a')], review, lambda *a: (True, 'saved')),
        config=config(tmp_path),
    )
    assert calls[0] == 9
    assert result.status == 'completed'


def test_unknown_approved_id_cannot_satisfy_target(tmp_path, monkeypatch):
    monkeypatch.setattr(agent.time, 'sleep', lambda _: None)
    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_news', 'News')],
        tools=agent.EditorialAgentTools(lambda j: {}, lambda j, c: [Post('a')],
            lambda *a: {'errors': [], 'approved_post_ids': ['nonexistent'], 'retryable': False},
            lambda *a: (_ for _ in ()).throw(AssertionError('must not upload'))),
        config=config(tmp_path),
    )
    assert result.status == 'blocked'
    assert result.completed_jobs == 0


def test_generation_provider_exhaustion_pauses_without_retry(tmp_path):
    calls = []

    def generate(job, context):
        calls.append(job.kind)
        raise RuntimeError('Token Plan 用量上限')

    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_news', 'News')],
        tools=agent.EditorialAgentTools(lambda j: {}, generate, lambda *a: [], lambda *a: (True, 'saved')),
        config=config(tmp_path),
    )
    assert result.status == 'blocked'
    assert calls == ['daily_news']


def test_transient_context_failure_retries_and_finishes_other_job(tmp_path, monkeypatch):
    monkeypatch.setattr(agent.time, 'sleep', lambda _: None)
    sync_calls = [0]
    generated = []

    def sync(job):
        if job.kind == 'daily_news':
            sync_calls[0] += 1
            if sync_calls[0] == 1:
                raise TimeoutError('temporary network timeout')
        return {}

    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_news', 'News'), agent.AgentJob('daily_ai_digest', 'AI')],
        tools=agent.EditorialAgentTools(sync,
            lambda j, c: (generated.append(j.kind) or [Post(j.kind)]),
            lambda *a: [], lambda *a: (True, 'saved')),
        config=config(tmp_path),
    )
    assert result.status == 'completed'
    assert generated == ['daily_ai_digest', 'daily_news']


def test_structured_approval_is_authoritative_after_content_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(agent.time, 'sleep', lambda _: None)
    calls = [0]

    def review(job, posts, context):
        calls[0] += 1
        if calls[0] == 1:
            return {'errors': ['missing one'], 'approved_post_ids': ['a']}
        posts[:] = [Post('a'), Post('b')]
        return {'errors': ['a invalidated'], 'approved_post_ids': ['b'], 'retryable': False}

    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_news', 'News', count=2)],
        tools=agent.EditorialAgentTools(lambda j: {}, lambda j, c: [Post('a')], review, lambda *a: (True, 'saved')),
        config=config(tmp_path),
    )
    checkpoint = agent.load_agent_checkpoint(result.checkpoint_path)
    assert checkpoint['job_states']['0']['reviewed_post_ids'] == ['b']
    assert result.status == 'partial'
    assert result.completed_jobs == 0


def test_transient_429_rotates_and_retries_instead_of_pausing_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(agent.time, 'sleep', lambda _: None)
    calls = []

    def generate(job, context):
        calls.append(job.kind)
        if calls == ['daily_news']:
            raise RuntimeError('HTTP 429 rate_limit_error: Token Plan 速率限制')
        return [Post(job.kind)]

    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_news', 'News'), agent.AgentJob('daily_ai_digest', 'AI')],
        tools=agent.EditorialAgentTools(lambda j: {}, generate, lambda *a: [], lambda *a: (True, 'saved')),
        config=config(tmp_path),
    )
    assert result.status == 'completed'
    assert calls == ['daily_news', 'daily_ai_digest', 'daily_news']
    assert not any(event['node'] == 'provider_pause' for event in result.events)


@pytest.mark.parametrize('error', [
    'OPENCODEX_HTTP_400', 'OPENCODEX_PREVIOUS_REQUEST_UNCERTAIN',
    'OPENCODEX_UPDATE_REQUIRES_COMPATIBILITY_REVIEW', 'OPENCODEX_UNVERIFIED_PROVIDER_FORBIDDEN',
    'WOOL_VERIFICATION_INCOMPLETE',
])
def test_permanent_image_failure_retains_other_deliveries_without_retry(tmp_path, monkeypatch, error):
    monkeypatch.setattr(agent.time, 'sleep', lambda _: None)
    calls = []

    def generate(job, context):
        calls.append(job.kind)
        if job.kind == 'daily_wool' and calls.count(job.kind) <= 2:
            raise RuntimeError(error)
        return [Post(job.kind)]

    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_wool', 'Wool'), agent.AgentJob('daily_news', 'News')],
        tools=agent.EditorialAgentTools(lambda j: {}, generate, lambda *a: [], lambda *a: (True, 'saved')),
        config=config(tmp_path))
    assert calls == ['daily_wool', 'daily_news']
    assert result.status == 'partial'
    assert result.completed_jobs == 1


@pytest.mark.parametrize('reason', [
    'daily ai digest official material insufficient: no recent verified items',
    'AI讯息材料不足：第2条标题或摘要未完成中文改写',
])
def test_identical_digest_shortage_stops_after_three_attempts(tmp_path, monkeypatch, reason):
    monkeypatch.setattr(agent.time, 'sleep', lambda _: None)
    calls = []

    def generate(job, context):
        calls.append(job.kind)
        if len(calls) <= 5:
            raise RuntimeError(reason)
        return [Post('ai')]

    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_ai_digest', 'AI')],
        tools=agent.EditorialAgentTools(lambda j: {}, generate, lambda *a: [], lambda *a: (True, 'saved')),
        config=config(tmp_path))
    assert len(calls) == 3
    assert result.status == 'blocked'
    assert any('SOURCE_REFRESH_REQUIRED' in error for error in result.errors)


def test_wool_notice_is_kept_in_checkpoint_after_delivery(tmp_path):
    post = Post('wool')
    post.platform = {'daily_wool': {'user_notice': '今日暂未发现可核验且可领取的AI福利。'}}
    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_wool', 'AI福利')],
        tools=agent.EditorialAgentTools(lambda j: {}, lambda j, c: [post], lambda *a: [], lambda *a: (True, 'saved')),
        config=config(tmp_path))
    checkpoint = agent.load_agent_checkpoint(result.checkpoint_path)
    assert checkpoint['job_states']['0']['wool_notice'] == '今日暂未发现可核验且可领取的AI福利。'
