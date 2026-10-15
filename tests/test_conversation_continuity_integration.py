"""Real PostgreSQL graph + API; providers/platform are controlled local adapters."""
from types import MethodType
from collections import Counter
import pytest

from apps.web_service import Workbench
from src.agent.editorial_agent import AgentJob, EditorialAgentConfig, EditorialAgentTools, run_editorial_agent
from src.agent.goal_store import GoalStore
from src.publish.draft_delivery import content_revision_fingerprint, has_current_draft_receipt
from src.storage.files import load_post, save_post
from src.storage.models import Post
from test_conversation_continuity import pg_isolated
from test_run_resume_contract import isolated, start_plan
from test_conversation_goals import goal_store


@pytest.mark.parametrize('amend', [False, True])
def test_ten_plus_one_plus_one_continues_after_pause_without_duplicate_delivery(pg_isolated, tmp_path, monkeypatch, amend):
    current, client, _ = pg_isolated
    cid, plan, original = start_plan(pg_isolated)
    monkeypatch.setattr(current, '_agent_checkpoint_state', MethodType(Workbench._agent_checkpoint_state, current))
    frozen = current._read_agent_conversation(cid)['plans'][0]['frozen_execution']
    context = frozen['goal_context']
    goals = GoalStore(current.conversation_store.knowledge_store)
    jobs = [AgentJob(j['kind'], j['title'], j['count'], job_id=j['job_id']) for j in plan['jobs']]
    created, writes, reviews = [], [], []
    data = current.root / 'data'

    def new_posts(kind, count):
        created.append((kind, count))
        posts = [Post(title=f'{kind} {len(created)} {i}', body='controlled verified source fixture') for i in range(count)]
        for post in posts:
            save_post(post, base=data)
        return posts

    def generate(job, _context):
        return new_posts(job.kind, 9 if job.kind == 'daily_news' else 1)

    def review(job, posts, _context):
        reviews.append(job.kind)
        if job.kind == 'daily_news' and reviews.count(job.kind) > 1 and len(posts) < job.count:
            posts.extend(new_posts(job.kind, job.count - len(posts)))
        return {'approved_post_ids': [p.id for p in posts], 'errors': []}

    def upload(job, post, _context):
        if has_current_draft_receipt(post, platform='xhs'):
            return True, 'already saved'
        post.platform['xhs_draft'] = {'revision_fingerprint': content_revision_fingerprint(post)}
        save_post(post, base=data)
        writes.append(post.id)
        if len(writes) == 7:
            goals.enqueue_turn(context['namespace'], cid, 'pause-source',
                {'kind': 'pause_goal', 'goal_id': context['goal_id']}, 1, 'integration-pause')
        return True, 'saved'

    tools = EditorialAgentTools(lambda _job: {}, generate, review, upload,
                                load_posts=lambda ids: [load_post(pid, base=data) for pid in ids])
    config = EditorialAgentConfig(checkpoint_dir=data / 'runs/agent', checkpoint_backend='postgres',
                                  goal_context=context, retry_delay_s=0)
    first = run_editorial_agent(jobs, tools=tools, config=config, run_id=original['id'])
    assert first.status == 'paused'
    assert len(writes) == 7
    before = goals.snapshot(context['namespace'], context['goal_id'])
    assert before['jobs'][0]['delivered_verified'] == 7
    assert before['jobs'][0]['retained_ready'] == 2
    current.jobs[original['id']]['status'] = 'interrupted'
    if amend:
        proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
            json={'content': '剩下改成科技新闻，图片门槛只做参考'}).json()
        response = client.post(f"/api/conversations/{cid}/turns/{proposal['turn']['turn_id']}/confirm",
            headers={'X-Workbench': '1', 'Idempotency-Key': 'integration-handoff'},
            json={'expected_goal_revision': 1, 'proposal_hash': proposal['plan']['semantic_hash']})
        assert response.status_code in {200, 202}, response.text
        resumed = response.json()['run']
        assert resumed['agent_run_id'] != original['id']
        revised = next(p for p in current._read_agent_conversation(cid)['plans'] if p.get('job_id') == resumed['id'])['frozen_execution']
        jobs = [AgentJob(j['kind'], j['title'], j['count'], prompt=j['prompt'], job_id=j['job_id']) for j in revised['jobs']]
        second_config = EditorialAgentConfig(checkpoint_dir=data / 'runs/agent', checkpoint_backend='postgres',
            goal_context=revised['goal_context'], revision_manifest=revised['revision_manifest'], retry_delay_s=0)
    else:
        response = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                               json={'content': '继续'})
        assert response.status_code == 202, response.text
        resumed = response.json()['run']
        assert resumed['agent_run_id'] == original['id']
        second_config = EditorialAgentConfig(checkpoint_dir=data / 'runs/agent', checkpoint_backend='postgres',
            resume_from=first.checkpoint_path, goal_context=context, retry_delay_s=0)
    second = run_editorial_agent(jobs, tools=tools, config=second_config, run_id=resumed['agent_run_id'])
    assert second.status == 'completed', [(j['kind'], j['delivered_verified'], j['retained_ready'], j['uncertain'])
                                         for j in goals.snapshot(context['namespace'], context['goal_id'])['jobs']]
    assert len(writes) == len(set(writes)) == 12
    assert Counter(created) == Counter({('daily_news', 9): 1, ('daily_news', 1): 1,
                                        ('daily_ai_digest', 1): 1, ('daily_global_map', 1): 1})
    after = goals.snapshot(context['namespace'], context['goal_id'])
    assert [j['delivered_verified'] for j in after['jobs']] == [10, 1, 1]
    assert after['state'] == 'completed'


def test_actual_executor_rejects_foreign_manifest_before_business_tools(tmp_path):
    from copy import deepcopy
    from src.agent.conversation_control import build_resume_manifest
    from src.agent.plan_contract import digest

    goals, ns, _, goal = goal_store()
    revised = {**deepcopy(goal['plan']), 'id': 'revised-plan'}
    manifest = build_resume_manifest(goal, revised)
    manifest['namespace'] = 'another-account'
    manifest['manifest_hash'] = digest({k: v for k, v in manifest.items() if k != 'manifest_hash'})
    current = goals.revise(ns, goal['goal_id'], revised, expected_revision=1)
    goals.bind_attempt(ns, goal['goal_id'], 'manifest-scope-test')
    def forbidden(*args):
        raise ValueError('business_tools_called_before_manifest_validation')
    with pytest.raises(ValueError, match='GOAL_MANIFEST_INVALID'):
        run_editorial_agent([AgentJob(j['kind'], j.get('title', j['kind']), j['count'], job_id=j['job_id'])
                              for j in revised['jobs']],
            tools=EditorialAgentTools(forbidden, forbidden, forbidden, forbidden),
            config=EditorialAgentConfig(checkpoint_backend='postgres', checkpoint_dir=tmp_path,
                goal_context={'namespace': ns, 'goal_id': goal['goal_id'], 'goal_revision': current['revision']},
                revision_manifest=manifest), run_id='manifest-scope-test')
