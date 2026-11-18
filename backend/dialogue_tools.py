"""Fixed model-visible contracts. Approval and action identity belong to the host."""
from copy import deepcopy


def _schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": False}


_ID = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,128}$"}
_TEXT = {"type": "string", "maxLength": 2000}
_POST = _schema({"post_id": _ID}, ("post_id",))


def _entry(description, parameters, risk="readonly", permission="read", resource_id="", **extra):
    return {"description": description, "parameters": parameters, "risk": risk, "readonly": risk == "readonly",
            "permission": permission, "resource_id": resource_id, **extra}


TOOL_CATALOG = {
    "capabilities": _entry("List exact callable tool names and schemas; internal policy IDs are not callable MCP names.", _schema()),
    "list_capabilities": _entry("List exact callable tools and separately registered MCP tool IDs; never enables them.", _schema()),
    "sync_account": _entry("Read published posts and metrics from the configured dedicated profile, refresh local account preferences and knowledge. Never changes public platform content.",
        _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 1000}}),
        "platform_read", "platform_read", "builtin:account.sync"),
    "news_search": _entry("Collect dated news materials. Returns material IDs, sources and dates; no writing.",
        _schema({"query": _TEXT, "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                 "days": {"type": "integer", "minimum": 1, "maximum": 7}}, ("query",)),
        resource_id="builtin:news.search"),
    "list_materials": _entry("Read retained dated materials from this conversation without network collection. Use before news_search on continuation. Query ranks existing materials; it is not a hard relevance filter.",
        _schema({"query": _TEXT, "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                 "offset": {"type": "integer", "minimum": 0}}), resource_id="builtin:artifacts.read"),
    "generate_news": _entry("Fetch and verify the selected material's original article, then write exactly one daily_news or daily_wow draft without images. A search result with no body can be selected: this tool checks its source before writing and rejects insufficient evidence. Failure does not select another material.",
        _schema({"material_id": _ID, "column": {"enum": ["daily_news", "daily_wow"]},
                 "instructions": _TEXT}, ("material_id", "column")), "generation", "generate",
        resource_ids={"daily_news": "builtin:news.generate", "daily_wow": "builtin:wow.generate"}),
    "generate_digest": _entry("Produce one ai_digest, ai_wool or global_map issue. Never substitutes stale news for missing material. Map/card rendering and ai_wool reference-policy images are included.",
        _schema({"column": {"enum": ["ai_digest", "ai_wool", "global_map"]}, "instructions": _TEXT}, ("column",)),
        "generation", "generate", resource_ids={"ai_digest": "builtin:ai.generate", "ai_wool": "builtin:wool.generate", "global_map": "builtin:global_map.generate"}),
    "create_image": _entry("Create images for one owned draft using configured models and reference-image policy; no provider override.",
        _POST, "generation", "generate", "builtin:image.generate"),
    "inspect_draft": _entry("Read one conversation-owned draft and its retained receipts.", _POST, resource_id="builtin:artifacts.read"),
    "list_drafts": _entry("List this conversation's retained local drafts and receipt status; no platform write or regeneration.",
        _schema(), resource_id="builtin:artifacts.read"),
    "review_draft": _entry("Validate one owned draft without repair, generation or upload.", _POST, resource_id="builtin:content.review"),
    "save_draft": _entry("Save one reviewed owned draft to the configured dedicated profile, serially. Draft only; requires verified receipt.",
        _POST, "platform_write", "save_draft", "builtin:xhs.drafts.save_batch"),
    "knowledge_search": _entry("Search existing RAG in the host namespace for the specified purpose; no indexing or installation.",
        _schema({"query": _TEXT, "purpose": {"enum": ["evidence", "duplicate_reference", "style_reference", "operations"]},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 30}}, ("query",))),
    "list_skills": _entry("List registered skills and enabled state, without executing scripts.", _schema()),
    "read_skill": _entry("Read an enabled registered skill body, or a registered resource by ID, never an arbitrary file path.",
        _schema({"skill_id": _ID, "resource_id": {"type": "string", "maxLength": 256}}, ("skill_id",))),
    "mcp_call": _entry("Call only an exact tool_id from capabilities.mcp_tools. Never prefix builtin policy IDs with mcp:. News generation uses generate_news/generate_digest directly.",
        _schema({"tool_id": {"type": "string", "pattern": "^mcp:(?!builtin:)[A-Za-z0-9_-]+:[A-Za-z0-9_.-]+$"},
                 "arguments": {"type": "object"}}, ("tool_id", "arguments")), permission="mcp_read"),
}


def tool_schemas():
    """OpenAI function format also accepted by LangChain convert_to_openai_tool."""
    return [{"type": "function", "function": {"name": name, "description": row["description"],
            "parameters": deepcopy(row["parameters"])}} for name, row in TOOL_CATALOG.items()]


def resource_id(name, arguments):
    row = TOOL_CATALOG[name]
    return row.get("resource_ids", {}).get(arguments.get("column"), row["resource_id"]) or (
        arguments.get("tool_id", "") if name == "mcp_call" else "")


def capability_view(resources):
    """Describe this loop's executable interface, not the legacy policy catalog."""
    rows = {row['id']: row for row in resources}
    mcps = [{key: row.get(key) for key in ('id', 'name', 'description', 'enabled', 'revoked_at', 'input_schema', 'health')}
            for row in resources if row.get('kind') == 'mcp']
    tools = []
    for name, spec in TOOL_CATALOG.items():
        refs = spec.get('resource_ids') or ({'': spec['resource_id']} if spec['resource_id'] else {})
        policies = []
        for column, identity in refs.items():
            row = rows.get(identity, {})
            policies.append({'column': column, 'resource_id': identity,
                'enabled': bool(row.get('enabled')) and not bool(row.get('revoked_at')),
                'revoked_at': row.get('revoked_at'), 'health': row.get('health', {'status': 'not_registered'})})
        enabled = any(row['enabled'] for row in policies) if policies else True
        if name == 'mcp_call':
            enabled = any(row.get('enabled') and not row.get('revoked_at') for row in mcps)
        tools.append({'name': name, 'description': spec['description'], 'input_schema': deepcopy(spec['parameters']),
            'enabled': enabled, 'permission': spec['permission'], 'policies': policies})
    return {'tools': tools, 'count': len(tools), 'mcp_tools': mcps,
        'usage': '直接使用tools中的name和input_schema。policies里的builtin标识仅用于权限，不是可调用工具。'
                 'mcp_call只能使用mcp_tools中完整id，不得自行拼接。生成新闻直接用generate_news；AI集合稿用generate_digest。'}
