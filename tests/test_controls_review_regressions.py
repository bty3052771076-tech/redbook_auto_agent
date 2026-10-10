"""Regression paths from the independent review; all external work is isolated."""
from copy import deepcopy
from pathlib import Path
import json
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_capability_store import store
from test_run_resume_contract import isolated, start_plan, checkpoint
from test_task_calibration import calibration, workbench
from test_task_plan_v3 import proposed
from test_capability_api import client


def test_resume_keeps_frozen_profile_models_and_namespace(isolated, monkeypatch):
    service, _, _ = isolated
    monkeypatch.setattr(service, 'environment', lambda: {
        'XHS_CHROME_USER_DATA_DIR':'E:/owned-original-profile','XHS_CHROME_PROFILE':'Original'})
    cid, _, original = start_plan(isolated)
    saved = service._read_agent_conversation(cid)
    frozen = saved['plans'][0]['frozen_execution']
    service.jobs[original['id']]['status'] = 'interrupted'
    checkpoint(service, original['id'])
    monkeypatch.setattr(service, 'environment', lambda: {'XHS_CHROME_USER_DATA_DIR':'E:/wrong-profile'})
    requests = []
    monkeypatch.setattr(service, 'submit', lambda request, key: requests.append(deepcopy(request)) or {'id':uuid4().hex})
    service.resume_agent_run(cid, original['id'], 'resume-frozen')
    for key in ('host_environment', 'model_runtime', 'capability_namespace'):
        expected = deepcopy(frozen[key])
        if key == 'host_environment':
            expected['AGENT_REQUIRE_FROZEN_CAPABILITIES'] = '1'
        assert requests[0].get(key) == expected


def test_resume_does_not_recreate_missing_capability_snapshot(store, monkeypatch):
    from src.agent.capabilities import execution
    from src.agent.capabilities.models import CapabilityError
    monkeypatch.setattr(execution, 'CapabilityStore', lambda **kwargs: store)
    with pytest.raises(CapabilityError, match='CAPABILITY_SNAPSHOT_MISSING'):
        execution.runtime_tools(None, uuid4().hex, require_frozen=True)


def test_v3_resume_rejects_missing_entire_execution_snapshot(isolated, monkeypatch):
    service, _, _ = isolated
    cid, _, original = start_plan(isolated)
    saved = service._read_agent_conversation(cid)
    saved['plans'][0].pop('frozen_execution')
    service._write_agent_conversation(saved)
    service.jobs[original['id']]['status'] = 'interrupted'
    checkpoint(service, original['id'])
    requests = []
    monkeypatch.setattr(service, 'submit', lambda *args: requests.append(args) or {'id':uuid4().hex})
    with pytest.raises(ValueError, match='PLAN_FROZEN_INCOMPLETE'):
        service.resume_agent_run(cid, original['id'], 'missing-snapshot')
    assert requests == []


def test_both_platform_profiles_and_cdp_policy_stay_frozen(isolated, monkeypatch):
    from backend.plan_service import PlanService
    from src.publish.toutiao_steps import resolve_toutiao_profile_config, _resolve_toutiao_cdp_url
    service, _, _ = isolated
    monkeypatch.setattr(service, 'environment', lambda: {
        'XHS_CHROME_USER_DATA_DIR':'E:/original-xhs','XHS_CHROME_PROFILE':'XHSOriginal',
        'TOUTIAO_CHROME_USER_DATA_DIR':'E:/original-toutiao','TOUTIAO_CHROME_PROFILE':'TTOriginal',
        'TOUTIAO_CDP_URL':'http://127.0.0.1:19800','TOUTIAO_AUTO_ATTACH_CDP':'1'})
    frozen = PlanService(service)._host_environment()
    assert frozen['TOUTIAO_CHROME_USER_DATA_DIR'] == 'E:/original-toutiao'
    monkeypatch.setattr(service, 'environment', lambda: {
        'XHS_CHROME_USER_DATA_DIR':'E:/wrong-xhs','TOUTIAO_CHROME_USER_DATA_DIR':'E:/wrong-toutiao',
        'TOUTIAO_CHROME_PROFILE':'Wrong','TOUTIAO_CDP_URL':'http://127.0.0.1:19999','TOUTIAO_AUTO_ATTACH_CDP':'1'})
    _, env = service.plan({'kind':'agent','platform':'both','model_runtime':{
        'snapshots':{},'legacy_roles':{},'legacy_configs':{},'namespace':'test'},
        'host_environment':frozen,'capability_namespace':'test'}, 'a'*32)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    path, _, args = resolve_toutiao_profile_config()
    assert path == Path('E:/original-toutiao')
    assert args == ['--profile-directory=TTOriginal']
    assert _resolve_toutiao_cdp_url() is None


def test_unset_toutiao_profile_freezes_effective_xhs_fallback(isolated, monkeypatch):
    from backend.plan_service import PlanService
    service, _, _ = isolated
    monkeypatch.setattr(service, 'environment', lambda: {'XHS_CHROME_USER_DATA_DIR':'E:/original',
        'XHS_CHROME_PROFILE':'Original'})
    frozen = PlanService(service)._host_environment()
    assert frozen['TOUTIAO_CHROME_USER_DATA_DIR'] == 'E:/original'
    assert frozen['TOUTIAO_CHROME_PROFILE'] == 'Original'


@pytest.mark.parametrize('tampered', [False, True])
def test_resume_rebuilds_missing_plan_but_rejects_changed_frozen_file(isolated, monkeypatch, tampered):
    service, _, _ = isolated
    cid, plan, original = start_plan(isolated)
    service.jobs[original['id']]['status'] = 'interrupted'
    checkpoint(service, original['id'])
    path = service.directory/'conversations'/cid/'plans'/(plan['id']+'.json')
    if tampered:
        value = json.loads(path.read_text(encoding='utf-8'))
        value['jobs'][0]['count'] = 1
        path.write_text(json.dumps(value),encoding='utf-8')
    else:
        path.unlink()
    requests = []
    monkeypatch.setattr(service,'submit', lambda *args: requests.append(args) or {'id':uuid4().hex})
    if tampered:
        from src.agent.plan_contract import PlanContractError
        with pytest.raises(PlanContractError) as failure:
            service.resume_agent_run(cid, original['id'], 'changed-plan-file')
        assert failure.value.code == 'PLAN_FROZEN_MISMATCH'
        assert requests == []
    else:
        service.resume_agent_run(cid, original['id'], 'missing-plan-file')
        assert path.is_file()
        assert json.loads(path.read_text(encoding='utf-8'))['jobs'][0]['count'] == 10


def test_stdio_stderr_redacts_before_it_reaches_disk(tmp_path):
    from backend.settings import configure_runtime
    from src.agent.mcp_manager import MCPManager
    manager = MCPManager(configure_runtime())
    manager.root = tmp_path
    secret = 'offline-canary-'+uuid4().hex
    params = SimpleNamespace(env={'NEWS_API_KEY':secret})
    # The child writes to an OS handle, not Python's write method.
    with manager._stderr_log(params) as sink:
        script = 'import os; os.write(2,'+repr(('credential='+secret+'\n').encode())+')'
        result = subprocess.run([sys.executable, '-c', script], stderr=sink, capture_output=False)
        assert result.returncode == 0
    log = (tmp_path/'data/tmp/mcp-stderr.log').read_text(encoding='utf-8')
    assert 'credential=' in log and '[redacted]' in log
    assert secret not in log


@pytest.mark.parametrize('name,annotations', [
    ('delete_post', {'readOnlyHint':False, 'destructiveHint':True}),
    ('publish_post', {'readOnlyHint':True, 'destructiveHint':False}),
    ('search', {}),
])
def test_mcp_write_or_unknown_effect_cannot_be_approved(store, tmp_path, name, annotations):
    from src.agent.capabilities.mcp_service import MCPConnectionService
    from src.agent.capabilities.models import CapabilityError, digest
    schema = {'type':'object'}
    row = store.put('mcp_review', 'mcp', {'name':'review','enabled':True,'health':{'status':'ready'},
        'tools':[{'name':name,'input_schema':schema,'schema_hash':digest(schema),'annotations':annotations}]}, expected_revision=0)
    with pytest.raises(CapabilityError, match='MCP_READ_ONLY_REQUIRED'):
        MCPConnectionService(tmp_path, store).tool_policy(row['id'], {
            'expected_revision':row['revision'],'tool_name':name,'schema_hash':digest(schema),
            'stages':['preparation'],'purpose':'evidence','enabled':True,'read_only_confirmed':True})


def test_mcp_runtime_does_not_call_destructive_frozen_tool():
    from src.agent.capabilities.mcp_runtime import MCPRuntime
    from src.agent.capabilities.models import CapabilityError, digest
    schema = {'type':'object'}
    tool = {'id':'mcp:review:delete_post','input_schema':schema,'schema_hash':digest(schema),
        'read_only_confirmed':True,'annotations':{'readOnlyHint':False,'destructiveHint':True},
        'connection':{'id':'review'}}
    with MCPRuntime(manager_factory=lambda connection: pytest.fail('write service started')) as runtime:
        with pytest.raises(CapabilityError, match='MCP_READ_ONLY_REQUIRED'):
            runtime.call(tool, {})
        assert not runtime.running


def test_unavailable_provider_stays_unexecutable_until_explicit_model_change(calibration):
    from backend.task_recognition import validate_candidate, parse_task
    from backend.plan_service import PlanService
    current, _, cid, base, _, _ = calibration
    output = proposed()
    output['provider_requests']['writer'] = 'unavailable-provider'
    candidate = validate_candidate(parse_task(json.dumps(output)), '生成5条每日新闻', base, current)
    assert not candidate['executable']
    record = {'id':'review-recognition','base_plan_id':base['id'],'base_plan_version':base['version'],
        'status':'needs_input','candidate':candidate}
    saved = current._read_agent_conversation(cid)
    saved.setdefault('task_recognitions', []).append(record)
    current._write_agent_conversation(saved)
    service = PlanService(current)
    adopted = service.adopt(cid, record['id'], {'base_plan_version':base['version'], 'editable_fields':{}}, 'review-adopt')['plan']
    assert not adopted['executable']
    assert any(e['field']=='model_roles.writer' for e in adopted['field_errors'])
    roles = {**adopted['model_roles'], 'writer':'minimax:MiniMax-M3'}
    repaired = service.save(cid, adopted['id'], {'base_plan_version':adopted['version'],
        'editable_fields':{'model_roles':roles}}, 'review-model-choice')['plan']
    assert repaired['executable']


def test_import_preview_never_echoes_authentication_and_save_preserves_it(store, tmp_path):
    from src.agent.capabilities.mcp_service import MCPConnectionService
    service = MCPConnectionService(tmp_path, store)
    secret = 'import-canary-'+uuid4().hex
    preview = service.import_preview({'mcpServers':{'example':{'url':'http://127.0.0.1:19876/mcp',
        'headers':{'Authorization':'Bearer '+secret},'env':{'TOKEN':secret}}}})
    assert secret not in json.dumps(preview)
    seed = preview['rows'][0]
    assert seed['headers'] == {'Authorization':'已配置'}
    saved = service.save({key:value for key,value in seed.items() if key not in {'issues','headers','environment'}})
    assert secret not in json.dumps(saved)
    private = service.get(saved['id'])
    assert service.credentials.read(private['header_refs']['Authorization']) == 'Bearer '+secret
    assert service.credentials.read(private['environment_refs']['TOKEN']) == secret
    assert saved['enabled'] is False


def test_public_mcp_tools_show_effective_saved_policy_after_reload(store, tmp_path):
    from src.agent.capabilities.mcp_service import MCPConnectionService
    from src.agent.capabilities.models import digest
    service = MCPConnectionService(tmp_path, store)
    schema = {'type':'object'}
    row = store.put('mcp_policy', 'mcp', {'name':'policy','enabled':True,'health':{'status':'ready'},
        'tools':[{'name':'search','input_schema':schema,'schema_hash':digest(schema),
                  'annotations':{'readOnlyHint':True,'destructiveHint':False}}]}, expected_revision=0)
    service.tool_policy(row['id'], {'expected_revision':row['revision'],'tool_name':'search',
        'schema_hash':digest(schema),'stages':['evidence'],'purpose':'evidence','enabled':True,'read_only_confirmed':True})
    reloaded = next(item for item in service.list()['rows'] if item['id']==row['id'])
    assert reloaded['tools'][0]['enabled'] is True
    assert reloaded['tools'][0]['stages'] == ['evidence']
    assert reloaded['tools'][0]['purpose'] == 'evidence'


@pytest.mark.parametrize('field,value', [
    ('builtin', True), ('tools', [{'name':'publish_post'}]),
    ('tool_policies', {'search':{'enabled':True}}),
    ('environment_refs', {'TOKEN':'foreign-reference'}), ('kind','skill'),
])
def test_mcp_configuration_cannot_inject_privileged_state(store, tmp_path, field, value):
    from src.agent.capabilities.mcp_service import MCPConnectionService
    from src.agent.capabilities.models import CapabilityError
    service = MCPConnectionService(tmp_path, store)
    with pytest.raises(CapabilityError, match='MCP_FIELD_INVALID'):
        service.save({'name':'injected','transport':'streamable_http',
            'url':'http://127.0.0.1:19800/mcp',field:value})
    assert store.resources('mcp') == []


def test_builtin_mcp_execution_target_cannot_be_replaced(store, tmp_path):
    from src.agent.capabilities.mcp_service import MCPConnectionService
    from src.agent.capabilities.models import CapabilityError
    service = MCPConnectionService(tmp_path, store)
    with pytest.raises(CapabilityError, match='MCP_BUILTIN_LOCKED'):
        service.save({'transport':'streamable_http','url':'http://127.0.0.1:19800/mcp'},'mcp_local')
    assert service.get('mcp_local')['transport'] == 'stdio'


def test_custom_mcp_target_change_requires_rediscovery_and_approval(store, tmp_path):
    from src.agent.capabilities.mcp_service import MCPConnectionService
    from src.agent.capabilities.models import digest
    service = MCPConnectionService(tmp_path, store)
    schema = {'type':'object'}
    original = service.save({'name':'original','transport':'streamable_http','url':'http://127.0.0.1:19800/mcp'})
    row = store.put(original['id'], 'mcp', {**service.get(original['id']), 'enabled':True,
        'tools':[{'name':'search','input_schema':schema,'schema_hash':digest(schema),
                  'annotations':{'readOnlyHint':True}}],
        'tool_policies':{'search':{'enabled':True,'stages':['evidence'],'purpose':'evidence','read_only_confirmed':True}}},
        expected_revision=original['revision'])
    changed = service.save({'url':'http://127.0.0.1:19801/mcp','expected_revision':row['revision']},row['id'])
    assert changed['enabled'] is False
    assert changed['tools'] == []
    assert changed.get('tool_policies') == {}


def test_mcp_batch_checks_keep_results_after_one_connection_fails(client, monkeypatch):
    from src.agent.capabilities.models import CapabilityError
    from src.agent.mcp_manager import MCPManager
    import time
    test, manager = client
    rows = [manager.mcp.save({'name':name,'transport':'streamable_http',
        'url':'http://127.0.0.1:19800/'+name}) for name in ('bad','good')]
    def catalog(self):
        if self.server.server_id == rows[0]['id']:
            raise CapabilityError('MCP_TIMEOUT','controlled timeout',next_action='检查地址')
        self.complete, self.protocol_version = True, '2025-03-26'
        return []
    monkeypatch.setattr(MCPManager, 'list_tools', catalog)
    response = test.post('/api/mcp/checks',headers={'x-workbench':'1','idempotency-key':uuid4().hex},
        json={'connection_ids':[row['id'] for row in rows]})
    assert response.status_code == 200, response.text
    operation = response.json()['operation_id']
    for _ in range(100):
        result = test.get('/api/capabilities/checks/'+operation).json()
        if result['status'] not in {'queued','running'}:
            break
        time.sleep(.02)
    assert [item['id'] for item in result['results']] == [row['id'] for row in rows]
    assert result['results'][0]['health']['error_code'] == 'MCP_TIMEOUT'
    assert result['results'][1]['health']['status'] == 'ready'
    assert result['status'] == 'degraded'


def test_skill_restore_uses_revision_not_configuration_hash(store, tmp_path):
    from src.agent.capabilities.skill_service import SkillService
    service = SkillService(tmp_path, store)
    original = store.put('skill_restore', 'skill', {'name':'restore','enabled':True,'version':'content-v1',
        'version_hash':'content-v1','body':'原始正文','resources':{}}, expected_revision=0)
    updated = service.patch(original['id'], {'expected_revision':original['revision'],'body':'更新正文'})
    version = service.get(original['id'])['versions'][-1]
    assert version['hash'] != original['version']
    restored = service.patch(original['id'], {'expected_revision':updated['revision'], 'version_revision':version['revision']})
    assert restored['body'] == original['body']
    assert restored['version'] == original['version']


@pytest.mark.parametrize('changes', [
    {'transport':'streamable_http','url':'http://127.0.0.1:19800/mcp'},
    {'command':'E:/untrusted/python.exe'},
    {'args':['-m','another_package']},
    {'cwd':'E:/untrusted'},
    {'environment_refs':{'TOKEN':'foreign-reference'}},
    {'header_refs':{'Authorization':'foreign-reference'}},
    {'args':7},
    {'allowed_tools':[{}]},
    {'command':{}},
])
def test_legacy_builtin_target_is_quarantined_before_use(store, tmp_path, changes):
    from src.agent.capabilities.mcp_service import MCPConnectionService
    from src.agent.mcp_manager import MCPManager
    from src.agent.capabilities.models import CapabilityError
    service = MCPConnectionService(tmp_path, store)
    legacy = store.put('mcp_local','mcp',{**service.builtin(), **changes},expected_revision=0)
    visible = next(row for row in service.list()['rows'] if row['id']=='mcp_local')
    assert visible['enabled'] is False
    assert visible['health']['error_code'] == 'MCP_BUILTIN_MISMATCH'
    with pytest.raises(CapabilityError, match='MCP_BUILTIN_MISMATCH'):
        service.get('mcp_local')
    with pytest.raises(CapabilityError, match='MCP_BUILTIN_MISMATCH'):
        MCPManager(tmp_path, connection=legacy)


def test_old_frozen_builtin_target_is_rejected_before_session_opens(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from src.agent.mcp_manager import MCPManager
    from src.agent.capabilities.mcp_runtime import MCPRuntime
    from src.agent.capabilities.models import CapabilityError, digest
    schema = {'type':'object'}
    frozen = {'id':'mcp:mcp_local:runtime_status','input_schema':schema,'schema_hash':digest(schema),
        'connection':{'id':'mcp_local','builtin':True,'transport':'streamable_http',
                      'url':'http://127.0.0.1:19800/mcp'}}
    @asynccontextmanager
    async def unsafe_session(self):
        raise RuntimeError('untrusted session was opened')
        yield
    monkeypatch.setattr(MCPManager,'_session',unsafe_session)
    with MCPRuntime(manager_factory=lambda row:MCPManager(tmp_path,connection=row)) as runtime:
        with pytest.raises(CapabilityError, match='MCP_BUILTIN_MISMATCH'):
            runtime.call(frozen,{})


@pytest.mark.parametrize('builtin', [True, False])
def test_metadata_and_empty_inactive_target_fields_preserve_approval(store, tmp_path, builtin):
    from src.agent.capabilities.mcp_service import MCPConnectionService
    from src.agent.capabilities.models import digest
    service = MCPConnectionService(tmp_path, store)
    schema = {'type':'object'}
    row = service.builtin() if builtin else {'id':'mcp_custom','kind':'mcp','name':'HTTP',
        'transport':'streamable_http','url':'http://127.0.0.1:19800/mcp'}
    name = 'runtime_status' if builtin else 'search'
    row.update(enabled=True, health={'status':'ready'},
        tools=[{'name':name,'input_schema':schema,'schema_hash':digest(schema),
                'annotations':{'readOnlyHint':True}}],
        tool_policies={name:{'enabled':True,'stages':['evidence'],'purpose':'evidence',
                                  'read_only_confirmed':True}})
    saved = store.put(row['id'],'mcp',row,expected_revision=0)
    edited = service.save({'expected_revision':saved['revision'],'name':'renamed','timeout_seconds':45,
        **({'url':''} if builtin else {'command':'','cwd':'','args':[]})},row['id'])
    assert edited['enabled'] is True
    assert edited['tools'][0]['enabled'] is True
    assert edited['tool_policies'][name]['purpose'] == 'evidence'


def test_untrusted_legacy_connection_can_be_retired_without_launch(store, tmp_path):
    from src.agent.capabilities.mcp_service import MCPConnectionService
    service = MCPConnectionService(tmp_path, store)
    row = store.put('mcp_local','mcp',{**service.builtin(),'args':['-m','untrusted']},expected_revision=0)
    service.retire(row['id'],row['revision'])
    assert all(item['id']!='mcp_local' for item in service.list()['rows'])


def test_null_legacy_credential_references_do_not_break_list_or_retirement(store, tmp_path):
    from src.agent.capabilities.mcp_service import MCPConnectionService
    service = MCPConnectionService(tmp_path, store)
    row = store.put('mcp_local','mcp',{**service.builtin(),
        'environment_refs':None,'header_refs':None},expected_revision=0)
    visible = next(item for item in service.list()['rows'] if item['id']==row['id'])
    assert visible['environment'] == {} and visible['headers'] == {}
    retired = service.retire(row['id'],row['revision'])
    assert retired['retired'] is True and retired['enabled'] is False


@pytest.mark.parametrize('changes', [
    {'tool_policies':[{'search':{'enabled':True}}]},
    {'tools':{'name':'search'}},
    {'tools':[{}]},
    {'environment_refs':['foreign-reference']},
])
def test_malformed_legacy_containers_block_only_one_connection(store, tmp_path, changes):
    from src.agent.capabilities.mcp_service import MCPConnectionService
    from src.agent.capabilities.models import CapabilityError
    service = MCPConnectionService(tmp_path, store)
    broken = store.put('mcp_legacy','mcp',{'name':'legacy','enabled':True,
        'transport':'streamable_http','url':'http://127.0.0.1:19800/mcp', **changes},expected_revision=0)
    healthy = service.save({'name':'healthy','transport':'streamable_http','url':'http://127.0.0.1:19801/mcp'})
    rows = {item['id']:item for item in service.list()['rows']}
    assert rows[broken['id']]['health']['error_code'] == 'MCP_CONFIG_INVALID'
    assert rows[broken['id']]['enabled'] is False
    assert rows[healthy['id']]['name'] == 'healthy'
    with pytest.raises(CapabilityError, match='MCP_CONFIG_INVALID'):
        service.get(broken['id'])
    service.tools()
    assert service.retire(broken['id'],broken['revision'])['retired'] is True


@pytest.mark.parametrize('has_catalog', [True, False])
def test_null_legacy_tool_policy_can_be_approved_or_report_missing_catalog(store, tmp_path, has_catalog):
    from src.agent.capabilities.mcp_service import MCPConnectionService
    from src.agent.capabilities.models import CapabilityError, digest
    service = MCPConnectionService(tmp_path, store)
    schema = {'type':'object'}
    catalog = [{'name':'search','input_schema':schema,'schema_hash':digest(schema),
                'annotations':{'readOnlyHint':True}}] if has_catalog else None
    row = store.put('mcp_null_policy','mcp',{'name':'legacy','transport':'streamable_http',
        'url':'http://127.0.0.1:19800/mcp','tools':catalog,'tool_policies':None},expected_revision=0)
    policy = {'expected_revision':row['revision'],'tool_name':'search','schema_hash':digest(schema),
        'enabled':True,'stages':['evidence'],'purpose':'evidence','read_only_confirmed':True}
    if has_catalog:
        approved = service.tool_policy(row['id'],policy)
        assert approved['tools'][0]['enabled'] is True
    else:
        with pytest.raises(CapabilityError, match='MCP_SCHEMA_CONFLICT'):
            service.tool_policy(row['id'],policy)
