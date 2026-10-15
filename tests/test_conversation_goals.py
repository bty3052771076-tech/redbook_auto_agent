from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from src.agent.conversation_store import PostgresConversationStore
from src.knowledge.store import KnowledgeStore


def goal_store():
    from src.agent.goal_store import GoalStore
    knowledge = KnowledgeStore.from_env()
    namespace = 'goals_' + uuid4().hex
    conversations = PostgresConversationStore(knowledge, namespace=namespace)
    cid = uuid4().hex
    conversations.save({'id': cid, 'messages': [], 'plans': [], 'runs': [], 'created_at': 1})
    goals = GoalStore(knowledge)
    plan = {'id': uuid4().hex, 'jobs': [{'job_id': 'news', 'kind': 'daily_news', 'count': 10}],
            'delivery': 'save_draft'}
    goal = goals.create(namespace, cid, plan)
    return goals, namespace, cid, goal


def test_goal_projection_counts_current_versions_once():
    goals, ns, _, goal = goal_store()
    for i in range(6):
        goals.record_artifact(ns, goal['goal_id'], 'news', str(i), 'v1',
                              state='delivered', origin_run_id='original', receipt_ref='verified:' + str(i))
    goals.record_artifact(ns, goal['goal_id'], 'news', 'retained', 'old', state='rejected')
    goals.record_artifact(ns, goal['goal_id'], 'news', 'retained', 'v2', state='approved')
    goals.record_artifact(ns, goal['goal_id'], 'news', 'retained', 'v2', state='approved')
    snapshot = goals.snapshot(ns, goal['goal_id'])
    assert snapshot['jobs'][0]['delivered_verified'] == 6
    assert snapshot['jobs'][0]['remaining'] == 4
    assert snapshot['jobs'][0]['new_generation_needed'] == 3
    assert snapshot['revision'] == 1
    assert snapshot['ledger_revision'] >= 7


def test_goal_turn_idempotency_and_namespace():
    goals, ns, cid, goal = goal_store()
    args = (ns, cid, 'message', {'kind': 'continue_goal', 'goal_id': goal['goal_id']}, 1, 'same')
    first = goals.enqueue_turn(*args)
    assert goals.enqueue_turn(*args)['turn_id'] == first['turn_id']
    with pytest.raises(ValueError, match='REQUEST_KEY_CONFLICT'):
        goals.enqueue_turn(ns, cid, 'message', {'kind': 'pause_goal', 'goal_id': goal['goal_id']}, 1, 'same')
    with pytest.raises(KeyError):
        goals.snapshot('another_namespace', goal['goal_id'])
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda worker: goals.claim_turn(ns, first['turn_id'], worker), ['one', 'two']))
    assert len([claim for claim in claims if claim]) == 1


def test_uncertain_delivery_is_not_regenerated_or_counted_success():
    goals, ns, _, goal = goal_store()
    goals.record_artifact(ns, goal['goal_id'], 'news', 'pending', 'v1', state='uncertain')
    row = goals.snapshot(ns, goal['goal_id'])['jobs'][0]
    assert row['remaining'] == 10
    assert row['uncertain'] == 1
    assert row['new_generation_needed'] == 9


def test_goal_revision_changes_only_for_confirmed_plan():
    goals, ns, _, goal = goal_store()
    plan = {**goal['plan'], 'id': uuid4().hex, 'jobs': [{'job_id': 'news', 'kind': 'daily_news', 'count': 13}]}
    changed = goals.revise(ns, goal['goal_id'], plan, expected_revision=1)
    assert changed['revision'] == 2
    assert changed['jobs'][0]['requested'] == 13
    with pytest.raises(ValueError, match='GOAL_REVISION_CONFLICT'):
        goals.revise(ns, goal['goal_id'], plan, expected_revision=1)


def test_changed_generation_requirements_invalidate_old_approvals_atomically():
    from copy import deepcopy
    goals, ns, _, goal = goal_store()
    original = deepcopy(goal['plan'])
    original['delivery'] = 'generate_only'
    original['jobs'][0]['prompt'] = 'old theme'
    goal = goals.revise(ns, goal['goal_id'], original, expected_revision=1)
    for i in range(10):
        goals.record_artifact(ns, goal['goal_id'], 'news', str(i), 'v1', state='approved', review_ref='old-review')
    updated = deepcopy(original)
    updated['id'] = uuid4().hex
    updated['jobs'][0]['prompt'] = 'new theme'
    result = goals.revise(ns, goal['goal_id'], updated, expected_revision=2)
    assert result['jobs'][0]['approved_current'] == 0
    assert result['jobs'][0]['remaining'] == 10
    assert {a['eligibility_state'] for a in result['artifacts']} == {'needs_review'}
    goals.record_artifact(ns, goal['goal_id'], 'news', '0', 'v1', state='approved', review_ref='new-review')
    assert goals.snapshot(ns, goal['goal_id'])['jobs'][0]['approved_current'] == 1


def test_both_goal_ddl_entrypoints_wait_for_shared_pg_lock(monkeypatch):
    from threading import Barrier
    from backend import app as module
    knowledge = KnowledgeStore.from_env()
    monkeypatch.setattr(module.KnowledgeStore, 'from_env', lambda **kw: knowledge)
    ready = Barrier(3)

    def run(entry):
        ready.wait(timeout=5)
        entry()

    with ThreadPoolExecutor(2) as pool:
        with knowledge.connection('migration') as blocker, blocker.transaction():
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext('redbook-agent-goal-schema'))")
            futures = [pool.submit(run, entry) for entry in (knowledge.ensure_schema, module.ensure_review_schema)]
            ready.wait(timeout=5)
            import time
            time.sleep(.4)
            waited = [not future.done() for future in futures]
        for future in futures:
            future.result(timeout=10)
    assert waited == [True, True]
    with knowledge.connection() as conn:
        assert conn.execute('SELECT max(version) AS version FROM knowledge.schema_migrations').fetchone()['version'] >= 4


def test_cross_namespace_goal_cannot_be_attached_by_update_turn():
    goals, ns, cid, goal = goal_store()
    other, other_ns, _, foreign = goal_store()
    turn = goals.enqueue_turn(ns, cid, 'message', {'kind': 'route'}, None, 'link-goal')
    with pytest.raises((KeyError, ValueError)):
        goals.update_turn(ns, turn['turn_id'], state='queued', goal_id=foreign['goal_id'])
    assert goals.turn(ns, turn['turn_id'])['goal_id'] is None
    assert other.snapshot(other_ns, foreign['goal_id'])['revision'] == 1


def test_turn_result_cannot_commit_after_goal_epoch_changes():
    goals, ns, cid, goal = goal_store()
    goals.bind_attempt(ns, goal['goal_id'], 'old')
    turn = goals.enqueue_turn(ns, cid, 'message', {'kind': 'pause_goal', 'goal_id': goal['goal_id']}, 1, 'pause-fence')
    claim = goals.claim_turn(ns, turn['turn_id'], 'old-worker')
    goals.bind_attempt(ns, goal['goal_id'], 'new')
    with pytest.raises(ValueError, match='LOST'):
        goals.record_result(claim, {'state': 'applied'})
    assert goals.turn(ns, turn['turn_id'])['state'] == 'applying'


def test_goal_ddl_upgrades_existing_non_cascade_constraint():
    from src.agent.goal_store import ensure_goal_schema
    knowledge = KnowledgeStore.from_env()
    with knowledge.connection('migration') as conn, conn.transaction():
        assert conn.execute('SELECT current_database() AS name').fetchone()['name'].startswith('redbook_agent_test_')
        conn.execute('''ALTER TABLE agent.conversation_goals DROP CONSTRAINT conversation_goals_conversation_id_fkey,
            ADD CONSTRAINT conversation_goals_conversation_id_fkey FOREIGN KEY(conversation_id)
            REFERENCES agent.conversations(conversation_id)''')
        ensure_goal_schema(conn)
        cascade = conn.execute("""SELECT confdeltype FROM pg_constraint
            WHERE conrelid='agent.conversation_goals'::regclass
            AND conname='conversation_goals_conversation_id_fkey'""").fetchone()['confdeltype']
        # Rollback leaves the shared test schema unchanged even on RED.
        conn.execute('''ALTER TABLE agent.conversation_goals DROP CONSTRAINT conversation_goals_conversation_id_fkey,
            ADD CONSTRAINT conversation_goals_conversation_id_fkey FOREIGN KEY(conversation_id)
            REFERENCES agent.conversations(conversation_id) ON DELETE CASCADE''')
    assert cascade == 'c'


def test_goal_migration_keeps_original_confirmed_date():
    from datetime import datetime, timezone
    goals, ns, cid, goal = goal_store()
    plan = {**goal['plan'], 'id': uuid4().hex,
            'confirmed_at': datetime(2026, 1, 1, 4, tzinfo=timezone.utc).timestamp()}
    assert goals.create(ns, cid, plan)['target_date'] == '2026-01-01'
