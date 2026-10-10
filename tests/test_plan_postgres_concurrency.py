"""Independent connections and locks, isolated from the production namespace."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from apps.web_service import Workbench
from backend.plan_service import PlanService
from backend.settings import configure_runtime
from src.agent.conversation_store import PostgresConversationStore
from src.agent.plan_contract import PlanContractError


@pytest.fixture
def pair(tmp_path, monkeypatch):
    configure_runtime()
    monkeypatch.setattr(PlanService, '_model_runtime', lambda self, plan: {
        'snapshots':{}, 'legacy_roles':{}, 'legacy_configs':{}, 'namespace':'test'})
    namespace = 'plan_race_' + uuid4().hex
    stores = [PostgresConversationStore(namespace=namespace) for _ in range(2)]
    currents = [Workbench(tmp_path, conversation_store=store) for store in stores]
    for current in currents:
        monkeypatch.setattr(current, 'environment', lambda: {})
        monkeypatch.setattr(current, 'providers', lambda: {'bindings': {}})
        monkeypatch.setattr(current, 'models', lambda: {'rows': []})
    cid = currents[0].create_agent_conversation()['id']
    plan = currents[0].append_agent_message(cid, '生成10条每日新闻，不上传')['plan']
    try:
        yield currents, cid, plan
    finally:
        with stores[0].knowledge_store.connection() as conn:
            conn.execute('DELETE FROM agent.conversations WHERE account_namespace=%s', (namespace,))


def race(currents, action):
    barrier = Barrier(2)
    originals = [current._read_agent_conversation for current in currents]
    for current, original in zip(currents, originals):
        used = [False]

        def read(cid, original=original, used=used):
            row = original(cid)
            if not used[0]:
                used[0] = True
                barrier.wait(timeout=10)
            return row

        current._read_agent_conversation = read

    def call(index):
        try:
            return ('success', action(index))
        except PlanContractError as error:
            return ('conflict', error.status, error.code)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(call, index) for index in range(2)]
            return [future.result(timeout=20) for future in futures]
    finally:
        for current, original in zip(currents, originals):
            current._read_agent_conversation = original


def test_two_connections_save_only_one_revision(pair):
    currents, cid, plan = pair
    base = currents[0]._read_agent_conversation(cid)
    job = PlanService(currents[0]).current_plan(cid)['jobs'][0]
    results = race(currents, lambda i: PlanService(currents[i]).save(cid, plan['id'], {
        'base_plan_version': plan['version'], 'conversation_revision': base['_revision'],
        'editable_fields': {'jobs': [{'target_job_id': job['job_id'], 'count': 5 + i}]}}, 'edit-' + str(i)))
    assert sorted(result[0] for result in results) == ['conflict', 'success']
    assert next(result for result in results if result[0] == 'conflict')[1] == 409
    saved = currents[0]._read_agent_conversation(cid)
    assert len(saved['plans']) == 2
    assert len(saved['plan_requests']) == 1
    assert saved['runs'] == []


def test_two_connections_claim_one_execution_identity(pair):
    currents, cid, plan = pair
    normalized = PlanService(currents[0]).current_plan(cid)
    payload = {'version': plan['version'], 'semantic_hash': normalized['semantic_hash'],
               'configuration_fingerprint': normalized['configuration_fingerprint'], 'skill_mode': 'off', 'skill_names': []}
    results = race(currents, lambda i: PlanService(currents[i]).claim(cid, plan['id'], payload, 'confirm-' + str(i)))
    assert all(result[0] == 'success' for result in results)
    identities = {result[1]['plan']['execution_request_id'] for result in results}
    assert len(identities) == 1
    saved = currents[0]._read_agent_conversation(cid)
    assert saved['plans'][-1]['execution_request_id'] in identities
    assert set(saved['confirmation_requests']) == {'confirm-0', 'confirm-1'}
    assert {receipt['execution_request_id'] for receipt in saved['confirmation_requests'].values()} == identities


def test_save_and_confirm_compete_on_same_database_revision(pair):
    currents, cid, plan = pair
    base = currents[0]._read_agent_conversation(cid)
    normalized = PlanService(currents[0]).current_plan(cid)
    edit = {'base_plan_version': plan['version'], 'conversation_revision': base['_revision'],
            'editable_fields': {'delivery': 'save_draft'}}
    confirm = {'version': plan['version'], 'semantic_hash': normalized['semantic_hash'],
               'skill_mode': 'off', 'skill_names': []}
    results = race(currents, lambda i: PlanService(currents[i]).save(cid, plan['id'], edit, 'edit') if i == 0
                   else PlanService(currents[i]).claim(cid, plan['id'], confirm, 'confirm'))
    assert sorted(result[0] for result in results) == ['conflict', 'success']
    assert next(result for result in results if result[0] == 'conflict')[1] == 409
    saved = currents[0]._read_agent_conversation(cid)
    assert not (len(saved['plans']) == 2 and saved['plans'][0].get('execution_request_id'))


def test_configuration_changes_require_reconfirmation(pair, monkeypatch):
    currents, cid, plan = pair
    service = PlanService(currents[0])
    displayed = service.current_plan(cid)
    monkeypatch.setattr(currents[0], 'environment', lambda: {'MINIMAX_LLM_MODEL': 'different-model'})
    payload = {'version': plan['version'], 'semantic_hash': displayed['semantic_hash'],
               'configuration_fingerprint': displayed['configuration_fingerprint'], 'skill_mode': 'off', 'skill_names': []}
    with pytest.raises(PlanContractError) as error:
        service.claim(cid, plan['id'], payload, 'stale-config')
    assert error.value.code == 'PLAN_CONFIGURATION_CHANGED'
    assert not currents[0]._read_agent_conversation(cid)['plans'][-1].get('execution_request_id')
    refreshed = service.current_plan(cid)
    payload['configuration_fingerprint'] = refreshed['configuration_fingerprint']
    claimed = service.claim(cid, plan['id'], payload, 'refreshed-config')
    assert claimed['plan']['frozen_execution']['configuration_fingerprint'] == refreshed['configuration_fingerprint']


def test_confirmation_key_cannot_be_reused_for_another_plan(pair):
    currents, cid, plan = pair
    service = PlanService(currents[0])
    payload = {'version': plan['version'], 'skill_mode': 'off', 'skill_names': []}
    first = service.claim(cid, plan['id'], payload, 'same-key')
    assert service.claim(cid, plan['id'], payload, 'same-key')['plan']['execution_request_id'] == first['plan']['execution_request_id']
    with pytest.raises(PlanContractError) as error:
        service.claim(cid, plan['id'], {**payload, 'skill_mode': 'auto'}, 'same-key')
    assert error.value.status == 409


def test_additional_confirmation_key_is_reserved_for_the_same_claim(pair):
    currents, cid, plan = pair
    service = PlanService(currents[0])
    payload = {'version': plan['version']}
    original = service.claim(cid, plan['id'], payload, 'first-key')['plan']['execution_request_id']
    assert service.claim(cid, plan['id'], payload, 'alias-key')['plan']['execution_request_id'] == original
    saved = currents[1]._read_agent_conversation(cid)
    assert saved['confirmation_requests']['alias-key']['execution_request_id'] == original
    currents[0].append_agent_message(cid, '生成1条每日新闻，不上传')
    new_plan = service.current_plan(cid)
    with pytest.raises(PlanContractError) as error:
        service.claim(cid, new_plan['id'], {'version': new_plan['version']}, 'alias-key')
    assert error.value.code == 'CONFIRM_REQUEST_CONFLICT'
