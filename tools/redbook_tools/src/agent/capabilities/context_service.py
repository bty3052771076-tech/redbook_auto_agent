from __future__ import annotations

import time

from src.agent.compaction import comparison_context, estimate_tokens

from .models import CapabilityError, safe, digest


DEFAULT_POLICY = {'mode': 'manual', 'soft_threshold': 12000, 'keep_recent': 16}


class ContextService:
    def __init__(self, capabilities):
        self.capabilities = capabilities
        self.workbench = capabilities.workbench
        self.store = capabilities.store

    def _conversation(self, identity):
        from apps.web_service import valid_conversation_id
        return self.workbench._read_agent_conversation(valid_conversation_id(identity))

    def policy(self, identity):
        row = self.store.get('policy:context:' + identity) or {}
        return {key: row.get(key, value) for key, value in DEFAULT_POLICY.items()}, row.get('revision', 0)

    def overview(self, identity):
        conversation = self._conversation(identity)
        status = self.workbench.agent_context_status(identity)
        context = status.get('context') or {}
        policy, revision = self.policy(identity)
        plans = conversation.get('plans') or []
        selected = (plans[-1].get('model_roles') or {}).get('agent', '') if plans else ''
        selected = selected or self.workbench.providers().get('bindings', {}).get('agent') or ''
        model_selection = self.model_selection(selected)
        with self.store.knowledge_store.connection() as conn:
            rows = conn.execute('''SELECT s.version,s.through_seq,s.summary,s.constraints,
                s.task_state,s.input_tokens,s.output_tokens,s.status,s.created_at
                FROM agent.compaction_snapshots s JOIN agent.conversations c
                ON c.conversation_id=s.conversation_id
                WHERE c.conversation_id=%s AND c.account_namespace=%s
                ORDER BY s.version DESC LIMIT 200''', (identity, self.store.namespace)).fetchall()
            calls = conn.execute('''SELECT x.id,x.run_id,x.status,x.payload,x.started_at
                FROM agent.capability_calls x JOIN agent.capability_snapshots s
                ON s.namespace=x.namespace AND s.run_id=x.run_id
                WHERE x.namespace=%s AND s.payload->>'conversation_id'=%s
                AND jsonb_array_length(coalesce(x.payload->'context_requests','[]'::jsonb))>0
                ORDER BY x.started_at DESC,x.id DESC LIMIT 51''', (self.store.namespace, identity)).fetchall()
        requests = [{**request, 'call_id': call['id'], 'run_id': call['run_id'], 'call_status': call['status']}
                    for call in calls[:50] for request in call['payload']['context_requests']]
        actual_usage = {'requests': requests, 'history_incomplete': len(calls)>50,
                        'next_call_cursor': calls[49]['id'] if len(calls)>50 else None} if requests else None
        tokens = estimate_tokens(comparison_context(context.get('snapshot'), context.get('recent_messages', [])))
        through = status.get('through_seq', 0)
        coverage = {'summary_through_seq': through,
                    'retained_sequences': [message['seq'] for message in context.get('recent_messages', [])]}
        return safe({**status, 'model': model_selection['model'], 'model_selection': model_selection, 'tokens_estimate': tokens,
                     'policy': policy, 'policy_revision': revision, 'snapshots': [dict(row) for row in rows],
                     'coverage': coverage, 'context': {**context, 'coverage': coverage},
                     'actual_usage': actual_usage, 'threshold_exceeded': tokens > policy['soft_threshold']})

    def model_selection(self, selected):
        from src.model_platforms.integration import legacy_controller
        result = {'ref': selected, 'model': '', 'source': 'current_binding', 'available': False}
        if selected.startswith('m_'):
            row = next((row for row in self.workbench.models().get('rows', []) if row['id']==selected), None)
            if row:
                result.update(model=row.get('model', ''), provider=row.get('provider', ''), available=bool(row.get('available')))
            return result
        provider, _, model = selected.partition(':')
        try:
            config = legacy_controller(self.workbench.environment(), provider=provider, model=model)
        except ValueError as error:
            result['error_code'] = getattr(error, 'code', 'MODEL_UNAVAILABLE')
        else:
            result.update(model=config.model, provider=config.provider, available=True,
                          ref=selected or config.provider+':'+config.model)
        return result

    def save_policy(self, identity, data):
        self._conversation(identity)
        if set(data) - {'expected_revision', *DEFAULT_POLICY}:
            raise CapabilityError('CONTEXT_POLICY_INVALID', '存在未知的上下文策略字段', status=422)
        if data.get('mode') not in {'manual', 'auto'}:
            raise CapabilityError('CONTEXT_POLICY_INVALID', '请选择自动或手动压缩', status=422)
        for key, minimum, maximum in (('expected_revision', 0, 2147483647),
                                      ('soft_threshold', 256, 200000), ('keep_recent', 1, 1000)):
            value = data.get(key)
            if type(value) is not int or not minimum <= value <= maximum:
                raise CapabilityError('CONTEXT_POLICY_INVALID', f'{key}必须是{minimum}至{maximum}的整数', status=422)
        row = self.store.put('policy:context:' + identity, 'policy',
                             {key: data[key] for key in DEFAULT_POLICY},
                             expected_revision=data['expected_revision'], reason='修改对话上下文策略')
        return {'policy': {key: row[key] for key in DEFAULT_POLICY}, 'policy_revision': row['revision'],
                'effective_scope': '下次明确压缩或确认执行时检查；不改变已冻结任务'}

    def compact(self, identity, key=''):
        self._conversation(identity)
        policy, revision = self.policy(identity)
        return self.capabilities.operations.submit(
            lambda: self.workbench.compact_agent_conversation(identity, policy=policy),
            title='压缩对话上下文', key=key,
            request={'conversation_id':identity,'policy_revision':revision,'policy':policy})

    def prepare(self, identity):
        overview = self.overview(identity)
        if overview['policy']['mode'] != 'auto' or not overview['threshold_exceeded']:
            return {'status': 'not_needed', 'policy': overview['policy']}
        request={'conversation_id':identity,'policy':overview['policy'],
                 'policy_revision':overview['policy_revision'],'coverage':overview['coverage']}
        key='auto-context:'+digest(request)
        deadline=time.monotonic()+600
        while True:
            try:
                operation=self.capabilities.operations.submit(
                    lambda:self.workbench.compact_agent_conversation(identity,policy=overview['policy']),
                    title='确认前压缩对话上下文',key=key,request=request)
                break
            except CapabilityError as exc:
                if exc.code!='OPERATION_BUSY' or time.monotonic()>=deadline:raise
                time.sleep(.05)
        while operation['status'] in {'queued','running'}:
            if time.monotonic()>=deadline:
                raise CapabilityError('CONTEXT_COMPACTION_PENDING','压缩仍在运行，未启动内容任务；请稍后确认',
                                      status=409,retryable=True)
            time.sleep(.05)
            operation=self.capabilities.operations.get(operation['operation_id'])
        if operation['status']!='succeeded':
            raise CapabilityError('CONTEXT_COMPACTION_FAILED',
                                  (operation.get('error') or {}).get('message') or '压缩失败，请查看检测记录',
                                  status=409,retryable=True)
        result = operation['results']
        if result.get('status') in {'blocked', 'conflict'}:
            raise CapabilityError('CONTEXT_COMPACTION_' + result['status'].upper(),
                                  result.get('error') or '上下文压缩未完成，请刷新或改用手动策略', status=409)
        return result
