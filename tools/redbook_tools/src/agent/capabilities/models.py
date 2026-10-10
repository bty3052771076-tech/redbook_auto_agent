from __future__ import annotations

import hashlib
import json
import re
from typing import Any


class CapabilityError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 422, resource_id: str = '',
                 next_action: str = '', retryable: bool = False, revision: int | None = None):
        super().__init__(f'{code}: {message}')
        self.code, self.message, self.status = code, message, status
        self.resource_id, self.next_action = resource_id, next_action
        self.retryable, self.revision = retryable, revision

    def public(self) -> dict:
        return {'code': self.code, 'message': self.message, 'error': str(self),
                'resource_id': self.resource_id, 'next_action': self.next_action,
                'retryable': self.retryable, 'revision': self.revision}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def safe(value: Any) -> Any:
    from src.agent.editorial_agent import _redact_text
    if isinstance(value, dict):
        return {str(key): ('[已配置]' if re.search(r'(?i)secret|password|authorization|api.?key|access.?token', str(key))
                           and not str(key).endswith('_ref') else safe(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def bounded_summary(value: Any, limit: int = 2000) -> str:
    if hasattr(value, 'id'):
        value = {'id': getattr(value, 'id'), 'title': getattr(value, 'title', '')}
    return json.dumps(safe(value), ensure_ascii=False, default=lambda obj: type(obj).__name__)[:limit]
