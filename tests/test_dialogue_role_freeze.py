"""Real role-store selection and subsequent dialogue environment inheritance."""
import json
import time

import httpx

from apps import gui
from apps.web_service import Workbench
from backend.dialogue import DialogueService
from src.model_platforms import RuntimeClient
from test_conversational_agent_cancellation import no_external_work
from test_conversational_agent_store import dialogue_store


def test_dialogue_role_freeze_survives_default_changes_without_provider_switch(dialogue_store, tmp_path, monkeypatch):
    ledger, _ = dialogue_store
    monkeypatch.setattr(gui, 'build_subprocess_env', lambda values: dict(values))
    current = Workbench(tmp_path, conversation_store=ledger.conversations)
    models = current.model_platforms()
    connection = models.add_connection({'name': 'Offline dialogue model', 'adapter': 'openai_chat',
        'base_url': 'http://127.0.0.1:11434/v1', 'network': 'local', 'auth_mode': 'none'})
    models.authorize(connection['connection_id'], {'roles': ['agent', 'writer'], 'risk_accepted': True,
        'max_requests': 20, 'expires_at': time.time() + 3600}, models.state()['revision'])
    client = RuntimeClient(models, transport=httpx.MockTransport(lambda request: httpx.Response(200,
        json={'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]})))
    refs = []
    for name in ('first', 'second'):
        row = models.add_model({'connection_id': connection['connection_id'], 'upstream_model_id': name,
                                'enabled': True}, models.state()['revision'])
        for capability in ('text', 'structured'):
            models.check(row['model_ref'], capability, client)
        refs.append(row['model_ref'])
    first, second = refs
    # Seed known legacy image IDs through the store's trusted compatibility boundary.
    models.bind({'agent': first, 'writer': second, 'image': 'minimax:image-01'}, models.state()['revision'], allow_legacy=True)
    host = DialogueService(current, store=ledger, directory=tmp_path / 'dialogue')
    before = host.environment()
    snapshots = json.loads(before['RUN_MODEL_SNAPSHOTS'])
    assert snapshots['agent']['model_ref'] == first and snapshots['writer']['model_ref'] == second
    assert before['IMAGE_PROVIDER'] == 'minimax' and before['MINIMAX_IMAGE_MODEL'] == 'image-01'
    frozen_keys = {'RUN_MODEL_SNAPSHOTS', 'RUN_LEGACY_MODEL_ROLES', 'RUN_LEGACY_MODEL_CONFIGS',
        'MODEL_PLATFORMS_DIR', 'MODEL_PLATFORMS_NAMESPACE', 'CONTROLLER_MODEL_REF', 'WRITER_MODEL_REF',
        'IMAGE_PROVIDER', 'MINIMAX_IMAGE_MODEL', 'AUTO_IMAGE'}
    frozen = {k: v for k, v in before.items() if k in frozen_keys}
    models.bind({'agent': second, 'writer': first, 'image': 'ccswitch:gpt-image-2'}, models.state()['revision'], allow_legacy=True)
    after = host.environment(frozen)
    assert json.loads(after['RUN_MODEL_SNAPSHOTS']) == snapshots
    assert after['IMAGE_PROVIDER'] == 'minimax' and after['MINIMAX_IMAGE_MODEL'] == 'image-01'
    assert after['ALLOW_PAID_LLM_FALLBACK'] == after['MINIMAX_ALLOW_PAYGO'] == '0'
    new = host.environment()
    assert json.loads(new['RUN_MODEL_SNAPSHOTS'])['agent']['model_ref'] == second
    assert new['IMAGE_PROVIDER'] == 'ccswitch'
