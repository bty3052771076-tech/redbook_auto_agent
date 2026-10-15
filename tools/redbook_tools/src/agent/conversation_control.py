"""Cooperative control at durable boundaries and explicit revision imports."""
from copy import deepcopy
import os

from src.agent.goal_store import GoalStore, invalidated_job_ids
from src.agent.plan_contract import digest


class ConversationPaused(RuntimeError):
    def __init__(self, kind='pause_goal'):
        self.kind = kind
        super().__init__('CONVERSATION_CANCELLED' if kind == 'cancel_goal' else 'CONVERSATION_PAUSED')


def artifact_version(post):
    if hasattr(post, 'assets') and hasattr(post, 'body'):
        from src.publish.draft_delivery import content_revision_fingerprint
        return content_revision_fingerprint(post)
    from src.agent.editorial_agent import _content_version
    return _content_version(post)


class ConversationControl:
    def __init__(self, context, *, store=None):
        self.context = dict(context or {})
        self.store = store or GoalStore()
        self.goal_id = self.context['goal_id']
        self.namespace = self.context['namespace']
        snapshot = self.store.snapshot(self.namespace, self.goal_id)
        self.attempt_id = os.getenv('AGENT_WEB_ATTEMPT_ID') or self.context.get('attempt_id') or snapshot['active_attempt_id']
        self.epoch = snapshot['lease_epoch']
        self.revision = int(self.context['goal_revision'])

    def check_boundary(self, stage, post_id=None):
        goal = self.store.snapshot(self.namespace, self.goal_id)
        if (goal['active_attempt_id'] != self.attempt_id or goal['lease_epoch'] != self.epoch
                or goal['revision'] != self.revision):
            raise ValueError('GOAL_LEASE_LOST: current attempt cannot write')
        if goal['state'] in {'paused', 'cancelled'}:
            raise ConversationPaused('cancel_goal' if goal['state'] == 'cancelled' else 'pause_goal')
        for turn in self.store.turns(self.namespace, goal['conversation_id']):
            if turn['goal_id'] != self.goal_id or turn['state'] not in {'queued', 'awaiting_confirmation'}:
                continue
            kind = turn['command_payload'].get('kind')
            if kind not in {'pause_goal', 'cancel_goal', 'amend_goal'}:
                continue
            state = 'cancelled' if kind == 'cancel_goal' else 'paused'
            self.store.set_state(self.namespace, self.goal_id, state, expected_epoch=self.epoch)
            if kind != 'amend_goal':
                claim = self.store.claim_turn(self.namespace, turn['turn_id'], self.attempt_id)
                if claim:
                    self.store.record_result(claim, {'state': 'applied', 'boundary': stage, 'post_id': post_id})
            raise ConversationPaused(kind)
        return 'continue'

    def record_state(self, state):
        jobs = state.get('jobs') or []
        records = dict(state.get('job_states') or {})
        if state.get('current_job'):
            records[str(state.get('job_index', 0))] = state
        uploaded = set(state.get('uploaded_post_ids') or [])
        for key, record in records.items():
            if not str(key).isdigit() or int(key) >= len(jobs):
                continue
            job = jobs[int(key)]
            jid = job.get('job_id') or f"{key}:{job['kind']}"
            approved = set(record.get('reviewed_post_ids') or [])
            item_status = state.get('item_status') or {}
            for post in record.get('posts') or []:
                pid = str(getattr(post, 'id', ''))
                if not pid:
                    continue
                status, receipt = ('approved' if pid in approved else 'generated'), ''
                from src.publish.draft_delivery import has_current_draft_receipt
                platforms = ('xhs', 'toutiao') if self.context.get('platform') == 'both' else (self.context.get('platform') or 'xhs',)
                if hasattr(post, 'platform') and all(has_current_draft_receipt(post, platform=p) for p in platforms):
                    status, receipt = 'delivered', f"post:{pid}:{artifact_version(post)}"
                elif pid in uploaded:
                    status = 'uncertain'
                elif any(k.startswith(str(key) + ':' + pid + ':') and v == 'uncertain' for k, v in item_status.items()):
                    status = 'uncertain'
                self.store.record_artifact(self.namespace, self.goal_id, jid, pid, artifact_version(post),
                    state=status, origin_run_id=state['run_id'], origin_job_key=str(key),
                    review_ref=f"checkpoint:{state['run_id']}:{key}" if pid in approved else '',
                    receipt_ref=receipt, lease_epoch=self.epoch)


def check_boundary(goal_id, attempt_id, stage, post_id=None, *, namespace='local'):
    store = GoalStore()
    goal = store.snapshot(namespace, goal_id)
    return ConversationControl({'goal_id': goal_id, 'namespace': namespace, 'goal_revision': goal['revision'],
                                'attempt_id': attempt_id}, store=store).check_boundary(stage, post_id)


def build_resume_manifest(goal_snapshot, confirmed_plan):
    invalidated = set(invalidated_job_ids(goal_snapshot['plan'], confirmed_plan))
    records = []
    for index, job in enumerate(confirmed_plan['jobs']):
        jid = job.get('job_id') or f"{index}:{job['kind']}"
        items = [a for a in goal_snapshot['artifacts'] if a['job_id'] == jid]
        delivered = [a for a in items if a['eligibility_state'] == 'delivered' and a['receipt_ref']]
        pending = [a for a in items if a['eligibility_state'] != 'delivered']
        if any(a['eligibility_state'] == 'uncertain' for a in pending):
            raise ValueError('GOAL_DELIVERY_UNCERTAIN: reconcile previous writes before revision')
        changed = jid in invalidated
        if confirmed_plan.get('delivery') == 'generate_only':
            delivered = delivered + [a for a in pending if a['eligibility_state'] == 'approved' and not changed
                                     and a['eligibility_revision'] == goal_snapshot['revision']]
            pending = [a for a in pending if a not in delivered]
        count = max(0, int(job['count']) - len(delivered))
        records.append({'job_id': jid, 'kind': job['kind'], 'requested': job['count'],
                        'delivered_before': len(delivered), 'execute_count': count,
                        'retained': [{**a, 'needs_review': True} for a in pending
                                     if a['eligibility_state'] in {'approved', 'generated', 'needs_review'}][:count]})
    manifest = {'schema_version': 'goal-resume.v1', 'goal_id': goal_snapshot['goal_id'],
                'namespace': goal_snapshot['namespace'], 'source_revision': goal_snapshot['revision'],
                'ledger_revision': goal_snapshot['ledger_revision'], 'plan_id': confirmed_plan['id'], 'jobs': records}
    return {**manifest, 'manifest_hash': digest(manifest)}


def import_revision(manifest, new_run_id, *, context=None, confirmed_jobs=None):
    body = {k: v for k, v in manifest.items() if k != 'manifest_hash'}
    if manifest.get('schema_version') != 'goal-resume.v1' or digest(body) != manifest.get('manifest_hash'):
        raise ValueError('GOAL_MANIFEST_INVALID')
    if context is not None or confirmed_jobs is not None:
        try:
            if context is None or confirmed_jobs is None:
                raise ValueError('GOAL_MANIFEST_INVALID: context and confirmed_jobs are required together')
            if (not all(context[key] for key in ('namespace', 'goal_id', 'active_plan_id'))
                    or manifest['namespace'] != context['namespace'] or manifest['goal_id'] != context['goal_id']
                    or manifest['plan_id'] != context['active_plan_id']
                    or type(context['revision']) is not int or type(manifest['source_revision']) is not int
                    or manifest['source_revision'] != context['revision'] - 1):
                raise ValueError('GOAL_MANIFEST_INVALID: goal ownership or revision mismatch')
            expected = [(job['job_id'], job['kind'], job['count']) for job in confirmed_jobs]
            actual = [(job['job_id'], job['kind'], job['requested']) for job in manifest['jobs']]
            if (not expected or actual != expected or len({job[0] for job in expected}) != len(expected)
                    or any(not job[0] or type(job[2]) is not int or job[2] < 1 for job in expected)):
                raise ValueError('GOAL_MANIFEST_INVALID: confirmed job order or count mismatch')
            for row in manifest['jobs']:
                if (any(type(row[key]) is not int or row[key] < 0
                        for key in ('requested', 'delivered_before', 'execute_count'))
                        or row['execute_count'] != max(0, row['requested'] - row['delivered_before'])
                        or len(row['retained']) > row['execute_count']):
                    raise ValueError('GOAL_MANIFEST_INVALID: remaining job count mismatch')
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError('GOAL_MANIFEST_INVALID: incomplete validation context') from exc
    records, completed = {}, []
    for index, row in enumerate(manifest['jobs']):
        if row['execute_count'] == 0:
            completed.append(index)
        records[str(index)] = {'status': 'completed' if row['execute_count'] == 0 else 'pending',
            'post_ids': [a['post_id'] for a in row['retained']], 'posts': [], 'reviewed_post_ids': [],
            'reviewed_posts': [], 'approved_versions': {}, 'review_complete': False, 'retryable': True,
            'context': {}, 'job_blocked': False}
    return {'job_states': records, 'completed_job_indices': completed,
            'job_index': next((i for i in range(len(records)) if i not in completed), len(records)),
            'plan_complete': True, 'controller_decision': {'summary': '沿用已确认修订，仅处理剩余任务'},
            'run_id': new_run_id}
