"""Associate messages with durable goals before invoking the existing executor."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re
import time
from uuid import NAMESPACE_URL, uuid4, uuid5

from src.agent.conversation_store import PostgresConversationStore
from src.agent.goal_store import GoalStore
from src.agent.plan_contract import PlanContractError, digest, normalize_plan
from apps.web_service import ACTIVE


def continuity_enabled(current):
    import os
    return isinstance(getattr(current, 'conversation_store', None), PostgresConversationStore) and os.getenv('AGENT_CONVERSATION_CONTINUITY', '1') != '0'


class ConversationService:
    def __init__(self, current):
        self.current = current
        self.namespace = current.conversation_store.namespace
        self.goals = GoalStore(current.conversation_store.knowledge_store)
        current.goal_store = self.goals

    def _legacy_run(self, saved, plan):
        from src.agent.plan_contract import verify_execution
        identity = plan.get('agent_run_id') or plan.get('job_id') or plan.get('execution_request_id')
        frozen = plan.get('frozen_execution')
        if not identity or identity not in saved.get('runs', []) or not frozen:
            return None
        try:
            verify_execution(frozen)
            if digest(frozen) != plan.get('frozen_execution_hash'):
                return None
            if [(j.get('job_id'), j.get('kind')) for j in frozen['jobs']] != [
                    (j.get('job_id'), j.get('kind')) for j in plan['jobs']]:
                return None
        except ValueError:
            return None
        return identity

    def _project_legacy(self, goal, plan, thread):
        from src.agent.artifact_store import AgentArtifactStore
        from src.agent.conversation_control import ConversationControl
        artifacts = AgentArtifactStore(self.goals.knowledge_store)
        records, uploaded = {}, []
        for index, job in enumerate(plan['jobs']):
            posts = artifacts.load(thread, str(index))
            records[str(index)] = {'posts': posts, 'reviewed_post_ids': []}
            # Receipts, not the historical uploaded flag, establish successful delivery.
            uploaded.extend(p.id for p in posts if (p.platform or {}).get('xhs_draft')
                            or (p.platform or {}).get('toutiao_draft'))
        control = ConversationControl({'goal_id': goal['goal_id'], 'namespace': self.namespace,
            'goal_revision': goal['revision'], 'attempt_id': goal['active_attempt_id'],
            'platform': plan.get('platform', 'xhs')}, store=self.goals)
        control.record_state({'run_id': thread, 'jobs': plan['jobs'], 'job_states': records,
                              'uploaded_post_ids': uploaded})
        self.goals.finish_legacy_projection(self.namespace, goal['goal_id'])

    def ensure_goals(self, cid):
        saved = self.current._read_agent_conversation(cid)
        rows = self.goals.list(self.namespace, cid)
        known = {g['active_plan_id'] for g in rows}
        plan_map = {p['id']: p for p in saved.get('plans', [])}
        for row in rows:
            parent = row['plan'].get('parent_plan_id') or (row['plan'].get('continuation') or {}).get('base_plan_id')
            while parent and parent not in known:
                known.add(parent)
                parent = plan_map.get(parent, {}).get('parent_plan_id')
        for plan in saved.get('plans', []):
            if not plan.get('jobs') or plan.get('plan_kind') == 'draft_management' or plan.get('continuation'):
                continue
            if plan['id'] not in known:
                # Earlier editable revisions belong to their latest plan, not new goals.
                if any(p.get('parent_plan_id') == plan['id'] for p in saved['plans']):
                    continue
                if plan.get('parent_plan_id') in known and not plan.get('copied_from_plan_id'):
                    continue
                executed = plan.get('job_id') or plan.get('execution_request_id')
                thread = self._legacy_run(saved, plan) if executed else None
                if executed and not thread:
                    continue
                row = self.goals.create(self.namespace, cid, plan)
                if executed:
                    self.goals.bind_attempt(self.namespace, row['goal_id'], plan.get('resume_job_id') or plan.get('job_id') or plan['execution_request_id'])
                    row = self.goals.snapshot(self.namespace, row['goal_id'])
                    self._project_legacy(row, plan, thread)
                rows.append(self.goals.snapshot(self.namespace, row['goal_id']))
            else:
                row = next((g for g in rows if g['active_plan_id'] == plan['id']), None)
                if row is None:
                    continue
                if not row['active_attempt_id'] and (plan.get('job_id') or plan.get('execution_request_id')):
                    self.goals.bind_attempt(self.namespace, row['goal_id'], plan.get('resume_job_id') or plan.get('job_id') or plan['execution_request_id'])
                if row.get('legacy_projection_pending'):
                    thread = self._legacy_run(saved, plan)
                    if thread:
                        self._project_legacy(self.goals.snapshot(self.namespace, row['goal_id']), plan, thread)
        rows = self.goals.list(self.namespace, cid)
        return rows

    def goal_for_plan(self, cid, plan):
        rows = self.ensure_goals(cid)
        linked = (plan.get('continuation') or {}).get('goal_id')
        goal = next((g for g in rows if g['goal_id'] == linked or g['active_plan_id'] == plan['id']), None)
        if goal is None and plan.get('parent_plan_id') and not plan.get('copied_from_plan_id'):
            saved = self.current._read_agent_conversation(cid)
            plans = {p['id']: p for p in saved['plans']}
            parent = plan.get('parent_plan_id')
            while parent and goal is None:
                goal = next((g for g in rows if g['active_plan_id'] == parent), None)
                parent = plans.get(parent, {}).get('parent_plan_id')
        if goal is None:
            return self.goals.create(self.namespace, cid, plan)
        if goal['active_plan_id'] != plan['id']:
            return self.goals.revise(self.namespace, goal['goal_id'], plan, expected_revision=goal['revision'])
        return goal

    def _job(self, goal):
        identity = goal.get('active_attempt_id')
        if not identity:
            return None
        return self.current.jobs.get(identity) or self._journal(identity)

    def _journal(self, identity):
        from apps.web_service import read_json, valid_id
        return read_json(self.current.directory / 'jobs' / (valid_id(identity) + '.json'), {})

    def _message(self, cid, turn, text):
        saved = self.current._read_agent_conversation(cid)
        mid = turn['source_message_id']
        user = next((m for m in saved['messages'] if m['id'] == mid), None)
        aid = uuid5(NAMESPACE_URL, turn['turn_id'] + ':assistant').hex
        assistant = next((m for m in saved['messages'] if m['id'] == aid), None)
        if not user:
            user = {'id': mid, 'role': 'user', 'content': turn['command_payload']['content'], 'created_at': time.time(), 'turn_id': turn['turn_id']}
            saved['messages'].append(user)
        if not assistant:
            assistant = {'id': aid, 'role': 'assistant', 'content': text, 'created_at': time.time(), 'turn_id': turn['turn_id']}
            saved['messages'].append(assistant)
            saved['updated_at'] = time.time()
            self.current._write_agent_conversation(saved)
        return user, assistant

    def _notice(self, cid, identity, text):
        saved = self.current._read_agent_conversation(cid)
        message_id = uuid5(NAMESPACE_URL, identity).hex
        existing = next((m for m in saved['messages'] if m['id'] == message_id), None)
        if existing:
            return existing
        message = {'id': message_id, 'role': 'assistant', 'content': text, 'created_at': time.time()}
        saved['messages'].append(message)
        saved['updated_at'] = time.time()
        self.current._write_agent_conversation(saved)
        return message

    @staticmethod
    def progress_text(goal):
        pieces = []
        for job in goal['jobs']:
            count = job['approved_current'] if goal['plan'].get('delivery') == 'generate_only' else job['delivered_verified']
            pieces.append(f"{job['title']} {count}/{job['requested']}，待上传{job['retained_ready']}，尚需生成{job['new_generation_needed']}")
        return '；'.join(pieces)

    def handle_message(self, conversation_id, request, request_key):
        try:
            return self._route_message(conversation_id, request, request_key)
        except ValueError as exc:
            turn = self.goals.find_turn(self.namespace, conversation_id, request_key)
            if turn and turn['state'] in {'accepted', 'applying'}:
                self._message(conversation_id, turn, '未执行：' + str(exc))
                self.goals.update_turn(self.namespace, turn['turn_id'], state='rejected',
                    result={'code': getattr(exc, 'code', 'MESSAGE_INVALID'), 'error': self.current.redact(str(exc)),
                            'status': getattr(exc, 'status', 400)})
            raise

    def _route_message(self, conversation_id, request, request_key):
        cid = conversation_id
        content = str(request['content']).strip()
        existing = self.goals.find_turn(self.namespace, cid, request_key)
        request_digest = digest(request)
        if existing:
            if existing['command_payload'].get('request_digest') != request_digest:
                raise PlanContractError('REQUEST_KEY_CONFLICT', '同一请求键不能用于另一条消息', status=409)
            if existing['result'].get('response'):
                return existing['result']['response']
            if existing['state'] == 'rejected':
                result = existing['result']
                raise PlanContractError(result.get('code', 'MESSAGE_INVALID'), result.get('error', '消息需要修正'), status=result.get('status', 400))
            turn = existing
        else:
            mid = uuid5(NAMESPACE_URL, cid + ':' + (request.get('client_message_id') or request_key)).hex
            turn = self.goals.enqueue_turn(self.namespace, cid, mid,
                {'kind': 'route', 'content': content, 'request': deepcopy(request),
                 'request_digest': request_digest, 'state': 'accepted'}, None, request_key)
        claim = self.goals.claim_turn(self.namespace, turn['turn_id'], uuid4().hex)
        if not claim:
            return {'turn': self.goals.turn(self.namespace, turn['turn_id']), 'action': 'accepted', 'plan': None, 'run': None, 'goal': None}
        turn = claim
        rows = self.ensure_goals(cid)
        new_intent = re.sub(r'(?:不要|不用|不必|无需|禁止)\s*(?:再)?\s*(?:另开(?:新任务|一批)?|新一批|新建任务|重新开始)', '', content)
        explicit_new = bool(re.search(r'另开|新一批|新建任务|重新开始', new_intent))
        selected = next((g for g in rows if g['goal_id'] == request.get('goal_id')), None)
        if request.get('goal_id') and selected is None:
            raise PlanContractError('GOAL_NOT_FOUND', '目标不属于当前对话', status=404)
        candidates = [g for g in rows if g['state'] not in {'completed', 'cancelled'}]
        goal = selected or (candidates[0] if len(candidates) == 1 else rows[-1] if len(rows) == 1 else None)
        if goal and request.get('expected_goal_revision') not in (None, goal['revision']):
            raise PlanContractError('GOAL_REVISION_CONFLICT', '目标要求已更新，请刷新核对', status=409)
        if goal and not explicit_new and request.get('expected_attempt_id') not in (None, goal['active_attempt_id']):
            raise PlanContractError('GOAL_ATTEMPT_CONFLICT', '活动执行已变化，请刷新后核对', status=409)
        continuation = bool(re.search(r'继续|补齐|剩下|剩余|接着|恢复', content))
        explicit_new = explicit_new or (not continuation and bool(re.match(r'^(?:请|帮我|今天|今日|现在|为我|\s)*(?:生成|制作)', content)))
        control = 'cancel_goal' if re.search(r'取消(?:任务|这个|当前)?|放弃(?:任务|这个|当前)?', content) else 'pause_goal' if re.search(r'暂停|先不要上传|停止(?:任务|上传|生成)', content) else ''
        amendment = bool(re.search(r'改成|改为|修改|门槛|只供参考|只做参考|仅供参考|额外|增加|删除|删掉|(?:不要|不再|禁止|不需要|不|先不|暂不)上传|只生成|仅生成', content))
        from backend.progress import is_status_question
        status = is_status_question(content) or bool(re.search(r'到哪了|进度|完成几|还差几', content))
        action, run, plan = 'clarify', None, None
        initial_response = None
        reply = ''
        if explicit_new or (not rows and not continuation and not control and not status):
            parsed = self.current._parse_agent_message(content)
            if parsed.get('plan_kind') == 'draft_management':
                if (parsed.get('management') or {}).get('mode') == 'publish':
                    raise PlanContractError('PUBLIC_PUBLISH_UNAVAILABLE', '独立界面暂不执行公开发布，请在平台人工确认')
                return self.current.append_agent_message(cid, content)
            result = self.current.append_agent_message(cid, content, message_id=turn['source_message_id'],
                                                       plan_id=uuid5(NAMESPACE_URL, turn['turn_id'] + ':plan').hex)
            initial_response = result
            plan = result['plan']
            if plan.get('jobs'):
                goal = self.goals.create(self.namespace, cid, plan)
                action = 'new_goal'
            reply = result['assistant']['content']
        elif not goal:
            reply = ('请选择要继续的目标；当前有多个目标。' if rows else
                     '旧任务归属或冻结证据不足，仅只读保留，请核对后另建明确任务。' if
                     any(p.get('job_id') or p.get('execution_request_id') for p in self.current._read_agent_conversation(cid)['plans']) else
                     '请先说明要生成的栏目和数量，建立任务后即可发送“继续”。')
        elif re.search(r'再来\s*[0-9一二三四五六七八九十]+', content) and not re.search(r'额外|增加|补齐', content):
            reply = '“再来”是补齐原目标，还是额外增加？请明确后继续。'
        elif control:
            action = control
            reply = '已收到，等待当前操作结束后' + ('取消任务。' if control == 'cancel_goal' else '暂停后续生成和上传。')
            command = {**turn['command_payload'], 'kind': action, 'goal_id': goal['goal_id']}
            turn = self.goals.update_turn(self.namespace, turn['turn_id'], state='queued', command=command, goal_id=goal['goal_id'])
            if not (self._job(goal) and self._job(goal).get('status') in ACTIVE):
                self.goals.set_state(self.namespace, goal['goal_id'],
                    'cancelled' if control == 'cancel_goal' else 'paused', expected_epoch=goal['lease_epoch'])
        elif amendment:
            return self._amend(cid, turn, goal)
        elif continuation:
            action = 'continue_goal'
            job = self._job(goal)
            pending = [t for t in self.goals.turns(self.namespace, cid) if t['goal_id'] == goal['goal_id']
                       and t['command_payload'].get('kind') == 'amend_goal'
                       and t['state'] in {'awaiting_confirmation', 'needs_input', 'queued', 'applying', 'interrupted'}]
            today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
            target_date = goal.get('target_date') or ''
            cross_day = target_date != today
            date_confirmed = bool(target_date and target_date in content and '确认' in content and '原目标' in content)
            if pending:
                latest = pending[-1]
                saved = self.current._read_agent_conversation(cid)
                plan = next((p for p in reversed(saved['plans'])
                             if (p.get('continuation') or {}).get('turn_id') == latest['turn_id']), None)
                if latest['command_payload'].get('confirmed') and (not cross_day or date_confirmed):
                    pauses = [item for item in self.goals.turns(self.namespace, cid)
                              if item['goal_id'] == goal['goal_id'] and item['kind'] == 'pause_goal'
                              and item['state'] in {'queued', 'applying', 'applied'}
                              and item['accepted_at'] > latest['accepted_at']]
                    if pauses:
                        latest = self.goals.update_turn(self.namespace, latest['turn_id'], state=latest['state'],
                            command={**latest['command_payload'], 'resume_after_pause_id': pauses[-1]['turn_id']})
                    if latest['state'] == 'interrupted':
                        self.goals.update_turn(self.namespace, latest['turn_id'], state='queued')
                    handoff = self.apply_confirmed_turn(cid, latest['turn_id'])
                    run, goal = handoff.get('run'), handoff.get('goal', goal)
                    reply = handoff.get('assistant', {}).get('content') or '已确认修改正在交接，请等待。'
                else:
                    reply = '有尚未确认或修正的修改，请先确认或放弃候选；未恢复旧版。'
            elif cross_day and not date_confirmed:
                reply = f'原目标日期为{target_date or "未知"}，不计入今日新批次；请核对时效并发送“确认继续{target_date}的原目标”，或另开今日新批次。'
            elif goal['state'] == 'cancelled':
                reply = '目标已取消，不会恢复；请另开明确的新任务。'
            elif job and job.get('status') in ACTIVE:
                reply = '原任务正在执行。' + self.progress_text(goal)
            elif goal['jobs'] and all(j['remaining'] == 0 for j in goal['jobs']):
                reply = '该目标已完成，无需重复生成。' + self.progress_text(goal)
            elif not job:
                reply = '原计划尚未确认执行，请先确认计划。'
                plan = goal['plan']
            else:
                # The durable turn exists before starting. The same key resumes the same request.
                if date_confirmed:
                    self.goals.update_turn(self.namespace, turn['turn_id'], state='applying', goal_id=goal['goal_id'],
                        command={**turn['command_payload'], 'kind': 'continue_goal', 'goal_id': goal['goal_id'],
                                 'confirmed_target_date': target_date})
                run = self.current.resume_agent_run(cid, job['id'], 'turn-' + turn['turn_id'])
                reply = '沿用原计划继续。' + self.progress_text(goal)
        elif status:
            action = 'status'
            reply = self.progress_text(goal)
        else:
            reply = '请说明是继续原任务、修改剩余要求，还是另开一批；原计划已保留。'
        if initial_response is not None:
            message, assistant = initial_response['message'], initial_response['assistant']
        else:
            message, assistant = self._message(cid, turn, reply)
        response = {'message': message, 'assistant': assistant, 'plan': plan, 'run': run,
                    'action': action, 'goal': self.goals.snapshot(self.namespace, goal['goal_id']) if goal else None}
        pending = action in {'pause_goal', 'cancel_goal'} and bool(self._job(goal) and self._job(goal).get('status') in ACTIVE)
        turn = self.goals.update_turn(self.namespace, turn['turn_id'], state='queued' if pending else 'applied',
                    command={**turn['command_payload'], 'kind': action, 'goal_id': goal['goal_id'] if goal else None},
                    goal_id=goal['goal_id'] if goal else None, result={'response': response})
        response['turn'] = turn
        # Store a compact response without recursively embedding its own turn result.
        self.goals.update_turn(self.namespace, turn['turn_id'], state=turn['state'], result={'response': deepcopy(response)})
        return self.current.redact(response)

    def _amend(self, cid, turn, goal):
        from backend.plan_service import PlanService
        from src.agent.plan_contract import apply_edits, KINDS
        text = turn['command_payload']['content']
        saved = self.current._read_agent_conversation(cid)
        base = next(p for p in saved['plans'] if p['id'] == goal['active_plan_id'])
        fields = {}
        clauses = re.split(r'[，,。；;\n]', text)
        for clause in clauses:
            if re.search(r'图片|配图|评分|门槛', clause) and re.search(r'只供参考|只做参考|仅供参考|必须|强制|关闭|开启|不再|不要', clause):
                parsed = self.current._parse_agent_message(clause)
                fields['image_score_required'] = (False if re.search(r'只供参考|只做参考|仅供参考', clause)
                    else parsed.get('image_score_required', base.get('image_score_required', True)))
        mentioned = [kind for kind, title in KINDS.items() if title in text or (kind == 'daily_ai_digest' and 'AI资讯' in text)]
        selected = [j for j in base['jobs'] if j['kind'] in mentioned]
        if not selected:
            selected = [j for j in base['jobs'] if j['kind'] == 'daily_news'] or base['jobs'][:1]
        additional = re.search(r'(?:额外(?:再)?(?:增加)?|增加)\s*(\d+)\s*条', text)
        total = re.search(r'(?:总数|总共|改为|改成|减少到|减到)\s*(\d+)\s*条', text)
        theme = None
        unsupported_model = any(re.search(r'模型.{0,12}(?:改成|改为|换成|换为|切换)', clause) for clause in clauses)
        for clause in clauses:
            candidate = re.search(
                r'^(?:剩下(?:的)?(?:内容|任务)?|剩余(?:的)?(?:内容|任务)?|(?:每日)?新闻(?:的)?(?:选题|主题|内容)?|选题|主题)'
                r'\s*(?:改成|改为)\s*(.+)$', clause.strip())
            if candidate and not re.match(
                    r'(?:只|仅)?(?:保存|生成|上传)|不要上传|(?:只供|只做|仅供)参考|(?:图片|配图|门槛|评分|模型|交付|速度|预算)',
                    candidate.group(1)):
                theme = candidate
                break
        job_edits = []
        for job in base['jobs']:
            edit = {'target_job_id': job['job_id']}
            if job in selected:
                if additional:
                    edit['count'] = job['count'] + int(additional.group(1))
                elif total:
                    edit['count'] = int(total.group(1))
                if theme and not total:
                    value = theme.group(1).strip()
                    edit.update(search_keywords=[value], topic_preferences=[value])
            job_edits.append(edit)
        if additional or total or theme:
            fields['jobs'] = job_edits
        if re.search(r'只生成|仅生成|(?:不要|不再|禁止|不需要|不|先不|暂不)上传', text):
            fields['delivery'] = 'generate_only'
        elif '草稿' in text:
            fields['delivery'] = 'save_draft'
        plan = apply_edits(base, fields)
        diff = [{'field': name, 'before': base.get(name), 'after': plan.get(name)}
                for name in fields if base.get(name) != plan.get(name)]
        needs_input = unsupported_model or not diff or not plan['executable']
        plan.update(source_message_id=turn['source_message_id'], source_text_hash=digest(text),
                    continuation={'goal_id': goal['goal_id'], 'turn_id': turn['turn_id'],
                        'expected_goal_revision': goal['revision'], 'base_plan_id': base['id'], 'scope': 'remaining',
                        'diff': diff})
        saved = self.current._read_agent_conversation(cid)
        if not any(m['id'] == turn['source_message_id'] for m in saved['messages']):
            saved['messages'].append({'id': turn['source_message_id'], 'role': 'user', 'content': text, 'created_at': time.time()})
        service = PlanService(self.current)
        key, request_hash = 'amend-' + turn['turn_id'], digest([turn['turn_id'], fields])
        result = service._receipt(saved, key, request_hash) or service._append(
            saved, base, plan, key, request_hash, '已整理剩余任务修订')
        plan = result['plan']
        if needs_input:
            saved = self.current._read_agent_conversation(cid)
            stored = next(p for p in saved['plans'] if p['id'] == plan['id'])
            issue = {'code': 'AMENDMENT_NEEDS_INPUT', 'field': 'model_roles' if unsupported_model else 'continuation.diff',
                     'message': '模型修改尚未解析，请编辑模型字段或手动校准后再确认。' if unsupported_model
                                else '修改尚未解析为有效差异，请编辑或手动校准后再确认。'}
            stored.update(executable=False, status='needs_input',
                          field_errors=stored.get('field_errors', []) + [issue])
            self.current._write_agent_conversation(saved)
            plan = deepcopy(stored)
        response = {'message': next(m for m in self.current._read_agent_conversation(cid)['messages'] if m['id'] == turn['source_message_id']),
            'assistant': self.current._read_agent_conversation(cid)['messages'][-1],
            'plan': plan, 'run': None, 'goal': goal, 'action': 'amend_goal'}
        command = {**turn['command_payload'], 'kind': 'amend_goal', 'goal_id': goal['goal_id'],
                   'expected_goal_revision': goal['revision'], 'expected_attempt_id': goal['active_attempt_id'],
                   'proposal_plan_id': plan['id']}
        pending = self.goals.update_turn(self.namespace, turn['turn_id'], state='awaiting_confirmation',
                     command=command, goal_id=goal['goal_id'], proposal_plan_id=plan['id'], result={'response': response})
        response['turn'] = pending
        self.goals.update_turn(self.namespace, turn['turn_id'], state='awaiting_confirmation', result={'response': deepcopy(response)})
        return self.current.redact(response)

    def confirm_turn(self, conversation_id, turn_id, expected_goal_revision, proposal_hash, request_key):
        turn = self.goals.turn(self.namespace, turn_id)
        if turn['conversation_id'] != conversation_id:
            raise PlanContractError('TURN_NOT_FOUND', '交互不属于当前对话', status=404)
        if turn['state'] == 'cancelled':
            raise PlanContractError('TURN_DISCARDED', '此候选已放弃，原计划保持不变', status=409)
        if turn['command_payload'].get('kind') != 'amend_goal':
            raise PlanContractError('TURN_NOT_CONFIRMABLE', '此交互无需确认')
        confirmation = digest([request_key, expected_goal_revision, proposal_hash])
        previous = turn['command_payload'].get('confirmation_hash')
        if previous:
            if previous != confirmation:
                raise PlanContractError('TURN_CONFIRM_CONFLICT', '已按另一组修改确认，请刷新核对', status=409)
            if turn['result'].get('confirmed_response'):
                return turn['result']['confirmed_response']
        saved = self.current._read_agent_conversation(conversation_id)
        plan = next((p for p in reversed(saved['plans']) if (p.get('continuation') or {}).get('turn_id') == turn_id), None)
        if not plan or plan.get('semantic_hash') != proposal_hash:
            raise PlanContractError('PLAN_HASH_CONFLICT', '候选内容已更新，请核对最新计划后确认', status=409)
        if any(error.get('code') == 'AMENDMENT_NEEDS_INPUT' for error in plan.get('field_errors', [])):
            raise PlanContractError('AMENDMENT_NEEDS_INPUT', '修改尚待补充，请编辑或校准后确认', status=409)
        goal = self.goals.snapshot(self.namespace, turn['goal_id'])
        already_revised = (previous and goal['active_plan_id'] == plan['id']
                           and goal['revision'] == expected_goal_revision + 1)
        if not already_revised and plan.get('semantic_hash') == normalize_plan(goal['plan'])['semantic_hash']:
            raise PlanContractError('AMENDMENT_NEEDS_INPUT', '尚无有效修改差异，请编辑或校准后确认', status=409)
        if expected_goal_revision != goal['revision'] and not (goal['active_plan_id'] == plan['id'] and goal['revision'] == expected_goal_revision + 1):
            raise PlanContractError('GOAL_REVISION_CONFLICT', '目标已修改，请刷新合并', status=409)
        from backend.plan_service import PlanService
        if not PlanService(self.current).validate_models(plan)['executable']:
            raise PlanContractError('PLAN_NEEDS_INPUT', '请修正候选字段后再确认', status=409)
        command = {**turn['command_payload'], 'confirmed': True, 'confirmation_hash': confirmation,
                   'confirmation_key': request_key, 'proposal_plan_id': plan['id'], 'proposal_hash': proposal_hash}
        if not previous or turn['state'] == 'interrupted':
            self.goals.update_turn(self.namespace, turn_id, state='queued', command=command, proposal_plan_id=plan['id'])
        return self.apply_confirmed_turn(conversation_id, turn_id)

    def discard_turn(self, cid, turn_id):
        turn = self.goals.turn(self.namespace, turn_id)
        if turn['conversation_id'] != cid:
            raise PlanContractError('TURN_NOT_FOUND', '交互不属于当前对话', status=404)
        if turn['state'] not in {'awaiting_confirmation', 'needs_input', 'cancelled'}:
            raise PlanContractError('TURN_ALREADY_CONFIRMED', '候选已确认或正在应用，请使用暂停任务', status=409)
        turn = self.goals.update_turn(self.namespace, turn_id, state='cancelled')
        saved = self.current._read_agent_conversation(cid)
        for plan in saved['plans']:
            if (plan.get('continuation') or {}).get('turn_id') == turn_id:
                plan['status'] = 'discarded'
        saved['updated_at'] = time.time()
        self.current._write_agent_conversation(saved)
        return {'turn': turn, 'goal': self.goals.snapshot(self.namespace, turn['goal_id']),
                'plan': None, 'run': None, 'action': 'discard_amendment',
                'assistant': self._notice(cid, turn_id + ':discard', '已放弃候选，原计划与已有产物已保留。')}

    def apply_confirmed_turn(self, cid, turn_id):
        turn = self.goals.turn(self.namespace, turn_id)
        if turn['conversation_id'] != cid or not turn['command_payload'].get('confirmed'):
            raise PlanContractError('TURN_NOT_CONFIRMABLE', '交互尚未确认或不属于当前对话', status=409)
        if turn['state'] == 'applied' and turn['result'].get('confirmed_response'):
            return turn['result']['confirmed_response']
        from src.agent.artifact_store import AgentArtifactStore
        key = 'goal-' + digest([self.namespace, turn['goal_id']])[:48]
        try:
            with AgentArtifactStore(self.goals.knowledge_store).lease(key) as assert_alive:
                return self._apply_confirmed_turn_locked(cid, turn_id, assert_alive)
        except RuntimeError as exc:
            if not str(exc).startswith('AGENT_RUN_ALREADY_ACTIVE:'):
                raise
            return {'turn': self.goals.turn(self.namespace, turn_id),
                    'goal': self.goals.snapshot(self.namespace, turn['goal_id']),
                    'plan': None, 'run': None, 'action': 'amend_goal',
                    'assistant': {'role': 'assistant', 'content': '该目标正在交接，请等待已有交接完成。'}}

    def _apply_confirmed_turn_locked(self, cid, turn_id, assert_alive):
        assert_alive()
        turn = self.goals.turn(self.namespace, turn_id)
        goal = self.goals.snapshot(self.namespace, turn['goal_id'])
        controls = [item for item in self.goals.turns(self.namespace, cid)
                    if item['goal_id'] == goal['goal_id'] and item['kind'] in {'cancel_goal', 'pause_goal'}
                    and item['state'] in {'queued', 'applying', 'applied'} and item['accepted_at'] > turn['accepted_at']]
        cancelled = goal['state'] == 'cancelled' or any(item['kind'] == 'cancel_goal' for item in controls)
        if cancelled:
            turn = self.goals.update_turn(self.namespace, turn_id, state='cancelled')
            return {'turn': turn, 'goal': goal, 'plan': None, 'run': None, 'action': 'amend_goal',
                    'assistant': self._notice(cid, turn_id + ':cancelled', '目标已取消，原排队修订不会启动。')}
        pauses = [item for item in controls if item['kind'] == 'pause_goal']
        if pauses and turn['command_payload'].get('resume_after_pause_id') != pauses[-1]['turn_id']:
            turn = self.goals.update_turn(self.namespace, turn_id, state='interrupted',
                result={'code': 'GOAL_PAUSED_AFTER_CONFIRMATION', 'error': '后续暂停仍有效，等待明确继续'})
            return {'turn': turn, 'goal': goal, 'plan': None, 'run': None, 'action': 'amend_goal',
                    'assistant': self._notice(cid, turn_id + ':paused', '目标已暂停，排队修订需要明确继续后才会启动。')}
        job = self._job(goal)
        if self.current.process is not None or any(j.get('status') in ACTIVE for j in self.current.jobs.values()):
            return {'turn': turn, 'goal': goal, 'plan': None, 'run': None, 'action': 'amend_goal',
                    'assistant': self._notice(cid, turn_id + ':queued', '修改已确认，等待当前操作安全结束后继续。')}
        claim = self.goals.claim_turn(self.namespace, turn_id, uuid4().hex)
        if not claim:
            return turn['result'].get('confirmed_response') or {'turn': turn, 'goal': goal, 'run': None, 'plan': None, 'action': 'amend_goal'}
        try:
            saved = self.current._read_agent_conversation(cid)
            plan = next(p for p in saved['plans'] if p['id'] == turn['proposal_plan_id'])
            if (plan.get('semantic_hash') != turn['command_payload'].get('proposal_hash')
                    or saved['plans'][-1]['id'] != plan['id']):
                raise PlanContractError('PLAN_HASH_CONFLICT', '候选在排队后已更新，请核对最新计划并重新确认', status=409)
            expected = turn['command_payload']['expected_goal_revision']
            already_revised = goal['active_plan_id'] == plan['id'] and goal['revision'] == expected + 1
            if not already_revised and goal['revision'] != expected:
                raise PlanContractError('GOAL_REVISION_CONFLICT', '排队修订已过期，请核对最新目标后重新确认', status=409)
            if (not already_revised and turn['command_payload'].get('expected_attempt_id', goal['active_attempt_id'])
                    != goal['active_attempt_id']):
                raise PlanContractError('GOAL_ATTEMPT_CONFLICT', '活动执行已变化，请重新确认', status=409)
            from src.agent.conversation_control import build_resume_manifest
            if goal['active_plan_id'] != plan['id']:
                # The original run lease proves there is no in-flight old graph.
                from src.agent.artifact_store import AgentArtifactStore
                thread = self.current.agent_checkpoint_id(job['id'], saved) if job else ''
                from contextlib import nullcontext
                with AgentArtifactStore(self.goals.knowledge_store).lease(thread) if thread else nullcontext():
                    assert_alive()
                    manifest = build_resume_manifest(self.goals.snapshot(self.namespace, goal['goal_id']), plan)
                    plan['revision_manifest'] = manifest
                    saved['updated_at'] = time.time()
                    self.current._write_agent_conversation(saved)
                    goal = self.goals.revise(self.namespace, goal['goal_id'], plan, expected_revision=expected,
                                             expected_attempt_id=goal['active_attempt_id'])
            from backend.plan_service import PlanService
            assert_alive()
            run = PlanService(self.current).confirm(cid, plan['id'], {'conversation_id': cid, 'version': plan['version']},
                                                   'handoff-' + turn_id)
            goal = self.goals.snapshot(self.namespace, goal['goal_id'])
            response = {'turn': {**turn, 'state': 'applied'}, 'goal': goal, 'plan': plan, 'run': run,
                        'action': 'amend_goal', 'assistant': self._notice(cid, turn_id + ':applied', '新修订已生效，保留已交付内容并继续剩余任务。')}
            assert_alive()
            self.goals.record_result(claim, {'state': 'applied', 'confirmed_response': response,
                'attempt_id': run['id'], 'agent_run_id': run.get('agent_run_id', run['id']),
                'resume_of': job['id'] if job else '', 'plan_id': plan['id']})
            return self.current.redact(response)
        except Exception as exc:
            busy = str(exc).startswith('AGENT_RUN_ALREADY_ACTIVE:')
            conflict = getattr(exc, 'code', '') in {'GOAL_REVISION_CONFLICT', 'GOAL_ATTEMPT_CONFLICT', 'PLAN_HASH_CONFLICT'}
            self.goals.record_result(claim, {'state': 'queued' if busy else 'rejected' if conflict else 'interrupted',
                                            'code': getattr(exc, 'code', 'GOAL_HANDOFF_INTERRUPTED'),
                                            'error': self.current.redact(str(exc))})
            if busy:
                return {'turn': self.goals.turn(self.namespace, turn_id), 'goal': goal, 'plan': None, 'run': None,
                        'action': 'amend_goal', 'assistant': {'role': 'assistant', 'content': '等待原执行安全结束后继续交接。'}}
            raise

    def process_pending(self):
        for turn in self.goals.pending_turns(self.namespace):
            if turn['kind'] == 'route':
                request = turn['command_payload'].get('request')
                if request:
                    self.handle_message(turn['conversation_id'], request, turn['request_key'])
            elif turn['command_payload'].get('confirmed'):
                self.apply_confirmed_turn(turn['conversation_id'], turn['turn_id'])
