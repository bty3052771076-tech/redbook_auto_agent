"""Bind governance to the existing business adapters, not a second workflow."""
from dataclasses import replace
from functools import wraps
import os
from pathlib import Path
import json
from uuid import uuid4

from .dispatcher import ToolDispatcher
from .registry import KIND_TO_TOOL, builtin_catalog
from .store import CapabilityStore
from .models import CapabilityError, digest, safe
from .skill_runtime import resource_tools, read_resource


def wrap_tools(tools, dispatcher, *, mcp_runtime=None, selected_skills=None):
    skills={skill['id']:skill for skill in (selected_skills or []) if skill.get('id')}
    def bind(callback, resource, stage):
        if callback is None:
            return None

        @wraps(callback)
        def call(*args, **kwargs):
            job = args[0] if args else None
            identity = resource(job) if callable(resource) else resource
            summary = {'kind': getattr(job, 'kind', ''), 'count': getattr(job, 'count', None)}
            if args and isinstance(args[-1],dict):
                args=(*args[:-1],{**args[-1],'job_kind':getattr(job,'kind','')})
            return dispatcher.call(identity, lambda: callback(*args, **kwargs), stage=stage, arguments=summary)

        return call

    def stage_tools(stage):
        return [row for row in dispatcher.snapshot['tools'].values() if row.get('kind') in {'mcp','skill_resource'}
                and row.get('enabled') and row.get('binding') != 'unbound' and stage in row.get('stages',[])]

    def select_and_run(jobs, context, stage, available):
        catalog_input = [{key:row.get(key) for key in ('id','name','description','input_schema','purpose','resource_names')}
                         for row in available]
        if len(json.dumps(catalog_input,ensure_ascii=False,default=str)) > 60000:
            raise CapabilityError('MCP_SELECTION_CATALOG_TOO_LARGE','当前阶段工具目录过大，请减少已批准的使用范围')
        context.update(preparation_tools=catalog_input,tool_stage=stage)
        result = tools.plan(jobs,context)
        if not isinstance(result,dict):
            raise CapabilityError('MCP_SELECTION_INVALID','主控选择结果必须是对象')
        requests = result.get('tool_calls') or []
        if not isinstance(requests,list) or len(requests)>3 or any(not isinstance(item,dict) for item in requests):
            raise CapabilityError('MCP_SELECTION_INVALID','主控每阶段最多选择3项只读工具')
        results, skill_results, catalog = [], [], {row['id']:row for row in available}
        for request in requests:
            identity = str(request.get('tool_id') or '')
            row = catalog.get(identity)
            if row is None:
                raise CapabilityError('CAPABILITY_STAGE_DENIED','主控选择了此阶段未授权的工具',resource_id=identity)
            is_skill=row.get('kind')=='skill_resource'
            try:
                def invoke():
                    if is_skill:
                        return read_resource(skills[identity.split(':',1)[1]],request.get('arguments',{}))
                    if mcp_runtime is None:
                        raise ValueError('MCP_RUNTIME_UNAVAILABLE')
                    return mcp_runtime.call(row,request.get('arguments',{}))
                output = dispatcher.call(identity,invoke,stage=stage,
                    arguments={'arguments':request.get('arguments',{}),'purpose':row['purpose'],
                               'reason':str(request.get('reason') or '')[:200]})
                path = Path(os.getenv('REDBOOK_RUNTIME_ROOT') or Path.cwd()) / 'data/agent/evidence' / dispatcher.snapshot['run_id'] / (uuid4().hex+'.json')
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(json.dumps(safe(output),ensure_ascii=False,default=str),encoding='utf-8')
                (skill_results if is_skill else results).append({'tool_id':identity,'purpose':row['purpose'],'status':'succeeded',
                                'result_ref':str(path),'output':safe(output)})
            except CapabilityError:
                raise
            except Exception as exc:
                results.append({'tool_id':identity,'status':'failed','error':safe(str(exc))})
        return {**result,'mcp_'+stage:results,'skill_'+stage:skill_results}

    def plan(jobs, context):
        return dispatcher.call('builtin:controller.plan',
            lambda:select_and_run(jobs,context,'preparation',stage_tools('preparation')),
            stage='preparation',arguments={'jobs':len(jobs)})

    def evidence_review(callback):
        if callback is None:
            return None
        def review(job, posts, context):
            def execute():
                available = stage_tools('evidence')
                if available and tools.plan is not None:
                    artifacts=[]
                    for post in posts:
                        value = post if isinstance(post,dict) else {key:getattr(post,key,'') for key in ('id','title','body')}
                        artifact={key:value.get(key,'') for key in ('id','title','body')}
                        artifact['body_hash'] = digest(artifact['body'])
                        if len(str(artifact['body'])) > 4000:
                            artifact.update(body='',body_omitted=True)
                        artifacts.append(safe(artifact))
                    version=digest({'artifacts':artifacts,'tools':available})
                    if context.get('mcp_evidence_version') != version:
                        result=dispatcher.call('builtin:controller.plan',
                            lambda:select_and_run([job],{**context,'artifacts':artifacts},'evidence',available),
                            stage='evidence',arguments={'kind':job.kind,'posts':len(artifacts),'tools':len(available)})
                        context.update(mcp_evidence=result['mcp_evidence'],mcp_evidence_version=version)
                return callback(job,posts,{**context,'job_kind':job.kind})
            return dispatcher.call('builtin:content.review',execute,stage='review',arguments={'kind':job.kind,'posts':len(posts)})
        return review

    return replace(tools,
        sync_context=bind(tools.sync_context, 'builtin:account.sync', 'preparation'),
        plan=plan if tools.plan is not None else None,
        generate=bind(tools.generate, lambda job: KIND_TO_TOOL.get(job.kind, 'builtin:unknown'), 'generate'),
        review=evidence_review(tools.review),
        revalidate_completed=evidence_review(tools.revalidate_completed),
        load_posts=bind(tools.load_posts, 'builtin:artifacts.read', 'recovery'),
        reconcile_uploads=bind(tools.reconcile_uploads, 'builtin:xhs.drafts.read', 'evidence'),
        upload=bind(tools.upload, 'builtin:xhs.drafts.save_batch', 'upload'),
        upload_batch=bind(tools.upload_batch, 'builtin:xhs.drafts.save_batch', 'upload'),
        capability_cleanup=mcp_runtime.close if mcp_runtime is not None else tools.capability_cleanup)


def runtime_tools(tools, run_id, *, progress=None, conversation_context=None, require_frozen=False):
    store = CapabilityStore(namespace=os.getenv('AGENT_CAPABILITY_NAMESPACE', 'local'))
    store.ensure_schema()
    snapshot = store.snapshot(run_id)
    if snapshot is None:
        if require_frozen or os.getenv('AGENT_REQUIRE_FROZEN_CAPABILITIES') == '1':
            raise CapabilityError('CAPABILITY_SNAPSHOT_MISSING','原任务的能力快照不存在；不能用当前配置替代续跑',status=409)
        snapshot = store.freeze(run_id, builtin_catalog()+resource_tools((conversation_context or {}).get('skills',[])),
                                metadata={'origin': 'cli', 'policy_source': 'runtime_registry'})
    from .mcp_runtime import MCPRuntime
    from src.agent.mcp_manager import MCPManager
    from src.model_platforms.security import CredentialStore
    root = Path(os.getenv('REDBOOK_RUNTIME_ROOT') or Path.cwd())
    credentials = CredentialStore(root/'data/agent/credentials/mcp')
    runtime = MCPRuntime(manager_factory=lambda connection: MCPManager(root,connection=connection,credentials=credentials,namespace=store.namespace))
    for skill in (conversation_context or {}).get('skills',[]):
        identity = skill.get('id')
        if identity:
            call = store.start_call({'run_id':run_id,'resource_id':identity,'resource_name':skill.get('name'),
                'stage':'preparation','status':'running','origin':snapshot.get('origin','business'),'version':skill.get('version_hash'),
                'action':'body_loaded','loaded_bodies':1,'loaded_resources':0,'loaded_characters':len(skill.get('body') or '')})
            store.finish_call(call,'succeeded',{'loaded_bodies':1,'loaded_resources':0})
    return wrap_tools(tools, ToolDispatcher(store, snapshot, origin='diagnostic' if snapshot.get('origin')=='diagnostic' else 'business', progress=progress),mcp_runtime=runtime,
                      selected_skills=(conversation_context or {}).get('skills',[]))
