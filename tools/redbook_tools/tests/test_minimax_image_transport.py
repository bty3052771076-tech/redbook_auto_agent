from __future__ import annotations

import json
import os
import socket
import ssl
import traceback
import urllib.request

import httpx
import pytest

from src.images import auto_image, minimax_images as mm


@pytest.fixture(autouse=True)
def isolated_transport(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("test must not use network or urllib's Windows bypass path")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(urllib.request, "urlopen", denied)
    monkeypatch.setattr(urllib.request, "proxy_bypass", denied)
    monkeypatch.setattr(mm, "load_minimax_subscription_key", lambda: "test-subscription-secret")
    for name in ("MINIMAX_BILLING_MODE", "MINIMAX_ALLOW_PAID_CREDITS", "MINIMAX_ALLOW_PAYGO",
                 "MINIMAX_IMAGE_TIMEOUT_S", "MINIMAX_IMAGE_DOWNLOAD_TIMEOUT_S", "MINIMAX_IMAGE_MODELS",
                 "MINIMAX_IMAGE_MODEL", "MINIMAX_IMAGE_BASE_URL", "MINIMAX_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def mock_http(monkeypatch, handler):
    original = httpx.Client
    settings = []

    def client(**kwargs):
        settings.append(kwargs.copy())
        # Use a real httpx client and response decoder; replace only network I/O.
        return original(**{**kwargs, "transport": httpx.MockTransport(handler), "trust_env": False})

    monkeypatch.setattr(httpx, "Client", client)
    return settings


def config():
    return mm.MiniMaxImageConfig(api_key="test-subscription-secret", base_url="https://api.example/v1")


def test_submit_preserves_json_auth_timeout_and_verified_proxy_policy(monkeypatch):
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, json={"base_resp": {"status_code": 0}, "data": {"image_urls": []}})

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    before = os.environ.copy()
    settings = mock_http(monkeypatch, serve)
    result = mm._request_json(cfg=config(), payload={"prompt": "完整场景", "n": 1}, timeout_s=7)
    assert result["base_resp"]["status_code"] == 0
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://api.example/v1/image_generation"
    assert requests[0].headers["authorization"] == "Bearer test-subscription-secret"
    assert json.loads(requests[0].content) == {"prompt": "完整场景", "n": 1}
    assert requests[0].extensions["timeout"]["read"] == 7
    assert settings[0].get("verify", True) is True
    assert settings[0].get("trust_env", True) is True
    assert "proxy" not in settings[0]
    assert os.environ == before


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_http_errors_keep_status_and_redact_echoed_credentials(monkeypatch, status):
    raw = 'Bearer test-subscription-secret https://user:pass@cdn.example/x?token=signed-value'
    mock_http(monkeypatch, lambda r: httpx.Response(status, text=raw))
    with pytest.raises(mm.MiniMaxImageAPIError) as caught:
        mm._request_json(cfg=config(), payload={}, timeout_s=5)
    error = caught.value
    assert error.status == status and error.code == "http_error"
    rendered = str(error) + error.message + error.url
    assert str(status) in rendered
    for secret in ("test-subscription-secret", "user:pass", "signed-value"):
        assert secret not in rendered


@pytest.mark.parametrize("body,code", [
    ('{"base_resp":{"status_code":1008,"status_msg":"insufficient balance"}}', "1008"),
    ('{"base_resp":{"status_code":1004,"status_msg":"api_key=test-subscription-secret invalid key"}}', "1004"),
    ('{"base_resp":{"status_code":2013,"status_msg":"invalid prompt"}}', "2013"),
    ('not json token=test-subscription-secret', "invalid_json"),
    ('[]', "invalid_response"),
])
def test_api_error_contract_and_invalid_response(monkeypatch, body, code):
    mock_http(monkeypatch, lambda r: httpx.Response(200, text=body))
    with pytest.raises(mm.MiniMaxImageAPIError) as caught:
        mm._request_json(cfg=config(), payload={}, timeout_s=5)
    assert caught.value.status == 200 and caught.value.code == code
    assert "test-subscription-secret" not in str(caught.value)


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout, httpx.ProxyError])
def test_network_error_retains_type_stage_and_safe_nested_cause(monkeypatch, error_type):
    def fail(request):
        try:
            raise ssl.SSLEOFError("TLS EOF at https://alice:password@proxy.example/t?key=hidden-query")
        except ssl.SSLEOFError as cause:
            raise error_type("connection failed Bearer test-subscription-secret", request=request) from cause

    mock_http(monkeypatch, fail)
    with pytest.raises(mm.MiniMaxImageAPIError) as caught:
        mm._request_json(cfg=config(), payload={}, timeout_s=5)
    error = caught.value
    assert error.status is None and error.code == "network_error"
    assert "submit" in str(error) and error_type.__name__ in str(error)
    assert "SSLEOFError" in str(error) and "TLS EOF" in str(error)
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    for secret in ("test-subscription-secret", "alice:password", "hidden-query"):
        assert secret not in rendered


def test_download_redirect_has_no_subscription_auth_and_keeps_bytes(monkeypatch, tmp_path):
    requests = []
    payload = b"image fixture content" * 3

    def serve(request):
        requests.append(request)
        if request.url.host == "cdn.example":
            return httpx.Response(302, headers={"location": "https://storage.example/image.png?sig=abc"})
        return httpx.Response(200, content=payload)

    settings = mock_http(monkeypatch, serve)
    path = tmp_path / "image.png"
    mm._download(url="https://cdn.example/image.png", path=path, timeout_s=3)
    assert path.read_bytes() == payload
    assert len(requests) == 2
    assert all("authorization" not in r.headers for r in requests)
    assert all(r.extensions["timeout"]["read"] == 3 for r in requests)
    assert settings[0].get("verify", True) is True
    assert settings[0].get("trust_env", True) is True


@pytest.mark.parametrize("size", [0, 15, 16])
def test_download_minimum_size_does_not_write_invalid_file(monkeypatch, tmp_path, size):
    mock_http(monkeypatch, lambda r: httpx.Response(200, content=b"x" * size))
    path = tmp_path / "image.png"
    if size < 16:
        with pytest.raises(RuntimeError, match="too few bytes"):
            mm._download(url="https://cdn.example/image", path=path, timeout_s=3)
        assert not path.exists()
    else:
        mm._download(url="https://cdn.example/image", path=path, timeout_s=3)
        assert path.read_bytes() == b"x" * size


@pytest.mark.parametrize("failure", ["http", "timeout"])
def test_download_failure_has_safe_stage_and_does_not_overwrite(monkeypatch, tmp_path, failure):
    def serve(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(str(request.url), request=request)
        return httpx.Response(503, text="unavailable")

    mock_http(monkeypatch, serve)
    path = tmp_path / "image.png"
    path.write_bytes(b"previous good image")
    with pytest.raises(mm.MiniMaxImageAPIError) as caught:
        mm._download(url="https://cdn.example/image?signature=private-value", path=path, timeout_s=3)
    assert "download" in str(caught.value)
    assert "private-value" not in str(caught.value) + caught.value.url
    assert path.read_bytes() == b"previous good image"
    assert caught.value.code == ("http_error" if failure == "http" else "network_error")


@pytest.mark.parametrize("setting,value", [
    ("MINIMAX_BILLING_MODE", "paygo"), ("MINIMAX_ALLOW_PAID_CREDITS", "1"),
    ("MINIMAX_ALLOW_PAYGO", "true"),
])
def test_subscription_only_rejects_paid_modes_before_transport(monkeypatch, tmp_path, setting, value):
    calls = []
    mock_http(monkeypatch, lambda request: calls.append(request))
    monkeypatch.setenv(setting, value)
    with pytest.raises(RuntimeError, match="subscription_only|disabled by policy"):
        mm.generate_minimax_image(post_id="test", prompt="scene", dest_dir=tmp_path)
    assert calls == []


def test_abandoned_minimax_message_includes_safe_cause_without_extra_retries(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("MINIMAX_IMAGE_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("MINIMAX_TOKEN_PLAN_API_KEY", "unlabelled-secret")

    def fail(**kwargs):
        calls.append(kwargs)
        raise httpx.ReadTimeout("download failed unlabelled-secret https://u:p@cdn.example/a?key=private")

    monkeypatch.setattr(mm, "generate_minimax_image", fail)
    with pytest.raises(auto_image.ImageGenerationAbandoned) as caught:
        auto_image.fetch_and_download_related_images(
            title="news", body="", topics=[], prompt_hint="scene", provider="minimax",
            count=1, dest_dir=tmp_path,
        )
    assert len(calls) == 1 and caught.value.attempts == 1
    assert "ReadTimeout" in str(caught.value) and "download failed" in str(caught.value)
    text = str(caught.value) + repr(caught.value.errors)
    text += "".join(traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__))
    for secret in ("unlabelled-secret", "u:p", "private"):
        assert secret not in text


def test_minimax_error_constructor_redacts_url_and_json_key_values():
    error = mm.MiniMaxImageAPIError(
        url="https://name:password@api.example/v1?token=private#fragment",
        status=500, code="http_error",
        message='{"api_key": "test-key", "Authorization": "Bearer header-key"}',
    )
    rendered = str(error) + error.url + error.message
    for secret in ("name:password", "private", "fragment", "test-key", "header-key"):
        assert secret not in rendered
    assert "api.example" in rendered


def test_other_provider_abandoned_contract_is_unchanged():
    error = auto_image.ImageGenerationAbandoned(provider="aliyun", attempts=2, errors=["upstream failure"])
    assert str(error) == "image generation abandoned (provider=aliyun, attempts=2)"
    assert error.errors == ["upstream failure"]


@pytest.mark.parametrize("source", ["system", "environment", "no_proxy"])
def test_real_httpx_routing_honors_proxy_sources_without_registry_bypass(monkeypatch, source):
    import httpx._client
    import httpx._utils

    selected = []
    for name in tuple(os.environ):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            monkeypatch.delenv(name)
    if source != "system":
        monkeypatch.setenv("HTTPS_PROXY", "http://explicit.example:8899")
    if source == "no_proxy":
        monkeypatch.setenv("NO_PROXY", "api.example")
    monkeypatch.setattr(httpx._utils, "getproxies", lambda: urllib.request.getproxies_environment() or {
        "https": "http://system.example:7897",
    })

    class Transport(httpx.BaseTransport):
        def __init__(self, **kwargs):
            assert kwargs["verify"] is True and kwargs["trust_env"] is True
            proxy = kwargs.get("proxy")
            self.proxy = str(proxy.url) if proxy else None

        def handle_request(self, request):
            selected.append(self.proxy)
            return httpx.Response(200, json={"base_resp": {"status_code": 0}})

    monkeypatch.setattr(httpx._client, "HTTPTransport", Transport)
    mm._request_json(cfg=config(), payload={}, timeout_s=5)
    assert selected == [{"system": "http://system.example:7897", "environment": "http://explicit.example:8899",
                         "no_proxy": None}[source]]


def test_invalid_ca_path_fails_closed_without_request(monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    with pytest.raises(mm.MiniMaxImageAPIError) as caught:
        mm._request_json(cfg=config(), payload={}, timeout_s=5)
    assert "FileNotFoundError" in str(caught.value)
    assert caught.value.code == "network_error"


def test_missing_subscription_key_fails_before_transport(monkeypatch, tmp_path):
    monkeypatch.setattr(mm, "load_minimax_subscription_key", lambda: "")
    with pytest.raises(RuntimeError, match="api_key missing"):
        mm.generate_minimax_image(post_id="test", prompt="scene", dest_dir=tmp_path)
