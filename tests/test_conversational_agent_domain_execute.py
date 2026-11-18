"""Real execute/dispatcher/PG entry; only external I/O boundaries are replaced."""

from contextlib import asynccontextmanager
from dataclasses import asdict
import io
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from PIL import Image, ImageDraw
import pytest

from backend import dialogue_tool_worker as domain
from src.agent.capabilities.models import digest
from src.agent.capabilities.store import CapabilityStore
from src.knowledge.store import KnowledgeStore
from src.storage.files import load_post, save_post
from src.storage.models import AssetInfo, Execution, Post
from test_conversational_agent_cancellation import no_external_work
from test_conversational_agent_store import dialogue_store


@pytest.fixture
def action(dialogue_store, tmp_path, monkeypatch):
    store, cid = dialogue_store
    root = tmp_path / 'runtime'
    root.mkdir()
    caps = CapabilityStore(store.knowledge, namespace=store.namespace)
    caps.ensure_schema()
    session = store.session(cid)
    store.transition(cid, state='running', execution_id='fixture-execution')
    context = {'conversation_id': cid, 'execution_id': 'fixture-execution', 'thread_id': session['thread_id'],
        'namespace': store.namespace, 'action_id': uuid4().hex, 'directory': str(tmp_path / 'action'),
        'session_directory': str(root / 'dialogue' / cid), 'owned_post_ids': [], 'current_date': '2026-11-17T12:00:00+08:00'}
    env = {'REDBOOK_RUNTIME_ROOT': str(root), 'MODEL_PLATFORMS_DIR': str(root / 'data/model_platforms'),
        'MODEL_PLATFORMS_NAMESPACE': 'agent', 'AUTO_VLM_SCORE_REQUIRED': '0',
        'XHS_CHROME_USER_DATA_DIR': str(root / 'fixture-profile'),
        'MINIMAX_TOKEN_PLAN_API_KEY': 'offline-only-no-provider-request'}
    output = io.StringIO()
    monkeypatch.setattr(domain, '_OUTPUT', output)

    def execute(name, arguments):
        context['action_id'] = uuid4().hex
        context['directory'] = str(tmp_path / 'actions' / context['action_id'])
        result = domain.execute(name, arguments, env, context)
        assert json.loads((Path(context['directory']) / 'result.json').read_text(encoding='utf-8')) == result
        return result

    return SimpleNamespace(store=store, cid=cid, root=root, caps=caps, context=context,
                           env=env, output=output, execute=execute)


def retained_post(action, *, news=False):
    path = action.root / 'fixture.png'
    image = Image.new('RGB', (128, 128), 'white')
    ImageDraw.Draw(image).rectangle((0, 0, 64, 127), fill='black')
    image.save(path)
    post = Post(title='Offline fixture', body='A verified local fixture used only for isolated acceptance testing.',
                assets=[AssetInfo(path=str(path))], platform={'news': {'column': 'daily_news'}} if news else {})
    save_post(post, base=action.root / 'data')
    action.context['owned_post_ids'].append(post.id)
    return post


@pytest.mark.parametrize('name,arguments', [
    ('news_search', {'query': 'fixture', 'command': 'never execute'}),
    ('generate_news', {'material_id': '../outside', 'column': 'daily_news'}),
    ('generate_digest', {'column': 'unknown'}),
    ('create_image', {'post_id': 'fixture', 'provider': 'unapproved'}),
    ('news_search', {'query': 'fixture', 'limit': float('nan')}),
    ('news_search', []),
])
def test_execute_rejects_invalid_schema_before_business_work(action, name, arguments):
    result = action.execute(name, arguments)
    assert result['ok'] is False and result['error_code'] == 'TOOL_ARGUMENTS_INVALID'
    assert action.caps.calls(run_id=action.context['action_id'])['rows'] == []


@pytest.mark.parametrize('revoked', [False, True])
def test_execute_disabled_or_revoked_tool_never_reaches_network(action, monkeypatch, revoked):
    from src.news import daily_news

    row = action.caps.put('builtin:news.search', 'tool', {'enabled': not revoked}, expected_revision=0)
    if revoked:
        action.caps.revoke(row['id'], expected_revision=row['revision'], reason='fixture revocation')
    else:
        action.caps.put(row['id'], 'tool', {'enabled': False}, expected_revision=row['revision'])
    monkeypatch.setattr(daily_news, 'fetch_daily_news_candidates', lambda *args, **kwargs: pytest.fail('Disabled source I/O ran'))
    result = action.execute('news_search', {'query': 'fixture'})
    assert result['ok'] is False
    assert result['error_code'] == ('CAPABILITY_REVOKED' if revoked else 'CAPABILITY_DISABLED')
    assert result['status'] == 'missing_capability'


def test_execute_rejects_cross_conversation_post_and_material(action):
    foreign = Post(title='Foreign fixture', body='Never read or change this draft.')
    save_post(foreign, base=action.root / 'data')
    before = (action.root / 'data/posts' / foreign.id / 'post.json').read_bytes()
    result = action.execute('inspect_draft', {'post_id': foreign.id})
    assert result['error_code'] == 'POST_SCOPE_DENIED'
    materials = Path(action.context['session_directory']) / 'materials'
    materials.mkdir(parents=True)
    (materials / 'foreign.json').write_text(json.dumps({'conversation_id': 'other',
        'namespace': action.caps.namespace, 'item': {}}), encoding='utf-8')
    result = action.execute('generate_news', {'material_id': 'foreign', 'column': 'daily_news'})
    assert result['error_code'] == 'MATERIAL_SCOPE_DENIED'
    assert (action.root / 'data/posts' / foreign.id / 'post.json').read_bytes() == before


def test_execute_cancelled_execution_starts_no_action(action):
    action.store.transition(action.cid, state='cancelled')
    result = action.execute('capabilities', {})
    assert result['ok'] is False and result['error_code'] == 'EXECUTION_NOT_ACTIVE'
    assert action.caps.calls(run_id=action.context['action_id'])['rows'] == []


def test_execute_rag_uses_real_management_interface_with_owned_namespace_and_redacts(action, monkeypatch):
    calls = []
    secret = 'fixture-private-key-should-not-appear'
    action.env['FIXTURE_API_KEY'] = secret

    def search(self, query, **kwargs):
        calls.append((query, kwargs))
        return [{'record_id': 'fixture-document', 'snippet': 'Source includes ' + secret}]

    monkeypatch.setattr(KnowledgeStore, 'search', search)
    result = action.execute('knowledge_search', {'query': 'fixture', 'purpose': 'evidence', 'limit': 2})
    assert result['ok'] is True and result['rows'][0]['record_id'] == 'fixture-document'
    assert calls == [('fixture', {'purpose': 'evidence', 'limit': 2, 'account_namespace': action.caps.namespace})]
    assert secret not in json.dumps(result) + action.output.getvalue()
    assert '[redacted]' in result['rows'][0]['snippet']


@pytest.mark.parametrize('state,code', [('enabled', None), ('disabled', 'SKILL_DISABLED'), ('revoked', 'CAPABILITY_REVOKED')])
def test_execute_real_skill_selection_enforces_current_policy(action, state, code):
    row = action.caps.put('skill_fixture', 'skill', {'name': 'fixture', 'description': 'offline fixture',
        'enabled': state != 'disabled', 'body': 'Read-only skill body.', 'version': 'fixture-v1',
        'version_hash': 'fixture-v1', 'resources': {'references/source.md': 'Local source.'}}, expected_revision=0)
    if state == 'revoked':
        action.caps.revoke(row['id'], expected_revision=row['revision'], reason='fixture revocation')
    result = action.execute('read_skill', {'skill_id': row['id']})
    if code:
        assert result['ok'] is False and result['error_code'] == code
    else:
        assert result['ok'] is True and result['body'] == 'Read-only skill body.'
        resource = action.execute('read_skill', {'skill_id': row['id'], 'resource_id': 'references/source.md'})
        assert resource['ok'] is True and resource['content'] == 'Local source.'
        escaped = action.execute('read_skill', {'skill_id': row['id'], 'resource_id': '../outside'})
        assert escaped['error_code'] == 'SKILL_RESOURCE_INVALID'


def test_execute_mcp_keeps_real_runtime_schema_and_session_checks(action, monkeypatch):
    from src.agent.mcp_manager import MCPManager

    manager = domain._manager(action.root, action.caps)
    created = manager.mcp.save({'name': 'offline MCP fixture', 'transport': 'streamable_http',
                               'url': 'http://127.0.0.1:19877/mcp'})
    connection = action.caps.get(created['id'])
    schema = {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query'], 'additionalProperties': False}
    annotations = {'readOnlyHint': True, 'destructiveHint': False}
    listed = {'name': 'lookup', 'description': 'fixture', 'input_schema': schema, 'annotations': annotations}
    tool = {**listed, 'schema_hash': digest(schema)}
    action.caps.put(connection['id'], 'mcp', {**connection, 'enabled': True, 'tools': [tool], 'tool_policies': {
        'lookup': {'schema_hash': digest(schema), 'enabled': True, 'stages': ['preparation'], 'purpose': 'evidence',
                   'read_only_confirmed': True, 'effect_hash': digest(annotations)}}}, expected_revision=connection['revision'])
    calls, closed = [], []

    class Session:
        async def call_tool(self, name, arguments, **kwargs):
            calls.append((name, arguments))
            return SimpleNamespace(isError=False, structuredContent={'fixture': 'read-only result'})

    @asynccontextmanager
    async def session(self):
        try:
            yield Session()
        finally:
            closed.append(True)

    async def listing(self, session):
        self.complete = True
        return [listed]

    monkeypatch.setattr(MCPManager, '_session', session)
    monkeypatch.setattr(MCPManager, '_list', listing)
    identity = f"mcp:{connection['id']}:lookup"
    result = action.execute('mcp_call', {'tool_id': identity, 'arguments': {'query': 'fixture'}})
    assert result['ok'] is True and result['output'] == {'fixture': 'read-only result'}
    assert calls == [('lookup', {'query': 'fixture'})] and closed == [True]
    invalid = action.execute('mcp_call', {'tool_id': identity, 'arguments': {'command': 'must not execute'}})
    assert invalid['error_code'] == 'MCP_ARGUMENTS_INVALID'
    assert calls == [('lookup', {'query': 'fixture'})]


def test_execute_image_failure_is_single_attempt_and_disables_internal_retry_flags(action, monkeypatch):
    from src.workflow import create_post
    import os

    post = retained_post(action, news=True)
    calls = []

    def no_image(**kwargs):
        calls.append(dict(os.environ))
        return [], [], False

    monkeypatch.setattr(create_post, '_fetch_daily_news_related_images', no_image)
    result = action.execute('create_image', {'post_id': post.id})
    assert result['ok'] is False and result['error_code'] == 'IMAGE_GENERATION_FAILED'
    assert len(calls) == 1
    assert calls[0]['LLM_RATE_LIMIT_MAX_RETRIES'] == calls[0]['LLM_TRANSIENT_RETRY_MAX'] == '0'
    assert calls[0]['OPENCODEX_IMAGE_FALLBACK'] == 'none'
    assert load_post(post.id, base=action.root / 'data').assets == post.assets


def test_execute_missing_platform_readback_is_uncertain_and_never_marks_uploaded(action, monkeypatch):
    from src.publish import playwright_steps

    post = retained_post(action)
    calls = []

    def save_fixture(current, **kwargs):
        calls.append(current.id)
        return Execution(post_id=current.id, result='saved_draft', steps=[])

    monkeypatch.setattr(playwright_steps, '_run_save_draft_sync_unlocked', save_fixture)
    result = action.execute('save_draft', {'post_id': post.id})
    assert result['ok'] is False and result['status'] == 'uncertain'
    assert result['error_code'] == 'DRAFT_SAVE_UNCERTAIN' and result['retryable'] is False
    assert calls == [post.id]
    saved = load_post(post.id, base=action.root / 'data')
    assert saved.uploaded is False and 'xhs_draft' not in saved.platform


@pytest.mark.parametrize('complete', [True, False])
def test_execute_account_sync_uses_existing_analytics_and_real_pg_without_platform_writes(action, monkeypatch, complete):
    from src.publish import playwright_steps

    calls = []

    def collect(**kwargs):
        calls.append(kwargs['limit'])
        return {'items': [{'title': 'Fixture published metric', 'url': 'https://fixture.invalid/note',
                           'likes': 3, 'comments': 1, 'favorites': 2}], 'complete': complete}

    monkeypatch.setattr(playwright_steps, 'run_collect_published_metrics_sync', collect)
    result = action.execute('sync_account', {'limit': 2})
    assert calls == [2]
    if complete:
        assert result['ok'] is True and result['fetched'] == 1 and result['platform_readonly'] is True
        assert (action.root / 'data/analytics/published_metrics.jsonl').is_file()
        with action.store.knowledge.connection() as conn:
            row = conn.execute('SELECT account_namespace FROM knowledge.documents WHERE account_namespace=%s AND record_id=%s',
                               (action.caps.namespace, 'account-reader-preferences')).fetchone()
        assert row['account_namespace'] == action.caps.namespace
    else:
        assert result['ok'] is False and result['error_code'] == 'ACCOUNT_SYNC_INCOMPLETE'
        assert not (action.root / 'data/analytics/published_metrics.jsonl').exists()


def test_execute_list_drafts_only_exposes_owned_retained_posts(action):
    own = retained_post(action)
    foreign = Post(title='Foreign fixture', body='Must not be discovered by this conversation.')
    save_post(foreign, base=action.root / 'data')
    result = action.execute('list_drafts', {})
    assert result['ok'] is True
    visible = json.dumps(result)
    assert own.id in visible and foreign.id not in visible
