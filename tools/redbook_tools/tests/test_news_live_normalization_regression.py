from types import SimpleNamespace

import pytest

from src.workflow import create_post as w


def source(text):
    return SimpleNamespace(title="", description=text, content="")


def test_complete_last_fact_survives_cleanup_and_repeated_normalization():
    first = "《卫报》报道，霍尔木兹海峡的原油出口已大体恢复至战前水平，产油方采用替代方式将燃油运出海湾。"
    last = "报道同时指出，柴油等成品油运输仍受到限制，相关物流尚未完全恢复。"
    content = w._limit_daily_news_content(first + last)
    assert last in content
    assert w._limit_daily_news_content(content) == content


@pytest.mark.parametrize("evidence,claim", [
    ("His wife was seven months pregnant.", "其妻子怀孕七个月。"),
    ("The delay lasted three months.", "延误持续3个月。"),
    ("A businessman was attacked.", "一名商人遭到袭击。"),
    ("Three intruders broke in.", "三名入侵者闯入。"),
])
def test_supported_live_quantity_translations(evidence, claim):
    assert not w._daily_news_has_unsupported_numeric_claim(claim, source(evidence))


@pytest.mark.parametrize("evidence,claim", [
    ("Seven months pregnant.", "怀孕七天。"),
    ("Three intruders broke in.", "30名入侵者闯入。"),
    ("A businessman spoke.", "仅一名商人发言。"),
    ("The assault lasted 45 minutes.", "袭击持续约45分钟。"),
])
def test_quantity_fix_keeps_units_roles_and_bounds_strict(evidence, claim):
    assert w._daily_news_has_unsupported_numeric_claim(claim, source(evidence))
