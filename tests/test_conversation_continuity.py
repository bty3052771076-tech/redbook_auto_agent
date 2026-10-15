from uuid import uuid4

import pytest

from test_run_resume_contract import isolated, start_plan, checkpoint
from src.agent.conversation_store import PostgresConversationStore
from src.knowledge.store import KnowledgeStore


@pytest.fixture
def pg_isolated(isolated):
    current, client, commands = isolated
    current.conversation_store = PostgresConversationStore(KnowledgeStore.from_env(), namespace='continuity_' + uuid4().hex)
    from src.agent.capabilities.store import CapabilityStore
    CapabilityStore(current.conversation_store.knowledge_store, namespace=current.conversation_store.namespace).ensure_schema()
    return current, client, commands


@pytest.mark.parametrize('text', ['继续', '继续补齐剩下3条', '继续完成10条每日新闻，已完成7条，不要重复'])
def test_followup_resumes_original_goal_without_new_plan(pg_isolated, text):
    current, client, commands = pg_isolated
    cid, plan, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=plan['jobs'])
    response = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'}, json={'content': text})
    assert response.status_code in {200, 202}, response.text
    data = response.json()
    assert data['action'] == 'continue_goal'
    assert data['run']['agent_run_id'] == run['id']
    assert len(current._read_agent_conversation(cid)['plans']) == 1
    assert data['goal']['jobs'][0]['requested'] == 10
    assert len(commands) == 2


def test_ambiguous_addition_needs_selection_without_new_plan(pg_isolated):
    current, client, commands = pg_isolated
    cid, _, _ = start_plan(pg_isolated)
    result = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'}, json={'content': '再来3条'}).json()
    assert result['action'] == 'clarify'
    assert len(current._read_agent_conversation(cid)['plans']) == 1
    assert len(commands) == 1


def test_active_goal_and_duplicate_message_do_not_spawn_worker(pg_isolated):
    current, client, commands = pg_isolated
    cid, _, _ = start_plan(pg_isolated)
    headers = {'X-Workbench': '1', 'Idempotency-Key': 'continue-once'}
    first = client.post(f'/api/conversations/{cid}/messages', headers=headers, json={'content': '继续'})
    again = client.post(f'/api/conversations/{cid}/messages', headers=headers, json={'content': '继续'})
    assert first.status_code in {200, 202}, first.text
    assert again.json()['turn']['turn_id'] == first.json()['turn']['turn_id']
    assert len(commands) == 1


@pytest.mark.parametrize('text', ['继续，但不要上传', '继续完成，只生成本地稿', '继续，不再上传'])
def test_followup_delivery_restriction_is_a_revision_not_old_upload_resume(pg_isolated, text):
    current, client, commands = pg_isolated
    cid, plan, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=plan['jobs'])
    result = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                         json={'content': text})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['action'] == 'amend_goal'
    assert data['run'] is None
    assert data['plan']['delivery'] == 'generate_only'
    assert data['plan']['continuation']['scope'] == 'remaining'
    assert len(commands) == 1


def test_explicit_new_batch_has_separate_goal(pg_isolated):
    current, client, _ = pg_isolated
    cid, _, _ = start_plan(pg_isolated)
    result = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                         json={'content': '今天另开一批10条每日新闻'}).json()
    assert result['action'] == 'new_goal'
    assert len(client.get(f'/api/conversations/{cid}/goals').json()['rows']) == 2
    assert len(current._read_agent_conversation(cid)['plans']) == 2


def test_status_plus_continue_keeps_both_actions(pg_isolated):
    current, client, _ = pg_isolated
    cid, plan, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=plan['jobs'])
    result = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                         json={'content': '现在到哪了？继续完成'}).json()
    assert result['action'] == 'continue_goal'
    assert result['run']['agent_run_id'] == run['id']
    assert result['assistant']['content']


def test_accepted_message_is_replayed_after_handler_restart(pg_isolated):
    from backend.conversation_service import ConversationService
    from src.agent.plan_contract import digest
    current, client, _ = pg_isolated
    cid = current.create_agent_conversation()['id']
    coordinator = ConversationService(current)
    request = {'content': '生成1条每日新闻'}
    coordinator.goals.enqueue_turn(coordinator.namespace, cid, uuid4().hex,
        {'kind': 'route', 'state': 'accepted', 'content': request['content'], 'request': request,
         'request_digest': digest(request)}, None, 'accepted-before-restart')
    coordinator.process_pending()
    coordinator.process_pending()
    saved = current._read_agent_conversation(cid)
    assert len(saved['plans']) == 1
    assert len(saved['messages']) == 2


def test_stale_attempt_cannot_control_new_attempt(pg_isolated):
    current, client, _ = pg_isolated
    cid, _, _ = start_plan(pg_isolated)
    goal = client.get(f'/api/conversations/{cid}/goals').json()['rows'][0]
    response = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
        json={'content': '暂停', 'goal_id': goal['goal_id'], 'expected_goal_revision': goal['revision'],
              'expected_attempt_id': 'f' * 32})
    assert response.status_code == 409


@pytest.mark.parametrize('text', ['继续，不要重新开始', '不要另开新任务，继续原任务'])
def test_negated_new_task_instruction_keeps_original_goal(pg_isolated, text):
    current, client, commands = pg_isolated
    cid, plan, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=plan['jobs'])
    response = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                           json={'content': text})
    assert response.status_code == 202
    assert response.json()['action'] == 'continue_goal'
    assert response.json()['run']['agent_run_id'] == run['id']
    assert len(current._read_agent_conversation(cid)['plans']) == 1
    assert len(commands) == 2


@pytest.mark.parametrize('text,state', [('暂停', 'paused'), ('取消任务', 'cancelled')])
def test_inactive_control_persists_goal_state_before_marking_applied(pg_isolated, text, state):
    from backend.conversation_service import ConversationService
    current, client, commands = pg_isolated
    cid, _, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    result = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                         json={'content': text})
    assert result.status_code in {200, 202}, result.text
    data = result.json()
    assert data['turn']['state'] == 'applied'
    coordinator = ConversationService(current)
    assert coordinator.goals.snapshot(coordinator.namespace, data['goal']['goal_id'])['state'] == state
    assert len(commands) == 1


def test_replaying_old_resume_key_never_rebinds_newer_live_attempt(pg_isolated):
    from backend.conversation_service import ConversationService
    current, _, commands = pg_isolated
    cid, plan, original = start_plan(pg_isolated)
    current.jobs[original['id']]['status'] = 'interrupted'
    checkpoint(current, original['id'], jobs=plan['jobs'])
    first = current.resume_agent_run(cid, original['id'], 'resume-first-request-001')
    current.jobs[first['id']]['status'] = 'failed'
    second = current.resume_agent_run(cid, first['id'], 'resume-second-request-002')
    coordinator = ConversationService(current)
    before = coordinator.goals.list(coordinator.namespace, cid)[0]
    assert before['active_attempt_id'] == second['id']
    again = current.resume_agent_run(cid, original['id'], 'resume-first-request-001')
    assert again['id'] == first['id']
    after = coordinator.goals.snapshot(coordinator.namespace, before['goal_id'])
    assert after['active_attempt_id'] == second['id']
    assert after['lease_epoch'] == before['lease_epoch']
    assert len(commands) == 3


def test_continue_cannot_bypass_unconfirmed_amendment_or_direct_resume(pg_isolated):
    current, client, commands = pg_isolated
    cid, original, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=original['jobs'])
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                          json={'content': '剩下改成科技新闻'}).json()
    response = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                           json={'content': '继续'}).json()
    assert response['run'] is None
    assert response['plan']['id'] == proposal['plan']['id']
    assert '确认' in response['assistant']['content']
    with pytest.raises(ValueError, match='PENDING'):
        current.resume_agent_run(cid, run['id'], 'resume-cannot-bypass-001')
    assert len(commands) == 1


def test_cross_day_continue_requires_explicit_original_date_confirmation(pg_isolated):
    from backend.conversation_service import ConversationService
    from psycopg.types.json import Jsonb
    current, client, commands = pg_isolated
    cid, plan, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=plan['jobs'])
    coordinator = ConversationService(current)
    goal = coordinator.goals.list(coordinator.namespace, cid)[0]
    with coordinator.goals.knowledge_store.connection() as conn:
        conn.execute('UPDATE agent.conversation_goals SET payload=payload || %s WHERE namespace=%s AND goal_id=%s',
                     (Jsonb({'target_date': '2026-01-01'}), coordinator.namespace, goal['goal_id']))
    response = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                           json={'content': '继续'}).json()
    assert response['run'] is None
    assert '2026-01-01' in response['assistant']['content']
    with pytest.raises(ValueError, match='DATE'):
        current.resume_agent_run(cid, run['id'], 'cross-day-direct-001')
    confirmed = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                            json={'content': '确认继续2026-01-01的原目标'}).json()
    assert confirmed['run']['agent_run_id'] == run['id']
    assert confirmed['goal']['target_date'] == '2026-01-01'
    assert len(commands) == 2


def test_legacy_without_provable_frozen_run_is_readonly(pg_isolated):
    from backend.conversation_service import ConversationService
    current, client, commands = pg_isolated
    cid, _, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    coordinator = ConversationService(current)
    with coordinator.goals.knowledge_store.connection() as conn:
        conn.execute('DELETE FROM agent.conversation_goals WHERE namespace=%s AND conversation_id=%s',
                     (coordinator.namespace, cid))
    saved = current._read_agent_conversation(cid)
    saved['plans'][0].pop('frozen_execution')
    saved['plans'][0].pop('frozen_execution_hash')
    current._write_agent_conversation(saved)
    assert coordinator.ensure_goals(cid) == []
    response = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                           json={'content': '继续'}).json()
    assert response['run'] is None
    assert '只读' in response['assistant']['content']
    assert len(commands) == 1


def test_proven_legacy_run_projects_only_current_real_pg_receipts(pg_isolated):
    from backend.conversation_service import ConversationService
    from src.agent.artifact_store import AgentArtifactStore
    from src.publish.draft_delivery import content_revision_fingerprint
    from src.storage.models import Post
    current, _, _ = pg_isolated
    cid, original, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    current.persist(current.jobs[run['id']])
    artifacts = AgentArtifactStore(current.conversation_store.knowledge_store)
    artifacts.ensure_schema()
    post = Post(title='verified local artifact', body='source-supported test content')
    post.platform['xhs_draft'] = {'revision_fingerprint': content_revision_fingerprint(post)}
    artifacts.save(run['id'], '0', post, phase='uploaded')
    stale = Post(title='changed artifact', body='original content')
    stale.platform['xhs_draft'] = {'revision_fingerprint': content_revision_fingerprint(stale)}
    stale.body = 'changed after delivery'
    artifacts.save(run['id'], '0', stale, phase='uploaded')
    coordinator = ConversationService(current)
    with coordinator.goals.knowledge_store.connection() as conn:
        conn.execute('DELETE FROM agent.conversation_goals WHERE namespace=%s AND conversation_id=%s',
                     (coordinator.namespace, cid))
    projected = coordinator.ensure_goals(cid)[0]
    assert projected['jobs'][0]['job_id'] == original['jobs'][0]['job_id']
    assert projected['jobs'][0]['delivered_verified'] == 1
    assert projected['jobs'][0]['uncertain'] == 1
    assert coordinator.ensure_goals(cid)[0]['jobs'][0]['delivered_verified'] == 1
