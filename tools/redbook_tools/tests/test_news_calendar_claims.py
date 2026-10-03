from types import SimpleNamespace
import socket

import pytest

from src.workflow import create_post as w


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("calendar tests must not connect to services")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def source(text="", **metadata):
    return SimpleNamespace(title=text, description="", content="", **metadata)


@pytest.mark.parametrize("evidence,claim", [
    ("Enter Shikari announce indefinite hiatus from 2027.", "乐队宣布2027年无限期暂停。"),
    ("The band stopped in 2027.", "乐队于2027年停止活动。"),
    ("The band has been inactive since 2027.", "乐队自2027年起停止活动。"),
    ("Concerns over the university's 2024 investigation.", "外界质疑校方2024年的调查。"),
    ("She was convicted over a 1994 murder.", "她因1994年的谋杀案被定罪。"),
    ("乐队宣布2027年无限期暂停。", "The band stopped in 2027."),
    ("The band stopped on October 1.", "乐队于10月1日停止活动。"),
    ("The band stopped on 1 October.", "乐队于10月1日停止活动。"),
    ("The band stopped on Oct. 1st.", "乐队于10月1日停止活动。"),
    ("The band stopped on 1st Oct.", "乐队于10月1日停止活动。"),
    ("The band stopped on OCTOBER 01, 2027.", "乐队于2027年10月1日停止活动。"),
    ("The band stopped on 1 October 2027.", "乐队于2027年10月1日停止活动。"),
    ("乐队于2027年10月1日停止活动。", "The band stopped on October 1, 2027."),
    ("The band stopped on October 1, 2027.", "乐队于2027年停止活动。"),
    ("The band stopped on October 1, 2027.", "乐队于10月1日停止活动。"),
    ("The band stopped on February 29, 2028.", "乐队于2028年2月29日停止活动。"),
])
def test_explicit_calendar_translation_is_supported(evidence, claim):
    assert not w._daily_news_has_unsupported_numeric_claim(claim, source(evidence))


@pytest.mark.parametrize("evidence,claim", [
    ("The band stopped from 2027.", "乐队宣布2028年停止活动。"),
    ("The band stopped.", "乐队宣布2027年停止活动。"),
    ("The band stopped on October 1.", "乐队于10月9日停止活动。"),
    ("The band stopped on October 1.", "乐队于2027年10月1日停止活动。"),
    ("The band stopped in 2027. Another show was on October 1.", "乐队于2027年10月1日停止活动。"),
    ("乐队于2027年暂停。另一场演出在10月1日。", "乐队于2027年10月1日停止活动。"),
    ("The show was on October 2, 2027, and October 1, 2028.", "演出于2027年10月1日举行。"),
    ("The band stopped on October 1, 2028.", "乐队于2027年10月1日停止活动。"),
    ("There were 2027 people.", "乐队于2027年停止活动。"),
    ("The band was inactive for 2027 years.", "乐队于2027年停止活动。"),
    ("乐队持续2027年。", "The band stopped in 2027."),
    ("The band stopped in 2027.", "乐队持续2027年。"),
    ("The band stopped in 2027.5.", "乐队于2027年停止活动。"),
    ("The band stopped in 20270.", "乐队于2027年停止活动。"),
    ("The band stopped in 2027-A.", "乐队于2027年停止活动。"),
    ("The band stopped in 2027%.", "乐队于2027年停止活动。"),
    ("The band earned in 2027 euros.", "乐队于2027年停止活动。"),
    ("A 2024 dollar investigation budget.", "2024年的调查。"),
    ("A 2024-person investigation team.", "2024年的调查。"),
    ("A 2024.5 investigation score.", "2024年的调查。"),
    ("It was not a 2024 investigation.", "2024年的调查。"),
    ("This was after the 2024 investigation.", "这发生于2024年。"),
    ("The show was on October 1, 2027.5.", "演出于2027年10月1日举行。"),
    ("The show was on 1 October 2027-A.", "演出于2027年10月1日举行。"),
    ("The band did not stop in 2027.", "乐队于2027年停止活动。"),
    ("乐队并非在2027年停止活动。", "The band stopped in 2027."),
    ("The show was after October 1.", "演出于10月1日举行。"),
    ("The show was on approximately October 1.", "演出于10月1日举行。"),
    ("The show was on October 32.", "演出于10月32日举行。"),
    ("The show was on April 31.", "演出于4月31日举行。"),
    ("The show was on February 29, 2027.", "演出于2027年2月29日举行。"),
    ("The show was on October 1st.", "The show was on October 1th."),
    ("More than 100 people arrived on October 1.", "10月1日，100人抵达。"),
    ("Three people arrived on October 1.", "10月1日，30人抵达。"),
    ("The show lasted three years.", "演出持续3个月。"),
])
def test_calendar_normalization_does_not_invent_evidence(evidence, claim):
    assert w._daily_news_has_unsupported_numeric_claim(claim, source(evidence))


@pytest.mark.parametrize("claim", [
    "乐队于2027年停止活动。",
    "乐队于10月1日停止活动。",
    "乐队于2027年10月1日停止活动。",
])
def test_publication_metadata_never_proves_calendar_claim(claim):
    picked = source(
        "The band announced a hiatus.", seendate="2027-10-01T12:00:00Z",
        published_at="2027-10-01", event_date="2027-10-01", url="https://example.com/2027/10/01",
    )
    assert w._daily_news_has_unsupported_numeric_claim(claim, picked)


@pytest.mark.parametrize("text,want", [
    ("乐队于2027年停止活动。", {("2027", "calendar_year", "exact")}),
    ("The band stopped from 2027.", {("2027", "calendar_year", "exact")}),
    ("乐队于10月1日停止活动。", {("10-1", "month_day", "exact")}),
    ("The band stopped on October 1.", {("10-1", "month_day", "exact")}),
    ("The show lasted three years.", {("3", "年", "exact")}),
])
def test_calendar_spans_are_not_also_duration_quantities(text, want):
    assert w._daily_news_numeric_claims(text) == want


def test_calendar_fix_preserves_real_lead_without_rewriting_or_deleting_it():
    picked = source("Enter Shikari announce indefinite hiatus from 2027.", seendate="2026-10-02")
    lead = "乐队Enter Shikari宣布2027年起无限期暂停活动。"
    body = f"内容：\n{lead}\n\n评价：\n后续活动安排仍需以乐队公开信息为准。\n\n日期：2026-10-02\n\n来源：公开声明"
    result = w._finalize_daily_news_body(body, picked, "国际新闻", title_hint="乐队宣布暂停活动")
    assert result.startswith("内容：\n" + lead)
