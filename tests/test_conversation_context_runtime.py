from uuid import uuid4

import pytest

from src.agent.conversation_store import PostgresConversationStore
from src.agent.goal_store import GoalStore
from src.knowledge.store import KnowledgeStore


def history():
    store = PostgresConversationStore(KnowledgeStore.from_env(), namespace='runtime_context_' + uuid4().hex)
    cid = uuid4().hex
    messages = []
    for i in range(30):
        messages.extend([{'id': f'{cid}-u{i}', 'role': 'user', 'content': '已完成的历史资料' * 35},
                         {'id': f'{cid}-a{i}', 'role': 'assistant', 'content': '已记录'}])
    store.save({'id': cid, 'context_schema_version': 2, 'messages': messages, 'plans': [], 'runs': []})
    goals = GoalStore(store.knowledge_store)
    goal = goals.create(store.namespace, cid, {'id': uuid4().hex, 'delivery': 'generate_only',
        'jobs': [{'job_id': 'news', 'kind': 'daily_news', 'count': 10, 'prompt': '国际新闻'}]})
    turn = goals.enqueue_turn(store.namespace, cid, cid + '-u29', {'kind': 'status', 'goal_id': goal['goal_id']}, 1, 'applied')
    goals.update_turn(store.namespace, turn['turn_id'], state='applied')
    return store, goals, cid, goal


def test_runtime_compacts_before_a_later_controller_request():
    from src.agent.context_builder import prepare_model_context
    store, goals, cid, goal = history()
    calls = []
    def summarize(payload):
        calls.append(payload['through_seq'])
        return {'summary': '旧资料已处理，继续按当前确认要求执行。', 'decisions': [],
                'superseded_requirements': [], 'open_questions': [], 'artifact_refs': [], 'evidence_refs': []}
    built = prepare_model_context(store, goals, cid, goal['goal_id'], 'controller',
                                   {'context_window': 9000, 'max_output_tokens': 1200}, summarize=summarize)
    assert calls
    assert built['can_send']
    assert built['envelope']['history_summary']['version'] >= 1
    assert built['envelope']['goal']['plan']['jobs'][0]['count'] == 10
    assert cid + '-u0' not in built['manifest']['pinned_message_ids']
    updated = store.get(cid)
    updated['messages'].append({'id': 'latest', 'role': 'user', 'content': '图片门槛只做参考'})
    store.save(updated)
    latest = prepare_model_context(store, goals, cid, goal['goal_id'], 'controller',
                                    {'context_window': 9000, 'max_output_tokens': 1200}, summarize=summarize)
    assert latest['envelope']['recent_exchanges'][-1]['content'] == '图片门槛只做参考'


def test_pending_changes_cannot_disappear_into_compacted_history():
    from src.agent.context_builder import ContextBuilder
    store, goals, cid, goal = history()
    turn = goals.enqueue_turn(store.namespace, cid, cid + '-u1',
        {'kind': 'amend_goal', 'goal_id': goal['goal_id'], 'state': 'awaiting_confirmation'}, 1, 'pending')
    built = ContextBuilder(store, goals).build(cid, goal['goal_id'], 'controller', {'context_window': 100000})
    assert cid + '-u1' in built['manifest']['pinned_message_ids']
    assert cid + '-u0' not in built['manifest']['pinned_message_ids']


def test_concurrent_compaction_does_not_invoke_two_summarizers():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from src.agent.compaction import compact_conversation
    store, _, cid, _ = history()
    entered, release = Event(), Event()
    second_calls = []
    def summarize(payload):
        entered.set()
        assert release.wait(10)
        return {'summary': '历史已整理，当前约定以结构化计划为准。'}
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(compact_conversation, store, cid, summarize=summarize, purpose='controller',
                            model_limits={'context_window': 9000, 'max_output_tokens': 1200})
        try:
            assert entered.wait(5)
            second = compact_conversation(store, cid, summarize=lambda payload: second_calls.append(payload) or {'summary': 'duplicate'},
                purpose='controller', model_limits={'context_window': 9000, 'max_output_tokens': 1200})
            assert second['status'] == 'in_progress'
            assert not second_calls
        finally:
            release.set()
        assert first.result(10)['status'] == 'compacted'


def test_actual_input_feedback_is_persistent_conservative_and_profile_scoped():
    from types import SimpleNamespace
    from src.agent.capabilities import context_usage as usage
    from src.agent.capabilities.store import CapabilityStore
    from src.agent.context_builder import ContextBuilder
    from src.config import LLMConfig

    assert hasattr(usage, 'record_input_usage'), 'actual input feedback is not connected to the estimator'
    store, goals, cid, goal = history()
    caps = CapabilityStore(store.knowledge_store, namespace=store.namespace)
    caps.ensure_schema()
    limits = {'provider': 'offline-a', 'model': 'model-a', 'request_format': 'openai_chat',
              'context_window': 100000, 'max_output_tokens': 1000}
    current = ContextBuilder(store, goals)
    before = current.build(cid, goal['goal_id'], 'controller', limits)
    messages = [{'role': 'user', 'content': 'private-input-canary ' * 40}]
    config = LLMConfig(provider='offline-a', model='model-a', api_key='private-key-canary')
    raw_estimate = usage.input_evidence(messages)['tokens_estimate']
    with usage.observe_context_requests(caps, cid, 'controller'):
        response = usage.invoke_observed(config, messages, lambda: SimpleNamespace(
            content='ok', usage_metadata={'input_tokens': raw_estimate * 2, 'output_tokens': 10000000}))
    assert response.content == 'ok'
    reopened = ContextBuilder(PostgresConversationStore(store.knowledge_store, namespace=store.namespace), goals)
    calibrated = reopened.build(cid, goal['goal_id'], 'controller', limits)
    assert calibrated['tokens_estimate'] >= before['tokens_estimate'] * 2
    assert calibrated['tokens_estimate'] < before['tokens_estimate'] * 3
    assert calibrated['budget'] == before['budget']
    assert calibrated['manifest']['usage_kind'] == 'estimated'
    assert calibrated['calibration']['samples'] == 1
    for changed in ({'provider': 'offline-b'}, {'model': 'model-b'}, {'request_format': 'openai_responses'}):
        assert reopened.build(cid, goal['goal_id'], 'controller', {**limits, **changed})['tokens_estimate'] == before['tokens_estimate']
    usage.record_input_usage(caps, config, messages, {'usage': {'input_tokens': 1, 'output_tokens': 10000000}})
    usage.record_input_usage(caps, config, messages, {'usage': {'output_tokens': 10000000, 'total_tokens': 10000000}})
    after = reopened.build(cid, goal['goal_id'], 'controller', limits)
    assert after['tokens_estimate'] == calibrated['tokens_estimate']
    assert after['calibration']['samples'] == 2
    rows = caps.resources('context_calibration')
    assert len(rows) == 1
    assert 'private-input-canary' not in str(rows) and 'private-key-canary' not in str(rows)


def test_soft_target_miss_still_allows_business_and_does_not_repeat_summary():
    from src.agent.context_builder import prepare_model_context
    store, goals, cid, goal = history()
    limits = {'context_window': 9000, 'max_output_tokens': 1200,
              'system_instructions': 'host ' * 3200}
    calls = []

    def summarize(payload):
        calls.append(payload['through_seq'])
        return {'summary': 'Completed history; follow the current host plan.'}

    built = prepare_model_context(store, goals, cid, goal['goal_id'], 'controller', limits, summarize=summarize)
    assert built['can_send']
    assert built['budget']['target_tokens'] < built['fixed_tokens_estimate'] <= built['input_budget']
    assert built['compaction']['saved'] and built['compaction']['can_continue']
    assert built['compaction']['target_met'] is False
    count = len(calls)
    assert prepare_model_context(store, goals, cid, goal['goal_id'], 'controller', limits,
                                 summarize=summarize)['can_send']
    assert len(calls) == count


@pytest.mark.parametrize('fits', [True, False])
def test_prepare_handles_in_progress_with_a_bounded_recoverable_result(fits):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import time
    from src.agent.compaction import compact_conversation
    from src.agent.context_builder import prepare_model_context

    store, goals, cid, goal = history()
    limits = {'context_window': 16000 if fits else 9000, 'max_output_tokens': 1200}
    entered, release = Event(), Event()
    duplicate = []

    def summarize(payload):
        entered.set()
        assert release.wait(10)
        return {'summary': 'Completed history.'}

    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(compact_conversation, store, cid, summarize=summarize, purpose='controller',
                            model_limits=limits, soft_limit_tokens=256)
        try:
            assert entered.wait(5)
            started = time.monotonic()
            if fits:
                built = prepare_model_context(store, goals, cid, goal['goal_id'], 'controller', limits,
                    summarize=lambda payload: duplicate.append(payload) or {'summary': 'duplicate'})
                assert built['can_send'] and built['compaction']['status'] == 'in_progress'
            else:
                with pytest.raises(ValueError, match='CONTEXT_COMPACTION_IN_PROGRESS') as error:
                    prepare_model_context(store, goals, cid, goal['goal_id'], 'controller', limits,
                        summarize=lambda payload: duplicate.append(payload) or {'summary': 'duplicate'},
                        wait_timeout_seconds=.05)
                assert error.value.retryable is True
            assert time.monotonic() - started < 2
            assert not duplicate
        finally:
            release.set()
        assert first.result(10)['saved']


def test_emergency_preflight_compacts_below_soft_trigger_and_retry_uses_rebuilt_payload():
    from src.agent.context_builder import prepare_model_context
    from src.agent.compaction import invoke_with_compaction
    store, goals, cid, goal = history()
    limits = {'context_window': 100000, 'max_output_tokens': 1200}
    summaries, requests = [], []

    def summarize(payload):
        summaries.append(payload)
        return {'summary': 'Completed history.'}

    built = prepare_model_context(store, goals, cid, goal['goal_id'], 'controller', limits, summarize=summarize)
    assert not summaries

    def invoke():
        requests.append(built['envelope'])
        if len(requests) == 1:
            raise ValueError('context_length_exceeded')
        return 'done'

    def compact():
        nonlocal built
        built = prepare_model_context(store, goals, cid, goal['goal_id'], 'controller', limits,
                                      summarize=summarize, force_compaction=True)
        return {'saved': built['compaction']['saved'], 'can_continue': built['can_send']}

    assert invoke_with_compaction(invoke, compact) == 'done'
    assert len(requests) == 2 and summaries
    assert requests[0]['history_summary'] is None
    assert requests[1]['history_summary']['summary'] == 'Completed history.'
    assert built['compaction']['can_continue']


def test_emergency_flag_cannot_override_manual_policy():
    from src.agent.context_builder import prepare_model_context
    from src.agent.capabilities.store import CapabilityStore
    store, goals, cid, goal = history()
    caps = CapabilityStore(store.knowledge_store, namespace=store.namespace)
    caps.ensure_schema()
    caps.put('policy:context:' + cid, 'policy', {'mode': 'manual', 'soft_threshold': 256, 'keep_recent': 4},
             expected_revision=0)
    calls = []
    with pytest.raises(ValueError, match='CONTEXT_COMPACTION_REQUIRED') as error:
        prepare_model_context(store, goals, cid, goal['goal_id'], 'controller',
            {'context_window': 100000, 'max_output_tokens': 1200},
            summarize=lambda payload: calls.append(payload), force_compaction=True)
    assert error.value.retryable is True
    assert not calls


def test_waiter_reuses_committed_snapshot_without_calling_another_summarizer():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Timer
    from src.agent.compaction import compact_conversation
    from src.agent.context_builder import prepare_model_context

    store, goals, cid, goal = history()
    limits = {'context_window': 9000, 'max_output_tokens': 1200}
    entered, release = Event(), Event()
    calls = []

    def summarize(payload):
        entered.set()
        assert release.wait(10)
        return {'summary': 'Completed history.'}

    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(compact_conversation, store, cid, summarize=summarize,
                            purpose='controller', model_limits=limits)
        timer = Timer(.15, release.set)
        try:
            assert entered.wait(5)
            timer.start()
            built = prepare_model_context(store, goals, cid, goal['goal_id'], 'controller', limits,
                summarize=lambda payload: calls.append(payload), wait_timeout_seconds=2)
            assert built['can_send'] and built['compaction']['saved']
            assert built['compaction']['reused'] and built['compaction']['can_continue']
            assert built['envelope']['history_summary']['summary'] == 'Completed history.'
            assert not calls
        finally:
            release.set()
            timer.cancel()
        assert first.result(10)['saved']


def test_unaudited_feedback_cannot_use_output_only_and_profiles_do_not_share_namespaces():
    from types import SimpleNamespace
    from src.agent.capabilities.context_usage import observe_context_requests, invoke_observed
    from src.agent.capabilities.store import CapabilityStore
    from src.agent.context_builder import ContextBuilder
    from src.config import LLMConfig

    store, goals, cid, goal = history()
    caps = CapabilityStore(store.knowledge_store, namespace=store.namespace)
    caps.ensure_schema()
    config = LLMConfig(provider='offline', model='profile-test', api_key='test-only')
    limits = {'provider': 'offline', 'model': 'profile-test', 'request_format': 'openai_chat',
              'context_window': 100000, 'max_output_tokens': 1000}
    before = ContextBuilder(store, goals).build(cid, goal['goal_id'], 'controller', limits)
    messages = [{'role': 'system', 'content': 'boundaries'}, {'role': 'user', 'content': 'request'}]
    with observe_context_requests(caps, cid, 'controller'):
        invoke_observed(config, messages, lambda: SimpleNamespace(content='ok',
            usage_metadata={'output_tokens': 1000000, 'total_tokens': 1000000}))
    assert ContextBuilder(store, goals).build(cid, goal['goal_id'], 'controller', limits)['tokens_estimate'] == before['tokens_estimate']
    with observe_context_requests(caps, cid, 'controller'):
        invoke_observed(config, messages, lambda: SimpleNamespace(content='ok',
            usage_metadata={'input_tokens': 1000, 'output_tokens': 1}), request_format='openai_responses')
    assert ContextBuilder(store, goals).build(cid, goal['goal_id'], 'controller', limits)['calibration']['samples'] == 0
    assert ContextBuilder(store, goals).build(cid, goal['goal_id'], 'controller', {
        **limits, 'request_format': 'openai_responses'})['calibration']['samples'] == 1
    other_store, other_goals, other_cid, other_goal = history()
    assert ContextBuilder(other_store, other_goals).build(other_cid, other_goal['goal_id'], 'controller', {
        **limits, 'request_format': 'openai_responses'})['calibration']['samples'] == 0


def test_feedback_uses_the_same_request_estimate_captured_before_invocation():
    from types import SimpleNamespace
    from src.agent.capabilities.context_usage import observe_context_requests, invoke_observed, input_evidence
    from src.agent.capabilities.store import CapabilityStore
    from src.config import LLMConfig

    store, _, cid, _ = history()
    caps = CapabilityStore(store.knowledge_store, namespace=store.namespace)
    caps.ensure_schema()
    config = LLMConfig(provider='offline', model='captured-input', api_key='test-only')
    messages = [{'role': 'system', 'content': 'system ' * 50},
                {'role': 'user', 'content': 'actual payload ' * 40}]
    estimated = input_evidence(messages)['tokens_estimate']

    def invoke():
        messages.clear()
        return SimpleNamespace(content='ok', usage_metadata={'input_tokens': estimated * 2})

    with observe_context_requests(caps, cid, 'controller'):
        invoke_observed(config, messages, invoke)
    profile = caps.resources('context_calibration')[0]
    assert profile['factor'] == pytest.approx(2.2)
    request = caps.calls()['rows'][0]['context_requests'][0]
    assert request['tokens_estimate'] == estimated
    assert request['actual_tokens']['input_tokens'] == estimated * 2


def test_preflight_budgets_the_final_sent_manifest_and_external_input():
    from src.agent.compaction import estimate_tokens
    from src.agent.context_builder import prepare_model_context
    store, goals, cid, goal = history()
    external = {'system_prompt': 'exact system', 'payload': {'jobs': ['current']}}
    built = prepare_model_context(store, goals, cid, goal['goal_id'], 'controller',
        {'context_window': 100000, 'max_output_tokens': 1200, 'external_input': external},
        summarize=lambda payload: pytest.fail('summary is not needed'))
    assert built['tokens_estimate'] == estimate_tokens([built['envelope'], external])
    assert built['compaction'] == {'status': 'not_needed', 'saved': False, 'can_continue': True}


@pytest.mark.parametrize('succeeds', [True, False])
def test_compaction_never_changes_business_ledger_lease_or_postgres_checkpoint(succeeds):
    from langgraph.checkpoint.base import empty_checkpoint
    from psycopg import sql
    from src.agent.compaction import compact_conversation
    from src.agent.postgres_checkpoint import postgres_checkpointer

    store, goals, cid, goal = history()
    plan = {**goal['plan'], 'delivery': 'save_draft'}
    goal = goals.revise(store.namespace, goal['goal_id'], plan, expected_revision=goal['revision'])
    epoch = goals.bind_attempt(store.namespace, goal['goal_id'], 'business-attempt', state='paused')
    for index in range(10):
        state = 'delivered' if index < 7 else 'approved' if index < 9 else 'uncertain'
        goals.record_artifact(store.namespace, goal['goal_id'], 'news', 'post-' + str(index), 'version-1',
            state=state, receipt_ref='receipt-' + str(index) if index < 7 else '', lease_epoch=epoch)
    with postgres_checkpointer(store.knowledge_store) as saver:
        checkpoint = empty_checkpoint()
        checkpoint['channel_values'] = {'job_index': 7, 'approved_post_ids': ['post-7', 'post-8']}
        checkpoint['channel_versions'] = {'job_index': '1', 'approved_post_ids': '1'}
        config = saver.put({'configurable': {'thread_id': cid, 'checkpoint_ns': 'business'}}, checkpoint,
            {'source': 'loop', 'step': 7}, checkpoint['channel_versions'])
        saver.put_writes(config, [('pending_upload', 'post-7')], 'upload-task')
        assert saver.get_tuple(config).checkpoint['channel_values']['job_index'] == 7

    def checkpoints():
        with store.knowledge_store.connection() as conn:
            return {table: conn.execute(sql.SQL('SELECT * FROM {} WHERE thread_id=%s ORDER BY 1,2,3').format(
                sql.Identifier(table)), (cid,)).fetchall()
                for table in ('checkpoints', 'checkpoint_blobs', 'checkpoint_writes')}

    before = goals.snapshot(store.namespace, goal['goal_id'])
    graph_before = checkpoints()
    raw_before = store.context_messages(cid)
    assert before['lease_epoch'] == epoch and before['active_attempt_id'] == 'business-attempt'
    assert (before['jobs'][0]['delivered_verified'], before['jobs'][0]['retained_ready'],
            before['jobs'][0]['uncertain'], before['jobs'][0]['remaining']) == (7, 2, 1, 3)

    def summarize(payload):
        if not succeeds:
            raise RuntimeError('controlled summary failure')
        return {'summary': 'Historical material only; no business state changes.'}

    result = compact_conversation(store, cid, summarize=summarize, purpose='controller',
        model_limits={'context_window': 9000, 'max_output_tokens': 1200, 'goal_id': goal['goal_id']})
    assert result['saved'] is succeeds
    assert result['status'] == ('compacted' if succeeds else 'blocked')
    assert goals.snapshot(store.namespace, goal['goal_id']) == before
    assert checkpoints() == graph_before
    assert store.context_messages(cid) == raw_before
    assert bool(store.active_snapshot(cid)) is succeeds


def test_accepted_unapplied_turn_is_pinned_and_cannot_enter_summary_prefix():
    from src.agent.compaction import compact_conversation
    from src.agent.context_builder import ContextBuilder

    store, goals, cid, goal = history()
    message_id = cid + '-u15'
    turn = goals.enqueue_turn(store.namespace, cid, message_id,
        {'kind': 'amend_goal', 'goal_id': goal['goal_id'], 'state': 'accepted'},
        goal['revision'], 'accepted-not-applied')
    before = goals.snapshot(store.namespace, goal['goal_id'])
    limits = {'context_window': 16000, 'max_output_tokens': 1200, 'goal_id': goal['goal_id']}
    built = ContextBuilder(store, goals).build(cid, goal['goal_id'], 'controller', limits)
    assert message_id in built['manifest']['pinned_message_ids']
    sources = []

    def summarize(payload):
        sources.append(payload)
        return {'summary': 'Completed older history.'}

    result = compact_conversation(store, cid, summarize=summarize, purpose='controller',
                                 model_limits=limits, soft_limit_tokens=256)
    assert result['saved'] and sources
    assert store.active_snapshot(cid)['through_seq'] < 31
    assert all(message_id not in [m['id'] for m in source['new_messages']] for source in sources)
    rebuilt = ContextBuilder(store, goals).build(cid, goal['goal_id'], 'controller', limits)
    assert message_id in rebuilt['manifest']['pinned_message_ids']
    assert any(m['id'] == message_id for m in rebuilt['envelope']['recent_exchanges'])
    assert goals.turn(store.namespace, turn['turn_id'])['state'] == 'accepted'
    assert goals.snapshot(store.namespace, goal['goal_id']) == before
