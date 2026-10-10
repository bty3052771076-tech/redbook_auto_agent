from __future__ import annotations

from datetime import datetime,timezone
from pathlib import Path
import time

from .models import CapabilityError,safe
from .registry import builtin_catalog,KIND_TO_TOOL
from .store import CapabilityStore
from .runtime_paths import RuntimePaths
from .operations import OperationManager
from .mcp_service import MCPConnectionService
from .skill_service import SkillService
from .knowledge_service import KnowledgeManagement
from src.agent.memory_service import MemoryService


class CapabilityService:
    def __init__(self, root: Path, *, store=None, workbench=None):
        self.root=Path(root)
        self.store=store or CapabilityStore()
        self.store.ensure_schema()
        self.workbench=workbench
        self.mcp=MCPConnectionService(root,self.store)
        self.skills=SkillService(root,self.store)
        self.memory=MemoryService(self.store)
        self.knowledge=KnowledgeManagement(self.store.knowledge_store,self.store)
        self.operations=OperationManager(self.store,root=self.root)
        from .context_service import ContextService
        self.context=ContextService(self)

    def catalog(self, *, query: str='', kind: str='', group: str='', status: str='', limit: int=50,cursor: str='') -> dict:
        rows=self.all_tools()
        rows=[r for r in rows if (not query or query.casefold() in (r['name']+' '+r['description']).casefold())
              and (not kind or r['kind']==kind) and (not group or r['group']==group)
              and (not status or r.get('health',{}).get('status')==status or ('enabled' if r.get('enabled') else 'disabled')==status)]
        counts={'registered':len(rows),'available':sum(bool(r.get('enabled')) and r.get('health',{}).get('status')=='ready' for r in rows),
                'enabled':sum(bool(r.get('enabled')) for r in rows)}
        rows.sort(key=lambda r:r['id'])
        if cursor:
            rows=[r for r in rows if r['id']>cursor]
        visible=rows[:limit]
        active={}
        for reference in self.store.active_references():
            active.setdefault(reference['resource_id'],[]).append(reference)
        visible=[{**row,'active_runs':active.get(row['id'],[])} for row in visible]
        visible=[{**row,'connection':{key:row['connection'].get(key) for key in ('id','name','revision','transport')}}
                 if row.get('connection') else row for row in visible]
        database=self.store.knowledge_store.status()
        issues=[{'resource_id':r['id'],'message':r['health'].get('error','能力依赖不可用')} for r in rows if r.get('health',{}).get('status')=='blocked']
        try:
            environment=RuntimePaths.resolve(self.root).public()
        except CapabilityError as exc:
            environment={'runtime_root':str(self.root),'error':exc.public()}
        env=self.workbench.environment() if self.workbench else {}
        environment.update(profile=env.get('XHS_CHROME_USER_DATA_DIR',str(self.root/'data/browser/chrome-profile')),
                           profile_directory=env.get('XHS_CHROME_PROFILE') or 'Default',namespace=self.store.namespace,profile_login='未验证')
        return safe({'rows':visible,'total':counts['registered'],'counts':counts,'next_cursor':visible[-1]['id'] if len(rows)>limit else None,
                     'database':database,'environment':environment,'issues':issues,
                     'skills':self.skills.list(),'mcp':self.mcp.list(),'recent_calls':self.store.calls(limit=10)['rows']})

    def all_tools(self) -> list[dict]:
        configured={r['id']:r for r in self.store.resources('tool')}
        rows=[{**r,**configured.get(r['id'],{}),**{key:r[key] for key in
            ('version','input_schema','output_schema','concurrency','concurrency_policy','idempotency','recovery')}}
            for r in builtin_catalog()]
        rows.extend(self.mcp.tools())
        return rows

    def detail(self, identity: str) -> dict:
        tools={r['id']:r for r in self.all_tools()}
        row=tools.get(identity)
        if row is None:
            raise CapabilityError('CAPABILITY_NOT_FOUND','工具不存在',status=404,resource_id=identity)
        from .trials import trial_contract
        labels={'database':'PostgreSQL知识库','profile':'小红书专用浏览器配置',
                'agent_model':'智能体主控模型','writer_model':'内容写稿模型','image_model':'生图模型'}
        dependencies=[{'resource_id':dependency,'name':tools[dependency]['name'],'kind':tools[dependency]['kind']}
                      if dependency in tools else labels.get(dependency,dependency)
                      for dependency in row.get('dependencies',[])]
        return safe({**row,'dependency_details':dependencies,'trial':trial_contract(identity),'versions':self.store.versions(identity),'recent_calls':self.store.calls(resource_id=identity)['rows'],
                     'active_runs':self.store.active_references(identity)})

    def patch(self, identity: str, data: dict) -> dict:
        row=self.detail(identity)
        if row['kind']=='mcp':
            raise CapabilityError('MCP_POLICY_REQUIRED','请在连接详情中管理该工具的使用范围')
        if row.get('required') and data.get('enabled') is False:
            raise CapabilityError('CAPABILITY_REQUIRED','审核和保留产物是必需依赖，不能停用')
        allowed={'expected_revision','enabled','timeout_seconds','reason'}
        if set(data)-allowed:
            raise CapabilityError('CAPABILITY_SETTING_INVALID','此参数不允许修改')
        payload={k:v for k,v in row.items() if k not in {'versions','recent_calls','active_runs','dependency_details','trial'}}
        if 'enabled' in data:
            if not isinstance(data['enabled'],bool):
                raise CapabilityError('CAPABILITY_SETTING_INVALID','enabled必须为布尔值')
            payload['enabled']=data['enabled']
        if 'timeout_seconds' in data:
            if not row.get('timeout_configurable'):
                raise CapabilityError('CAPABILITY_TIMEOUT_UNSUPPORTED','此适配器尚未支持可配置单次超时',status=422)
            if isinstance(data['timeout_seconds'],bool) or not isinstance(data['timeout_seconds'],(int,float)):
                raise CapabilityError('CAPABILITY_TIMEOUT_INVALID','超时必须是数字',status=422)
            number=float(data['timeout_seconds'])
            if not 1<=number<=row.get('max_timeout_seconds',60):
                raise CapabilityError('CAPABILITY_TIMEOUT_INVALID','超时超出适配器允许范围')
            payload['timeout_seconds']=number
        return self.store.put(identity,'tool',payload,expected_revision=int(data['expected_revision']),reason=data.get('reason','修改工具策略'),clear_revocation=bool(data.get('enabled')))

    def revoke(self, identity: str, data: dict) -> dict:
        row=self.detail(identity)
        revision=int(data['expected_revision'])
        if not self.store.get(identity):
            payload={k:v for k,v in row.items() if k not in {'versions','recent_calls','active_runs','dependency_details','trial'}}
            row=self.store.put(identity,'tool',payload,expected_revision=0,reason='登记撤销策略')
            if revision==0:
                revision=row['revision']
        return self.store.revoke(identity,expected_revision=revision,reason=data.get('reason','停止后续调用'))

    def check(self, resource_ids: list[str]) -> list[dict]:
        results=[]
        for identity in resource_ids:
            row=self.detail(identity)
            health={'status':'ready','probe':'configuration','observed_at':datetime.now(timezone.utc).isoformat()}
            for dependency in row.get('dependencies',[]):
                if dependency=='database':
                    if self.store.knowledge_store.status()['status']!='ready':
                        health.update(status='blocked',error='PostgreSQL不可用')
                elif dependency=='profile':
                    health.update(status='unknown',error='Profile路径已配置；登录状态须通过专用浏览器只读检测')
                elif dependency.endswith('_model') and self.workbench:
                    binding=self.workbench.providers().get('bindings',{}).get(dependency[:-6],'')
                    if binding:
                        try:
                            if binding.startswith('m_'):
                                self.workbench.model_platforms().resolve(dependency[:-6],binding)
                        except Exception as exc:
                            health.update(status='blocked',error=str(safe(str(exc))))
            config={k:v for k,v in row.items() if k not in {'versions','recent_calls','active_runs','dependency_details','trial'}}
            if identity.startswith('mcp:'):
                server=self.mcp.discover(identity.split(':',2)[1])
                results.append({'resource_id':identity,'health':server['health']})
            else:
                self.store.put(identity,'tool',{**config,'health':health},expected_revision=row['revision'],reason='只读配置检测')
                results.append({'resource_id':identity,'health':health})
        return results

    def plan(self, conversation_id: str, plan_id: str) -> tuple[dict,dict]:
        conversation=self.workbench._read_agent_conversation(conversation_id)
        plan=next((p for p in conversation.get('plans',[]) if p['id']==plan_id),None)
        if not plan:
            raise CapabilityError('PLAN_NOT_FOUND','计划不存在',status=404)
        return conversation,plan

    def plan_capabilities(self, conversation_id: str, plan_id: str) -> dict:
        conversation,plan=self.plan(conversation_id,plan_id)
        run_id=plan.get('agent_run_id') or plan.get('job_id') or plan.get('resume_job_id') or plan.get('execution_request_id')
        snapshot=self.store.snapshot(run_id) if run_id else None
        if snapshot:
            return {**snapshot,'tools':list(snapshot['tools'].values()),'frozen':True,'version':plan['version']}
        tools={r['id']:r for r in self.all_tools()}
        selected=['builtin:account.sync','builtin:controller.plan','builtin:content.review','builtin:artifacts.read']
        selected.extend(KIND_TO_TOOL[j['kind']] for j in plan.get('jobs',[]) if j.get('kind') in KIND_TO_TOOL)
        if plan.get('delivery') not in {'generate','generate_only','local_only'}:
            selected.append('builtin:xhs.drafts.save_batch')
        def collect(identity):
            for dependency in tools.get(identity,{}).get('dependencies',[]):
                if dependency.startswith(('builtin:','mcp:')) and dependency not in selected:
                    selected.append(dependency)
                    collect(dependency)
        for identity in list(selected):
            collect(identity)
        disabled=set(plan.get('disabled_tools',[]))
        blockers=[{'resource_id':identity,'message':f"{tools[identity]['name']}已停用"} for identity in selected
                  if identity in disabled or not tools[identity].get('enabled',True)]
        skill_policy=self.store.get('policy:skills') or {}
        skill_mode=plan.get('skill_mode',skill_policy.get('mode','off'))
        names=plan.get('skill_names',[])
        query=' '.join(str(j.get('kind',''))+' '+str(j.get('prompt','')) for j in plan.get('jobs',[]))
        try:
            skills=self.skills.select(query,mode=skill_mode,names=names)
        except CapabilityError as exc:
            skills=[]
            blockers.append(exc.public())
        preferences=[]
        for job in plan.get('jobs',[]):
            preferences.extend({**row,'applies_to':[job['kind']]} for row in self.memory.select(
                account=conversation.get('account_id',''),column=job['kind'],conversation=conversation_id))
        grouped={}
        for row in preferences:
            if row['id'] in grouped:
                grouped[row['id']]['applies_to'].extend(row['applies_to'])
            else:grouped[row['id']]=row
        memory=list(grouped.values())
        environment=self.workbench.environment()
        models=plan.get('model_roles') or self.workbench.providers().get('bindings',{})
        return safe({'tools':[tools[r] for r in dict.fromkeys(selected)],'skills':skills,'skill_mode':skill_mode,'skill_names':names,
                     'skill_candidates':self.skills.list()['rows'],'disabled_tools':list(disabled),
                     'memory':memory,'models':models,'origin':plan.get('diagnostic_origin','business'),'profile':{'user_data_dir':environment.get('XHS_CHROME_USER_DATA_DIR',''),
                     'profile_directory':environment.get('XHS_CHROME_PROFILE') or 'Default','login':'未验证'},
                     'readiness':{'ready':not blockers,'blockers':blockers},'frozen':bool(run_id),'snapshot_id':None,'version':plan['version']})

    def save_plan(self, conversation_id: str, plan_id: str, data: dict) -> dict:
        with self.workbench.lock:
            conversation,plan=self.plan(conversation_id,plan_id)
            if plan.get('job_id') or plan.get('resume_job_id'):
                raise CapabilityError('PLAN_FROZEN','已执行计划的能力已冻结',status=409)
            if plan['version']!=data.get('version'):
                raise CapabilityError('REVISION_CONFLICT','计划已变化，请刷新',status=409)
            mode=data.get('skill_mode','off')
            self.skills.select(' '.join(j.get('prompt','') for j in plan.get('jobs',[])),mode=mode,names=data.get('skill_names',[]))
            disabled=data.get('disabled_tools',[])
            catalog={r['id']:r for r in self.all_tools()}
            if not isinstance(disabled,list) or any(identity not in catalog or catalog[identity].get('required') for identity in disabled):
                raise CapabilityError('CAPABILITY_SELECTION_INVALID','不能停用必需工具或使用未知工具')
            from backend.plan_service import PlanService
            result = PlanService(self.workbench).save(conversation_id, plan_id, {
                'base_plan_version':data['version'], 'conversation_revision':conversation.get('_revision'),
                'editable_fields':{'skill_mode':mode, 'skill_names':data.get('skill_names',[]), 'disabled_tools':disabled}},
                data.get('request_key') or __import__('uuid').uuid4().hex)
        return {'plan':result['plan'],**self.plan_capabilities(conversation_id,result['plan']['id'])}

    def freeze_plan(self, conversation_id: str, plan_id: str, run_id: str, *, connection=None, preview=None, plan=None) -> dict:
        preview=preview or self.plan_capabilities(conversation_id,plan_id)
        if not preview['readiness']['ready']:
            blocker=preview['readiness']['blockers'][0]
            raise CapabilityError('CAPABILITY_PREFLIGHT_BLOCKED',blocker.get('message','能力预检未通过'),status=409,resource_id=blocker.get('resource_id',''))
        if plan is None:
            _,plan=self.plan(conversation_id,plan_id)
        from .skill_runtime import resource_tools
        tools=self.all_tools()+resource_tools(preview.get('skills',[]))
        disabled=set(plan.get('disabled_tools',[]))
        return self.store.freeze(run_id,tools,disabled_tools=disabled,connection=connection,
            metadata={k:v for k,v in preview.items() if k not in {'tools','snapshot_id','frozen','skill_candidates'}} |
                {'conversation_id':conversation_id,'plan_id':plan_id,'namespace':self.store.namespace})
