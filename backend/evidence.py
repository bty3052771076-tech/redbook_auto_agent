"""Read-only source evidence extracted from a local post record."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def _public_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def source_evidence(platform: dict) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    news = platform.get("news") or {}
    picked = news.get("picked") or {}
    if isinstance(picked, dict):
        url = _public_url(news.get("source_url") or picked.get("url"))
        if url:
            rows.append({
                "title": str(picked.get("title") or "原始新闻"),
                "source": str(picked.get("source") or picked.get("domain") or ""),
                "published_at": str(picked.get("seendate") or picked.get("published_at") or ""),
                "url": url,
            })
    digest = platform.get("ai_digest") or {}
    if isinstance(digest, dict):
        for item in digest.get("items") or []:
            if not isinstance(item, dict):
                continue
            url = _public_url(item.get("url"))
            if not url:
                continue
            rows.append({
                "title": str(item.get("title") or "原始资讯"),
                "source": str(item.get("source_name") or item.get("vendor") or ""),
                "published_at": str(item.get("published_at") or ""),
                "url": url,
            })
    return rows
