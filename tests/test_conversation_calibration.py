"""Manual continuation calibration against disposable PostgreSQL state."""
from copy import deepcopy
import json
import threading
import time
from uuid import uuid4

import pytest

from apps.web_service import Workbench
from backend.conversation_service import ConversationService
from backend.plan_service import PlanService
from backend.task_recognition import RecognitionService
from src.agent.conversation_store import PostgresConversationStore
from src.agent.goal_store import GoalStore
from src.agent.plan_contract import apply_edits
from src.knowledge.store import KnowledgeStore


@pytest.fixture
def continuation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    current = Workbench(tmp_path, conversation_store=PostgresConversationStore(
        KnowledgeStore.from_env(), namespace='calibration_' + uuid4().hex))
    monkeypatch.setattr(current, 'providers', lambda: {'bindings': {}})
    monkeypatch.setattr(current, '_run', lambda *a: pytest.fail('calibration started content worker'))
    monkeypatch.setattr(PlanService, 'configuration_fingerprint', lambda self: 'test-model-configuration')
    monkeypatch.setattr(current, 'environment', lambda: {
        'MINIMAX_TOKEN_PLAN_API_KEY': 'test-only-key', 'MINIMAX_LLM_MODEL': 'MiniMax-M3',
        'MINIMAX_BILLING_MODE': 'subscription_only'})
    monkeypatch.setattr(current, 'models', lambda: {'rows': [
        {'id': 'minimax:MiniMax-M3', 'provider': 'minimax', 'model': 'MiniMax-M3',
         'kind': 'llm', 'selectable': True, 'cost_class': 'subscription_included'}]})
    cid = current.create_agent_conversation()['id']
    original = current.append_agent_message(cid,
        '生成10条每日新闻，关键词：伊朗、关税；1条每日AI讯息；1条每日全球事件关注图，保存草稿')['plan']
    saved = current._read_agent_conversation(cid)
    original = apply_edits(original, {'performance_mode': 'speed'})
    original.update(job_id='original-attempt', frozen_execution={'sentinel': 'immutable'})
    saved['plans'][-1] = original
    current._write_agent_conversation(saved)
    goals = GoalStore(current.conversation_store.knowledge_store)
    namespace = current.conversation_store.namespace
    goal = goals.create(namespace, cid, original)
    goals.bind_attempt(namespace, goal['goal_id'], 'original-attempt')
    current.jobs['original-attempt'] = {'id': 'original-attempt', 'status': 'interrupted'}
    for index in range(8):
        goals.record_artifact(namespace, goal['goal_id'], original['jobs'][0]['job_id'],
            f'post-{index}', 'v1', state='delivered' if index < 7 else 'approved',
            review_ref=f'review-{index}', receipt_ref=f'receipt-{index}' if index < 7 else '')
    saved = current._read_agent_conversation(cid)
    current.conversation_store.save_snapshot(cid, expected_revision=saved['_revision'], snapshot={
        'through_seq': saved['_last_message_seq'], 'summary': 'Historical request: retain all three columns.',
        'constraints': [], 'evidence_refs': [], 'task_state': {}, 'input_tokens': 100, 'output_tokens': 20})
    text = '剩下的图片门槛改成只供参考，选题保持原样'
    proposal = ConversationService(current).handle_message(cid, {'content': text}, 'amend-images')
    plan = proposal['plan']
    body = {'source_message_id': proposal['message']['id'], 'base_plan_id': plan['id'],
            'base_plan_version': plan['version']}
    output = {
        'schema_version': 'task-recognition.v3', 'intent': 'revise',
        'jobs': [{key: deepcopy(job[key]) for key in (
            'kind', 'count', 'search_keywords', 'topic_preferences', 'topic_brief',
            'topic_brief_strength', 'content_constraints', 'evaluation_viewpoint')}
            for job in original['jobs']],
        'options': {'delivery': None, 'platform': None, 'performance_mode': None,
                    'image_score_required': False, 'skip_quota_sync': True},
        'provider_requests': {'agent': None, 'writer': None, 'image': None},
        'annotations': [{'id': 'r1', 'normalized_instruction': '图片评分仅供参考', 'evidence_ids': ['u1']}],
        'clarifications': [], 'field_suggestions': [], 'summary': '仅调整剩余稿件图片门槛',
    }
    calls = []

    def invoke(config, payload):
        calls.append(deepcopy(payload))
        return json.dumps(output, ensure_ascii=False)

    recognition = RecognitionService(current, invoke)
    monkeypatch.setattr(current, 'submit', lambda *a, **kw: pytest.fail('calibration executed business'))
    monkeypatch.setattr(current, 'execute_agent_plan', lambda *a, **kw: pytest.fail('calibration executed plan'))
    monkeypatch.setattr(current, 'resume_agent_run', lambda *a, **kw: pytest.fail('calibration resumed business'))
    return current, cid, goals, original, proposal, body, output, calls, recognition


def wait_result(service, cid, rid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = service.get(cid, rid)
        if result['status'] != 'running' and not service.gate.locked():
            return result
        time.sleep(.01)
    pytest.fail('calibration did not finish')


def test_payload_uses_confirmed_plan_history_and_real_goal_job_counts(continuation):
    current, cid, goals, original, proposal, body, _, calls, service = continuation
    before = goals.snapshot(current.conversation_store.namespace, proposal['goal']['goal_id'])
    result = wait_result(service, cid, service.start(cid, body, 'calibrate-context')['id'])
    assert result['status'] == 'ready', result
    assert len(calls) == 1
    payload = calls[0]
    assert payload['user_message'] == '剩下的图片门槛改成只供参考，选题保持原样'
    assert payload['confirmed_plan'] == before['plan']
    assert payload['confirmed_plan']['jobs'] == original['jobs']
    assert payload['user_overrides'] == original['manual_overrides']
    context = payload['context']
    assert context['schema_version'] == 'conversation-context.v2'
    assert context['goal']['plan'] == before['plan']
    assert context['goal']['revision'] == 1
    assert context['history_summary']['summary'] == 'Historical request: retain all three columns.'
    assert context['history_summary']['authority'] == 'historical_only'
    assert any(m['id'] == body['source_message_id'] and m['content'] == payload['user_message']
               for m in context['recent_exchanges'])
    assert context['execution_facts']['ledger_revision'] == 8
    assert context['execution_facts']['jobs'] == before['jobs']
    assert context['execution_facts']['jobs'][0]['delivered_verified'] == 7
    assert context['execution_facts']['jobs'][0]['retained_ready'] == 1
    assert context['execution_facts']['jobs'][0]['new_generation_needed'] == 2
    assert goals.snapshot(current.conversation_store.namespace, before['goal_id']) == before
    assert 'test-only-key' not in json.dumps(payload)
    assert payload['output_schema']['properties']['schema_version']['const'] == 'task-recognition.v3'
    assert 'continuation' not in payload['output_schema']['properties']


def test_candidate_inherits_unrepeated_columns_and_can_be_adopted_without_execution(continuation):
    current, cid, _, original, proposal, body, _, calls, service = continuation
    saved_before = current._read_agent_conversation(cid)
    record = service.start(cid, body, 'calibrate-once')
    result = wait_result(service, cid, record['id'])
    assert result['status'] == 'ready', result
    candidate = result['candidate']
    assert candidate['source_message_id'] == proposal['message']['id'] != original['source_message_id']
    for job, prior in zip(candidate['jobs'], original['jobs'], strict=True):
        for field in ('job_id', 'kind', 'count', 'search_keywords', 'topic_preferences'):
            assert job[field] == prior[field]
    assert candidate['image_score_required'] is False
    assert candidate['delivery'] == 'save_draft'
    assert candidate['performance_mode'] == 'speed'
    metadata = candidate['continuation']
    assert {key: metadata[key] for key in ('goal_id', 'turn_id', 'expected_goal_revision', 'base_plan_id', 'scope')} == {
        'goal_id': proposal['goal']['goal_id'], 'turn_id': proposal['turn']['turn_id'],
        'expected_goal_revision': 1, 'base_plan_id': original['id'], 'scope': 'remaining'}
    assert {'field': 'image_score_required', 'before': True, 'after': False} in metadata['diff']
    assert not any(change['field'] == 'jobs' for change in metadata['diff']), metadata['diff']
    assert candidate['requirements'][0]['verification'] == 'locatable'
    assert candidate['requirements'][0]['semantic_verified'] is False
    assert service.start(cid, body, 'calibrate-once')['id'] == record['id']
    assert len(calls) == 1
    after = current._read_agent_conversation(cid)
    assert after['plans'] == saved_before['plans']
    assert after['runs'] == saved_before['runs'] == []
    adopted = service.adopt(cid, record['id'], body['base_plan_version'])['plan']
    assert adopted['continuation'] == metadata
    assert adopted['source_message_id'] == proposal['message']['id']
    assert current._read_agent_conversation(cid)['plans'][0]['frozen_execution'] == {'sentinel': 'immutable'}
    assert current._read_agent_conversation(cid)['runs'] == []


def test_running_goal_allows_readonly_continuation_calibration(continuation):
    current, cid, goals, _, proposal, body, _, _, service = continuation
    current.jobs['original-attempt']['status'] = 'running'
    before = goals.snapshot(current.conversation_store.namespace, proposal['goal']['goal_id'])
    result = wait_result(service, cid, service.start(cid, body, 'while-running')['id'])
    assert result['status'] == 'ready', result
    assert current.jobs['original-attempt']['status'] == 'running'
    assert goals.snapshot(current.conversation_store.namespace, before['goal_id']) == before


def test_later_calibration_compacts_in_background_before_sending_request(continuation, monkeypatch):
    from src.agent import compaction
    current, _, goals, _, _, _, output, calls, service = continuation
    cid = current.create_agent_conversation()['id']
    saved = current._read_agent_conversation(cid)
    for index in range(30):
        saved['messages'].extend([
            {'id': uuid4().hex, 'role': 'user', 'content': '这是已处理完的历史材料' * 150},
            {'id': uuid4().hex, 'role': 'assistant', 'content': '该轮已结束'}])
    current._write_agent_conversation(saved)
    original = current.append_agent_message(cid, '生成10条每日新闻，保存草稿')['plan']
    saved = current._read_agent_conversation(cid)
    saved['plans'][-1]['job_id'] = 'old-attempt'
    current._write_agent_conversation(saved)
    namespace = current.conversation_store.namespace
    goal = goals.create(namespace, cid, saved['plans'][-1])
    goals.bind_attempt(namespace, goal['goal_id'], 'old-attempt')
    current.jobs['old-attempt'] = {'id': 'old-attempt', 'status': 'interrupted'}
    applied = goals.enqueue_turn(namespace, cid, original['source_message_id'],
        {'kind': 'status', 'goal_id': goal['goal_id']}, 1, 'old-history-applied')
    goals.update_turn(namespace, applied['turn_id'], state='applied')
    proposal = ConversationService(current).handle_message(cid,
        {'content': '剩下的图片门槛改成只供参考'}, 'new-amendment')
    plan = proposal['plan']
    body = {'source_message_id': proposal['message']['id'], 'base_plan_id': plan['id'],
            'base_plan_version': plan['version']}
    entered, release = threading.Event(), threading.Event()
    def summarize(payload, **kwargs):
        entered.set()
        assert release.wait(8)
        return {'summary': '已结束的历史讨论不改变当前已确认计划。'}
    monkeypatch.setattr(compaction, 'minimax_summary', summarize)
    started = time.monotonic()
    try:
        record = service.start(cid, body, 'long-calibration')
        assert time.monotonic() - started < 1
        assert record['status'] == 'running'
        assert entered.wait(5)
        assert calls == []
    finally:
        release.set()
    result = wait_result(service, cid, record['id'])
    assert result['status'] == 'ready', result
    assert len(calls) == 1
    assert calls[0]['context']['history_summary']['version'] >= 1
    assert calls[0]['user_message'] == '剩下的图片门槛改成只供参考'


def test_saved_manual_edits_are_not_confused_with_local_regex_proposal(continuation):
    current, cid, _, original, proposal, body, _, calls, service = continuation
    revised = PlanService(current).save(cid, proposal['plan']['id'], {
        'base_plan_version': proposal['plan']['version'], 'editable_fields': {'platform': 'toutiao'}}, 'manual-platform')['plan']
    body.update(base_plan_id=revised['id'], base_plan_version=revised['version'])
    result = wait_result(service, cid, service.start(cid, body, 'manual-calibration')['id'])
    assert result['status'] == 'ready', result
    assert calls[0]['user_overrides']['options'] == {'performance_mode': 'speed', 'platform': 'toutiao'}
    assert result['candidate']['platform'] == 'toutiao'
    assert result['candidate']['jobs'][0]['search_keywords'] == original['jobs'][0]['search_keywords']


def test_invented_history_evidence_is_not_verified_or_used_as_authorization(continuation):
    _, cid, _, _, _, body, output, _, service = continuation
    output['annotations'][0]['evidence_ids'] = ['historical-summary', 'u999']
    result = wait_result(service, cid, service.start(cid, body, 'bad-evidence')['id'])
    assert result['status'] == 'ready', result
    note = result['candidate']['requirements'][0]
    assert note['verification'] == 'unverified'
    assert note['semantic_verified'] is False
    assert any(w['code'] == 'ANNOTATION_UNVERIFIED' for w in result['candidate']['warnings'])


@pytest.mark.parametrize('field,value', [
    ('expected_goal_revision', 99), ('goal_id', 'missing-goal'),
    ('turn_id', 'missing-turn'), ('scope', 'all'), ('base_plan_id', 'missing-plan')])
def test_invalid_continuation_binding_is_rejected_before_llm(continuation, field, value):
    current, cid, _, _, _, body, _, calls, service = continuation
    saved = current._read_agent_conversation(cid)
    saved['plans'][-1]['continuation'][field] = value
    current._write_agent_conversation(saved)
    with pytest.raises(ValueError, match='CONFLICT'):
        service.start(cid, body, 'invalid-binding')
    assert calls == []


def test_original_message_cannot_replace_current_followup(continuation):
    _, cid, _, original, _, body, _, calls, service = continuation
    body['source_message_id'] = original['source_message_id']
    with pytest.raises(ValueError, match='CONFLICT'):
        service.start(cid, body, 'wrong-source')
    assert calls == []


def test_goal_revision_changed_during_call_marks_candidate_stale(continuation):
    current, cid, goals, original, proposal, body, output, _, _ = continuation
    entered, release = threading.Event(), threading.Event()

    def invoke(config, payload):
        entered.set()
        assert release.wait(5)
        return json.dumps(output, ensure_ascii=False)

    service = RecognitionService(current, invoke)
    record = service.start(cid, body, 'revision-race')
    try:
        assert entered.wait(5)
        goals.revise(current.conversation_store.namespace, proposal['goal']['goal_id'], original, expected_revision=1)
    finally:
        release.set()
    result = wait_result(service, cid, record['id'])
    assert result['status'] == 'stale', result
    with pytest.raises(ValueError, match='CONFLICT'):
        service.adopt(cid, record['id'], body['base_plan_version'])


def test_running_noncontinuation_retains_idle_guard(continuation):
    current, cid, _, _, _, body, _, calls, service = continuation
    current.jobs['original-attempt']['status'] = 'running'
    saved = current._read_agent_conversation(cid)
    saved['plans'][-1].pop('continuation')
    current._write_agent_conversation(saved)
    with pytest.raises(ValueError):
        service.start(cid, body, 'legacy-running')
    assert calls == []
