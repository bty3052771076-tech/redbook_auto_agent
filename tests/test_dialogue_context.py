"""Native context compaction and amended-turn recovery with real PostgreSQL."""
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from test_conversational_agent_cancellation import no_external_work
from test_conversational_agent_framework import pg_agent_checkpointer, ScriptedToolChatModel
from test_conversational_agent_store import dialogue_store
from test_conversational_agent_worker import worker, call


def long_history(store, cid, count=40):
    for index in range(count):
        identity = uuid4().hex
        text = f'History {index}: ' + ('context evidence and user requirement ' * 120)
        store.message(cid, 'user', text, identity=identity)
        store.event(cid, 'user/message', {'text': text, 'message_id': identity}, key='input:' + identity)


def test_native_compaction_keeps_raw_history_and_does_not_reimport_old_events(worker):
    store, cid, executed, _, invoke, _ = worker
    long_history(store, cid)
    original = store.conversations.get(cid, message_limit=100)['messages']
    state, model = invoke([AIMessage(content='User goals and retained evidence summary.'),
                           AIMessage(content='First compacted turn completed.')])
    assert state['state'] == 'idle' and len(model.requests) == 2
    assert len(model.requests[1]) < 25
    assert len(state['messages']) == len(original) + 1
    assert executed == []
    assert [m['id'] for m in state['messages'][:len(original)]] == [m['id'] for m in original]
    identity = uuid4().hex
    store.message(cid, 'user', 'Continue without repeating old work.', identity=identity)
    store.event(cid, 'user/message', {'text': 'Continue without repeating old work.', 'message_id': identity})
    store.transition(cid, state='starting', execution_id=uuid4().hex,
                     input={'kind': 'message', 'text': 'Continue without repeating old work.', 'message_id': identity})
    state, next_model = invoke([AIMessage(content='Only the new turn was processed.')])
    assert len(next_model.requests) == 1
    assert len(next_model.requests[0]) < 26
    assert all(not (isinstance(m, HumanMessage) and str(m.content).startswith('History 0:'))
               for m in next_model.requests[0])
    assert len(state['messages']) == len(original) + 3


def test_invalid_compaction_preserves_original_canonical_messages(worker):
    store, cid, executed, _, invoke, _ = worker
    long_history(store, cid)
    original = store.conversations.get(cid, message_limit=100)['messages']
    with pytest.raises(ValueError, match='CONTEXT_SUMMARY_INVALID'):
        invoke([AIMessage(content='')])
    assert store.conversations.get(cid, message_limit=100)['messages'] == original
    assert executed == []


def test_new_direction_closes_old_pending_tool_without_executing_it(worker):
    store, cid, executed, _, invoke, _ = worker
    first, _ = invoke([call('generate_news', {'material_id': 'fixture', 'column': 'daily_news'}, 'pending-call')])
    assert first['state'] == 'waiting_approval'
    store.query("UPDATE agent.dialogue_requests SET status='superseded' WHERE conversation_id=%s", (cid,))
    identity = uuid4().hex
    store.message(cid, 'user', 'Do not generate; explain the available tools.', identity=identity)
    store.event(cid, 'user/message', {'text': 'Do not generate; explain the available tools.', 'message_id': identity})
    store.transition(cid, state='starting', execution_id=uuid4().hex, permissions={},
        input={'kind': 'message', 'text': 'Do not generate; explain the available tools.', 'message_id': identity})
    state, model = invoke([AIMessage(content='No generation was executed.')])
    assert state['state'] == 'idle' and executed == []
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == 'pending-call' for m in model.requests[0])
    assert model.requests[0][-1].content == 'Do not generate; explain the available tools.'


def test_host_configuration_answer_without_native_interrupt_starts_a_real_model_turn(worker):
    store, cid, executed, _, _, resume = worker
    request = store.request(cid, store.session(cid)['execution_id'], 'configuration', {
        'tool': 'controller', 'reason': 'Fixture pre-launch error.', 'allowed_decisions': ['configured']}, key=uuid4().hex)
    store.transition(cid, state='waiting_input')
    state, model = resume(request, {'decision': 'configured'}, [AIMessage(content='Configuration checked.')])
    assert state['state'] == 'idle' and len(model.requests) == 1
    assert executed == []
    assert 'configured' in model.requests[0][-1].content


def test_public_history_hides_reasoning_without_deleting_raw_canonical_messages(dialogue_store):
    store, cid = dialogue_store
    store.event(cid, 'assistant/message', {'text': '<think>private-event-fixture</think>Public event.'})
    store.message(cid, 'assistant', '<think>private-analysis-fixture</think>Visible answer.', identity=uuid4().hex)
    store.message(cid, 'assistant', '<think>private-analysis-only</think>', identity=uuid4().hex)
    visible = store.snapshot(cid)['messages']
    assert len(visible) == 1 and visible[0]['content'] == 'Visible answer.'
    canonical = store.conversations.get(cid)['messages']
    assert len(canonical) == 2 and '<think>' in canonical[0]['content']
    assert store.snapshot(cid)['events'][0]['payload']['text'] == 'Public event.'


def test_source_metadata_stays_in_ledger_but_not_fresh_or_cached_model_results(worker):
    store, cid, executed, result, invoke, _ = worker
    result['collection'] = {'source_snapshot': 'large-provider-metadata-fixture'}
    result['materials'] = [{'material_id': 'retained-fixture', 'title': 'Retained fixture source'}]
    state, model = invoke([call('news_search', {'query': 'fixture'}, 'first-source-call'),
        call('news_search', {'query': 'fixture'}, 'cached-source-call'), AIMessage(content='Retained materials inspected.')])
    assert len(executed) == 1
    assert store.actions(cid)[0]['result']['collection']['source_snapshot'] == 'large-provider-metadata-fixture'
    for messages in model.requests[1:]:
        outputs = [message.content for message in messages if isinstance(message, ToolMessage)]
        assert outputs and all('large-provider-metadata-fixture' not in content for content in outputs)
        assert all('retained-fixture' in content for content in outputs)


def test_capability_schemas_stay_in_ledger_but_not_repeated_model_context(worker):
    store, cid, executed, result, invoke, _ = worker
    result['tools'] = [{'name': 'generate_news', 'enabled': True, 'policies': [],
                        'input_schema': {'fixture-schema': 'retained-only'}}]
    state, model = invoke([call('capabilities', {}, 'first-discovery'),
        call('capabilities', {}, 'cached-discovery'), AIMessage(content='Ready to select a material.')])
    assert len(executed) == 1 and state['state'] == 'idle'
    assert store.actions(cid)[0]['result']['tools'][0]['input_schema'] == {'fixture-schema': 'retained-only'}
    for messages in model.requests[1:]:
        outputs = [message.content for message in messages if isinstance(message, ToolMessage)]
        assert outputs and all('fixture-schema' not in content for content in outputs)
        assert all('generate_news' in content and 'enabled' in content for content in outputs)


def test_changed_search_keywords_cannot_loop_on_identical_material_pool(worker):
    store, cid, executed, result, invoke, _ = worker
    result['materials'] = [{'material_id': 'retained-fixture', 'url': 'https://fixture.invalid/source'}]
    state, model = invoke([call('news_search', {'query': query}, f'search-{index}')
                           for index, query in enumerate(['fixture-a', 'fixture-b', 'fixture-c'])])
    assert len(executed) == len(model.requests) == 3
    assert state['state'] == 'waiting_input'
    assert state['requests'][0]['tool'] == 'news_search'
    assert '连续三次检索' in state['requests'][0]['reason']
    assert store.session(cid)['permissions'] == {}


def test_native_material_question_resume_keeps_user_answer_and_does_not_replay_search(worker):
    _, _, executed, result, invoke, resume = worker
    result['materials'] = [{'material_id': 'retained-fixture', 'url': 'https://fixture.invalid/source'}]
    first, _ = invoke([call('news_search', {'query': query}, f'search-{index}')
                       for index, query in enumerate(['fixture-a', 'fixture-b', 'fixture-c'])])
    state, model = resume(first['requests'][0], {'decision': 'answer', 'answer': 'Use retained fixture evidence.'}, [
        AIMessage(content='The user answer was received; no source request was replayed.')])
    assert state['state'] == 'idle' and len(executed) == 3
    assert isinstance(model.requests[0][-1], HumanMessage)
    assert 'Use retained fixture evidence.' in model.requests[0][-1].content


def seed_interrupted_parallel_turn(store, cid, open_saver):
    from backend.dialogue_worker import AgentRuntime, build_agent
    pending = AIMessage(content='', id='interrupted-model-fixture', tool_calls=[
        {'name': 'list_materials', 'args': {'query': 'fixture-a'}, 'id': 'pending-a'},
        {'name': 'list_materials', 'args': {'query': 'fixture-b'}, 'id': 'pending-b'},
        {'name': 'news_search', 'args': {'query': 'fixture-c'}, 'id': 'completed-c'}])
    with open_saver() as (saver, _):
        agent = build_agent(AgentRuntime(store, cid, store.session(cid)['execution_id']),
                            ScriptedToolChatModel(responses=[]), saver)
        agent.update_state({'configurable': {'thread_id': store.session(cid)['thread_id']}}, {'messages': [
            HumanMessage(content='Original fixture goal.'), pending,
            ToolMessage(content='Actual retained result.', tool_call_id='completed-c'),
            HumanMessage(content='Pause fixture turn.', id='pause-fixture')]}, as_node='tools')


@pytest.mark.parametrize('continuation', ['message', 'controller_recheck'])
def test_partial_parallel_results_are_closed_before_new_input_without_replay(worker, pg_agent_checkpointer, continuation):
    store, cid, executed, _, invoke, resume = worker
    _, open_saver = pg_agent_checkpointer
    seed_interrupted_parallel_turn(store, cid, open_saver)
    if continuation == 'controller_recheck':
        request = store.request(cid, store.session(cid)['execution_id'], 'error', {
            'tool': 'controller', 'reason': 'Incomplete fixture context.', 'allowed_decisions': ['configured']}, key=uuid4().hex)
        store.transition(cid, state='waiting_input')
        state, model = resume(request, {'decision': 'configured'}, [AIMessage(content='Recheck continued without replay.')])
    else:
        state, model = invoke([AIMessage(content='New direction continued without replay.')])
    assert state['state'] == 'idle' and executed == []
    messages = model.requests[0]
    pause_index = next(index for index, message in enumerate(messages) if message.id == 'pause-fixture')
    results = {m.tool_call_id: m for m in messages[:pause_index] if isinstance(m, ToolMessage)}
    assert set(results) == {'pending-a', 'pending-b', 'completed-c'}
    assert results['completed-c'].content == 'Actual retained result.'
    assert results['pending-a'].status == results['pending-b'].status == 'error'
    assert '不自动重放' in results['pending-a'].content
    assert store.actions(cid) == []
