from __future__ import annotations
from src.agent.capabilities.dispatcher import governed

import base64
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from src.config import DEFAULT_MINIMAX_LLM_BASE_URL
from src.minimax.quota import load_minimax_subscription_key


DEFAULT_BASE_URL = DEFAULT_MINIMAX_LLM_BASE_URL
DEFAULT_MODEL = "image-01"
DEFAULT_MODELS = ["image-01", "image-01-live"]
DEFAULT_ASPECT_RATIO = "3:4"
DEFAULT_TIMEOUT_S = 180.0
DEFAULT_DOWNLOAD_TIMEOUT_S = 60.0
MAX_PROMPT_CHARS = 1500


@dataclass(frozen=True)
class MiniMaxImageConfig:
    api_key: str
    base_url: str


@dataclass(frozen=True)
class MiniMaxImageResult:
    path: Path
    meta: dict[str, Any]


def safe_image_error(value: str, *, secrets: tuple[str, ...] = ()) -> str:
    """Sanitize diagnostics before truncation; never read local credential files."""
    text = str(value)
    known = set(secrets)
    known.update(v for k, v in os.environ.items()
                 if re.search(r"KEY|TOKEN|SECRET|PASSWORD", k, re.I) and len(v) >= 8)
    for secret in sorted(known, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[redacted]")

    def clean_url(match: re.Match) -> str:
        try:
            parts = urlsplit(match.group())
            host = parts.hostname or "redacted"
            if ":" in host:
                host = f"[{host}]"
            if parts.port:
                host += f":{parts.port}"
            return urlunsplit((parts.scheme, host, parts.path, "", ""))
        except ValueError:
            return "[redacted-url]"

    text = re.sub(r"[a-z][a-z0-9+.-]*://[^\s\"'<>]+", clean_url, text, flags=re.I)
    text = re.sub(r"\bBearer\s+[^\s\"',;}\]]+", "Bearer [redacted]", text, flags=re.I)
    text = re.sub(
        r"([\"']?(?:api[_-]?key|authorization|access_token|refresh_token|token|password|secret)[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)",
        r"\1[redacted]", text, flags=re.I,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", text)
    return " ".join(text.split())[:600]


def image_error_diagnostic(exc: BaseException, *, secrets: tuple[str, ...] = ()) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < 4:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {safe_image_error(str(current), secrets=secrets)}")
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return safe_image_error(" <- ".join(parts), secrets=secrets)


class MiniMaxImageAPIError(RuntimeError):
    def __init__(self, *, url: str, status: int | None, code: str, message: str,
                 secrets: tuple[str, ...] = ()):
        self.url = safe_image_error(url, secrets=secrets)
        self.status = status
        self.code = safe_image_error(code, secrets=secrets)
        self.message = safe_image_error(message, secrets=secrets)
        parts = [str(status)] if status is not None else []
        if self.code:
            parts.append(self.code)
        if self.message:
            parts.append(self.message)
        super().__init__("MiniMax image API error: " + ": ".join(parts or ["unknown"]))


def _split_models(value: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in re.split(r"[,\s;，；]+", value or "") if item.strip()))


def load_minimax_image_config() -> MiniMaxImageConfig:
    api_key = load_minimax_subscription_key()
    if not api_key:
        raise RuntimeError(
            "MiniMax Token Plan api_key missing: set MINIMAX_TOKEN_PLAN_API_KEY "
            "or create local docs/minimax_api-key.md"
        )
    billing_mode = (os.getenv("MINIMAX_BILLING_MODE") or "subscription_only").strip().lower()
    if billing_mode not in {"subscription", "subscription_only"}:
        raise RuntimeError("MiniMax image generation requires MINIMAX_BILLING_MODE=subscription_only")
    if any(
        (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}
        for name in ("MINIMAX_ALLOW_PAID_CREDITS", "MINIMAX_ALLOW_PAYGO")
    ):
        raise RuntimeError("MiniMax paid credits/paygo are disabled by policy")
    base_url = (
        os.getenv("MINIMAX_IMAGE_BASE_URL")
        or os.getenv("MINIMAX_BASE_URL")
        or DEFAULT_BASE_URL
    ).strip().rstrip("/")
    return MiniMaxImageConfig(api_key=api_key, base_url=base_url)


def _request_json(
    *,
    cfg: MiniMaxImageConfig,
    payload: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    url = f"{cfg.base_url}/image_generation"
    try:
        # HTTPX reads system/environment proxies without urllib's blocking
        # Windows proxy_bypass_registry -> getfqdn lookup.
        with httpx.Client(verify=True, trust_env=True, timeout=timeout_s, follow_redirects=True) as client:
            response = client.post(url, content=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "redbook-workflow/minimax-image",
            })
            raw = response.content.decode("utf-8", errors="replace")
            status = response.status_code
    except (httpx.HTTPError, OSError) as exc:
        raise MiniMaxImageAPIError(
            url=url, status=None, code="network_error",
            message=f"submit: {image_error_diagnostic(exc, secrets=(cfg.api_key,))}",
            secrets=(cfg.api_key,),
        ) from None
    if not 200 <= status < 300:
        raise MiniMaxImageAPIError(url=url, status=status, code="http_error",
                                  message=f"submit: {raw}", secrets=(cfg.api_key,))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise MiniMaxImageAPIError(url=url, status=status, code="invalid_json",
                                  message=f"submit: {raw}", secrets=(cfg.api_key,)) from None
    if not isinstance(data, dict):
        raise MiniMaxImageAPIError(url=url, status=status, code="invalid_response", message="response is not an object")
    base_resp = data.get("base_resp")
    if isinstance(base_resp, dict):
        code = str(base_resp.get("status_code") or "")
        if code not in {"", "0", "200"}:
            raise MiniMaxImageAPIError(
                url=url,
                status=status,
                code=code,
                message=str(base_resp.get("status_msg") or "provider rejected request"),
                secrets=(cfg.api_key,),
            )
    return data


def _download(*, url: str, path: Path, timeout_s: float) -> None:
    try:
        # A separate client prevents the subscription credential reaching a CDN.
        with httpx.Client(verify=True, trust_env=True, timeout=timeout_s, follow_redirects=True) as client:
            response = client.get(url, headers={"User-Agent": "redbook-workflow/minimax-image"})
            content = response.content
            status = response.status_code
    except (httpx.HTTPError, OSError) as exc:
        raise MiniMaxImageAPIError(url=url, status=None, code="network_error",
                                  message=f"download: {image_error_diagnostic(exc)}") from None
    if not 200 <= status < 300:
        raise MiniMaxImageAPIError(url=url, status=status, code="http_error",
                                  message=f"download: {content.decode('utf-8', errors='replace')}")
    if len(content) < 16:
        raise RuntimeError("MiniMax image download returned too few bytes")
    path.write_bytes(content)


def _extract_image(resp: dict[str, Any]) -> tuple[str | None, bytes | None]:
    data = resp.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("MiniMax image response missing data object")
    urls = data.get("image_urls")
    if isinstance(urls, list):
        for url in urls:
            if isinstance(url, str) and url.strip():
                return url.strip(), None
    encoded = data.get("image_base64")
    if isinstance(encoded, list):
        encoded = encoded[0] if encoded else ""
    if isinstance(encoded, str) and encoded.strip():
        try:
            return None, base64.b64decode(encoded)
        except Exception as exc:
            raise RuntimeError("MiniMax image_base64 is invalid") from exc
    raise RuntimeError("MiniMax image response contains no image_urls or image_base64")


def _model_candidates(model: Optional[str]) -> list[str]:
    configured = _split_models(os.getenv("MINIMAX_IMAGE_MODELS") or "")
    if configured:
        return configured
    selected = (model or os.getenv("MINIMAX_IMAGE_MODEL") or DEFAULT_MODEL).strip()
    return [selected] if selected else list(DEFAULT_MODELS)


@governed('builtin:image.generate', 'generate')
def generate_minimax_image(
    *,
    post_id: str,
    prompt: str,
    dest_dir: Path,
    timeout_s: Optional[float] = None,
    download_timeout_s: Optional[float] = None,
    model: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
) -> MiniMaxImageResult:
    if not prompt or not prompt.strip():
        raise ValueError("MiniMax image prompt must not be empty")
    prompt_text = prompt.strip()
    if len(prompt_text) > MAX_PROMPT_CHARS:
        raise ValueError(f"MiniMax image prompt exceeds {MAX_PROMPT_CHARS} characters")
    cfg = load_minimax_image_config()
    timeout = float(os.getenv("MINIMAX_IMAGE_TIMEOUT_S") or timeout_s or DEFAULT_TIMEOUT_S)
    download_timeout = float(
        os.getenv("MINIMAX_IMAGE_DOWNLOAD_TIMEOUT_S") or download_timeout_s or DEFAULT_DOWNLOAD_TIMEOUT_S
    )
    ratio = (aspect_ratio or os.getenv("MINIMAX_IMAGE_ASPECT_RATIO") or DEFAULT_ASPECT_RATIO).strip()
    candidates = _model_candidates(model)
    last_exc: Exception | None = None
    for model_name in candidates:
        payload = {
            "model": model_name,
            "prompt": prompt_text,
            "aspect_ratio": ratio,
            "response_format": "url",
            "n": 1,
            "prompt_optimizer": False,
        }
        try:
            response = _request_json(cfg=cfg, payload=payload, timeout_s=timeout)
            image_url, image_bytes = _extract_image(response)
            dest_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            path = dest_dir / f"minimax_{stamp}.png"
            if image_bytes is not None:
                if len(image_bytes) < 16:
                    raise RuntimeError("MiniMax base64 image returned too few bytes")
                path.write_bytes(image_bytes)
            elif image_url:
                _download(url=image_url, path=path, timeout_s=download_timeout)
            else:
                raise RuntimeError("MiniMax image response has no usable image")
            return MiniMaxImageResult(
                path=path,
                meta={
                    "mode": "minimax_image",
                    "provider": "minimax",
                    "post_id": post_id,
                    "model": model_name,
                    "aspect_ratio": ratio,
                    "prompt": prompt_text,
                    "response_format": "url" if image_url else "base64",
                    "src_url": image_url,
                    "request_id": response.get("id") or response.get("request_id"),
                    "metadata": response.get("metadata"),
                    "downloaded_path": str(path),
                    "downloaded_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as exc:
            last_exc = exc
            # Model fallback is allowed only for provider capability/quota or
            # transient failures; a malformed prompt/key should surface.
            text = str(exc).lower()
            if any(marker in text for marker in ("api_key missing", "401", "403", "invalid prompt", "1500")):
                raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("No MiniMax image model configured")
