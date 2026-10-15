"""Quantity reductions and receipts arriving between proposal and confirmation."""
from backend.conversation_service import ConversationService
from test_conversation_continuity import pg_isolated
from test_run_resume_contract import isolated, start_plan, checkpoint


def prepare(pg_isolated):
    current, client, _ = pg_isolated
    cid, plan, run = start_plan(pg_isolated)
    coordinator = ConversationService(current)
    goal = coordinator.ensure_goals(cid)[0]
    for index in range(9):
        coordinator.goals.record_artifact(coordinator.namespace, goal['goal_id'], plan['jobs'][0]['job_id'],
            str(index), 'v1', state='delivered' if index < 7 else 'approved',
            review_ref='review', receipt_ref='verified:' + str(index) if index < 7 else '')
    current.jobs[run['id']]['status'] = 'interrupted'
    checkpoint(current, run['id'], jobs=plan['jobs'])
    return current, client, cid, plan, run, coordinator, goal


def test_reducing_target_below_delivered_preserves_existing_posts(pg_isolated):
    current, client, cid, _, _, coordinator, goal = prepare(pg_isolated)
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                          json={'content': '每日新闻总数改为5条'}).json()
    response = coordinator.confirm_turn(cid, proposal['turn']['turn_id'], 1,
                                       proposal['plan']['semantic_hash'], 'reduce-total')
    assert response['run'] is not None
    current_goal = coordinator.goals.snapshot(coordinator.namespace, goal['goal_id'])
    news = current_goal['jobs'][0]
    assert (news['requested'], news['delivered_verified'], news['remaining']) == (5, 7, 0)
    assert len([a for a in current_goal['artifacts'] if a['eligibility_state'] == 'delivered']) == 7
    manifest = current._read_agent_conversation(cid)['plans'][-1]['revision_manifest']
    assert manifest['jobs'][0]['execute_count'] == 0


def test_confirmation_uses_receipt_arriving_after_proposal_without_revision_conflict(pg_isolated):
    current, client, cid, plan, _, coordinator, goal = prepare(pg_isolated)
    proposal = client.post(f'/api/conversations/{cid}/messages', headers={'X-Workbench': '1'},
                          json={'content': '剩下改成科技新闻'}).json()
    coordinator.goals.record_artifact(coordinator.namespace, goal['goal_id'], plan['jobs'][0]['job_id'],
                                     '7', 'v1', state='delivered', review_ref='review', receipt_ref='verified:7')
    response = coordinator.confirm_turn(cid, proposal['turn']['turn_id'], 1,
                                       proposal['plan']['semantic_hash'], 'latest-receipt')
    assert response['run'] is not None
    current_goal = coordinator.goals.snapshot(coordinator.namespace, goal['goal_id'])
    assert current_goal['revision'] == 2
    assert current_goal['jobs'][0]['delivered_verified'] == 8
    manifest = current._read_agent_conversation(cid)['plans'][-1]['revision_manifest']
    assert manifest['jobs'][0]['delivered_before'] == 8
    assert manifest['jobs'][0]['execute_count'] == 2
    assert [a['post_id'] for a in manifest['jobs'][0]['retained']] == ['8']
