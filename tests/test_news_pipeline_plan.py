import pytest
from src.agent.plan_contract import normalize_plan, execution_fields, verify_execution, digest, _execution_inputs


def plan(version=None):
    value={"jobs":[{"kind":"daily_news","count":1}]}
    if version is not None:
        value["generation_pipeline_version"]=version
    return normalize_plan(value)


def test_legacy_semantic_hash_unchanged_without_pipeline_field():
    value=plan()
    assert value["generation_pipeline_version"]=="legacy"
    assert "generation_pipeline_version" not in _execution_inputs(value)
    verify_execution(execution_fields(value))


def test_pipeline_version_is_frozen_and_mutation_rejected():
    value=plan("staged_v2")
    frozen=execution_fields(value)
    assert frozen["generation_pipeline_version"]=="staged_v2"
    verify_execution(frozen)
    frozen["generation_pipeline_version"]="legacy"
    with pytest.raises(ValueError, match="摘要"):
        verify_execution(frozen)


def test_unknown_pipeline_is_not_executable():
    assert plan("unsafe")["executable"] is False
