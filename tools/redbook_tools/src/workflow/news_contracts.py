"""Versioned, credential-free contracts for independently recoverable news work."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any

CONTRACT_VERSION = "news-stages-v1"
PIPELINE_VERSIONS = frozenset({"legacy", "staged_v2"})


def content_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StageContext:
    root_run_id: str
    execution_id: str
    job_key: str
    candidate_id: str
    namespace: str
    config_hash: str
    post_id: str = ""
    rule_version: str = CONTRACT_VERSION
    capability_hash: str = ""
    pipeline_version: str = "legacy"

    def __post_init__(self):
        if self.pipeline_version not in PIPELINE_VERSIONS:
            raise ValueError("unsupported generation_pipeline_version")
        if not self.namespace or not self.config_hash:
            raise ValueError("stage namespace and frozen config hash are required")

    def cache_key(self, stage: str, inputs: Any) -> str:
        return content_hash({"namespace": self.namespace, "stage": stage, "input": inputs,
                             "config": self.config_hash, "rules": self.rule_version,
                             "capabilities": self.capability_hash})


@dataclass(frozen=True)
class ReviewIssue:
    code: str
    scope: str
    severity: str = "error"
    evidence_id: str = ""
    field: str = ""
    repairable: bool = False
    verdict: str = "unresolved"


@dataclass(frozen=True)
class EvidenceBundle:
    event_id: str
    snapshot_hash: str
    source_url: str
    publisher: str
    published_at: str
    snapshot: str
    spans: tuple[dict[str, Any], ...] = ()
    unknowns: tuple[str, ...] = ()


@dataclass(frozen=True)
class TextArtifact:
    title: str
    content: str
    comment: str
    complexity: str = "normal"
    claim_refs: tuple[dict[str, Any], ...] = ()
    issues: tuple[ReviewIssue, ...] = ()

    @property
    def version(self) -> str:
        return content_hash(asdict(self))


@dataclass(frozen=True)
class SceneArtifact:
    text_hash: str
    evidence_hash: str
    scene_prompt: str
    fact_ids: tuple[str, ...] = ()
    illustration_type: str = "concept"
    prohibitions: tuple[str, ...] = ()
    issues: tuple[ReviewIssue, ...] = ()


@dataclass(frozen=True)
class ReadyArtifact:
    post_id: str
    content_hash: str
    image_hash: str
    policy_hash: str
    reservation_id: str
    decisions: dict[str, Any] = field(default_factory=dict)
