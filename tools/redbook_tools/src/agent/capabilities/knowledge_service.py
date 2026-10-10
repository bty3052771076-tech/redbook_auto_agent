from __future__ import annotations

import time
from psycopg.types.json import Jsonb

from .models import CapabilityError


class KnowledgeManagement:
    def __init__(self, knowledge_store, capability_store):
        self.store,self.capability_store=knowledge_store,capability_store

    def list(self, *, namespace: str='local', query: str='', type: str='', purpose: str='',
             index_status: str='', limit: int=50, cursor: str='') -> dict:
        from src.knowledge.store import _MODEL_ID,_CHUNK_VERSION
        with self.store.connection() as conn:
            rows=conn.execute('''WITH matches AS (SELECT d.id,d.record_id,d.record_type,d.account_namespace,d.title,d.source_url,d.source_published_at,
                d.allowed_purposes,d.updated_at,d.content_hash,
                CASE WHEN NOT ((d.title||d.body) ~ '[^[:space:]]') THEN 'skipped_empty' WHEN EXISTS(
                SELECT 1 FROM knowledge.chunks c WHERE c.document_id=d.id AND c.document_version=d.content_hash
                AND c.index_status='ready' AND c.model_id=%s AND c.chunk_version=%s) THEN 'ready' ELSE 'pending' END AS index_status
                FROM knowledge.documents d WHERE d.account_namespace=%s AND (%s='' OR d.record_type=%s)
                AND (%s='' OR lower(d.title||d.body) LIKE ('%%'||lower(%s)||'%%'))
                AND (%s='' OR (','||d.allowed_purposes||',') LIKE ('%%,'||%s||',%%'))
                AND d.id>%s) SELECT * FROM matches WHERE (%s='' OR index_status=%s)
                ORDER BY id LIMIT %s''',
                (_MODEL_ID,_CHUNK_VERSION,namespace,type,type,query,query,purpose,purpose,int(cursor or 0),
                 index_status,index_status,min(200,max(1,limit))+1)).fetchall()
            namespaces=[r['account_namespace'] for r in conn.execute('SELECT DISTINCT account_namespace FROM knowledge.documents ORDER BY account_namespace').fetchall()]
        visible=rows[:limit]
        return {'rows':[{**r,'id':r['record_id'],'document_id':r['id'],'updated_at':str(r['updated_at']),
                         'namespace':r['account_namespace'],'type':r['record_type'],
                         'published_at':r['source_published_at'],'purposes':r['allowed_purposes'].split(','),
                         'namespace_label':'历史未绑定' if namespace=='local' else namespace,
                         'allowed_purposes':r['allowed_purposes'].split(',')} for r in visible],
                'next_cursor':str(visible[-1]['id']) if len(rows)>limit and visible else None,
                'namespaces':namespaces,'index_progress':self.store.index_progress(account_namespace=namespace)}

    def detail(self, identity: str, namespace: str='local') -> dict:
        document=self.store.get(identity,account_namespace=namespace)
        with self.store.connection() as conn:
            row=conn.execute('SELECT id FROM knowledge.documents WHERE account_namespace=%s AND record_id=%s',(namespace,identity)).fetchone()
            chunks=conn.execute('SELECT chunk_id,document_version,chunk_index,content,char_start,char_end,index_status FROM knowledge.chunks WHERE document_id=%s ORDER BY chunk_index LIMIT 200',(row['id'],)).fetchall()
            versions=conn.execute('SELECT content_hash,snapshot,captured_at FROM knowledge.document_versions WHERE document_id=%s ORDER BY captured_at DESC LIMIT 200',(row['id'],)).fetchall()
            policy=conn.execute('SELECT revision,excluded_purposes,annotation,source_ref,updated_at FROM knowledge.document_policies WHERE account_namespace=%s AND record_id=%s',(namespace,identity)).fetchone()
        return {**document,'id':identity,'namespace':namespace,'chunks':[dict(r) for r in chunks],'versions':[dict(r) for r in versions],
                'policy':dict(policy) if policy else {'revision':0,'excluded_purposes':[],'annotation':'','source_ref':''}}

    def policy(self, identity: str, data: dict) -> dict:
        namespace=data.get('namespace','local')
        self.store.get(identity,account_namespace=namespace)
        excluded=data.get('excluded_purposes',[])
        if not isinstance(excluded,list) or any(p not in {'evidence','duplicate_reference','style_reference','operations'} for p in excluded):
            raise CapabilityError('KNOWLEDGE_PURPOSE_INVALID','检索用途无效')
        annotation=str(data.get('annotation',''))
        if len(annotation)>4000:
            raise CapabilityError('KNOWLEDGE_ANNOTATION_INVALID','注释最多4000字')
        with self.store.connection() as conn,conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(namespace+':'+identity,))
            old=conn.execute('SELECT revision FROM knowledge.document_policies WHERE account_namespace=%s AND record_id=%s FOR UPDATE',(namespace,identity)).fetchone()
            revision=old['revision'] if old else 0
            if revision!=data.get('expected_revision'):
                raise CapabilityError('REVISION_CONFLICT','资料策略已改变，请刷新',status=409,revision=revision)
            conn.execute('''INSERT INTO knowledge.document_policies(account_namespace,record_id,revision,excluded_purposes,annotation,source_ref)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(account_namespace,record_id) DO UPDATE SET
                revision=excluded.revision,excluded_purposes=excluded.excluded_purposes,annotation=excluded.annotation,
                source_ref=excluded.source_ref,updated_at=now()''',
                (namespace,identity,revision+1,list(set(excluded)),annotation,str(data.get('source_ref','manual:local_user'))))
            conn.execute('INSERT INTO agent.resource_changes(namespace,resource_id,revision,payload) VALUES (%s,%s,%s,%s)',
                         (self.capability_store.namespace,'knowledge:'+namespace+':'+identity,revision+1,Jsonb({'actor':'local_user','after':data})))
        return self.detail(identity,namespace)['policy']

    def search(self, data: dict) -> dict:
        from src.knowledge.store import _MODEL_ID
        query=str(data.get('query','')).strip()
        purpose=data.get('purpose','evidence')
        if not query or len(query)>2000 or purpose not in {'evidence','duplicate_reference','style_reference','operations'}:
            raise CapabilityError('KNOWLEDGE_QUERY_INVALID','请输入查询及有效检索用途')
        start=time.monotonic()
        rows=self.store.search(query,purpose=purpose,limit=min(100,max(1,int(data.get('limit',20)))),account_namespace=data.get('namespace','local'))
        return {'rows':rows,'elapsed_ms':(time.monotonic()-start)*1000,'ranking':'语义/词法 RRF 检索排序分数',
                'embedding_model':_MODEL_ID,'embedding_dimensions':384}
