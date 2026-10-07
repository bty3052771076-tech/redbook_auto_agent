from backend.task_recognition import RecognizedTask, validate_candidate
from test_task_calibration import calibration, candidate, workbench


def test_calibrated_plan_retains_the_public_schema_version(calibration):
    current, _, _, base, _, _ = calibration
    text = "生成5条每日新闻，关键词：伊朗、关税；速度优先，不上传"
    plan = validate_candidate(RecognizedTask.model_validate(candidate()), text, base, current)
    assert plan["schema_version"] == "task-recognition.v2"
