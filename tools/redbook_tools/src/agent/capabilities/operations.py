from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import os
from pathlib import Path
from uuid import uuid4

from psycopg.types.json import Jsonb

from .models import CapabilityError, safe, digest
from src.model_platforms.security import file_lock, PlatformError


class OperationManager:
    def __init__(self, store, *, root=None):
        self.store=store
        self.lease_directory=Path(root or os.getenv('REDBOOK_RUNTIME_ROOT') or Path.cwd())/'data/agent/locks/capabilities'
        self.executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='capability-diagnostic')
        self.lock=threading.RLock()
        self.futures={}
        with store.knowledge_store.connection('migration') as conn,conn.transaction():
            conn.execute('''CREATE TABLE IF NOT EXISTS agent.capability_operations(
                id text PRIMARY KEY,namespace text NOT NULL,status text NOT NULL,payload jsonb NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now())''')
            conn.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON agent.capability_operations TO redbook_app')

    def submit(self, callback, *, title: str, key: str='', request=None) -> dict:
        if not isinstance(key,str) or len(key)>256:
            raise CapabilityError('OPERATION_KEY_INVALID','请求键最多256字符',status=422)
        identity=digest([self.store.namespace,key]) if key else uuid4().hex
        request_hash=digest([title,request])
        recovery_context={field:request[field] for field in ('origin','trial_preview_id','resource_id')
                          if isinstance(request,dict) and field in request}
        lease=file_lock(self.lease_directory/(identity+'.lock'),timeout=0)
        try:
            lease.__enter__()
        except PlatformError:
            try:
                result=self.get(identity)
            except CapabilityError as exc:
                if exc.code!='OPERATION_NOT_FOUND':raise
                raise CapabilityError('OPERATION_BUSY','检测请求正在登记，请稍后用同一请求键重试',
                                      status=409,retryable=True) from None
            if result.get('request_hash')!=request_hash:
                raise CapabilityError('OPERATION_REQUEST_CONFLICT','同一请求键不能用于不同检测参数',status=409)
            return result
        handed_off=False
        try:
            with self.store.knowledge_store.connection() as conn,conn.transaction():
                inserted=conn.execute('INSERT INTO agent.capability_operations(id,namespace,status,payload) VALUES (%s,%s,\'queued\',%s) ON CONFLICT(id) DO NOTHING RETURNING id',
                                      (identity,self.store.namespace,Jsonb({'title':title,'stages':[],'results':[],
                                                                         'request_hash':request_hash,'lease_managed':True,
                                                                         'recovery_context':recovery_context}))).fetchone()
                if not inserted:
                    existing=conn.execute('SELECT payload FROM agent.capability_operations WHERE namespace=%s AND id=%s',
                                          (self.store.namespace,identity)).fetchone()
                    if not existing or existing['payload'].get('request_hash')!=request_hash:
                        raise CapabilityError('OPERATION_REQUEST_CONFLICT','同一请求键不能用于不同检测参数',status=409)
            if inserted:
                future=self.executor.submit(self._run,identity,callback,lease,recovery_context)
                self.futures[identity]=future
                def cancelled(completed):
                    if not completed.cancelled():return
                    try:
                        self._update(identity,'failed',{'error':{'code':'OPERATION_CANCELLED',
                            'message':'服务关闭，未开始的检测已取消','retryable':True,
                            'next_action':'需要时重新发起检测，原数据保留'}})
                    finally:lease.__exit__(None,None,None)
                future.add_done_callback(cancelled)
                handed_off=True
        finally:
            if not handed_off:lease.__exit__(None,None,None)
        return self.get(identity)

    def _update(self,identity,status,data):
        with self.store.knowledge_store.connection() as conn,conn.transaction():
            conn.execute('UPDATE agent.capability_operations SET status=%s,payload=payload||%s,updated_at=now() WHERE namespace=%s AND id=%s',
                         (status,Jsonb(safe(data)),self.store.namespace,identity))

    def _run(self,identity,callback,lease,recovery_context=None):
        try:
            self._update(identity,'running',{'stages':[{'name':'检测','status':'running'}]})
            result=callback()
            status = 'degraded' if isinstance(result,list) and any(
                isinstance(row,dict) and row.get('health',{}).get('status') in {'blocked','degraded'} for row in result) else 'succeeded'
            self._update(identity,status,{'results':result,'stages':[{'name':'检测','status':status}]})
        except Exception as exc:
            error=exc.public() if isinstance(exc,CapabilityError) else {'code':'CAPABILITY_CHECK_FAILED','message':str(safe(str(exc))),
                'next_action':'检查连接或配置后再次检测','retryable':True}
            uncertain='UNCERTAIN' in error['code']
            if not isinstance(exc,CapabilityError) and (recovery_context or {}).get('trial_preview_id'):
                uncertain=True
                error={'code':'TRIAL_RESULT_UNCERTAIN','message':'试运行记录未完成，原请求结果待核对',
                       'retryable':False,'next_action':'查看原试运行及其运行记录；不要直接重新生成或上传'}
            status='uncertain' if uncertain else 'failed'
            self._update(identity,status,{'error':error,'stages':[{'name':'检测','status':status}]})
        finally:
            lease.__exit__(None,None,None)

    def get(self,identity):
        with self.store.knowledge_store.connection() as conn:
            row=conn.execute('SELECT status,payload,created_at,updated_at FROM agent.capability_operations WHERE namespace=%s AND id=%s',
                             (self.store.namespace,identity)).fetchone()
        if not row:
            raise CapabilityError('OPERATION_NOT_FOUND','检测任务不存在',status=404)
        if row['status'] in {'queued','running'} and row['payload'].get('lease_managed'):
            try:
                with file_lock(self.lease_directory/(identity+'.lock'),timeout=0):
                    error={'code':'OPERATION_INTERRUPTED','message':'原检测进程已退出，未自动重复执行',
                           'retryable':True,'next_action':'重新发起检测；原任务及数据保留'}
                    with self.store.knowledge_store.connection() as conn,conn.transaction():
                        context=row['payload'].get('recovery_context') or {}
                        preview_id=context.get('trial_preview_id')
                        status='uncertain' if preview_id else 'failed'
                        data={}
                        if preview_id:
                            call=conn.execute("SELECT id,run_id,payload FROM agent.capability_calls WHERE namespace=%s AND payload->>'operation_key'=%s AND payload->>'origin'='diagnostic' ORDER BY started_at DESC LIMIT 1",
                                              (self.store.namespace,preview_id)).fetchone()
                            run_id=(call['run_id'] or call['payload'].get('submitted_run_id','')) if call else ''
                            error={'code':'TRIAL_RESULT_UNCERTAIN','message':'原试运行进程已退出，提交结果待核对，未重复执行',
                                   'retryable':False,'next_action':('先核对原运行编号：'+run_id if run_id else '查看原试运行预览：'+preview_id)+'；不要直接重新生成或上传'}
                            data={'run_id':run_id,'trial_preview_id':preview_id}
                            if call:
                                data['call_id']=call['id']
                                conn.execute("UPDATE agent.capability_calls SET status='uncertain',payload=payload||%s,ended_at=now() WHERE namespace=%s AND id=%s AND status IN ('running','queued')",
                                             (Jsonb({'recovery_error':error}),self.store.namespace,call['id']))
                        data.update(error=error,stages=[{'name':'检测','status':status}])
                        conn.execute("UPDATE agent.capability_operations SET status=%s,payload=payload||%s,updated_at=now() WHERE namespace=%s AND id=%s AND status IN ('queued','running')",
                                     (status,Jsonb(data),self.store.namespace,identity))
                        row=conn.execute('SELECT status,payload,created_at,updated_at FROM agent.capability_operations WHERE namespace=%s AND id=%s',
                                         (self.store.namespace,identity)).fetchone()
            except PlatformError:
                pass
        return {**row['payload'],'operation_id':identity,'status':row['status'],'created_at':str(row['created_at']),'updated_at':str(row['updated_at'])}

    def close(self):
        self.executor.shutdown(wait=True,cancel_futures=True)
