from copy import deepcopy

import pytest

from test_conversation_goals import goal_store


def test_pause_command_waits_for_real_boundary():
    from src.agent.conversation_control import ConversationControl, ConversationPaused
    goals, ns, cid, goal = goal_store()
    goals.bind_attempt(ns, goal['goal_id'], 'attempt')
    turn = goals.enqueue_turn(ns, cid, 'pause', {'kind': 'pause_goal', 'goal_id': goal['goal_id']}, 1, 'pause')
    assert goals.turn(ns, turn['turn_id'])['state'] == 'queued'
    control = ConversationControl({'goal_id': goal['goal_id'], 'namespace': ns, 'goal_revision': 1,
                                   'attempt_id': 'attempt'}, store=goals)
    with pytest.raises(ConversationPaused):
        control.check_boundary('before_upload')
    assert goals.turn(ns, turn['turn_id'])['state'] == 'applied'
    assert goals.snapshot(ns, goal['goal_id'])['state'] == 'paused'


def test_manifest_counts_retained_work_without_replaying_delivered():
    from src.agent.conversation_control import build_resume_manifest, import_revision
    goals, ns, _, goal = goal_store()
    for i in range(7):
        goals.record_artifact(ns, goal['goal_id'], 'news', str(i), 'v1', state='delivered', receipt_ref='verified:' + str(i))
    for i in range(2):
        goals.record_artifact(ns, goal['goal_id'], 'news', 'keep-' + str(i), 'v1', state='approved', origin_run_id='old', origin_job_key='0')
    snapshot = goals.snapshot(ns, goal['goal_id'])
    updated = deepcopy(snapshot['plan'])
    updated['id'] = 'new-plan'
    updated['jobs'][0]['prompt'] = '科技'
    manifest = build_resume_manifest(snapshot, updated)
    assert manifest['jobs'][0]['execute_count'] == 3
    assert manifest['jobs'][0]['delivered_before'] == 7
    assert len(manifest['jobs'][0]['retained']) == 2
    assert all(row['needs_review'] for row in manifest['jobs'][0]['retained'])
    initial = import_revision(manifest, 'new-thread')
    assert initial['job_states']['0']['post_ids'] == ['keep-0', 'keep-1']
    assert not initial['job_states']['0']['reviewed_post_ids']


def test_old_attempt_cannot_write_after_fencing():
    from src.agent.conversation_control import ConversationControl
    goals, ns, _, goal = goal_store()
    goals.bind_attempt(ns, goal['goal_id'], 'old')
    control = ConversationControl({'goal_id': goal['goal_id'], 'namespace': ns, 'goal_revision': 1,
                                   'attempt_id': 'old'}, store=goals)
    goals.bind_attempt(ns, goal['goal_id'], 'new')
    with pytest.raises(ValueError, match='GOAL_LEASE_LOST'):
        control.check_boundary('before_upload')


def test_executor_stops_before_second_upload_and_keeps_checkpoint(tmp_path):
    from dataclasses import dataclass
    from src.agent import editorial_agent as agent
    @dataclass
    class Post:
        id: str
    goals, ns, cid, goal = goal_store()
    goals.bind_attempt(ns, goal['goal_id'], 'attempt')
    uploaded = []
    def upload(job, post, context):
        uploaded.append(post.id)
        goals.enqueue_turn(ns, cid, 'stop', {'kind': 'pause_goal', 'goal_id': goal['goal_id']}, 1, 'stop')
        return True, 'saved'
    result = agent.run_editorial_agent(
        [agent.AgentJob('daily_news', 'News', count=2, job_id='news')],
        tools=agent.EditorialAgentTools(lambda job: {}, lambda *args: [Post('a'), Post('b')],
                                        lambda *args: [], upload),
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path,
            goal_context={'goal_id': goal['goal_id'], 'namespace': ns, 'goal_revision': 1, 'attempt_id': 'attempt'}),
        run_id='control-test')
    assert uploaded == ['a']
    assert result.status == 'paused'
    assert result.checkpoint_path.exists()


def test_current_receipt_projects_even_before_uploaded_flag_checkpoint():
    from src.agent.conversation_control import ConversationControl
    from src.publish.draft_delivery import content_revision_fingerprint
    from src.storage.models import Post
    goals, ns, _, goal = goal_store()
    goals.bind_attempt(ns, goal['goal_id'], 'attempt')
    post = Post(title='saved before checkpoint', body='verified test source')
    post.platform['xhs_draft'] = {'revision_fingerprint': content_revision_fingerprint(post)}
    control = ConversationControl({'goal_id': goal['goal_id'], 'namespace': ns, 'goal_revision': 1,
                                   'attempt_id': 'attempt', 'platform': 'xhs'}, store=goals)
    control.record_state({'run_id': 'attempt', 'jobs': goal['plan']['jobs'], 'uploaded_post_ids': [],
        'job_states': {'0': {'posts': [post], 'reviewed_post_ids': [post.id]}}})
    assert goals.snapshot(ns, goal['goal_id'])['jobs'][0]['delivered_verified'] == 1


@pytest.mark.parametrize('change', [
    'namespace', 'goal_id', 'plan_id', 'source_revision', 'job_id', 'kind', 'count', 'order',
])
def test_revision_import_rejects_rehashed_manifest_for_different_goal_or_jobs(change):
    from src.agent.conversation_control import build_resume_manifest, import_revision
    from src.agent.plan_contract import digest
    goals, ns, _, goal = goal_store()
    updated = deepcopy(goal['plan'])
    updated['id'] = 'new-plan'
    updated['jobs'].append({'job_id': 'ai', 'kind': 'daily_ai_digest', 'count': 1})
    manifest = build_resume_manifest(goal, updated)
    current = goals.revise(ns, goal['goal_id'], updated, expected_revision=1)
    if change in {'namespace', 'goal_id', 'plan_id'}:
        manifest[change] = 'different-owner'
    elif change == 'source_revision':
        manifest[change] = current['revision']
    elif change in {'job_id', 'kind'}:
        manifest['jobs'][0][change] = 'different-job'
    elif change == 'count':
        manifest['jobs'][0]['requested'] += 1
        manifest['jobs'][0]['execute_count'] += 1
    else:
        manifest['jobs'].reverse()
    manifest['manifest_hash'] = digest({k: v for k, v in manifest.items() if k != 'manifest_hash'})
    with pytest.raises(ValueError, match='GOAL_MANIFEST_INVALID'):
        import_revision(manifest, 'new-thread', context=current, confirmed_jobs=updated['jobs'])


def test_revision_import_validates_current_goal_and_full_confirmed_jobs():
    from src.agent.conversation_control import build_resume_manifest, import_revision
    goals, ns, _, goal = goal_store()
    goals.record_artifact(ns, goal['goal_id'], 'news', 'done', 'v1', state='delivered', receipt_ref='verified')
    snapshot = goals.snapshot(ns, goal['goal_id'])
    updated = {**deepcopy(snapshot['plan']), 'id': 'new-plan'}
    manifest = build_resume_manifest(snapshot, updated)
    current = goals.revise(ns, goal['goal_id'], updated, expected_revision=1)
    initial = import_revision(manifest, 'new-thread', context=current, confirmed_jobs=updated['jobs'])
    assert initial['job_states']['0']['status'] == 'pending'
    assert manifest['jobs'][0]['requested'] == 10
    assert manifest['jobs'][0]['execute_count'] == 9
    with pytest.raises(ValueError, match='GOAL_MANIFEST_INVALID'):
        import_revision(manifest, 'new-thread', context=current)
