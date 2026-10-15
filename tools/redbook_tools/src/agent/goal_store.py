"""Durable conversation goals and commands; artifacts remain in their original stores."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

from psycopg.types.json import Jsonb
from psycopg import sql

from src.knowledge.store import KnowledgeStore


def ensure_goal_schema(conn) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('redbook-agent-goal-schema'))")
    conn.execute('''CREATE TABLE IF NOT EXISTS agent.conversation_goals (
        goal_id text PRIMARY KEY, namespace text NOT NULL,
        conversation_id text NOT NULL REFERENCES agent.conversations(conversation_id) ON DELETE CASCADE,
        revision bigint NOT NULL DEFAULT 1, ledger_revision bigint NOT NULL DEFAULT 0,
        active_plan_id text NOT NULL, active_attempt_id text NOT NULL DEFAULT '',
        state text NOT NULL DEFAULT 'planned', lease_epoch bigint NOT NULL DEFAULT 0,
        applied_through_seq bigint NOT NULL DEFAULT 0, payload jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(namespace,conversation_id,active_plan_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS agent.conversation_turns (
        turn_id text PRIMARY KEY, namespace text NOT NULL,
        conversation_id text NOT NULL REFERENCES agent.conversations(conversation_id) ON DELETE CASCADE,
        source_message_id text NOT NULL, goal_id text REFERENCES agent.conversation_goals(goal_id) ON DELETE CASCADE,
        kind text NOT NULL, state text NOT NULL, expected_goal_revision bigint,
        expected_attempt_id text NOT NULL DEFAULT '', proposal_plan_id text NOT NULL DEFAULT '',
        command_payload jsonb NOT NULL, request_key text NOT NULL, request_hash text NOT NULL,
        result jsonb NOT NULL DEFAULT '{}', worker_id text NOT NULL DEFAULT '', lease_epoch bigint,
        claim_until timestamptz, accepted_at timestamptz NOT NULL DEFAULT now(), applied_at timestamptz,
        UNIQUE(namespace,conversation_id,request_key))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS agent.goal_artifact_links (
        namespace text NOT NULL, goal_id text NOT NULL REFERENCES agent.conversation_goals(goal_id) ON DELETE CASCADE,
        job_id text NOT NULL, post_id text NOT NULL, content_version text NOT NULL,
        origin_run_id text NOT NULL DEFAULT '', origin_job_key text NOT NULL DEFAULT '',
        eligibility_revision bigint NOT NULL, eligibility_state text NOT NULL,
        is_current boolean NOT NULL DEFAULT true, review_ref text NOT NULL DEFAULT '',
        receipt_ref text NOT NULL DEFAULT '', updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(namespace,goal_id,job_id,post_id,content_version))''')
    conn.execute('CREATE INDEX IF NOT EXISTS conversation_goals_lookup ON agent.conversation_goals(namespace,conversation_id,created_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS conversation_turns_queue ON agent.conversation_turns(namespace,state,accepted_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS goal_artifacts_current ON agent.goal_artifact_links(namespace,goal_id,job_id) WHERE is_current')
    for table, column, parent in (
            ('conversation_goals', 'conversation_id', 'conversations'),
            ('conversation_turns', 'conversation_id', 'conversations'),
            ('conversation_turns', 'goal_id', 'conversation_goals'),
            ('goal_artifact_links', 'goal_id', 'conversation_goals')):
        constraint = table + '_' + column + '_fkey'
        row = conn.execute('''SELECT confdeltype FROM pg_constraint
            WHERE conrelid=%s::regclass AND conname=%s''', ('agent.' + table, constraint)).fetchone()
        if row and row['confdeltype'] != 'c':
            conn.execute(sql.SQL('''ALTER TABLE agent.{} DROP CONSTRAINT {},
                ADD CONSTRAINT {} FOREIGN KEY({}) REFERENCES agent.{}({}) ON DELETE CASCADE''').format(
                sql.Identifier(table), sql.Identifier(constraint), sql.Identifier(constraint),
                sql.Identifier(column), sql.Identifier(parent), sql.Identifier(column)))


def _public(row):
    return {k: (v.isoformat() if isinstance(v, (datetime, date)) else deepcopy(v)) for k, v in dict(row).items()}


def _goal_plan(plan):
    return {key: deepcopy(value) for key, value in plan.items()
            if key not in {'frozen_execution', 'model_runtime', 'host_environment', 'conversation_context'}}


def invalidated_job_ids(previous, updated):
    old = {job.get('job_id'): job for job in previous.get('jobs', [])}
    fields = ('kind', 'prompt', 'search_keywords', 'topic_preferences', 'topic_brief',
              'topic_brief_strength', 'content_constraints', 'evaluation_viewpoint',
              'lookback_days', 'legacy_extra_requirements')
    return [job.get('job_id') for job in updated.get('jobs', [])
            if previous.get('image_score_required') != updated.get('image_score_required')
            or any(old.get(job.get('job_id'), {}).get(key) != job.get(key) for key in fields)]


class GoalStore:
    def __init__(self, knowledge_store: KnowledgeStore | None = None):
        self.knowledge_store = knowledge_store or KnowledgeStore.from_env()

    def create(self, namespace, conversation_id, plan, *, state='planned'):
        stamp = plan.get('confirmed_at') or (plan.get('frozen_execution') or {}).get('confirmed_at') or plan.get('created_at')
        executed = bool(plan.get('job_id') or plan.get('execution_request_id'))
        target = plan.get('target_date') or (datetime.fromtimestamp(float(stamp), timezone(timedelta(hours=8))).date().isoformat()
                 if stamp else '' if executed else datetime.now(timezone(timedelta(hours=8))).date().isoformat())
        with self.knowledge_store.connection() as conn, conn.transaction():
            if not conn.execute('SELECT 1 FROM agent.conversations WHERE conversation_id=%s AND account_namespace=%s',
                                (conversation_id, namespace)).fetchone():
                raise KeyError(conversation_id)
            goal_id = uuid4().hex
            row = conn.execute('''INSERT INTO agent.conversation_goals
                (goal_id,namespace,conversation_id,active_plan_id,state,payload)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(namespace,conversation_id,active_plan_id)
                DO UPDATE SET active_plan_id=EXCLUDED.active_plan_id RETURNING goal_id''',
                (goal_id, namespace, conversation_id, plan['id'], state,
                 Jsonb({'plan': _goal_plan(plan), 'target_date': target,
                        'legacy_projection_pending': executed}))).fetchone()
        return self.snapshot(namespace, row['goal_id'])

    def list(self, namespace, conversation_id):
        with self.knowledge_store.connection() as conn:
            rows = conn.execute('''SELECT goal_id FROM agent.conversation_goals
                WHERE namespace=%s AND conversation_id=%s ORDER BY created_at''', (namespace, conversation_id)).fetchall()
        return [self.snapshot(namespace, row['goal_id']) for row in rows]

    def snapshot(self, namespace, goal_id):
        with self.knowledge_store.connection() as conn, conn.transaction():
            # A single statement snapshot for goal metadata and artifact versions.
            row = conn.execute('''SELECT g.*, coalesce((SELECT jsonb_agg(to_jsonb(a))
                FROM agent.goal_artifact_links a WHERE a.namespace=g.namespace AND a.goal_id=g.goal_id
                AND a.is_current), '[]'::jsonb) AS artifacts
                FROM agent.conversation_goals g WHERE namespace=%s AND goal_id=%s''', (namespace, goal_id)).fetchone()
        if not row:
            raise KeyError(goal_id)
        result = _public(row)
        payload = result.pop('payload')
        result['plan'] = payload['plan']
        result['target_date'] = payload.get('target_date', '')
        result['legacy_projection_pending'] = payload.get('legacy_projection_pending', False)
        result['pending_user_changes'] = self.pending_changes(namespace, result['conversation_id'], goal_id)
        result['jobs'] = []
        for index, job in enumerate(result['plan'].get('jobs', [])):
            jid = job.get('job_id') or job.get('id') or f"{index}:{job['kind']}"
            items = [a for a in result['artifacts'] if a['job_id'] == jid]
            delivered = {a['post_id'] for a in items if a['eligibility_state'] == 'delivered' and a['receipt_ref']}
            ready = {a['post_id'] for a in items if a['eligibility_state'] == 'approved'
                     and a['eligibility_revision'] == result['revision']} - delivered
            uncertain = {a['post_id'] for a in items if a['eligibility_state'] == 'uncertain'} - delivered
            approved = delivered | ready
            requested = int(job['count'])
            done = len(approved) if result['plan'].get('delivery') == 'generate_only' else len(delivered)
            result['jobs'].append({'job_id': jid, 'kind': job['kind'], 'title': job.get('title', job['kind']),
                'requested': requested, 'generated_valid': len(approved), 'approved_current': len(approved),
                'delivered_verified': len(delivered), 'retained_ready': len(ready), 'uncertain': len(uncertain),
                'rejected': sum(a['eligibility_state'] == 'rejected' for a in items),
                'remaining': max(0, requested - done),
                'new_generation_needed': max(0, requested - len(approved | uncertain))})
        return result

    def finish_legacy_projection(self, namespace, goal_id):
        with self.knowledge_store.connection() as conn:
            conn.execute('''UPDATE agent.conversation_goals SET payload=payload || %s
                WHERE namespace=%s AND goal_id=%s''',
                (Jsonb({'legacy_projection_pending': False}), namespace, goal_id))

    def record_artifact(self, namespace, goal_id, job_id, post_id, content_version, *, state,
                        origin_run_id='', origin_job_key='', review_ref='', receipt_ref='', lease_epoch=None):
        if state not in {'generated', 'approved', 'delivered', 'uncertain', 'rejected', 'needs_review'}:
            raise ValueError('invalid artifact state')
        if not post_id or not content_version or (state == 'delivered' and not receipt_ref):
            raise ValueError('artifact version and verified delivery receipt required')
        with self.knowledge_store.connection() as conn, conn.transaction():
            goal = conn.execute('SELECT revision,lease_epoch FROM agent.conversation_goals WHERE namespace=%s AND goal_id=%s FOR UPDATE',
                                (namespace, goal_id)).fetchone()
            if not goal:
                raise KeyError(goal_id)
            if lease_epoch is not None and goal['lease_epoch'] != lease_epoch:
                raise ValueError('GOAL_LEASE_LOST')
            previous = conn.execute('''SELECT * FROM agent.goal_artifact_links WHERE namespace=%s AND goal_id=%s
                AND job_id=%s AND post_id=%s AND is_current''', (namespace, goal_id, job_id, post_id)).fetchone()
            if previous and previous['content_version'] == content_version:
                if previous['eligibility_state'] == 'delivered' and state in {'approved', 'generated'}:
                    return
                if (previous['eligibility_state'], previous['review_ref'], previous['receipt_ref'], previous['eligibility_revision']) == (state, review_ref, receipt_ref, goal['revision']):
                    return
            conn.execute('''UPDATE agent.goal_artifact_links SET is_current=false
                WHERE namespace=%s AND goal_id=%s AND job_id=%s AND post_id=%s''', (namespace, goal_id, job_id, post_id))
            conn.execute('''INSERT INTO agent.goal_artifact_links
                (namespace,goal_id,job_id,post_id,content_version,origin_run_id,origin_job_key,
                 eligibility_revision,eligibility_state,review_ref,receipt_ref)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace,goal_id,job_id,post_id,content_version) DO UPDATE
                SET eligibility_state=EXCLUDED.eligibility_state,eligibility_revision=EXCLUDED.eligibility_revision,
                    review_ref=EXCLUDED.review_ref,receipt_ref=EXCLUDED.receipt_ref,is_current=true,updated_at=now()''',
                (namespace, goal_id, job_id, post_id, content_version, origin_run_id, origin_job_key,
                 goal['revision'], state, review_ref, receipt_ref))
            conn.execute('UPDATE agent.conversation_goals SET ledger_revision=ledger_revision+1,updated_at=now() WHERE goal_id=%s', (goal_id,))

    def revise(self, namespace, goal_id, plan, *, expected_revision, expected_attempt_id=None):
        with self.knowledge_store.connection() as conn, conn.transaction():
            previous = conn.execute('''SELECT payload,revision,active_attempt_id FROM agent.conversation_goals
                WHERE namespace=%s AND goal_id=%s FOR UPDATE''', (namespace, goal_id)).fetchone()
            if (not previous or previous['revision'] != expected_revision
                    or expected_attempt_id is not None and previous['active_attempt_id'] != expected_attempt_id):
                raise ValueError('GOAL_REVISION_CONFLICT')
            invalidated = invalidated_job_ids(previous['payload']['plan'], plan)
            changed = conn.execute('''UPDATE agent.goal_artifact_links SET
                eligibility_state=CASE WHEN job_id=ANY(%s) AND eligibility_state='approved'
                    THEN 'needs_review' ELSE eligibility_state END,
                eligibility_revision=%s,updated_at=now()
                WHERE namespace=%s AND goal_id=%s AND is_current AND eligibility_state<>'delivered' ''',
                (invalidated, expected_revision + 1, namespace, goal_id)).rowcount
            row = conn.execute('''UPDATE agent.conversation_goals SET payload=payload || %s, active_plan_id=%s,
                revision=revision+1,ledger_revision=ledger_revision+%s,updated_at=now()
                WHERE namespace=%s AND goal_id=%s AND revision=%s RETURNING goal_id''',
                (Jsonb({'plan': _goal_plan(plan)}), plan['id'], int(bool(changed)), namespace, goal_id, expected_revision)).fetchone()
            if not row:
                raise ValueError('GOAL_REVISION_CONFLICT')
        return self.snapshot(namespace, goal_id)

    def bind_attempt(self, namespace, goal_id, attempt_id, *, state='running', expected_attempt_id=None,
                     expected_revision=None):
        with self.knowledge_store.connection() as conn:
            row = conn.execute('''UPDATE agent.conversation_goals SET active_attempt_id=%s,state=%s,
                lease_epoch=lease_epoch+CASE WHEN active_attempt_id=%s THEN 0 ELSE 1 END,
                updated_at=now() WHERE namespace=%s AND goal_id=%s
                AND (%s::text IS NULL OR active_attempt_id IN (%s,%s))
                AND (%s::bigint IS NULL OR revision=%s) RETURNING lease_epoch''',
                (attempt_id, state, attempt_id, namespace, goal_id, expected_attempt_id,
                 expected_attempt_id, attempt_id, expected_revision, expected_revision)).fetchone()
            if not row:
                raise ValueError('GOAL_LEASE_LOST: attempt or revision changed')
        return int(row['lease_epoch'])

    def set_state(self, namespace, goal_id, state, *, expected_epoch=None):
        with self.knowledge_store.connection() as conn:
            row = conn.execute('''UPDATE agent.conversation_goals SET state=%s,updated_at=now()
                WHERE namespace=%s AND goal_id=%s AND (%s::bigint IS NULL OR lease_epoch=%s) RETURNING goal_id''',
                (state, namespace, goal_id, expected_epoch, expected_epoch)).fetchone()
        if not row:
            raise ValueError('GOAL_LEASE_LOST')

    def enqueue_turn(self, namespace, conversation_id, message_id, command, expected_goal_revision, request_key):
        if not isinstance(request_key, str) or not request_key or len(request_key) > 120:
            raise ValueError('REQUEST_KEY_INVALID')
        request_hash = hashlib.sha256(json.dumps([message_id, command, expected_goal_revision], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        goal_id = command.get('goal_id') or None
        with self.knowledge_store.connection() as conn, conn.transaction():
            if not conn.execute('SELECT 1 FROM agent.conversations WHERE conversation_id=%s AND account_namespace=%s', (conversation_id, namespace)).fetchone():
                raise KeyError(conversation_id)
            if goal_id:
                goal = conn.execute('SELECT revision FROM agent.conversation_goals WHERE goal_id=%s AND namespace=%s AND conversation_id=%s FOR UPDATE', (goal_id, namespace, conversation_id)).fetchone()
                if not goal:
                    raise KeyError(goal_id)
                if expected_goal_revision is not None and goal['revision'] != expected_goal_revision:
                    raise ValueError('GOAL_REVISION_CONFLICT')
            row = conn.execute('''INSERT INTO agent.conversation_turns
                (turn_id,namespace,conversation_id,source_message_id,goal_id,kind,state,expected_goal_revision,
                 expected_attempt_id,proposal_plan_id,command_payload,request_key,request_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(namespace,conversation_id,request_key) DO UPDATE SET request_key=EXCLUDED.request_key RETURNING *''',
                (uuid4().hex, namespace, conversation_id, message_id, goal_id, command['kind'],
                 command.get('state', 'queued'), expected_goal_revision, command.get('expected_attempt_id', ''),
                 command.get('proposal_plan_id', ''), Jsonb(command), request_key, request_hash)).fetchone()
            if row['request_hash'] != request_hash:
                raise ValueError('REQUEST_KEY_CONFLICT')
        return _public(row)

    def turn(self, namespace, turn_id):
        with self.knowledge_store.connection() as conn:
            row = conn.execute('SELECT * FROM agent.conversation_turns WHERE namespace=%s AND turn_id=%s', (namespace, turn_id)).fetchone()
        if not row:
            raise KeyError(turn_id)
        return _public(row)

    def pending_changes(self, namespace, conversation_id, goal_id=None):
        with self.knowledge_store.connection() as conn:
            rows = conn.execute('''SELECT turn_id,source_message_id AS message_id,kind,state
                FROM agent.conversation_turns WHERE namespace=%s AND conversation_id=%s
                AND (%s::text IS NULL OR goal_id=%s) AND state IN ('accepted','awaiting_confirmation','queued','applying')
                ORDER BY accepted_at''', (namespace, conversation_id, goal_id, goal_id)).fetchall()
        return [dict(row) for row in rows]

    def pending_turns(self, namespace, limit=100):
        with self.knowledge_store.connection() as conn:
            rows = conn.execute('''SELECT * FROM agent.conversation_turns WHERE namespace=%s
                AND state IN ('accepted','queued','applying')
                AND (kind='route' OR command_payload->>'confirmed'='true')
                ORDER BY accepted_at LIMIT %s''', (namespace, limit)).fetchall()
        return [_public(row) for row in rows]

    def _advance_applied(self, conn, namespace, conversation_id):
        conn.execute('''UPDATE agent.conversation_goals g SET applied_through_seq=greatest(g.applied_through_seq,
            coalesce((SELECT max(m.seq) FROM agent.conversation_turns t JOIN agent.messages m
              ON m.conversation_id=t.conversation_id AND m.message_id=t.source_message_id
              WHERE t.namespace=g.namespace AND t.goal_id=g.goal_id AND t.state='applied'),0))
            WHERE g.namespace=%s AND g.conversation_id=%s''', (namespace, conversation_id))

    def find_turn(self, namespace, conversation_id, request_key):
        with self.knowledge_store.connection() as conn:
            row = conn.execute('SELECT * FROM agent.conversation_turns WHERE namespace=%s AND conversation_id=%s AND request_key=%s',
                               (namespace, conversation_id, request_key)).fetchone()
        return _public(row) if row else None

    def update_turn(self, namespace, turn_id, *, state, command=None, goal_id=None, result=None, proposal_plan_id=''):
        with self.knowledge_store.connection() as conn, conn.transaction():
            if goal_id is not None:
                owner = conn.execute('''SELECT 1 FROM agent.conversation_turns t
                    JOIN agent.conversation_goals g ON g.namespace=t.namespace AND g.conversation_id=t.conversation_id
                    WHERE t.namespace=%s AND t.turn_id=%s AND g.goal_id=%s''',
                    (namespace, turn_id, goal_id)).fetchone()
                if not owner:
                    raise ValueError('GOAL_NAMESPACE_CONFLICT')
            row = conn.execute('''UPDATE agent.conversation_turns SET state=%s,
                applied_at=CASE WHEN %s='applied' THEN coalesce(applied_at,now()) ELSE applied_at END,
                kind=coalesce(%s,kind),
                goal_id=coalesce(%s,goal_id),command_payload=coalesce(%s,command_payload),
                result=coalesce(%s,result),proposal_plan_id=CASE WHEN %s<>'' THEN %s ELSE proposal_plan_id END
                WHERE namespace=%s AND turn_id=%s RETURNING *''',
                (state, state, command.get('kind') if command else None,
                 goal_id, Jsonb(command) if command is not None else None, Jsonb(result) if result is not None else None,
                 proposal_plan_id, proposal_plan_id, namespace, turn_id)).fetchone()
        if not row:
            raise KeyError(turn_id)
        if state == 'applied':
            with self.knowledge_store.connection() as conn:
                self._advance_applied(conn, namespace, row['conversation_id'])
        return _public(row)

    def turns(self, namespace, conversation_id):
        with self.knowledge_store.connection() as conn:
            rows = conn.execute('SELECT * FROM agent.conversation_turns WHERE namespace=%s AND conversation_id=%s ORDER BY accepted_at', (namespace, conversation_id)).fetchall()
        return [_public(row) for row in rows]

    def claim_turn(self, namespace, turn_id, worker_id):
        with self.knowledge_store.connection() as conn, conn.transaction():
            row = conn.execute('''UPDATE agent.conversation_turns SET state='applying',worker_id=%s,
                claim_until=now()+interval '30 seconds' WHERE namespace=%s AND turn_id=%s
                AND (state IN ('accepted','queued') OR (state='applying' AND claim_until<now())) RETURNING *''', (worker_id, namespace, turn_id)).fetchone()
            if not row:
                return None
            if row['goal_id']:
                goal = conn.execute('SELECT lease_epoch FROM agent.conversation_goals WHERE namespace=%s AND goal_id=%s FOR UPDATE', (namespace, row['goal_id'])).fetchone()
                row['lease_epoch'] = goal['lease_epoch']
                conn.execute('UPDATE agent.conversation_turns SET lease_epoch=%s WHERE turn_id=%s', (row['lease_epoch'], turn_id))
        return _public(row)

    def record_result(self, claim, result):
        with self.knowledge_store.connection() as conn, conn.transaction():
            if claim.get('goal_id'):
                goal = conn.execute('''SELECT lease_epoch,revision,active_plan_id,active_attempt_id
                    FROM agent.conversation_goals WHERE namespace=%s AND goal_id=%s FOR UPDATE''',
                    (claim['namespace'], claim['goal_id'])).fetchone()
                command = claim.get('command_payload') or {}
                own_handoff = (command.get('confirmed') and goal
                    and goal['active_plan_id'] == claim.get('proposal_plan_id')
                    and goal['revision'] == command.get('expected_goal_revision', -2) + 1
                    and goal['lease_epoch'] <= (claim.get('lease_epoch') or 0) + 1
                    and (result.get('state') != 'applied' or result.get('attempt_id') == goal['active_attempt_id']))
                if not goal or goal['lease_epoch'] != claim.get('lease_epoch') and not own_handoff:
                    raise ValueError('TURN_CLAIM_LOST: goal lease changed')
            row = conn.execute('''UPDATE agent.conversation_turns SET state=%s,result=%s,
                applied_at=CASE WHEN %s='applied' THEN now() ELSE applied_at END,claim_until=NULL
                WHERE namespace=%s AND turn_id=%s AND worker_id=%s AND state='applying' RETURNING *''',
                (result.get('state', 'applied'), Jsonb(result), result.get('state', 'applied'),
                 claim['namespace'], claim['turn_id'], claim['worker_id'])).fetchone()
            if not row:
                raise ValueError('TURN_CLAIM_LOST')
            if row['state'] == 'applied':
                self._advance_applied(conn, claim['namespace'], row['conversation_id'])
        return _public(row)
