"""Conservative sentence-local evidence alignment; unknown claims stay unresolved."""
from __future__ import annotations

from decimal import Decimal
import re
from typing import Any

from .news_contracts import EvidenceBundle, ReviewIssue, StageContext, content_hash

_ROLES = {
    "injured": r"受伤|伤者|\binjured\b|\bwounded\b",
    "dead": r"死亡|死者|身亡|遇难|\bkilled\b|\bdead\b|\bdied\b",
    "layoff": r"裁员|解雇|\blaid off\b|\blayoffs?\b",
    "hire": r"招聘|录用|\bhir(?:e|ed|ing)\b",
    "recall": r"召回|\brecalled?\b",
    "announcement": r"公告|公布消息|\bannounced\b",
    "incident": r"事故发生|袭击发生|\boccurred\b",
}
_QUALITIES = {
    "planned": r"计划|拟|将|\bplan(?:s|ned)?\b|\bwill\b|\bproposed\b",
    "negative": r"没有|并未|否认|\bno\b|\bnot\b|\bdenied\b",
}
_EXTENDED = re.compile(
    r"(?P<bound>至少|约|近|超过|at least\s+|about\s+|over\s+)?"
    r"(?<![\d.,])(?P<n>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<scale>万|亿|thousand\s+|million\s+|billion\s+)?"
    r"(?P<unit>瓶|bottles?|个百分点|percentage points?)", re.I)


def _clauses(text: str):
    # Decimal points and thousands separators must not split a quantity.
    return [part for part in re.split(r"[。！？!?;；\n]|(?<!\d)[,，]|(?<!\d)\.(?!\d)", text) if part.strip()]


def _extended_claims(text: str):
    claims = set()
    for m in _EXTENDED.finditer(text):
        value = Decimal(m["n"].replace(",", ""))
        scale = (m["scale"] or "").strip().lower()
        value *= {"万": Decimal(10000), "亿": Decimal(100000000),
                  "thousand": Decimal(1000), "million": Decimal(1000000),
                  "billion": Decimal(1000000000)}.get(scale, Decimal(1))
        unit = "bottle" if m["unit"].lower().startswith("bottle") or m["unit"] == "瓶" else "percentage_point"
        bound = {"至少":"gte", "at least":"gte", "约":"approx", "近":"approx", "about":"approx",
                 "超过":"gt", "over":"gt"}.get((m["bound"] or "").strip().lower(), "exact")
        claims.add((str(value.normalize()), unit, bound))
    return claims


def _claims(text: str):
    # Reuse the regression-covered ordinal/date/person parser; new units extend it.
    from .create_post import _daily_news_numeric_claims
    return _daily_news_numeric_claims(text) | _extended_claims(text)


def _tags(text: str, patterns: dict[str, str]):
    return {name for name, pattern in patterns.items() if re.search(pattern, text, re.I)}


def _entities(text: str):
    entities = set(re.findall(r"[\u4e00-\u9fff]{1,20}(?:公司|集团|政府|法院)|\b[A-Z][A-Za-z0-9_-]+(?:\s+(?:Corp|Inc|Ltd))?\b", text))
    subject = re.match(r"\s*([\u4e00-\u9fff]{2,20}?)(?:计划|拟|已|将)?(?:裁员|招聘|召回|解雇|录用)", text)
    if subject:
        entities.add(subject[1])
    generic = {"The", "A", "At", "Three", "One", "Two", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
               "该公司", "这家公司", "公司", "集团", "政府"}
    return entities-generic


def _unambiguous(part: str, sentence: str) -> bool:
    # A bag of numbers and roles cannot associate two casualty counts. Preserve
    # an exact clause, otherwise require a narrower evidence span or a rewrite.
    roles = _tags(part, _ROLES)-{"announcement", "incident"}
    return len(roles) <= 1 or len(_claims(part)) <= 1 or part.strip() == sentence.strip()


def build_evidence_bundle(item, *, snapshot: str, context: StageContext) -> EvidenceBundle:
    if not snapshot.strip():
        raise ValueError("source snapshot is empty")
    spans = []
    for match in re.finditer(r"[^\n]+", snapshot):
        spans.append({"id": f"p{len(spans)+1}", "start": match.start(), "end": match.end(), "quote": match.group()})
    return EvidenceBundle(context.candidate_id, content_hash(snapshot), str(getattr(item,"url","")),
                          str(getattr(item,"source", "")), str(getattr(item,"seendate", "")), snapshot, tuple(spans))


def validate_claim_refs(refs, bundle: EvidenceBundle) -> list[ReviewIssue]:
    issues = []
    for ref in refs:
        start, end = ref.get("start"), ref.get("end")
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(bundle.snapshot)
                or bundle.snapshot[start:end] != ref.get("quote")):
            issues.append(ReviewIssue("invalid_evidence_span", "text", field="claim_refs", repairable=True))
    return issues


def review_numeric_claims(text: str, bundle: EvidenceBundle) -> list[ReviewIssue]:
    evidence = [(part, _claims(part)) for part in _clauses(bundle.snapshot)]
    issues = []
    for sentence in _clauses(text):
        for claim in _claims(sentence):
            matches = [part for part, values in evidence if claim[0] is not None and claim in values]
            roles, quality, entities = _tags(sentence, _ROLES), _tags(sentence, _QUALITIES), _entities(sentence)
            compatible = [part for part in matches
                          if (not roles or roles <= _tags(part, _ROLES))
                          and quality == _tags(part, _QUALITIES)
                          and (not entities or entities <= _entities(part))
                          and _unambiguous(part, sentence)]
            if compatible:
                continue
            issues.append(ReviewIssue("unsupported_numeric_claim", "text", field="content",
                                      evidence_id=bundle.snapshot_hash, repairable=True,
                                      verdict="contradicted" if matches else "unresolved"))
    return issues
