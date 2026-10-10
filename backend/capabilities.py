from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import re
from uuid import uuid4

from fastapi import APIRouter, Header, Query, Request
from psycopg import OperationalError
from psycopg_pool import PoolClosed, PoolTimeout

from src.agent.capabilities.models import CapabilityError,safe
from src.agent.capabilities.service import CapabilityService
from src.agent.capabilities.store import CapabilityStore


def manager(current):
    existing=getattr(current,'_capability_manager',None)
    if existing is None:
        namespace = getattr(current.conversation_store, 'namespace', 'local')
        knowledge = getattr(current.conversation_store, 'knowledge_store', None)
        existing=CapabilityService(current.root,store=CapabilityStore(knowledge,namespace=namespace),workbench=current)
        current._capability_manager=existing
    return existing


async def body(request: Request) -> dict:
    raw=await request.body()
    if len(raw)>2*1024*1024:
        raise CapabilityError('REQUEST_TOO_LARGE','请求最多2MiB')
    try:
        value=json.loads(raw or b'{}')
    except (ValueError,UnicodeError):
        raise CapabilityError('REQUEST_JSON_INVALID','请求必须是JSON对象') from None
    if not isinstance(value,dict):
        raise CapabilityError('REQUEST_JSON_INVALID','请求必须是JSON对象')
    return value


def create_capability_router(service, *, runtime_root=None):
    router=APIRouter()
    def current():
        return manager(service())

    @router.get('/api/capabilities')
    def catalog(query: str='',kind: str='',group: str='',status: str='',limit: int=Query(50,ge=1,le=200),cursor: str=''):
        try:
            return current().catalog(query=query,kind=kind,group=group,status=status,limit=limit,cursor=cursor)
        except (OperationalError, PoolClosed, PoolTimeout):
            pass
        except CapabilityError as exc:
            if exc.code != 'POSTGRES_UNAVAILABLE':
                raise
        from src.agent.capabilities.registry import builtin_catalog
        error = {'code':'POSTGRES_UNAVAILABLE','message':'PostgreSQL 暂不可用，仅显示静态内置目录',
                 'next_action':'恢复数据库连接后刷新；动态目录、启停配置和调用记录暂不可读取'}
        # Static definitions are not a persisted-settings fallback or an executable policy.
        rows = [{**row, 'enabled':False, 'configuration_available':False, 'revision':None,
                 'health':{'status':'blocked','error':error['message']}} for row in builtin_catalog()]
        rows = [row for row in rows if (not query or query.casefold() in (row['name']+' '+row['description']).casefold())
                and (not kind or kind == row['kind']) and (not group or group == row['group'])
                and (not status or status == 'blocked')]
        rows.sort(key=lambda row: row['id'])
        total = len(rows)
        rows = [row for row in rows if not cursor or row['id'] > cursor]
        visible = rows[:limit]
        return {'rows':visible,'total':total,'next_cursor':visible[-1]['id'] if len(rows)>limit else None,
            'read_only':True,'error':error,'database':{'status':'offline','observed_at':datetime.now(timezone.utc).isoformat()},
            'environment':{'runtime_root':str(runtime_root or ''),'configuration_state':'unavailable'},
            'issues':[{'kind':'database','message':error['message']}],
            'recent_calls':[],'recent_calls_available':False}

    @router.post('/api/capabilities/checks')
    async def checks(request: Request,idempotency_key: str=Header(default='')):
        data=await body(request)
        identities=data.get('resource_ids',[])
        if not isinstance(identities,list) or not 1<=len(identities)<=50 or any(not isinstance(i,str) for i in identities):
            raise CapabilityError('CHECK_SELECTION_INVALID','请选择1至50项工具')
        instance=current()
        for identity in identities:
            instance.detail(identity)
        return instance.operations.submit(lambda:instance.check(identities),title='检查工具配置',key=idempotency_key,request={'resource_ids':identities})

    @router.get('/api/capabilities/checks/{identity}')
    def operation(identity: str):
        return current().operations.get(identity)

    @router.post('/api/capabilities/{identity}/trial-preview')
    async def trial_preview(identity: str,request: Request):
        from src.agent.capabilities.trials import CapabilityTrials
        return CapabilityTrials(current()).preview(identity,await body(request))

    @router.post('/api/capability-trials/{identity}/confirm')
    async def trial_confirm(identity: str,request: Request):
        from src.agent.capabilities.trials import CapabilityTrials
        return CapabilityTrials(current()).confirm(identity,await body(request))

    @router.post('/api/capabilities/{identity}/revoke')
    async def revoke(identity: str,request: Request):
        return current().revoke(identity,await body(request))

    @router.get('/api/capabilities/{identity}')
    def tool(identity: str):
        return current().detail(identity)

    @router.patch('/api/capabilities/{identity}')
    async def tool_patch(identity: str,request: Request):
        return current().patch(identity,await body(request))

    @router.get('/api/mcp/connections')
    def connections():
        return current().mcp.list()

    @router.post('/api/mcp/connections')
    async def connection_create(request: Request):
        return current().mcp.save(await body(request))

    @router.post('/api/mcp/import-preview')
    async def import_mcp(request: Request):
        data=await body(request)
        return current().mcp.import_preview(data.get('config',{}))

    @router.post('/api/mcp/checks')
    async def connection_checks(request: Request,idempotency_key: str=Header(default='')):
        data = await body(request)
        identities = data.get('connection_ids',[])
        if not isinstance(identities,list) or not 1<=len(identities)<=50 or any(not isinstance(i,str) for i in identities):
            raise CapabilityError('CHECK_SELECTION_INVALID','请选择1至50项连接')
        instance = current()
        for identity in identities:
            instance.mcp.get(identity)
        def check():
            results = []
            for identity in dict.fromkeys(identities):
                try:
                    results.append(instance.mcp.discover(identity))
                except CapabilityError as exc:
                    row = instance.mcp.public(instance.mcp.get(identity))
                    row['health'] = {'status':'blocked','error_code':exc.code,'error':exc.message,'next_action':exc.next_action}
                    results.append(row)
            return results
        return instance.operations.submit(check,title='检查MCP连接',key=idempotency_key,request={'connection_ids':identities})

    @router.patch('/api/mcp/connections/{identity}')
    async def connection_patch(identity: str,request: Request):
        return current().mcp.save(await body(request),identity)

    @router.delete('/api/mcp/connections/{identity}')
    def connection_retire(identity: str,expected_revision: int=Query(...,ge=0)):
        return current().mcp.retire(identity,expected_revision)

    @router.post('/api/mcp/connections/{identity}/discover')
    def discover(identity: str,idempotency_key: str=Header(default='')):
        instance=current()
        instance.mcp.get(identity)
        return instance.operations.submit(lambda:instance.mcp.discover(identity),title='检测并发现MCP工具',key=idempotency_key,
                                          request={'connection_id':identity,'revision':instance.mcp.get(identity)['revision']})

    @router.post('/api/mcp/connections/{identity}/tool-policy')
    async def tool_policy(identity: str,request: Request):
        return current().mcp.tool_policy(identity,await body(request))

    @router.get('/api/skills')
    def skills():
        return current().skills.list()

    @router.post('/api/skills/import-preview')
    async def preview_skill(request: Request):
        data=await body(request)
        return current().skills.preview(str(data.get('source_path','')))

    @router.post('/api/skills/import-commit')
    async def commit_skill(request: Request):
        data=await body(request)
        return current().skills.commit(str(data.get('preview_id','')),str(data.get('hash','')),allow_new_version=bool(data.get('allow_new_version')))

    @router.get('/api/skills/{identity}/resources')
    def resource(identity: str,path: str):
        return current().skills.resource(identity,path)

    @router.get('/api/skills/{identity}')
    def skill(identity: str):
        return current().skills.get(identity)

    @router.patch('/api/skills/{identity}')
    async def skill_patch(identity: str,request: Request):
        return current().skills.patch(identity,await body(request))

    @router.post('/api/skills/{identity}/copy')
    async def copy_skill(identity: str, request: Request):
        return current().skills.copy(identity, await body(request))

    @router.delete('/api/skills/{identity}')
    def skill_retire(identity: str,expected_revision: int=Query(...,ge=0)):
        return current().skills.retire(identity,expected_revision)

    @router.get('/api/memory/items')
    def memories(query: str='',scope: str='',state: str='',limit: int=Query(50,ge=1,le=200),cursor: str=''):
        return current().memory.list(query=query,scope=scope,state=state,limit=limit,cursor=cursor)

    @router.post('/api/memory/items')
    async def create_memory(request: Request):
        return current().memory.save(await body(request))

    @router.get('/api/memory/items/{identity}')
    def memory(identity: str):
        return current().memory.get(identity)

    @router.patch('/api/memory/items/{identity}')
    async def patch_memory(identity: str,request: Request):
        return current().memory.save(await body(request),identity)

    @router.post('/api/memory/items/{identity}/forget')
    async def forget(identity: str,request: Request):
        data=await body(request)
        return current().memory.forget(identity,expected_revision=int(data['expected_revision']))

    @router.get('/api/knowledge/documents')
    def documents(namespace: str='local',query: str='',type: str='',purpose: str='',index_status: str='',limit: int=Query(50,ge=1,le=200),cursor: str=''):
        return current().knowledge.list(namespace=namespace,query=query,type=type,purpose=purpose,index_status=index_status,limit=limit,cursor=cursor)

    @router.get('/api/knowledge/documents/{identity}')
    def document(identity: str,namespace: str='local'):
        return current().knowledge.detail(identity,namespace)

    @router.patch('/api/knowledge/documents/{identity}/policy')
    async def document_policy(identity: str,request: Request):
        return current().knowledge.policy(identity,await body(request))

    @router.post('/api/knowledge/search')
    async def search_knowledge(request: Request):
        return current().knowledge.search(await body(request))

    @router.post('/api/knowledge/index-jobs')
    async def index(request: Request,idempotency_key: str=Header(default='')):
        from src.knowledge.embeddings import index_pending_documents
        data=await body(request)
        instance=current()
        namespace=str(data.get('namespace','local'))
        return instance.operations.submit(lambda:index_pending_documents(instance.store.knowledge_store,account_namespace=namespace),title='增量索引',key=idempotency_key,request={'namespace':namespace})

    @router.get('/api/conversations/{conversation_id}/plans/{plan_id}/capabilities')
    def plan_capabilities(conversation_id: str,plan_id: str):
        from apps.web_service import valid_id,valid_conversation_id
        return current().plan_capabilities(valid_conversation_id(conversation_id),valid_id(plan_id))

    @router.get('/api/conversations/{conversation_id}/context')
    def context_overview(conversation_id: str):
        return current().context.overview(conversation_id)

    @router.put('/api/conversations/{conversation_id}/context-policy')
    async def context_policy(conversation_id: str,request: Request):
        return current().context.save_policy(conversation_id,await body(request))

    @router.post('/api/conversations/{conversation_id}/compact')
    async def context_compact(conversation_id: str,request: Request,idempotency_key: str=Header(default='')):
        data=await body(request)
        if data:
            raise CapabilityError('CONTEXT_COMPACTION_INVALID','压缩接口不接收额外字段',status=422)
        return current().context.compact(conversation_id,idempotency_key)

    @router.put('/api/conversations/{conversation_id}/plans/{plan_id}/capabilities')
    async def save_plan_capabilities(conversation_id: str,plan_id: str,request: Request):
        from apps.web_service import valid_id,valid_conversation_id
        return current().save_plan(valid_conversation_id(conversation_id),valid_id(plan_id),await body(request))

    @router.get('/api/runs/{identity}/capabilities')
    def run_capabilities(identity: str):
        from apps.web_service import valid_id
        workbench=service()
        run_id=workbench.agent_checkpoint_id(valid_id(identity))
        instance=current()
        snapshot=instance.store.snapshot(run_id)
        if not snapshot:
            return {'status':'not_recorded','message':'该版本未采集能力使用记录','tools':[],'calls':[]}
        differences=[]
        for resource in snapshot['tools'].values():
            latest=instance.store.get(resource['id'])
            if latest and latest['revision']!=resource.get('revision',0):
                differences.append({'resource_id':resource['id'],'frozen_revision':resource.get('revision',0),
                                    'current_revision':latest['revision'],'revoked':bool(latest.get('revoked_at'))})
        return safe({**snapshot,'tools':list(snapshot['tools'].values()),'frozen':True,'calls':instance.store.calls(run_id=run_id)['rows'],'resume_diff':differences})

    @router.get('/api/capability-calls')
    def calls(run_id: str='',resource_id: str='',status: str='',origin: str='',limit: int=Query(50,ge=1,le=200),cursor: str=''):
        return current().store.calls(run_id=run_id,resource_id=resource_id,status=status,origin=origin,limit=limit,cursor=cursor)

    @router.get('/api/resource-changes')
    def changes(resource_id: str='',limit: int=Query(50,ge=1,le=200),cursor: str=''):
        return current().store.changes(resource_id=resource_id,limit=limit,cursor=cursor)

    return router
