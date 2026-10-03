from types import SimpleNamespace
import socket

import pytest

from src.workflow import create_post as w


PILOT_SOURCE = (
    "Who is the 'hero' Indian pilot who was stabbed on Israel-bound flight? "
    "Capt Smit Machchhar was attacked by another pilot onboard the Flydubai flight bound for Tel Aviv."
)
SPACE_SOURCE = (
    "SpaceX flight led by first Black female commander sets US speed record. "
    "Four astronauts pulled up at International Space Station after eight-hour express flight led by Jessica Watkins. "
    "SpaceX launched commander Jessica Watkins and her crew after a three-week delay."
)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("numeric tests must not connect to services")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def unsupported(claim, evidence):
    return w._daily_news_has_unsupported_numeric_claim(
        claim, SimpleNamespace(title=evidence, description="", content=""),
    )


@pytest.mark.parametrize("evidence,claim", [
    (PILOT_SOURCE, "一名印度飞行员在飞往以色列的航班上被刺伤。"),
    (PILOT_SOURCE, "机长斯米特遭另一名飞行员袭击。"),
    (SPACE_SOURCE, "杰西卡成为第一位黑人女性指挥官。"),
    (SPACE_SOURCE, "杰西卡成为第一位带领机组进入轨道的黑人女性指挥官。"),
    (SPACE_SOURCE, "杰西卡成为首位黑人女性指挥官。"),
    (SPACE_SOURCE, "4名宇航员在8小时飞行后抵达国际空间站，发射此前因维修推迟3周。"),
    ("A woman was injured.", "一名女子受伤。"),
    ("As an Indian woman, I describe my experience.", "作为一名印度女性，我描述自己的经历。"),
    ("A Sydney man was arrested.", "一名悉尼男子被捕。"),
    ("An officer spoke.", "一位官员发言。"),
    ("Another pilot spoke.", "另一名飞行员发言。"),
    ("A passenger spoke.", "一名乘客发言。"),
    ("One pilot spoke.", "一名飞行员发言。"),
    ("Two pilots spoke.", "2名飞行员发言。"),
    ("One astronaut spoke.", "1名宇航员发言。"),
    ("Four astronauts arrived.", "4名宇航员抵达。"),
    ("One commander spoke.", "1名指挥官发言。"),
    ("Two commanders spoke.", "2名指挥官发言。"),
    ("Two men spoke.", "2名男子发言。"),
    ("Two women spoke.", "2名女子发言。"),
    ("Three officers spoke.", "3名官员发言。"),
    ("An eight-hour flight.", "飞行持续8小时。"),
    ("A three-week delay.", "延误3周。"),
    ("A twenty-one-hour flight.", "飞行持续21小时。"),
    ("The first pilot spoke.", "第一位飞行员发言。"),
    ("The third officer spoke.", "第三位官员发言。"),
    ("第一位飞行员发言。", "The first pilot spoke."),
])
def test_frozen_pool_and_limited_person_translations(evidence, claim):
    assert not unsupported(claim, evidence)


@pytest.mark.parametrize("evidence,claim", [
    ("A woman was injured.", "3名女子受伤。"),
    ("A pilot spoke.", "1名飞行员发言。"),
    ("A pilot spoke.", "仅一名飞行员发言。"),
    ("A pilot spoke.", "至少一名飞行员发言。"),
    ("A woman spoke.", "一名飞行员发言。"),
    ("A passenger spoke.", "一名飞行员发言。"),
    ("Another commander spoke.", "一名飞行员发言。"),
    ("One person spoke.", "第一位人员发言。"),
    ("Three officials spoke.", "第三位官员发言。"),
    ("The third pilot spoke.", "3名飞行员发言。"),
    ("The first pilot spoke.", "第三位飞行员发言。"),
    ("The first pilot spoke.", "第一位指挥官发言。"),
    ("A commander spoke.", "首位指挥官发言。"),
    ("The first Black female commander spoke.", "第一位指挥官发言。"),
    ("The first female commander spoke.", "第一位黑人女性指挥官发言。"),
    ("Not a pilot but a passenger was injured.", "一名飞行员受伤。"),
    ("No woman was injured.", "一名女子受伤。"),
    ("A pilot was not injured.", "一名飞行员受伤。"),
    ("If a pilot were injured, help would arrive.", "一名飞行员受伤。"),
    ("A pilot could be injured.", "一名飞行员受伤。"),
    ("Another pilot might be injured.", "另一名飞行员受伤。"),
    ("A proposed commander could lead the crew.", "一名指挥官带领机组。"),
    ("A few officials were injured.", "一名官员受伤。"),
    ("A hundred people arrived.", "一名人员抵达。"),
    ("A pair of pilots arrived.", "一名飞行员抵达。"),
    ("A group of pilots arrived.", "一名飞行员抵达。"),
    ("A pilot plan was discussed.", "一名飞行员发言。"),
    ("No first pilot was selected.", "第一位飞行员获选。"),
    ("The first pilot could speak.", "第一位飞行员发言。"),
    ("A pilot spoke.", "第一位飞行员发言。"),
    ("Two pilots spoke.", "20名飞行员发言。"),
    ("More than three pilots spoke.", "3名飞行员发言。"),
    ("Not three pilots were injured.", "3名飞行员受伤。"),
    ("A nearly eight-hour flight.", "飞行持续8小时。"),
    ("A one-hundred-and-three-hour delay.", "延误3小时。"),
    ("Three hours delayed passengers.", "3名乘客被延误。"),
])
def test_mentions_ranks_and_counts_never_prove_each_other(evidence, claim):
    assert unsupported(claim, evidence)


def test_article_is_role_mention_not_exact_total():
    assert w._daily_news_numeric_claims("A pilot spoke.") == {("pilot", "person_mention", "singular")}
    assert w._daily_news_numeric_claims("一名飞行员发言。") == {("pilot", "person_mention", "singular")}
    assert ("1", "person", "exact") not in w._daily_news_numeric_claims("Another pilot spoke.")


@pytest.mark.parametrize("text", ["公司将这一项目用于测试。", "这一项计划仍未实施。", "那一个项目被取消。"])
def test_demonstrative_item_reference_is_not_exact_quantity(text):
    assert not w._daily_news_numeric_claims(text)


@pytest.mark.parametrize("text", ["公司发布一项计划。", "公司发布1项计划。", "公司仅有一个项目。", "这两个项目被取消。"])
def test_explicit_item_quantities_remain_checked(text):
    assert w._daily_news_numeric_claims(text)


def test_complete_ordinal_is_not_a_cardinal_substring():
    assert w._daily_news_numeric_claims("第三位官员发言。") == {("3", "ordinal:officer", "exact")}
    assert w._daily_news_numeric_claims("The third officer spoke.") == {("3", "ordinal:officer", "exact")}
