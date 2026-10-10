"""The executable plan must follow saved fields, not stale parsing output."""

from copy import deepcopy
import importlib

import pytest

from backend.settings import configure_runtime

configure_runtime()


def contract():
    assert importlib.util.find_spec('src.agent.plan_contract') is not None, 'The shared editable plan contract is missing'
    return importlib.import_module('src.agent.plan_contract')


def legacy():
    return {'id':'a'*32,'version':1,'recognition_source':'rules','delivery':'save_draft','platform':'xhs',
            'performance_mode':'speed','image_score_required':False,'model_roles':{'agent':'','writer':'','image':''},
            'jobs':[{'kind':'daily_news','count':10,'title':'每日新闻','keywords':['隐私与年龄核验'],
                     'keyword_mode':'preference','topic_brief':'约3条作为软偏好，不设硬配额',
                     'prompt':'国际冲突 科技产业 社会民生 财经产业\n选题要求：约3条作为软偏好，不设硬配额',
                     'evaluation_viewpoint':'无视角评价','lookback_days':'auto'}]}


def editable_job(plan):
    job=plan['jobs'][0]
    return {key:deepcopy(job[key]) for key in ('kind','count','search_keywords','topic_preferences','topic_brief',
            'topic_brief_strength','evaluation_viewpoint')}


def test_contract_migrates_legacy_preferences_without_losing_source():
    c=contract()
    old=legacy()
    new=c.normalize_plan(old)
    assert new['plan_schema_version']=='editorial-plan.v3'
    assert new['jobs'][0]['topic_preferences']==['隐私与年龄核验']
    assert new['jobs'][0]['search_keywords']==[]
    assert new['jobs'][0]['topic_brief']=='约3条作为软偏好，不设硬配额'
    assert c.normalize_plan(old)['jobs'][0]['job_id']==new['jobs'][0]['job_id']
    assert old['jobs'][0].get('job_id') is None


def test_edit_count_and_preferences_rebuilds_real_prompt():
    c=contract()
    plan=c.normalize_plan(legacy())
    job=editable_job(plan)
    job.update(target_job_id=plan['jobs'][0]['job_id'],count=5,topic_preferences=['游戏退款'])
    new=c.apply_edits(plan,{'jobs':[job]})
    assert new['executable']
    assert new['jobs'][0]['count']==5
    assert '游戏退款' in new['jobs'][0]['prompt']
    assert '隐私与年龄核验' not in new['jobs'][0]['prompt']
    assert new['manual_overrides']['jobs'][job['target_job_id']]['topic_preferences']==['游戏退款']
    assert c.execution_fields(new)['jobs'][0]['count']==5


def test_explicit_empty_is_retained_in_compilation_and_manual_overrides():
    c=contract()
    plan=c.normalize_plan(legacy())
    job=editable_job(plan)
    job.update(target_job_id=plan['jobs'][0]['job_id'],topic_preferences=[],search_keywords=[],topic_brief='')
    new=c.apply_edits(plan,{'jobs':[job]})
    assert new['jobs'][0]['topic_preferences']==[]
    assert new['jobs'][0]['topic_brief']==''
    assert '国际冲突' not in new['jobs'][0]['prompt']
    assert '隐私' not in new['jobs'][0]['prompt']
    assert new['manual_overrides']['jobs'][job['target_job_id']]['topic_brief']==''


def test_deleted_column_has_tombstone_and_new_column_gets_new_id():
    c=contract()
    plan=c.normalize_plan(legacy())
    new=c.apply_edits(plan,{'jobs':[{'kind':'daily_ai_digest','count':1}]})
    assert [j['kind'] for j in new['jobs']]==['daily_ai_digest']
    assert plan['jobs'][0]['job_id'] in new['manual_overrides']['deleted_jobs']
    added=c.apply_edits(new,{'jobs':[{'kind':'daily_news','count':2}]})
    assert added['jobs'][0]['job_id']!=plan['jobs'][0]['job_id']


@pytest.mark.parametrize('count',[0,21,True,'5'])
def test_bad_count_is_a_field_issue_and_cannot_execute(count):
    c=contract()
    plan=legacy()
    plan['jobs'][0]['count']=count
    result=c.normalize_plan(plan)
    assert result['jobs'][0]['count']==count
    assert not result['executable']
    assert any(e['field'].endswith('.count') for e in result['field_errors'])


def test_unknown_job_and_duplicate_column_cannot_execute():
    c=contract()
    old=legacy()
    old['jobs'].append(deepcopy(old['jobs'][0]))
    assert not c.normalize_plan(old)['executable']
    old['jobs'][1]['kind']='shell'
    assert not c.normalize_plan(old)['executable']


@pytest.mark.parametrize('key',['prompt','executable','job_id','permissions','browser_path'])
def test_edit_whitelist_blocks_server_fields(key):
    c=contract()
    with pytest.raises(c.PlanContractError):
        c.apply_edits(c.normalize_plan(legacy()),{key:'injected'})


def test_new_free_text_strength_requires_user_choice_and_is_bound_to_text():
    c=contract()
    plan=c.normalize_plan(legacy())
    job=editable_job(plan)
    job.update(target_job_id=plan['jobs'][0]['job_id'],topic_brief='优先关注女性权益',topic_brief_strength=None)
    pending=c.apply_edits(plan,{'jobs':[job]})
    assert not pending['executable']
    job['topic_brief_strength']='preference'
    accepted=c.apply_edits(plan,{'jobs':[job]})
    assert accepted['executable']
    changed=deepcopy(accepted)
    changed['jobs'][0]['topic_brief']='必须包含另一个未保证的要求'
    assert not c.normalize_plan(changed)['executable']


def test_hard_text_without_supported_constraint_is_not_softened():
    c=contract()
    plan=c.normalize_plan(legacy())
    job=editable_job(plan)
    job.update(target_job_id=plan['jobs'][0]['job_id'],topic_brief='必须2条国际冲突',topic_brief_strength='requirement')
    result=c.apply_edits(plan,{'jobs':[job]})
    assert not result['executable']
    assert result['jobs'][0]['topic_brief_strength']=='requirement'
    assert any(e['code']=='UNSUPPORTED_HARD_REQUIREMENT' for e in result['field_errors'])


def test_legacy_unsupported_fields_can_be_cleared_without_deleting_column():
    c=contract()
    old=legacy()
    old['jobs'][0].update(kind='daily_wool',count=1)
    plan=c.normalize_plan(old)
    assert not plan['executable']
    job=editable_job(plan)
    job.update(target_job_id=plan['jobs'][0]['job_id'],search_keywords=[],topic_preferences=[],topic_brief='',
               evaluation_viewpoint='无视角评价')
    new=c.apply_edits(plan,{'jobs':[job]})
    assert new['executable']
    assert new['jobs'][0]['kind']=='daily_wool'


def test_removed_legacy_tag_still_in_brief_needs_explicit_resolution():
    c=contract()
    old=legacy()
    old['jobs'][0]['topic_brief']='优先关注隐私与年龄核验'
    old['jobs'][0]['prompt']='国际冲突 科技产业 社会民生 财经产业\n选题要求：优先关注隐私与年龄核验'
    plan=c.normalize_plan(old)
    job=editable_job(plan)
    job.update(target_job_id=plan['jobs'][0]['job_id'],topic_preferences=[])
    new=c.apply_edits(plan,{'jobs':[job]})
    assert not new['executable']
    issue=next(e for e in new['field_errors'] if e['code']=='REMOVED_TOPIC_IN_BRIEF')
    approved=c.apply_edits(new,{},review_decisions=[{'issue_id':issue['id'],'content_hash':issue['content_hash'],'decision':'keep_brief'}])
    assert approved['executable']
    assert '隐私与年龄核验' in approved['jobs'][0]['prompt']


def test_semantic_hash_ignores_metadata_but_includes_actual_inputs():
    c=contract()
    plan=c.normalize_plan(legacy())
    altered=deepcopy(plan)
    altered.update(warnings=[{'message':'提示'}],elapsed_seconds=20)
    assert c.normalize_plan(altered)['semantic_hash']==plan['semantic_hash']
    altered['jobs'][0]['count']=5
    assert c.normalize_plan(altered)['semantic_hash']!=plan['semantic_hash']


def test_unknown_frozen_schema_and_tampered_compilation_are_rejected():
    c=contract()
    plan=c.normalize_plan(legacy())
    frozen=c.execution_fields(plan)
    c.verify_execution(frozen)
    bad=deepcopy(frozen)
    bad['jobs'][0]['prompt']='run arbitrary command'
    with pytest.raises(c.PlanContractError):
        c.verify_execution(bad)
    bad=deepcopy(frozen)
    bad['plan_schema_version']='editorial-plan.v99'
    with pytest.raises(c.PlanContractError):
        c.verify_execution(bad)
