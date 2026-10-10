import json
from copy import deepcopy
from uuid import uuid4

import pytest

from test_task_calibration import workbench
from test_plan_revisions import setup_plan, body
from test_plan_postgres_concurrency import pair
from src.agent.plan_contract import PlanContractError


def test_confirm_uses_saved_fields_and_frozen_cli_contract(workbench, monkeypatch):
    from backend.plan_service import PlanService
    from apps.cli import _load_agent_job_plan
    service = PlanService(workbench)
    cid, plan = setup_plan(workbench)
    job = service.current_plan(cid)['jobs'][0]
    plan = service.save(cid, plan['id'], body(plan, jobs=[{'target_job_id': job['job_id'], 'count': 5,
                  'search_keywords': ['芯片'], 'topic_preferences': ['游戏退款'], 'topic_brief': ''}]), 'save')['plan']
    calls = []
    monkeypatch.setattr(workbench, 'submit', lambda request, key: calls.append(deepcopy(request)) or {'id': request['run_id'], 'status': 'queued'})
    request = {'version': plan['version'], 'semantic_hash': plan['semantic_hash'], 'skill_mode': 'off', 'skill_names': []}
    result = service.confirm(cid, plan['id'], request, 'confirm-key')
    assert service.confirm(cid, plan['id'], request, 'confirm-key')['id'] == result['id']
    assert len(calls) == 1
    assert calls[0]['count'] == 5
    jobs = _load_agent_job_plan(calls[0]['agent_jobs_file'], 'auto', '无视角评价')
    assert jobs[0].count == 5
    assert '芯片' in jobs[0].prompt and '隐私' not in jobs[0].prompt
    saved = workbench._read_agent_conversation(cid)['plans'][-1]
    assert saved['execution_request_id'] == result['id']
    frozen = saved['frozen_execution']
    assert frozen['jobs'][0]['count'] == 5
    assert frozen['semantic_hash'] == plan['semantic_hash']
    with pytest.raises(PlanContractError) as error:
        service.save(cid, plan['id'], body(plan, delivery='save_draft'), 'late-edit')
    assert error.value.status == 409


def test_claimed_not_started_can_continue_but_start_uncertain_never_restarts(workbench, monkeypatch):
    from backend.plan_service import PlanService
    service = PlanService(workbench)
    cid, plan = setup_plan(workbench)
    plan = service.current_plan(cid)
    payload = {'version': plan['version'], 'semantic_hash': plan['semantic_hash'], 'skill_mode': 'off', 'skill_names': []}
    claim = service.claim(cid, plan['id'], payload, 'claim-only')
    run_id = claim['plan']['execution_request_id']
    assert claim['plan']['submission_state'] == 'claimed'
    calls = []
    monkeypatch.setattr(workbench, 'submit', lambda request, key: calls.append(key) or {'id': request['run_id'], 'status': 'queued'})
    assert service.confirm(cid, plan['id'], payload, 'claim-only')['id'] == run_id
    assert calls == [run_id]
    cid2, plan2 = setup_plan(workbench)
    plan2 = service.current_plan(cid2)
    payload2 = {**payload, 'version': plan2['version'], 'semantic_hash': plan2['semantic_hash']}
    claim2 = service.claim(cid2, plan2['id'], payload2, 'uncertain')
    saved = workbench._read_agent_conversation(cid2)
    saved['plans'][-1]['submission_state'] = 'starting'
    workbench.conversation_store.save(saved)
    result = service.confirm(cid2, plan2['id'], payload2, 'uncertain')
    assert result['id'] == claim2['plan']['execution_request_id']
    assert result['status'] == 'submission_uncertain'
    assert len(calls) == 1


def test_frozen_file_tampering_does_not_start(workbench, monkeypatch):
    from backend.plan_service import PlanService
    service = PlanService(workbench)
    cid, plan = setup_plan(workbench)
    plan = service.current_plan(cid)
    payload = {'version': plan['version'], 'semantic_hash': plan['semantic_hash'], 'skill_mode': 'off', 'skill_names': []}
    claim = service.claim(cid, plan['id'], payload, 'tamper')
    path = service.frozen_path(cid, plan['id'])
    path.parent.mkdir(parents=True, exist_ok=True)
    frozen = deepcopy(claim['plan']['frozen_execution'])
    frozen['jobs'][0]['prompt'] += '恶意附加'
    path.write_text(json.dumps(frozen), encoding='utf-8')
    monkeypatch.setattr(workbench, 'submit', lambda *args: pytest.fail('tampered file started'))
    with pytest.raises(PlanContractError, match='冻结'):
        service.confirm(cid, plan['id'], payload, 'tamper')


def test_failure_before_run_journal_can_resume_same_execution_identity(workbench,monkeypatch):
    from backend.plan_service import PlanService
    service=PlanService(workbench)
    cid,_=setup_plan(workbench)
    plan=service.current_plan(cid)
    payload={'version':plan['version'],'skill_mode':'off','skill_names':[]}
    def fail_before_journal(*args):raise OSError('injected failure before run journal')
    monkeypatch.setattr(workbench,'submit',fail_before_journal)
    with pytest.raises(OSError,match='before run journal'):
        service.confirm(cid,plan['id'],payload,'startup-crash')
    claimed=workbench._read_agent_conversation(cid)['plans'][-1]
    identity=claimed['execution_request_id']
    started=[]
    monkeypatch.setattr(workbench,'submit',lambda req,key:started.append(key) or {'id':key,'status':'queued'})
    result=service.confirm(cid,plan['id'],payload,'startup-crash')
    assert result['status']=='queued'
    assert result['id']==identity and started==[identity]


def test_other_process_owns_startup_lease_so_confirm_does_not_launch(workbench,monkeypatch):
    from backend.plan_service import PlanService
    from src.model_platforms.security import file_lock
    service=PlanService(workbench)
    cid,_=setup_plan(workbench)
    plan=service.current_plan(cid)
    payload={'version':plan['version'],'skill_mode':'off','skill_names':[]}
    claim=service.claim(cid,plan['id'],payload,'busy-startup')
    identity=claim['plan']['execution_request_id']
    started=[]
    monkeypatch.setattr(workbench,'submit',lambda req,key:started.append(key) or {'id':key,'status':'queued'})
    with file_lock(workbench.directory/'locks'/('start-'+identity+'.lock'),timeout=0):
        result=service.confirm(cid,plan['id'],payload,'busy-startup')
        assert result['status']=='submission_uncertain' and started==[]
    assert service.confirm(cid,plan['id'],payload,'busy-startup')['id']==identity
    assert started==[identity]


def test_failed_run_journal_write_does_not_leave_a_phantom_queued_job(workbench, monkeypatch):
    from threading import Event
    identity = uuid4().hex
    started = Event()
    request = {'kind': 'agent', 'reserved_job_id': identity, 'run_id': identity}
    monkeypatch.setattr(workbench, 'plan', lambda *args: ([], {}))
    monkeypatch.setattr(workbench, '_run', lambda *args: started.set())
    with monkeypatch.context() as patch:
        def failed_journal(*args):
            raise OSError('injected journal write failure')
        patch.setattr(workbench, 'persist', failed_journal)
        with pytest.raises(OSError, match='journal write failure'):
            workbench.submit(request, identity)
    assert identity not in workbench.jobs
    assert not (workbench.directory / 'jobs' / (identity + '.json')).exists()
    result = workbench.submit(request, identity)
    assert result['id'] == identity and started.wait(timeout=2)
    assert json.loads((workbench.directory / 'jobs' / (identity + '.json')).read_text(encoding='utf-8'))['id'] == identity


def test_cli_rejects_mismatched_v3_prompt(workbench):
    from src.agent.plan_contract import execution_fields, normalize_plan
    from apps.cli import _load_agent_job_plan
    cid, plan = setup_plan(workbench)
    path = workbench.directory / 'conversations' / cid / 'plans' / (plan['id'] + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    frozen = execution_fields(normalize_plan(plan))
    frozen['jobs'][0]['prompt'] = '旧任务中的另一个主题'
    path.write_text(json.dumps(frozen), encoding='utf-8')
    with pytest.raises(PlanContractError, match='冻结'):
        _load_agent_job_plan(path, 'auto', '无视角评价')


@pytest.mark.parametrize('partial', ['{', '{}'])
def test_incomplete_frozen_file_rebuilds_same_database_claim(workbench,monkeypatch,partial):
    from backend.plan_service import PlanService
    service = PlanService(workbench)
    cid, _ = setup_plan(workbench)
    plan = service.current_plan(cid)
    request = {'version':plan['version'],'semantic_hash':plan['semantic_hash'],'skill_mode':'off','skill_names':[]}
    claim = service.claim(cid,plan['id'],request,'incomplete-file')
    identity = claim['plan']['execution_request_id']
    path = service.frozen_path(cid,plan['id'])
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(partial,encoding='utf-8')
    calls=[]
    monkeypatch.setattr(workbench,'submit',lambda req,key:calls.append(req) or {'id':key,'status':'queued'})
    assert service.confirm(cid,plan['id'],request,'incomplete-file')['id'] == identity
    assert json.loads(path.read_text(encoding='utf-8'))['frozen_execution_hash'] == claim['plan']['frozen_execution_hash']
    assert len(calls) == 1


def test_resume_keeps_checkpoint_id_without_reusing_web_submission_id(workbench, monkeypatch):
    from apps.web_service import _write_json_atomic
    original = uuid4().hex
    _write_json_atomic(workbench.directory / 'jobs' / (original + '.json'),
                       {'id': original, 'key': 'older-key', 'digest': 'older-request', 'status': 'failed'})
    monkeypatch.setattr(workbench, 'plan', lambda request, identity: ([], {}))
    monkeypatch.setattr(workbench, '_run', lambda *args: None)
    resumed = workbench.submit({'kind': 'agent', 'run_id': original, 'resume_of': original}, uuid4().hex)
    assert resumed['id'] != original
    assert resumed['agent_run_id'] == original


def test_frozen_plan_copy_is_new_revision_and_never_starts_task(pair,monkeypatch):
    from backend.plan_service import PlanService
    currents,cid,_=pair
    workbench=currents[0]
    service=PlanService(workbench)
    plan=service.current_plan(cid)
    service.claim(cid,plan['id'],{'version':plan['version'],'skill_mode':'off','skill_names':[]},'frozen-copy')
    before=workbench._read_agent_conversation(cid)
    monkeypatch.setattr(workbench,'submit',lambda *a,**k:pytest.fail('copy executed task'))
    copied=service.copy(cid,plan['id'],{'base_plan_version':plan['version'],
                                    'conversation_revision':before['_revision']},'copy-key')['plan']
    assert copied['id']!=plan['id'] and copied['version']==plan['version']+1
    assert copied['copied_from_plan_id']==plan['id']
    assert copied['jobs']==plan['jobs']
    assert not any(key in copied for key in ('execution_request_id','frozen_execution','frozen_execution_hash',
                                            'submission_state','confirmation_key','confirm_request_hash'))
    assert workbench._read_agent_conversation(cid)['plans'][0]['execution_request_id']==before['plans'][0]['execution_request_id']
    assert service.copy(cid,plan['id'],{'base_plan_version':plan['version'],
                        'conversation_revision':before['_revision']},'copy-key')['plan']['id']==copied['id']
