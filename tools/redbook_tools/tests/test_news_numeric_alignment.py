from types import SimpleNamespace
import pytest
from src.workflow.news_contracts import StageContext
from src.workflow.news_evidence import build_evidence_bundle, review_numeric_claims, validate_claim_refs

CONTEXT = StageContext("run", "exec", "news", "candidate", "account", "frozen", pipeline_version="staged_v2")


def evidence(text):
    return build_evidence_bundle(SimpleNamespace(url="https://example.org/report", source="report",
        seendate="2026-10-10"), snapshot=text, context=CONTEXT)


@pytest.mark.parametrize("source,claim", [
    ("The company recalled 30,000 bottles.", "该公司召回3万瓶产品。"),
    ("Three officials were injured.", "3名官员受伤。"),
    ("At least three people were injured.", "至少3人受伤。"),
    ("收入增长10%。", "收入增长10%。"),
])
def test_equivalent_quantity_has_evidence(source, claim):
    assert review_numeric_claims(claim, evidence(source)) == []


@pytest.mark.parametrize("source,claim", [
    ("At least three people were injured.", "3人受伤。"),
    ("Three officials were injured.", "3名官员死亡。"),
    ("公司计划裁员30人。", "公司已裁员30人。"),
    ("甲公司裁员30人，乙公司招聘30人。", "乙公司裁员30人。"),
    ("苹果公司裁员30人。微软公司招聘30人。", "微软公司裁员30人。"),
    ("苹果裁员30人。微软招聘30人。", "微软裁员30人。"),
    ("Acme laid off 30 people. Globex hired 30 people.", "Globex laid off 30 people."),
    ("事故造成3人死亡和4人受伤。", "事故造成3人受伤。"),
    ("公司于10月10日公告此事。", "事故发生于10月10日。"),
    ("收入增长10%。", "收入增长10个百分点。"),
    ("没有3名官员受伤。", "3名官员受伤。"),
])
def test_same_digits_do_not_prove_different_fact(source, claim):
    assert review_numeric_claims(claim, evidence(source))


def test_reference_requires_exact_snapshot_span():
    bundle = evidence("公司召回30000瓶产品。")
    assert not validate_claim_refs([{"start":0,"end":2,"quote":"公司"}], bundle)
    assert validate_claim_refs([{"start":0,"end":2,"quote":"政府"}], bundle)
    assert validate_claim_refs([{"start":-1,"end":2,"quote":"公司"}], bundle)
