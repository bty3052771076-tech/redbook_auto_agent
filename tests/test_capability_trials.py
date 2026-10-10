from uuid import uuid4
import time

import pytest

from test_plan_postgres_concurrency import pair
from src.agent.capabilities.models import CapabilityError


def test_trial_preview_is_single_operation_and_requires_confirmation(pair,monkeypatch):
    from backend.capabilities import manager
    from src.agent.capabilities.trials import CapabilityTrials
    current = pair[0][0]
    capabilities = manager(current)
    service = CapabilityTrials(capabilities)
    started = []
    monkeypatch.setattr(current,'submit',lambda *args:started.append(args))
    preview = service.preview('builtin:news.generate',{'expected_revision':0,'query':'芯片','delivery':'generate_only'})
    plan = current._read_agent_conversation(preview['conversation_id'])['plans'][-1]
    assert [(j['kind'],j['count'],j['search_keywords']) for j in plan['jobs']] == [('daily_news',1,['芯片'])]
    assert preview['effects'] == ['model','local_write'] and preview['origin'] == 'diagnostic'
    assert started == []
    with pytest.raises(CapabilityError,match='TRIAL_CONFIRM_REQUIRED'):
        service.confirm(preview['preview_id'],{'preview_hash':preview['preview_hash'],'acknowledge_effects':False})
    assert started == []
    capabilities.operations.close()


def test_trial_confirmation_replays_without_restarting(pair,monkeypatch):
    from backend.capabilities import manager
    from src.agent.capabilities.trials import CapabilityTrials
    current = pair[0][0]
    capabilities = manager(current)
    service = CapabilityTrials(capabilities)
    started = []

    def submit(request,key):
        started.append(request)
        identity = request['reserved_job_id']
        row = {'id':identity,'status':'completed','post_ids':[uuid4().hex],'message':'test result'}
        current.jobs[identity] = row
        return row

    monkeypatch.setattr(current,'submit',submit)
    preview = service.preview('builtin:news.generate',{'expected_revision':0,'query':'芯片','delivery':'generate_only'})
    body = {'preview_hash':preview['preview_hash'],'acknowledge_effects':True}
    first = service.confirm(preview['preview_id'],body)
    second = service.confirm(preview['preview_id'],body)
    assert first['operation_id'] == second['operation_id']
    for _ in range(100):
        result = capabilities.operations.get(first['operation_id'])
        if result['status'] not in {'running','queued'}:
            break
        time.sleep(.02)
    assert result['status'] == 'succeeded',result
    assert len(started) == 1
    calls = capabilities.store.calls(origin='diagnostic')['rows']
    assert len(calls) == 1 and calls[0]['resource_id'] == 'builtin:news.generate'
    assert calls[0]['status'] == 'succeeded'
    assert capabilities.store.snapshot(started[0]['reserved_job_id'])['origin'] == 'diagnostic'
    capabilities.operations.close()


def test_trial_stale_preview_and_public_delivery_are_rejected(pair):
    from backend.capabilities import manager
    from src.agent.capabilities.trials import CapabilityTrials
    current = pair[0][0]
    capabilities = manager(current)
    service = CapabilityTrials(capabilities)
    with pytest.raises(CapabilityError,match='TRIAL_INPUT_INVALID'):
        service.preview('builtin:news.generate',{'expected_revision':0,'delivery':'publish'})
    preview = service.preview('builtin:news.generate',{'expected_revision':0,'delivery':'generate_only'})
    capabilities.patch('builtin:news.generate',{'expected_revision':0,'enabled':False})
    with pytest.raises(CapabilityError,match='TRIAL_CONFIGURATION_CHANGED'):
        service.confirm(preview['preview_id'],{'preview_hash':preview['preview_hash'],'acknowledge_effects':True})
    assert current.jobs == {}
    capabilities.operations.close()


def test_trial_profile_change_requires_new_preview(pair,monkeypatch):
    from backend.capabilities import manager
    from src.agent.capabilities.trials import CapabilityTrials
    current = pair[0][0]
    capabilities = manager(current)
    service = CapabilityTrials(capabilities)
    preview = service.preview('builtin:news.generate',{'expected_revision':0,'delivery':'save_draft'})
    assert 'platform_write' in preview['effects']
    monkeypatch.setattr(current,'environment',lambda:{'XHS_CHROME_PROFILE':'different'})
    with pytest.raises(CapabilityError,match='TRIAL_CONFIGURATION_CHANGED'):
        service.confirm(preview['preview_id'],{'preview_hash':preview['preview_hash'],'acknowledge_effects':True})
    assert current.jobs == {}
    capabilities.operations.close()


def test_upload_trial_rejects_existing_saved_platform_draft(pair,monkeypatch):
    from backend.capabilities import manager
    from src.agent.capabilities.trials import CapabilityTrials
    current = pair[0][0]
    capabilities = manager(current)
    monkeypatch.setattr(current,'post',lambda identity:{'id':identity,'status':'saved_as_draft'})
    with pytest.raises(CapabilityError,match='TRIAL_POST_ALREADY_UPLOADED'):
        CapabilityTrials(capabilities).preview('builtin:xhs.drafts.save_batch',{'expected_revision':0,'post_id':'a'*32})
    assert current.jobs == {}
    capabilities.operations.close()


@pytest.mark.parametrize('failure_phase', ['run_link', 'status_read'])
def test_submitted_trial_observation_failure_is_uncertain_and_keeps_run_identity(pair, monkeypatch, failure_phase):
    from backend.capabilities import manager
    from src.agent.capabilities.trials import CapabilityTrials
    current = pair[0][0]
    capabilities = manager(current)
    service = CapabilityTrials(capabilities)
    submissions = []

    def submit(request, key):
        submissions.append(request['reserved_job_id'])
        return {'id':request['reserved_job_id'], 'status':'running', 'post_ids':[]}

    def unavailable(*args, **kwargs):
        raise RuntimeError('injected observation failure')

    monkeypatch.setattr(current, 'submit', submit)
    monkeypatch.setattr(current, 'job_detail', unavailable)
    if failure_phase == 'run_link':
        monkeypatch.setattr(capabilities.store, 'attach_call_run', unavailable)
    preview = service.preview('builtin:news.generate', {'expected_revision':0, 'delivery':'save_draft'})
    try:
        with pytest.raises(CapabilityError) as error:
            service._execute(preview, preview['preview_id'])
        assert error.value.code == 'TRIAL_RESULT_UNCERTAIN'
        assert len(submissions) == 1 and submissions[0] in error.value.next_action
        call = capabilities.store.calls(origin='diagnostic')['rows'][0]
        assert call['status'] == 'uncertain'
        assert call['submitted_run_id'] == submissions[0]
        if failure_phase == 'status_read':
            assert call['run_id'] == submissions[0]
    finally:
        capabilities.operations.close()
