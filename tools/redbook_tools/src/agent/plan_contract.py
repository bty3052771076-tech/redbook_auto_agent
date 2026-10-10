"""Finite, framework-free editorial plan contract shared by UI and CLI."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from uuid import uuid4


PLAN_SCHEMA = 'editorial-plan.v3'
COMPILER_VERSION = 'editorial-prompt.v1'
KINDS = {'daily_news':'每日新闻','daily_ai_digest':'每日AI讯息','daily_wool':'每日羊毛',
         'daily_wow':'每日我去','daily_global_map':'今日全球事件关注图'}
TOPIC_KINDS = {'daily_news','daily_ai_digest','daily_wow'}
DEFAULT_PROMPTS = {
    'daily_news':'近期有具体进展、来源可核验的新闻',
    'daily_ai_digest':'模型发布 AI厂商产品更新 具体且可核验的AI动态',
    'daily_wool':'今日仍有效的AI福利、免费额度、活动和重置信息',
    'daily_wow':'真实、具体、近期且反差强烈的猎奇事件；不要恶心或虚构',
    'daily_global_map':'仅收录当日有具体进展、来源可追溯且位置可核验的全球事件；不足时保存本地报告，不虚构全球覆盖',
}
LEGACY_NEWS_PROMPT = '国际冲突 科技产业 社会民生 财经产业'
JOB_EDIT_FIELDS = {'kind','count','search_keywords','topic_preferences','topic_brief','topic_brief_strength',
                   'content_constraints','evaluation_viewpoint','legacy_extra_requirements'}
OPTION_FIELDS = {'delivery','platform','performance_mode','model_roles','image_score_required'}
CAPABILITY_FIELDS = {'skill_mode', 'skill_names', 'disabled_tools'}
EDITABLE_OPTIONS = OPTION_FIELDS | CAPABILITY_FIELDS
STRENGTH_ALIASES = {'soft_preference': 'preference', 'hard_requirement': 'requirement'}
CONTENT_RULE_LABELS = {'dedup': '查重', 'freshness': '时效性', 'facts': '事实核验',
                       'title': '标题', 'image': '配图', 'topic_balance': '选题分布'}


class PlanContractError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 422, field: str = ''):
        super().__init__(message)
        self.code,self.message,self.status,self.field=code,message,status,field

    def public(self):
        return {'code':self.code,'error':self.message,'field':self.field}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def field_path(job: dict, name: str) -> str:
    return f"jobs.{job['job_id']}.{name}"


def _issue(code: str, field: str, message: str, content=None) -> dict:
    content_hash=digest(content)
    return {'id':digest([code,field,content_hash])[:24],'code':code,'field':field,'message':message,
            'content_hash':content_hash}


def _list_issues(value, path, errors):
    if not isinstance(value,list) or len(value)>16 or any(not isinstance(v,str) or not v.strip() or len(v)>80 for v in value):
        errors.append(_issue('TOPICS_INVALID',path,'最多16项，每项需要1至80字',value))
    elif len(set(value))!=len(value):
        errors.append(_issue('TOPICS_DUPLICATE',path,'请移除重复项',value))


def _normalize_requirement_format(job: dict) -> None:
    strength = job.get('topic_brief_strength')
    if isinstance(strength, str):
        job['topic_brief_strength'] = STRENGTH_ALIASES.get(strength, strength)
    brief, constraints = job.get('topic_brief'), job.get('content_constraints')
    if (job.get('kind') not in TOPIC_KINDS or not isinstance(brief, str)
            or not isinstance(constraints, list) or len(constraints) > 16):
        return
    remaining, instructions = [], []
    for item in constraints:
        if (isinstance(item, dict) and set(item) == {'type', 'rule'}
                and isinstance(item['type'], str) and item['type'] in CONTENT_RULE_LABELS
                and isinstance(item['rule'], str) and 0 < len(item['rule'].strip()) <= 2000):
            if item['rule'] not in brief:
                instructions.append(CONTENT_RULE_LABELS[item['type']] + '：' + item['rule'])
        else:
            remaining.append(item)
    combined = brief + ('\n生成与审核要求：\n' + '\n'.join(instructions) if instructions else '')
    if len(combined) > 2000:
        return
    # Rebind an existing approval only for a lossless format migration, never an edited brief.
    approved = job.get('topic_brief_strength_hash') == digest(brief)
    job.update(topic_brief=combined, content_constraints=remaining)
    if approved:
        job['topic_brief_strength_hash'] = digest(combined)


def suggestion_value_valid(field: str, value) -> bool:
    if field == 'count':
        return type(value) is int
    if field in {'search_keywords', 'topic_preferences'}:
        return (isinstance(value, list) and len(value) <= 16
                and all(isinstance(item, str) and 0 < len(item.strip()) <= 80 for item in value)
                and len(set(value)) == len(value))
    if field in {'topic_brief', 'evaluation_viewpoint'}:
        return isinstance(value, str) and len(value) <= (500 if field == 'evaluation_viewpoint' else 2000)
    if field == 'topic_brief_strength':
        return value is None or isinstance(value, str) and value in {'preference', 'requirement'}
    if field == 'content_constraints':
        return isinstance(value, list) and len(value) <= 16 and all(
            isinstance(item, dict) and set(item) == {'type', 'value'} and item['type'] == 'count'
            and type(item['value']) is int for item in value)
    if field == 'image_score_required':
        return type(value) is bool
    if field == 'model_roles':
        return (isinstance(value, dict) and not set(value) - {'agent', 'writer', 'image'}
                and all(isinstance(ref, str) and len(ref) <= 260 for ref in value.values()))
    options = {'delivery': {'generate_only', 'save_draft'}, 'platform': {'xhs', 'toutiao', 'both'},
               'performance_mode': {'speed', 'balanced'}}
    return field in options and isinstance(value, str) and value in options[field]


def compile_job(job: dict) -> str:
    kind=job['kind']
    if kind not in TOPIC_KINDS:
        return DEFAULT_PROMPTS.get(kind,'')
    words=job.get('search_keywords',[])
    preferences=job.get('topic_preferences',[])
    prompt=' '.join(words) if isinstance(words,list) and all(isinstance(v,str) for v in words) and words else DEFAULT_PROMPTS.get(kind,'')
    if preferences and isinstance(preferences,list) and all(isinstance(v,str) for v in preferences):
        prompt+='\n选题偏向（软性排序，不作硬配额）：'+'、'.join(preferences)
    if job.get('topic_brief'):
        prompt+='\n选题要求：'+str(job['topic_brief'])
    if job.get('legacy_extra_requirements'):
        prompt+='\n已保留的旧版附加要求：'+str(job['legacy_extra_requirements'])
    return prompt


def _execution_inputs(plan: dict) -> dict:
    return {'plan_schema_version':PLAN_SCHEMA,'compiler_version':COMPILER_VERSION,
            'jobs':[{k:deepcopy(j.get(k)) for k in ('job_id','kind','count','search_keywords','topic_preferences',
                    'topic_brief','topic_brief_strength','content_constraints','evaluation_viewpoint','lookback_days',
                    'legacy_extra_requirements')} for j in plan['jobs']],
            **{key:deepcopy(plan[key]) for key in sorted(OPTION_FIELDS)},
            **{key:deepcopy(plan[key]) for key, default in
               (('skill_mode', 'off'), ('skill_names', []), ('disabled_tools', []))
               if plan.get(key, default) != default},
            'skip_quota_sync':True,'date_policy':'host_verified','billing_policy':'no_paid_fallback'}


def normalize_plan(value: dict) -> dict:
    if not isinstance(value,dict) or not isinstance(value.get('jobs',[]),list) or len(value.get('jobs',[]))>5:
        raise PlanContractError('PLAN_SHAPE_INVALID','计划需要至多5个栏目')
    version=value.get('plan_schema_version')
    if version and version!=PLAN_SCHEMA:
        raise PlanContractError('PLAN_SCHEMA_UNSUPPORTED','无法执行未支持的计划版本')
    plan=deepcopy(value)
    legacy=version!=PLAN_SCHEMA
    errors=[]
    plan.update(plan_schema_version=PLAN_SCHEMA,compiler_version=COMPILER_VERSION,skip_quota_sync=True,budget_minutes=0.0)
    plan.setdefault('recognition_source','rules')
    plan.setdefault('last_editor',plan['recognition_source'])
    plan.setdefault('manual_overrides',{'jobs':{},'options':{},'deleted_jobs':[]})
    plan.setdefault('field_origins',{})
    plan.setdefault('review_decisions',[])
    plan.setdefault('warnings',[])
    defaults={'delivery':'save_draft','platform':'xhs','performance_mode':'balanced','image_score_required':True,
              'model_roles':{'agent':'','writer':'','image':''},
              'skill_mode':'off','skill_names':[],'disabled_tools':[]}
    for key,default in defaults.items():
        plan.setdefault(key,deepcopy(default))
    for key,allowed in (('delivery',{'generate_only','save_draft'}),('platform',{'xhs','toutiao','both'}),
                        ('performance_mode',{'speed','balanced'})):
        if not isinstance(plan[key],str) or plan[key] not in allowed:
            errors.append(_issue('OPTION_UNSUPPORTED',key,'该选项目前不受支持',plan[key]))
    if type(plan['image_score_required']) is not bool:
        errors.append(_issue('OPTION_INVALID','image_score_required','图片评分开关必须为布尔值',plan['image_score_required']))
    if plan['skill_mode'] not in ('off', 'auto', 'manual'):
        errors.append(_issue('SKILL_MODE_INVALID','skill_mode','技能模式无效',plan['skill_mode']))
    for name, maximum in (('skill_names',3), ('disabled_tools',100)):
        items = plan[name]
        if not isinstance(items,list) or len(items)>maximum or any(not isinstance(v,str) or not v or len(v)>260 for v in items) or len(set(items)) != len(items):
            errors.append(_issue('CAPABILITY_SELECTION_INVALID',name,'能力选择格式无效',items))
    if plan['skill_mode']=='manual' and not plan['skill_names']:
        errors.append(_issue('SKILL_SELECTION_INVALID','skill_names','手动模式至少选择一项技能'))
    roles=plan['model_roles']
    if not isinstance(roles,dict) or set(roles)-{'agent','writer','image'} or any(not isinstance(ref,str) or len(ref)>260 for ref in roles.values()):
        errors.append(_issue('MODEL_ROLES_INVALID','model_roles','模型角色配置无效',roles))
    else:
        plan['model_roles']={role:roles.get(role,'') for role in ('agent','writer','image')}
    seen=set()
    for index,job in enumerate(plan['jobs']):
        if not isinstance(job,dict):
            raise PlanContractError('PLAN_SHAPE_INVALID','栏目必须为结构化对象',field=f'jobs.{index}')
        kind=job.get('kind','')
        job.setdefault('job_id',digest([plan.get('id',''),kind,index])[:32])
        path=lambda name:field_path(job,name)
        if kind not in KINDS:
            errors.append(_issue('COLUMN_UNSUPPORTED',path('kind'),'此栏目未接入',kind))
        if kind in seen:
            errors.append(_issue('COLUMN_DUPLICATE',path('kind'),'同一栏目只能添加一次',kind))
        seen.add(kind)
        job['title']=KINDS.get(kind,str(kind))
        count=job.setdefault('count',1)
        if type(count) is not int or not 1<=count<=(20 if kind=='daily_news' else 1):
            errors.append(_issue('COUNT_INVALID',path('count'),'每日新闻1至20篇，其他栏目固定1篇',count))
        if legacy:
            words=deepcopy(job.get('keywords',[]))
            mode=job.get('keyword_mode','default')
            job.setdefault('search_keywords',words if mode=='filter' else [])
            job.setdefault('topic_preferences',words if mode=='preference' else [])
            job['legacy_migrated']=True
            prefix=str(job.get('prompt','')).partition('\n选题要求：')[0]
            known={*DEFAULT_PROMPTS.values(),LEGACY_NEWS_PROMPT,KINDS.get(kind,'')}
            if isinstance(words,list) and all(isinstance(w,str) for w in words):
                known.add(' '.join(words))
            if mode=='default' and prefix==LEGACY_NEWS_PROMPT and not job['topic_preferences']:
                job['topic_preferences']=LEGACY_NEWS_PROMPT.split()
            if prefix and prefix not in known:
                job.setdefault('legacy_extra_requirements',str(job.get('prompt','')))
        job.setdefault('search_keywords',[])
        job.setdefault('topic_preferences',[])
        job.setdefault('topic_brief','')
        job.setdefault('evaluation_viewpoint','无视角评价')
        job.setdefault('lookback_days','auto')
        job.setdefault('legacy_extra_requirements','')
        job.setdefault('content_constraints',[])
        for name in ('search_keywords','topic_preferences'):
            _list_issues(job[name],path(name),errors)
        for name,limit in (('topic_brief',2000),('evaluation_viewpoint',500),('legacy_extra_requirements',12000)):
            if not isinstance(job[name],str) or len(job[name])>limit:
                errors.append(_issue('TEXT_INVALID',path(name),f'文本须不超过{limit}字',job[name]))
        if job['lookback_days']!='auto':
            errors.append(_issue('DATE_POLICY_READONLY',path('lookback_days'),'日期核验沿用宿主策略，不允许编辑',job['lookback_days']))
        if legacy and job['topic_brief']:
            required=any(r.get('strength')=='requirement' and r.get('scope') in ('plan','job:'+kind)
                         and r.get('target')=='job.topic_brief' for r in plan.get('requirements',[]) if isinstance(r,dict))
            job.setdefault('topic_brief_strength','requirement' if required else 'preference')
            job.setdefault('topic_brief_strength_hash',digest(job['topic_brief']))
        _normalize_requirement_format(job)
        job.setdefault('topic_brief_strength',None)
        if job['topic_brief'] and (job['topic_brief_strength'] not in ('preference','requirement')
                                  or job.get('topic_brief_strength_hash')!=digest(job['topic_brief'])):
            errors.append(_issue('REQUIREMENT_STRENGTH_NEEDED',path('topic_brief_strength'),'请为当前补充要求选择偏好或必须满足',job['topic_brief']))
        constraints=job['content_constraints']
        if not isinstance(constraints,list) or len(constraints)>16:
            errors.append(_issue('CONSTRAINT_INVALID',path('content_constraints'),'要求绑定格式无效',constraints))
            constraints=[]
        if job['topic_brief'] and job['topic_brief_strength']=='requirement':
            supported=bool(constraints) and all(isinstance(c,dict) and c.get('type')=='count'
                                              and type(c.get('value')) is int and c['value']==count for c in constraints)
            if not supported:
                errors.append(_issue('UNSUPPORTED_HARD_REQUIREMENT',path('topic_brief'),'必须满足的文字要求尚未绑定可保证的执行字段；请修改、改为偏好或删除',job['topic_brief']))
        if any(not isinstance(c,dict) or c.get('type') not in ('count',) for c in constraints):
            errors.append(_issue('CONSTRAINT_UNSUPPORTED',path('content_constraints'),'当前不能保证分类硬配额或任意文字硬约束',constraints))
        if kind not in TOPIC_KINDS:
            for name in ('search_keywords','topic_preferences','topic_brief','legacy_extra_requirements','evaluation_viewpoint'):
                if job[name] and not (name=='evaluation_viewpoint' and job[name]=='无视角评价'):
                    errors.append(_issue('FIELD_NOT_CONSUMED',path(name),'此栏目未接入该字段；可清除已有要求',job[name]))
        if job['legacy_extra_requirements']:
            problem=_issue('LEGACY_EXTRA_REVIEW',path('legacy_extra_requirements'),'请核对并明确保留或移除旧版附加要求',job['legacy_extra_requirements'])
            if not any(d.get('issue_id')==problem['id'] and d.get('content_hash')==problem['content_hash']
                       and d.get('decision')=='keep_brief' for d in plan['review_decisions']):
                errors.append(problem)
        for removed in job.get('removed_legacy_topics',[]):
            if isinstance(job['topic_brief'],str) and removed in job['topic_brief']:
                problem=_issue('REMOVED_TOPIC_IN_BRIEF',path('topic_brief'),'已删除偏向仍在补充说明中；请修改说明或明确保留',job['topic_brief'])
                if not any(d.get('issue_id')==problem['id'] and d.get('content_hash')==problem['content_hash']
                           and d.get('decision')=='keep_brief' for d in plan['review_decisions']):
                    errors.append(problem)
                    break
        job['prompt']=compile_job(job)
        job['keyword_mode']='filter' if job['search_keywords'] else 'preference' if job['topic_preferences'] else 'default'
        job['keywords']=deepcopy(job['search_keywords'] or job['topic_preferences'])
    if not plan['jobs']:
        errors.append(_issue('COLUMN_REQUIRED','jobs','请至少添加一个栏目'))
    plan['field_errors']=errors
    plan['unresolved_requirements']=[e['message'] for e in errors]
    plan['executable']=not errors
    plan['semantic_hash']=digest(_execution_inputs(plan))
    return plan


def apply_edits(plan: dict, editable_fields: dict, *, review_decisions: list | None = None) -> dict:
    base=normalize_plan(plan)
    if not isinstance(editable_fields,dict) or set(editable_fields)-EDITABLE_OPTIONS-{'jobs'}:
        raise PlanContractError('PLAN_FIELDS_FORBIDDEN','请求包含不允许编辑的字段')
    result=deepcopy(base)
    overrides=result['manual_overrides']
    overrides.setdefault('jobs',{})
    overrides.setdefault('options',{})
    overrides.setdefault('deleted_jobs',[])
    for name in EDITABLE_OPTIONS:
        if name in editable_fields:
            result[name]=deepcopy(editable_fields[name])
            overrides['options'][name]=deepcopy(result[name])
            result['field_origins'][name]='user_edit'
    if 'jobs' in editable_fields:
        submitted=editable_fields['jobs']
        if not isinstance(submitted,list) or len(submitted)>5:
            raise PlanContractError('PLAN_SHAPE_INVALID','栏目列表格式无效')
        old={j['job_id']:j for j in base['jobs']}
        jobs=[]
        used=set()
        for item in submitted:
            if not isinstance(item,dict) or set(item)-JOB_EDIT_FIELDS-{'target_job_id'}:
                raise PlanContractError('PLAN_FIELDS_FORBIDDEN','栏目包含不允许编辑的字段')
            target=item.get('target_job_id')
            if target:
                if target not in old or target in used:
                    raise PlanContractError('PLAN_JOB_CONFLICT','栏目已变化或重复，请刷新后保存',status=409)
                job=deepcopy(old[target])
                if 'kind' in item and item['kind']!=job['kind']:
                    raise PlanContractError('COLUMN_IDENTITY_READONLY','更换栏目请先删除再添加')
                used.add(target)
            else:
                job={'job_id':uuid4().hex,'kind':item.get('kind'),'count':1,'search_keywords':[],
                     'topic_preferences':[],'topic_brief':'','evaluation_viewpoint':'无视角评价','lookback_days':'auto'}
            before=deepcopy(job)
            job.update({k:deepcopy(v) for k,v in item.items() if k in JOB_EDIT_FIELDS})
            if 'topic_brief_strength' in item:
                job['topic_brief_strength_hash']=digest(job.get('topic_brief','')) if item['topic_brief_strength'] in ('preference','requirement') else ''
            if job.get('legacy_migrated'):
                removed=[w for w in before.get('search_keywords',[])+before.get('topic_preferences',[])
                         if w not in job.get('search_keywords',[])+job.get('topic_preferences',[])]
                job['removed_legacy_topics']=list(dict.fromkeys(job.get('removed_legacy_topics',[])+removed))
            changed={k:deepcopy(job[k]) for k in JOB_EDIT_FIELDS if k in item and k in job
                     and (job[k]!=before.get(k) or job[k] in ('',None,[]))}
            if changed:
                overrides['jobs'].setdefault(job['job_id'],{}).update(changed)
                if 'topic_brief_strength' in item:
                    overrides['jobs'][job['job_id']]['topic_brief_strength_hash']=job['topic_brief_strength_hash']
                for name in changed:
                    result['field_origins'][field_path(job,name)]='user_edit'
            jobs.append(job)
        deleted=[identity for identity in old if identity not in used]
        overrides['deleted_jobs']=list(dict.fromkeys(overrides['deleted_jobs']+deleted))
        overrides['job_order']=[j['job_id'] for j in jobs]
        result['jobs']=jobs
    if review_decisions is not None:
        if not isinstance(review_decisions,list) or len(review_decisions)>50 or any(not isinstance(d,dict)
            or set(d)!={'issue_id','content_hash','decision'} or d['decision']!='keep_brief' for d in review_decisions):
            raise PlanContractError('REVIEW_DECISION_INVALID','要求核对选择格式无效')
        allowed={(e['id'],e['content_hash']) for e in base['field_errors'] if e['code'] in {'REMOVED_TOPIC_IN_BRIEF','LEGACY_EXTRA_REVIEW'}}
        if any((d['issue_id'],d['content_hash']) not in allowed for d in review_decisions):
            raise PlanContractError('REVIEW_DECISION_STALE','核对内容已变化，请重新选择',status=409)
        result['review_decisions']+=deepcopy(review_decisions)
    result['last_editor']='user'
    return normalize_plan(result)


def execution_fields(plan: dict) -> dict:
    normalized=normalize_plan(plan)
    if not normalized['executable']:
        raise PlanContractError('PLAN_NEEDS_INPUT','计划仍有需要修正的字段',status=409)
    result=_execution_inputs(normalized)
    result['jobs']=deepcopy(normalized['jobs'])
    result['semantic_hash']=normalized['semantic_hash']
    return result


def verify_execution(frozen: dict) -> None:
    if frozen.get('plan_schema_version')!=PLAN_SCHEMA or frozen.get('compiler_version')!=COMPILER_VERSION:
        raise PlanContractError('PLAN_SCHEMA_UNSUPPORTED','冻结计划版本未受支持')
    normalized=normalize_plan(frozen)
    if not normalized['executable'] or frozen.get('semantic_hash')!=normalized['semantic_hash']:
        raise PlanContractError('PLAN_FROZEN_MISMATCH','冻结计划摘要不一致，不能启动任务')
    if any(a.get('prompt')!=b['prompt'] for a,b in zip(frozen['jobs'],normalized['jobs'])):
        raise PlanContractError('PLAN_FROZEN_MISMATCH','冻结提示词与已确认字段不一致')
