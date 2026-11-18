"""Durable control and action receipts beside the framework's native checkpoints."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb
from psycopg import sql

from src.agent.capabilities.models import safe
from src.agent.conversation_store import PostgresConversationStore
from src.knowledge.store import KnowledgeStore
from .dialogue_goal import goal_evidence


ACTIVE = {'starting', 'running', 'stopping'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class DialogueStore:
    def __init__(self, knowledge=None, *, namespace='local'):
        self.knowledge = knowledge or KnowledgeStore.from_env()
        self.namespace = namespace
        self.conversations = PostgresConversationStore(self.knowledge, namespace=namespace)

    def setup(self):
        with self.knowledge.connection('migration') as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('redbook-dialogue-schema-v1'))")
            conn.execute('CREATE SCHEMA IF NOT EXISTS agent')
            conn.execute('''CREATE TABLE IF NOT EXISTS agent.dialogue_sessions (
                namespace text NOT NULL, conversation_id text NOT NULL, thread_id text NOT NULL,
                state text NOT NULL DEFAULT 'idle', execution_id text NOT NULL DEFAULT '',
                revision bigint NOT NULL DEFAULT 1, control_epoch bigint NOT NULL DEFAULT 0,
                input jsonb NOT NULL DEFAULT '{}', permissions jsonb NOT NULL DEFAULT '{}',
                model_env jsonb NOT NULL DEFAULT '{}', pid integer, process_identity text,
                updated_at double precision NOT NULL,
                PRIMARY KEY(namespace,conversation_id))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS agent.dialogue_events (
                id bigserial PRIMARY KEY, namespace text NOT NULL, conversation_id text NOT NULL,
                kind text NOT NULL, payload jsonb NOT NULL, event_key text NOT NULL,
                created_at double precision NOT NULL, UNIQUE(namespace,conversation_id,event_key))''')
            conn.execute("ALTER TABLE agent.dialogue_sessions ADD COLUMN IF NOT EXISTS goal jsonb NOT NULL DEFAULT '{}'")
            conn.execute('''CREATE TABLE IF NOT EXISTS agent.dialogue_actions (
                id text PRIMARY KEY, namespace text NOT NULL, conversation_id text NOT NULL,
                execution_id text NOT NULL, call_id text NOT NULL, tool text NOT NULL,
                dedupe_key text NOT NULL, arguments jsonb NOT NULL, status text NOT NULL,
                result jsonb NOT NULL DEFAULT '{}', error_code text NOT NULL DEFAULT '',
                created_at double precision NOT NULL, updated_at double precision NOT NULL,
                UNIQUE(namespace,conversation_id,dedupe_key))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS agent.dialogue_requests (
                id text PRIMARY KEY, namespace text NOT NULL, conversation_id text NOT NULL,
                execution_id text NOT NULL, request_key text NOT NULL, kind text NOT NULL,
                status text NOT NULL DEFAULT 'pending', version bigint NOT NULL DEFAULT 1,
                payload jsonb NOT NULL, answer jsonb NOT NULL DEFAULT '{}',
                created_at double precision NOT NULL, updated_at double precision NOT NULL,
                UNIQUE(namespace,conversation_id,request_key))''')
            conn.execute('CREATE INDEX IF NOT EXISTS dialogue_events_conversation ON agent.dialogue_events(namespace,conversation_id,id)')
            conn.execute('CREATE INDEX IF NOT EXISTS dialogue_actions_conversation ON agent.dialogue_actions(namespace,conversation_id,created_at)')
            app = self.knowledge._credentials('app')['user']
            for table in ('dialogue_sessions', 'dialogue_events', 'dialogue_actions', 'dialogue_requests'):
                conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON agent.{} TO {}').format(sql.Identifier(table), sql.Identifier(app)))
            conn.execute(sql.SQL('GRANT USAGE,SELECT ON SEQUENCE agent.dialogue_events_id_seq TO {}').format(sql.Identifier(app)))
        tables = self.query("SELECT to_regclass('checkpoints') AS native_checkpoints", one=True)
        if not tables['native_checkpoints']:
            from src.agent.postgres_checkpoint import setup_postgres_checkpointer
            setup_postgres_checkpointer(self.knowledge)

    def query(self, statement, args=(), *, one=False):
        with self.knowledge.connection() as conn:
            cursor = conn.execute(statement, args)
            if cursor.description:
                return cursor.fetchone() if one else cursor.fetchall()

    def session(self, cid):
        self.conversations.get(cid, message_limit=1)
        thread = 'dialogue-v1:' + fingerprint([self.namespace, cid])
        self.query('''INSERT INTO agent.dialogue_sessions(namespace,conversation_id,thread_id,updated_at)
            VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''', (self.namespace, cid, thread, time.time()))
        return self.query('SELECT * FROM agent.dialogue_sessions WHERE namespace=%s AND conversation_id=%s',
                          (self.namespace, cid), one=True)

    def transition(self, cid, *, expected_execution_id=None, expected_goal=None, expected_state=None, **values):
        if set(values) - {'state', 'execution_id', 'input', 'permissions', 'model_env', 'pid', 'process_identity', 'control_epoch', 'goal'}:
            raise ValueError('INVALID_SESSION_UPDATE')
        values['updated_at'] = time.time()
        assignments = sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in values)
        args = [Jsonb(v) if isinstance(v, dict) else v for v in values.values()]
        where = sql.SQL('namespace=%s AND conversation_id=%s')
        args.extend([self.namespace, cid])
        if expected_execution_id is not None:
            where += sql.SQL(' AND execution_id=%s')
            args.append(expected_execution_id)
        if expected_goal is not None:
            where += sql.SQL(' AND goal=%s')
            args.append(Jsonb(expected_goal))
        if expected_state is not None:
            where += sql.SQL(' AND state=%s')
            args.append(expected_state)
        return self.query(sql.SQL('UPDATE agent.dialogue_sessions SET {},revision=revision+1 WHERE {} RETURNING *').format(assignments, where),
                          tuple(args), one=True)

    def event(self, cid, kind, payload, *, key=None):
        return self.query('''INSERT INTO agent.dialogue_events
            (namespace,conversation_id,kind,payload,event_key,created_at) VALUES(%s,%s,%s,%s,%s,%s)
            ON CONFLICT(namespace,conversation_id,event_key) DO UPDATE SET event_key=excluded.event_key RETURNING *''',
            (self.namespace, cid, kind, Jsonb(safe(payload)), key or uuid4().hex, time.time()), one=True)

    def message(self, cid, role, text, *, identity=None):
        identity = identity or uuid4().hex
        with self.knowledge.connection() as conn, conn.transaction():
            row = conn.execute('''SELECT conversation_id FROM agent.conversations
                WHERE conversation_id=%s AND account_namespace=%s FOR UPDATE''', (cid, self.namespace)).fetchone()
            if not row:
                raise KeyError('CONVERSATION_NOT_FOUND')
            saved = self.conversations.get(cid, connection=conn, message_limit=1)
            existing = conn.execute('SELECT content FROM agent.messages WHERE conversation_id=%s AND message_id=%s', (cid, identity)).fetchone()
            if existing:
                if existing['content'].get('content') != text or existing['content'].get('role') != role:
                    raise ValueError('MESSAGE_ID_CONFLICT')
                return existing['content']
            message = {'id': identity, 'role': role, 'content': str(text), 'created_at': time.time()}
            saved['messages'].append(message)
            saved['updated_at'] = time.time()
            self.conversations.save(saved, connection=conn)
            return message

    def request(self, cid, execution_id, kind, payload, *, key):
        self.query('''INSERT INTO agent.dialogue_requests
            (id,namespace,conversation_id,execution_id,request_key,kind,payload,created_at,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(namespace,conversation_id,request_key) DO NOTHING''',
            (uuid4().hex, self.namespace, cid, execution_id, key, kind, Jsonb(safe(payload)), time.time(), time.time()))
        return self.query('''SELECT * FROM agent.dialogue_requests
            WHERE namespace=%s AND conversation_id=%s AND request_key=%s''', (self.namespace, cid, key), one=True)

    def answer(self, cid, request_id, version, answer):
        row = self.query('''UPDATE agent.dialogue_requests SET status='answered',answer=%s,
            version=version+1,updated_at=%s WHERE namespace=%s AND conversation_id=%s AND id=%s
            AND status='pending' AND version=%s RETURNING *''',
            (Jsonb(answer), time.time(), self.namespace, cid, request_id, version), one=True)
        if row:
            return row
        old = self.query('SELECT * FROM agent.dialogue_requests WHERE namespace=%s AND conversation_id=%s AND id=%s',
                         (self.namespace, cid, request_id), one=True)
        if old and old['status'] == 'answered' and old['answer'] == answer:
            return old
        raise ValueError('REQUEST_VERSION_CONFLICT: 请刷新后核对，未重复执行')

    def claim_action(self, cid, execution_id, call_id, tool, arguments, *, business_key=None):
        key = business_key or fingerprint([tool, call_id, arguments])
        row = self.query('''INSERT INTO agent.dialogue_actions
            (id,namespace,conversation_id,execution_id,call_id,tool,dedupe_key,arguments,status,created_at,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'running',%s,%s)
            ON CONFLICT(namespace,conversation_id,dedupe_key) DO NOTHING RETURNING *''',
            (uuid4().hex, self.namespace, cid, execution_id, call_id, tool, key, Jsonb(arguments), time.time(), time.time()), one=True)
        if row:
            return row, True
        return self.query('''SELECT * FROM agent.dialogue_actions
            WHERE namespace=%s AND conversation_id=%s AND dedupe_key=%s''', (self.namespace, cid, key), one=True), False

    def finish_action(self, action_id, status, result):
        return self.query('''UPDATE agent.dialogue_actions SET status=%s,result=%s,error_code=%s,updated_at=%s
            WHERE namespace=%s AND id=%s RETURNING *''',
            (status, Jsonb(safe(result)), str(result.get('error_code') or result.get('code') or ''),
             time.time(), self.namespace, action_id), one=True)

    def actions(self, cid):
        return self.query('SELECT * FROM agent.dialogue_actions WHERE namespace=%s AND conversation_id=%s ORDER BY created_at DESC',
                          (self.namespace, cid))

    def action(self, cid, key):
        return self.query('''SELECT * FROM agent.dialogue_actions
            WHERE namespace=%s AND conversation_id=%s AND dedupe_key=%s''', (self.namespace, cid, key), one=True)

    def grant(self, cid, tool, *, calls=1, post_ids=None, arguments_scope=None, denied=False):
        with self.knowledge.connection() as conn, conn.transaction():
            row = conn.execute('''SELECT permissions,execution_id FROM agent.dialogue_sessions
                WHERE namespace=%s AND conversation_id=%s FOR UPDATE''', (self.namespace, cid)).fetchone()
            permissions = dict(row['permissions'])
            permissions[tool] = {'remaining': max(0, min(20, int(calls))),
                                 'execution_id': row['execution_id'],
                                 'post_ids': post_ids, 'denied': denied,
                                 'arguments_scope': arguments_scope or {},
                                 'date': datetime.now(timezone(timedelta(hours=8))).date().isoformat()}
            conn.execute('''UPDATE agent.dialogue_sessions SET permissions=%s,revision=revision+1,
                updated_at=%s WHERE namespace=%s AND conversation_id=%s''',
                (Jsonb(permissions), time.time(), self.namespace, cid))

    def consume_grant(self, cid, execution_id, tool, arguments):
        with self.knowledge.connection() as conn, conn.transaction():
            row = conn.execute('''SELECT permissions,state,execution_id FROM agent.dialogue_sessions
                WHERE namespace=%s AND conversation_id=%s FOR UPDATE''', (self.namespace, cid)).fetchone()
            if row['state'] != 'running' or row['execution_id'] != execution_id:
                raise ValueError('DIALOGUE_STOPPED: 执行身份已变化，未开始新动作')
            permissions = dict(row['permissions'])
            grant = dict(permissions.get(tool) or {})
            today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
            if (grant.get('execution_id') != execution_id or grant.get('denied')
                    or grant.get('date') != today or int(grant.get('remaining', 0)) < 1):
                return False
            if grant.get('post_ids') is not None and arguments.get('post_id') not in grant['post_ids']:
                return False
            if any(arguments.get(k) != v for k, v in grant.get('arguments_scope', {}).items()):
                return False
            grant['remaining'] -= 1
            permissions[tool] = grant
            conn.execute('''UPDATE agent.dialogue_sessions SET permissions=%s,revision=revision+1
                WHERE namespace=%s AND conversation_id=%s''', (Jsonb(permissions), self.namespace, cid))
            return True

    def abandon(self, cid, execution_id):
        self.query('''UPDATE agent.dialogue_actions SET status='uncertain',updated_at=%s
            WHERE namespace=%s AND conversation_id=%s AND execution_id=%s AND status='running' ''',
            (time.time(), self.namespace, cid, execution_id))

    def snapshot(self, cid):
        from src.llm.generate import _strip_reasoning_text
        def public_text(text):
            try:
                return _strip_reasoning_text(text)
            except ValueError:
                return '模型消息未完整分隔，已隐藏内部内容。'
        session = self.session(cid)
        events = self.query('''SELECT id,kind,payload,created_at FROM agent.dialogue_events
            WHERE namespace=%s AND conversation_id=%s ORDER BY id DESC LIMIT 100''', (self.namespace, cid))
        for event in events:
            if event['kind'] == 'assistant/message':
                event['payload']['text'] = public_text(event['payload'].get('text', ''))
        requests = self.query('''SELECT * FROM agent.dialogue_requests WHERE namespace=%s
            AND conversation_id=%s AND status='pending' ORDER BY created_at''', (self.namespace, cid))
        visible = []
        for row in requests:
            visible.append({'id': row['id'], 'kind': row['kind'], 'version': row['version'], **row['payload']})
        messages = []
        for message in self.conversations.get(cid, message_limit=100)['messages']:
            if message['role'] == 'assistant':
                message = {**message, 'content': public_text(message['content'])}
                if not message['content']:
                    continue
            messages.append(message)
        actions = self.actions(cid)
        goal = session['goal']
        public_goal = {**goal, 'counts': goal_evidence(goal, actions)[0]} if goal.get('state') != 'cleared' and goal else None
        return safe({'engine': 'langchain', 'state': session['state'], 'revision': session['revision'],
                     'execution_id': session['execution_id'], 'round_started_at': session['input'].get('round_started_at'),
                     'goal': public_goal, 'goal_version': goal.get('version', 0), 'events': list(reversed(events)),
                     'requests': visible, 'actions': actions,
                     'messages': messages})
