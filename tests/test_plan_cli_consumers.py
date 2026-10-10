"""Exercise saved revision -> frozen request -> CLI -> business callbacks offline."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from apps import cli
from backend.plan_service import PlanService
from src.publish.delivery_state import DeliveryStateStore
from src.storage.models import Execution, Post
from src.workflow.vision_review import VisionReviewResult
from test_plan_revisions import body, setup_plan
from test_task_calibration import workbench
from tools.redbook_tools.tests.test_cli_agent_non_news_repair import Ledger

REAL_MODEL_RUNTIME = PlanService._model_runtime


@pytest.mark.parametrize('delivery,platform,mode,scoring', [
    ('generate_only', 'xhs', 'balanced', False),
    ('save_draft', 'xhs', 'speed', True),
    ('save_draft', 'toutiao', 'balanced', False),
    ('save_draft', 'both', 'speed', True),
])
def test_saved_plan_fields_reach_cli_generated_news_and_delivery(workbench, monkeypatch, tmp_path,
                                                               delivery, platform, mode, scoring):
    env = {'MINIMAX_TOKEN_PLAN_API_KEY': 'offline-test-not-a-real-key',
           'MINIMAX_LLM_MODEL': 'MiniMax-M3', 'MINIMAX_IMAGE_MODEL': 'image-01',
           'MINIMAX_BILLING_MODE': 'subscription_only', 'MINIMAX_ALLOW_PAYGO': '0',
           'MINIMAX_ALLOW_PAID_CREDITS': '0', 'ALLOW_PAID_LLM_FALLBACK': '0',
           'AGENT_LLM_PROVIDER': 'minimax', 'MODEL_PLATFORMS_NAMESPACE': 'agent',
           'MODEL_PLATFORMS_DIR': str(tmp_path / 'model-platforms')}
    monkeypatch.setattr(workbench, 'environment', lambda: dict(env))
    monkeypatch.setattr(PlanService, '_model_runtime', REAL_MODEL_RUNTIME)
    service = PlanService(workbench)
    cid, base = setup_plan(workbench)
    job = service.current_plan(cid)['jobs'][0]
    saved = service.save(cid, base['id'], body(base, delivery=delivery, platform=platform,
        performance_mode=mode, image_score_required=scoring, jobs=[{
            'target_job_id': job['job_id'], 'count': 5, 'search_keywords': ['芯片'],
            'topic_preferences': ['游戏退款'], 'topic_brief': '',
            'evaluation_viewpoint': '从消费者权益角度简洁评价'}]), 'consumer-edit')['plan']
    requests = []
    monkeypatch.setattr(workbench, 'submit', lambda request, key:
                        requests.append(deepcopy(request)) or {'id': request['run_id'], 'status': 'queued'})
    service.confirm(cid, saved['id'], {'version': saved['version'], 'semantic_hash': saved['semantic_hash'],
                                    'skill_mode': 'off', 'skill_names': []}, 'consumer-confirm')
    args, environment = workbench.plan(requests[0], requests[0]['run_id'])
    ledger, generated, platform_calls = Ledger(), [], []
    monkeypatch.setattr(cli.KnowledgeStore, 'from_env', lambda: object())
    monkeypatch.setattr(cli, 'AgentArtifactStore', lambda store: ledger)
    monkeypatch.setattr(cli, 'prepare_local_knowledge_snapshot', lambda **kwargs: {'knowledge_status': 'ready'})
    monkeypatch.setattr(cli, 'DeliveryStateStore', lambda: DeliveryStateStore(_memory=True))

    def generator(**kwargs):
        generated.append(kwargs)
        return [Post(title=f'独立芯片事件{index}', body=f'具体事件{index}',
                     platform={'news': {'picked': {'url': f'https://example.test/{index}'}}})
                for index in range(kwargs['count'])]

    monkeypatch.setattr(cli, 'create_daily_news_posts', generator)

    def platform_runner(target):
        def run(post, **kwargs):
            platform_calls.append((target, post.id, kwargs))
            return Execution(post_id=post.id, attempt=1, result='saved_draft')
        return run

    monkeypatch.setattr(cli, 'run_save_draft_sync', platform_runner('xhs'))
    monkeypatch.setattr(cli, 'run_save_toutiao_draft_sync', platform_runner('toutiao'))
    monkeypatch.setattr(cli, 'run_publish_drafts_sync', lambda **kwargs: pytest.fail('public publisher called'))

    def core(jobs, *, tools, config, **kwargs):
        assert len(jobs) == 1 and jobs[0].count == 5
        assert config.provider == 'minimax' and config.use_subscription
        assert tools.upload_enabled == (delivery == 'save_draft')
        verdict = VisionReviewResult(ok=True, score=40, issues=(), retry_prompt='', provider='test', model='test')
        assert cli._vision_review_passes(verdict) is (not scoring)
        context = {'agent_run_id': requests[0]['run_id'], 'agent_job_key': '0:daily_news'}
        posts = tools.generate(jobs[0], context)
        assert len(posts) == 5
        if tools.upload_enabled:
            outcomes = tools.upload_batch(jobs[0], posts, context)
            assert len(outcomes) == 5 and all(ok for ok, detail in outcomes.values())
        return SimpleNamespace(status='completed', completed_jobs=1, requested_jobs=1,
                               uploaded_posts=posts if tools.upload_enabled else [], checkpoint_path='offline', errors=[])

    monkeypatch.setattr(cli, 'run_editorial_agent', core)
    result = CliRunner().invoke(cli.app, [*args[args.index('agent'):], '--no-preflight'], env=environment)
    assert result.exit_code == 0, result.output
    assert len(generated) == 1
    call = generated[0]
    assert call['count'] == 5 and call['performance_mode'] == mode
    assert call['evaluation_viewpoint'] == '从消费者权益角度简洁评价'
    assert call['lookback_days'] == 'auto'
    assert '芯片' in call['prompt_hint'] and '游戏退款' in call['prompt_hint']
    assert '隐私' not in call['prompt_hint']
    targets = ['xhs', 'toutiao'] if platform == 'both' else [platform]
    expected_targets = targets * 5 if delivery == 'save_draft' else []
    assert [target for target, pid, options in platform_calls] == expected_targets
    assert all(options['headless'] and options['login_hold'] == 0 for _, _, options in platform_calls)
