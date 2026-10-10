from __future__ import annotations

from .models import CapabilityError


def validate_tool(tools: dict, resource_id: str, stage: str, *, revoked: bool = False, seen=None) -> dict:
    row = tools.get(resource_id)
    if row is None:
        raise CapabilityError('CAPABILITY_UNKNOWN', '该工具没有登记执行适配器', resource_id=resource_id)
    if revoked:
        raise CapabilityError('CAPABILITY_REVOKED', '已停止此工具的后续调用', status=409, resource_id=resource_id)
    if not row.get('enabled', True):
        raise CapabilityError('CAPABILITY_DISABLED', '此工具已停用', status=409, resource_id=resource_id, next_action='启用工具或调整任务')
    if row.get('binding') == 'unbound':
        raise CapabilityError('CAPABILITY_UNBOUND', '工具尚未绑定使用范围', status=409, resource_id=resource_id)
    if stage not in row.get('stages', []):
        raise CapabilityError('CAPABILITY_STAGE_DENIED', '此阶段不允许使用该工具', status=409, resource_id=resource_id)
    if row.get('kind') == 'mcp':
        from .mcp_effects import require_read_only
        require_read_only(row, row.get('connection') or {}, confirmed=row.get('read_only_confirmed',False))
    visited = set(seen or ())
    if resource_id in visited:
        raise CapabilityError('CAPABILITY_DEPENDENCY_CYCLE', '工具依赖存在循环', resource_id=resource_id)
    visited.add(resource_id)
    for dependency in row.get('dependencies', []):
        if dependency.startswith(('builtin:', 'mcp:')):
            dependency_row = tools.get(dependency, {})
            validate_tool(tools, dependency, (dependency_row.get('stages') or [stage])[0], seen=visited)
    return row
