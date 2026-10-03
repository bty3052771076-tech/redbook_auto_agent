from types import SimpleNamespace

import pytest

from src.workflow import create_post as w


@pytest.mark.parametrize("evidence,claim", [
    ("Nearly 90 years after her disappearance.", "失踪近90年后，搜寻重新启动。"),
    ("On the 36th anniversary of reunification.", "德国统一36周年之际公布调查。"),
    ("On the 36th anniversary of reunification.", "德国统一日36周年这一时间点公布调查。"),
    ("Hundreds of people joined the protest.", "数百人参加了抗议。"),
])
def test_source_supported_prose_quantities_are_not_rejected(evidence, claim):
    picked = SimpleNamespace(title="", description=evidence, content="")
    assert not w._daily_news_has_unsupported_numeric_claim(claim, picked)


@pytest.mark.parametrize("evidence,claim", [
    ("Nearly 90 years after her disappearance.", "失踪90年后重新搜寻。"),
    ("Nearly 90 years after her disappearance.", "1937年失踪后重新搜寻。"),
    ("On the 36th anniversary of reunification.", "德国统一37周年之际公布调查。"),
    ("On the 36th anniversary of reunification.", "活动持续36周。"),
    ("Hundreds of people joined the protest.", "100人参加了抗议。"),
    ("Hundreds of people joined the protest.", "数千人参加了抗议。"),
])
def test_approximation_fix_does_not_license_exact_or_changed_quantities(evidence, claim):
    picked = SimpleNamespace(title="", description=evidence, content="")
    assert w._daily_news_has_unsupported_numeric_claim(claim, picked)


def test_explicit_one_day_quantity_is_not_a_unification_day_name():
    picked = SimpleNamespace(title="", description="The ceremony lasted two days.", content="")
    assert w._daily_news_has_unsupported_numeric_claim("庆典持续一日。", picked)
