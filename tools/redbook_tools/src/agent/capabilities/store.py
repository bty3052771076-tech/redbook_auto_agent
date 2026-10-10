from __future__ import annotations

import json
from contextlib import nullcontext
from typing import Any
from uuid import uuid4
import hashlib
import re

from psycopg.types.json import Jsonb

from .models import CapabilityError, digest, safe


class CapabilityStore:
    def __init__(self, knowledge_store=None, *, namespace: str = 'local'):
        from src.knowledge.store import KnowledgeStore
        self.knowledge_store = knowledge_store or KnowledgeStore.from_env()
        self.namespace = namespace

    def ensure_schema(self) -> None:
        with self.knowledge_store.connection('migration') as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('redbook-capability-schema'))")
            conn.execute('CREATE SCHEMA IF NOT EXISTS agent')
            conn.execute('''CREATE TABLE IF NOT EXISTS agent.capability_settings (
                namespace text NOT NULL, resource_id text NOT NULL, kind text NOT NULL,
                revision integer NOT NULL, payload jsonb NOT NULL, revoked_at timestamptz,
                updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(namespace,resource_id))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS agent.resource_versions (
                namespace text NOT NULL, resource_id text NOT NULL, revision integer NOT NULL,
                payload jsonb NOT NULL, hash text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY(namespace,resource_id,revision))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS agent.capability_snapshots (
                namespace text NOT NULL, run_id text NOT NULL, snapshot_id text NOT NULL,
                payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY(namespace,run_id), UNIQUE(snapshot_id))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS agent.capability_calls (
                id text PRIMARY KEY, namespace text NOT NULL, run_id text NOT NULL,
                resource_id text NOT NULL, status text NOT NULL, payload jsonb NOT NULL,
                started_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS agent.resource_changes (
                id bigserial PRIMARY KEY, namespace text NOT NULL, resource_id text NOT NULL,
                revision integer NOT NULL, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now())''')
            conn.execute('CREATE INDEX IF NOT EXISTS capability_calls_run_idx ON agent.capability_calls(namespace,run_id,started_at DESC)')
            conn.execute('CREATE INDEX IF NOT EXISTS capability_calls_resource_idx ON agent.capability_calls(namespace,resource_id,started_at DESC)')
            conn.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA agent TO redbook_app')
            conn.execute('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA agent TO redbook_app')

    def get(self, resource_id: str) -> dict | None:
        with self.knowledge_store.connection() as conn:
            row = conn.execute('SELECT payload,revision,revoked_at,updated_at FROM agent.capability_settings WHERE namespace=%s AND resource_id=%s',
                               (self.namespace, resource_id)).fetchone()
        if row is None:
            return None
        return {**row['payload'], 'id': resource_id, 'revision': row['revision'],
                'revoked_at': str(row['revoked_at']) if row['revoked_at'] else None, 'updated_at': str(row['updated_at'])}

    def resources(self, kind: str = '') -> list[dict]:
        with self.knowledge_store.connection() as conn:
            rows = conn.execute('SELECT resource_id,payload,revision,revoked_at,updated_at FROM agent.capability_settings WHERE namespace=%s AND (%s=\'\' OR kind=%s) ORDER BY resource_id',
                                (self.namespace, kind, kind)).fetchall()
        return [{**r['payload'], 'id': r['resource_id'], 'revision': r['revision'],
                 'revoked_at': str(r['revoked_at']) if r['revoked_at'] else None, 'updated_at': str(r['updated_at'])} for r in rows]

    def put(self, resource_id: str, kind: str, payload: dict, *, expected_revision: int,
            reason: str = '', actor: str = 'local_user', clear_revocation: bool = False,
            connection=None, revoke: bool = False) -> dict:
        with (nullcontext(connection) if connection is not None else self.knowledge_store.connection()) as conn, conn.transaction():
            if kind == 'memory':
                conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (self.namespace + ':memory-context',))
            conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (self.namespace + ':' + resource_id,))
            old = conn.execute('SELECT revision,payload FROM agent.capability_settings WHERE namespace=%s AND resource_id=%s FOR UPDATE',
                               (self.namespace, resource_id)).fetchone()
            actual = old['revision'] if old else 0
            if actual != expected_revision:
                raise CapabilityError('REVISION_CONFLICT', '配置已变化，请刷新后保存', status=409, resource_id=resource_id, revision=actual)
            revision = actual + 1
            canonical = {key: val for key, val in payload.items() if key not in {'revision', 'updated_at', 'revoked_at', 'versions'}}
            canonical.update(id=resource_id, kind=kind)
            canonical = json.loads(json.dumps(canonical, ensure_ascii=False, default=str))
            conn.execute('''INSERT INTO agent.capability_settings(namespace,resource_id,kind,revision,payload)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT(namespace,resource_id) DO UPDATE SET
                kind=excluded.kind,revision=excluded.revision,payload=excluded.payload,updated_at=now(),
                revoked_at=CASE WHEN %s THEN now() WHEN %s THEN NULL ELSE agent.capability_settings.revoked_at END''',
                         (self.namespace, resource_id, kind, revision, Jsonb(canonical), revoke, clear_revocation))
            conn.execute('INSERT INTO agent.resource_versions(namespace,resource_id,revision,payload,hash) VALUES (%s,%s,%s,%s,%s)',
                         (self.namespace, resource_id, revision, Jsonb(canonical), digest(canonical)))
            conn.execute('INSERT INTO agent.resource_changes(namespace,resource_id,revision,payload) VALUES (%s,%s,%s,%s)',
                         (self.namespace, resource_id, revision, Jsonb(safe({'actor': actor, 'reason': reason,
                            'before': old['payload'] if old else None, 'after': canonical}))))
            row = conn.execute('SELECT payload,revision,revoked_at,updated_at FROM agent.capability_settings WHERE namespace=%s AND resource_id=%s',
                               (self.namespace, resource_id)).fetchone()
            result = {**row['payload'], 'id': resource_id, 'revision': row['revision'],
                      'revoked_at': str(row['revoked_at']) if row['revoked_at'] else None, 'updated_at': str(row['updated_at'])}
        return result

    def version(self, resource_id: str, revision: int) -> dict:
        with self.knowledge_store.connection() as conn:
            row = conn.execute('SELECT payload,hash FROM agent.resource_versions WHERE namespace=%s AND resource_id=%s AND revision=%s',
                               (self.namespace, resource_id, revision)).fetchone()
        if not row:
            raise CapabilityError('RESOURCE_VERSION_MISSING', '冻结的能力版本不存在', status=409, resource_id=resource_id)
        return {**row['payload'], 'revision': revision, 'hash': row['hash']}

    def versions(self, resource_id: str) -> list[dict]:
        with self.knowledge_store.connection() as conn:
            rows = conn.execute('SELECT revision,hash,created_at FROM agent.resource_versions WHERE namespace=%s AND resource_id=%s ORDER BY revision DESC LIMIT 200',
                                (self.namespace, resource_id)).fetchall()
        return [dict(r) for r in rows]

    def revoke(self, resource_id: str, *, expected_revision: int, reason: str = '') -> dict:
        current = self.get(resource_id)
        if not current:
            raise CapabilityError('RESOURCE_NOT_FOUND', '能力尚未保存配置', status=404, resource_id=resource_id)
        updated = self.put(resource_id, current['kind'], {**current, 'enabled': False},
                           expected_revision=expected_revision, reason=reason or '停止后续调用', revoke=True)
        return {**updated, 'affected_runs': self.active_references(resource_id)}

    def active_references(self, resource_id: str = '') -> list[dict]:
        with self.knowledge_store.connection() as conn:
            rows = conn.execute('''SELECT run_id,snapshot_id,created_at FROM agent.capability_snapshots
                WHERE namespace=%s AND (%s='' OR payload->'tools' ? %s) ORDER BY created_at DESC''',
                                (self.namespace, resource_id, resource_id)).fetchall()
            locks = conn.execute("""SELECT classid::bigint AS high,objid::bigint AS low FROM pg_locks
                WHERE locktype='advisory' AND granted AND objsubid=1
                AND database=(SELECT oid FROM pg_database WHERE datname=current_database())""").fetchall()
            held = {(row['high'],row['low']) for row in locks}
            active = []
            for row in rows:
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',row['run_id']):
                    continue
                # This is the exact session lock used by AgentArtifactStore.lease.
                key = int.from_bytes(hashlib.sha256(('redbook:agent-run:'+row['run_id']).encode('ascii')).digest()[:8],'big')
                if (key >> 32,key & 0xffffffff) in held:
                    active.append(dict(row))
            if not active:
                return []
            run_ids=[row['run_id'] for row in active]
            snapshots={row['run_id']:row['tools'] for row in conn.execute(
                "SELECT run_id,payload->'tools' AS tools FROM agent.capability_snapshots WHERE namespace=%s AND run_id=ANY(%s)",
                (self.namespace,run_ids)).fetchall()}
            in_flight={(row['run_id'],row['resource_id']):row['count'] for row in conn.execute(
                "SELECT run_id,resource_id,count(*) AS count FROM agent.capability_calls WHERE namespace=%s AND run_id=ANY(%s) AND status='running' GROUP BY run_id,resource_id",
                (self.namespace,run_ids)).fetchall()}
        result=[]
        for row in active:
            for identity,tool in snapshots[row['run_id']].items():
                if not resource_id or identity==resource_id:
                    result.append({**row,'resource_id':identity,'version':tool.get('revision',0),
                                   'in_flight_calls':in_flight.get((row['run_id'],identity),0)})
        return result

    def freeze(self, run_id: str, catalog: list[dict], *, metadata: dict | None = None,
               disabled_tools=(), connection=None) -> dict:
        with (nullcontext(connection) if connection is not None else self.knowledge_store.connection()) as conn, conn.transaction():
            existing = conn.execute('SELECT payload FROM agent.capability_snapshots WHERE namespace=%s AND run_id=%s',
                                    (self.namespace, run_id)).fetchone()
            if existing:
                return existing['payload']
            configured = {row['resource_id']: {**row['payload'], 'revision': row['revision']}
                          for row in conn.execute('SELECT resource_id,payload,revision FROM agent.capability_settings WHERE namespace=%s',
                                                  (self.namespace,)).fetchall()}
            disabled = set(disabled_tools)
            tools = {row['id']: {**row, **configured.get(row['id'], {}),
                                **({'enabled': False} if row['id'] in disabled else {})} for row in catalog}
            payload = {**(metadata or {}), 'run_id': run_id, 'snapshot_id': uuid4().hex, 'tools': tools}
            conn.execute('''INSERT INTO agent.capability_snapshots(namespace,run_id,snapshot_id,payload)
                VALUES (%s,%s,%s,%s) ON CONFLICT(namespace,run_id) DO NOTHING''',
                         (self.namespace, run_id, payload['snapshot_id'], Jsonb(payload)))
            return conn.execute('SELECT payload FROM agent.capability_snapshots WHERE namespace=%s AND run_id=%s',
                                (self.namespace, run_id)).fetchone()['payload']

    def snapshot(self, run_id: str) -> dict | None:
        with self.knowledge_store.connection() as conn:
            row = conn.execute('SELECT payload FROM agent.capability_snapshots WHERE namespace=%s AND run_id=%s',
                               (self.namespace, run_id)).fetchone()
        return row['payload'] if row else None

    def start_call(self, payload: dict) -> str:
        identity = uuid4().hex
        with self.knowledge_store.connection() as conn, conn.transaction():
            conn.execute('INSERT INTO agent.capability_calls(id,namespace,run_id,resource_id,status,payload) VALUES (%s,%s,%s,%s,%s,%s)',
                         (identity, self.namespace, payload.get('run_id', ''), payload['resource_id'], payload['status'], Jsonb(safe(payload))))
        return identity

    def finish_call(self, identity: str, status: str, payload: dict) -> None:
        with self.knowledge_store.connection() as conn, conn.transaction():
            conn.execute('UPDATE agent.capability_calls SET status=%s,payload=payload||%s,ended_at=now() WHERE namespace=%s AND id=%s',
                         (status, Jsonb(safe(payload)), self.namespace, identity))

    def attach_call_run(self, identity: str, run_id: str) -> None:
        with self.knowledge_store.connection() as conn,conn.transaction():
            updated = conn.execute('''UPDATE agent.capability_calls SET run_id=%s
                WHERE namespace=%s AND id=%s AND (run_id='' OR run_id=%s) RETURNING id''',
                (run_id,self.namespace,identity,run_id)).fetchone()
            if not updated:
                raise CapabilityError('CALL_RUN_CONFLICT','诊断记录已关联另一任务，未覆盖运行身份',status=409)

    def record_model_request(self, call_id: str, request_id: str, payload: dict, *, append=False) -> None:
        with self.knowledge_store.connection() as conn, conn.transaction():
            row = conn.execute('SELECT payload FROM agent.capability_calls WHERE namespace=%s AND id=%s FOR UPDATE',
                               (self.namespace, call_id)).fetchone()
            if row is None:
                raise CapabilityError('CALL_NOT_FOUND', '调用记录不存在，未提交模型请求', status=409)
            requests = list(row['payload'].get('context_requests') or [])
            if append:
                requests.append({**payload, 'request_id': request_id})
            else:
                target = next((item for item in requests if item['request_id'] == request_id), None)
                if target is None:
                    raise CapabilityError('REQUEST_NOT_FOUND', '模型请求记录不存在', status=409)
                target.update(payload)
            conn.execute('UPDATE agent.capability_calls SET payload=payload||%s WHERE namespace=%s AND id=%s',
                         (Jsonb(safe({'context_requests': requests})), self.namespace, call_id))

    def calls(self, *, run_id: str = '', resource_id: str = '', status: str = '', origin: str = '',
              limit: int = 50, cursor: str = '') -> dict:
        with self.knowledge_store.connection() as conn:
            rows = conn.execute('''SELECT id,run_id,resource_id,status,payload,started_at,ended_at FROM agent.capability_calls
                WHERE namespace=%s AND (%s='' OR run_id=%s) AND (%s='' OR resource_id=%s)
                AND (%s='' OR status=%s) AND (%s='' OR payload->>'origin'=%s)
                AND (%s='' OR (started_at,id) < (SELECT started_at,id FROM agent.capability_calls WHERE id=%s))
                ORDER BY started_at DESC,id DESC LIMIT %s''',
                (self.namespace, run_id, run_id, resource_id, resource_id, status, status, origin, origin, cursor, cursor, max(1,min(200,limit))+1)).fetchall()
        has_more = len(rows) > limit
        visible = rows[:limit]
        return {'rows': [{**r['payload'], **{k: str(v) if k.endswith('_at') and v else v for k,v in r.items() if k != 'payload'}} for r in visible],
                'next_cursor': visible[-1]['id'] if has_more and visible else None}

    def changes(self, *, resource_id: str = '', limit: int = 50, cursor: str = '') -> dict:
        with self.knowledge_store.connection() as conn:
            rows = conn.execute('''SELECT id,resource_id,revision,payload,created_at FROM agent.resource_changes
                WHERE namespace=%s AND (%s='' OR resource_id=%s) AND (%s='' OR id < %s)
                ORDER BY id DESC LIMIT %s''', (self.namespace,resource_id,resource_id,cursor,int(cursor or 2**63-1),max(1,min(200,limit))+1)).fetchall()
        visible = rows[:limit]
        return {'rows': [{**r['payload'],'id':str(r['id']),'resource_id':r['resource_id'],'revision':r['revision'],'created_at':str(r['created_at'])} for r in visible],
                'next_cursor': str(visible[-1]['id']) if len(rows)>limit and visible else None}
