import pytest

from backend.task_recognition import RecognizedTask, validate_candidate
from test_task_calibration import calibration, candidate, wait_recognition, workbench


def test_calibration_payload_separates_host_defaults_from_user_evidence(calibration):
    _, client, cid, _, body, calls = calibration
    started = client.post(f'/api/conversations/{cid}/task-recognitions', headers={'X-Workbench': '1'}, json=body)
    assert wait_recognition(client, cid, started.json()['id'])['status'] == 'ready'
    payload = calls[0][1]
    assert payload['user_evidence'][0] == {'id': 'u1', 'quote': '生成5条每日新闻'}
    assert all('prompt' not in job for job in payload['local_plan']['jobs'])
    assert all(row['quote'] in payload['user_message'] for row in payload['user_evidence'])


def test_evidence_id_anchors_requirement_without_model_copying_default_prompt(calibration):
    current, _, _, base, _, _ = calibration
    text = '生成5条每日新闻，关键词：伊朗、关税；速度优先，不上传'
    value = candidate()
    value['requirements'][0].update(original_text='用户指定伊朗与关税作为检索条件', evidence_quote='@u2')
    plan = validate_candidate(RecognizedTask.model_validate(value), text, base, current)
    requirement = plan['requirements'][0]
    assert requirement['evidence_quote'] == '关键词：伊朗、关税'
    assert requirement['original_text'] == '关键词：伊朗、关税'
    assert plan['jobs'][0]['keywords'] == ['伊朗', '关税']


def test_paraphrased_original_text_is_not_confused_with_verbatim_evidence(calibration):
    current, _, _, base, _, _ = calibration
    text = '生成5条每日新闻，关键词：伊朗、关税；速度优先，不上传'
    value = candidate()
    value['requirements'][0]['original_text'] = '用户要求关注伊朗与关税'
    plan = validate_candidate(RecognizedTask.model_validate(value), text, base, current)
    assert plan['requirements'][0]['original_text'] == '伊朗、关税'


@pytest.mark.parametrize('quote', ['@u999', '国际冲突 科技产业 社会民生 财经产业', '用户允许公开发布'])
def test_invalid_evidence_still_rejected_and_names_the_requirement(calibration, quote):
    current, _, _, base, _, _ = calibration
    value = candidate()
    value['requirements'][0]['evidence_quote'] = quote
    text = '生成5条每日新闻，关键词：伊朗、关税；速度优先，不上传'
    with pytest.raises(ValueError, match='r1'):
        validate_candidate(RecognizedTask.model_validate(value), text, base, current)
