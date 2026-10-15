from copy import deepcopy

import pytest

from test_conversation_continuity import pg_isolated
from test_run_resume_contract import isolated, start_plan, checkpoint


def test_amendment_changes_remaining_only(pg_isolated):
    current, client, commands = pg_isolated
    cid, original, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=original['jobs'])
    frozen = deepcopy(current._read_agent_conversation(cid)['plans'][0]['frozen_execution'])
    response = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
        json={'content': '剩下的改成科技新闻，图片门槛只做参考'})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['action'] == 'amend_goal'
    assert data['turn']['state'] == 'awaiting_confirmation'
    assert len(commands) == 1
    assert data['plan']['image_score_required'] is False
    assert '科技' in data['plan']['jobs'][0]['prompt']
    for new_job, old_job in zip(data['plan']['jobs'][1:], original['jobs'][1:]):
        for field in ('job_id', 'kind', 'count', 'prompt', 'search_keywords', 'topic_preferences', 'evaluation_viewpoint'):
            assert new_job.get(field) == old_job.get(field)
    assert data['plan']['continuation']['scope'] == 'remaining'
    assert current._read_agent_conversation(cid)['plans'][0]['frozen_execution'] == frozen


def test_explicit_additional_quantity_revises_total(pg_isolated):
    _, client, _ = pg_isolated
    cid, _, _ = start_plan(pg_isolated)
    data = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
        json={'content': '每日新闻额外增加3条'}).json()
    assert data['plan']['jobs'][0]['count'] == 13
    assert data['goal']['jobs'][0]['requested'] == 10


def test_confirmed_amendment_forks_thread_once(pg_isolated):
    current, client, commands = pg_isolated
    cid, original, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=original['jobs'])
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
        json={'content': '剩下的改成科技新闻，图片门槛只做参考'}).json()
    headers = {'X-Workbench': '1', 'Idempotency-Key': 'confirm-amendment-once'}
    response = client.post(f"/api/conversations/{cid}/turns/{proposal['turn']['turn_id']}/confirm", headers=headers,
        json={'expected_goal_revision': proposal['goal']['revision'], 'proposal_hash': proposal['plan']['semantic_hash']})
    assert response.status_code in {200, 202}, response.text
    result = response.json()
    assert result['run']['agent_run_id'] != run['id']
    assert result['goal']['goal_id'] == proposal['goal']['goal_id']
    assert result['goal']['revision'] == 2
    again = client.post(f"/api/conversations/{cid}/turns/{proposal['turn']['turn_id']}/confirm", headers=headers,
        json={'expected_goal_revision': proposal['goal']['revision'], 'proposal_hash': proposal['plan']['semantic_hash']})
    assert again.json()['run']['id'] == result['run']['id']
    assert len(commands) == 2


def test_running_amendment_is_durable_until_worker_stops(pg_isolated):
    from backend.conversation_service import ConversationService
    current, client, commands = pg_isolated
    cid, original, run = start_plan(pg_isolated)
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
        json={'content': '剩下改成科技新闻'}).json()
    response = client.post(f"/api/conversations/{cid}/turns/{proposal['turn']['turn_id']}/confirm",
        headers={'X-Workbench': '1', 'Idempotency-Key': 'queued-confirm-once'},
        json={'expected_goal_revision': 1, 'proposal_hash': proposal['plan']['semantic_hash']})
    assert response.status_code == 202, response.text
    assert response.json()['run'] is None
    assert len(commands) == 1
    checkpoint(current, run['id'], jobs=original['jobs'])
    current.jobs[run['id']]['status'] = 'failed'
    ConversationService(current).process_pending()
    assert len(commands) == 2
    turn = ConversationService(current).goals.turn(current.conversation_store.namespace, proposal['turn']['turn_id'])
    assert turn['state'] == 'applied'


def test_generic_plan_confirm_cannot_bypass_amendment_handoff(pg_isolated):
    current, client, commands = pg_isolated
    cid, _, _ = start_plan(pg_isolated)
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
        json={'content': '剩下改成科技新闻'}).json()
    result = client.post(f"/api/plans/{proposal['plan']['id']}/confirm", headers={'X-Workbench': '1'},
        json={'conversation_id': cid, 'version': proposal['plan']['version']})
    assert result.status_code in {200, 202}, result.text
    assert len(commands) == 1


def test_discard_candidate_removes_queued_pause_without_starting_work(pg_isolated):
    from backend.conversation_service import ConversationService
    current, client, commands = pg_isolated
    cid, original, _ = start_plan(pg_isolated)
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                          json={'content': '剩下改成科技新闻'}).json()
    response = client.post(f"/api/conversations/{cid}/turns/{proposal['turn']['turn_id']}/discard", headers={'X-Workbench': '1'})
    assert response.status_code == 200, response.text
    turn = ConversationService(current).goals.turn(current.conversation_store.namespace, proposal['turn']['turn_id'])
    assert turn['state'] == 'cancelled'
    assert response.json()['goal']['active_plan_id'] == original['id']
    assert len(commands) == 1


def test_stale_queued_amendment_cannot_overwrite_later_goal_revision(pg_isolated):
    from backend.conversation_service import ConversationService
    current, client, commands = pg_isolated
    cid, original, run = start_plan(pg_isolated)
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                          json={'content': '剩下改成科技新闻'}).json()
    coordinator = ConversationService(current)
    coordinator.confirm_turn(cid, proposal['turn']['turn_id'], 1, proposal['plan']['semantic_hash'], 'queued-old')
    latest = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                        json={'content': '剩下改成财经新闻'}).json()
    advanced = coordinator.goals.revise(coordinator.namespace, proposal['goal']['goal_id'],
                                       latest['plan'], expected_revision=1)
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=original['jobs'])
    try:
        coordinator.apply_confirmed_turn(cid, proposal['turn']['turn_id'])
    except ValueError:
        pass
    after = coordinator.goals.snapshot(coordinator.namespace, advanced['goal_id'])
    assert after['revision'] == 2
    assert after['active_plan_id'] == latest['plan']['id']
    assert len(commands) == 1


def test_busy_pg_run_lease_keeps_confirmed_handoff_retryable(pg_isolated):
    from backend.conversation_service import ConversationService
    from src.agent.artifact_store import AgentArtifactStore
    current, client, commands = pg_isolated
    cid, original, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=original['jobs'])
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                          json={'content': '剩下改成科技新闻'}).json()
    coordinator = ConversationService(current)
    with AgentArtifactStore(coordinator.goals.knowledge_store).lease(run['id']):
        try:
            coordinator.confirm_turn(cid, proposal['turn']['turn_id'], 1,
                                     proposal['plan']['semantic_hash'], 'busy-handoff')
        except RuntimeError:
            pass
        turn = coordinator.goals.turn(coordinator.namespace, proposal['turn']['turn_id'])
        assert turn['state'] == 'queued'
        assert len(commands) == 1
    coordinator.process_pending()
    coordinator.process_pending()
    assert coordinator.goals.turn(coordinator.namespace, turn['turn_id'])['state'] == 'applied'
    assert len(commands) == 2


@pytest.mark.parametrize('text', ['修改一下', '剩下的改为', '每日新闻改为21条'])
def test_unparsed_or_invalid_amendment_cannot_confirm_empty_patch(pg_isolated, text):
    current, client, commands = pg_isolated
    cid, _, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                          json={'content': text}).json()
    assert proposal['turn']['state'] in {'needs_input', 'awaiting_confirmation'}
    assert proposal['plan']['executable'] is False
    confirmed = client.post(f"/api/conversations/{cid}/turns/{proposal['turn']['turn_id']}/confirm",
        headers={'X-Workbench': '1'}, json={'expected_goal_revision': 1,
                                          'proposal_hash': proposal['plan']['semantic_hash']})
    assert confirmed.status_code == 409, confirmed.text
    assert len(commands) == 1


def test_explicit_continue_retries_confirmed_interrupted_handoff(pg_isolated, monkeypatch):
    from backend.conversation_service import ConversationService
    from backend.plan_service import PlanService
    current, client, commands = pg_isolated
    cid, original, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=original['jobs'])
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                          json={'content': '剩下改成科技新闻'}).json()
    coordinator = ConversationService(current)
    real_confirm = PlanService.confirm
    with monkeypatch.context() as scoped:
        scoped.setattr(PlanService, 'confirm', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('crash before launch')))
        with pytest.raises(RuntimeError, match='crash before launch'):
            coordinator.confirm_turn(cid, proposal['turn']['turn_id'], 1,
                                     proposal['plan']['semantic_hash'], 'interrupted-handoff')
    assert PlanService.confirm is real_confirm
    assert coordinator.goals.turn(coordinator.namespace, proposal['turn']['turn_id'])['state'] == 'interrupted'
    result = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                         json={'content': '继续'}).json()
    assert result['run'] is not None, result
    assert result['goal']['revision'] == 2
    assert result['goal']['active_plan_id'] == proposal['plan']['id']
    assert coordinator.goals.turn(coordinator.namespace, proposal['turn']['turn_id'])['state'] == 'applied'
    assert len(commands) == 2


def test_same_confirmation_retries_after_revision_commit_before_launch(pg_isolated, monkeypatch):
    from backend.conversation_service import ConversationService
    from backend.plan_service import PlanService
    current, client, commands = pg_isolated
    cid, original, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=original['jobs'])
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                          json={'content': '剩下改成科技新闻'}).json()
    coordinator = ConversationService(current)
    args = (cid, proposal['turn']['turn_id'], 1, proposal['plan']['semantic_hash'], 'retry-identical-confirm')
    with monkeypatch.context() as scoped:
        scoped.setattr(PlanService, 'confirm', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('before launch')))
        with pytest.raises(RuntimeError, match='before launch'):
            coordinator.confirm_turn(*args)
    result = coordinator.confirm_turn(*args)
    assert result['run'] is not None
    assert result['goal']['revision'] == 2
    assert coordinator.confirm_turn(*args)['run']['id'] == result['run']['id']
    assert len(commands) == 2


def test_amendment_append_is_idempotent_after_turn_result_write_crash(pg_isolated, monkeypatch):
    from backend.conversation_service import ConversationService
    current, _, _ = pg_isolated
    cid, _, _ = start_plan(pg_isolated)
    coordinator = ConversationService(current)
    real_update = coordinator.goals.update_turn

    def crash(ns, tid, **kwargs):
        if kwargs.get('state') == 'awaiting_confirmation':
            raise RuntimeError('turn write interrupted')
        return real_update(ns, tid, **kwargs)

    request, key = {'content': '剩下改成科技新闻'}, 'append-before-crash'
    with monkeypatch.context() as scoped:
        scoped.setattr(coordinator.goals, 'update_turn', crash)
        with pytest.raises(RuntimeError, match='turn write interrupted'):
            coordinator.handle_message(cid, request, key)
    turn = coordinator.goals.find_turn(coordinator.namespace, cid, key)
    with coordinator.goals.knowledge_store.connection() as conn:
        conn.execute("UPDATE agent.conversation_turns SET claim_until=now()-interval '1 second' WHERE namespace=%s AND turn_id=%s",
                     (coordinator.namespace, turn['turn_id']))
    coordinator.handle_message(cid, request, key)
    assert len(current._read_agent_conversation(cid)['plans']) == 2


def test_goal_handoff_pg_lease_serializes_turns_and_survives_claim_expiry(pg_isolated):
    from backend.conversation_service import ConversationService
    from src.agent.artifact_store import AgentArtifactStore
    from src.agent.plan_contract import digest
    current, client, commands = pg_isolated
    cid, original, run = start_plan(pg_isolated)
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                          json={'content': '剩下改成科技新闻'}).json()
    coordinator = ConversationService(current)
    args = (cid, proposal['turn']['turn_id'], 1, proposal['plan']['semantic_hash'], 'goal-lock-handoff')
    coordinator.confirm_turn(*args)
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=original['jobs'])
    lock_key = 'goal-' + digest([coordinator.namespace, proposal['goal']['goal_id']])[:48]
    with AgentArtifactStore(coordinator.goals.knowledge_store).lease(lock_key):
        result = coordinator.apply_confirmed_turn(cid, proposal['turn']['turn_id'])
        assert result['run'] is None
        assert coordinator.goals.turn(coordinator.namespace, proposal['turn']['turn_id'])['state'] == 'queued'
        assert len(commands) == 1
    coordinator.process_pending()
    assert len(commands) == 2


def test_newer_saved_candidate_prevents_old_queued_goal_mutation(pg_isolated):
    from backend.conversation_service import ConversationService
    from backend.plan_service import PlanService
    current, client, commands = pg_isolated
    cid, original, run = start_plan(pg_isolated)
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                          json={'content': '剩下改成科技新闻'}).json()
    coordinator = ConversationService(current)
    coordinator.confirm_turn(cid, proposal['turn']['turn_id'], 1, proposal['plan']['semantic_hash'], 'old-plan-queued')
    PlanService(current).save(cid, proposal['plan']['id'], {'base_plan_version': proposal['plan']['version'],
        'editable_fields': {'image_score_required': False}}, 'edit-after-queued')
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=original['jobs'])
    with pytest.raises(ValueError):
        coordinator.apply_confirmed_turn(cid, proposal['turn']['turn_id'])
    assert coordinator.goals.snapshot(coordinator.namespace, proposal['goal']['goal_id'])['revision'] == 1
    assert len(commands) == 1


@pytest.mark.parametrize('text', [
    '剩下的图片门槛改成只供参考，选题保持原样',
    '模型改为xxx',
    '剩下的改为保存草稿',
    '剩下的速度改为快一点',
])
def test_non_topic_amendment_preserves_original_proposal_keywords(pg_isolated, text):
    current, client, commands = pg_isolated
    cid, original, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    response = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                           json={'content': text})
    assert response.status_code == 200, response.text
    proposal = response.json()['plan']
    for actual, expected in zip(proposal['jobs'], original['jobs']):
        for field in ('job_id', 'kind', 'count', 'prompt', 'search_keywords', 'topic_preferences'):
            assert actual[field] == expected[field], (text, field, actual[field], expected[field])
    if '图片门槛' in text:
        assert proposal['image_score_required'] is False
    assert len(commands) == 1


def test_unsupported_model_change_needs_input_even_with_valid_image_diff(pg_isolated):
    current, client, commands = pg_isolated
    cid, original, run = start_plan(pg_isolated)
    current.jobs[run['id']]['status'] = 'interrupted'
    response = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
        json={'content': '模型改为xxx，图片门槛只供参考'})
    assert response.status_code == 200, response.text
    data = response.json()
    proposal = data['plan']
    assert proposal['jobs'][0]['search_keywords'] == original['jobs'][0]['search_keywords']
    assert proposal['model_roles'] == original['model_roles']
    assert proposal['image_score_required'] is False
    assert proposal['status'] == 'needs_input'
    assert proposal['executable'] is False
    assert any(e['field'] == 'model_roles' for e in proposal['field_errors'])
    confirmed = client.post(f"/api/conversations/{cid}/turns/{data['turn']['turn_id']}/confirm",
        headers={'X-Workbench': '1'}, json={'expected_goal_revision': 1,
                                          'proposal_hash': proposal['semantic_hash']})
    assert confirmed.status_code == 409, confirmed.text
    assert len(commands) == 1


def test_later_cancel_prevents_already_confirmed_queued_handoff(pg_isolated):
    from backend.conversation_service import ConversationService
    current, client, commands = pg_isolated
    cid, original, run = start_plan(pg_isolated)
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                          json={'content': '剩下改成科技新闻'}).json()
    coordinator = ConversationService(current)
    coordinator.confirm_turn(cid, proposal['turn']['turn_id'], 1, proposal['plan']['semantic_hash'], 'queued-confirm')
    assert len(commands) == 1
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=original['jobs'])
    cancelled = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                           json={'content': '取消任务'}).json()
    assert cancelled['goal']['state'] == 'cancelled'
    coordinator.process_pending()
    assert coordinator.goals.snapshot(coordinator.namespace, proposal['goal']['goal_id'])['state'] == 'cancelled'
    assert len(commands) == 1
    assert coordinator.goals.turn(coordinator.namespace, proposal['turn']['turn_id'])['state'] == 'cancelled'


def test_later_pause_holds_handoff_until_explicit_continue(pg_isolated):
    from backend.conversation_service import ConversationService
    current, client, commands = pg_isolated
    cid, original, run = start_plan(pg_isolated)
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                          json={'content': '剩下改成科技新闻'}).json()
    coordinator = ConversationService(current)
    coordinator.confirm_turn(cid, proposal['turn']['turn_id'], 1, proposal['plan']['semantic_hash'], 'queued-confirm')
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=original['jobs'])
    client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'}, json={'content': '暂停任务'})
    coordinator.process_pending()
    assert coordinator.goals.snapshot(coordinator.namespace, proposal['goal']['goal_id'])['state'] == 'paused'
    assert len(commands) == 1
    response = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'}, json={'content': '继续'})
    assert response.status_code == 202, response.text
    assert response.json()['run'] is not None
    assert len(commands) == 2
