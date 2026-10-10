"""Append-only plan revisions; the conversation CAS is the cross-process boundary."""
from copy import deepcopy
from contextlib import nullcontext
import time
import json
from uuid import uuid4

from src.agent.conversation_store import ConversationConflict, PostgresConversationStore
from src.agent.plan_contract import (JOB_EDIT_FIELDS, OPTION_FIELDS, EDITABLE_OPTIONS, PlanContractError,
                                    apply_edits, digest, field_path, normalize_plan, execution_fields, verify_execution)
from src.agent.plan_contract import suggestion_value_valid
from src.model_platforms.security import PlatformError, file_lock


class PlanService:
    def __init__(self, current):
        self.current = current

    def current_plan(self, cid):
        saved = self.current._read_agent_conversation(cid)
        if not saved.get('plans'):
            raise PlanContractError('PLAN_NOT_FOUND', '尚无任务计划', status=404)
        return self.describe(saved['plans'][-1])

    def configuration_fingerprint(self):
        from backend.capabilities import manager
        env = self.current.environment()
        names = ('LLM_PROVIDER', 'IMAGE_PROVIDER', 'AGENT_LLM_PROVIDER', 'MINIMAX_LLM_MODEL',
                 'MINIMAX_IMAGE_MODEL', 'MINIMAX_BILLING_MODE', 'MINIMAX_ALLOW_PAYGO',
                 'MINIMAX_ALLOW_PAID_CREDITS', 'ALLOW_PAID_LLM_FALLBACK',
                 'XHS_CHROME_USER_DATA_DIR', 'XHS_CHROME_PROFILE', 'TOUTIAO_CHROME_USER_DATA_DIR',
                 'TOUTIAO_CHROME_PROFILE', 'XHS_BROWSER_CHANNEL', 'TOUTIAO_BROWSER_CHANNEL', 'MODEL_PLATFORMS_NAMESPACE')
        value = {'settings': self.current.settings(), 'bindings': self.current.providers().get('bindings', {}),
                 'environment': {name: env.get(name, '') for name in names},
                 'models': sorted(({name: row.get(name) for name in
                                   ('id', 'kind', 'provider', 'model', 'selectable', 'cost_class', 'revision', 'version')}
                                  for row in self.current.models()['rows']), key=lambda row: row['id'])}
        platform_state = self.current.model_platforms().state()
        value['platform_configuration_revision'] = platform_state['revision']
        value['provider_endpoints'] = {name: item for name, item in env.items()
                                       if name.endswith(('_BASE_URL', '_MODEL'))}
        value['capabilities'] = [{key: row.get(key) for key in ('id','revision','revoked_at')}
                                 for row in manager(self.current).store.resources()
                                 if row.get('kind') in {'tool','mcp','skill','memory','policy'}]
        return digest(value)

    def _model_runtime(self, plan):
        from src.model_platforms.integration import checkpoint_models, freeze_run_environment
        roles = plan['model_roles']
        env = self.current.freeze_model_roles(self.current.environment(), {
            'kind':'agent', 'agent_id':roles['agent'], 'llm_id':roles['writer'], 'image_id':roles['image']})
        env.update(freeze_run_environment(env))
        return checkpoint_models(env)

    def _host_environment(self):
        env = self.current.environment()
        allowed = {'LLM_PROVIDER','IMAGE_PROVIDER','AGENT_LLM_PROVIDER','MINIMAX_BILLING_MODE',
                   'MINIMAX_ALLOW_PAYGO','MINIMAX_ALLOW_PAID_CREDITS','ALLOW_PAID_LLM_FALLBACK',
                   'XHS_CHROME_USER_DATA_DIR','XHS_CHROME_PROFILE','AGENT_CAPABILITY_NAMESPACE'}
        value = {key:item for key,item in env.items() if key in allowed or key.endswith(('_MODEL','_BASE_URL'))}
        value.update(TOUTIAO_CHROME_USER_DATA_DIR=env.get('TOUTIAO_CHROME_USER_DATA_DIR') or env.get('XHS_CHROME_USER_DATA_DIR',''),
                     TOUTIAO_CHROME_PROFILE=env.get('TOUTIAO_CHROME_PROFILE') or env.get('XHS_CHROME_PROFILE',''),
                     XHS_BROWSER_CHANNEL=env.get('XHS_BROWSER_CHANNEL') or 'chrome',
                     TOUTIAO_BROWSER_CHANNEL=env.get('TOUTIAO_BROWSER_CHANNEL') or env.get('XHS_BROWSER_CHANNEL') or 'chrome',
                     XHS_CDP_URL='',TOUTIAO_CDP_URL='',TOUTIAO_AUTO_ATTACH_CDP='0')
        return value

    def describe(self, plan):
        result = self.validate_models(plan)
        result['configuration_fingerprint'] = self.configuration_fingerprint()
        return result

    def _receipt(self, saved, key, request_hash):
        if not isinstance(key, str) or not key or len(key) > 120:
            raise PlanContractError('REQUEST_KEY_INVALID', '需要至多120字符的请求键')
        receipt = saved.get('plan_requests', {}).get(key)
        if receipt:
            if receipt['request_hash'] != request_hash:
                raise PlanContractError('PLAN_REQUEST_CONFLICT', '同一请求键不能用于不同修改', status=409)
            plan = next(p for p in saved['plans'] if p['id'] == receipt['plan_id'])
            return self.current.redact({'plan': self.describe(plan), 'conversation_revision': receipt['revision']})
        return None

    def _base(self, saved, pid, body):
        plan = saved.get('plans', [])[-1] if saved.get('plans') else {}
        if plan.get('id') != pid or plan.get('version') != body['base_plan_version']:
            raise PlanContractError('TASK_PLAN_CONFLICT', '计划已在其他页面更新，当前编辑内容已保留；请刷新后合并', status=409)
        if plan.get('job_id') or plan.get('resume_job_id') or plan.get('execution_request_id'):
            raise PlanContractError('PLAN_FROZEN', '已执行或正在提交的计划不可修改，请复制为新计划', status=409)
        if body.get('conversation_revision') is not None and body['conversation_revision'] != saved.get('_revision', 0):
            raise PlanContractError('CONVERSATION_CONFLICT', '对话已更新，当前编辑内容已保留；请刷新后合并', status=409)
        if plan.get('plan_kind') == 'draft_management':
            raise PlanContractError('PLAN_NOT_EDITABLE', '草稿管理继续使用原有确认流程')
        return normalize_plan(plan)

    def validate_models(self, plan):
        plan = normalize_plan(plan)
        catalog = {m['id']: m for m in self.current.models()['rows']}
        for role, ref in plan['model_roles'].items():
            if not ref:
                continue
            row = catalog.get(ref)
            if not row or not row.get('selectable') or row.get('kind') != ('image' if role == 'image' else 'llm'):
                plan['field_errors'].append({'id': digest([role, ref])[:24], 'content_hash': digest(ref),
                    'code': 'MODEL_NOT_AVAILABLE', 'field': 'model_roles.' + role,
                    'message': '该角色模型不可用，请检查供应商或选择可用模型'})
        plan['executable'] = not plan['field_errors']
        plan['unresolved_requirements'] = [e['message'] for e in plan['field_errors']]
        return plan

    def _append(self, saved, base, plan, key, request_hash, action):
        plan = self.validate_models(plan)
        plan.update(id=uuid4().hex, version=max(p.get('version', 0) for p in saved['plans']) + 1,
                    parent_plan_id=base['id'], created_at=time.time(), last_editor='user',
                    status='ready' if plan['executable'] else 'needs_input')
        for name in ('job_id', 'resume_job_id', 'execution_request_id', 'frozen_execution', 'submission_state', 'run_id',
                     'agent_run_id','frozen_execution_hash','confirm_request_hash','confirmation_key','confirmed_at',
                     'startup_lease_managed'):
            plan.pop(name, None)
        plan['changes'] = [{'field': key, 'before': base.get(key), 'after': plan.get(key)}
                           for key in ('jobs', *sorted(EDITABLE_OPTIONS)) if base.get(key) != plan.get(key)]
        saved['plans'].append(plan)
        saved.setdefault('messages', []).append({'id': uuid4().hex, 'role': 'assistant', 'created_at': time.time(),
            'content': action + '，等待确认执行。' if plan['executable'] else action + '，请修正标出的字段后再确认执行。',
            'plan_id': plan['id']})
        saved['status'] = 'planned'
        saved.setdefault('plan_requests', {})[key] = {'request_hash': request_hash, 'plan_id': plan['id'],
                                                      'revision': saved.get('_revision', 0) + 1}
        saved['updated_at'] = time.time()
        try:
            self.current.conversation_store.save(saved)
        except ConversationConflict:
            latest = self.current._read_agent_conversation(saved['id'])
            receipt = self._receipt(latest, key, request_hash)
            if receipt:
                return receipt
            raise PlanContractError('CONVERSATION_CONFLICT', '对话已在其他页面更新，当前编辑内容已保留', status=409) from None
        return self._receipt(saved, key, request_hash)

    def save(self, cid, pid, body, key):
        if not key or len(key) > 120:
            raise PlanContractError('REQUEST_KEY_INVALID', '需要至多120字符的请求键')
        request_hash = digest(['save', pid, body])
        with self.current.lock:
            saved = self.current._read_agent_conversation(cid)
            receipt = self._receipt(saved, key, request_hash)
            if receipt:
                return receipt
            base = self._base(saved, pid, body)
            plan = apply_edits(base, body.get('editable_fields', {}), review_decisions=body.get('review_decisions'))
            return self._append(saved, base, plan, key, request_hash, '已保存编辑计划')

    def restore(self, cid, pid, body, key):
        request_hash = digest(['restore', pid, body])
        with self.current.lock:
            saved = self.current._read_agent_conversation(cid)
            receipt = self._receipt(saved, key, request_hash)
            if receipt:
                return receipt
            base = self._base(saved, pid, body)
            target = next((p for p in saved['plans'] if p['id'] == body.get('restore_plan_id')), None)
            if target is None:
                raise PlanContractError('PLAN_NOT_FOUND', '恢复目标不属于当前对话', status=404)
            target = normalize_plan(target)
            active = {j['kind']: j['job_id'] for j in base['jobs']}
            fields = {k: deepcopy(target[k]) for k in EDITABLE_OPTIONS}
            fields['jobs'] = [{**{k: deepcopy(j[k]) for k in JOB_EDIT_FIELDS if k in j},
                              **({'target_job_id': active[j['kind']]} if j['kind'] in active else {})}
                             for j in target['jobs']]
            plan = apply_edits(base, fields)
            plan['restored_from_plan_id'] = target['id']
            return self._append(saved, base, plan, key, request_hash, '已恢复历史计划为新修订')

    def copy(self, cid, pid, body, key):
        if set(body)-{'base_plan_version','conversation_revision'}:
            raise PlanContractError('PLAN_COPY_INVALID','复制只接收当前版本和对话版本')
        request_hash=digest(['copy',pid,body])
        with self.current.lock:
            saved=self.current._read_agent_conversation(cid)
            receipt=self._receipt(saved,key,request_hash)
            if receipt:return receipt
            current=saved.get('plans',[])[-1] if saved.get('plans') else {}
            if current.get('id')!=pid or current.get('version')!=body.get('base_plan_version'):
                raise PlanContractError('TASK_PLAN_CONFLICT','当前计划已变化，请刷新后再复制',status=409)
            if body.get('conversation_revision')!=saved.get('_revision',0):
                raise PlanContractError('CONVERSATION_CONFLICT','对话已更新，请刷新后再复制',status=409)
            if current.get('plan_kind')=='draft_management':
                raise PlanContractError('PLAN_NOT_EDITABLE','草稿管理继续使用原有确认流程')
            copied=normalize_plan(current)
            copied['copied_from_plan_id']=pid
            return self._append(saved,current,copied,key,request_hash,'已复制计划为待确认的新修订')

    def adopt(self, cid, rid, body, key):
        request_hash = digest(['adopt', rid, body])
        with self.current.lock:
            saved = self.current._read_agent_conversation(cid)
            receipt = self._receipt(saved, key, request_hash)
            if receipt:
                return receipt
            record = next((r for r in saved.get('task_recognitions', []) if r['id'] == rid), None)
            if record is None:
                raise PlanContractError('RECOGNITION_NOT_FOUND', '校准结果不属于当前对话', status=404)
            base = self._base(saved, record['base_plan_id'], body)
            if record['status'] not in {'ready', 'needs_input'} or not record.get('candidate'):
                raise PlanContractError('CANDIDATE_NOT_READY', '候选未完成或已经过期，请重新校准', status=409)
            if record['base_plan_version'] != body['base_plan_version']:
                raise PlanContractError('CANDIDATE_STALE', '候选基础版本已变化', status=409)
            candidate = normalize_plan(record['candidate'])
            paths = body.get('accepted_candidate_paths', [])
            suggestions = {s['field_path']: s for s in candidate.get('field_suggestions', [])}
            if not isinstance(paths, list) or len(paths) > 32 or any(p not in suggestions for p in paths):
                raise PlanContractError('SUGGESTION_PATH_INVALID', '只能采用当前候选中存在的字段建议')
            fields = {}
            jobs = {j['job_id']: j for j in candidate['jobs']}
            for path in paths:
                suggestion = suggestions[path]
                if not suggestion_value_valid(path.rsplit('.', 1)[-1], suggestion.get('value')):
                    raise PlanContractError('SUGGESTION_VALUE_INVALID', '建议字段格式无效，请直接编辑该属性')
                if path in OPTION_FIELDS:
                    current = base[path]
                    fields[path] = deepcopy(suggestion['value'])
                else:
                    matches = [(identity, name) for identity in jobs for name in JOB_EDIT_FIELDS
                               if field_path(jobs[identity], name) == path and name != 'kind']
                    if not matches:
                        raise PlanContractError('SUGGESTION_PATH_INVALID', '建议的栏目已经移除')
                    identity, name = matches[0]
                    prior = next((j for j in base['jobs'] if j['job_id'] == identity), None)
                    if prior is None:
                        raise PlanContractError('SUGGESTION_STALE', '建议不属于当前栏目', status=409)
                    current = prior.get(name)
                    fields.setdefault('jobs', {j['job_id']: {'target_job_id': j['job_id']} for j in candidate['jobs']})
                    fields['jobs'][identity][name] = deepcopy(suggestion['value'])
                if suggestion['base_value_hash'] != digest(current):
                    raise PlanContractError('SUGGESTION_STALE', '字段已变化，请核对新的建议', status=409)
            if 'jobs' in fields:
                fields['jobs'] = list(fields['jobs'].values())
            if fields:
                candidate = apply_edits(candidate, fields)
            candidate = apply_edits(candidate, body.get('editable_fields', {}), review_decisions=body.get('review_decisions'))
            candidate['recognition_id'] = rid
            record.update(status='adopted')
            result = self._append(saved, base, candidate, key, request_hash, '已采用校准并保存编辑计划')
            return result

    def _existing_claim(self, cid, pid, key, request_hash):
        for _ in range(3):
            saved = self.current._read_agent_conversation(cid)
            plan = next(p for p in saved['plans'] if p['id'] == pid)
            receipt = saved.get('confirmation_requests', {}).get(key)
            if plan.get('confirm_request_hash') != request_hash or receipt and receipt['request_hash'] != request_hash:
                raise PlanContractError('CONFIRM_REQUEST_CONFLICT', '同一确认请求键不能用于另一组计划或选项', status=409)
            if receipt:
                return {'plan': deepcopy(plan), 'conversation_revision': saved.get('_revision', 0)}
            saved.setdefault('confirmation_requests', {})[key] = {'request_hash': request_hash, 'plan_id': pid,
                'execution_request_id': plan['execution_request_id']}
            saved['updated_at'] = time.time()
            try:
                result = self.current.conversation_store.save(saved)
                return {'plan': deepcopy(plan), 'conversation_revision': result.get('_revision', saved.get('_revision', 0) + 1)}
            except ConversationConflict:
                continue
        raise PlanContractError('CONVERSATION_CONFLICT', '确认记录正在被其他进程更新，请稍后用原请求键重试', status=409)

    def claim(self, cid, pid, body, key):
        from backend.capabilities import manager
        if not isinstance(key, str) or not key or len(key) > 120:
            raise PlanContractError('REQUEST_KEY_INVALID', '需要至多120字符的请求键')
        request_hash = digest(['confirm', pid, body])
        with self.current.lock:
            saved = self.current._read_agent_conversation(cid)
            receipt = saved.get('confirmation_requests', {}).get(key)
            if receipt and receipt['request_hash'] != request_hash:
                raise PlanContractError('CONFIRM_REQUEST_CONFLICT', '同一确认请求键不能用于另一组计划或选项', status=409)
            plan = next((p for p in saved['plans'] if p['id'] == pid), None)
            if plan is None:
                raise PlanContractError('PLAN_NOT_FOUND', '计划不属于当前对话', status=404)
            if plan.get('execution_request_id'):
                if plan.get('confirm_request_hash') != request_hash:
                    raise PlanContractError('CONFIRM_REQUEST_CONFLICT', '计划已按另一组确认选项认领，不能重复启动', status=409)
                return self._existing_claim(cid, pid, key, request_hash)
            self._base(saved, pid, {'base_plan_version': body['version']})
            normalized = self.validate_models(plan)
            if not normalized['executable']:
                raise PlanContractError('PLAN_NEEDS_INPUT', '请修正当前计划的字段后再确认', status=409)
            if body.get('semantic_hash') and body['semantic_hash'] != normalized['semantic_hash']:
                raise PlanContractError('PLAN_HASH_CONFLICT', '确认内容与当前保存计划不一致，请刷新后核对', status=409)
            fingerprint = self.configuration_fingerprint()
            if body.get('configuration_fingerprint') and body['configuration_fingerprint'] != fingerprint:
                raise PlanContractError('PLAN_CONFIGURATION_CHANGED', '模型或运行配置已改变；请刷新核对后重新确认', status=409)
            mode = body.get('skill_mode', normalized['skill_mode'])
            names = body.get('skill_names', normalized['skill_names'])
            if mode not in {'off', 'auto', 'manual'} or not isinstance(names, list) or len(names) > 3 or any(not isinstance(n, str) for n in names):
                raise PlanContractError('SKILL_SELECTION_INVALID', '技能选择无效，最多3项')
            if mode == 'manual' and not names:
                raise PlanContractError('SKILL_SELECTION_INVALID', '手动模式至少选择一项技能')
            if mode != normalized['skill_mode'] or names != normalized['skill_names']:
                raise PlanContractError('SKILL_SELECTION_CHANGED', '请先保存本次技能选择，再确认执行', status=409)
            capabilities = manager(self.current)
            preview = capabilities.plan_capabilities(cid, pid)
            frozen = execution_fields(normalized)
            frozen.update(plan_id=pid, plan_version=plan['version'], source_message_id=plan.get('source_message_id', ''),
                          source_text_hash=plan.get('source_text_hash', ''), skill_mode=mode, skill_names=deepcopy(names),
                          configuration_fingerprint=fingerprint, confirmed_at=time.time())
            frozen.update(model_runtime=self._model_runtime(normalized), host_environment=self._host_environment(),
                          capability_namespace=capabilities.store.namespace)
            context = self.current._agent_memory_for_execution(cid)
            context.update(confirmed_requirements=deepcopy(frozen['jobs']), preferences=deepcopy(preview['memory']))
            frozen.update(conversation_context=context, selected_skills=deepcopy(preview['skills']))
            identity = uuid4().hex
            saved.setdefault('confirmation_requests', {})[key] = {'request_hash': request_hash, 'plan_id': pid,
                                                                   'execution_request_id': identity}
            saved['updated_at'] = time.time()
            try:
                store = self.current.conversation_store
                with (store.knowledge_store.connection() if isinstance(store, PostgresConversationStore) else nullcontext(None)) as conn:
                    with conn.transaction() if conn is not None else nullcontext():
                        snapshot = capabilities.freeze_plan(cid, pid, identity, connection=conn, preview=preview, plan=normalized)
                        frozen['capability_snapshot_id'] = snapshot['snapshot_id']
                        if self.configuration_fingerprint() != fingerprint:
                            raise PlanContractError('PLAN_CONFIGURATION_CHANGED', '能力或模型配置在确认时改变，请刷新后重新确认', status=409)
                        plan.update(execution_request_id=identity, confirm_request_hash=request_hash, confirmation_key=key,
                                    submission_state='claimed', status='submitting', frozen_execution=frozen,
                                    frozen_execution_hash=digest(frozen))
                        result = store.save(saved, connection=conn) if conn is not None else store.save(saved)
            except ConversationConflict:
                latest = self.current._read_agent_conversation(cid)
                existing = next((p for p in latest['plans'] if p['id'] == pid), {})
                if existing.get('execution_request_id') and existing.get('confirm_request_hash') == request_hash:
                    return self._existing_claim(cid, pid, key, request_hash)
                raise PlanContractError('CONVERSATION_CONFLICT', '计划已在其他进程改变，请刷新后核对', status=409) from None
            return {'plan': deepcopy(plan), 'conversation_revision': result.get('_revision', saved.get('_revision', 0) + 1)}

    def frozen_path(self, cid, pid):
        return self.current.directory / 'conversations' / cid / 'plans' / (pid + '.json')

    def _begin_start(self, cid, pid, identity):
        saved = self.current._read_agent_conversation(cid)
        plan = next(p for p in saved['plans'] if p['id'] == pid)
        recoverable = plan.get('submission_state') == 'claimed' or (
            plan.get('submission_state') == 'starting' and plan.get('startup_lease_managed') is True)
        if plan.get('execution_request_id') != identity or not recoverable:
            return False
        plan['submission_state'] = 'starting'
        plan['startup_lease_managed'] = True
        saved['updated_at'] = time.time()
        try:
            self.current.conversation_store.save(saved)
        except ConversationConflict:
            return False
        return True

    def _attach_run(self, cid, pid, run):
        for _ in range(3):
            saved = self.current._read_agent_conversation(cid)
            plan = next(p for p in saved['plans'] if p['id'] == pid)
            if plan.get('execution_request_id') != run['id']:
                raise PlanContractError('RUN_ID_MISMATCH', '运行身份与冻结计划不一致', status=409)
            plan.update(submission_state='submitted', status='running', job_id=run['id'], run_id=run['id'], agent_run_id=run['id'])
            if run['id'] not in saved['runs']:
                saved['runs'].append(run['id'])
            saved['status'] = 'running'
            saved['updated_at'] = time.time()
            try:
                self.current.conversation_store.save(saved)
                return self.current.redact(run)
            except ConversationConflict:
                continue
        return self.current.redact({**run, 'submission_state': 'submission_uncertain',
                                    'message': '任务已存在，但对话回写未确认；不会重复启动，请刷新核对运行记录'})

    def confirm(self, cid, pid, body, key):
        from apps.web_service import _write_json_atomic
        from backend.capabilities import manager
        # Model work happens before the CAS/transaction, never inside the claim.
        saved = self.current._read_agent_conversation(cid)
        receipt = saved.get('confirmation_requests', {}).get(key)
        if receipt and receipt['request_hash'] != digest(['confirm', pid, body]):
            raise PlanContractError('CONFIRM_REQUEST_CONFLICT', '同一确认请求键不能用于另一组计划或选项', status=409)
        pending = next((p for p in saved['plans'] if p['id'] == pid), None)
        if pending and not pending.get('execution_request_id'):
            self._base(saved, pid, {'base_plan_version': body.get('version')})
            normalized = self.validate_models(pending)
            if not normalized['executable']:
                raise PlanContractError('PLAN_NEEDS_INPUT', '请修正当前计划的字段后再确认', status=409)
            if not isinstance(key,str) or not key or len(key)>120:
                raise PlanContractError('REQUEST_KEY_INVALID','需要至多120字符的请求键')
            if body.get('semantic_hash') and body['semantic_hash'] != normalized['semantic_hash']:
                raise PlanContractError('PLAN_HASH_CONFLICT','确认内容与当前保存计划不一致，请刷新后核对',status=409)
            if body.get('configuration_fingerprint') and body['configuration_fingerprint'] != self.configuration_fingerprint():
                raise PlanContractError('PLAN_CONFIGURATION_CHANGED','模型或运行配置已改变；请刷新核对后重新确认',status=409)
            if body.get('skill_mode',normalized['skill_mode']) != normalized['skill_mode'] or body.get('skill_names',normalized['skill_names']) != normalized['skill_names']:
                raise PlanContractError('SKILL_SELECTION_CHANGED','请先保存本次技能选择，再确认执行',status=409)
            capabilities = manager(self.current)
            policy, _ = capabilities.context.policy(cid)
            if policy['mode'] == 'auto':
                capabilities.context.prepare(cid)
        claim = self.claim(cid, pid, body, key)
        plan = claim['plan']
        identity = plan['execution_request_id']
        frozen = deepcopy(plan['frozen_execution'])
        verify_execution(frozen)
        if digest(frozen) != plan['frozen_execution_hash']:
            raise PlanContractError('PLAN_FROZEN_MISMATCH', '数据库冻结计划摘要不一致')
        path = self.ensure_frozen_file(cid, plan)
        run = self.current.jobs.get(identity)
        journal = self.current.directory / 'jobs' / (identity + '.json')
        if not run and journal.exists():
            run = json.loads(journal.read_text(encoding='utf-8'))
        if run:
            return self._attach_run(cid, pid, run)
        lease = file_lock(self.current.directory / 'locks' / ('start-' + identity + '.lock'), timeout=0)
        try:
            lease.__enter__()
        except PlatformError as error:
            if error.code != 'CONFIG_BUSY':
                raise
            return {'id': identity, 'status': 'submission_uncertain',
                    'message': '另一进程正在提交同一任务；请稍后核对运行记录，不会重复启动'}
        try:
            run = self.current.jobs.get(identity)
            if not run and journal.exists():
                run = json.loads(journal.read_text(encoding='utf-8'))
            if run:
                return self._attach_run(cid, pid, run)
            # submit persists the journal before starting its worker. An abandoned
            # managed lease with no journal can safely retry the same identity.
            if not self._begin_start(cid, pid, identity):
                return {'id': identity, 'status': 'submission_uncertain',
                        'message': '执行请求已被认领，启动结果尚未确认；请核对运行记录，不会重复启动'}
            news = next((j for j in frozen['jobs'] if j['kind'] == 'daily_news'), frozen['jobs'][0])
            roles = frozen['model_roles']
            request = {'kind': 'agent', 'title': '智能体：已确认计划', 'run_id': identity, 'reserved_job_id': identity, 'count': news['count'],
                'prompts': [news['prompt']], 'evaluation_viewpoint': news['evaluation_viewpoint'], 'lookback_days': 'auto',
                'assets_glob': 'assets/empty/*', 'platform': frozen['platform'], 'performance_mode': frozen['performance_mode'],
                'budget_minutes': 0.0, 'agent_jobs_file': str(path), 'image_score_required': frozen['image_score_required'],
                'agent_id': roles['agent'], 'llm_id': roles['writer'], 'image_id': roles['image'], 'delivery': frozen['delivery']}
            request.update({key:deepcopy(frozen[key]) for key in ('model_runtime','host_environment','capability_namespace') if key in frozen})
            run = self.current.submit(request, identity)
            if run['id'] != identity:
                raise PlanContractError('RUN_ID_MISMATCH', '提交结果身份不符，停止后续操作', status=409)
            return self._attach_run(cid, pid, run)
        finally:
            lease.__exit__(None, None, None)

    def ensure_frozen_file(self, cid, plan):
        from apps.web_service import _write_json_atomic
        frozen = plan['frozen_execution']
        verify_execution(frozen)
        if digest(frozen) != plan.get('frozen_execution_hash'):
            raise PlanContractError('PLAN_FROZEN_MISMATCH', '数据库冻结计划摘要不一致')
        path = self.frozen_path(cid, plan['id'])
        existing = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding='utf-8'))
            except ValueError:
                existing = {}
            except OSError as exc:
                raise PlanContractError('PLAN_FROZEN_UNAVAILABLE', '无法读取冻结文件，请检查运行区权限') from exc
            if not isinstance(existing, dict):
                existing = {}
            if (('frozen_execution_hash' in existing and existing['frozen_execution_hash'] != plan['frozen_execution_hash'])
                    or any(k in existing and existing[k] != v for k, v in frozen.items())):
                raise PlanContractError('PLAN_FROZEN_MISMATCH', '冻结文件与数据库记录不一致，未启动任务')
        if existing.get('frozen_execution_hash') != plan['frozen_execution_hash'] or any(k not in existing for k in frozen):
            _write_json_atomic(path, {**frozen, 'frozen_execution_hash': plan['frozen_execution_hash']})
        return path
