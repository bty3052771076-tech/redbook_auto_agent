"""MiniMax reasoning budgets and compaction use distinct, observable policies."""
import json

import httpx
from langchain_core.messages import HumanMessage
import pytest

from backend.dialogue_model import build_model
from src.model_platforms.security import PlatformError
from test_conversational_agent_cancellation import no_external_work


def model_fixture(tmp_path, monkeypatch, model='MiniMax-M3', response=None):
    monkeypatch.chdir(tmp_path)
    client = build_model({'MINIMAX_TOKEN_PLAN_API_KEY': 'offline-fixture-no-api',
        'MINIMAX_LLM_MODEL': model, 'MODEL_PLATFORMS_DIR': str(tmp_path / 'model-platforms'),
        'MINIMAX_BILLING_MODE': 'subscription_only', 'ALLOW_PAID_LLM_FALLBACK': '0'})
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response or {'choices': [
            {'message': {'content': 'Fixture response.'}, 'finish_reason': 'stop'}]})
    client._client.transport = httpx.MockTransport(respond)
    return client, requests


def test_m3_controller_has_reasoning_budget_but_summary_disables_thinking(tmp_path, monkeypatch):
    model, requests = model_fixture(tmp_path, monkeypatch)
    model.invoke([HumanMessage(content='Plan a bounded fixture task.')])
    assert requests[0]['max_tokens'] == 16384
    assert requests[0]['reasoning_split'] is True
    assert requests[0].get('thinking', {}).get('type') != 'disabled'
    summary = model.for_summary()
    summary.invoke([HumanMessage(content='Summarize retained facts only.')])
    assert requests[1]['max_tokens'] == 8192
    assert requests[1]['thinking'] == {'type': 'disabled'}
    assert model._snapshot['parameters']['max_output_tokens'] == 16384
    assert summary._snapshot['purpose'] == 'compaction'


def test_flash_keeps_existing_medium_and_131072_policy_for_summary(tmp_path, monkeypatch):
    model, requests = model_fixture(tmp_path, monkeypatch, 'MiniMax-M3.1-Flash')
    model.for_summary().invoke([HumanMessage(content='Summarize fixture.')])
    assert requests[0]['max_completion_tokens'] == 131072
    assert requests[0]['reasoning_effort'] == 'medium'
    assert 'thinking' not in requests[0]


@pytest.mark.parametrize('finish,expected', [('length', 'length'), ('content_filter', 'content_filter')])
def test_incomplete_legacy_reply_records_finish_reason_and_budget_without_retry(tmp_path, monkeypatch, finish, expected):
    model, requests = model_fixture(tmp_path, monkeypatch, response={
        'choices': [{'message': {'content': '<think>private-fixture-reasoning</think>'}, 'finish_reason': finish}],
        'usage': {'completion_tokens': 8192}, 'id': 'fixture-private-request'})
    with pytest.raises(PlatformError) as error:
        model.for_summary().invoke([HumanMessage(content='Fixture summary.')])
    assert error.value.code == 'OUTPUT_INCOMPLETE'
    assert 'finish_reason=' + expected in str(error.value)
    assert 'max_output_tokens=8192' in str(error.value)
    assert 'private-fixture-reasoning' not in str(error.value)
    assert len(requests) == 1
