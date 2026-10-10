from backend.task_recognition import RecognizedTask, validate_candidate
from test_task_calibration import calibration, candidate, workbench


def test_unrequested_quota_option_cannot_block_published_metrics_refresh(calibration):
    current, _, _, base, _, _ = calibration
    text = "生成5条每日新闻，关键词：伊朗、关税；刷新帖子数据，不上传"
    value = candidate()
    value["options"]["skip_quota_sync"] = False
    plan = validate_candidate(RecognizedTask.model_validate(value), text, base, current)
    assert plan["skip_quota_sync"] is True
    assert plan["executable"] is True
    assert plan["unresolved_requirements"] == []


def test_express_request_for_unsupported_quota_sync_still_needs_input(calibration):
    current, _, _, base, _, _ = calibration
    text = "生成5条每日新闻，关键词：伊朗、关税；同步模型额度，不上传"
    value = candidate()
    value["options"]["skip_quota_sync"] = False
    plan = validate_candidate(RecognizedTask.model_validate(value), text, base, current)
    assert plan['skip_quota_sync'] is True
    assert any(w['code'] == 'QUOTA_POLICY_READONLY' for w in plan['warnings'])
    assert plan['executable'] is True


def test_quota_balance_statement_is_not_a_sync_request(calibration):
    current, _, _, base, _, _ = calibration
    text = "生成5条每日新闻，关键词：伊朗、关税；额度充足，不上传"
    value = candidate()
    value["options"]["skip_quota_sync"] = False
    plan = validate_candidate(RecognizedTask.model_validate(value), text, base, current)
    assert plan["skip_quota_sync"] is True
    assert plan["executable"] is True


def test_explicit_no_quota_sync_cannot_be_overridden_by_model_default(calibration):
    current, _, _, base, _, _ = calibration
    text = "生成5条每日新闻，关键词：伊朗、关税；不需要再次同步额度，不上传"
    value = candidate()
    value["options"]["skip_quota_sync"] = False
    plan = validate_candidate(RecognizedTask.model_validate(value), text, base, current)
    assert plan["skip_quota_sync"] is True
    assert plan["executable"] is True
