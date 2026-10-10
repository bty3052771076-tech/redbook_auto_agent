from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
from uuid import uuid4

from .capabilities.models import CapabilityError


class MemoryService:
    def __init__(self, store):
        self.store=store

    def list(self, *, query: str='', scope: str='', state: str='', limit: int=50, cursor: str='') -> dict:
        rows=[r for r in self.store.resources('memory') if (not query or query.casefold() in r.get('content','').casefold())
              and (not scope or r.get('scope')==scope) and (not state or ('forgotten' if r.get('forgotten') else 'active' if r.get('active') else 'inactive')==state)]
        rows.sort(key=lambda row:row['id'])
        if cursor:
            rows=[r for r in rows if r['id']>cursor]
        return {'rows':rows[:limit],'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None}

    def get(self, identity: str) -> dict:
        row=self.store.get(identity)
        if not row or row.get('kind')!='memory':
            raise CapabilityError('MEMORY_NOT_FOUND','偏好不存在',status=404,resource_id=identity)
        return {**row,'versions':self.store.versions(identity)}

    def save(self, data: dict, identity: str='') -> dict:
        current=self.get(identity) if identity else {}
        value={**current,**{k:v for k,v in data.items() if k in {'content','key','scope','scope_id','origin','source_ref','active','expires_at'}}}
        content=str(value.get('content','')).strip()
        if not content or len(content)>2000:
            raise CapabilityError('MEMORY_CONTENT_INVALID','偏好需要1至2000字的明确内容')
        scope=value.get('scope','workspace')
        if scope not in {'workspace','account','column','conversation'} or (scope!='workspace' and not value.get('scope_id')):
            raise CapabilityError('MEMORY_SCOPE_INVALID','请选择作用范围并填写关联对象')
        origin=value.get('origin','manual')
        if origin not in {'manual','inferred','confirmed'}:
            raise CapabilityError('MEMORY_ORIGIN_INVALID','来源须为人工、推断或已确认')
        expiry=value.get('expires_at')
        if expiry:
            try:
                datetime.fromisoformat(expiry.replace('Z','+00:00'))
            except (ValueError,TypeError):
                raise CapabilityError('MEMORY_EXPIRY_INVALID','有效期格式无效') from None
        if current.get('forgotten'):
            raise CapabilityError('MEMORY_FORGOTTEN','已遗忘偏好不可原地启用，请新建偏好',status=409)
        value.update(content=content,scope=scope,origin=origin,active=bool(value.get('active',origin!='inferred')),
                     source_ref=value.get('source_ref') or 'manual:local_user',forgotten=False)
        if origin=='inferred' and value['active']:
            raise CapabilityError('MEMORY_CONFIRM_REQUIRED','推断偏好需要确认后启用')
        return self.store.put(identity or 'memory_'+uuid4().hex,'memory',value,
                              expected_revision=int(data.get('expected_revision',0)),reason=data.get('reason','保存长期偏好'))

    def select(self, *, account: str='', column: str='', conversation: str='') -> list[dict]:
        scope_ids={'workspace':'','account':account,'column':column,'conversation':conversation}
        priority={'workspace':0,'account':1,'column':2,'conversation':3}
        now=datetime.now(timezone.utc)
        rows=[]
        for row in self.store.resources('memory'):
            if not row.get('active') or row.get('forgotten') or row.get('origin')=='inferred':
                continue
            scope=row.get('scope','workspace')
            if scope!='workspace' and (not scope_ids.get(scope) or row.get('scope_id')!=scope_ids[scope]):
                continue
            if row.get('expires_at'):
                expiry=datetime.fromisoformat(row['expires_at'].replace('Z','+00:00'))
                if (expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc))<=now:
                    continue
            rows.append(row)
        chosen={}
        for row in sorted(rows,key=lambda r:(priority[r['scope']],r.get('updated_at',''),r['revision'])):
            key=row.get('key') or row['id']
            previous=chosen.get(key)
            overridden = []
            if previous:
                overridden = [{name:previous.get(name) for name in
                    ('id','revision','content','source_ref','scope','scope_id','origin','updated_at')} |
                    {'reason':'更具体的作用范围优先' if priority[row['scope']] > priority[previous['scope']]
                     else '同作用范围采用最新明确修正', 'replaced_by':row['id']}] + previous.get('overridden_preferences',[])
            chosen[key]={**row,'overridden_ids':([previous['id']]+previous.get('overridden_ids',[])) if previous else [],
                         'overridden_preferences':overridden}
        if len(chosen) > 100:
            raise CapabilityError('MEMORY_SELECTION_TOO_LARGE','当前任务匹配超过100项偏好，请停用无关项或缩小作用范围')
        return list(chosen.values())

    def forget(self, identity: str, *, expected_revision: int) -> dict:
        row=self.get(identity)
        with self.store.knowledge_store.connection() as conn,conn.transaction():
            saved=self.store.put(identity,'memory',{**row,'active':False,'forgotten':True},expected_revision=expected_revision,
                                 reason='忘记偏好并使派生上下文失效',connection=conn)
            # Unknown legacy provenance is conservative only within the affected scope.
            from psycopg.types.json import Jsonb
            count=conn.execute('''UPDATE agent.compaction_snapshots s SET status='invalidated'
                FROM agent.conversations c WHERE c.conversation_id=s.conversation_id
                AND c.account_namespace=%s AND s.status='active' AND (
                    s.task_state->'memory_refs' ? %s
                    OR s.task_state->'memory_refs' @> %s
                    OR (NOT (s.task_state ? 'memory_refs') AND (
                        %s='workspace'
                        OR (%s='conversation' AND c.conversation_id=%s)
                        OR (%s='account' AND (c.payload->>'account_id'=%s OR s.task_state->>'account_id'=%s))
                        OR (%s='column' AND (c.payload->>'column'=%s OR s.task_state->>'column'=%s
                            OR EXISTS (SELECT 1 FROM jsonb_array_elements(
                                CASE WHEN jsonb_typeof(c.payload->'plans')='array' THEN c.payload->'plans' ELSE '[]' END
                            ) p, LATERAL jsonb_array_elements(
                                CASE WHEN jsonb_typeof(p->'jobs')='array' THEN p->'jobs' ELSE '[]' END
                            ) j WHERE j->>'kind'=%s)))
                    )))''',
                (self.store.namespace,identity,Jsonb([{'id':identity}]),row['scope'],row['scope'],row.get('scope_id',''),
                 row['scope'],row.get('scope_id',''),row.get('scope_id',''),row['scope'],row.get('scope_id',''),
                 row.get('scope_id',''),row.get('scope_id',''))).rowcount
        return {**saved,'status':'forgotten','invalidated_snapshots':count}

    def _forgotten(self, *, account='', columns=(), conversation=''):
        scopes = {'workspace': {''}, 'account': {account} if account else set(),
                  'column': set(columns), 'conversation': {conversation} if conversation else set()}
        return [row for row in self.store.resources('memory') if row.get('forgotten')
                and (row.get('scope') == 'workspace' or row.get('scope_id') in scopes.get(row.get('scope'), set()))]

    def _forgotten_contents(self, rows):
        if not rows:
            return []
        with self.store.knowledge_store.connection() as conn:
            versions = conn.execute('''SELECT DISTINCT payload->>'content' AS content FROM agent.resource_versions
                WHERE namespace=%s AND resource_id=ANY(%s)''',
                (self.store.namespace, [row['id'] for row in rows])).fetchall()
        return sorted({row['content'] for row in rows} | {row['content'] for row in versions if row['content']},
                      key=len, reverse=True)

    def filter_forgotten(self, text: str, *, account='', column='', conversation='') -> str:
        rows = self._forgotten(account=account, columns=[column] if column else [], conversation=conversation)
        for content in self._forgotten_contents(rows):
            text = text.replace(content, '[已忘记的偏好]')
        return text

    def filter_context(self, context: dict, *, account='', columns=(), conversation='') -> dict:
        rows = self._forgotten(account=account, columns=columns, conversation=conversation)
        contents = self._forgotten_contents(rows)
        source_ids = {row.get('source_ref', '').removeprefix('message:') for row in rows}
        value = deepcopy(context)

        def clean(text):
            for content in contents:
                text = text.replace(content, '[已忘记的偏好]')
            return text

        def visit(item):
            if isinstance(item, list):
                return [visit(child) for child in item]
            if not isinstance(item, dict):
                return item
            for key, child in item.items():
                if key in {'summary', 'prior_summary', 'content'} and isinstance(child, str):
                    item[key] = '[已忘记的偏好来源]' if key == 'content' and item.get('id') in source_ids else clean(child)
                elif key in {'constraints', 'prior_constraints'} and isinstance(child, list):
                    item[key] = [text for text in child if not isinstance(text, str) or clean(text) == text]
                elif isinstance(child, (dict, list)):
                    item[key] = visit(child)
            return item

        return visit(value)
