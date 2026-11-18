"""Real PostgreSQL session/action/request ledger acceptance, no external tools."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from backend.dialogue_store import DialogueStore
from src.knowledge.store import KnowledgeStore
from test_conversational_agent_cancellation import no_external_work


@pytest.fixture
def dialogue_store():
    store = DialogueStore(KnowledgeStore.from_env(), namespace='dialogue_acceptance_' + uuid4().hex)
    with store.knowledge.connection() as conn:
        assert conn.execute('SELECT current_database() AS name').fetchone()['name'].startswith('redbook_agent_test_')
    store.setup()
    cid = uuid4().hex
    store.conversations.save({'id': cid, 'messages': [], 'plans': [], 'runs': [], 'created_at': 1})
    return store, cid


def test_canonical_messages_are_idempotent_and_preserve_earlier_history(dialogue_store):
    store, cid = dialogue_store
    for index in range(4):
        store.message(cid, 'user', f'fixture history {index}', identity=f'history-{index}')
    before = store.conversations.get(cid)['messages']
    new = store.message(cid, 'assistant', 'fixture reply', identity='fixture-answer')
    assert store.message(cid, 'assistant', 'fixture reply', identity='fixture-answer') == new
    after = store.conversations.get(cid)['messages']
    assert after[:-1] == before and after[-1] == new
    with pytest.raises(ValueError, match='MESSAGE_ID_CONFLICT'):
        store.message(cid, 'assistant', 'different reply', identity='fixture-answer')
    foreign = DialogueStore(store.knowledge, namespace='another-namespace')
    with pytest.raises(KeyError):
        foreign.message(cid, 'assistant', 'must not be added')
    assert store.conversations.get(cid)['messages'] == after


def test_concurrent_action_claim_admits_exactly_one_worker(dialogue_store):
    store, cid = dialogue_store

    def claim(index):
        return store.claim_action(cid, 'execution-1', f'call-{index}', 'save_draft',
                                  {'post_id': 'fixture-post'}, business_key='fixture-post:version-1')

    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(claim, [0, 1]))
    assert sum(admitted for row, admitted in claims) == 1
    assert claims[0][0]['id'] == claims[1][0]['id']
    assert len(store.actions(cid)) == 1


def test_completed_action_is_reused_across_tool_call_ids_but_not_content_versions(dialogue_store):
    store, cid = dialogue_store
    row, admitted = store.claim_action(cid, 'execution-1', 'call-1', 'save_draft',
                                      {'post_id': 'fixture-post'}, business_key='fixture-post:version-1')
    assert admitted
    store.finish_action(row['id'], 'completed', {'post_id': 'fixture-post', 'receipt': 'fixture-only'})
    reused, admitted = store.claim_action(cid, 'execution-2', 'different-call-id', 'save_draft',
                                         {'post_id': 'fixture-post'}, business_key='fixture-post:version-1')
    assert not admitted and reused['id'] == row['id']
    assert reused['status'] == 'completed' and reused['result']['receipt'] == 'fixture-only'
    changed, admitted = store.claim_action(cid, 'execution-2', 'new-version-call', 'save_draft',
                                          {'post_id': 'fixture-post'}, business_key='fixture-post:version-2')
    assert admitted and changed['id'] != row['id']


def test_abandoned_inflight_action_is_uncertain_and_not_replayed(dialogue_store):
    store, cid = dialogue_store
    row, _ = store.claim_action(cid, 'execution-1', 'call-1', 'save_draft',
                                {'post_id': 'fixture-post'}, business_key='fixture-post:version-1')
    done, _ = store.claim_action(cid, 'execution-1', 'call-2', 'generate_news', {'material_id': 'fixture-material'})
    store.finish_action(done['id'], 'completed', {'post_id': 'fixture-success'})
    store.abandon(cid, 'execution-1')
    replay, admitted = store.claim_action(cid, 'execution-2', 'new-call', 'save_draft',
                                         {'post_id': 'fixture-post'}, business_key='fixture-post:version-1')
    assert not admitted and replay['id'] == row['id'] and replay['status'] == 'uncertain'
    assert next(action for action in store.actions(cid) if action['id'] == done['id'])['status'] == 'completed'


def test_pending_request_answer_is_versioned_owned_and_idempotent(dialogue_store):
    store, cid = dialogue_store
    request = store.request(cid, 'execution-1', 'permission',
        {'tool': 'generate_news', 'scope': 'request'}, key='fixture-request')
    assert store.request(cid, 'execution-1', 'permission', {'tool': 'generate_news', 'scope': 'request'},
                         key='fixture-request')['id'] == request['id']
    with pytest.raises(ValueError, match='REQUEST_VERSION_CONFLICT'):
        store.answer(cid, request['id'], request['version'] + 1, {'decision': 'allow'})
    with pytest.raises(ValueError, match='REQUEST_VERSION_CONFLICT'):
        store.answer(uuid4().hex, request['id'], request['version'], {'decision': 'allow'})
    answer = {'decision': 'deny'}
    answered = store.answer(cid, request['id'], request['version'], answer)
    assert answered['answer'] == answer and answered['status'] == 'answered'
    assert store.answer(cid, request['id'], request['version'], answer)['id'] == request['id']
    with pytest.raises(ValueError, match='REQUEST_VERSION_CONFLICT'):
        store.answer(cid, request['id'], request['version'], {'decision': 'allow'})


def test_tool_grant_enforces_exact_tool_post_scope_and_count(dialogue_store):
    store, cid = dialogue_store
    store.session(cid)
    store.transition(cid, state='running', execution_id='fixture-execution')
    store.grant(cid, 'save_draft', calls=1, post_ids=['fixture-owned-post'])
    assert store.consume_grant(cid, 'fixture-execution', 'create_image', {'post_id': 'fixture-owned-post'}) is False
    assert store.consume_grant(cid, 'fixture-execution', 'save_draft', {'post_id': 'outside-scope'}) is False
    assert store.consume_grant(cid, 'fixture-execution', 'save_draft', {'post_id': 'fixture-owned-post'}) is True
    assert store.consume_grant(cid, 'fixture-execution', 'save_draft', {'post_id': 'fixture-owned-post'}) is False


def test_unused_old_execution_grant_cannot_authorize_replacement_execution(dialogue_store):
    store, cid = dialogue_store
    store.session(cid)
    store.transition(cid, state='running', execution_id='old-execution')
    store.grant(cid, 'save_draft', calls=1, post_ids=['fixture-owned-post'])
    store.transition(cid, state='running', execution_id='replacement-execution')
    assert store.consume_grant(cid, 'replacement-execution', 'save_draft', {'post_id': 'fixture-owned-post'}) is False, (
        'Unused authorization leaked from an older execution into its replacement')


@pytest.mark.parametrize('field', ['column', 'tool_id', 'skill_id'])
def test_grant_argument_scope_cannot_change_column_registered_tool_or_skill(dialogue_store, field):
    store, cid = dialogue_store
    store.session(cid)
    store.transition(cid, state='running', execution_id='fixture-execution')
    store.grant(cid, 'fixture-tool', calls=1, arguments_scope={field: 'approved-value'})
    assert store.consume_grant(cid, 'fixture-execution', 'fixture-tool', {field: 'different-value'}) is False
    assert store.consume_grant(cid, 'fixture-execution', 'fixture-tool', {field: 'approved-value'}) is True
