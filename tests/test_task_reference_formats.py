"""Evidence format compatibility without relaxing verbatim provenance checks."""

from copy import deepcopy
import json

import pytest

from backend import app as module
from backend.task_recognition import RecognizedTask, parse_task, validate_candidate
from test_task_calibration import calibration, candidate, wait_recognition, workbench


TEXT = "生成5条每日新闻，关键词：伊朗、关税；速度优先，不上传"


@pytest.mark.parametrize("reference", [
    "@u2 关键词：伊朗、关税",
    "@u2：关键词：伊朗、关税",
    '@u2: "关键词：伊朗、关税"',
    " @u2 ",
    "u2",
    "“伊朗、关税”",
])
def test_verified_reference_formats_are_canonicalized(calibration, reference):
    current, _, _, base, _, _ = calibration
    value = candidate()
    value["requirements"][0]["evidence_quote"] = reference
    original = deepcopy(value)
    plan = validate_candidate(RecognizedTask.model_validate(value), TEXT, base, current)
    quote = "伊朗、关税" if reference == "“伊朗、关税”" else "关键词：伊朗、关税"
    assert plan["requirements"][0]["evidence_quote"] == quote
    assert plan["requirements"][0]["original_text"] == quote
    assert plan["jobs"][0]["keywords"] == ["伊朗", "关税"]
    assert value == original


@pytest.mark.parametrize("reference,reason", [
    ("@u999 关键词：伊朗、关税", "unknown_evidence_id"),
    ("@u1 关键词：伊朗、关税", "reference_text_mismatch"),
    ("@u2 关键词：伊朗、石油", "reference_text_mismatch"),
    ("国际冲突 科技产业 社会民生 财经产业", "not_verbatim_user_quote"),
])
def test_invalid_reference_cannot_be_repaired_by_discarding_its_text(calibration, reference, reason):
    current, _, _, base, _, _ = calibration
    value = candidate()
    value["requirements"][0]["evidence_quote"] = reference
    plan = validate_candidate(RecognizedTask.model_validate(value), TEXT, base, current)
    assert plan['requirements'][0]['verification'] == 'unverified'
    assert plan['requirements'][0]['evidence_quote'] == reference
    assert plan['executable'] is True
    diagnostics = plan['validation_diagnostics']
    assert diagnostics == [{"requirement_id": "r1", "field": "evidence_quote", "reason": reason,
                            "supplied_reference": reference}]


def test_calibration_api_accepts_verified_id_plus_quote_without_executing(calibration, monkeypatch):
    current, client, cid, base, body, _ = calibration
    value = candidate()
    value["requirements"][0]["evidence_quote"] = "@u2 关键词：伊朗、关税"
    monkeypatch.setattr(module, "recognition_call", lambda config, payload: json.dumps(value, ensure_ascii=False))
    response = client.post(f"/api/conversations/{cid}/task-recognitions", headers={"X-Workbench": "1"}, json=body)
    result = wait_recognition(client, cid, response.json()["id"])
    assert result["status"] == "ready", result
    assert result["candidate"]["requirements"][0]["evidence_quote"] == "关键词：伊朗、关税"
    assert current.get_agent_conversation(cid)["plans"][-1]["id"] == base["id"]
    assert current.get_agent_conversation(cid)["runs"] == []


def test_failure_record_preserves_specific_validation_diagnostics(calibration, monkeypatch):
    current, client, cid, base, body, _ = calibration
    value = candidate()
    value["requirements"][0]["evidence_quote"] = "@u1 关键词：伊朗、关税"
    monkeypatch.setattr(module, "recognition_call", lambda config, payload: json.dumps(value, ensure_ascii=False))
    root = f"/api/conversations/{cid}/task-recognitions"
    response = client.post(root, headers={"X-Workbench": "1"}, json=body)
    rid = response.json()["id"]
    result = wait_recognition(client, cid, rid)
    assert result["status"] == "ready"
    assert result['candidate']['requirements'][0]['verification'] == 'unverified'
    assert result.get("validation_diagnostics") == [{"requirement_id": "r1", "field": "evidence_quote",
        "reason": "reference_text_mismatch", "supplied_reference": "@u1 关键词：伊朗、关税"}]
    assert client.get(f"{root}/{rid}").json()["validation_diagnostics"] == result["validation_diagnostics"]
    assert current.get_agent_conversation(cid)["plans"][-1]["id"] == base["id"]
    assert current.get_agent_conversation(cid)["runs"] == []


def test_schema_diagnostics_name_invalid_fields_without_echoing_input():
    value = candidate()
    value["options"]["delivery"] = {'secret': 'test-secret-input-do-not-echo'}
    with pytest.raises(ValueError) as error:
        parse_task(json.dumps(value))
    assert getattr(error.value, "diagnostics", []) == [{"field": "options.delivery", "reason": "string_type"}]
    assert "test-secret-input" not in str(error.value)
