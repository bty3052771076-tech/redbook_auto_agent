"""Observe actual model inputs without persisting prompts or credentials."""
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from src.agent.compaction import estimate_tokens
from .models import digest


def input_evidence(messages):
    texts = [message.get('content', '') if isinstance(message, dict) else getattr(message, 'content', '')
             for message in messages]
    evidence = {'input_hash': digest(texts), 'input_characters': sum(len(str(text)) for text in texts),
                'tokens_estimate': estimate_tokens(texts), 'memory_refs': [], 'skill_refs': [],
                'retained_sequences': [], 'snapshot_version': 0, 'summary_through_seq': 0}

    def inspect(value, depth=0):
        if depth > 12:
            evidence['references_incomplete'] = True
            return
        if isinstance(value, list):
            for item in value:
                inspect(item, depth + 1)
        elif isinstance(value, dict):
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
    return evidence


def response_tokens(response):
    usage = getattr(response, 'usage_metadata', None)
    if not isinstance(usage, dict) or not usage:
        metadata = getattr(response, 'response_metadata', {}) or {}
        usage = metadata.get('token_usage') if isinstance(metadata, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    result = {}
    for name, alternative in (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'),
                              ('total_tokens', 'total_tokens')):
        value = usage.get(name, usage.get(alternative))
        if type(value) is int and value >= 0:
            result[name] = value
    return result or None


def invoke_observed(config, messages, callback):
    from .dispatcher import current_invocation
    dispatcher, call_id, resource_id = current_invocation()
    if dispatcher is None or call_id is None:
        return callback()
    identity = uuid4().hex
    snapshot = config.platform_snapshot or {}
    record = {**input_evidence(messages), 'request_id': identity, 'status': 'request_attempted',
              'observed_at': datetime.now(timezone.utc).isoformat(), 'actual_tokens': None,
              'model': config.model, 'provider': config.provider,
              'model_ref': snapshot.get('model_ref') or config.provider + ':' + config.model,
              'model_role': snapshot.get('role') or ('agent' if resource_id == 'builtin:controller.plan' else 'writer')}
    dispatcher.store.record_model_request(call_id, identity, record, append=True)
    try:
        response = callback()
    except Exception as error:
        dispatcher.store.record_model_request(call_id, identity, {
            'status': 'request_failed', 'error_type': type(error).__name__})
        raise
    dispatcher.store.record_model_request(call_id, identity, {
        'status': 'response_received', 'actual_tokens': response_tokens(response)})
    return response
