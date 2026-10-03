from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from apps import cli
from src.agent.editorial_agent import AgentJob
from src.storage.models import Post


class Ledger:
    """Mirror PG's all-phases load and detached payloads, without a database."""

    def __init__(self):
        self.rows = {}
        self.phases = {}

    def ensure_schema(self):
        pass

    def save(self, run_id, job_key, post, *, phase):
        key = (run_id, job_key, post.id)
        self.rows[key] = post.model_copy(deep=True)
        self.phases[key] = phase

    def load(self, run_id, job_key):
        return [post.model_copy(deep=True) for key, post in self.rows.items()
                if key[:2] == (run_id, job_key)]


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    ledger = Ledger()
    captured = {}
    monkeypatch.setattr(cli.KnowledgeStore, "from_env", lambda: object())
    monkeypatch.setattr(cli, "AgentArtifactStore", lambda store: ledger)
    monkeypatch.setattr(cli, "prepare_local_knowledge_snapshot", lambda **kw: {"knowledge_status": "ready"})

    def capture(jobs, *, tools, **kwargs):
        captured["tools"] = tools
        return SimpleNamespace(status="completed", completed_jobs=0, requested_jobs=0,
                               uploaded_posts=[], checkpoint_path="unused", errors=[])

    monkeypatch.setattr(cli, "run_editorial_agent", capture)
    result = CliRunner().invoke(cli.app, ["agent", "--no-preflight", "--no-refresh-quotas", "--skill-mode", "off"])
    assert result.exit_code == 0, result.output
    # Typer restores scoped environment after capturing the callbacks.
    monkeypatch.setenv("AI_DIGEST_MIN_OFFICIAL_ITEMS", "1")
    context = {"agent_run_id": "test-run", "agent_job_key": "1:daily_ai_digest"}
    return captured["tools"], ledger, context


def digest(post_id, *, official=True):
    return Post(id=post_id, title=f"Digest {post_id}", body=f"Concrete event {post_id}", platform={
        "ai_digest": {"items": [{"source_name": "Vendor", "source_type": "official",
                                  "title": "OpenAI新增Responses API语音接口",
                                  "summary": "OpenAI为Responses API新增语音输入输出接口，开发者可以在单次请求中处理音频。",
                                  "url": f"https://vendor.example/{post_id}"}],
                      "source_meta": {"selected_official_count": int(official)}}})


def test_failed_digest_is_replaced_and_not_resurrected_from_ledger(adapter, monkeypatch):
    tools, ledger, context = adapter
    old = digest("old", official=False)
    fresh = digest("fresh")
    good_elsewhere = Post(id="news-good", title="Other completed column", body="Untouched")
    ledger.save("test-run", "0:daily_news", good_elsewhere, phase="approved")
    ledger.save("test-run", context["agent_job_key"], old, phase="reviewed")
    calls = []
    monkeypatch.setattr(cli, "create_daily_ai_digest_posts", lambda **kw: calls.append(kw) or [fresh])
    monkeypatch.setattr(cli, "_run_auto_quality_gate", lambda posts, **kw: [])
    posts = [old]
    job = AgentJob("daily_ai_digest", "AI digest")

    result = tools.review(job, posts, context)

    assert len(calls) == 1, "Failed retained digest must trigger fresh generation"
    assert result["approved_post_ids"] == ["fresh"]
    assert "old" in result["rejected_post_ids"]
    assert result["errors"] == []
    assert ledger.load("test-run", "0:daily_news")[0] == good_elsewhere
    # Core retains the union of old and new posts; PG also loads all phases.
    replay = tools.review(job, [old], context)
    assert replay["approved_post_ids"] == ["fresh"]
    assert len(calls) == 1


def test_completed_digest_revalidation_does_not_trust_saved_receipt(adapter):
    tools, _, context = adapter
    bad = digest("saved")
    bad.uploaded = True
    bad.platform["ai_digest"]["items"][0].update({
        "title": "Qwen2.5-VL开放权重模型发布",
        "summary": "Hugging Face在Transformers v5.18.0中发布Qwen2.5-VL开放权重模型。",
        "url": "https://github.com/huggingface/transformers/releases/tag/v5.18.0",
        "event_type": "model_release", "product": "Qwen2.5-VL",
    })
    callback = getattr(tools, "revalidate_completed", None)
    assert callable(callback), "Completed jobs need current semantic revalidation"
    assert callback(AgentJob("daily_ai_digest", "AI"), [bad], context)
    assert callback(AgentJob("daily_ai_digest", "AI"), [digest("valid")], context) == []


def test_replacement_preserves_existing_platform_target_without_false_receipt(adapter, monkeypatch):
    tools, _, context = adapter
    old, fresh = digest("saved", official=False), digest("revision")
    old.uploaded = True
    old.platform["xhs_draft"] = {"title": "Original platform title", "execution_id": "readback-proof"}
    monkeypatch.setattr(cli, "create_daily_ai_digest_posts", lambda **kw: [fresh])
    monkeypatch.setattr(cli, "_run_auto_quality_gate", lambda *a, **kw: [])
    posts = [old]
    result = tools.review(AgentJob("daily_ai_digest", "AI"), posts, context)
    assert result["approved_post_ids"] == ["revision"]
    target = posts[-1].platform.get("agent_draft_replacement", {})
    assert target.get("xhs", {}).get("title") == "Original platform title"
    assert "xhs_draft" not in posts[-1].platform
    assert not posts[-1].uploaded


def test_upload_replacement_updates_existing_draft_without_creating_duplicate(adapter, monkeypatch):
    from src.publish.delivery_state import DeliveryStateStore
    from src.storage.models import Execution

    tools, _, context = adapter
    post = digest("revision")
    post.platform["agent_draft_replacement"] = {"xhs": {"title": "Original title", "post_id": "ancestor"}}
    state = DeliveryStateStore(_memory=True)
    monkeypatch.setattr(cli, "DeliveryStateStore", lambda: state)
    monkeypatch.setattr(cli, "_resolve_asset_paths", lambda *a: [])
    calls = []

    def update(post, **kwargs):
        calls.append(kwargs)
        return Execution(post_id=post.id, attempt=1, result="saved_draft")

    def forbidden(*args, **kwargs):
        raise AssertionError("Replacement must not create another platform draft")

    monkeypatch.setattr(cli, "run_update_draft_sync", update)
    monkeypatch.setattr(cli, "run_save_draft_sync", forbidden)
    ok, detail = tools.upload(AgentJob("daily_ai_digest", "AI"), post, context)
    assert ok, detail
    assert calls[0]["existing_title"] == "Original title"
    assert post.platform["xhs_draft"]["title"] == post.title
    assert tools.upload(AgentJob("daily_ai_digest", "AI"), post, context)[0]
    assert len(calls) == 1


@pytest.mark.parametrize("kind,generator", [
    ("daily_ai_digest", "create_daily_ai_digest_posts"),
    ("daily_global_map", "create_global_map_post_from_service"),
    ("daily_wow", "create_daily_news_posts"),
    ("daily_wool", "create_daily_wool_posts"),
])
def test_non_news_generation_commits_before_review(adapter, monkeypatch, kind, generator):
    tools, ledger, context = adapter
    post = digest("generated")
    monkeypatch.setattr(cli, generator, lambda **kw: post if kind == "daily_global_map" else [post])
    assert tools.generate(AgentJob(kind, kind), context) == [post]
    assert ledger.load("test-run", context["agent_job_key"]) == [post]
    assert list(ledger.phases.values()) == ["generated"]


def test_still_failed_replacement_stays_rejected_and_next_retry_uses_leaf(adapter, monkeypatch):
    tools, ledger, context = adapter
    old = digest("old", official=False)
    candidates = [digest("bad-new", official=False), digest("good-new")]
    generated = []
    checked = []
    original = cli._agent_ai_digest_review_issues

    def generate(**kwargs):
        generated.append(kwargs)
        return [candidates.pop(0)]

    def sources(post, **kwargs):
        checked.append(post.id)
        return original(post, **kwargs)

    monkeypatch.setattr(cli, "create_daily_ai_digest_posts", generate)
    monkeypatch.setattr(cli, "_agent_ai_digest_review_issues", sources)
    monkeypatch.setattr(cli, "_run_auto_quality_gate", lambda *a, **kw: [])
    job = AgentJob("daily_ai_digest", "AI")
    posts = [old]
    first = tools.review(job, posts, context)
    assert first["approved_post_ids"] == []
    assert first["errors"]
    assert len(generated) == 1
    second = tools.review(job, posts, context)
    assert second["approved_post_ids"] == ["good-new"]
    assert checked.count("old") == 1
    assert len(generated) == 2
    assert ledger.load("test-run", context["agent_job_key"])


def test_interruption_after_replacement_commit_does_not_revive_old(adapter, monkeypatch):
    tools, ledger, context = adapter
    old, fresh = digest("old", official=False), digest("fresh")
    calls = []
    monkeypatch.setattr(cli, "create_daily_ai_digest_posts", lambda **kw: calls.append(kw) or [fresh])
    monkeypatch.setattr(cli, "_run_auto_quality_gate", lambda *a, **kw: [])
    original_save = ledger.save

    def interrupt(run_id, job_key, post, *, phase):
        if phase == "superseded":
            raise KeyboardInterrupt("crash before updating old artifact")
        original_save(run_id, job_key, post, phase=phase)

    monkeypatch.setattr(ledger, "save", interrupt)
    job = AgentJob("daily_ai_digest", "AI")
    with pytest.raises(KeyboardInterrupt):
        tools.review(job, [old], context)
    monkeypatch.setattr(ledger, "save", original_save)
    result = tools.review(job, [old], context)
    assert result["approved_post_ids"] == [fresh.id]
    assert len(calls) == 1


@pytest.mark.parametrize("reason", ["stale: older than two days", "historical_duplicate", "vision score 45 below 70"])
def test_replacement_keeps_date_dedupe_and_score_failures(adapter, monkeypatch, reason):
    tools, ledger, context = adapter
    old, fresh = digest("old"), digest("fresh")
    calls = []
    gate_calls = []
    monkeypatch.setattr(cli, "create_daily_ai_digest_posts", lambda **kw: calls.append(kw) or [fresh])

    def gate(posts, **kwargs):
        gate_calls.append(([p.id for p in posts], kwargs))
        return [reason]

    monkeypatch.setattr(cli, "_run_auto_quality_gate", gate)
    result = tools.review(AgentJob("daily_ai_digest", "AI", lookback_days=2), [old], context)
    assert not result["approved_post_ids"]
    assert reason in result["errors"]
    assert len(calls) == 1
    assert calls[0]["lookback_days"] == 2
    assert [ids for ids, kw in gate_calls] == [["old"], ["fresh"]]
    assert all(kw["require_vision"] and kw["reuse_vision_results"] for ids, kw in gate_calls)


def test_real_quality_gate_still_checks_delivered_history(adapter, monkeypatch):
    tools, ledger, context = adapter
    old, fresh = digest("old", official=False), digest("fresh")
    history = fresh.model_copy(deep=True)
    history.id = "previously-uploaded"
    history.uploaded = True
    monkeypatch.setenv("AI_DIGEST_HISTORY_SKIP_TODAY", "0")
    monkeypatch.setattr(cli, "list_posts", lambda: [history, old])
    monkeypatch.setattr(cli, "configured_vision_review_model", lambda: False)
    monkeypatch.setattr(cli, "create_daily_ai_digest_posts", lambda **kw: [fresh])
    result = tools.review(AgentJob("daily_ai_digest", "AI"), [old], context)
    assert not result["approved_post_ids"]
    assert any("previously-uploaded" in str(issue) for issue in result["errors"])


def test_terminal_provider_failure_does_not_regenerate(adapter, monkeypatch):
    tools, ledger, context = adapter
    monkeypatch.setattr(cli, "_run_auto_quality_gate", lambda *a, **kw: ["Token Plan 用量上限"])
    def forbidden(**kwargs):
        raise AssertionError("must not consume more model calls")
    monkeypatch.setattr(cli, "create_daily_ai_digest_posts", forbidden)
    result = tools.review(AgentJob("daily_ai_digest", "AI"), [digest("old")], context)
    assert result["retryable"] is False
    assert result["approved_post_ids"] == []


def test_map_failed_quality_is_replaced_without_touching_passing_item(adapter, monkeypatch, tmp_path):
    import json

    from src.global_map.review import stored_global_map_review_issues

    tools, ledger, context = adapter
    good, bad, fresh = (Post(id=value, title=value, body=value) for value in ("good", "bad", "fresh"))
    catalog = tmp_path / "countries.geojson"
    catalog.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"properties": {"name": name}} for name in ("China", "Germany")
    ]}), encoding="utf-8")
    monkeypatch.setenv("GLOBAL_MAP_BASEMAP_PATH", str(catalog))
    for post in (good, bad, fresh):
        events = []
        for index, (title, country, latitude, longitude) in enumerate([
            ("China factory activity expands", "China", 35.8617, 104.1954),
            ("Germany opens a new factory", "Germany", 51.1657, 10.4515),
            ("China completes reconstruction", "China", 35.8617, 104.1954),
        ], 1):
            events.append({
                "event_key": f"{post.id}-event-{index}", "title": title, "summary": "",
                "country": country, "location_name": country,
                "latitude": latitude, "longitude": longitude,
                "location_precision": "country", "location_method": "explicit_country_name",
                "publisher_count": 1, "score": 0.8, "recency": 1.0,
                "evidence": [{
                    "title": title, "summary": "", "source": "Test Publisher",
                    "url": f"https://example.org/{post.id}/event-{index}",
                    "published_at": "2026-10-02T09:00:00+00:00", "authority": 0.8,
                }],
            })
        post.platform["global_map"] = {
            "target_date": "2026-10-02", "cutoff": "2026-10-02T18:00:00+08:00",
            "source_state": "partial", "coverage_status": "limited", "upload_allowed": True,
            "located_event_count": 3, "country_count": 2, "events": events,
            "raw_item_count": 3, "independent_event_count": 3, "publisher_count": 1,
            "warning": "", "map_mode": "coordinate-grid",
        }
        # The quality failure below must not be a missing-snapshot failure.
        assert stored_global_map_review_issues(post.platform["global_map"]) == []
    calls = []
    monkeypatch.setattr(cli, "create_global_map_post_from_service", lambda **kw: calls.append(kw) or fresh)
    monkeypatch.setattr(cli, "validate_post_batch", lambda *a, **kw: SimpleNamespace(issues=[]))
    monkeypatch.setattr(cli, "_run_auto_quality_gate", lambda posts, **kw: ["no map"] if posts[0].id == "bad" else [])
    original = good.model_copy(deep=True)
    posts = [good, bad]
    result = tools.review(AgentJob("daily_global_map", "Map", count=2), posts, context)
    assert result["approved_post_ids"] == ["good", "fresh"]
    assert good == original
    assert len(calls) == 1


def test_generator_cannot_relabel_old_failed_input_as_new(adapter, monkeypatch):
    tools, ledger, context = adapter
    old = digest("old", official=False)
    monkeypatch.setattr(cli, "create_daily_ai_digest_posts", lambda **kw: [old])
    result = tools.review(AgentJob("daily_ai_digest", "AI"), [old], context)
    assert not result["approved_post_ids"]
    assert any("NON_NEWS_REPLACEMENT_INVALID" in error for error in result["errors"])


def test_actual_core_uploads_only_approved_replacement_and_keeps_other_job(adapter, monkeypatch, tmp_path):
    from src.agent import editorial_agent as agent

    tools, ledger, context = adapter
    old, fresh, wool = digest("old", official=False), digest("fresh"), Post(id="wool", title="Wool", body="Offer")
    candidates = [old, fresh]
    monkeypatch.setattr(cli, "create_daily_ai_digest_posts", lambda **kw: [candidates.pop(0)])
    monkeypatch.setattr(cli, "create_daily_wool_posts", lambda **kw: [wool])
    monkeypatch.setattr(cli, "_run_auto_quality_gate", lambda *a, **kw: [])
    uploaded = []
    result = agent.run_editorial_agent(
        [AgentJob("daily_wool", "Wool"), AgentJob("daily_ai_digest", "AI")],
        tools=agent.EditorialAgentTools(
            sync_context=lambda job: {}, generate=tools.generate, review=tools.review,
            upload=lambda job, post, ctx: (uploaded.append(post.id) or True, "fake saved"),
        ),
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path / "checkpoints"),
        run_id="offline-core-integration",
    )
    assert result.status == "completed"
    assert result.completed_jobs == 2
    assert uploaded == ["wool", "fresh"]
    assert not candidates
    checkpoint = agent.load_agent_checkpoint(result.checkpoint_path)
    assert checkpoint["job_states"]["1"]["post_ids"] == ["old", "fresh"]
    assert checkpoint["job_states"]["1"]["reviewed_post_ids"] == ["fresh"]
