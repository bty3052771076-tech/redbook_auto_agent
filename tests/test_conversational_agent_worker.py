"""Actual application create_agent middleware, real PG, scripted local model."""

from uuid import uuid4
import time

from langchain_core.messages import AIMessage, ToolMessage
import pytest

from backend.dialogue_worker import run_agent
from src.storage.files import load_post, save_post
from src.storage.models import Post
from test_conversational_agent_cancellation import no_external_work
from test_conversational_agent_framework import ScriptedToolChatModel, pg_agent_checkpointer
from test_conversational_agent_store import dialogue_store


def call(name, arguments, identity):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': arguments, 'id': identity}])


@pytest.fixture
def worker(dialogue_store, pg_agent_checkpointer, tmp_path):
    store, cid = dialogue_store
    _, open_saver = pg_agent_checkpointer
    store.session(cid)
    execution = 'worker-fixture-' + uuid4().hex
    message_id = 'fixture-user-' + uuid4().hex
    store.transition(cid, execution_id=execution, state='starting', input={
        'kind': 'message', 'text': 'Use only offline fixture materials.', 'message_id': message_id})
    store.message(cid, 'user', 'Use only offline fixture materials.', identity=message_id)
    executed = []
    result = {'ok': True, 'status': 'succeeded', 'post_ids': ['fixture-retained-post']}

    def executor(name, arguments, action):
        executed.append((name, arguments, action['id']))
        if result.get('ok') and name in {'generate_news', 'generate_digest'}:
            save_post(Post(id=result['post_ids'][0], title='Retained local fixture', body='Verified fixture body.'),
                      base=tmp_path / 'data')
        return dict(result)

    def invoke(responses):
        model = ScriptedToolChatModel(responses=responses)
        with open_saver() as (saver, _):
            state = run_agent(store, cid, store.session(cid)['execution_id'], model, saver,
                env={'REDBOOK_RUNTIME_ROOT': str(tmp_path)}, directory=tmp_path / 'dialogue', executor=executor)
        return state, model

    def resume(request, answer, responses):
        store.answer(cid, request['id'], request['version'], answer)
        execution = 'worker-resume-' + uuid4().hex
        store.transition(cid, execution_id=execution, state='starting',
            input={'kind': 'resume', 'request_id': request['id'], 'answer': answer})
        return invoke(responses)

    return store, cid, executed, result, invoke, resume


def test_readonly_capability_discovery_does_not_request_generation_authority(worker):
    store, cid, executed, _, invoke, _ = worker
    state, model = invoke([call('capabilities', {}, 'capabilities-call'), AIMessage(content='Capabilities inspected.')])
    assert state['state'] == 'idle'
    assert [name for name, arguments, action in executed] == ['capabilities']
    assert state['requests'] == [] and store.session(cid)['permissions'] == {}
    assert any(isinstance(message, ToolMessage) for message in model.requests[1])


def test_two_generation_rounds_keep_distinct_posts_and_approval(worker, tmp_path):
    store, cid, executed, result, invoke, resume = worker
    first, _ = invoke([call('generate_news', {'material_id': 'source-first', 'column': 'daily_news'}, 'reused-call')])
    finished, _ = resume(first['requests'][0], {'decision': 'approve_once'}, [AIMessage(content='First draft saved locally.')])
    assert finished['state'] == 'idle' and len(executed) == 1

    msg, execution = uuid4().hex, uuid4().hex
    store.event(cid, 'user/message', {'text': 'Generate a different second draft.', 'message_id': msg})
    store.transition(cid, execution_id=execution, state='starting', permissions={},
                     input={'kind': 'message', 'text': 'Generate a different second draft.', 'message_id': msg})
    result['post_ids'] = ['second-draft-fixture']
    waiting, model = invoke([call('generate_news', {'material_id': 'source-second', 'column': 'daily_news'}, 'reused-call')])
    assert waiting['state'] == 'waiting_approval' and len(executed) == 1
    assert any('First draft saved locally.' == item.content for item in model.requests[0])
    done, _ = resume(waiting['requests'][0], {'decision': 'approve_once'}, [AIMessage(content='Second draft saved locally.')])
    assert done['state'] == 'idle'
    assert [args['material_id'] for _, args, _ in executed] == ['source-first', 'source-second']
    assert {row['result']['post_ids'][0] for row in done['actions']} == {'fixture-retained-post', 'second-draft-fixture'}
    assert [m['content'] for m in done['messages'] if m['role'] == 'assistant'] == [
        'First draft saved locally.', 'Second draft saved locally.']
    for identity in ('fixture-retained-post', 'second-draft-fixture'):
        assert load_post(identity, base=tmp_path / 'data').id == identity


def test_goal_continues_at_idle_boundary_preserves_native_approval_and_completes(worker):
    store, cid, executed, result, invoke, resume = worker
    store.transition(cid, goal={'id': uuid4().hex, 'version': 1, 'state': 'active',
        'started_at': time.time(), 'objective': '生成两篇不同的本地稿件', 'stage': 'local',
        'target': 2, 'max_turns': 5, 'turns': 0})
    first, _ = invoke([call('generate_news', {'material_id': 'goal-first', 'column': 'daily_news'}, 'goal-first')])
    assert first['goal']['turns'] == 0 and first['state'] == 'waiting_approval'
    next_turn, model = resume(first['requests'][0], {'decision': 'approve_once'}, [
        AIMessage(content='First stage finished.'),
        call('generate_news', {'material_id': 'goal-second', 'column': 'daily_news'}, 'goal-second'),
    ])
    assert len(model.requests) == 2 and next_turn['goal']['turns'] == 1
    assert next_turn['goal']['counts']['local'] == 1 and next_turn['state'] == 'waiting_approval'
    assert '生成两篇不同的本地稿件' in str(model.requests[-1][0].content)
    result['post_ids'] = ['goal-second-post']
    finished, model = resume(next_turn['requests'][0], {'decision': 'approve_once'}, [AIMessage(content='Second stage finished.')])
    assert len(model.requests) == 1
    assert finished['state'] == 'idle' and finished['goal']['state'] == 'completed'
    assert finished['goal']['counts'] == {'local': 2, 'reviewed': 0, 'saved': 0}
    assert len(executed) == 2


def test_unknown_tool_requests_capability_without_enabling_or_executing_it(worker):
    store, cid, executed, _, invoke, _ = worker
    state, model = invoke([call('install_arbitrary_mcp', {'command': 'never execute this'}, 'unknown-call')])
    assert state['state'] == 'waiting_input'
    assert len(state['requests']) == 1
    request = state['requests'][0]
    assert request['kind'] == 'capability' and 'install_arbitrary_mcp' in request['tool']
    assert executed == [] and store.actions(cid) == []
    assert store.session(cid)['permissions'] == {}
    assert len(model.requests) == 1


def test_approve_once_then_deny_sibling_tool_preserves_success_across_native_resumes(worker):
    store, cid, executed, _, invoke, resume = worker
    first, _ = invoke([call('generate_news', {'material_id': 'fixture-source', 'column': 'daily_news'}, 'generate-call')])
    assert first['state'] == 'waiting_approval' and executed == []
    permission = first['requests'][0]
    assert permission['kind'] == 'approval' and permission['tool'] == 'generate_news'
    pending = store.query('SELECT payload FROM agent.dialogue_requests WHERE id=%s', (permission['id'],), one=True)
    assert pending['payload']['native_interrupt_id']
    second, _ = resume(permission, {'decision': 'approve_once', 'tool': 'save_draft', 'max_calls': 999}, [
        call('save_draft', {'post_id': 'fixture-retained-post'}, 'save-call')])
    assert [name for name, arguments, action in executed] == ['generate_news']
    assert second['state'] == 'waiting_approval'
    save_permission = second['requests'][0]
    assert save_permission['tool'] == 'save_draft'
    assert 'save_draft' not in store.session(cid)['permissions']
    third, model = resume(save_permission, {'decision': 'deny'}, [AIMessage(content='Local draft kept; platform save was denied.')])
    assert third['state'] == 'idle'
    assert [name for name, arguments, action in executed] == ['generate_news']
    assert store.session(cid)['permissions']['save_draft']['denied'] is True
    successful = [row for row in store.actions(cid) if row['status'] == 'succeeded']
    assert len(successful) == 1 and successful[0]['result']['post_ids'] == ['fixture-retained-post']
    assert any(isinstance(message, ToolMessage) and 'PERMISSION_DENIED' in str(message.content)
               for message in model.requests[0])


def test_approve_once_does_not_cover_a_second_changed_action(worker):
    store, cid, executed, _, invoke, resume = worker
    first, _ = invoke([call('generate_news', {'material_id': 'fixture-source-1', 'column': 'daily_news'}, 'generate-1')])
    second, _ = resume(first['requests'][0], {'decision': 'approve_once'}, [
        call('generate_news', {'material_id': 'fixture-source-2', 'column': 'daily_news'}, 'generate-2')])
    assert len(executed) == 1
    assert second['state'] == 'waiting_approval'
    assert second['requests'][0]['arguments']['material_id'] == 'fixture-source-2'


def test_reused_tool_call_id_cannot_reuse_generation_approval_for_platform_save(worker):
    _, _, executed, _, invoke, resume = worker
    first, _ = invoke([call('generate_news', {'material_id': 'fixture-source', 'column': 'daily_news'}, 'reused-call')])
    state, _ = resume(first['requests'][0], {'decision': 'approve_once'}, [
        call('save_draft', {'post_id': 'fixture-retained-post'}, 'reused-call'),
        AIMessage(content='This message must not be reached before separate save approval.'),
    ])
    assert [name for name, arguments, action in executed] == ['generate_news'], (
        'A model-controlled call ID reused generation approval to execute platform save')
    assert state['state'] == 'waiting_approval' and state['requests'][0]['tool'] == 'save_draft'


def test_reused_tool_call_id_cannot_expand_once_grant_to_changed_arguments(worker):
    _, _, executed, _, invoke, resume = worker
    first, _ = invoke([call('generate_news', {'material_id': 'fixture-source-1', 'column': 'daily_news'}, 'reused-call')])
    state, _ = resume(first['requests'][0], {'decision': 'approve_once'}, [
        call('generate_news', {'material_id': 'fixture-source-2', 'column': 'daily_news'}, 'reused-call'),
        AIMessage(content='This message must not be reached before approval of the new source.'),
    ])
    assert len(executed) == 1, 'A model-controlled call ID expanded once approval to different arguments'
    assert state['state'] == 'waiting_approval'
    assert state['requests'][0]['arguments']['material_id'] == 'fixture-source-2'


def test_same_error_after_one_changed_repair_interrupts_before_another_model_call(worker):
    store, cid, executed, result, invoke, _ = worker
    store.grant(cid, 'generate_news', calls=5)
    result.update(ok=False, status='failed', error_code='FIXTURE_BAD_SOURCE', error='same source failure')
    state, model = invoke([
        call('generate_news', {'material_id': 'fixture-source-1', 'column': 'daily_news'}, 'try-1'),
        call('generate_news', {'material_id': 'fixture-source-2', 'column': 'daily_news'}, 'try-2'),
    ])
    assert len(executed) == 2 and len(model.requests) == 2
    assert state['state'] == 'waiting_input'
    assert state['requests'][0]['kind'] == 'error'
    assert len(store.actions(cid)) == 2


def test_switching_generation_tool_cannot_reset_same_error_budget(worker):
    store, cid, executed, result, invoke, _ = worker
    store.grant(cid, 'generate_news', calls=5)
    store.grant(cid, 'generate_digest', calls=5)
    result.update(ok=False, status='failed', error_code='FIXTURE_BAD_SOURCE', error='same source failure')
    state, model = invoke([
        call('generate_news', {'material_id': 'fixture-source-1', 'column': 'daily_news'}, 'news-try'),
        call('generate_digest', {'column': 'ai_digest'}, 'digest-try'),
    ])
    assert len(executed) == 2 and len(model.requests) == 2
    assert state['state'] == 'waiting_input'
    assert state['requests'][0]['kind'] == 'error'


@pytest.mark.parametrize('name,first_args,next_args', [
    ('generate_news', {'material_id': 'fixture-1', 'column': 'daily_news'}, {'material_id': 'fixture-2', 'column': 'daily_wow'}),
    ('generate_digest', {'column': 'ai_digest'}, {'column': 'ai_wool'}),
    ('mcp_call', {'tool_id': 'mcp:fixture:read_one', 'arguments': {}}, {'tool_id': 'mcp:fixture:read_two', 'arguments': {}}),
])
def test_session_grant_does_not_cross_column_or_registered_tool_scope(worker, name, first_args, next_args):
    _, _, executed, _, invoke, resume = worker
    first, _ = invoke([call(name, first_args, 'scope-first')])
    second, _ = resume(first['requests'][0], {'decision': 'approve_session', 'max_calls': 3}, [
        call(name, next_args, 'scope-second')])
    assert len(executed) == 1
    assert second['state'] == 'waiting_approval' and second['requests'][0]['arguments'] == next_args


def test_cached_repeated_success_is_bounded_and_asks_instead_of_infinite_model_loop(worker):
    store, cid, executed, _, invoke, _ = worker
    state, model = invoke([call('capabilities', {}, f'repeated-{index}') for index in range(3)])
    assert len(executed) == 1 and len(store.actions(cid)) == 1
    assert len(model.requests) == 3
    assert state['state'] == 'waiting_input' and state['requests'][0]['kind'] == 'question'


def test_native_interrupt_id_resume_does_not_approve_parallel_sibling(worker):
    store, _, executed, _, invoke, resume = worker
    first, _ = invoke([AIMessage(content='', tool_calls=[
        {'name': 'generate_news', 'args': {'material_id': 'fixture-source', 'column': 'daily_news'}, 'id': 'parallel-generate'},
        {'name': 'mcp_call', 'args': {'tool_id': 'mcp:fixture:read_one', 'arguments': {}}, 'id': 'parallel-mcp'},
    ])])
    assert first['state'] == 'waiting_approval' and len(first['requests']) == 2 and executed == []
    native_ids = [store.query('SELECT payload FROM agent.dialogue_requests WHERE id=%s', (request['id'],), one=True)
                  ['payload']['native_interrupt_id'] for request in first['requests']]
    assert len(set(native_ids)) == 2
    generate = next(request for request in first['requests'] if request['tool'] == 'generate_news')
    second, model = resume(generate, {'decision': 'approve_once'}, [])
    assert second['state'] == 'waiting_approval'
    assert [name for name, arguments, action in executed] == ['generate_news']
    assert model.requests == []
    assert len(second['requests']) == 1 and second['requests'][0]['tool'] == 'mcp_call'
    third, _ = resume(second['requests'][0], {'decision': 'deny'}, [AIMessage(content='Parallel fixture generation retained; MCP denied.')])
    assert third['state'] == 'idle'
    assert [name for name, arguments, action in executed] == ['generate_news']


@pytest.mark.parametrize('status,code', [('missing_capability', 'MODEL_NOT_CONFIGURED'), ('uncertain', 'DRAFT_SAVE_UNCERTAIN')])
def test_configuration_and_uncertain_results_interrupt_after_first_action(worker, status, code):
    store, cid, executed, result, invoke, _ = worker
    store.grant(cid, 'generate_news', calls=5)
    result.update(ok=False, status=status, error_code=code, error='fixture requires user decision')
    state, model = invoke([call('generate_news', {'material_id': 'fixture-source', 'column': 'daily_news'}, 'blocked-call')])
    assert len(executed) == 1 and len(model.requests) == 1
    assert state['state'] == 'waiting_input' and len(state['requests']) == 1
    assert state['requests'][0]['kind'] == ('configuration' if status == 'missing_capability' else 'error')
    if status == 'uncertain':
        assert 'configured' not in state['requests'][0]['allowed_decisions']
