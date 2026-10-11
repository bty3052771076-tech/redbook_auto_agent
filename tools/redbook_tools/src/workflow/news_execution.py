"""Recoverable stage calls used by the existing news candidate coordinator."""
from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
import hashlib
import os
from uuid import uuid4

from .news_contracts import StageContext, content_hash
from .performance import RunContext, stage_span
from .request_outcome import CompletedModelError, remote_outcome_unknown
from .model_observation import model_observation
from .stage_store import NewsStageStore, StageKey


class StagePending(RuntimeError):
    pass


def job_stage_options(job, context, *, run_id, performance_mode):
    if context.get("generation_pipeline_version") != "staged_v2":
        return {}
    return {"generation_pipeline_version": "staged_v2", "stage_context": StageContext(
        root_run_id=str(context.get("agent_run_id") or run_id), execution_id=uuid4().hex,
        job_key=str(context.get("agent_job_key") or job.kind), candidate_id="",
        namespace=content_hash(str(Path.cwd().resolve())),
        config_hash=content_hash([vars(job), performance_mode]), pipeline_version="staged_v2")}


def build_stage_runtime(configs, *, job_key, options, performance_mode=None, context=None):
    from .model_capacity import ModelCapacityStore
    frozen=content_hash({"models":[vars(config) for config in configs],"options":options,
        "image_config":{k:v for k,v in os.environ.items() if k.startswith(("IMAGE_","AUTO_","MINIMAX_","OPENCODEX_","RUN_MODEL_"))}})
    context=context or StageContext(uuid4().hex,uuid4().hex,job_key,"",
        content_hash([str(Path.cwd().resolve()),os.getenv("XHS_CHROME_USER_DATA_DIR",""),os.getenv("XHS_CHROME_PROFILE","")]),
        frozen,pipeline_version="staged_v2")
    from src.agent.capabilities.dispatcher import current_invocation
    dispatcher, _, _ = current_invocation()
    snapshot = getattr(dispatcher, "snapshot", {}) or {}
    permissions = {"namespace": snapshot.get("namespace"), "tools": snapshot.get("tools", {})}
    capability_hash = content_hash([context.capability_hash, permissions,
                                   os.getenv("AGENT_CAPABILITY_NAMESPACE", "")])
    profiles = {key: value for key, value in os.environ.items()
                if key.startswith(("XHS_", "TOUTIAO_")) and
                any(part in key for part in ("PROFILE", "USER_DATA_DIR", "ACCOUNT"))}
    # A browser directory is not an authenticated account ID. Until identity is
    # verified by the platform, share artifacts only inside the same root run.
    namespace = content_hash([context.namespace, context.root_run_id, profiles,
                              capability_hash, str(Path.cwd().resolve())])
    context=replace(context,namespace=namespace,capability_hash=capability_hash,
                    config_hash=content_hash([context.config_hash,frozen]),pipeline_version="staged_v2")
    store=NewsStageStore()
    store.ensure_schema()
    ModelCapacityStore(store.store).ensure_schema()
    return NewsStageRuntime(context,store,RunContext.create(
        performance_mode,run_id=context.execution_id,telemetry_dir="data/runs/news-performance"))


class NewsStageRuntime:
    def __init__(self, context: StageContext, store: NewsStageStore, telemetry: RunContext | None = None):
        self.context, self.store, self.telemetry = context, store, telemetry

    def for_candidate(self, identity: str):
        return NewsStageRuntime(replace(self.context, candidate_id=identity), self.store, self.telemetry)

    def key(self, stage, inputs):
        c = self.context
        if stage == "post_identity":
            inputs = {"root_run_id": c.root_run_id, "job_key": c.job_key, "input": inputs}
        return StageKey(c.namespace,c.root_run_id,c.job_key,c.candidate_id,stage,c.cache_key(stage,inputs),c.rule_version)

    def call(self, stage, inputs, callback, *, external=True):
        key = self.key(stage, inputs)
        cached = self.store.get_cached(key)
        if cached is not None:
            if self.telemetry:
                self.telemetry.record(stage,"cache_hit",candidate_id=self.context.candidate_id,input_hash=key.input_hash)
            return deepcopy(cached["result"])
        owner = self.context.execution_id + ":" + uuid4().hex
        lease = self.store.claim_stage(key,owner=owner,lease_seconds=600,external=external)
        if lease is None:
            cached = self.store.get_cached(key)
            if cached is not None:
                return deepcopy(cached["result"])
            raise StagePending("STAGE_PENDING_OR_UNKNOWN: existing request requires reconciliation")
        observed = {}
        try:
            span = (stage_span(self.telemetry,stage,span_id=owner,external=external,root_run_id=self.context.root_run_id,
                              execution_id=self.context.execution_id,candidate_id=self.context.candidate_id)
                    if self.telemetry else nullcontext())
            with model_observation(self.telemetry, stage, self.context) as observed, span:
                result = callback()
            if isinstance(result,dict) and result.get("_fallback_error"):
                raise CompletedModelError("STAGE_MODEL_ERROR: "+str(result["_fallback_error"]))
            if not self.store.commit_stage(lease,payload={"result":result}):
                raise StagePending("STAGE_LEASE_LOST: result retained remotely but not committed")
            return deepcopy(result)
        except BaseException as exc:
            completed = observed.get("completed", 0) == observed.get("attempt", 0) > 0 and not observed.get("unknown")
            self.store.fail_stage(lease, code=type(exc).__name__,
                                  uncertain=external and (observed.get("unknown", False) or
                                                         (not completed and remote_outcome_unknown(exc))))
            raise

    def images(self, inputs, callback):
        def create():
            paths, metadata, fallback = callback()
            return {"paths":[str(Path(path).resolve()) for path in paths],"metadata":metadata,"fallback":fallback,
                    "hashes":[hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths]}
        result = self.call("image",inputs,create)
        paths = [Path(path) for path in result["paths"]]
        if not paths or len(paths) != len(result["hashes"]):
            raise ValueError("IMAGE_CACHE_INVALID: missing image manifest")
        for path, expected in zip(paths,result["hashes"]):
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("IMAGE_CACHE_INVALID: retained image is missing or changed; no blind regeneration")
        return paths,result["metadata"],result["fallback"]
