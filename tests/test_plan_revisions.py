from copy import deepcopy
from uuid import uuid4

import pytest

from test_task_calibration import workbench
from src.agent.plan_contract import PlanContractError


def setup_plan(current):
    cid = current.create_agent_conversation()['id']
    plan = current.append_agent_message(cid, '生成10条每日新闻，关键词：隐私、退款，不上传')['plan']
    return cid, plan


def body(plan, **fields):
    return {'base_plan_version': plan['version'], 'conversation_revision': 0,
            'editable_fields': fields, 'review_decisions': []}


def test_save_recompiles_appends_and_is_idempotent_without_execution(workbench):
    from backend.plan_service import PlanService
    service = PlanService(workbench)
    cid, plan = setup_plan(workbench)
    job = service.current_plan(cid)['jobs'][0]
    request = body(plan, jobs=[{'target_job_id': job['job_id'], 'count': 5,
                              'search_keywords': [], 'topic_preferences': ['游戏退款'], 'topic_brief': ''}])
    result = service.save(cid, plan['id'], request, 'edit-1')
    assert result['plan']['parent_plan_id'] == plan['id']
    assert result['plan']['jobs'][0]['count'] == 5
    assert '隐私' not in result['plan']['jobs'][0]['prompt']
    assert '游戏退款' in result['plan']['jobs'][0]['prompt']
    assert service.save(cid, plan['id'], request, 'edit-1')['plan']['id'] == result['plan']['id']
    saved = workbench.get_agent_conversation(cid)
    assert len(saved['plans']) == 2
    assert saved['runs'] == []
    assert '编辑' in saved['messages'][-1]['content']


def test_changed_key_and_stale_edit_are_conflicts(workbench):
    from backend.plan_service import PlanService
    service = PlanService(workbench)
    cid, plan = setup_plan(workbench)
    service.save(cid, plan['id'], body(plan, delivery='save_draft'), 'same')
    for key in ('same', 'another'):
        with pytest.raises(PlanContractError) as error:
            service.save(cid, plan['id'], body(plan, delivery='generate_only'), key)
        assert error.value.status == 409


def test_invalid_fields_can_be_saved_but_not_executed(workbench):
    from backend.plan_service import PlanService
    service = PlanService(workbench)
    cid, plan = setup_plan(workbench)
    job = service.current_plan(cid)['jobs'][0]
    result = service.save(cid, plan['id'], body(plan, jobs=[{'target_job_id': job['job_id'], 'count': 0}]), 'invalid')
    assert result['plan']['status'] == 'needs_input'
    assert result['plan']['executable'] is False


def test_restore_creates_revision_and_rebuilds_user_overrides(workbench):
    from backend.plan_service import PlanService
    service = PlanService(workbench)
    cid, plan = setup_plan(workbench)
    job = service.current_plan(cid)['jobs'][0]
    edited = service.save(cid, plan['id'], body(plan, jobs=[{'target_job_id': job['job_id'], 'count': 5}]), 'edit')['plan']
    restored = service.restore(cid, edited['id'], {**body(edited), 'restore_plan_id': plan['id']}, 'restore')['plan']
    assert restored['id'] not in (plan['id'], edited['id'])
    assert restored['jobs'][0]['count'] == 10
    assert restored['manual_overrides']['jobs'][job['job_id']]['count'] == 10


def test_adopt_invalid_candidate_can_be_edited_and_suggestion_selection_is_explicit(workbench):
    from backend.plan_service import PlanService
    from src.agent.plan_contract import apply_edits, normalize_plan, digest, field_path
    service = PlanService(workbench)
    cid, plan = setup_plan(workbench)
    base = normalize_plan(plan)
    job = base['jobs'][0]
    base = apply_edits(base, {'jobs': [{'target_job_id': job['job_id'], 'count': 5}]})
    saved = workbench._read_agent_conversation(cid)
    saved['plans'][-1] = base
    candidate = deepcopy(base)
    candidate['field_suggestions'] = [{'field_path': field_path(job, 'count'), 'value': 8,
                                      'base_value_hash': digest(5), 'reason': '建议'}]
    saved['task_recognitions'] = [{'id': 'recognition', 'status': 'ready', 'base_plan_id': base['id'],
                                 'base_plan_version': base['version'], 'candidate': candidate}]
    workbench._write_agent_conversation(saved)
    payload = {**body(base), 'accepted_candidate_paths': [field_path(job, 'count')],
               'editable_fields': {'jobs': [{'target_job_id': job['job_id'], 'count': 6}]}}
    adopted = service.adopt(cid, 'recognition', payload, 'adopt')['plan']
    assert adopted['jobs'][0]['count'] == 6
    assert adopted['manual_overrides']['jobs'][job['job_id']]['count'] == 6
    assert service.adopt(cid, 'recognition', payload, 'adopt')['plan']['id'] == adopted['id']


def test_postgres_conversation_namespace_blocks_cross_scope():
    from backend.settings import configure_runtime
    configure_runtime()
    from src.agent.conversation_store import PostgresConversationStore
    store = PostgresConversationStore(namespace='plan_test_' + uuid4().hex)
    other = PostgresConversationStore(namespace=store.namespace + '_other')
    cid = uuid4().hex
    try:
        saved = store.save({'id': cid, 'title': '隔离测试', 'messages': [], 'plans': []})
        assert saved['_revision'] == 1
        with pytest.raises(KeyError):
            other.get(cid)
        assert other.list() == []
        with pytest.raises(KeyError):
            other.context_messages(cid)
    finally:
        with store.knowledge_store.connection() as conn:
            conn.execute('DELETE FROM agent.conversations WHERE conversation_id=%s AND account_namespace=%s',
                         (cid, store.namespace))
