import importlib
import importlib.util
import json
from copy import deepcopy

import pytest


class Conversations:
    namespace = 'context-test'

    def __init__(self):
        self.conversation = {'id': 'c', 'plans': [], 'pending_user_changes': []}
        self.messages = [
            {'id': 'u1', 'seq': 1, 'role': 'user', 'content': 'old request'},
            {'id': 'a1', 'seq': 2, 'role': 'assistant', 'content': 'old answer'},
            {'id': 'u2', 'seq': 3, 'role': 'user', 'content': 'continue only remaining'},
            {'id': 'a2', 'seq': 4, 'role': 'assistant', 'content': 'waiting'},
            {'id': 'u3', 'seq': 5, 'role': 'user', 'content': 'remove old preference'},
        ]
        self.snapshot = None

    def get(self, identity):
        return deepcopy(self.conversation)

    def context_messages(self, identity):
        return deepcopy(self.messages)

    def active_snapshot(self, identity):
        return deepcopy(self.snapshot)


class Goals:
    def snapshot(self, namespace, goal_id):
        assert namespace == 'context-test' and goal_id == 'g'
        return {
            'goal': {'goal_id': 'g', 'goal_revision': 3, 'status': 'paused',
                     'confirmed_requirements': ['save drafts only'],
                     'plan': {'jobs': [{'job_id': 'news', 'count': 10}, {'job_id': 'map', 'count': 1}]}},
            'ledger_revision': 8,
            'jobs': [{'job_id': 'news', 'delivered': 7, 'remaining': 3}, {'job_id': 'map', 'remaining': 0}],
        }


def builder(store, goals=None):
    assert importlib.util.find_spec('src.agent.context_builder'), 'ContextBuilder is not implemented'
    module = importlib.import_module('src.agent.context_builder')
    return module.ContextBuilder(conversation_store=store, goal_store=goals)


def test_context_preserves_latest_changes_and_authoritative_state():
    store = Conversations()
    store.snapshot = {'version': 2, 'through_seq': 2, 'summary': 'old target 100; public upload',
                      'constraints': ['old preference']}
    store.conversation['pending_user_changes'] = [{'message_id': 'u3', 'patch': {'image_text': None}}]
    result = builder(store, Goals()).build('c', 'g', 'route_amendment', {'context_window': 16384, 'max_output_tokens': 1024}, 'u3')
    envelope = result['envelope']
    assert envelope['schema_version'] == 'conversation-context.v2'
    assert envelope['goal']['goal_revision'] == 3
    assert envelope['execution_facts']['jobs'][0]['remaining'] == 3
    assert envelope['confirmed_requirements'] == ['save drafts only']
    assert envelope['pending_user_changes'][0]['message_id'] == 'u3'
    assert envelope['history_summary']['authority'] == 'historical_only'
    assert 'old preference' not in envelope['confirmed_requirements']
    assert result['manifest']['pinned_message_ids'] == ['u3']
    assert result['manifest']['retained_sequences'] == [3, 4, 5]


@pytest.mark.parametrize('purpose', ['writer', 'image', 'review'])
def test_context_scopes_writer_image_and_controller_inputs(purpose):
    store = Conversations()
    store.conversation['retrieved_context'] = [
        {'id': 's1', 'job_id': 'news', 'verified': True, 'content': 'official source'},
        {'id': 's2', 'job_id': 'map', 'content': 'unrelated map source'},
        {'id': 's3', 'job_id': 'news', 'image_base64': 'secret-pixels', 'artifact_ref': 'asset:3'},
    ]
    result = builder(store, Goals()).build('c', 'g', purpose, {'job_id': 'news', 'context_window': 16384})
    envelope = result['envelope']
    assert envelope['recent_exchanges'] == []
    assert [job['job_id'] for job in envelope['goal']['plan']['jobs']] == ['news']
    assert 'unrelated map source' not in json.dumps(envelope)
    assert 'secret-pixels' not in json.dumps(envelope)
    assert result['manifest']['retained_sequences'] == []
    assert result['manifest']['source_hashes']


def test_model_window_budget_and_unknown_limit():
    current = builder(Conversations())
    result = current.build('c', None, 'controller', {'context_window': 10000, 'max_output_tokens': 1000})
    assert result['input_budget'] == 6952
    assert result['budget']['trigger_tokens'] == 5214
    assert result['budget']['target_tokens'] == 3476
    assert result['budget']['estimated'] is False
    fallback = current.build('c', None, 'controller', {})
    assert fallback['budget']['context_window'] == 16384 and fallback['budget']['estimated'] is True
    smaller = current.build('c', None, 'controller', {'context_window': 5000, 'max_output_tokens': 1000})
    assert smaller['input_budget'] == 1952


def test_required_input_too_large_is_not_truncated():
    store = Conversations()
    text = 'KEEP_REQUIREMENT ' * 4000
    store.messages[-1]['content'] = text
    result = builder(store).build('c', None, 'controller', {'context_window': 4096, 'max_output_tokens': 1024}, 'u3')
    assert result['status'] == 'too_large'
    assert result['error_code'] == 'CONTEXT_REQUIRED_INPUT_TOO_LARGE'
    assert text in json.dumps(result['envelope'])


def test_system_and_tool_schema_are_counted_as_fixed_input():
    result = builder(Conversations()).build('c', None, 'controller', {
        'context_window': 4096, 'max_output_tokens': 1024,
        'system_instructions': 'boundary ' * 2000, 'tools': [{'name': 'safe', 'description': 'tool ' * 1000}],
    })
    assert result['status'] == 'too_large'
    assert result['error_code'] == 'CONTEXT_REQUIRED_INPUT_TOO_LARGE'


def test_recent_complete_exchanges_and_unfinished_tool_calls_are_retained():
    store = Conversations()
    store.messages = []
    for i in range(12):
        store.messages.extend([{'id': f'u{i}', 'seq': i * 2 + 1, 'role': 'user', 'content': 'x'},
                               {'id': f'a{i}', 'seq': i * 2 + 2, 'role': 'assistant', 'content': 'y'}])
    store.messages[3]['tool_calls'] = [{'id': 'call-old', 'name': 'read', 'args': {}}]
    result = builder(store).build('c', None, 'controller', {'context_window': 16384})
    assert 4 in result['manifest']['retained_sequences']
    assert {21, 22, 23, 24} <= set(result['manifest']['retained_sequences'])
    assert 'a1' in result['manifest']['pinned_message_ids']


def test_context_growth_triggers_compaction_but_fixed_input_still_fits():
    store = Conversations()
    store.messages = [{'id': f'u{i}', 'seq': i + 1, 'role': 'user', 'content': 'history ' * 120} for i in range(30)]
    result = builder(store).build('c', None, 'controller', {'context_window': 8000, 'max_output_tokens': 1000})
    assert result['status'] == 'needs_compaction'
    assert result['tokens_estimate'] > result['budget']['trigger_tokens']
    assert result['manifest']['retained_sequences'] == list(range(1, 31))


def test_actual_input_manifest_is_derived_from_sent_envelope_not_claimed_manifest():
    from src.agent.capabilities.context_usage import input_evidence
    envelope = {'schema_version': 'conversation-context.v2', 'purpose': 'controller',
                'history_summary': {'version': 4, 'through_seq': 10, 'summary': 'historical'},
                'recent_exchanges': [{'seq': 11, 'id': 'u11', 'role': 'user', 'content': 'sent'}],
                'retrieved_context': [{'id': 'source', 'content': 'actual source'}],
                'manifest': {'retained_sequences': [999], 'source_hashes': ['invented']}}
    result = input_evidence([{'role': 'user', 'content': json.dumps(envelope)}])
    assert result['retained_sequences'] == [11]
    assert result['snapshot_version'] == 4 and result['summary_through_seq'] == 10
    assert result['source_hashes'] and 'invented' not in result['source_hashes']
    assert result['usage_kind'] == 'estimated'


def test_flat_goal_snapshot_keeps_revision_and_current_plan():
    class FlatGoals:
        def snapshot(self, namespace, goal_id):
            return {'goal_id': 'g', 'revision': 7, 'state': 'paused',
                    'plan': {'jobs': [{'job_id': 'news', 'count': 10}], 'delivery': 'save_draft'},
                    'ledger_revision': 9, 'jobs': [{'job_id': 'news', 'remaining': 3}]}
    result = builder(Conversations(), FlatGoals()).build('c', 'g', 'controller', {})
    assert result['envelope']['goal']['revision'] == 7
    assert result['envelope']['goal']['plan']['delivery'] == 'save_draft'
    assert result['envelope']['execution_facts']['jobs'][0]['remaining'] == 3


def test_legacy_snapshot_that_split_tool_exchange_restores_call_and_result_pair():
    store = Conversations()
    store.messages = [
        {'id': 'u', 'seq': 1, 'role': 'user', 'content': 'read'},
        {'id': 'a', 'seq': 2, 'role': 'assistant', 'tool_calls': [{'id': 't', 'name': 'read', 'args': {}}]},
        {'id': 't', 'seq': 3, 'role': 'tool', 'tool_call_id': 't', 'content': 'result'},
        {'id': 'u2', 'seq': 4, 'role': 'user', 'content': 'continue'},
    ]
    store.snapshot = {'version': 1, 'through_seq': 2, 'summary': 'old history'}
    result = builder(store).build('c', None, 'controller', {})
    assert result['manifest']['retained_sequences'] == [2, 3, 4]


def test_domain_scope_does_not_copy_other_jobs_through_duplicate_goal_fields():
    class DuplicatedGoals(Goals):
        def snapshot(self, namespace, goal_id):
            state = super().snapshot(namespace, goal_id)
            state['goal']['jobs'] = state['goal']['plan']['jobs']
            return state
    result = builder(Conversations(), DuplicatedGoals()).build('c', 'g', 'writer', {'job_id': 'news'})
    assert 'map' not in json.dumps(result['envelope'])


def test_actual_input_hash_includes_roles_and_tool_calls():
    from src.agent.capabilities.context_usage import input_evidence
    first = input_evidence([{'role': 'user', 'content': 'same'}])
    second = input_evidence([{'role': 'system', 'content': 'same'}])
    tool = input_evidence([{'role': 'user', 'content': 'same', 'tool_calls': [{'id': 'x', 'name': 'read', 'args': {}}]}])
    assert len({first['input_hash'], second['input_hash'], tool['input_hash']}) == 3


def test_unapplied_user_turns_are_all_fixed_not_only_latest_message():
    class UnappliedGoals(Goals):
        def snapshot(self, namespace, goal_id):
            return {**super().snapshot(namespace, goal_id), 'applied_through_seq': 1}
    store = Conversations()
    store.snapshot = {'version': 1, 'through_seq': 4, 'summary': 'past'}
    result = builder(store, UnappliedGoals()).build('c', 'g', 'controller', {})
    assert set(result['manifest']['pinned_message_ids']) == {'u2', 'u3'}
    assert {3, 5} <= set(result['manifest']['retained_sequences'])


def test_writer_does_not_treat_unverified_retrieval_as_verified_evidence():
    store = Conversations()
    store.conversation['retrieved_context'] = [
        {'id': 'good', 'job_id': 'news', 'verified': True, 'content': 'official'},
        {'id': 'bad', 'job_id': 'news', 'verified': False, 'content': 'unverified rumor'}]
    result = builder(store, Goals()).build('c', 'g', 'writer', {'job_id': 'news'})
    assert [source['id'] for source in result['envelope']['retrieved_context']] == ['good']


def test_explicit_zero_window_is_configuration_error_not_unknown_fallback():
    with pytest.raises(ValueError, match='CONTEXT_MODEL_BUDGET_INVALID'):
        builder(Conversations()).build('c', None, 'controller', {'context_window': 0})


@pytest.mark.parametrize('response, expected', [
    ({'usage': {'prompt_tokens': 123, 'completion_tokens': 99}}, {'input_tokens': 123, 'output_tokens': 99}),
    ({'usage': {'input_tokens': 123, 'output_tokens': 99}}, {'input_tokens': 123, 'output_tokens': 99}),
    ({'usage': {'output_tokens': 99, 'total_tokens': 100}}, {'output_tokens': 99, 'total_tokens': 100}),
    ({'usage': {'input_tokens': True, 'output_tokens': -1}}, None),
])
def test_native_usage_normalization_keeps_input_separate_from_output(response, expected):
    from src.agent.capabilities.context_usage import response_tokens
    assert response_tokens(response) == expected


def test_system_message_and_envelope_copy_are_both_budgeted():
    limits = {'context_window': 10000, 'max_output_tokens': 1000, 'system_instructions': 'host ' * 1500}
    current = builder(Conversations())
    once = current.build('c', None, 'controller', limits)
    twice = current.build('c', None, 'controller', {
        **limits, 'external_input': {'system_prompt': 'host ' * 1500, 'payload': {'jobs': []}}})
    assert twice['tokens_estimate'] >= once['tokens_estimate'] + 1500
    assert twice['fixed_tokens_estimate'] >= once['fixed_tokens_estimate'] + 1500
    assert twice['budget'] == once['budget']


def test_response_usage_properties_are_supported_without_instance_dict():
    from src.agent.capabilities.context_usage import response_tokens

    class Response:
        __slots__ = ()

        @property
        def usage_metadata(self):
            return {'input_tokens': 123, 'output_tokens': 4}

    assert response_tokens(Response()) == {'input_tokens': 123, 'output_tokens': 4}
