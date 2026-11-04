import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from threading import Lock
import time

import httpx
from PIL import Image, ImageDraw
import pytest

from src.images import ccswitch_images as relay


def image_bytes(color="teal"):
    image = Image.new("RGB", (128, 192), "white")
    ImageDraw.Draw(image).rectangle((10, 10, 90, 120), fill=color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def configured(monkeypatch, tmp_path):
    home, codex = tmp_path / "ccswitch", tmp_path / "codex"
    home.mkdir()
    codex.mkdir()
    database = home / "cc-switch.db"
    settings = {"config": 'model = "gpt-example"\n',
                "auth": {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                         "tokens": {"access_token": "stale-token", "account_id": "stale-account"}}}
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE providers (id TEXT,app_type TEXT,category TEXT,settings_config TEXT,is_current INT)")
        db.execute("INSERT INTO providers VALUES (?,?,?,?,1)",
                   ("official", "codex", "official", json.dumps(settings)))
        db.execute("CREATE TABLE proxy_config (app_type TEXT,enabled INT,auto_failover_enabled INT,listen_address TEXT,listen_port INT)")
        db.execute("INSERT INTO proxy_config VALUES ('codex',1,0,'127.0.0.1',15721)")
    auth = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": {"access_token": "live-oauth-token", "account_id": "live-account"}}
    (codex / "auth.json").write_text(json.dumps(auth))
    monkeypatch.setenv("CCSWITCH_HOME", str(home))
    monkeypatch.setenv("CCSWITCH_CODEX_HOME", str(codex))
    monkeypatch.setenv("CCSWITCH_IMAGE_BASE_URL", "http://127.0.0.1:15721")
    monkeypatch.setenv("CCSWITCH_IMAGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CCSWITCH_IMAGE_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("CCSWITCH_IMAGE_CONCURRENCY", "1")
    return database, codex, settings, auth


@pytest.fixture
def mock_network(monkeypatch, configured):
    original = httpx.Client

    def install(callback):
        def handle(request):
            assert request.url.host == "127.0.0.1"
            assert request.url.port == 15721
            if request.method == "GET":
                assert request.url.path == "/health"
                return httpx.Response(200, json={"status": "healthy"})
            return callback(request)
        monkeypatch.setattr(relay.httpx, "Client",
                            lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    return install


def response():
    return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image_bytes()).decode()}]})


def update_provider(database, settings, **columns):
    with sqlite3.connect(database) as db:
        db.execute("UPDATE providers SET settings_config=?", (json.dumps(settings),))
        for key, value in columns.items():
            assert key in {"category", "is_current"}
            db.execute(f"UPDATE providers SET {key}=?", (value,))


def test_subscription_state_is_read_only_and_uses_live_not_stored_oauth(configured):
    database, codex, _, _ = configured
    before = {p: p.read_bytes() for p in (database, codex / "auth.json")}
    route = relay._subscription_state()
    assert route.headers["Authorization"] == "Bearer live-oauth-token"
    assert route.headers["ChatGPT-Account-Id"] == "live-account"
    assert route.proof["paid_fallback"] is False
    assert "live-oauth-token" not in repr(route)
    assert "live-oauth-token" not in json.dumps(route.proof)
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize("change", [
    {"auth": {"auth_mode": "apikey", "OPENAI_API_KEY": "dummy"}},
    {"config": 'model_provider="custom"'},
    {"config": '[model_providers.custom]\nbase_url="https://example.com/v1"'},
    {"config": 'experimental_bearer_token="dummy"'},
    {"base_url": "https://api.openai.com/v1"},
    {"baseURL": "https://api.example.com/v1"},
])
def test_non_subscription_routes_are_blocked(configured, change):
    database, _, original, _ = configured
    settings = deepcopy(original)
    settings.update(change)
    update_provider(database, settings)
    with pytest.raises(relay.CCSwitchImageError, match="SUBSCRIPTION_ROUTE_REQUIRED"):
        relay._subscription_state()


@pytest.mark.parametrize("statement", [
    "UPDATE proxy_config SET auto_failover_enabled=1",
    "UPDATE proxy_config SET enabled=0",
    "UPDATE proxy_config SET listen_address='0.0.0.0'",
    "UPDATE proxy_config SET listen_port=12345",
    "UPDATE providers SET is_current=0",
])
def test_unverified_proxy_state_is_blocked(configured, statement):
    with sqlite3.connect(configured[0]) as db:
        db.execute(statement)
    with pytest.raises(relay.CCSwitchImageError, match="ROUTE_UNVERIFIED"):
        relay._subscription_state()


def test_nonofficial_card_is_blocked(configured):
    update_provider(configured[0], configured[2], category="custom")
    with pytest.raises(relay.CCSwitchImageError, match="SUBSCRIPTION_ROUTE_REQUIRED"):
        relay._subscription_state()


@pytest.mark.parametrize("auth", [{}, {"auth_mode": "apikey", "OPENAI_API_KEY": "dummy"},
                                  {"auth_mode": "chatgpt", "tokens": {"access_token": "PROXY_MANAGED", "account_id": "a"}}])
def test_live_login_is_required_without_refresh(configured, auth):
    (configured[1] / "auth.json").write_text(json.dumps(auth))
    with pytest.raises(relay.CCSwitchImageError, match="EXISTING_LOGIN_REQUIRED"):
        relay._subscription_state()


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "http://localhost:15721",
                                  "http://127.0.0.1:15721/v1", "http://127.0.0.1:15721?q=x",
                                  "http://user:pass@127.0.0.1:15721"])
def test_exact_loopback_origin_only(monkeypatch, url):
    monkeypatch.setenv("CCSWITCH_IMAGE_BASE_URL", url)
    with pytest.raises(relay.CCSwitchImageError, match="LOOPBACK_URL_REQUIRED"):
        relay.connection_settings()


def test_generate_and_cache_across_posts_without_opencodex(mock_network, configured, tmp_path, monkeypatch):
    from src.images import opencodex_images
    monkeypatch.setattr(opencodex_images, "preflight", lambda *_: pytest.fail("OpenCodex must not be contacted"))
    calls = []
    mock_network(lambda request: (calls.append(request) or response()))
    first = relay.generate_subscription_image(post_id="first", prompt="teapot", dest_dir=tmp_path / "one")
    second = relay.generate_subscription_image(post_id="second", prompt="teapot", dest_dir=tmp_path / "two")
    assert len(calls) == 1
    assert first.path.read_bytes() == second.path.read_bytes()
    assert second.meta["cache_hit"]
    assert second.meta["cached_from_post_id"] == "first"
    assert first.meta["provider"] == "ccswitch"
    assert first.meta["width"] == 128
    assert calls[0].headers["Authorization"] == "Bearer live-oauth-token"
    assert json.loads(calls[0].content)["model"] == relay.MODEL
    assert "live-oauth-token" not in "".join(p.read_text() for p in (tmp_path / "state").glob("*.json"))


def test_two_references_use_edit_route(mock_network, tmp_path):
    refs = []
    for name, color in (("composition", "coral"), ("persona", "blue")):
        path = tmp_path / (name + ".png")
        path.write_bytes(image_bytes(color))
        refs.append(path)
    calls = []
    mock_network(lambda request: (calls.append(request) or response()))
    relay.generate_subscription_image(post_id="edit", prompt="edit pose and identity",
                                      dest_dir=tmp_path / "assets", reference_paths=refs)
    assert calls[0].url.path == "/v1/images/edits"
    assert len(json.loads(calls[0].content)["images"]) == 2


def test_ambiguous_failure_never_resubmits_or_falls_back(mock_network, monkeypatch, tmp_path):
    from src.images import minimax_images
    monkeypatch.setattr(minimax_images, "generate_minimax_image", lambda **_: pytest.fail("no fallback"))
    monkeypatch.setenv("OPENCODEX_IMAGE_FALLBACK", "minimax")
    calls = []
    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("lost result", request=request)
    mock_network(fail)
    for post in ("first", "new-post"):
        with pytest.raises(relay.CCSwitchImageError, match="UNCERTAIN"):
            relay.generate_subscription_image(post_id=post, prompt="same scene",
                                              dest_dir=tmp_path / post, allow_minimax_fallback=True)
    assert len(calls) == 1


@pytest.mark.parametrize("status,detail,code", [
    (502, "无法解密指定的数据。 (os error -2146893008)", "TLS_FORWARD_FAILED"),
    (401, "session mismatch", "AUTH_FAILED"),
    (429, "quota exceeded", "QUOTA_OR_RATE_LIMIT"),
    (403, "not permitted", "HTTP_403"),
])
def test_actionable_errors_are_sanitized_and_preserved(mock_network, tmp_path, status, detail, code):
    calls = []
    def reject(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": detail + " live-oauth-token live-account"}})
    mock_network(reject)
    with pytest.raises(relay.CCSwitchImageError, match=code) as error:
        relay.generate_subscription_image(post_id="test", prompt="scene", dest_dir=tmp_path / "assets")
    assert "live-oauth-token" not in str(error.value)
    assert "live-account" not in str(error.value)
    saved = json.loads(next((tmp_path / "state").glob("*.json")).read_text())
    assert saved["status"] == ("uncertain" if status == 502 else "rejected")
    assert saved["error"] == "CCSWITCH_" + code
    with pytest.raises(relay.CCSwitchImageError):
        relay.generate_subscription_image(post_id="test", prompt="scene", dest_dir=tmp_path / "assets")
    assert len(calls) == 1


def test_route_change_before_submission_does_not_post(mock_network, monkeypatch, tmp_path):
    route = relay._subscription_state()
    monkeypatch.setattr(relay, "preflight", lambda _: route)
    changed = relay.SubscriptionRoute(route.base_url, route.headers, {**route.proof, "account_hash": "changed"})
    monkeypatch.setattr(relay, "_subscription_state", lambda: changed)
    mock_network(lambda _: pytest.fail("must not submit"))
    with pytest.raises(relay.CCSwitchImageError, match="ROUTE_CHANGED"):
        relay.generate_subscription_image(post_id="test", prompt="scene", dest_dir=tmp_path / "assets")


def test_corrupt_cache_is_not_silently_regenerated(mock_network, tmp_path):
    calls = []
    mock_network(lambda request: (calls.append(request) or response()))
    args = dict(post_id="test", prompt="scene", dest_dir=tmp_path / "assets")
    relay.generate_subscription_image(**args).path.write_bytes(b"broken")
    with pytest.raises(relay.CCSwitchImageError, match="CACHE_ARTIFACT_INVALID"):
        relay.generate_subscription_image(**args)
    assert len(calls) == 1


def test_default_serial_image_slot_does_not_change_chat_concurrency(mock_network, tmp_path):
    active = peak = 0
    lock = Lock()
    def handle(_request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return response()
    mock_network(handle)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda i: relay.generate_subscription_image(
            post_id=str(i), prompt=f"scene {i}", dest_dir=tmp_path / str(i)), range(3)))
    assert len(results) == 3
    assert peak == 1


def test_gui_binding_and_catalog_preserve_ccswitch(monkeypatch, tmp_path):
    from apps.gui import build_provider_env_overrides
    from apps.web_service import Workbench
    env = build_provider_env_overrides({}, image_provider="ccswitch", image_model=relay.MODEL,
                                       llm_provider="minimax", llm_model="MiniMax-M3")
    assert env["IMAGE_PROVIDER"] == "ccswitch"
    assert env["CCSWITCH_IMAGE_ENABLED"] == "1"
    (tmp_path / ".env.gui").write_text("CCSWITCH_IMAGE_ENABLED=1\n")
    workbench = Workbench(root=tmp_path)
    item = next(row for row in workbench.models()["rows"] if row["id"] == "ccswitch:gpt-image-2")
    assert item["selectable"] and item["kind"] == "image"
    assert item["cost_class"] == "subscription_included"


def test_generic_image_entry_uses_only_selected_ccswitch(monkeypatch, tmp_path):
    from src.images import auto_image, opencodex_images
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        return relay.SubscriptionImageResult(tmp_path / "result.png", {"provider": "ccswitch"})
    monkeypatch.setattr(relay, "generate_subscription_image", generate)
    monkeypatch.setattr(opencodex_images, "generate_subscription_image", lambda **_: pytest.fail("wrong provider"))
    paths, meta = auto_image.fetch_and_download_related_images(
        title="Test", body="event", topics=[], prompt_hint="event", dest_dir=tmp_path / "assets", provider="ccswitch", count=1)
    assert len(calls) == 1 and len(paths) == 1
    assert meta[0]["provider"] == "ccswitch"


def test_wool_neutral_cover_uses_ccswitch_without_old_relay(monkeypatch, tmp_path):
    from src.wool import image_edit
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        return relay.SubscriptionImageResult(tmp_path / "result.png", {"provider": "ccswitch"})
    monkeypatch.setenv("REDBOOK_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("IMAGE_PROVIDER", "ccswitch")
    monkeypatch.setattr(relay, "generate_subscription_image", generate)
    monkeypatch.setattr(image_edit, "generate_subscription_image", lambda **_: pytest.fail("old relay"))
    _path, meta = image_edit.create_wool_image(post_id="test", offers=[], issue_date="2026-10-24")
    assert len(calls) == 1 and calls[0]["allow_minimax_fallback"] is False
    assert meta["provider"] == "ccswitch"


def test_ccswitch_is_accepted_by_news_ai_image_policy():
    from src.workflow.create_post import _AI_IMAGE_PROVIDER_ALIASES
    assert "ccswitch" in _AI_IMAGE_PROVIDER_ALIASES


@pytest.mark.parametrize("provider,blocked", [("ccswitch", False), ("pexels", True)])
def test_news_review_still_blocks_non_ai_but_accepts_ccswitch(provider, blocked):
    from src.storage.models import Post
    from src.workflow.quality_gate import _post_issues
    post = Post(title="企业发布芯片", body="内容：一家企业发布新芯片。\n日期：2026-10-24",
                platform={"news": {"image_policy": "ai_required", "source_url": "https://example.com/news",
                                   "picked": {"seendate": "2026-10-24"}},
                          "images": [{"provider": provider}]})
    assert any(issue.code == "ai_image_required" for issue in _post_issues(post)) is blocked


def test_cli_preflight_keeps_explicit_ccswitch_binding(monkeypatch, tmp_path):
    from apps import cli
    metrics = tmp_path / "metrics.csv"
    metrics.write_text("title,likes\nnews,1\n")
    for key, value in {"LLM_PROVIDER": "minimax", "IMAGE_PROVIDER": "ccswitch",
                       "MINIMAX_USE_SUBSCRIPTION": "1", "MINIMAX_LLM_MODEL": "MiniMax-M3"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli, "load_quota_records", lambda **kwargs: ([], []))
    monkeypatch.setattr(cli, "_path_is_fresh", lambda *args, **kwargs: True)
    monkeypatch.setattr(cli, "_refresh_metrics_for_preflight", lambda **kwargs: pytest.fail("no browser"))
    monkeypatch.setattr(cli, "_refresh_quotas_for_preflight", lambda **kwargs: pytest.fail("no quota sync"))
    report = cli._prepare_auto_pipeline(
        headless=True, login_hold=0, wait_timeout=10, metrics_max_age_hours=24,
        quota_max_age_hours=2, require_image=True, refresh_quotas=False,
        metrics_path=metrics, quota_dir=tmp_path / "quota", provider_keys={"minimax": True})
    assert "IMAGE_PROVIDER" not in report.model_plan.environment()
    assert report.model_plan.llm.provider == "minimax"


def test_agent_adapter_keeps_capability_governance(monkeypatch, tmp_path):
    dispatcher = pytest.importorskip("src.agent.capabilities.dispatcher")
    if not hasattr(relay.generate_subscription_image, "__wrapped__"):
        pytest.skip("Workflow does not use the standalone agent capability dispatcher")
    def deny(*args, **kwargs):
        raise RuntimeError("CAPABILITY_DENIED")
    monkeypatch.setattr(dispatcher, "managed_call", deny)
    with pytest.raises(RuntimeError, match="CAPABILITY_DENIED"):
        relay.generate_subscription_image(post_id="test", prompt="scene", dest_dir=tmp_path)
