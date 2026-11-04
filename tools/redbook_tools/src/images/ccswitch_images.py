"""Read-only CC Switch subscription image client; never starts or changes a relay."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Iterator
from urllib.parse import urlsplit

import httpx
from PIL import Image
from src.workflow.resources import memory_bound

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from .opencodex_images import (
    OpenCodexImageError, SubscriptionImageResult, _file_lock, _write_json, validate_image,
)
from src.agent.capabilities.dispatcher import governed

MODEL = "gpt-image-2"
MAX_RESPONSE_BYTES = 100 * 1024 * 1024


class CCSwitchImageError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.fallback_safe = False
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class SubscriptionRoute:
    base_url: str
    headers: dict[str, str] = field(repr=False)
    proof: dict


def connection_settings() -> tuple[str, Path, Path]:
    base = (os.getenv("CCSWITCH_IMAGE_BASE_URL") or "http://127.0.0.1:15721").rstrip("/")
    address = urlsplit(base)
    if (address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port
            or address.path or address.query or address.fragment or address.username or address.password):
        raise CCSwitchImageError("CCSWITCH_LOOPBACK_URL_REQUIRED")
    home = Path(os.getenv("CCSWITCH_HOME") or Path.home() / ".cc-switch")
    codex_home = Path(os.getenv("CCSWITCH_CODEX_HOME") or Path.home() / ".codex")
    return base, home, codex_home


def image_connection_status(env: dict | None = None) -> dict:
    values = os.environ if env is None else env
    enabled = str(values.get("CCSWITCH_IMAGE_ENABLED", "0")).lower() in {"1", "true", "yes", "on"}
    return {"enabled": enabled, "model": MODEL, "billing": "chatgpt_subscription",
            "remaining": None, "requires_preflight": True}


def _subscription_state() -> SubscriptionRoute:
    base, home, codex_home = connection_settings()
    try:
        with sqlite3.connect((home / "cc-switch.db").resolve().as_uri() + "?mode=ro",
                             uri=True, timeout=2) as database:
            database.execute("PRAGMA query_only=ON")
            providers = database.execute(
                "SELECT id,category,settings_config FROM providers WHERE app_type='codex' AND is_current=1"
            ).fetchall()
            proxy = database.execute(
                "SELECT enabled,auto_failover_enabled,listen_address,listen_port "
                "FROM proxy_config WHERE app_type='codex'"
            ).fetchone()
        if (len(providers) != 1 or not proxy or proxy[0] != 1 or proxy[1] != 0
                or proxy[2] != "127.0.0.1" or proxy[3] != urlsplit(base).port):
            raise CCSwitchImageError("CCSWITCH_SUBSCRIPTION_ROUTE_UNVERIFIED",
                                     "需要已启用的本机 Codex 路由，且自动故障转移关闭；程序不会代为修改")
        provider_id, category, raw = providers[0]
        settings = json.loads(raw)
        config = tomllib.loads(settings.get("config") or "")
        saved_auth = settings.get("auth") or {}
        if (category != "official" or saved_auth.get("OPENAI_API_KEY")
                or saved_auth.get("auth_mode") != "chatgpt"
                or config.get("model_provider") not in {None, "openai"}
                or config.get("model_providers") or config.get("experimental_bearer_token")
                or any(settings.get(k) for k in ("base_url", "baseURL", "api_key", "apiKey"))):
            raise CCSwitchImageError("CCSWITCH_SUBSCRIPTION_ROUTE_REQUIRED",
                                     "仅接受官方 ChatGPT 订阅路由，禁止 API Key 或自定义上游")
        # A saved provider card may contain old OAuth. Reuse the live session, never refresh or switch it.
        auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8-sig"))
        tokens = auth.get("tokens") or {}
        access, account = tokens.get("access_token"), tokens.get("account_id")
        if (auth.get("auth_mode") != "chatgpt" or auth.get("OPENAI_API_KEY")
                or not isinstance(access, str) or not access or access.startswith("sk-")
                or access == "PROXY_MANAGED" or not isinstance(account, str) or not account):
            raise CCSwitchImageError("CCSWITCH_EXISTING_LOGIN_REQUIRED",
                                     "需要当前 Codex 的 ChatGPT 登录态；不会自动登录、刷新或切换账号")
        proof = {"provider": "ccswitch", "model": MODEL, "billing": "chatgpt_subscription",
                 "provider_config_hash": sha256(raw.encode()).hexdigest(),
                 "provider_id_hash": sha256(provider_id.encode()).hexdigest(),
                 "account_hash": sha256(account.encode()).hexdigest(),
                 "base_url": base, "auto_failover": False, "paid_fallback": False}
        return SubscriptionRoute(base, {"Authorization": "Bearer " + access,
                                        "ChatGPT-Account-Id": account}, proof)
    except CCSwitchImageError:
        raise
    except (OSError, ValueError, TypeError, sqlite3.Error):
        raise CCSwitchImageError("CCSWITCH_CONFIGURATION_UNAVAILABLE",
                                 "无法只读核验 CC Switch 配置或当前登录态") from None


def preflight(client: httpx.Client) -> SubscriptionRoute:
    route = _subscription_state()
    try:
        response = client.get(route.base_url + "/health", timeout=5)
        response.raise_for_status()
        if response.json().get("status") != "healthy":
            raise ValueError("unhealthy")
    except (httpx.HTTPError, ValueError, AttributeError):
        raise CCSwitchImageError("CCSWITCH_SERVICE_UNAVAILABLE",
                                 "CC Switch 路由未就绪；程序不会启动或重启服务") from None
    return route


def _safe_error(payload: bytes, headers: dict[str, str]) -> str:
    try:
        value = json.loads(payload)
        error = value.get("error", {})
        message = error.get("message", "") if isinstance(error, dict) else str(error)
    except (ValueError, AttributeError):
        message = "上游未返回可识别的 JSON 错误"
    message = str(message)
    for secret in (headers.get("Authorization", "")[7:], headers.get("ChatGPT-Account-Id", "")):
        if secret:
            message = message.replace(secret, "[redacted]")
    message = re.sub(r"\b(?:eyJ[A-Za-z0-9_.-]+|sk-[A-Za-z0-9_-]+)", "[redacted]", message)
    return message[:1200]


def _http_error(status: int, payload: bytes, headers: dict[str, str]) -> CCSwitchImageError:
    detail = _safe_error(payload, headers)
    if "-2146893008" in detail or "无法解密指定的数据" in detail:
        return CCSwitchImageError("CCSWITCH_TLS_FORWARD_FAILED", detail)
    if status == 401:
        return CCSwitchImageError("CCSWITCH_AUTH_FAILED", detail)
    if status == 429:
        return CCSwitchImageError("CCSWITCH_QUOTA_OR_RATE_LIMIT", detail)
    return CCSwitchImageError(f"CCSWITCH_HTTP_{status}", detail)


def _validate(content: bytes, inputs: list[str]) -> tuple[int, int]:
    try:
        return validate_image(content, inputs)
    except OpenCodexImageError as exc:
        raise CCSwitchImageError(exc.code.replace("OPENCODEX_", "CCSWITCH_")) from None


def _runtime_path(name: str, default: str) -> Path:
    return Path(os.getenv(name) or Path(os.getenv("REDBOOK_RUNTIME_ROOT") or ".") / default)


@contextmanager
def _image_slot() -> Iterator[None]:
    root = _runtime_path("CCSWITCH_IMAGE_LOCK_DIR", "data/runs/ccswitch-image-locks")
    count = max(1, min(2, int(os.getenv("CCSWITCH_IMAGE_CONCURRENCY") or "1")))
    started = time.monotonic()
    while True:
        for index in range(count):
            lock = _file_lock(root / f"slot-{index}.lock", wait=False)
            try:
                lock.__enter__()
            except OpenCodexImageError:
                continue
            try:
                yield
            finally:
                lock.__exit__(None, None, None)
            return
        if time.monotonic() - started > 620:
            raise CCSwitchImageError("CCSWITCH_IMAGE_QUEUE_TIMEOUT")
        time.sleep(0.05)


@memory_bound("image", limit=2)
@governed('builtin:image.generate', 'generate')
def generate_subscription_image(*, post_id: str, prompt: str, dest_dir: Path,
                                reference_paths: list[Path] | None = None,
                                allow_minimax_fallback: bool = False) -> SubscriptionImageResult:
    # This compatibility argument never authorizes another provider or billing route.
    if not prompt.strip() or len(prompt) > 16000:
        raise CCSwitchImageError("CCSWITCH_INVALID_PROMPT")
    references = reference_paths or []
    if references and len(references) != 2:
        raise CCSwitchImageError("CCSWITCH_TWO_REFERENCES_REQUIRED")
    hashes = []
    for path in references:
        if path.stat().st_size > 10 * 1024 * 1024:
            raise CCSwitchImageError("CCSWITCH_REFERENCE_TOO_LARGE")
        try:
            with Image.open(path) as image:
                mime = Image.MIME.get(image.format)
                image.verify()
        except (OSError, ValueError):
            raise CCSwitchImageError("CCSWITCH_REFERENCE_FORMAT_INVALID") from None
        if mime not in {"image/jpeg", "image/png", "image/webp"}:
            raise CCSwitchImageError("CCSWITCH_REFERENCE_FORMAT_INVALID")
        digest = sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        hashes.append(digest.hexdigest())
    base, _home, _codex_home = connection_settings()
    body = {"model": MODEL, "prompt": prompt.strip(), "n": 1,
            "quality": "medium", "size": "1024x1536", "background": "opaque"}
    endpoint = "edits" if references else "generations"
    key = sha256(json.dumps({"provider": "ccswitch", "base_url": base, "model": MODEL,
                            "prompt": prompt.strip(), "inputs": hashes,
                            "quality": body["quality"], "size": body["size"]}, sort_keys=True).encode()).hexdigest()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    state_dir = _runtime_path("CCSWITCH_IMAGE_STATE_DIR", "data/runs/ccswitch-image-state")
    shared = state_dir / f"{key}.json"
    record = dest_dir / f"ccswitch-{key}.json"
    file = dest_dir / f"ccswitch-{key}.png"
    with _file_lock(state_dir / f"{key}.lock"):
        for checkpoint in (record, shared):
            if not checkpoint.exists():
                continue
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            if saved.get("status") == "complete":
                source = Path(saved.get("artifact_path") or dest_dir / saved["file"])
                content = source.read_bytes() if source.is_file() else b""
                if sha256(content).hexdigest() != saved.get("sha256"):
                    raise CCSwitchImageError("CCSWITCH_CACHE_ARTIFACT_INVALID")
                _validate(content, hashes)
                if source.resolve() != file.resolve():
                    temporary = file.with_suffix(".png.tmp")
                    temporary.write_bytes(content)
                    os.replace(temporary, file)
                meta = {**saved, "cache_hit": True, "cached_from_post_id": saved.get("post_id"),
                        "post_id": post_id, "artifact_path": str(file.resolve())}
                _write_json(record, meta)
                return SubscriptionImageResult(file, meta)
            if saved.get("status") in {"submitted", "uncertain"}:
                raise CCSwitchImageError("CCSWITCH_PREVIOUS_REQUEST_UNCERTAIN",
                                         "已保留请求状态，不自动重发，避免重复生成")
            if saved.get("status") == "rejected":
                raise CCSwitchImageError(saved.get("error") or "CCSWITCH_PREVIOUS_REQUEST_REJECTED")
        started = time.monotonic()
        meta = {"provider": "ccswitch", "model": MODEL, "mode": "ai_generated",
                "billing": "chatgpt_subscription", "post_id": post_id, "endpoint": endpoint,
                "request_key": key, "input_hashes": hashes, "status": "not_submitted",
                "cache_hit": False, "paid_fallback": False}

        def save():
            _write_json(shared, meta)
            _write_json(record, meta)

        try:
            with _image_slot(), httpx.Client(trust_env=False, follow_redirects=False,
                                            timeout=httpx.Timeout(310, connect=5)) as client:
                route = preflight(client)
                if references:
                    encoded = []
                    for path, expected_hash in zip(references, hashes):
                        content = path.read_bytes()
                        if sha256(content).hexdigest() != expected_hash:
                            raise CCSwitchImageError("CCSWITCH_REFERENCE_CHANGED")
                        with Image.open(BytesIO(content)) as image:
                            mime = Image.MIME[image.format]
                        encoded.append({"image_url": f"data:{mime};base64," + base64.b64encode(content).decode("ascii")})
                    body["images"] = encoded
                    del content
                if route.base_url != base or _subscription_state().proof != route.proof:
                    raise CCSwitchImageError("CCSWITCH_ROUTE_CHANGED")
                meta.update(route.proof, status="submitted",
                            queue_elapsed_s=round(time.monotonic() - started, 3))
                save()
                upstream = time.monotonic()
                with client.stream("POST", base + "/v1/images/" + endpoint,
                                   json=body, headers=route.headers) as response:
                    payload, length = bytearray(), 0
                    success = 200 <= response.status_code < 300
                    limit = MAX_RESPONSE_BYTES if success else 16384
                    for chunk in response.iter_bytes():
                        length += len(chunk)
                        if length > limit:
                            raise CCSwitchImageError("CCSWITCH_RESPONSE_TOO_LARGE")
                        payload.extend(chunk)
                    if not success:
                        # Explicit 4xx rejections have no image result; 5xx/transport failures stay uncertain.
                        if response.status_code in {400, 401, 403, 404, 413, 415, 422, 429}:
                            meta["status"] = "rejected"
                        raise _http_error(response.status_code, payload, route.headers)
                results = json.loads(payload).get("data")
                del payload
                if not isinstance(results, list) or len(results) != 1:
                    raise CCSwitchImageError("CCSWITCH_IMAGE_CONTRACT_CHANGED")
                content = base64.b64decode(results[0]["b64_json"], validate=True)
                del results
                body.pop("images", None)
                if references:
                    encoded.clear()
                meta["upstream_elapsed_s"] = round(time.monotonic() - upstream, 3)
                if _subscription_state().proof != route.proof:
                    raise CCSwitchImageError("CCSWITCH_ROUTE_CHANGED_AFTER_SUBMISSION")
            width, height = _validate(content, hashes)
            temporary = file.with_suffix(".png.tmp")
            temporary.write_bytes(content)
            os.replace(temporary, file)
            meta.update(status="complete", width=width, height=height, file=file.name,
                        artifact_path=str(file.resolve()), sha256=sha256(content).hexdigest(),
                        elapsed_s=round(time.monotonic() - started, 3))
            save()
            return SubscriptionImageResult(file, meta)
        except (CCSwitchImageError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            error = exc if isinstance(exc, CCSwitchImageError) else CCSwitchImageError("CCSWITCH_RESULT_UNCERTAIN")
            meta.update(error=error.code,
                        status="uncertain" if meta["status"] == "submitted" else meta["status"],
                        elapsed_s=round(time.monotonic() - started, 3))
            save()
            raise error from None
