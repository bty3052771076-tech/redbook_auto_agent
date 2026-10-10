import json
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from typer.testing import CliRunner

from apps import cli
from src.storage.models import AssetInfo, Post
from src.workflow.vision_review import VisionReviewResult


def _result(score=40, ok=False):
    return VisionReviewResult(ok=ok, score=score, issues=('scene is illustrative',),
                              retry_prompt='change composition', provider='test', model='test')


def _post(tmp_path, *, news=True):
    path = tmp_path / 'picture.png'
    image = Image.new('RGB', (200, 200), 'white')
    ImageDraw.Draw(image).rectangle((10, 10, 130, 150), fill='green')
    image.save(path)
    return Post(title='A specific current event', body='A complete description of the reported event.',
                assets=[AssetInfo(path=str(path), validated=True)],
                platform={'news': {'picked': {'title': 'One source event', 'url': 'https://example.com/event'}}} if news else {})


def test_default_score_gate_remains_strict(monkeypatch):
    monkeypatch.delenv('AUTO_VLM_SCORE_REQUIRED', raising=False)
    assert cli._vision_review_passes(_result(69, True)) is False
    assert cli._vision_review_passes(_result(70, True)) is True
    assert cli._vision_review_passes(_result(90, False)) is False


@pytest.mark.parametrize('setting', ['0', 'false', 'off', 'no'])
def test_explicit_advisory_score_does_not_block_low_verdict(monkeypatch, setting):
    monkeypatch.setenv('AUTO_VLM_SCORE_REQUIRED', setting)
    assert cli._vision_review_passes(_result()) is True


def test_advisory_first_image_is_retained_without_redraw(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_VLM_SCORE_REQUIRED', '0')
    monkeypatch.chdir(tmp_path)
    post = _post(tmp_path)
    first_hash = post.assets[0].path
    result, repairs, errors, history = cli._review_with_bounded_image_repair(
        post, config=SimpleNamespace(provider='test', model='test'), viewpoint='neutral',
        max_repairs=1, review_fn=lambda *args, **kwargs: _result(),
        regenerate_fn=lambda *args: pytest.fail('advisory score must not redraw'),
    )
    assert result.score == 40 and result.ok is False
    assert repairs == 0 and errors == [] and len(history) == 1
    assert post.assets[0].path == first_hash


def test_advisory_mode_does_not_generate_visual_surplus(monkeypatch):
    monkeypatch.setenv('AUTO_VLM_SCORE_REQUIRED', '0')
    assert cli._daily_news_visual_spare_count(10) == 0


def test_advisory_selection_still_requires_deterministic_approval(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_VLM_SCORE_REQUIRED', '0')
    monkeypatch.setenv('DAILY_NEWS_SELECTION_POLICY', 'soft')
    low = _post(tmp_path)
    low.platform['quality_gate'] = {'deterministic_ok': True, 'vision': {'ok': False, 'score': 40}}
    unscored = low.model_copy(deep=True)
    unscored.id = 'unscored'
    unscored.platform['quality_gate'] = {'deterministic_ok': True}
    invalid = low.model_copy(deep=True)
    invalid.id = 'invalid'
    invalid.platform['quality_gate']['deterministic_ok'] = False
    selected, failed, unused = cli._select_visual_ready_daily_news_posts(
        [low, unscored, invalid], requested_count=2,
    )
    assert [post.id for post in selected] == [low.id, 'unscored']
    assert [post.id for post in failed] == ['invalid'] and unused == []


def test_advisory_mode_still_blocks_broken_image(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_VLM_SCORE_REQUIRED', '0')
    monkeypatch.chdir(tmp_path)
    post = _post(tmp_path, news=False)
    (tmp_path / 'picture.png').write_bytes(b'not an image')
    errors = cli._run_auto_quality_gate(
        [post], expected_count=1, evaluation_viewpoint='neutral', require_vision=True,
    )
    assert errors and post.platform['quality_gate']['deterministic_ok'] is False


def test_advisory_mode_can_report_missing_reviewer_without_blocking_valid_image(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_VLM_SCORE_REQUIRED', '0')
    monkeypatch.setenv('AUTO_VLM_REVIEW', '1')
    monkeypatch.delenv('VLM_REVIEW_MODEL', raising=False)
    monkeypatch.delenv('VLM_REVIEW_PROVIDER', raising=False)
    monkeypatch.chdir(tmp_path)
    post = _post(tmp_path, news=False)
    errors = cli._run_auto_quality_gate(
        [post], expected_count=1, evaluation_viewpoint='neutral', require_vision=True,
    )
    assert errors == []
    assert post.platform['quality_gate']['deterministic_ok'] is True
    assert post.platform['quality_gate']['image_score_required'] is False


def test_auto_cli_accepts_explicit_advisory_flag():
    result = CliRunner().invoke(cli.app, ['auto', '--title', '每日新闻', '--count', '0', '--no-image-score-required'])
    assert result.exit_code == 1 and 'count 必须 >= 1' in result.output


@pytest.mark.parametrize('suffix,want', [('', True), ('，关闭图片评分硬门槛', False), ('，图片评分仅供参考', False), ('，启用图片评分硬门槛', True)])
def test_agent_message_freezes_explicit_score_policy(tmp_path, workbench_factory, suffix, want):
    service = workbench_factory(tmp_path)
    plan = service.append_agent_message(service.create_agent_conversation()['id'], '生成10条每日新闻并存入草稿箱' + suffix)['plan']
    assert plan['image_score_required'] is want


def test_agent_request_passes_advisory_policy_to_cli(tmp_path, workbench_factory):
    service = workbench_factory(tmp_path)
    args, env = service.plan({'kind': 'agent', 'image_score_required': False}, 'a' * 32)
    assert '--no-image-score-required' in args
    assert env['AUTO_VLM_SCORE_REQUIRED'] == '0'
    assert env['MINIMAX_ALLOW_PAYGO'] == '0' and env['MINIMAX_ALLOW_PAID_CREDITS'] == '0'


def test_invalid_score_policy_is_not_coerced_from_string(tmp_path, workbench_factory):
    service = workbench_factory(tmp_path)
    with pytest.raises(ValueError, match='image_score_required'):
        service.plan({'kind': 'agent', 'image_score_required': 'false'}, 'b' * 32)


def test_score_policy_survives_plan_confirmation_and_resume(tmp_path, workbench_factory, monkeypatch):
    service = workbench_factory(tmp_path)
    conversation = service.create_agent_conversation()
    planned = service.append_agent_message(conversation['id'], '生成1条每日新闻并存入草稿箱，关闭图片评分硬门槛')['plan']
    # This direct Workbench entry point represents a pre-v3 workflow plan.
    # The new GUI confirms v3 plans through PlanService and freezes execution.
    saved = service._read_agent_conversation(conversation['id'])
    saved['plans'][-1]['plan_schema_version'] = 'editorial-plan.v2'
    service._write_agent_conversation(saved)
    monkeypatch.setattr(service, 'agent_context_status', lambda _: {'status': 'ready', 'context': {}})
    submitted = []
    run_id = 'c' * 32

    def submit(request, key):
        submitted.append(request)
        return {'id': run_id if len(submitted) == 1 else 'd' * 32, 'status': 'failed', 'kind': 'agent', 'agent_run_id': run_id}

    monkeypatch.setattr(service, 'submit', submit)
    job = service.execute_agent_plan(conversation['id'], planned['id'], planned['version'], 'first')
    service.jobs[run_id] = job
    frozen = json.loads((service.directory / 'conversations' / conversation['id'] / 'plans' / f"{planned['id']}.json").read_text(encoding='utf-8'))
    assert service._read_agent_conversation(conversation['id'])['plans'][-1]['plan_schema_version'] == 'editorial-plan.v2'
    assert frozen['image_score_required'] is False and submitted[0]['image_score_required'] is False
    monkeypatch.setenv('AUTO_VLM_SCORE_REQUIRED', '1')
    monkeypatch.setattr(service, '_agent_checkpoint_state', lambda _: {'status': 'failed'})
    service.resume_agent_run(conversation['id'], run_id, 'resume')
    assert submitted[1]['image_score_required'] is False and submitted[1]['run_id'] == run_id


@pytest.mark.parametrize('reuse', [False, True])
def test_advisory_low_score_is_explicitly_reference_only(tmp_path, monkeypatch, reuse):
    from src.workflow.review_cache import stamp_vision_cache
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('AUTO_VLM_SCORE_REQUIRED', '0')
    monkeypatch.setenv('AUTO_VLM_REVIEW', '1')
    monkeypatch.setattr(cli, 'configured_vision_review_model', lambda: True)
    monkeypatch.setattr(cli, 'load_vision_review_config', lambda: SimpleNamespace(provider='test', model='test'))
    monkeypatch.setattr(cli, 'review_post_image', lambda *a, **kw: _result(3))
    monkeypatch.setattr(cli, 'regenerate_daily_news_post_image', lambda *a, **kw: pytest.fail('no redraw'))
    events = []
    monkeypatch.setattr(cli, '_emit_progress_event', lambda *a: events.append(a))
    post = _post(tmp_path, news=False)
    if reuse:
        post.platform['quality_gate'] = {'vision': stamp_vision_cache(post, {'ok': False, 'score': 3}, 'neutral')}
        monkeypatch.setattr(cli, 'review_post_image', lambda *a, **kw: pytest.fail('reuse cached score'))
    errors = cli._run_auto_quality_gate([post], expected_count=1, evaluation_viewpoint='neutral',
                                      require_vision=True, reuse_vision_results=True)
    assert errors == []
    assert post.platform['quality_gate']['image_score_mode'] == 'advisory'
    assert post.platform['quality_gate']['vision']['score'] == 3
    assert any('仅供参考' in str(event) for event in events)
