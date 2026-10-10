"""Real PostgreSQL and browser, with only the external model request replaced."""
import json
import os
from pathlib import Path
from unittest.mock import patch

from apps import gui
from backend import task_recognition
from backend import app as module
from scripts.verify_live_task_calibration import verify
from test_task_plan_v3 import proposed


def test_calibration_can_edit_and_save_without_reloading_after_model_reply(monkeypatch):
    env = {
        'MINIMAX_TOKEN_PLAN_API_KEY': 'offline-test-not-a-real-key',
        'MINIMAX_LLM_MODEL': 'MiniMax-M3', 'MINIMAX_IMAGE_MODEL': 'image-01',
        'MINIMAX_BILLING_MODE': 'subscription_only', 'MINIMAX_ALLOW_PAYGO': '0',
        'MINIMAX_ALLOW_PAID_CREDITS': '0', 'ALLOW_PAID_LLM_FALLBACK': '0',
        'AGENT_LLM_PROVIDER': 'minimax', 'LLM_PROVIDER': 'minimax', 'IMAGE_PROVIDER': 'minimax',
    }
    original = gui.load_env_file
    runtime = Path('E:/AI/codex/redbook_runtime')
    monkeypatch.setattr(gui, 'load_env_file', lambda path: dict(env) if Path(path).resolve() == (runtime / '.env.gui').resolve() else original(path))
    calls = []

    def external_request(config, payload):
        assert config.api_key == env['MINIMAX_TOKEN_PLAN_API_KEY']
        assert payload['user_message'].startswith('生成10条')
        assert 'base_plan' not in payload and 'local_plan' not in payload
        calls.append(config.model)
        candidate = proposed()
        candidate['jobs'][0]['count'] = 8
        return json.dumps(candidate, ensure_ascii=False)

    monkeypatch.setattr(task_recognition, 'call_model', external_request)
    previous_service = module.app.state.service
    previous_call = module.recognition_call
    with patch.dict(os.environ):
        result = verify(runtime)
    assert module.app.state.service is previous_service
    assert module.recognition_call is previous_call
    assert result['status'] == 'passed'
    assert result['plan_version'] == 2
    assert result['tool_count'] == 5
    assert calls == ['MiniMax-M3']
    assert result['new_model_calls'] == 1
    assert result['content_runs'] == result['platform_writes'] == 0
