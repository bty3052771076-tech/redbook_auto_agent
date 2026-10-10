"""Confirmed plans own their capability versions, in the same PG transaction."""
import json
from copy import deepcopy

import pytest

from test_plan_postgres_concurrency import pair
from backend.capabilities import manager
from backend.plan_service import PlanService
from src.agent.plan_contract import apply_edits, normalize_plan


@pytest.fixture
def setup(pair, monkeypatch):
    currents, cid, plan = pair
    monkeypatch.setattr(PlanService, '_model_runtime', lambda self, plan: {
        'snapshots': {}, 'legacy_roles': {}, 'legacy_configs': {}, 'namespace': 'test'}, raising=False)
    return currents, cid, plan


def payload(service, cid):
    plan = service.current_plan(cid)
    return {'version': plan['version'], 'semantic_hash': plan['semantic_hash'],
            'configuration_fingerprint': plan['configuration_fingerprint'],
            'skill_mode': plan.get('skill_mode', 'off'), 'skill_names': plan.get('skill_names', [])}


def test_capability_fields_are_explicit_execution_inputs():
    base = normalize_plan({'id': 'plan', 'jobs': [{'kind': 'daily_news', 'count': 1}]})
    changed = apply_edits(base, {'skill_mode': 'manual', 'skill_names': ['skill_test'],
                                'disabled_tools': ['builtin:image.generate']})
    assert changed['semantic_hash'] != base['semantic_hash']
    assert changed['manual_overrides']['options']['skill_mode'] == 'manual'


def test_capability_adjustment_appends_revision_without_starting(setup):
    currents, cid, plan = setup
    current = currents[0]
    result = manager(current).save_plan(cid, plan['id'], {
        'version': plan['version'], 'skill_mode': 'off', 'skill_names': [],
        'disabled_tools': ['builtin:image.generate']})
    saved = current._read_agent_conversation(cid)
    assert len(saved['plans']) == 2
    assert saved['plans'][0]['id'] == plan['id']
    assert result['plan']['id'] != plan['id']
    assert saved['runs'] == []


def test_claim_freezes_skills_and_later_file_rebuild_uses_original(setup, monkeypatch):
    currents, cid, plan = setup
    current = currents[0]
    instance = manager(current)
    assert instance.store.namespace == current.conversation_store.namespace
    instance.store.put('skill_test', 'skill', {
        'kind': 'skill', 'name': 'news-style', 'description': '每日新闻', 'enabled': True,
        'body': '原始技能正文', 'version': 'v1', 'version_hash': 'v1',
        'resources': {'references/style.md': '原始附件'}}, expected_revision=0)
    changed = instance.save_plan(cid, plan['id'], {'version': plan['version'],
        'skill_mode': 'manual', 'skill_names': ['skill_test'], 'disabled_tools': []})['plan']
    service = PlanService(current)
    request = payload(service, cid)
    claim = service.claim(cid, changed['id'], request, 'freeze')
    identity = claim['plan']['execution_request_id']
    assert instance.store.namespace == current.conversation_store.namespace
    snapshot = instance.store.snapshot(identity)
    assert snapshot['skills'][0]['body'] == '原始技能正文'
    old = instance.store.get('skill_test')
    instance.store.put('skill_test', 'skill', {**old, 'body': '替换正文', 'version': 'v2'}, expected_revision=old['revision'])
    calls = []
    monkeypatch.setattr(current, 'submit', lambda req, key: calls.append(deepcopy(req)) or {'id': key, 'status': 'queued'})
    service.confirm(cid, changed['id'], request, 'freeze')
    frozen = json.loads(service.frozen_path(cid, changed['id']).read_text(encoding='utf-8'))
    assert frozen['selected_skills'][0]['body'] == '原始技能正文'
    assert frozen['selected_skills'][0]['resources']['references/style.md'] == '原始附件'
    assert calls[0]['capability_namespace'] == instance.store.namespace
    assert calls[0]['model_runtime'] == claim['plan']['frozen_execution']['model_runtime']


def test_failed_snapshot_rolls_back_claim(setup, monkeypatch):
    currents, cid, plan = setup
    current = currents[0]
    instance = manager(current)
    service = PlanService(current)
    original = instance.store.freeze
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('freeze-fault')
    monkeypatch.setattr(instance.store, 'freeze', fail)
    before = current._read_agent_conversation(cid)
    with pytest.raises(RuntimeError, match='freeze-fault'):
        service.claim(cid, plan['id'], payload(service, cid), 'rollback')
    after = current._read_agent_conversation(cid)
    assert after['_revision'] == before['_revision']
    assert not after['plans'][-1].get('execution_request_id')
    with instance.store.knowledge_store.connection() as conn:
        assert conn.execute('SELECT count(*) AS n FROM agent.capability_snapshots WHERE namespace=%s',
                            (instance.store.namespace,)).fetchone()['n'] == 0
