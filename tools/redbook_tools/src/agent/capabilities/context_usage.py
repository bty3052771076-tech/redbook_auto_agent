"""Observe actual model inputs without persisting prompts or credentials."""
import json
import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from uuid import uuid4

from src.agent.compaction import estimate_tokens
from .models import digest


_OBSERVED_CONTEXT = ContextVar('observed_context_request', default=None)


@contextmanager
def observe_context_requests(store, conversation_id, purpose):
    """Audit calibration/summary requests that do not have a tool dispatcher."""
    from .dispatcher import current_invocation
    if current_invocation()[0] is not None or _OBSERVED_CONTEXT.get() is not None:
        yield
        return
    run_id = 'context_' + uuid4().hex
    store.freeze(run_id, [], metadata={'conversation_id': conversation_id, 'purpose': purpose})
    call_id = store.start_call({'run_id': run_id, 'resource_id': 'builtin:controller.plan',
                                'resource_name': purpose, 'stage': purpose, 'status': 'running',
                                'origin': 'context', 'operation_key': run_id})
    token = _OBSERVED_CONTEXT.set((store, call_id, purpose))
    try:
        yield
    except Exception:
        store.finish_call(call_id, 'failed', {})
        raise
    else:
        store.finish_call(call_id, 'succeeded', {})
    finally:
        _OBSERVED_CONTEXT.reset(token)


def input_evidence(messages):
    texts = [message.get('content', '') if isinstance(message, dict) else getattr(message, 'content', '')
             for message in messages]
    normalized = [{key: message.get(key) if isinstance(message, dict) else getattr(message, key, None)
                   for key in ('role', 'type', 'content', 'tool_calls', 'tool_call_id', 'name')} for message in messages]
    evidence = {'input_hash': digest(normalized), 'input_characters': sum(len(str(text)) for text in texts),
                'tokens_estimate': estimate_tokens(normalized), 'memory_refs': [], 'skill_refs': [],
                'retained_sequences': [], 'snapshot_version': 0, 'summary_through_seq': 0,
                'source_hashes': [], 'pinned_message_ids': [], 'usage_kind': 'estimated'}

    def inspect(value, depth=0):
        if depth > 12:
            evidence['references_incomplete'] = True
            return
        if isinstance(value, list):
            for item in value:
                inspect(item, depth + 1)
        elif isinstance(value, dict):
            if value.get('schema_version') == 'conversation-context.v2':
                from src.agent.context_builder import content_hash
                summary = value.get('history_summary') or {}
                if summary.get('summary'):
                    evidence.update(snapshot_version=summary.get('version', 0),
                                    summary_through_seq=summary.get('through_seq', 0),
                                    summary_hash=digest(summary['summary']))
                recent = value.get('recent_exchanges') or []
                evidence['retained_sequences'].extend(row['seq'] for row in recent
                    if isinstance(row, dict) and type(row.get('seq')) is int)
                sent_ids = {row.get('id') for row in recent if isinstance(row, dict)}
                evidence['pinned_message_ids'].extend(identity for identity in
                    (value.get('manifest') or {}).get('pinned_message_ids', []) if identity in sent_ids)
                evidence['source_hashes'].extend(content_hash(row) for row in value.get('retrieved_context', []))
            if value.get('schema_version') == 'conversation-summary.v2':
                evidence['retained_sequences'].extend(row['seq'] for row in value.get('new_messages', [])
                    if isinstance(row, dict) and type(row.get('seq')) is int)
                evidence['snapshot_version'] = value.get('snapshot_version', 0)
                evidence['summary_through_seq'] = value.get('prior_through_seq', 0)
                evidence['source_hashes'].extend(digest(ref) for ref in value.get('allowed_artifact_refs', []))
            for key, item in value.items():
                if key in {'conversation_context', 'conversation_memory'} and isinstance(item, dict):
                    if item.get('summary'):
                        evidence.update(snapshot_version=item.get('snapshot_version', 0),
                                        summary_through_seq=item.get('through_seq', 0), summary_hash=digest(item['summary']))
                    evidence['retained_sequences'].extend(row['seq'] for row in item.get('recent_messages', [])
                        if isinstance(row, dict) and type(row.get('seq')) is int)
                if key == 'preferences' and isinstance(item, list):
                    evidence['memory_refs'].extend({'id': row['id'], 'revision': row['revision']} for row in item
                        if isinstance(row, dict) and isinstance(row.get('id'), str) and type(row.get('revision')) is int)
                if key == 'skills' and isinstance(item, list):
                    evidence['skill_refs'].extend({'id': row['id'], 'version_hash': row['version_hash']} for row in item
                        if isinstance(row, dict) and row.get('body') and isinstance(row.get('id'), str)
                        and isinstance(row.get('version_hash'), str))
                if key != 'manifest':
                    inspect(item, depth + 1)
        elif isinstance(value, str):
            for tag, key in (('agent_preferences', 'preferences'), ('agent_skills', 'skills')):
                for match in re.finditer('<' + tag + '>(.*?)</' + tag + '>', value, re.DOTALL):
                    try:
                        inspect({key: json.loads(match.group(1))}, depth + 1)
                    except (ValueError, TypeError):
                        evidence['references_incomplete'] = True

    for text in texts:
        try:
            value = json.loads(text) if isinstance(text, str) else text
        except ValueError:
            value = text
        inspect(value)
    for key in ('memory_refs', 'skill_refs'):
        evidence[key] = list({digest(row): row for row in evidence[key]}.values())
    evidence['retained_sequences'] = sorted(set(evidence['retained_sequences']))
    evidence['source_hashes'] = sorted(set(evidence['source_hashes']))
    evidence['pinned_message_ids'] = sorted(set(evidence['pinned_message_ids']))
    return evidence


def response_tokens(response):
    value = response if isinstance(response, dict) else {
        key: getattr(response, key, None) for key in ('usage_metadata', 'response_metadata', 'usage')}
    usage = value.get('usage_metadata')
    if not isinstance(usage, dict) or not usage:
        metadata = value.get('response_metadata') or {}
        usage = metadata.get('token_usage') if isinstance(metadata, dict) else {}
    if not isinstance(usage, dict) or not usage:
        usage = value.get('usage')
    usage = usage if isinstance(usage, dict) else {}
    result = {}
    for name, alternative in (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'),
                              ('total_tokens', 'total_tokens')):
        value = usage.get(name, usage.get(alternative))
        if type(value) is int and value >= 0:
            result[name] = value
    return result or None


def model_request_format(config, request_format=None):
    return request_format or (getattr(config, 'platform_snapshot', None) or {}).get('adapter') or 'openai_chat'


def _calibration_key(limits):
    identity = {key: limits.get(key) for key in ('provider', 'model', 'request_format')}
    if any(not isinstance(value, str) or not value for value in identity.values()):
        return None
    return 'context-calibration:' + digest(identity)


def input_calibration(store, model_limits):
    """Read a profile's high-water input multiplier without changing model limits."""
    default = {'factor': 1.0, 'samples': 0}
    identity = _calibration_key(model_limits)
    knowledge = getattr(store, 'knowledge_store', None)
    if identity is None or knowledge is None:
        return default
    with knowledge.connection() as conn:
        if not conn.execute("SELECT to_regclass('agent.capability_settings') AS name").fetchone()['name']:
            return default
        row = conn.execute('''SELECT payload FROM agent.capability_settings
            WHERE namespace=%s AND resource_id=%s AND revoked_at IS NULL''', (store.namespace, identity)).fetchone()
    if not row:
        return default
    factor = row['payload'].get('factor', 1.0)
    if type(factor) not in {int, float} or not math.isfinite(factor) or factor < 1:
        return default
    return {'factor': factor, 'samples': row['payload'].get('samples', 0)}


def record_input_usage(store, config, messages, response, *, request_format=None, input_estimate=None):
    """Persist input-only calibration. Neither prompts nor output counts are stored."""
    actual = (response_tokens(response) or {}).get('input_tokens')
    knowledge = getattr(store, 'knowledge_store', None)
    if type(actual) is not int or actual <= 0 or knowledge is None or not callable(getattr(store, 'put', None)):
        return None
    limits = {'provider': config.provider, 'model': config.model,
              'request_format': model_request_format(config, request_format)}
    identity = _calibration_key(limits)
    if identity is None:
        return None
    estimated = input_evidence(messages)['tokens_estimate'] if input_estimate is None else input_estimate
    if type(estimated) is not int or estimated <= 0:
        return None
    with knowledge.connection() as conn, conn.transaction():
        if not conn.execute("SELECT to_regclass('agent.capability_settings') AS name").fetchone()['name']:
            return None
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (store.namespace + ':' + identity,))
        row = conn.execute('''SELECT payload,revision,revoked_at FROM agent.capability_settings
            WHERE namespace=%s AND resource_id=%s FOR UPDATE''', (store.namespace, identity)).fetchone()
        if row and row['revoked_at']:
            return None
        old = row['payload'] if row else {}
        # A lower observation never relaxes a previous underestimate correction.
        factor = max(1.0, old.get('factor', 1.0), actual / estimated * 1.10 if actual > estimated else 1.0)
        payload = {**limits, 'factor': factor, 'samples': old.get('samples', 0) + 1,
                   'schema_version': 'context-input-calibration.v1'}
        return store.put(identity, 'context_calibration', payload, expected_revision=row['revision'] if row else 0,
                         actor='input_usage', reason='Conservative input estimate feedback', connection=conn)


def invoke_observed(config, messages, callback, *, request_format=None):
    from src.workflow.model_observation import observe_model_call
    from .dispatcher import current_invocation
    dispatcher, call_id, resource_id = current_invocation()
    scope = _OBSERVED_CONTEXT.get()
    if (dispatcher is None or call_id is None) and scope is None:
        return observe_model_call(config, callback)
    store = dispatcher.store if dispatcher is not None and call_id is not None else scope[0]
    if dispatcher is None or call_id is None:
        call_id, resource_id = scope[1], 'builtin:controller.plan'
    identity = uuid4().hex
    snapshot = config.platform_snapshot or {}
    record = {**input_evidence(messages), 'request_id': identity, 'status': 'request_attempted',
              'observed_at': datetime.now(timezone.utc).isoformat(), 'actual_tokens': None,
              'model': config.model, 'provider': config.provider,
              'request_format': model_request_format(config, request_format),
              'model_ref': snapshot.get('model_ref') or config.provider + ':' + config.model,
              'model_role': snapshot.get('role') or ('agent' if resource_id == 'builtin:controller.plan' else 'writer')}
    store.record_model_request(call_id, identity, record, append=True)
    try:
        response = observe_model_call(config, callback)
    except Exception as error:
        store.record_model_request(call_id, identity, {
            'status': 'request_failed', 'error_type': type(error).__name__})
        raise
    actual = response_tokens(response)
    record_input_usage(store, config, messages, response, request_format=request_format,
                       input_estimate=record['tokens_estimate'])
    store.record_model_request(call_id, identity, {
        'status': 'response_received', 'actual_tokens': actual, 'usage_kind': 'actual' if actual else 'estimated'})
    return response
