"""One host-owned action per process; no batch agent, arbitrary shell or routing."""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
import io
import json
import os
from pathlib import Path
import re
import sys
from threading import RLock
from uuid import uuid4

from jsonschema import Draft202012Validator

from backend.dialogue_tools import TOOL_CATALOG, resource_id, capability_view
from src.agent.capabilities.dispatcher import ToolDispatcher
from src.agent.capabilities.models import CapabilityError, safe
from src.agent.capabilities.policy import validate_tool
from src.agent.capabilities.service import CapabilityService
from src.agent.capabilities.store import CapabilityStore
from src.model_platforms.security import PlatformError
from src.model_platforms.store import atomic_json


_ENV_LOCK = RLock()
_OUTPUT = sys.stdout


class _ActionStopped(BaseException):
    """Do not let browser progress handlers swallow a control or revocation gate."""
    def __init__(self, error):
        self.error, self.code = error, error["code"]


def _private_directory(value):
    path = Path(value).resolve()
    if path.drive.lower() != "e:" or path == Path(path.anchor):
        raise CapabilityError("ACTION_DIRECTORY_INVALID", "Action directories must be private directories on E:")
    return path


def _redact(value, env):
    value = safe(value)
    secrets = [str(v) for k, v in env.items() if v and len(str(v)) >= 4
               and re.search(r"KEY|TOKEN|SECRET|PASSWORD|DATABASE_URL", k, re.I)]
    def clean(item):
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "[redacted]")
            return item
        if isinstance(item, dict):
            return {k: clean(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in item]
        return item
    return clean(value)


def _progress(env, **event):
    event = _redact(event, env)
    if "detail" in event and not isinstance(event["detail"], str):
        event["detail"] = json.dumps(event["detail"], ensure_ascii=True, default=str)
    _OUTPUT.write(json.dumps(event, ensure_ascii=True, default=str) + "\n")
    _OUTPUT.flush()


class _ProgressOutput(io.TextIOBase):
    def __init__(self, env):
        self.env, self.pending = env, ""

    def write(self, value):
        self.pending += value
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line.strip():
                _progress(self.env, type="progress", detail=line[:4000])
        if len(self.pending) > 8000:
            self.pending = ""
        return len(value)

    def flush(self):
        if self.pending.strip():
            _progress(self.env, type="progress", detail=self.pending[:4000])
        self.pending = ""


@contextmanager
def _environment(env):
    # execute is also importable for offline tests; production uses a fresh process.
    with _ENV_LOCK:
        previous, cwd = dict(os.environ), Path.cwd()
        try:
            os.environ.update(env)
            root = _private_directory(env.get("REDBOOK_RUNTIME_ROOT") or cwd)
            os.chdir(root)
            yield root
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(previous)


def _check_execution(context, store):
    if not context.get("execution_id"):
        raise CapabilityError("EXECUTION_NOT_ACTIVE", "Missing host execution identity")
    with store.knowledge_store.connection() as conn:
        row = conn.execute("""SELECT state,execution_id,thread_id FROM agent.dialogue_sessions
            WHERE namespace=%s AND conversation_id=%s""",
            (context["namespace"], context["conversation_id"])).fetchone()
    if (not row or row["state"] != "running" or row["execution_id"] != context["execution_id"]
            or row["thread_id"] != context["thread_id"]):
        raise CapabilityError("EXECUTION_NOT_ACTIVE", "Execution was paused, cancelled or replaced")


class _ActionDispatcher(ToolDispatcher):
    def __init__(self, store, snapshot, context, manager, tool_name, **kwargs):
        super().__init__(store, snapshot, **kwargs)
        self.context, self.manager, self.tool_name = context, manager, tool_name

    def check(self, identity, stage):
        _check_execution(self.context, self.store)
        rows = {row["id"]: row for row in self.manager.all_tools()}
        for row in rows.values():
            if row.get("revoked_at") or row.get("connection", {}).get("revoked_at"):
                row["enabled"] = False
                row["revoked_at"] = row.get("revoked_at") or row["connection"]["revoked_at"]
        if self.tool_name == "generate_news":
            for key in ("builtin:news.generate", "builtin:wow.generate"):
                if key in rows:
                    rows[key] = {**rows[key], "dependencies": [dependency for dependency in
                        rows[key].get("dependencies", []) if dependency != "builtin:image.generate"]}
        self.snapshot["tools"] = rows
        validate_tool(rows, identity, stage, revoked=bool(rows.get(identity, {}).get("revoked_at")))

    def call(self, *args, **kwargs):
        self.check(args[0], kwargs["stage"])
        return super().call(*args, **kwargs)


def _manager(root, store):
    # Reuse the read-only domain API without running its schema/operation setup.
    from src.agent.capabilities.mcp_service import MCPConnectionService
    from src.agent.capabilities.skill_service import SkillService
    from src.agent.capabilities.knowledge_service import KnowledgeManagement
    manager = object.__new__(CapabilityService)
    manager.root, manager.store, manager.workbench = root, store, None
    manager.mcp = MCPConnectionService(root, store)
    manager.skills = SkillService(root, store)
    manager.knowledge = KnowledgeManagement(store.knowledge_store, store)
    return manager


def _owned_post(arguments, context):
    from src.storage.files import load_post
    identity = arguments["post_id"]
    if identity not in context.get("owned_post_ids", []):
        raise CapabilityError("POST_SCOPE_DENIED", "Draft is not owned by this conversation")
    return load_post(identity)


def _now(context):
    value = datetime.fromisoformat(context["current_date"])
    return value.replace(tzinfo=value.tzinfo or timezone(timedelta(hours=8)))


def _news_search(arguments, context, env):
    from src.news.daily_news import fetch_daily_news_candidates, filter_recent_news_items, _relevance_score
    from src.news.history import collect_used_news_url_keys, filter_used_news_items
    limit, days = arguments.get("limit", 10), arguments.get("days", 1)
    query = arguments['query'].strip()
    score = lambda item: _relevance_score(replace(item, source='', domain=''), query)
    def qualified_count(candidates):
        recent, _ = filter_recent_news_items(candidates, max_age_days=days, now=_now(context))
        return sum(score(item) > 0 for item in recent)
    # Category feeds ignore query terms; a full raw pool is not a topic match.
    items, metadata = fetch_daily_news_candidates(query, max_records=60,
        search_days=days, expand_query_variants=False, exhaustive_sources=False,
        persist_source_health=False, minimum_qualified_records=1 if query else None,
        qualified_count_callback=qualified_count if query else None,
        progress_callback=lambda stage, status, detail: _progress(env, stage=stage, status=status, detail=detail))
    items, freshness = filter_recent_news_items(items, max_age_days=days, now=_now(context))
    items, duplicates = filter_used_news_items(items, collect_used_news_url_keys())
    items.sort(key=score, reverse=True)
    matched = sum(score(item) > 0 for item in items)
    materials = _private_directory(context["session_directory"]) / "materials"
    materials.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in items[:limit]:
        if not item.url.startswith(("https://", "http://")) or not (item.source or item.domain):
            continue
        identity = uuid4().hex
        atomic_json(materials / (identity + ".json"), {
            "conversation_id": context["conversation_id"], "namespace": context["namespace"],
            "material_id": identity, "days": days, "item": asdict(item)})
        rows.append({"material_id": identity, "title": item.title, "date": item.seendate,
                     "source": item.source or item.domain, "url": item.url,
                     "relevance_score": score(item), "description_excerpt": str(item.description or '')[:240],
                     "has_content": bool(item.content)})
    return {"materials": rows, "count": len(rows), "freshness": freshness,
            "duplicates": duplicates, "collection": metadata, "gap": not bool(rows),
            "query_match_count": matched, "query_gap": bool(query) and matched == 0}


def _list_materials(arguments, context):
    from src.news.daily_news import NewsItem, filter_recent_news_items, _relevance_score
    from src.news.history import collect_used_news_url_keys, filter_used_news_items, normalize_news_url_key
    directory = _private_directory(context['session_directory']) / 'materials'
    used = collect_used_news_url_keys()
    rows, seen, skipped = [], set(), 0
    for path in sorted(directory.glob('*.json')):
        try:
            if not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError('MATERIAL_PATH_INVALID')
            saved = json.loads(path.read_text(encoding='utf-8'))
            if (saved.get('conversation_id') != context['conversation_id']
                    or saved.get('namespace') != context['namespace']
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', path.stem)):
                raise ValueError('MATERIAL_SCOPE_DENIED')
            item = NewsItem(**saved['item'])
            fresh, _ = filter_recent_news_items([item], max_age_days=int(saved['days']), now=_now(context))
            fresh, _ = filter_used_news_items(fresh, used)
            key = normalize_news_url_key(item.url)
            if (not fresh or not item.url.startswith(('https://', 'http://')) or not (item.source or item.domain)
                    or key in seen):
                skipped += 1
                continue
            seen.add(key)
            rows.append({'material_id': path.stem, 'title': item.title, 'date': item.seendate,
                'source': item.source or item.domain, 'url': item.url,
                'description_excerpt': str(item.description or '')[:240], 'has_content': bool(item.content),
                'relevance_score': _relevance_score(item, arguments.get('query', ''))})
        except (OSError, ValueError, TypeError, KeyError):
            skipped += 1
    rows.sort(key=lambda row: (row['relevance_score'], str(row['date'] or ''), row['material_id']), reverse=True)
    offset, limit = arguments.get('offset', 0), arguments.get('limit', 10)
    visible = rows[offset:offset + limit]
    return {'materials': visible, 'count': len(visible), 'total': len(rows), 'skipped_records': skipped,
        'next_offset': offset + limit if offset + limit < len(rows) else None, 'network_collection': False,
        'gap': not bool(rows)}


def _generate_news(arguments, context, env):
    from src.news.daily_news import NewsItem, filter_recent_news_items, _cjk_story_event_signature, _same_cjk_story_event
    from src.news.history import collect_used_news_url_keys, filter_used_news_items
    from src.config import load_llm_configs
    from src.storage.files import iter_posts, save_revision
    from src.storage.models import Revision, RevisionSource
    from src.workflow.create_post import _prepare_daily_news_candidate, _supervise_daily_news_candidates
    from src.workflow.model_queues import ModelWorkQueues, infer_llm_provider
    materials = _private_directory(context["session_directory"]) / "materials"
    path = materials / (arguments["material_id"] + ".json")
    if not path.is_file() or not path.resolve().is_relative_to(materials.resolve()):
        raise CapabilityError("MATERIAL_NOT_FOUND", "Select a material ID from this conversation's news_search result")
    material = json.loads(path.read_text(encoding="utf-8"))
    if material.get("conversation_id") != context["conversation_id"] or material.get("namespace") != context["namespace"]:
        raise CapabilityError("MATERIAL_SCOPE_DENIED", "Material belongs to another conversation")
    item = NewsItem(**material["item"])
    fresh, _ = filter_recent_news_items([item], max_age_days=material["days"], now=_now(context))
    fresh, _ = filter_used_news_items(fresh, collect_used_news_url_keys())
    if not fresh:
        raise CapabilityError("MATERIAL_STALE_OR_USED", "The selected material is stale, undated or already used")
    signature = _cjk_story_event_signature(item)
    for previous in iter_posts():
        if not (previous.uploaded or previous.id in context.get("owned_post_ids", [])):
            continue
        picked = previous.platform.get("news", {}).get("picked")
        if isinstance(picked, dict) and picked.get("title"):
            old = NewsItem(**{"url": "", **{key: value for key, value in picked.items() if key in NewsItem.__dataclass_fields__}})
            if _same_cjk_story_event(signature, _cjk_story_event_signature(old)):
                raise CapabilityError("MATERIAL_DUPLICATE", "This event already has a retained or delivered draft")
    configs = load_llm_configs()
    progress = lambda stage, status, detail: _progress(env, stage=stage, status=status, detail=detail)
    selected, selection = _supervise_daily_news_candidates([item], cfgs=configs,
        prompt_hint=arguments.get("instructions", ""), target_count=1, required_china_count=0,
        required_international_conflict_count=0, progress_callback=progress, column=arguments["column"])
    if not selected:
        raise CapabilityError("MATERIAL_REJECTED", "The selected material failed column evidence checks")
    # Use the one-candidate helper, not the batch coordinator or manual-mode bypasses.
    with ModelWorkQueues(llm_workers=1, image_workers=1, llm_provider=infer_llm_provider(configs)) as queues:
        result = _prepare_daily_news_candidate(candidate_index=1, picked=selected[0], cfgs=configs,
            asset_paths=[], copy_assets=True, auto_image_enabled=False,
            prompt_norm=arguments.get("instructions", ""), viewpoint_norm="",
            target_count=1, single_material_mode=False,
            base_meta={"material_id": arguments["material_id"], "search_days": material["days"],
                       "selection_pool": {"llm_supervisor": selection}},
            progress_callback=progress, model_queues=queues, post_quality_callback=None, column=arguments["column"])
    if result.status != "success" or result.post is None:
        raise CapabilityError("MATERIAL_GENERATION_FAILED", result.error or result.reason or "Selected material did not produce a draft")
    save_revision(Revision(post_id=result.post.id, source=RevisionSource.llm, content=result.draft))
    return [result.post]


def _generate_digest(arguments, context, env, dispatcher):
    column = arguments["column"]
    if column == "ai_digest":
        from src.workflow.create_post import create_daily_ai_digest_posts
        return create_daily_ai_digest_posts(asset_paths=[], count=1, auto_image=False,
            prompt_hint=arguments.get("instructions", ""), lookback_days=2)
    if column == "ai_wool":
        from src.wool.workflow import create_daily_wool_posts
        return create_daily_wool_posts(count=1, prompt_hint=arguments.get("instructions", ""),
            now=_now(context), progress=lambda stage, detail: _progress(env, stage=stage, detail=detail))
    from src.global_map import service
    if str(env.get("GLOBAL_MAP_ENABLED", "0")).lower() not in {"1", "true", "yes", "on"}:
        raise CapabilityError("GLOBAL_MAP_DISABLED", "Enable the registered global-map capability before using it")
    client, runtime = service._service_client_and_runtime()
    try:
        dispatcher.check("builtin:news.search", "preparation")
        probe = runtime.ensure_ready()
        if not probe.ready:
            raise CapabilityError(probe.error_code or "WM_NOT_READY", probe.message)
        batch = dispatcher.call("builtin:news.search", lambda: service._fetch_current_digest(client),
            stage="preparation", arguments={"source": "configured_worldmonitor"})
        def translate(snapshot):
            return dispatcher.call("builtin:writer.generate", lambda: service.translate_map_snapshot(snapshot),
                stage="generate", arguments={"column": "global_map", "operation": "translate"})
        post = service.create_global_map_post(batch, output_dir=Path(context["directory"]) / "map",
            target_date=_now(context).date().isoformat(), cutoff=datetime.now(timezone.utc),
            translate_events=translate)
        return [post] if post else []
    finally:
        runtime.release()


def _retain(posts, context):
    from src.storage.files import save_post
    if len(posts) > 1:
        raise CapabilityError("SINGLE_POST_CONTRACT", "A bounded tool unexpectedly returned multiple drafts")
    for post in posts:
        post.platform["dialogue"] = {key: context[key] for key in
            ("conversation_id", "thread_id", "action_id", "namespace")}
        save_post(post)
    return {"post_ids": [post.id for post in posts], "posts": [post.model_dump(mode="json") for post in posts],
            "gap": not bool(posts)}


def _review(post, context):
    from src.storage.files import iter_posts
    from src.validation.rules import validate_post
    from src.workflow.quality_gate import validate_post_batch
    from src.global_map.review import stored_global_map_review_issues
    from src.publish.draft_delivery import content_revision_fingerprint
    errors = list(validate_post(post).errors)
    errors.extend(issue.message for issue in validate_post_batch([post], expected_count=1,
        historical_posts=iter_posts()).issues)
    if post.platform.get("news"):
        from src.news.daily_news import NewsItem, filter_recent_news_items
        picked = post.platform["news"].get("picked") or {}
        item = NewsItem(**{"title": "", "url": "", **{
            key: value for key, value in picked.items() if key in NewsItem.__dataclass_fields__}})
        days = post.platform["news"].get("search_days") or 1
        current, _ = filter_recent_news_items([item], max_age_days=int(days), now=_now(context))
        if not current:
            errors.append("NEWS_SOURCE_EXPIRED: source date is missing or outside this draft's date window")
        if post.platform["news"].get("column") == "daily_wow":
            from apps.cli import _daily_wow_source_issues
            errors.extend(_daily_wow_source_issues([post]))
    if post.platform.get("ai_digest"):
        from apps.cli import _agent_ai_digest_review_issues
        errors.extend(_agent_ai_digest_review_issues(post, min_official=0))
    if "global_map" in post.platform:
        errors.extend(stored_global_map_review_issues(post.platform["global_map"]))
    from apps.cli import (image_score_gate_required, _local_ai_digest_vision_result,
                          _local_global_map_vision_result, cached_vision_matches)
    required = image_score_gate_required()
    if required:
        vision = (post.platform.get("quality_gate") or {}).get("vision") or {}
        local = _local_ai_digest_vision_result(post) or _local_global_map_vision_result(post)
        viewpoint = post.platform.get("news", {}).get("evaluation_viewpoint", "")
        if local is None and not (cached_vision_matches(post, vision, viewpoint)
                and vision.get("ok") and int(vision.get("score") or 0) >= 70):
            errors.append("VISION_REVIEW_REQUIRED: the configured mandatory image review has no passing current result")
    return {"post_id": post.id, "passed": not errors, "errors": list(dict.fromkeys(errors)),
            "revision_fingerprint": content_revision_fingerprint(post), "image_score_mode": "required" if required else "advisory"}


def _sync_account(arguments, context, manager, dispatcher):
    from src.publish.playwright_steps import run_collect_published_metrics_sync
    from src.storage.models import PublishedMetric
    from src.storage.files import save_published_metrics_snapshot, load_post
    from src.analytics.post_sync import sync_published_metrics_to_posts
    from src.analytics.published_metrics import analyze_published_metrics
    from src.knowledge.ingest import document_from_post
    from src.knowledge.models import KnowledgeDocument
    if not os.environ.get("XHS_CHROME_USER_DATA_DIR"):
        raise CapabilityError("PROFILE_NOT_CONFIGURED", "Configure the dedicated account profile before synchronization")

    def progress(detail):
        try:
            dispatcher.check("builtin:account.sync", "preparation")
        except CapabilityError as exc:
            raise _ActionStopped(exc.public()) from None
        _progress(os.environ, type="progress", detail=detail)

    result = run_collect_published_metrics_sync(limit=arguments.get("limit", 100), login_hold=0,
        wait_timeout_ms=120000, headless=True, progress_callback=progress)
    metrics = [PublishedMetric.model_validate(item) for item in result.get("items", [])]
    if not metrics or result.get("complete") is not True:
        raise CapabilityError("ACCOUNT_SYNC_INCOMPLETE", "Account collection was empty or incomplete; previous local analytics were not overwritten")
    dispatcher.check("builtin:account.sync", "preparation")
    snapshot = save_published_metrics_snapshot(metrics)
    synced = sync_published_metrics_to_posts(metrics)
    analysis = analyze_published_metrics(top_n=6)
    documents = [replace(document_from_post(load_post(identity)), account_namespace=context["namespace"])
                 for identity in synced.get("updated_post_ids", [])]
    documents.append(KnowledgeDocument(record_id="account-reader-preferences", record_type="account_metrics",
        account_namespace=context["namespace"], title="Account reader preferences",
        body=json.dumps(asdict(analysis), ensure_ascii=True), allowed_purposes=("operations", "style_reference"),
        metadata={"metrics_count": len(metrics), "source": "dedicated_profile_published_metrics"}))
    dispatcher.check("builtin:account.sync", "preparation")
    knowledge = manager.store.knowledge_store.upsert_documents(documents)
    indexing = manager.store.knowledge_store.index_progress(account_namespace=context["namespace"])
    return {"fetched": len(metrics), "complete": True, "scope_limit": arguments.get("limit", 100),
            "target_total": result.get("target_total"), "required_total": result.get("required_total"),
            "missing_count": result.get("missing_count", 0), "snapshot_count": snapshot["count"],
            "synced": synced, "reader_preferences": asdict(analysis), "knowledge": knowledge,
            "knowledge_status": "pending_index" if indexing.get("pending_documents") else "ready",
            "index_progress": indexing, "platform_readonly": True}


def _readback_evidence(execution, post):
    if execution.result != "saved_draft" or execution.post_id != post.id:
        return None
    for step in execution.steps:
        if step.name != "readback_saved_draft" or step.status != "success":
            continue
        # This is the persisted positive evidence emitted by the existing browser runner.
        match = re.fullmatch(r"title=True body=True images=(\d+)/(\d+)", step.detail or "")
        if match and int(match[1]) == int(match[2]) == len(post.assets) and int(match[1]) > 0:
            return {"execution_id": execution.id, "step": step.name, "title_ok": True, "body_ok": True,
                    "actual_image_count": int(match[1]), "expected_image_count": int(match[2])}
    return None


def _create_image(post):
    from src.storage.files import save_post, post_dir
    if post.platform.get("news", {}).get("column") == "daily_wow":
        from src.workflow.create_post import (_fetch_daily_news_related_images, _daily_wow_image_prompt_for_post,
            _preferred_image_title, _preferred_image_hint, _build_asset_infos)
        paths, metadata, _ = _fetch_daily_news_related_images(title=_preferred_image_title(post, post.title),
            body=post.body, topics=post.topics, prompt_hint=_preferred_image_hint(post, ""),
            dest_dir=post_dir(post.id) / "assets", ai_first=True, image_policy="ai_required",
            prompt_override=_daily_wow_image_prompt_for_post(post))
        if not paths:
            raise CapabilityError("IMAGE_GENERATION_FAILED", "No daily_wow illustration was created")
        post.assets, post.platform["images"] = _build_asset_infos(paths), metadata
        if metadata:
            post.platform["image"] = metadata[0]
        save_post(post)
    elif post.platform.get("news"):
        from src.workflow.create_post import regenerate_daily_news_post_image
        if not regenerate_daily_news_post_image(post, ""):
            raise CapabilityError("IMAGE_GENERATION_FAILED", "No image was created for this draft")
    elif post.platform.get("daily_wool"):
        from src.wool.image_edit import create_wool_image
        from src.wool.models import WoolOffer
        from src.wool.workflow import _asset_info
        wool = post.platform["daily_wool"]
        path, metadata = create_wool_image(post_id=post.id,
            offers=[WoolOffer(**offer) for offer in wool["offers"]], issue_date=wool["issue_date"])
        post.assets, post.platform["images"] = [_asset_info(path)], [metadata]
        save_post(post)
    elif not post.assets:
        raise CapabilityError("IMAGE_STRATEGY_UNAVAILABLE", "This draft has no registered image strategy")
    return {"post_ids": [post.id], "post_id": post.id, "assets": [asset.model_dump(mode="json") for asset in post.assets]}


def _save_draft(post, context, dispatcher):
    from src.storage.files import load_post
    from src.publish.concurrency import xhs_upload_slot
    from src.publish.draft_delivery import content_revision_fingerprint
    # Keep receipt recheck, submission and receipt commit inside the same shared lock.
    version = content_revision_fingerprint(post)
    with xhs_upload_slot():
        dispatcher.check("builtin:xhs.drafts.save_batch", "upload")
        current = load_post(post.id)
        if content_revision_fingerprint(current) != version:
            raise CapabilityError("DRAFT_VERSION_CHANGED", "Draft changed while waiting for the profile; approval must be renewed")
        return _save_draft_locked(current, context, dispatcher)


def _save_draft_locked(post, context, dispatcher):
    from src.storage.files import save_post, list_executions
    from src.storage.models import Execution, PostStatus, now_iso
    from src.publish.draft_delivery import has_current_draft_receipt, content_revision_fingerprint
    from src.publish.playwright_steps import _run_save_draft_sync_unlocked
    if not os.environ.get("XHS_CHROME_USER_DATA_DIR"):
        raise CapabilityError("PROFILE_NOT_CONFIGURED", "Configure the dedicated browser profile before saving")
    review = _review(post, context)
    if not review["passed"]:
        raise CapabilityError("DRAFT_REVIEW_FAILED", "; ".join(review["errors"][:5]))
    if has_current_draft_receipt(post, platform="xhs"):
        receipt = post.platform["xhs_draft"]
        previous = next((item for item in list_executions(post.id) if item.id == receipt.get("execution_id")), None)
        evidence = _readback_evidence(previous, post) if previous else None
        if not evidence:
            raise CapabilityError("DRAFT_RECEIPT_UNCERTAIN", "Existing fingerprint has no retained positive platform read-back evidence; do not upload again")
        return {"post_ids": [post.id], "post_id": post.id, "receipt": receipt, "already_current": True,
                "platform_receipt_confirmed": True, "readback_evidence": evidence}
    if post.platform.get("xhs_draft"):
        raise CapabilityError("DRAFT_REPLACEMENT_REQUIRED", "An older remote version exists; do not create a second draft")

    def progress(detail):
        try:
            dispatcher.check("builtin:xhs.drafts.save_batch", "upload")
        except CapabilityError as exc:
            raise _ActionStopped(exc.public()) from None
        _progress(os.environ, type="progress", detail=detail)

    dispatcher.check("builtin:xhs.drafts.save_batch", "upload")
    # The caller holds the existing xhs_upload_slot; use its original browser implementation.
    execution = _run_save_draft_sync_unlocked(post, assets=[asset.path for asset in post.assets],
        dry_run=False, login_hold=0, wait_timeout_ms=120000, headless=True,
        execution=Execution(post_id=post.id, result="pending"), progress_callback=progress)
    evidence = _readback_evidence(execution, post)
    if evidence is None:
        raise CapabilityError("DRAFT_SAVE_UNCERTAIN", "No verified read-back receipt; do not replay the upload")
    post.status, post.uploaded, post.uploaded_at = PostStatus.saved_draft, True, now_iso()
    post.updated_at = now_iso()
    post.platform["xhs_draft"] = {"title": post.title, "saved_at": post.updated_at,
        "execution_id": execution.id, "revision_fingerprint": content_revision_fingerprint(post)}
    save_post(post)
    if not has_current_draft_receipt(post, platform="xhs"):
        raise CapabilityError("DRAFT_RECEIPT_UNCERTAIN", "The stored receipt does not match the retained content revision")
    return {"post_ids": [post.id], "post_id": post.id, "receipt": post.platform["xhs_draft"],
            "platform_receipt_confirmed": True, "readback_evidence": evidence}


def _invoke(name, arguments, env, context, manager, dispatcher):
    if name in {"capabilities", "list_capabilities"}:
        return capability_view(list(dispatcher.snapshot["tools"].values()))
    if name == "list_skills":
        return manager.skills.list()
    if name == "read_skill":
        registered = manager.skills.get(arguments["skill_id"])
        if registered.get("revoked_at"):
            raise CapabilityError("CAPABILITY_REVOKED", "Skill was revoked", resource_id=registered["id"])
        skill = manager.skills.select("", mode="manual", names=[arguments["skill_id"]])[0]
        if skill.get("revoked_at"):
            raise CapabilityError("CAPABILITY_REVOKED", "Skill was revoked", resource_id=skill["id"])
        resource = arguments.get("resource_id")
        if resource:
            from src.agent.capabilities.skill_runtime import read_resource
            return read_resource(skill, {"path": resource})
        return {"skill_id": skill["id"], "body": skill["body"], "version": skill.get("version_hash"),
                "resource_ids": list(skill.get("resources", {}))}
    if name == "mcp_call":
        from src.agent.capabilities.mcp_runtime import MCPRuntime
        from src.agent.mcp_manager import MCPManager
        with MCPRuntime(manager_factory=lambda connection: MCPManager(manager.root, connection=connection,
                credentials=manager.mcp.credentials, namespace=manager.store.namespace)) as runtime:
            return {"output": runtime.call(dispatcher.snapshot["tools"][arguments["tool_id"]], arguments["arguments"])}
    if name == "knowledge_search":
        return manager.knowledge.search({**arguments, "namespace": context["namespace"]})
    if name == "sync_account":
        return _sync_account(arguments, context, manager, dispatcher)
    if name == "news_search":
        return _news_search(arguments, context, env)
    if name == 'list_materials':
        return _list_materials(arguments, context)
    if name == "generate_news":
        return _retain(_generate_news(arguments, context, env), context)
    if name == "generate_digest":
        return _retain(_generate_digest(arguments, context, env, dispatcher), context)
    if name == "list_drafts":
        rows = []
        for identity in context.get("owned_post_ids", []):
            try:
                post = _owned_post({"post_id": identity}, context)
            except FileNotFoundError:
                continue
            from src.publish.draft_delivery import has_current_draft_receipt
            rows.append({"post_id": post.id, "title": post.title, "status": str(post.status),
                "image_count": len(post.assets), "current_draft_receipt": has_current_draft_receipt(post, platform="xhs")})
        return {"drafts": rows, "count": len(rows)}
    post = _owned_post(arguments, context)
    if name == "inspect_draft":
        return {"post_id": post.id, "post": post.model_dump(mode="json")}
    if name == "review_draft":
        return _review(post, context)
    if name == "create_image":
        return _create_image(post)
    return _save_draft(post, context, dispatcher)


def execute(name, args, env, context) -> dict:
    env = {**os.environ, **env}
    directory = _private_directory(context["directory"])
    directory.mkdir(parents=True, exist_ok=True)
    output = _ProgressOutput(env)
    try:
        if name not in TOOL_CATALOG:
            raise CapabilityError("CAPABILITY_UNKNOWN", "Tool is not registered", resource_id=name)
        try:
            valid_json = isinstance(args, dict) and len(json.dumps(args, allow_nan=False)) <= 24000
        except (TypeError, ValueError):
            valid_json = False
        if not valid_json:
            raise CapabilityError("TOOL_ARGUMENTS_INVALID", "Arguments must be a bounded JSON object")
        validator = Draft202012Validator(TOOL_CATALOG[name]["parameters"])
        if not validator.is_valid(args):
            raise CapabilityError("TOOL_ARGUMENTS_INVALID", "Arguments do not match this tool's strict schema")
        _progress(env, type="tool", name=name, status="started", action_id=context["action_id"])
        with _environment(env) as root, redirect_stdout(output):
            store = CapabilityStore(namespace=context["namespace"])
            _check_execution(context, store)
            manager = _manager(root, store)
            rows = {row["id"]: row for row in manager.all_tools()}
            for row in rows.values():
                if row.get("revoked_at") or row.get("connection", {}).get("revoked_at"):
                    row["enabled"] = False
            identity = resource_id(name, args)
            dispatcher = _ActionDispatcher(store, {"run_id": context["action_id"], "tools": rows}, context, manager, name)
            if name in {"generate_news", "generate_digest", "create_image"}:
                from src.model_platforms.integration import freeze_run_environment
                os.environ.update(freeze_run_environment(dict(os.environ), roles=("writer",)))
                os.environ.update(LLM_RATE_LIMIT_MAX_RETRIES="0", LLM_TRANSIENT_RETRY_MAX="0",
                                  DAILY_NEWS_CANDIDATE_RETRY_LIMIT="0", ALLOW_PAID_LLM_FALLBACK="0",
                                  OPENCODEX_IMAGE_FALLBACK="none")
            callback = lambda: _invoke(name, args, env, context, manager, dispatcher)
            with dispatcher.bound():
                if identity:
                    row = rows.get(identity)
                    if row is None:
                        raise CapabilityError("CAPABILITY_UNKNOWN", "Capability is not registered", resource_id=identity)
                    stage = "preparation" if name == "mcp_call" else row["stages"][0]
                    payload = dispatcher.call(identity, callback, stage=stage, arguments=args)
                else:
                    payload = callback()
        result = {"ok": True, "status": "succeeded", **payload}
    except (Exception, _ActionStopped) as exc:
        domain_error = re.match(r"^([A-Z][A-Z0-9_]{3,}):\s*(.+)", str(exc), re.S)
        code = getattr(exc, "code", None) or (domain_error.group(1) if domain_error else "TOOL_EXECUTION_FAILED")
        from psycopg import Error as PostgresError
        if isinstance(exc, PostgresError):
            code = "POSTGRES_UNAVAILABLE"
        missing = code.startswith(("CAPABILITY_", "SKILL_", "MCP_", "MODEL_", "CREDENTIAL_", "BILLING_", "PROFILE_", "POSTGRES_", "KNOWLEDGE_")) or code in {
            "RATE_LIMITED", "REQUEST_BUDGET_EXHAUSTED", "GLOBAL_MAP_DISABLED", "WM_NOT_READY",
            "WOOL_PERSONA_NOT_CONFIGURED", "WOOL_IMAGE_RESOURCE_MISSING"}
        uncertain = "UNCERTAIN" in code or name == "save_draft" and (isinstance(exc, _ActionStopped) or code == "TOOL_EXECUTION_FAILED")
        error = exc.error if isinstance(exc, _ActionStopped) else exc.public() if isinstance(exc, (CapabilityError, PlatformError)) else {
            "code": code, "message": domain_error.group(2)[:2000] if domain_error else
            "Action failed; inspect the retained action progress. No automatic replay.", "exception_type": type(exc).__name__}
        result = {"ok": False, "status": "uncertain" if uncertain else "missing_capability" if missing else "failed",
                  "error": error, "error_code": code, "retryable": False}
        if missing:
            result["missing_capability"] = error
    output.flush()
    result = _redact({"tool": name, "action_id": context["action_id"], **result}, env)
    atomic_json(directory / "result.json", result)
    _progress(env, type="tool", name=name, status=result["status"], action_id=context["action_id"])
    return result


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m backend.dialogue_tool_worker <request.json>")
    request_path = Path(sys.argv[1]).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if _private_directory(request["context"]["directory"]) != request_path.parent:
        raise SystemExit("Request must reside in the host action directory")
    execute(request["name"], request["arguments"], request.get("env", {}), request["context"])


if __name__ == "__main__":
    main()
