"""Calibration may disagree with rules; the user still gets an editable plan."""

from copy import deepcopy
import json

import pytest

from backend.task_recognition import parse_task, recognition_payload, validate_candidate
from test_task_calibration import calibration, candidate, workbench


def proposed():
    return {'schema_version':'task-recognition.v3','intent':'generate',
            'jobs':[{'kind':'daily_news','count':5,'search_keywords':['游戏退款'],
                     'topic_preferences':['平台规则变化'],'topic_brief':'约3篇作为软偏好',
                     'topic_brief_strength':'preference','content_constraints':[],'evaluation_viewpoint':None}],
            'options':{'delivery':'generate_only','platform':'xhs','performance_mode':'speed','image_score_required':False},
            'provider_requests':{'agent':None,'writer':None,'image':None},
            'annotations':[{'id':'r1','scope':'job:daily_news','normalized_instruction':'优先平台规则变化',
                            'evidence_ids':['u2']}],
            'clarifications':[],'field_suggestions':[],'summary':'建议生成5篇近期新闻'}


@pytest.mark.parametrize('field,value', [('count', 'eight'), ('search_keywords', '退款'),
    ('topic_preferences', {'unexpected': True}), ('image_score_required', 'false'),
    ('model_roles', {'unknown_role': 'secret'}), ('content_constraints', [{'type': 'execute', 'value': 'shell'}])])
def test_malformed_field_suggestions_do_not_poison_core_candidate(calibration, field, value):
    current, _, _, base, _, _ = calibration
    output = proposed()
    output['field_suggestions'] = [{'field': field, 'value': value, 'reason': '建议',
                                    **({'kind': 'daily_news'} if field in {'count', 'search_keywords', 'topic_preferences', 'content_constraints'} else {})}]
    result = validate_candidate(parse_task(json.dumps(output)), '生成5条每日新闻', base, current)
    assert result['jobs'][0]['count'] == 5
    assert result['executable']
    assert not result['field_suggestions']
    assert any(row['code'] == 'SUGGESTION_VALUE_INVALID' for row in result['warnings'])


def test_explicit_false_option_remains_user_override_when_value_was_already_false(calibration):
    from src.agent.plan_contract import apply_edits
    current, _, _, base, _, _ = calibration
    base = deepcopy(base)
    base['image_score_required'] = False
    base = apply_edits(base, {'image_score_required': False})
    assert base['manual_overrides']['options']['image_score_required'] is False
    output = proposed()
    output['options']['image_score_required'] = True
    result = validate_candidate(parse_task(json.dumps(output)), '生成5条每日新闻', base, current)
    assert result['image_score_required'] is False
    assert any(row['field_path'] == 'image_score_required' and row['value'] is True for row in result['field_suggestions'])


def test_payload_does_not_use_regex_answer_as_llm_input(calibration):
    current,_,_,base,_,_=calibration
    payload=recognition_payload('生成10条每日新闻，关键词：隐私',base,current)
    assert 'local_plan' not in payload
    assert 'base_plan' not in payload
    assert payload['user_message']=='生成10条每日新闻，关键词：隐私'
    assert payload['output_schema']['properties']['schema_version']['const']=='task-recognition.v3'


def test_llm_can_change_count_remove_keywords_and_add_new_topic(calibration):
    current,_,_,base,_,_=calibration
    value=candidate()
    value['jobs'][0].update(count=3,keywords=['游戏退款'])
    plan=validate_candidate(parse_task(json.dumps(value)),'生成5条每日新闻，关键词：伊朗、关税；不上传',base,current)
    assert plan['jobs'][0]['count']==3
    assert plan['jobs'][0]['search_keywords']==['游戏退款']
    assert plan['executable']
    assert plan['recognition_source']=='llm'
    assert plan['warnings']


@pytest.mark.parametrize('annotation',[{'id':'r1','evidence_ids':['u999']},'malformed',
    {'id':'r1','evidence_ids':['u1'],'normalized_instruction':'不相关解释'}])
def test_annotation_problem_does_not_discard_core_plan(calibration,annotation):
    current,_,_,base,_,_=calibration
    value=proposed()
    value['annotations']=[annotation]
    result=validate_candidate(parse_task(json.dumps(value)),'生成5条每日新闻',base,current)
    assert result['jobs'][0]['count']==5
    assert result['executable']
    assert result['requirements'][0]['verification'] in ('unverified','locatable')
    assert result['requirements'][0].get('semantic_verified') is not True


def test_v2_mismatched_id_plus_quote_stays_unverified_not_falsely_repaired(calibration):
    current,_,_,base,_,_=calibration
    value=candidate()
    value['requirements'][0]['evidence_quote']='@u1 关键词：伊朗、关税'
    result=validate_candidate(parse_task(json.dumps(value)),'生成5条每日新闻，关键词：伊朗、关税',base,current)
    assert result['executable']
    note=result['requirements'][0]
    assert note['verification']=='unverified'
    assert note['evidence_quote']=='@u1 关键词：伊朗、关税'
    assert result['validation_diagnostics'][0]['reason']=='reference_text_mismatch'


@pytest.mark.parametrize('change',[{'count':21},{'kind':'unknown_column'}])
def test_locatable_invalid_core_value_is_editable_but_not_executable(calibration,change):
    current,_,_,base,_,_=calibration
    value=proposed()
    value['jobs'][0].update(change)
    result=validate_candidate(parse_task(json.dumps(value)),'生成5条每日新闻',base,current)
    assert result['jobs']
    assert result['field_errors']
    assert not result['executable']


def test_manual_clear_is_preserved_and_alternative_is_explicit_suggestion(calibration):
    from src.agent.plan_contract import normalize_plan,apply_edits
    current,_,_,base,_,_=calibration
    base=normalize_plan(base)
    job=base['jobs'][0]
    base=apply_edits(base,{'jobs':[{'target_job_id':job['job_id'],'kind':'daily_news','count':2,
                                 'search_keywords':[],'topic_preferences':[],'topic_brief':''}]})
    value=proposed()
    value['field_suggestions']=[{'kind':'daily_news','field':'count','value':8,'reason':'扩大选题'}]
    result=validate_candidate(parse_task(json.dumps(value)),'生成5条每日新闻，关键词：伊朗、关税',base,current)
    assert result['jobs'][0]['count']==2
    assert result['jobs'][0]['search_keywords']==[]
    assert result['jobs'][0]['topic_brief']==''
    suggestion=next(s for s in result['field_suggestions'] if s['field_path'].endswith('.count'))
    assert suggestion['value']==8
    assert suggestion['base_value_hash']
