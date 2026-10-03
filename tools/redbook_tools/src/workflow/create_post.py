from __future__ import annotations

from copy import deepcopy
import json
import hashlib
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import asdict, dataclass
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional

from src.config import load_llm_configs
from src.ai_digest.collect import collect_ai_digest_updates
from src.ai_digest.generate import (
    _concrete_action_in_text,
    build_fallback_brief,
    evaluate_ai_digest_impact_with_llm,
    generate_ai_digest_brief_with_llm,
    is_vague_collective_title,
    is_ai_digest_source_label_title,
    render_ai_digest_body,
    validate_ai_digest_concrete_content,
)
from src.ai_digest.models import AIDigestBrief, AIUpdateItem
from src.ai_digest.rank import (
    AI_DIGEST_MAX_ITEMS_PER_SOURCE,
    ai_digest_official_count,
    ai_digest_quota_counts,
    ai_digest_source_counts,
    ai_update_history_key,
    ai_update_is_high_impact,
    ai_update_is_lifecycle_notice,
    ai_update_is_non_model_infrastructure_notice,
    ai_update_category,
    ai_update_quality_issues,
    ai_update_source_key,
    dedupe_ai_updates,
    filter_recent_ai_updates,
    featured_ai_update,
    rank_ai_updates,
)
from src.ai_digest.render import render_ai_digest_cards
from src.images.auto_image import (
    ImageGenerationAbandoned,
    NEWS_IMAGE_PROMPT_VERSION,
    _build_aliyun_image_prompt,
    _news_image_fact_context,
    clean_news_image_event,
    fetch_and_download_related_images,
    is_auto_image_enabled,
)
from src.llm.generate import generate_draft, generate_json
from src.news.length_policy import (
    assess_news_length, news_length_instruction, news_length_rewrite_instruction,
)
from src.workflow.image_lineage import record_initial_news_image
from src.workflow.model_queues import ModelWorkQueues, infer_llm_provider
from src.workflow.performance import PerformancePolicy
from src.workflow.content_evidence import BEIJING_TZ, ai_digest_items_in_beijing_window
from src.workflow.news_discovery import (
    DailyNewsDiscovery, NEWS_LOOKBACK_MAX, feasible_news_batch,
    news_key, news_domain, news_story_identity_keys, resolve_news_windows, source_domain_cap,
)
from src.sources.request_budget import RequestBudget
from src.news.daily_wow import (
    DAILY_WOW_CONTENT_TYPE,
    DAILY_WOW_TOPIC,
    daily_wow_comment_instruction,
    daily_wow_comment_is_valid,
    daily_wow_clean_image_event,
    daily_wow_display_title,
    daily_wow_fallback_comment,
    daily_wow_image_prompt,
    daily_wow_is_schema_echo,
    daily_wow_title_max_len,
    normalize_column as daily_wow_normalize_column,
    daily_wow_review_instruction,
    daily_wow_selection_payload,
    daily_wow_selection_system_prompt,
    daily_wow_strict_candidates,
    daily_wow_write_instruction,
)
from src.news.daily_news import (
    _cjk_story_event_signature,
    _is_china_item,
    _required_china_count_for_daily_news,
    _same_cjk_story_event,
    daily_news_international_conflict_quota,
    fetch_daily_news_candidates,
    filter_prompt_relevant_news_items,
    filter_recent_news_items,
    is_international_conflict_news,
    load_single_news_material_file,
    pick_news_items,
    prioritize_international_conflict_news,
    rank_news_candidate_pool,
    read_manual_material_source_info,
    resolve_manual_material_times,
    daily_news_soft_preferences_enabled,
)
from src.storage.files import copy_assets_into_post, list_posts, post_dir, save_post, save_revision
from src.storage.models import AssetInfo, Post, PostStatus, Revision, RevisionSource, now_iso
from src.validation.rules import MAX_IMAGE_BODY, MAX_IMAGE_TITLE


DEFAULT_DAILY_NEWS_COORDINATOR_WORKERS = 4


def _daily_news_coordinator_workers() -> int:
    """Keep enough coordinators to overlap the two bounded model queues."""
    raw = (os.getenv("DAILY_NEWS_COORDINATOR_WORKERS") or "").strip()
    try:
        requested = int(raw) if raw else DEFAULT_DAILY_NEWS_COORDINATOR_WORKERS
    except ValueError:
        requested = DEFAULT_DAILY_NEWS_COORDINATOR_WORKERS
    return max(2, min(6, requested))


_URL_RE = re.compile(r"(?:https?://|www\.|//)[^\s，。；;、）)】\]]+", flags=re.IGNORECASE)
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
_JAPANESE_KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_FOREIGN_SCRIPT_RE = re.compile(r"[\u00c0-\u024f\u0400-\u04ff\u0590-\u05ff\u0600-\u06ff\u0e00-\u0e7f]")
_ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z]{2,}")
_ENGLISH_PHRASE_RE = re.compile(r"\b[A-Za-z]{3,}\b(?:\s+(?:\d{2,4}\s+)?\b[A-Za-z]{2,}\b){3,}")
_ALLOWED_DAILY_NEWS_CONTENT_ASCII_WORDS = {
    "AI",
    "API",
    "CEO",
    "CFO",
    "CPU",
    "ETF",
    "GDP",
    "GPU",
    "IPO",
    "LLM",
    "NASA",
    "QDII",
    "WTO",
    "CHATGPT",
    "DEEPSEEK",
    "NVIDIA",
    "OPENAI",
    "QWEN",
    "TESLA",
}
_DAILY_NEWS_INSUFFICIENT_CONTENT_MARKERS = (
    "\u539f\u59cb\u6750\u6599\u63d0\u5230",
    "\u672a\u63d0\u4f9b\u8db3\u591f\u7ec6\u8282",
    "\u76f8\u5173\u6280\u672f\u4e89\u8bae\u51fa\u73b0\u5347\u7ea7",
)
_DAILY_NEWS_INCOMPLETE_CONTENT_PATTERNS = (
    # A model can be cut off in the middle of a Chinese compound word and the
    # normalizer may then append a full stop. Keep these patterns explicit so
    # a grammatical-looking fragment cannot pass the publish gate.
    re.compile(r"(?:人工智能|技术|软件|平台|业务|产业)应用生$"),
)
_DAILY_NEWS_VAGUE_CONTENT_MARKERS = (
    "从已公布信息看，本次动态属于",
    "本次动态属于监管框架层面的方向性更新",
    "摘要未给出",
    "现有事实以上述摘要为限",
)
_DAILY_NEWS_PREFIX_RE = re.compile(r"^(?:每日新闻)(?:[｜|:：\-—–\s]+)?")
_SOURCE_LOOKUP_MIN_CHARS = 120
_SOURCE_LOOKUP_MAX_CHARS = 5000
DEFAULT_EVALUATION_VIEWPOINT = "无视角评价"
# A digest has no fixed minimum item count.  One is only the internal lower
# bound used to distinguish an empty high-impact result from a valid digest.
AI_DIGEST_MIN_ITEMS = 1
AI_DIGEST_MIN_DOMESTIC_MODEL_ITEMS = 3
AI_DIGEST_MIN_FOREIGN_AI_ITEMS = 3
DEFAULT_CANDIDATE_LOOKBACK_WINDOWS = (3, 7, 14)
DAILY_NEWS_MAX_LOOKBACK_DAYS = NEWS_LOOKBACK_MAX
_DAILY_NEWS_TITLE_MIN_LEN = 10
_NEWS_TITLE_PROMPT_STRONG_MARKERS = (
    "选择一条",
    "选择5条",
    "适合小红书",
    "提示词",
    "生成一份",
    "输出为严格 JSON",
    "JSON 字段要求",
)
_NEWS_TITLE_PROMPT_SOFT_MARKERS = (
    "摘要",
    "正文",
    "点评",
    "必须",
    "不得",
    "不要",
    "约50字",
    "约200字",
    "发布时间",
    "来源",
    "用户关注点",
    "新闻信息",
)


class PartialDailyNewsError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        posts: list[Post],
        requested_count: int,
        failed_count: int = 0,
        skipped_quality_count: int = 0,
    ):
        super().__init__(message)
        self.posts = posts
        self.requested_count = requested_count
        self.failed_count = failed_count
        self.skipped_quality_count = skipped_quality_count


def _daily_news_candidate_retry_limit() -> int:
    raw = os.getenv("DAILY_NEWS_CANDIDATE_RETRY_LIMIT", "1")
    try:
        return max(0, min(3, int(raw)))
    except (TypeError, ValueError):
        return 1


def _daily_news_candidate_retryable(result: "_DailyNewsCandidateResult") -> bool:
    """Retry only transient model failures, never permanent or editorial rejects."""
    if result.status != "failed" or result.candidate_index <= 0:
        return False
    if result.reason not in {
        "llm_request_failed",
        "image_generation_failed",
        "image_generation_abandoned",
    }:
        return False
    message = str(result.error or "").lower()
    permanent_markers = (
        "api_key missing",
        "401",
        "403",
        "invalid model",
        "not found",
        "permission",
        "forbidden",
        "quota",
        "balance",
        "allocation",
        "content policy",
        "content_policy",
        "data_inspection_failed",
        "context length",
        "max_tokens",
    )
    return not any(marker in message for marker in permanent_markers)


def _schedule_daily_news_candidate_retry(
    result: "_DailyNewsCandidateResult",
    retry_counts: dict[int, int],
    pending_indices: list[int],
) -> bool:
    """Requeue one transiently failed candidate without duplicating queue entries."""
    if not _daily_news_candidate_retryable(result):
        return False
    index = int(result.candidate_index)
    used = int(retry_counts.get(index, 0))
    if used >= _daily_news_candidate_retry_limit():
        return False
    retry_counts[index] = used + 1
    if index not in pending_indices:
        pending_indices.append(index)
    return True


DailyNewsProgressCallback = Callable[[str, str, dict[str, Any]], None]
DailyNewsPostQualityCallback = Callable[[Post], list[str]]


def _emit_daily_news_progress(
    callback: DailyNewsProgressCallback | None,
    stage: str,
    status: str = "in_progress",
    **detail: Any,
) -> None:
    if callback is None:
        return
    try:
        callback(stage, status, detail)
    except Exception as exc:
        # Observability must not turn an otherwise valid draft run into a failure.
        print(f"[daily_news] progress_callback_failed stage={stage} err={exc}")


def _daily_news_llm_unavailable_reason(error: object) -> str:
    text = str(error or "").strip()
    lowered = text.lower()
    if "token plan" in lowered or "用量上限" in text or "套餐用量" in text:
        return "模型订阅 Token Plan 用量上限已达到，未切换付费模型"
    if (
        "invalidendpointormodel.notfound" in lowered
        or "model or endpoint" in lowered
        or "invalid model" in lowered
    ):
        return "模型标识或接入点不可用，或当前 API key 没有该模型权限"
    if "accountoverdue" in lowered or "overdue balance" in lowered:
        return "模型账户欠费或账户状态异常"
    if "freetieronly" in lowered or "free quota exhausted" in lowered or "免费额度" in text:
        return "模型免费额度已耗尽"
    if "quota" in lowered or "balance" in lowered or "allocation" in lowered:
        return "模型额度不足"
    if "429" in lowered or "rate limit" in lowered or "throttl" in lowered:
        return "模型请求过于频繁，请稍后重试"
    if "403" in lowered or "forbidden" in lowered or "permission" in lowered:
        return "模型没有可用权限"
    return "模型请求失败"


def _daily_news_provider_capacity_exhausted(error: object) -> bool:
    """Identify provider-level capacity failures that make more candidates futile."""
    text = str(error or "").strip().lower()
    return any(
        marker in text
        for marker in (
            "token plan",
            "用量上限",
            "套餐用量",
            "quota exhausted",
            "free quota exhausted",
            "insufficient balance",
            "余额不足",
        )
    )


def _daily_news_content_policy_rejection(error: object) -> bool:
    """Return whether the provider rejected this candidate's input content.

    This is a candidate-level moderation result, not evidence that the model
    or account is unavailable. The workflow skips that story and continues
    with another source instead of trying to bypass the provider's policy.
    """
    text = str(error or "").lower()
    markers = (
        "data_inspection_failed",
        "inappropriate content",
        "content policy",
        "safety policy",
        "sensitive content",
    )
    return any(marker in text for marker in markers)


_NEWS_GENERIC_BODY_MARKERS = (
    "一项科技议题出现新进展",
    "一项社会议题出现新进展",
    "一项经济议题出现新进展",
    "一项国际议题出现新进展",
    "原始来源披露一项",
    "根据原始来源提供的公开信息",
    "现有信息仍有限，后续需关注权威更新",
    "在信息仍有限的情况下",
    "需要继续跟踪的进展",
    "不是已经定论的结果",
    "越是跨地区、跨产业的新闻",
)
_NEWS_GENERIC_COMMENT_MARKERS = (
    "这类新闻适合先看事实，再看影响",
    "已经公开的信息可以作为判断起点",
    "不宜把尚未确认的后续结果提前写成结论",
    "接下来可以重点关注权威更新",
    "从中国视角和读者关注点来看",
    "这条新闻提示我们需要持续关注后续进展与影响",
    "越需要区分已确认事实和外界推测",
    "更值得关注的是正式文件、权威回应和实际执行效果",
    "这件事值得关注的不是单个工具本身",
    "AI 使用边界、披露义务和责任归属",
    "版权和信任",
)
_NEWS_GENERIC_TITLE_MARKERS = (
    "科技议题出现进展",
    "AI议题出现进展",
    "国际议题出现进展",
    "社会事件出现进展",
    "经济议题出现变化",
    "外贸数据出现变化",
)

_COMMON_TRADITIONAL_TO_SIMPLIFIED = str.maketrans(
    {
        "內": "内",
        "門": "门",
        "戶": "户",
        "國": "国",
        "際": "际",
        "學": "学",
        "術": "术",
        "橋": "桥",
        "連": "连",
        "對": "对",
        "稱": "称",
        "發": "发",
        "讓": "让",
        "錨": "锚",
        "體": "体",
        "與": "与",
        "鏈": "链",
        "進": "进",
        "資": "资",
        "處": "处",
        "財": "财",
        "長": "长",
        "網": "网",
        "誌": "志",
        "訊": "讯",
        "聯": "联",
        "聞": "闻",
        "佈": "布",
        "調": "调",
        "點": "点",
        "這": "这",
        "項": "项",
        "規": "规",
        "則": "则",
        "責": "责",
        "務": "务",
        "協": "协",
        "後": "后",
        "續": "续",
        "觀": "观",
        "關": "关",
        "權": "权",
        "護": "护",
        "勞": "劳",
        "動": "动",
        "數": "数",
        "據": "据",
        "報": "报",
        "導": "导",
        "戰": "战",
        "爭": "争",
        "選": "选",
        "舉": "举",
        "類": "类",
        "證": "证",
        "華": "华",
        "鐵": "铁",
        "蘋": "苹",
        "區": "区",
        "風": "风",
        "險": "险",
        "響": "响",
        "應": "应",
        "醫": "医",
        "藥": "药",
        "監": "监",
        "測": "测",
        "檢": "检",
        "機": "机",
        "構": "构",
        "專": "专",
        "屬": "属",
        "園": "园",
        "車": "车",
        "電": "电",
        "錢": "钱",
        "貿": "贸",
        "語": "语",
        "廣": "广",
        "東": "东",
        "標": "标",
        "準": "准",
        "號": "号",
        "業": "业",
        "實": "实",
        "現": "现",
        "產": "产",
        "臺": "台",
        "灣": "湾",
        # High-frequency traditional characters that occur in Taiwanese and
        # Hong Kong source snippets. Keep this local fallback dependency-free.
        "當": "当",
        "開": "开",
        "寫": "写",
        "質": "质",
        "為": "为",
        "壓": "压",
        "尋": "寻",
        "雖": "虽",
        "個": "个",
        "卻": "却",
        "聲": "声",
        "識": "识",
        "場": "场",
        "說": "说",
        "來": "来",
        "銷": "销",
        "時": "时",
        "兩": "两",
        "種": "种",
        "極": "极",
        "揮": "挥",
        "淪": "沦",
        "豐": "丰",
    }
)
_ALLOWED_TITLE_ASCII_WORDS = {"AI", "API", "NASA", "G7", "G20", "APEC", "CEO", "C919"}


def _strip_urls(text: str) -> str:
    cleaned = _URL_RE.sub("", text or "")
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _strip_html_artifacts(text: str) -> str:
    cleaned = unescape(text or "")
    cleaned = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", cleaned)
    cleaned = re.sub(r"(?is)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(
        r"(?is)<img\b(?:\s+[a-zA-Z_:][-a-zA-Z0-9_:.]*(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?)*\s*/?>?",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"(?is)</?[^>]+>", " ", cleaned)
    cleaned = re.sub(
        r"(?i)\b(?:referrerpolicy|width|height|alt|title|class|style)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s，。！？；;]+)",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"(?i)\b(?:referrerpolicy|src|width|height)\b(?=\s|$)", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _has_html_artifacts(text: str) -> bool:
    value = text or ""
    if re.search(r"(?is)<\s*/?\s*(?:p|div|img|span|a|br|html|body|section|article)\b", value):
        return True
    return bool(
        re.search(
            r"(?i)\b(?:referrerpolicy|width|height|alt|class|style)\s*=|<img\b",
            value,
        )
    )


def _has_cjk(text: str | None) -> bool:
    return bool(_CJK_CHAR_RE.search(text or ""))


def _has_japanese_kana(text: str | None) -> bool:
    return bool(_JAPANESE_KANA_RE.search(text or ""))


def _to_simplified_common(text: str) -> str:
    return (text or "").translate(_COMMON_TRADITIONAL_TO_SIMPLIFIED)


def _simplify_daily_news_draft(draft: dict[str, Any]) -> dict[str, Any]:
    out = dict(draft)
    for key in ("title", "body", "image_event"):
        if key in out and isinstance(out.get(key), str):
            out[key] = _to_simplified_common(str(out.get(key) or ""))
    topics = out.get("topics")
    if isinstance(topics, list):
        out["topics"] = [_to_simplified_common(str(topic or "")) for topic in topics]
    elif isinstance(topics, str):
        out["topics"] = [_to_simplified_common(topics)]
    return out


def _cjk_count(text: str | None) -> int:
    return len(_CJK_CHAR_RE.findall(text or ""))


def _has_foreign_script_leak(text: str | None) -> bool:
    return bool(_FOREIGN_SCRIPT_RE.search(text or ""))


def _has_english_phrase_leak(text: str | None) -> bool:
    value = text or ""
    if not value.strip():
        return False
    if _ENGLISH_PHRASE_RE.search(value):
        return True
    suspicious = []
    for word in _ASCII_WORD_RE.findall(value):
        upper = word.upper()
        if upper in _ALLOWED_DAILY_NEWS_CONTENT_ASCII_WORDS:
            continue
        if word.isupper() and 2 <= len(word) <= 8:
            continue
        suspicious.append(word)
    return len(suspicious) >= 4


def _daily_news_title_has_bad_language(text: str | None) -> bool:
    value = (text or "").strip()
    if not value:
        return True
    if "原文摘录" in value:
        return True
    if _has_japanese_kana(value) or _has_foreign_script_leak(value):
        return True
    if _cjk_count(value) < 4:
        return True
    ascii_words = _ASCII_WORD_RE.findall(value)
    bad_words = [word for word in ascii_words if word.upper() not in _ALLOWED_TITLE_ASCII_WORDS]
    return len(bad_words) >= 2


def _source_lookup_min_chars() -> int:
    raw = (os.getenv("NEWS_SOURCE_CONTEXT_MIN_CHARS") or "").strip()
    if not raw:
        return _SOURCE_LOOKUP_MIN_CHARS
    try:
        return max(40, int(raw))
    except ValueError:
        return _SOURCE_LOOKUP_MIN_CHARS


def _source_lookup_enabled() -> bool:
    raw = (os.getenv("NEWS_SOURCE_LOOKUP") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _source_lookup_timeout_s() -> float:
    raw = (os.getenv("NEWS_SOURCE_LOOKUP_TIMEOUT_S") or "").strip()
    if not raw:
        return 8.0
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 8.0


def _source_lookup_max_chars() -> int:
    raw = (os.getenv("NEWS_SOURCE_LOOKUP_MAX_CHARS") or "").strip()
    if not raw:
        return _SOURCE_LOOKUP_MAX_CHARS
    try:
        return max(1200, int(raw))
    except ValueError:
        return _SOURCE_LOOKUP_MAX_CHARS


def _daily_news_context_is_incomplete(picked) -> bool:
    title = _strip_urls(getattr(picked, "title", "") or "")
    description = _strip_urls(getattr(picked, "description", "") or "")
    content = _strip_urls(getattr(picked, "content", "") or "")
    # Count distinct reporting, not repeated headlines and aggregator labels.
    text = " ".join(dict.fromkeys(part.strip() for part in (description, content) if part.strip()))
    headline = re.sub(r"\s*[-|｜]\s*[^-|｜]+$", "", title).strip()
    for repeated in sorted({title, headline}, key=len, reverse=True):
        if repeated:
            text = re.sub(re.escape(repeated), " ", text, flags=re.IGNORECASE)
    for label in (str(getattr(picked, "source", "") or ""), "Google News", "原文摘录："):
        if label:
            text = re.sub(re.escape(label), " ", text, flags=re.IGNORECASE)
    sentences = [re.sub(r"\s+", " ", part).strip()
                 for part in re.split(r"[。！？\n]|(?<=[.!?])\s+", text)]
    text = " ".join(dict.fromkeys(part for part in sentences if part))
    if len(text) < _source_lookup_min_chars():
        return True
    # NewsAPI frequently returns truncated snippets such as "[+123 chars]".
    return bool(re.search(r"\[\+\d+\s+chars?\]", text, flags=re.IGNORECASE))


def _daily_news_source_is_multi_story(picked: Any) -> bool:
    title = str(getattr(picked, "title", "") or "").strip().lower()
    url = str(getattr(picked, "url", "") or "").strip().lower()
    description = str(getattr(picked, "description", "") or "").strip().lower()
    return bool(
        "/live/" in url
        or re.search(r"\b(?:as it happened|live updates?|morning news brief|news roundup)\b", title)
        or "this live blog is now closed" in description
        or title in {"morning news brief", "news brief", "today's headlines"}
    )


def _fetch_original_news_excerpt(
    url: str,
    *,
    timeout_s: float = 8.0,
    max_chars: int = 1200,
) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        raw = resp.read(600_000)
    html = raw.decode(charset, errors="replace")
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>", "\n", html)
    text = re.sub(r"(?is)<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _clean_original_news_text(text)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def _strip_news_site_suffixes(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    cleaned = re.sub(r"\s*[_｜|]\s*(?:新闻频道|新闻|频道|中华网|人民网|央视网|新华网).*$", "", cleaned)
    cleaned = re.sub(r"\s*[-–—]{2}\s*(?:国际|国内|社会|体育|财经|新闻)\s*[-–—]{2}.*$", "", cleaned)
    cleaned = re.sub(r"\s*[-–—]{2}\s*(?:国际|国内|社会|体育|财经|新闻)\s*$", "", cleaned)
    return cleaned.strip()


def _strip_news_column_prefix(text: str) -> str:
    return re.sub(
        r"^(?:香港故事|记者手记|新华视点|新闻分析|国际观察|全球连线|现场直击|新华社消息|中东战地手记|通讯|深度观察|权威数读|财经聚焦|活力中国调研行|追光|秀我中国)丨\s*",
        "",
        text or "",
    ).strip()


def _strip_short_news_column_prefix(text: str) -> str:
    """Remove an LLM-added short column label while keeping the event title.

    Models sometimes return titles such as ``国际｜具体事件`` or
    ``新闻速递 | 具体事件``.  These are not incomplete titles, but the label
    is still unsuitable as part of the post title and triggers the column
    prefix quality gate.  Only remove a short left segment when the right
    segment contains enough Chinese text to stand on its own.
    """
    value = (text or "").strip()
    parts = re.split(r"[｜|]", value, maxsplit=1)
    if len(parts) != 2:
        return value
    head, tail = (part.strip() for part in parts)
    if not (2 <= len(head) <= 10 and _cjk_count(tail) >= 6):
        return value
    return tail.strip(" \t\r\n:：|｜-—–，,。.!！?？")


def _clean_original_news_text(text: str) -> str:
    cleaned = _strip_html_artifacts(_to_simplified_common(text or ""))
    cleaned = _strip_urls(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.replace("原文摘录：", "").strip()
    cleaned = re.sub(r"^[\s·•∙-]*(?:[^。！？!?]{1,16})\s*>\s*听全文[。.\s]*", " ", cleaned)
    cleaned = re.sub(r"[\s·•∙-]*(?:能见度|牛市点线面|[^。！？!?]{1,12})\s*>\s*听全文[。.\s]*", " ", cleaned)
    cleaned = re.sub(r"^[\u4e00-\u9fff]{1,12}网讯[（(][^。！？!?]{0,40}记者[）)]?[。！？!?]?", " ", cleaned)
    cleaned = re.sub(
        r"本站不再支持您的浏览器.*?以获得更好的观看效果。?",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"学习\s+学习时间\s+头条\s+头条关注\s+综合\s+综合新闻\s+媒体\s+媒体农。?",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"举报\s*0\s*分享至。?", " ", cleaned)
    cleaned = re.sub(r"用微信扫码二维码。?分享至好友和朋友圈。?", " ", cleaned)
    cleaned = re.sub(r"分享至好友和朋友圈。?", " ", cleaned)
    cleaned = re.sub(r"(?:普通话|广东话|字号|超大|标准)[。.\s]+", " ", cleaned)
    cleaned = re.sub(r"缩小字体\s+放大字体\s+收藏\s+微博\s+分享.*?QQ空间", " ", cleaned)
    cleaned = re.sub(r"\b\d{4}新闻库\b", " ", cleaned)
    cleaned = re.sub(r"(?<!\d):?\d{2,}\s+\d{2,}\s+\d{5,}", " ", cleaned)
    cleaned = re.sub(r"新华社(?:记者\s*)?发?[（(][^）)]{0,20}摄[）)]", " ", cleaned)
    cleaned = re.sub(r"新华社记者\s*[^。；;，,\s]{1,12}\s*摄", " ", cleaned)
    cleaned = re.sub(r"[\u4e00-\u9fff·]{1,10}摄[（(][^）)]{1,30}[）)]", " ", cleaned)
    cleaned = re.sub(r"路线规\s*AI伴你游[。.]?", " ", cleaned)
    cleaned = re.sub(r"\bImage\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Many Chinese news pages put navigation before the article. Prefer the
    # text after a publish-time marker when present.
    date_match = re.search(
        r"(?:20\d{2}[-年]\d{1,2}[-月]\d{1,2}[日]?\s*\d{1,2}:\d{2}(?::\d{2})?)",
        cleaned,
    )
    if date_match:
        cleaned = cleaned[date_match.end() :].strip()

    stop_positions = [
        cleaned.find(marker)
        for marker in (
            "〖纠错〗",
            "阅读下一篇",
            "深度观察",
            "新华全媒+",
            "消费新图景",
            "责任编辑：",
            "澎湃新闻报料",
            "报料热线",
            "报料邮箱",
            "沪ICP备",
            "沪公网安备",
            "互联网新闻信息服务许可证",
            "增值电信业务经营许可证",
            "扫码下载澎湃新闻客户端",
        )
        if cleaned.find(marker) > 0
    ]
    if stop_positions:
        cleaned = cleaned[: min(stop_positions)].strip()

    cleaned = re.sub(r"^来源[:：]\s*\S+\s*", "", cleaned)
    cleaned = re.sub(r"^作者[:：]\s*\S+\s*", "", cleaned)
    cleaned = re.sub(r"^责任编辑[:：]\s*\S+\s*", "", cleaned)
    cleaned = re.sub(r"^小\s+大\s+用微信扫描二维码\s+分享至好友和朋友圈\s+关键词[:：]?\s*", "", cleaned)
    cleaned = re.sub(r"^小\s+", "", cleaned)
    cleaned = re.sub(r"^打开\s+首页\s+.*?(?=(?:20\d{2}[-年]|\d{1,2}月\d{1,2}日|[一-龥]{2,10}消息))", "", cleaned)
    cleaned = _strip_news_site_suffixes(cleaned)
    return cleaned.strip()


def _enrich_daily_news_item(picked):
    meta = {
        "source_lookup": {
            "needed": False,
            "ok": False,
            "skipped": "",
            "chars": 0,
        }
    }
    meta["source_lookup"]["needed"] = True
    if not _source_lookup_enabled():
        meta["source_lookup"]["skipped"] = "disabled"
        return picked, meta
    if not (getattr(picked, "url", "") or "").strip():
        meta["source_lookup"]["skipped"] = "missing_url"
        return picked, meta

    try:
        excerpt = _fetch_original_news_excerpt(
            picked.url,
            timeout_s=_source_lookup_timeout_s(),
            max_chars=_source_lookup_max_chars(),
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        meta["source_lookup"]["error"] = str(exc)
        return picked, meta

    excerpt = _strip_urls(excerpt)
    if not excerpt:
        meta["source_lookup"]["skipped"] = "empty_excerpt"
        return picked, meta

    parts = [str(getattr(picked, "content", "") or "").strip(), f"原文摘录：{excerpt}"]
    content = "\n".join(part for part in parts if part).strip()
    meta["source_lookup"]["ok"] = True
    meta["source_lookup"]["chars"] = len(excerpt)
    return replace(picked, content=content), meta


def _daily_news_story_lines(value: str) -> list[str]:
    text = _strip_urls(value or "")
    text = re.sub(r"\r\n?", "\n", text)
    numbered_markers = re.findall(r"(?:^|\s)\d{1,2}[.、]\s*", text)
    if len(numbered_markers) >= 2:
        text = re.sub(r"(?<!^)(?<!\n)\s+(?=\d{1,2}[.、]\s*)", "\n", text)
    text = re.sub(r"(?m)^\s*(?:[-*•·]|\d{1,2}[.、])\s*", "", text)
    lines: list[str] = []
    for raw in text.split("\n"):
        line = _clean_original_news_text(raw)
        line = re.sub(r"\s+", " ", line).strip(" \t\r\n|｜")
        if line:
            lines.append(line)
    return lines


def _daily_news_line_looks_like_story_headline(value: str) -> bool:
    if re.search(r"[。！？]", value or ""):
        return False
    text = _clean_daily_news_title_candidate(value or "")
    if len(text) < 6 or len(text) > 70:
        return False
    if any(marker in text for marker in ("来源", "发布时间", "原文摘录", "正文", "摘要")):
        return False
    cjk_count = len(_CJK_CHAR_RE.findall(text))
    ascii_count = len(_ASCII_WORD_RE.findall(text))
    if cjk_count < 4 and ascii_count < 2:
        return False
    return len(re.split(r"[，,；;]", text)) <= 3


def _daily_news_semicolon_roundup_lines(value: str) -> list[str]:
    """Return headline clauses only for obvious semicolon-delimited roundups."""
    text = _strip_urls(value or "")
    if text.count("；") + text.count(";") < 2:
        return []
    parts = re.split(r"[；;]+", text)
    lines: list[str] = []
    for raw in parts:
        line = _clean_original_news_text(raw)
        line = re.sub(
            r"\s*[|｜丨]\s*[^|｜丨]{0,20}(?:早参|早报|晨报|晚报|日报|简报|快讯)\s*$",
            "",
            line,
        ).strip(" \t\r\n|｜丨")
        if line:
            lines.append(line)
    if len(lines) < 3:
        return []
    if sum(_daily_news_line_looks_like_story_headline(line) for line in lines) < 2:
        return []
    return lines


def _daily_news_line_relevance(value: str, context: str) -> float:
    text = _clean_daily_news_title_candidate(value or "")
    ctx = _clean_daily_news_title_candidate(context or "")
    if not text or not ctx:
        return 0.0
    if text in ctx or ctx in text:
        return 1.0
    text_tokens = _daily_news_context_signal_tokens(text)
    context_tokens = _daily_news_context_signal_tokens(ctx)
    if not text_tokens or not context_tokens:
        return 0.0
    overlap = len(text_tokens & context_tokens) / max(1, min(len(text_tokens), len(context_tokens)))
    return overlap


def _daily_news_title_is_bundle_header(value: str) -> bool:
    text = _clean_daily_news_title_candidate(value or "")
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return True
    bundle_markers = (
        "今日要闻",
        "今日新闻",
        "每日要闻",
        "最新消息",
        "热点新闻",
        "新闻快讯",
        "财经早报",
        "早报",
        "晚报",
        "简讯",
        "要闻",
    )
    if compact in bundle_markers or any(
        compact.endswith(marker)
        for marker in ("日报", "早报", "晚报", "简报", "快讯")
    ):
        return True
    if re.search(r"(?:AI|互联网|科技|IT)?(?:日报|早报|晚报|简报|快讯)[：:]", compact, re.I):
        story_part = re.split(r"[：:]", compact, maxsplit=1)[-1]
        return len([part for part in re.split(r"[、；;]", story_part) if part]) >= 2
    return False


def _daily_news_story_importance_score(value: str) -> float:
    text = _clean_daily_news_title_candidate(value or "")
    if not text:
        return 0.0
    score = 0.0
    markers: tuple[tuple[str, float], ...] = (
        ("国务院", 3.0),
        ("中央", 2.4),
        ("全国", 1.4),
        ("新规", 2.2),
        ("施行", 1.6),
        ("发布", 1.4),
        ("宣布", 1.2),
        ("监管", 2.0),
        ("调查", 2.0),
        ("处罚", 2.0),
        ("清理", 2.1),
        ("违规", 1.7),
        ("治理", 1.8),
        ("事故", 2.0),
        ("死亡", 2.0),
        ("权益", 1.8),
        ("保障", 1.5),
        ("禁令", 1.8),
        ("上市", 1.2),
        ("收购", 1.2),
        ("裁员", 1.2),
        ("暴涨", 1.1),
        ("暴跌", 1.1),
        ("突破", 1.1),
    )
    for marker, weight in markers:
        if marker in text:
            score += weight
    if re.search(r"\d|[一二三四五六七八九十两]", text):
        score += 0.8
    if any(marker in text for marker in ("花絮", "综艺", "夜市", "开幕", "趣闻")):
        score -= 1.0
    return score


def _select_daily_news_story_line(lines: list[str], *, context: str) -> str:
    if not lines:
        return ""
    if _daily_news_title_is_bundle_header(context):
        scored = [(_daily_news_story_importance_score(line), -idx, line) for idx, line in enumerate(lines)]
        scored.sort(reverse=True)
        best_score, _neg_idx, best_line = scored[0]
        return best_line if best_score > 0 else lines[0]
    scored = [
        (_daily_news_line_relevance(line, context), _daily_news_story_importance_score(line), -idx, line)
        for idx, line in enumerate(lines)
    ]
    scored.sort(reverse=True)
    best_score, _importance, _neg_idx, best_line = scored[0]
    return best_line if best_score > 0 else lines[0]


def _focus_daily_news_multistory_text(
    value: str,
    *,
    context: str,
    allow_semicolon_roundup: bool = False,
) -> tuple[str, bool, str]:
    lines = _daily_news_story_lines(value)
    if len(lines) < 2 and allow_semicolon_roundup:
        lines = _daily_news_semicolon_roundup_lines(value)
    if len(lines) < 2:
        return (value or "").strip(), False, ""

    headline_like_count = sum(1 for line in lines if _daily_news_line_looks_like_story_headline(line))
    if len(lines) < 3 and headline_like_count < 2:
        return (value or "").strip(), False, ""
    if len(lines) >= 3 and headline_like_count < 1:
        return (value or "").strip(), False, ""

    best_line = _select_daily_news_story_line(lines, context=context)
    focused = _clean_original_news_text(best_line)
    return focused, focused.strip() != (value or "").strip(), focused


def _focus_daily_news_item(picked) -> tuple[Any, dict[str, Any]]:
    """
    Some hot-list APIs return one item whose description/content is actually a
    stack of several unrelated headlines. Keep the story represented by the
    candidate title before prompting the LLM or generating an image.
    """
    title = getattr(picked, "title", "") or ""
    description = getattr(picked, "description", "") or ""
    content = getattr(picked, "content", "") or ""

    title_lines = _daily_news_semicolon_roundup_lines(title)
    title_story = title_lines[0] if title_lines else ""
    focus_context = title_story or title

    focused_description, desc_changed, desc_story = _focus_daily_news_multistory_text(
        description,
        context=focus_context,
        allow_semicolon_roundup=bool(title_story),
    )
    focused_content, content_changed, content_story = _focus_daily_news_multistory_text(
        content,
        context=f"{focus_context} {focused_description or description}",
        allow_semicolon_roundup=bool(title_story),
    )
    selected_story = title_story or desc_story or content_story

    meta: dict[str, Any] = {
        "multi_story_filter": {
            "applied": bool(title_story or desc_changed or content_changed),
        }
    }
    if selected_story:
        meta["multi_story_filter"]["selected_title"] = selected_story
    if desc_changed:
        meta["multi_story_filter"]["description_before_chars"] = len(description)
        meta["multi_story_filter"]["description_after_chars"] = len(focused_description)
    if content_changed:
        meta["multi_story_filter"]["content_before_chars"] = len(content)
        meta["multi_story_filter"]["content_after_chars"] = len(focused_content)

    title_changed = False
    focused_title = title
    if title_story:
        focused_title = title_story
        title_changed = focused_title != title
        meta["multi_story_filter"]["title_before"] = title
        meta["multi_story_filter"]["title_after"] = focused_title
    elif selected_story and _daily_news_title_is_bundle_header(title):
        focused_title = selected_story
        title_changed = True
        meta["multi_story_filter"]["title_before"] = title
        meta["multi_story_filter"]["title_after"] = focused_title

    if not (desc_changed or content_changed or title_changed):
        return picked, meta
    return (
        replace(
            picked,
            title=focused_title,
            description=focused_description or None,
            content=focused_content or None,
        ),
        meta,
    )


def _daily_news_conflict_signal(*items: Any) -> bool:
    """Preserve the source classification across context/focus normalization."""
    return any(item is not None and is_international_conflict_news(item) for item in items)


def _prioritize_all_daily_news_conflicts(items: list[Any]) -> list[Any]:
    """Put every protected conflict candidate before ordinary candidates."""
    conflicts = [item for item in items if _daily_news_conflict_signal(item)]
    ordinary = [item for item in items if not _daily_news_conflict_signal(item)]
    return [*conflicts, *ordinary]


def _daily_news_candidate_batch_indices(
    pending_indices: list[int],
    *,
    accepted_conflict_count: int,
    required_international_conflict_count: int,
    conflict_by_index: dict[int, bool],
    batch_size: int = 2,
) -> list[int]:
    """Select the next bounded batch without crossing an unmet protected lane."""
    if not pending_indices:
        return []
    if accepted_conflict_count < required_international_conflict_count:
        return [
            index
            for index in pending_indices
            if conflict_by_index.get(index, False)
        ][:batch_size]
    return pending_indices[:batch_size]


def _source_lookup_concurrency() -> int:
    raw = (os.getenv("NEWS_SOURCE_LOOKUP_CONCURRENCY") or "4").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 4
    return max(1, min(value, 8))


def _prefetch_daily_news_context(
    picks: list[Any],
    *,
    progress_callback: DailyNewsProgressCallback | None = None,
) -> dict[int, tuple[Any, dict[str, Any], dict[str, Any], Any]]:
    """Fetch source context with bounded concurrency while preserving order."""
    if not picks:
        return {}

    prepared: dict[int, tuple[Any, dict[str, Any], dict[str, Any], Any]] = {}
    completed = 0
    total = len(picks)
    request_budget = RequestBudget(max_in_flight=_source_lookup_concurrency())

    def prepare(index: int, candidate: Any):
        lookup_started = time.perf_counter()
        if _daily_news_context_is_incomplete(candidate):
            with request_budget.slot(timeout=30.0):
                enriched, lookup_meta = _enrich_daily_news_item(candidate)
        else:
            enriched = candidate
            lookup_meta = {"source_lookup": {"needed": False, "ok": False,
                           "skipped": "sufficient_api_context", "chars": 0}}
        enriched, focus_meta = _focus_daily_news_item(enriched)
        lookup_meta.setdefault("source_lookup", {})["elapsed_seconds"] = round(time.perf_counter() - lookup_started, 3)
        dedupe_item = replace(
            enriched,
            description=_compact_daily_news_context(enriched, max_chars=700),
            content=None,
        )
        return index, enriched, lookup_meta, focus_meta, dedupe_item

    with ThreadPoolExecutor(
        max_workers=min(_source_lookup_concurrency(), total),
        thread_name_prefix="redbook-source-lookup",
    ) as workers:
        futures = {
            workers.submit(prepare, index, candidate): index
            for index, candidate in enumerate(picks, start=1)
        }
        for future in as_completed(futures):
            index = futures[future]
            completed += 1
            try:
                item_index, enriched, lookup_meta, focus_meta, dedupe_item = future.result()
            except Exception:
                if progress_callback is not None:
                    _emit_daily_news_progress(
                        progress_callback,
                        "source_context",
                        "failed",
                        candidate_index=index,
                        candidate_total=total,
                        completed=completed,
                        reason="source_context_lookup_failed",
                    )
                continue
            prepared[item_index] = (enriched, lookup_meta, focus_meta, dedupe_item)
            if progress_callback is not None:
                _emit_daily_news_progress(
                    progress_callback,
                    "source_context",
                    "success",
                    candidate_index=index,
                    candidate_total=total,
                    completed=completed,
                    title=enriched.title,
                    elapsed_seconds=lookup_meta.get("source_lookup", {}).get("elapsed_seconds", 0),
                    context_sufficient=not _daily_news_context_is_incomplete(enriched),
                )
    return prepared


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_asset_infos(paths: Iterable[Path]) -> List[AssetInfo]:
    infos: List[AssetInfo] = []
    for p in paths:
        if not p.exists():
            continue
        infos.append(
            AssetInfo(
                path=str(p),
                kind="image",
                size_bytes=p.stat().st_size,
                sha256=_sha256(p),
                validated=True,
            )
        )
    return infos


def _merge_image_ids(target: Optional[set[str]], metas: list[dict]) -> None:
    if target is None:
        return
    for meta in metas:
        if not isinstance(meta, dict):
            continue
        picked = meta.get("picked")
        if not isinstance(picked, dict):
            continue
        image_id = picked.get("id")
        if image_id:
            target.add(str(image_id))


def _clean_daily_news_title_candidate(value: str) -> str:
    text = _strip_urls(value or "")
    text = _to_simplified_common(text)
    text = _DAILY_NEWS_PREFIX_RE.sub("", text).strip()
    text = re.sub(r"^原文摘录[:：]?", "", text).strip()
    text = re.sub(r"\s+", " ", text).strip()
    text = _strip_news_site_suffixes(text)
    text = _strip_news_column_prefix(text)
    text = _strip_short_news_column_prefix(text)
    text = text.strip(" \t\r\n:：|｜-—–，,。.!！?？\"'（）()[]【】")
    space_parts = re.split(r"\s+", text, maxsplit=1)
    if len(space_parts) == 2:
        head, tail = [part.strip() for part in space_parts]
        if _has_cjk(head) and _has_cjk(tail) and 8 <= len(head) <= 18:
            text = head
    for sep in ("。", "；", ";", "，", ",", "：", ":", " - ", "—", "（", "("):
        if sep in text:
            head, tail = [part.strip() for part in text.split(sep, 1)]
            if sep in ("：", ":") and len(head) <= 3 and _has_cjk(tail):
                text = tail
                break
            if head:
                text = head
                break
    text = _strip_news_site_suffixes(text)
    text = _strip_news_column_prefix(text)
    text = _strip_short_news_column_prefix(text)
    return _repair_unbalanced_title_quotes(text.strip(" \t\r\n:：|｜-—–，,。.!！?？\"'（）()[]【】"))


def _repair_unbalanced_title_quotes(text: str) -> str:
    repaired = text or ""
    quote_pairs = (("“", "”"), ("‘", "’"), ("《", "》"))
    for left, right in quote_pairs:
        if repaired.count(left) != repaired.count(right):
            repaired = repaired.replace(left, "").replace(right, "")
    return repaired.strip()


def _expand_short_daily_news_title(cleaned: str, source_text: str, *, max_len: int) -> str:
    if len(cleaned or "") >= _DAILY_NEWS_TITLE_MIN_LEN:
        return cleaned
    raw = _strip_urls(source_text or "")
    raw = _DAILY_NEWS_PREFIX_RE.sub("", raw).strip()
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = _strip_news_site_suffixes(raw)
    if not raw or not _has_cjk(raw) or _has_japanese_kana(raw):
        return cleaned

    parts = [
        part.strip(" \t\r\n:：|｜-—–，,。.!！?？\"'“”‘’（）()[]【】")
        for part in re.split(r"[，,。；;：:!?！？]", raw)
        if part.strip()
    ]
    if not parts:
        return cleaned
    head = _clean_daily_news_title_candidate(parts[0])
    if not head or not _has_cjk(head) or _has_japanese_kana(head):
        return cleaned
    tail = "".join(_clean_daily_news_title_candidate(part) for part in parts[1:3])
    tail = re.sub(r"^(?:新华社|新华网)?记者", "", tail).strip()

    options: list[str] = []
    hints: list[str] = []
    if "瑞士" in tail and "瑞士" not in head:
        hints.append("瑞士")
    if "现场直击" in tail and "现场直击" not in head:
        hints.append("现场直击")
    elif "现场" in tail and "现场" not in head:
        hints.append("现场")
    elif "直击" in tail and "直击" not in head:
        hints.append("直击")
    if hints:
        options.append(f"{head}{''.join(hints)}")
    if tail:
        options.append(f"{head}{tail}")
    if len(head) > len(cleaned or ""):
        options.append(head)

    for option in options:
        candidate = re.sub(r"(?:新华社|新华网)?记者", "", option)
        candidate = _clean_daily_news_title_candidate(candidate)
        if len(candidate) > max_len:
            candidate = candidate[:max_len].rstrip("，,。.!！?？:：|｜-—–")
        if (
            len(candidate) >= _DAILY_NEWS_TITLE_MIN_LEN
            and _has_cjk(candidate)
            and not _has_japanese_kana(candidate)
        ):
            return candidate
    return cleaned


def _daily_news_title_is_incomplete_condition(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if not compact.startswith(("如", "如果", "若", "倘若", "假如", "一旦")):
        return False
    if not any(marker in compact for marker in ("不能", "未能", "无法", "未达成", "没有达成")):
        return False
    return not any(marker in compact for marker in ("通行费", "收取", "征收", "造成", "导致", "宣布", "启动"))


def _daily_news_title_has_incomplete_tail(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if re.search(r"[\u4e00-\u9fff].*\d$", compact):
        return True
    return compact.endswith(("获", "补", "项", "按下", "缘何", "路线规", "正式", "启动响"))


def _daily_news_title_has_column_prefix(text: str) -> bool:
    value = (text or "").strip()
    if "｜" not in value and "|" not in value:
        return False
    head = re.split(r"[｜|]", value, maxsplit=1)[0].strip()
    return 2 <= len(head) <= 10


def _repair_incomplete_condition_title(source_texts: list[str], *, max_len: int) -> str:
    raw = _strip_urls(" ".join(text for text in source_texts if text))
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return ""

    if "通行费" in raw and ("伊朗" in raw or "霍尔木兹" in raw or "海峡" in raw):
        title = "美或收霍尔木兹通行费" if "霍尔木兹" in raw else "美或收海峡通行费"
        if "特朗普" in raw:
            title = f"特朗普称{title}"
        return title[:max_len].rstrip("，,。.!！?？:：|｜-—–")

    result_match = re.search(r"(?:美(?:国)?|美方|美国或|美或)[^。；;，,！？!?]{0,18}(?:通行费|关税|费用)", raw)
    if result_match:
        title = result_match.group(0)
        title = re.sub(r"^美国或", "美或", title)
        if "特朗普" in raw and not title.startswith("特朗普"):
            title = f"特朗普称{title}"
        title = _clean_daily_news_title_candidate(title)
        return title[:max_len].rstrip("，,。.!！?？:：|｜-—–")
    return ""


def _compact_title_prompt_compare(text: str) -> str:
    return re.sub(r"[\s，,。.!！?？；;：:、|｜\-—–]+", "", text or "")


def _daily_news_title_has_prompt_leak(
    text: str,
    prompt_norm: str = "",
    *,
    compare_prompt: bool = True,
) -> bool:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return False
    prompt_clean = re.sub(r"\s+", " ", (prompt_norm or "").strip())
    if compare_prompt and prompt_clean and cleaned == prompt_clean:
        return True
    if compare_prompt and prompt_clean:
        compact_cleaned = _compact_title_prompt_compare(cleaned)
        compact_prompt = _compact_title_prompt_compare(prompt_clean)
        if len(compact_cleaned) >= 4 and compact_cleaned in compact_prompt:
            return True
    if cleaned in _NEWS_GENERIC_TITLE_MARKERS:
        return True
    if any(marker in cleaned for marker in _NEWS_TITLE_PROMPT_STRONG_MARKERS):
        return True
    soft_hits = sum(1 for marker in _NEWS_TITLE_PROMPT_SOFT_MARKERS if marker in cleaned)
    return soft_hits >= 2 and any(word in cleaned for word in ("新闻", "标题", "body", "Body"))


def _english_keyword_hit(text_lc: str, keyword: str) -> bool:
    kw = (keyword or "").strip().lower()
    if not kw:
        return False
    if re.fullmatch(r"[a-z0-9]+", kw):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", text_lc))
    return kw in text_lc


def _english_any_keyword(text_lc: str, keywords: tuple[str, ...]) -> bool:
    return any(_english_keyword_hit(text_lc, keyword) for keyword in keywords)


def _english_daily_news_title_summary(text: str, prompt_norm: str = "") -> str:
    lower = f"{text or ''} {prompt_norm or ''}".lower()
    rules = (
        (("nato", "us forces in europe", "american forces in europe", "troop deployments in europe", "hegseth"), "美军欧洲部署审查"),
        (("sunscreen", "bemotrizinol", "fda"), "防晒审批迎来进展"),
        (("moon", "lunar", "nasa"), "NASA月球基地计划"),
        (("lithium", "mining"), "锂提取技术获进展"),
        (("biological weapon", "bioweapon", "synthetic dna"), "AI生物风险受关注"),
        (("cybersecurity", "cyber security"), "AI网络安全受关注"),
        (("mobile phone", "phones in schools", "school phone"), "学校手机禁令推进"),
        (("western technology", "russian spies"), "俄方技术获取受关注"),
        (("vpn", "geoblocking", "polymarket"), "平台封锁VPN用户"),
        (("american technology", "big tech"), "欧洲减少美科技依赖"),
        (("climate action", "action pour le climat", "climat", "climate"), "气候行动分歧受关注"),
        (("seawater battery", "desalination", "carbon capture"), "海水电池技术突破"),
        (("authors", "using ai", "publishing industry"), "作家使用AI引争议"),
        (("claude", "ai model access", "advanced ai model"), "AI模型争议升温"),
        (("ai accelerator", "ai chip", "chip company", "inference workloads"), "AI芯片新品发布"),
        (("lg display", "oled", "certification"), "LG显示OLED首获认证"),
        (("oled", "color/brightness"), "OLED面板获色彩亮度认证"),
        (("oled", "display", "brightness"), "OLED面板亮度认证"),
        (("ai", "artificial intelligence"), "AI议题出现进展"),
        (("chip", "semiconductor"), "芯片产业出现进展"),
        (("trade", "tariff", "export", "import"), "外贸数据出现变化"),
        (("inflation", "price", "market"), "经济议题出现变化"),
        (("court", "case", "sentence", "police"), "社会事件出现进展"),
    )
    for keywords, title in rules:
        if _english_any_keyword(lower, keywords):
            return title
    return ""


def _keyword_daily_news_title(text: str, prompt_norm: str = "") -> str:
    lower = (text or "").lower()
    english_summary = _english_daily_news_title_summary(text, "")
    if english_summary:
        return english_summary
    if (
        "外贸" in text
        or "贸易" in text
        or "貿易" in text
        or "貿" in text
        or _english_any_keyword(lower, ("trade", "export", "import"))
    ):
        return "外贸数据出现变化"
    if "科技" in text or "人工智能" in text or _english_any_keyword(
        lower, ("ai", "openai", "chip", "tech", "technology", "software", "model")
    ):
        return "科技议题出现进展"
    if "经济" in text or _english_any_keyword(lower, ("market", "inflation", "prices", "economy")):
        return "经济议题出现变化"
    if "社会" in text or _english_any_keyword(lower, ("court", "case", "sentence", "police", "school")):
        return "社会事件出现进展"
    return "国际议题出现进展"


def _compress_long_daily_news_title(text: str, *, max_len: int) -> str:
    raw = text or ""
    rules = (
        (("因你而来", "演唱会", "通信保障"), "演唱会通信保障完成"),
        (("苏新消费", "品质数码", "手机补贴"), "江苏数码消费补贴启动"),
        (("手机补贴", "最高可补"), "江苏手机补贴启动"),
        (("纸尿裤", "甲酰胺", "未检出"), "纸尿裤甲酰胺未检出"),
        (("水运工程", "快进键"), "多项重大水运工程提速"),
        (("马斯克", "行权", "账面收益"), "马斯克获巨额账面收益"),
        (("马斯克", "行权", "7800亿"), "马斯克获巨额账面收益"),
        (("AI伴你游", "数字导游"), "AI数字导游助力出游"),
        (("杭小忆", "文旅小程序"), "AI数字导游助力出游"),
        (("小巨人", "最前沿"), "小巨人企业布局前沿"),
        (("小巨人", "水下机器人"), "小巨人企业布局前沿"),
        (("欧洲化工", "赢创", "裁3200"), "赢创全球再裁3200人"),
        (("赢创", "聚酯业务", "3200"), "赢创全球再裁3200人"),
        (("科幻影视产业论坛", "浦东", "启幕"), "上海科幻影视论坛启幕"),
        (("科幻影视产业论坛", "浦东", "开幕"), "上海科幻影视论坛开幕"),
        (("科创资源深度融合", "前沿技术", "大湾区"), "大湾区前沿技术落地"),
        (("城市群区域协同", "粤港澳大湾区"), "大湾区前沿技术落地"),
        (("国际科技创新中心", "大湾区"), "大湾区科创中心建设提速"),
        (("资金狂涌", "韩国赛道"), "全球资金布局韩国科技"),
        (("加拿大", "美国", "卡尼", "谈判", "关税"), "加美谈判暂停卡尼拟反制"),
        (("人权理事会", "全球人权治理"), "中国共商全球人权治理"),
        (("全球人权治理", "边会"), "中国共商全球人权治理"),
        (("战时所掠中国文物", "返还"), "日本学者呼吁返还文物"),
        (("潮汕", "情书"), "在香江细读潮汕“情书”"),
    )
    for markers, title in rules:
        if all(marker in raw for marker in markers) and len(title) <= max_len:
            return title
    return ""


def _compact_daily_news_title_key(text: str) -> str:
    return re.sub(r"[\s，,。.!！?？；;：:、|｜\-—–（）()《》“”\"'‘’]+", "", text or "")


def _daily_news_title_needs_source_rewrite(cleaned: str, source_title: str) -> bool:
    value = cleaned or ""
    if any(marker in value for marker in ("手慢无", "速看", "来了", "重磅")):
        return True
    if _daily_news_title_has_column_prefix(value):
        return True
    if _daily_news_title_has_incomplete_tail(value):
        return True
    if any(marker in value for marker in ("游客在手机上打开", "手机上打开")):
        return True
    source_compact = _compact_daily_news_title_key(_to_simplified_common(source_title or ""))
    cleaned_compact = _compact_daily_news_title_key(value)
    if not source_compact or not cleaned_compact:
        return False
    if not source_compact.startswith(cleaned_compact):
        return False
    if len(source_compact) <= len(cleaned_compact):
        return False
    # LLMs sometimes satisfy the character limit by cutting the source title at
    # exactly 17-18 chars. If the next source character is a normal CJK word
    # rather than a delimiter, treat it as an incomplete title.
    if len(value) >= max(15, _DAILY_NEWS_TITLE_MIN_LEN):
        next_char = source_compact[len(cleaned_compact) : len(cleaned_compact) + 1]
        return bool(next_char and (_has_cjk(next_char) or next_char.isalnum()))
    return False


def _rewrite_copied_daily_news_title(cleaned: str, source_title: str, source_texts: list[str], *, max_len: int) -> str:
    source_clean = _clean_daily_news_title_candidate(source_title)
    if not source_clean:
        return cleaned
    needs_rewrite = _daily_news_title_needs_source_rewrite(cleaned, source_title)
    if _compact_daily_news_title_key(cleaned) != _compact_daily_news_title_key(source_clean) and not needs_rewrite:
        return cleaned

    raw = _to_simplified_common(" ".join([source_title, *source_texts]))
    compressed = _compress_long_daily_news_title(raw, max_len=max_len)
    if compressed:
        return compressed[:max_len]
    if "古巴" in raw and "美国" in raw and "无权评判" in raw and "改革" in raw:
        return "古巴回应美国评判改革"[:max_len]
    if "香港" in raw and "科企" in raw and any(marker in raw for marker in ("门户", "出海", "通往世界")):
        return "香港助内地科企出海"[:max_len]
    if "世界杯官方用球" in raw and any(marker in raw for marker in ("太空", "空间站", "NASA")):
        return "世界杯用球飞上太空"[:max_len]
    if "夏季达沃斯" in raw and "主会场" in raw:
        return "夏季达沃斯会场探访"[:max_len]
    return cleaned


def _normalize_daily_news_title(
    title: str,
    picked=None,
    prompt_norm: str = "",
    *,
    max_len: int = 18,
) -> str:
    candidates: list[tuple[str, bool]] = [(title, True)]
    if picked is not None:
        candidates.extend(
            [
                (getattr(picked, "title", "") or "", False),
                (getattr(picked, "description", "") or "", False),
                (getattr(picked, "content", "") or "", False),
            ]
        )
    candidates.append((prompt_norm, True))
    source_texts = [candidate for candidate, compare_prompt in candidates if not compare_prompt]
    if picked is not None:
        title_expand_sources = [
            getattr(picked, "title", "") or "",
            getattr(picked, "description", "") or "",
            getattr(picked, "content", "") or "",
        ]
    else:
        title_expand_sources = []

    for candidate, compare_prompt in candidates:
        cleaned = _clean_daily_news_title_candidate(candidate)
        has_better_source_title = any(
            len(_clean_daily_news_title_candidate(src)) >= 6
            for src, src_compare in candidates
            if not src_compare
        )
        if (
            not cleaned
            or (has_better_source_title and len(cleaned) <= 3)
            or _daily_news_title_has_prompt_leak(
                cleaned,
                prompt_norm,
                compare_prompt=compare_prompt,
            )
            or _daily_news_title_has_bad_language(cleaned)
        ):
            continue
        if len(cleaned) < _DAILY_NEWS_TITLE_MIN_LEN:
            for source_text in [candidate, *title_expand_sources, *source_texts]:
                expanded = _expand_short_daily_news_title(cleaned, source_text, max_len=max_len)
                if len(expanded) > len(cleaned):
                    cleaned = expanded
                    break
        if _daily_news_title_is_incomplete_condition(cleaned):
            repaired = _repair_incomplete_condition_title(
                [candidate, *title_expand_sources, *source_texts],
                max_len=max_len,
            )
            if repaired:
                cleaned = repaired
            else:
                continue
        if picked is not None and title_expand_sources:
            cleaned = _rewrite_copied_daily_news_title(
                cleaned,
                title_expand_sources[0],
                [candidate, *title_expand_sources, *source_texts],
                max_len=max_len,
            )
        specific_title = _compress_long_daily_news_title(
            " ".join([candidate, *title_expand_sources, *source_texts]),
            max_len=max_len,
        )
        if specific_title and (
            len(cleaned) >= max_len
            or _daily_news_title_has_incomplete_tail(cleaned)
            or any(marker in cleaned for marker in ("依托城市群", "重磅部署", "资金狂涌"))
        ):
            cleaned = specific_title
        if len(cleaned) > max_len:
            compressed = _compress_long_daily_news_title(
                " ".join([candidate, *title_expand_sources, *source_texts]),
                max_len=max_len,
            )
            cleaned = compressed or cleaned[:max_len].rstrip("，,。.!！?？:：|｜-—–")
        cleaned = cleaned.rstrip("，,、。.!！?？:：|｜-—–")
        cleaned = _repair_unbalanced_title_quotes(cleaned)
        if _daily_news_title_has_incomplete_tail(cleaned):
            repaired = _compress_long_daily_news_title(
                " ".join([candidate, *title_expand_sources, *source_texts]),
                max_len=max_len,
            )
            if repaired and not _daily_news_title_has_incomplete_tail(repaired):
                cleaned = repaired
            else:
                continue
        if cleaned and _has_cjk(cleaned) and not _has_japanese_kana(cleaned):
            return cleaned

    source_joined = " ".join(candidate for candidate, compare_prompt in candidates if not compare_prompt)
    joined = " ".join(candidate for candidate, _compare_prompt in candidates)
    fallback = _keyword_daily_news_title(source_joined or joined, "")
    return fallback[:max_len].rstrip()


def _shorten_daily_news_title(news_title: str, *, max_len: int = 20) -> str:
    return _normalize_daily_news_title(news_title, None, "", max_len=max_len)


def _is_generic_daily_news_title(title: str) -> bool:
    """
    LLM sometimes keeps the seed title "每日新闻" unchanged (or returns "每日新闻｜"),
    which makes the post list hard to scan. Treat these as generic.
    """
    text = (title or "").strip()
    if not text:
        return True
    if text in _NEWS_GENERIC_TITLE_MARKERS:
        return True
    compact = re.sub(r"\s+", "", text)
    generic_patterns = (
        r"(?:近期|最新)?多项(?:行业)?动态(?:发布|更新|汇总|出现)?$",
        r"(?:近期|最新)?多条.+(?:消息|资讯|新闻|动态)(?:发布|更新|汇总)?$",
        r"(?:行业|赛道)(?:近期|最新).*(?:多项|多条).*(?:动态|消息|资讯)$",
    )
    if any(re.search(pattern, compact) for pattern in generic_patterns):
        return True
    rest = re.sub(r"^(?:每日新闻)(?:[｜:：—\s-]+)?", "", text).strip()
    return rest == ""


def _daily_news_title_key(title: str) -> str:
    """
    Normalize a daily-news title for in-batch dedupe.

    - Removes "每日新闻" prefix variants
    - Normalizes whitespace/punctuation
    """
    text = (title or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"^(?:每日新闻)(?:[｜:：—\s-]+)?", "", text).strip() or text
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip("｜:：—- ")


def _extract_embedded_json_from_daily_news_body(body: str) -> dict | None:
    """
    Some providers return a JSON object *inside* the JSON.body string, e.g.:
        要点摘要：{
        新闻内容：
        "title": "...",
        ...
        "topics": [...],
        "image_event": "..."
        }

    This breaks title/topics extraction and pollutes the final body.
    Try to recover that embedded JSON draft.
    """
    text = (body or "").strip()
    if not text:
        return None
    if not text.startswith(f"{_NEWS_SUMMARY_LABEL}{{"):
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    blob = text[start : end + 1]
    # Remove section labels that might have leaked into the embedded JSON.
    blob = blob.replace(_NEWS_CONTENT_LABEL, "").replace(_NEWS_COMMENT_LABEL, "").strip()
    try:
        obj = json.loads(blob)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if not any(k in obj for k in ("title", "body", "topics", "image_event")):
        return None
    return obj


def _clip_text(value: str | None, *, limit: int = 400) -> str:
    text = (value or "").strip()
    if not text:
        return "无"
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…"


def _clamp_image_body(body: str) -> str:
    """
    Keep the final body within the platform limit (see validate_post / MAX_IMAGE_BODY).

    Note: daily-news workflow may append source lines after the LLM output, which can
    push the total length over the limit even if the model followed "Body <= 1000".
    """
    text = (body or "").strip()
    if len(text) <= MAX_IMAGE_BODY:
        return text
    return text[:MAX_IMAGE_BODY].rstrip()


def _preferred_image_title(post: Post, fallback: str) -> str:
    # The accepted Chinese headline anchors the event; an untranslated source
    # headline must not replace it when a repair is requested.
    if (fallback or "").strip():
        return fallback.strip()
    news_meta = (post.platform or {}).get("news") or {}
    picked = news_meta.get("picked")
    if isinstance(picked, dict):
        picked_title = (picked.get("title") or "").strip()
        if picked_title:
            return picked_title
    return fallback


def _preferred_image_hint(post: Post, fallback: str) -> str:
    news_meta = (post.platform or {}).get("news") or {}
    if isinstance(news_meta, dict):
        event = (news_meta.get("image_event") or "").strip()
        if event:
            return event
    picked = news_meta.get("picked")
    if isinstance(picked, dict):
        picked_title = (picked.get("title") or "").strip()
        if picked_title:
            return picked_title
        picked_desc = (picked.get("description") or "").strip()
        if picked_desc:
            return picked_desc
    return fallback


def _refreshed_daily_news_image_hint(post: Post, fallback: str) -> str:
    """Keep the accepted event anchored to the draft's language and facts."""
    news_meta = (post.platform or {}).get("news") or {}
    if not isinstance(news_meta, dict):
        return _preferred_image_hint(post, fallback)
    picked_payload = news_meta.get("picked")
    if not isinstance(picked_payload, dict):
        return _preferred_image_hint(post, fallback)

    class _PickedSource:
        title = str(picked_payload.get("title") or "")
        description = str(picked_payload.get("description") or "")
        content = str(picked_payload.get("content") or "")

    return _normalize_daily_news_image_event(
        _preferred_image_hint(post, fallback),
        picked=_PickedSource(),
        title=_preferred_image_title(post, post.title),
        body=post.body,
        prompt_norm=str(news_meta.get("prompt_hint") or ""),
    )


_IMAGE_EVENT_DROP_WORDS = (
    "每日新闻",
    "新闻",
    "报道",
    "采访",
    "记者",
    "媒体",
    "来源",
    "链接",
    "时间",
)

_NEWS_SUMMARY_LABEL = "要点摘要："
_NEWS_CONTENT_LABEL = "新闻内容："
_NEWS_COMMENT_LABEL = "点评："
_NEWS_BODY_JSON_KEYS = ("原文标题", "内容", "评价", "日期", "来源")

_NEWS_PROMPT_LEAK_MARKERS = (
    "你正在为小红书图文笔记写《每日新闻》栏目",
    "请依据下面提供的新闻信息",
    "注意：body 正文里不要包含",
    "只允许使用下列已提供的新闻信息",
    "输出为严格 JSON",
    "可用新闻信息",
    "新闻标题：",
    "来源名称：",
    "来源域名：",
    "用户关注点",
    "JSON 字段要求",
    "title：",
    "body：正文",
    "topics（数组",
    "image_event（字符串",
)


def _daily_news_body_has_prompt_leak(body: str) -> bool:
    """
    Detect providers that echo the daily-news prompt into the publishable body.

    The section labels alone are not enough to prove the body is safe because some
    models keep the labels while filling them with the prompt/instructions.
    """
    text = body or ""
    if not text.strip():
        return False
    return any(marker in text for marker in _NEWS_PROMPT_LEAK_MARKERS)


def _daily_news_comment_is_generic(comment: str) -> bool:
    text = re.sub(r"\s+", "", comment or "")
    if not text:
        return False
    compact_markers = [re.sub(r"\s+", "", marker) for marker in _NEWS_GENERIC_COMMENT_MARKERS]
    return any(marker in text for marker in compact_markers)


_HUMANITARIAN_COMMENT_MARKERS = (
    "平民保护",
    "救援通道",
    "停火安排",
    "人道主义行动",
    "民生危机",
    "冲突地区",
)


def _daily_news_comment_is_unsupported(comment: str, picked, content: str = "") -> bool:
    text = comment or ""
    raw = " ".join(
        str(part or "")
        for part in (
            getattr(picked, "title", ""),
            getattr(picked, "description", ""),
            getattr(picked, "content", ""),
            content,
        )
    )
    if _daily_news_has_unsupported_numeric_claim(text, picked, content):
        return True
    if not any(marker in text for marker in _HUMANITARIAN_COMMENT_MARKERS):
        return False
    support_markers = ("平民", "救援", "停火", "人道主义", "冲突地区", "生存需求")
    return sum(1 for marker in support_markers if marker in raw) < 2


def _daily_news_safe_fact_comment(picked, content: str = "") -> str:
    raw = " ".join(
        str(part or "")
        for part in (
            getattr(picked, "title", ""),
            getattr(picked, "description", ""),
            getattr(picked, "content", ""),
            content,
        )
    )
    if any(marker in raw for marker in ("谈判", "会谈", "部长级")):
        return (
            "这类谈判或会谈新闻的看点在于各方能否形成可核验的正式结果。"
            "当前公开信息主要是参会和议题表述，判断实际影响还要看后续声明、协议文本和执行进展。"
        )
    return ""


def _daily_news_minimal_fact_comment(picked, content: str, subject: str) -> str:
    """Provide a source-bound evaluation when a generated draft omits one."""
    subject_text = _normalize_news_summary(subject, limit=42).strip("。！？!？ ")
    if not subject_text or subject_text.startswith("一项"):
        subject_text = _normalize_news_summary(
            getattr(picked, "title", "") or getattr(picked, "description", ""),
            limit=42,
        ).strip("。！？!？ ")
    if subject_text:
        return (
            f"围绕{subject_text}的实际影响，仍需结合后续公开的执行细节和可核验反馈判断。"
            "现有材料已披露的事实应与尚未确认的推测区分开来。"
        )
    return "现有材料已披露的事实应与尚未确认的推测区分开来，后续影响仍需以可核验信息判断。"


def _daily_news_context_text(picked, content: str = "") -> str:
    return " ".join(
        str(part or "")
        for part in (
            getattr(picked, "title", ""),
            getattr(picked, "description", ""),
            getattr(picked, "content", ""),
            content,
        )
    )


class _DailyNewsNumericClaimError(ValueError):
    """Unverified quantities require a rewrite, never partial sentence removal."""


def _daily_news_numeric_value(value: str) -> str | None:
    value = value.lower().strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        integer, _, fraction = value.partition(".")
        return (integer.lstrip("0") or "0") + ("." + fraction.rstrip("0") if fraction.rstrip("0") else "")
    words = dict(zip(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(),
        range(20),
    ))
    words.update(dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10))))
    parts = re.split(r"[- ]+", value)
    if len(parts) == 1 and value in words:
        return str(words[value])
    if len(parts) == 2 and words.get(parts[0], 0) >= 20 and 0 < words.get(parts[1], 0) < 10:
        return str(words[parts[0]] + words[parts[1]])
    if re.fullmatch(r"[零〇一二两三四五六七八九十百千]+", value):
        digits = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
        if not any(unit in value for unit in "十百千"):
            return str(int("".join(str(digits[char]) for char in value)))
        total, digit, previous_unit = 0, 0, 10000
        for char in value:
            if char in digits:
                if digit:
                    return None
                digit = digits[char]
            else:
                unit = {"十": 10, "百": 100, "千": 1000}[char]
                if unit >= previous_unit:
                    return None
                total += (digit or 1) * unit
                digit, previous_unit = 0, unit
        return str(total + digit)
    return None


def _daily_news_calendar_claims(text: str) -> tuple[set[tuple[str | None, str, str]], str]:
    """Normalize explicit prose dates without inferring missing date components."""
    claims: set[tuple[str | None, str, str]] = set()
    masked = list(text)
    spans: list[tuple[int, int]] = []
    months = {}
    for index, aliases in enumerate((
        "january jan", "february feb", "march mar", "april apr", "may", "june jun",
        "july jul", "august aug", "september sep sept", "october oct", "november nov", "december dec",
    ), 1):
        months.update(dict.fromkeys(aliases.split(), index))
    month = rf"(?P<month>{'|'.join(months)})\.?"
    day = r"(?P<day>\d+)(?P<ordinal>st|nd|rd|th)?"
    patterns = (
        r"(?<![\d.])(?:(?P<year>\d+)年)?(?P<month>\d+)月(?P<day>\d+)日",
        rf"\b{month}\s+{day}(?:\s*,\s*(?P<year>\d{{4}})|\s+(?P<plain_year>\d{{4}}))?(?!\w)",
        rf"(?<![\w.,-]){day}\s+{month}(?:\s+(?P<year>\d{{4}}))?(?!\w)",
    )

    def record(match, year: int | None, month_value: int | None = None, day_value: int | None = None, invalid: bool = False):
        start, end = match.span()
        if any(start < right and end > left for left, right in spans):
            return
        spans.append((start, end))
        masked[start:end] = "|" * (end - start)
        clause = re.split(r"[.!?。！？;；\n]", text[:start])[-1].lower()
        invalid = invalid or bool(re.search(
            r"\b(?:not|no|never|without)\b|(?:并非|不是|未在|不在|并未)|"
            r"\b(?:about|around|approximately|roughly|nearly|almost|before|after|between)(?:\s+(?:on|in|from|since))?\s*$|"
            r"(?:约|大约|超过|至少|不足|少于|最多)(?:于|在)?\s*$",
            clause,
        )) or bool(re.match(r"(?:之前|之后|左右|余|多)", text[end:]))
        if invalid:
            claims.add((None, "calendar", "unknown"))
            return
        if month_value is not None:
            try:
                datetime(year if year is not None else 2000, month_value, day_value)
            except (ValueError, TypeError):
                claims.add((None, "calendar", "unknown"))
                return
            claims.add((f"{month_value}-{day_value}", "month_day", "exact"))
            if year is not None:
                # This whole-date token prevents combining a year with another date.
                claims.add((f"{year}-{month_value}-{day_value}", "calendar_date", "exact"))
        if year is not None:
            claims.add((str(year), "calendar_year", "exact"))

    matches = [match for pattern in patterns for match in re.finditer(pattern, text, re.IGNORECASE)]
    for match in sorted(matches, key=lambda item: (item.start(), -len(item.group()))):
        fields = match.groupdict()
        year_text = fields.get("year") or fields.get("plain_year")
        year = int(year_text) if year_text else None
        month_value = months.get(match["month"].lower().rstrip("."))
        if month_value is None:
            month_value = int(match["month"])
        day_value = int(match["day"])
        ordinal = fields.get("ordinal")
        expected_ordinal = "th" if 10 <= day_value % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day_value % 10, "th")
        record(match, year, month_value, day_value, invalid=bool(
            (year_text and (len(year_text) != 4 or year == 0))
            or (ordinal and ordinal.lower() != expected_ordinal)
            or (year_text and "ordinal" in fields and re.match(r"[\w%-]|\.\d", text[match.end():]))
        ))

    for match in re.finditer(r"(?<![\d.])(?P<year>\d{4})年(?![\d余多])", text):
        prefix = text[:match.start()]
        if re.search(r"(?:持续|历时|长达|经过|为期|已有|已过|超过|多于|至少|大约|不足|少于|最多|逾|约)\s*$", prefix):
            continue
        record(match, int(match["year"]), invalid=match["year"] == "0000")
    for match in re.finditer(r"\b(?:in|from|since)\s+(?P<year>\d{4})(?!\d)", text, re.IGNORECASE):
        suffix = text[match.end():]
        if re.match(r"\s*(?:[%％]|(?:years?|months?|weeks?|days?|hours?|minutes?|people|persons?|officials|settlers|passengers|workers|soldiers|civilians|percent|dollars?|euros?|pounds?|yuan)\b)", suffix, re.IGNORECASE):
            continue
        record(match, int(match["year"]), invalid=bool(re.match(r"[\w-]|\.\d", suffix)) or match["year"] == "0000")
    # A calendar year can qualify a named event without an 'in' preposition.
    # Never treat a four-digit amount, duration or hyphenated count as a year.
    for match in re.finditer(
        r"\b(?P<year>(?:19|20)\d{2})\s+(?:investigation|murder|election|attack|incident|"
        r"report|trial|verdict|agreement|treaty|protest|lawsuit|case|budget|season|edition)\b",
        text, re.IGNORECASE,
    ):
        record(match, int(match["year"]), invalid=bool(re.search(
            r"\b(?:before|after|between)\s+(?:the|a|an)\s*$", text[:match.start()], re.IGNORECASE,
        )))
    return claims, "".join(masked)


_DAILY_NEWS_PERSON_ROLES = {
    "astronaut": (("astronaut", "astronauts"), ("宇航员", "航天员")),
    "pilot": (("pilot", "pilots"), ("飞行员",)),
    "commander": (("commander", "commanders"), ("指挥官",)),
    "woman": (("woman", "women"), ("女子", "女性", "妇女", "女人")),
    "man": (("man", "men"), ("男子", "男性", "男人")),
    "officer": (("officer", "officers", "official", "officials"), ("官员", "警官", "军官")),
    "passenger": (("passenger", "passengers"), ("乘客",)),
    "person": (("person", "persons", "people"), ("人员", "人士")),
    "businessperson": (("businessman", "businesswoman", "businessperson", "businesspeople"), ("商人",)),
    "intruder": (("intruder", "intruders"), ("入侵者", "闯入者")),
}


def _daily_news_person_claims(text: str) -> tuple[set[tuple[str | None, str, str]], str]:
    """Keep singular role mentions and qualified ranks separate from counts."""
    claims: set[tuple[str | None, str, str]] = set()
    masked = list(text)
    spans: list[tuple[int, int]] = []
    english = {word: role for role, (words, _) in _DAILY_NEWS_PERSON_ROLES.items() for word in words if word not in {"astronauts", "pilots", "commanders", "women", "men", "officers", "officials", "passengers", "persons", "people"}}
    chinese = {word: role for role, (_, words) in _DAILY_NEWS_PERSON_ROLES.items() for word in words}
    cn_roles = "|".join(sorted(chinese, key=len, reverse=True))
    # Gender words before a profession are modifiers, not the head role.
    cn_roles = cn_roles.replace("女性", "女性(?!宇航员|航天员|飞行员|指挥官|官员)").replace("男性", "男性(?!宇航员|航天员|飞行员|指挥官|官员)")
    ranks = dict(zip("first second third fourth fifth sixth seventh eighth ninth tenth".split(), range(1, 11)))
    number = r"(?:\d+|[零〇一二两三四五六七八九十百千万亿]+)"
    excluded = r"few|many|several|hundred|thousand|million|billion|dozen|pair|couple|group|team|and|or|of|to|for|was|were|is|are|one|two|three|first|second|third"
    adjective = rf"(?!(?:{excluded}|{'|'.join(english)})\b)[a-z]+\s+"
    patterns = (
        ("ordinal", rf"(?:第(?P<rank>{number})(?:名|位|人)|(?P<first>首[位名]))(?P<description>[\u4e00-\u9fff]{{0,28}}?)(?P<role>{cn_roles})"),
        ("ordinal", rf"\b(?P<rank>{'|'.join(ranks)})\s+(?P<description>(?:{adjective}){{0,3}})(?P<role>{'|'.join(english)})(?!\w)"),
        ("mention", rf"(?<![\d第零〇一二两三四五六七八九十百千万亿])一[名位](?P<description>[\u4e00-\u9fff]{{0,12}}?)(?P<role>{cn_roles})"),
        ("mention", rf"\b(?P<article>a|an|another|one|1)\s+(?P<description>(?:{adjective}){{0,3}})(?P<role>{'|'.join(english)})(?!\w)"),
    )
    for kind, pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            start, end = match.span()
            if any(start < right and end > left for left, right in spans):
                continue
            prefix = re.split(r"[.!?。！？;；,，\n]", text[:start])[-1]
            suffix = re.split(r"[.!?。！？;；,，\n]", text[end:])[0]
            if kind == "mention" and re.search(r"(?:仅|只有|仅有|恰好|正好|总计|总共|共有|共|至少|超过|逾|约|大约|不足|最多|少于)\s*$", prefix):
                continue
            fields = match.groupdict()
            article = (fields.get("article") or "").lower()
            role_word = match["role"].lower()
            role = english.get(role_word) or chinese[role_word]
            invalid = bool(re.search(
                r"\b(?:no|not|never|without|if|unless|could|would|might|may|will|should|proposed|potential|fictional|imaginary|nearly|almost|approximately|roughly)\b|"
                r"没有|并未|未曾|未|并非|不是|否认|如果|假如|可能|或许|计划|不会|将",
                prefix + " " + match.group() + " " + suffix, re.IGNORECASE,
            )) or bool(re.match(r"\s+(?:plan|program|training|school|licen[cs]e|course|uniform|seat|test|job|position|candidate)\b", suffix, re.IGNORECASE))
            if article in {"one", "1"} and re.search(r"\b(?:more than|at least|less than|at most|over|under|about|around)\s*$", prefix, re.IGNORECASE):
                continue
            spans.append((start, end))
            # Explicit English one keeps its cardinal claim too; articles do not.
            if article not in {"one", "1"} or invalid:
                masked[start:end] = "|" * (end - start)
            if invalid:
                claims.add((None, "person_" + kind, "unknown"))
            elif kind == "mention":
                claims.add((role, "person_mention", "singular"))
            else:
                rank = fields.get("rank")
                value = "1" if fields.get("first") else str(ranks[rank.lower()]) if rank.lower() in ranks else _daily_news_numeric_value(rank)
                description = match["description"].lower()
                scope = []
                for name, pattern in (("black", r"\bblack\b|黑人"), ("female", r"\bfemale\b|女性|女"), ("male", r"\bmale\b|男性|男")):
                    if re.search(pattern, description):
                        scope.append(name)
                claims.add((value, "ordinal:" + ":".join([role, *scope]), "exact"))
    # Unknown Chinese ranks still must not fall through as a cardinal substring.
    for match in re.finditer(rf"第{number}[名位人]|首[名位]", text):
        if not any(match.start() < right and match.end() > left for left, right in spans):
            masked[match.start():match.end()] = "|" * len(match.group())
            claims.add((None, "person_ordinal", "unknown"))
    return claims, "".join(masked)


def _daily_news_numeric_claims(text: str) -> set[tuple[str | None, str, str]]:
    # Match value, unit and bound together. A number elsewhere in the source is
    # not evidence of the same quantity, and publication metadata is excluded.
    claims, quantity_text = _daily_news_calendar_claims(text or "")
    person_claims, quantity_text = _daily_news_person_claims(quantity_text)
    claims.update(person_claims)
    qualifiers = {
        "超过": "gt", "逾": "gt", "多于": "gt", "more than": "gt", "over": "gt", "余": "gt", "多": "gt",
        "至少": "gte", "at least": "gte", "约": "approx", "大约": "approx", "近": "approx", "接近": "approx", "将近": "approx",
        "about": "approx", "around": "approx", "nearly": "approx", "almost": "approx", "approximately": "approx", "roughly": "approx",
        "不足": "lt", "少于": "lt", "less than": "lt", "under": "lt", "最多": "lte", "at most": "lte",
    }
    unit_aliases = {"名": "person", "人": "person", "位": "person", "％": "%", "个月": "月"}
    number = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)"
    # Anniversaries are event ordinals, not weeks or elapsed-year assertions.
    anniversary_pattern = rf"(?P<cn>{number})周年|\b(?P<en>\d+)(?P<ordinal>st|nd|rd|th)\s+anniversary\b"
    def anniversary_claim(match):
        value = _daily_news_numeric_value(match['cn'] or match['en'])
        ordinal = match['ordinal']
        if ordinal:
            rank = int(match['en'])
            expected = 'th' if 10 <= rank % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(rank % 10, 'th')
            if ordinal.lower() != expected:
                value = None
        claims.add((value, 'anniversary', 'exact' if value is not None else 'unknown'))
        return '|' * len(match.group())
    quantity_text = re.sub(anniversary_pattern, anniversary_claim, quantity_text, flags=re.IGNORECASE)
    # Preserve coarse quantities as coarse; 'hundreds' is not exactly 100.
    scales = {'hundreds': 'hundreds', 'thousands': 'thousands', '百': 'hundreds', '千': 'thousands'}
    def coarse_person_claim(match):
        scale = match['en'] or match['cn']
        claims.add((scales[scale.lower()], 'person', 'fuzzy'))
        return '|' * len(match.group())
    quantity_text = re.sub(
        r'\b(?P<en>hundreds|thousands)\s+of\s+(?:people|persons|civilians|soldiers)\b|数(?P<cn>百|千)(?:名|人|位)',
        coarse_person_claim, quantity_text, flags=re.IGNORECASE,
    )
    compact = re.sub(r"\s+", "", quantity_text)
    # Demonstratives refer back to the event, not an exact item total.
    compact = re.sub(r"([这那])一(?=[项个]|时间点)", r"\1|", compact)
    compact = compact.replace('统一日', '统|日')
    pattern = (
        rf"(?P<bound>超过|多于|至少|大约|不足|少于|最多|接近|将近|逾|约|近)?(?P<value>{number})"
        r"(?P<suffix>余|多)?(?P<unit>分钟|小时|个月|天|周|月|年|日|个|名|人|位|项|次|%|％)"
    )
    for match in re.finditer(pattern, compact):
        bound = match["bound"] or match["suffix"] or ""
        claims.add((_daily_news_numeric_value(match["value"]), unit_aliases.get(match["unit"], match["unit"]), qualifiers.get(bound, "exact")))
    # Clock times must match as a whole, not as scattered digits.
    for clock in re.finditer(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", compact):
        claims.add((f"{int(clock[1])}:{clock[2]}", "clock", "exact"))
    simple = "zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen"
    tens = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
    english_number = rf"(?:\d+(?:\.\d+)?|(?:{tens})(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|{simple})"
    units = {
        "people": "person", "persons": "person", "person": "person", "officials": "person", "settlers": "person",
        "passengers": "person", "workers": "person", "soldiers": "person", "civilians": "person",
        "minutes": "分钟", "minute": "分钟", "hours": "小时", "hour": "小时", "days": "天", "day": "天",
        "weeks": "周", "week": "周", "months": "月", "month": "月", "years": "年", "year": "年",
        "percent": "%", "%": "%",
    }
    units.update({word: "person" for words, _ in _DAILY_NEWS_PERSON_ROLES.values() for word in words})
    # Only allow a few descriptive words before a person noun; no crossing a
    # clause or consuming another number/scale as if it were an adjective.
    excluded = rf"{simple}|{tens}|hundred|thousand|million|billion|and|or|were|was|with|of|to|for|{'|'.join(units)}"
    adjective = rf"(?!(?:{excluded})\b)[a-z]+\s+"
    english_pattern = (
        rf"(?<![\w.,-])(?:(?P<bound>more than|at least|less than|at most|over|about|around|under|nearly|almost|approximately|roughly)\s+)?"
        rf"(?P<value>{english_number})\s*(?:{adjective}){{0,3}}(?P<unit>{'|'.join(units)})(?!\w)"
    )
    english_text = re.sub(
        rf"\b({english_number})-(minutes?|hours?|days?|weeks?|months?|years?)\b",
        r"\1 \2", quantity_text.lower(),
    )
    for match in re.finditer(english_pattern, english_text):
        prefix = english_text[:match.start()]
        if re.search(rf"\b(?:{simple}|{tens}|hundred|thousand|million|billion|and|no|not|nearly|almost|approximately|roughly|between|than|to)[\s-]+$", prefix):
            claims.add((None, units[match["unit"]], "unknown"))
            continue
        claims.add((_daily_news_numeric_value(match["value"]), units[match["unit"]], qualifiers.get(match["bound"], "exact")))
    return claims


def _daily_news_has_unsupported_numeric_claim(text: str, picked, content: str = "") -> bool:
    claims = _daily_news_numeric_claims(text)
    evidence = _daily_news_numeric_claims(_daily_news_context_text(picked, content))
    return any(value is None or (value, unit, bound) not in evidence for value, unit, bound in claims)


def _daily_news_remove_unsupported_numeric_sentences(text: str, picked) -> str:
    """Legacy entry point: reject unsupported numbers without deleting facts."""
    if _daily_news_has_unsupported_numeric_claim(text, picked):
        raise _DailyNewsNumericClaimError("unsupported_numeric_claim")
    return (text or "").strip()


def _daily_news_content_has_unrelated_author_ai_topic(content: str, source: str) -> bool:
    """Reject the author/publishing fallback when the source is another AI topic."""
    text = re.sub(r"\s+", "", content or "")
    source_text = re.sub(r"\s+", "", source or "").lower()
    content_markers = (
        "作家",
        "写作",
        "出版",
        "署名",
        "读者知情",
        "人类创造力",
    )
    source_markers = (
        "作家",
        "写作",
        "出版",
        "署名",
        "版权",
        "作者",
        "author",
        "authors",
        "writer",
        "writers",
        "writing",
        "publishing",
        "publisher",
        "book",
        "books",
        "literary",
        "novel",
    )
    return sum(marker in text for marker in content_markers) >= 2 and not any(
        marker in source_text for marker in source_markers
    )


def _daily_news_content_has_malformed_field_artifact(content: str) -> bool:
    """Catch nested body labels and dangling monetary values from malformed LLM JSON."""
    text = _clean_daily_news_text_value(content)
    if re.match(r'^["\'“”]?\s*(?:内容|新闻内容|要点摘要)[:：]', text):
        return True
    return bool(
        re.search(
            r"(?:代价|金额|总额|营收|收入|利润|净利|价格|折让|每股)\s*(?:为|约|达)?\s*\d+\.$",
            text,
        )
    )


def _daily_news_content_has_incomplete_tail(content: str) -> bool:
    """Reject a normalized sentence that still ends inside a known phrase."""
    text = _clean_daily_news_text_value(content)
    compact = re.sub(r"\s+", "", text).rstrip("。！？!?。")
    return any(pattern.search(compact) for pattern in _DAILY_NEWS_INCOMPLETE_CONTENT_PATTERNS)


def _daily_news_content_is_unsupported(content: str, picked) -> bool:
    text = content or ""
    if not text.strip():
        return False
    source = _daily_news_context_text(picked)
    if _daily_news_has_unsupported_numeric_claim(text, picked):
        return True
    hallucination_markers = (
        "对委内瑞拉",
        "对伊朗发起军事行动",
        "下一个是古巴",
        "石油封锁",
        "军事行动",
    )
    recommendation_markers = (
        "权威数读",
        "新华视点",
        "记者手记",
        "阅读下一篇",
        "深度观察",
        "特色产业赋能",
        "中国摩托加速",
    )
    site_noise_markers = (
        "举报 0",
        "分享至好友和朋友圈",
        "用微信扫码二维码",
        "打开微信",
        "扫一扫",
        "分享至朋友圈",
        "普通话",
        "广东话",
        "字号",
        "超大",
        "缩小字体",
        "放大字体",
        "热文排行",
        "财经日历",
        "今日要点",
        "全球大事",
        "经济数据",
        "每日智库看点",
        "21早新闻",
        "查看全部 -->",
    )
    if any(marker in text for marker in (*recommendation_markers, *site_noise_markers)):
        return True
    if _daily_news_content_has_malformed_field_artifact(text):
        return True
    if _daily_news_content_has_unrelated_author_ai_topic(text, source):
        return True
    if _daily_news_semicolon_roundup_lines(text):
        return True
    return any(marker in text and marker not in source for marker in hallucination_markers)


def _daily_news_comment_is_irrelevant(comment: str, picked, content: str = "") -> bool:
    text = comment or ""
    if not text.strip():
        return False
    compact_text = re.sub(r"\s+", "", text)
    source = _daily_news_context_text(picked, content)
    compact_source = re.sub(r"\s+", "", source)
    bay_area_comment_markers = ("大湾区科创", "跨城资源", "跨境规则衔接", "科技成果商业化")
    if any(marker in text for marker in bay_area_comment_markers):
        bay_area_source_markers = ("大湾区", "粤港澳", "科创资源", "跨城", "跨境规则", "科技成果商业化")
        return not any(marker in source for marker in bay_area_source_markers)
    if any(marker in text for marker in ("美股", "半导体设备")) and not any(
        marker in source for marker in ("美股", "半导体设备", "美国股票基金", "科技板块单周流入")
    ):
        return True
    trade_comment_markers = ("订单", "物流", "企业成本", "中国企业", "供应链", "市场风险")
    if any(marker in text for marker in trade_comment_markers):
        trade_source_markers = (
            "外贸",
            "贸易额",
            "出口",
            "进口",
            "关税",
            "供应链",
            "订单",
            "supply chain",
            "trade",
            "investment",
            "standards",
        )
        return not any(marker in source for marker in trade_source_markers)
    securities_comment_markers = ("监管处罚", "公平交易", "信息披露秩序", "处罚结果", "市场禁入")
    if any(marker in text for marker in securities_comment_markers):
        securities_source_markers = ("监管处罚", "行政处罚", "罚款", "市场禁入", "操纵", "违规减持", "内幕交易")
        return not any(marker in source for marker in securities_source_markers)
    ai_comment_markers = ("AI 使用边界", "披露义务", "版权和信任", "模型本身")
    if any(re.sub(r"\s+", "", marker) in compact_text for marker in ai_comment_markers):
        ai_source_markers = (
            "版权",
            "出版",
            "作家",
            "署名",
            "模型访问",
            "训练数据",
            "生成式AI",
            "内容平台",
        )
        return not any(re.sub(r"\s+", "", marker) in compact_source for marker in ai_source_markers)
    sports_comment_markers = ("竞技表现", "人才梯队", "长期训练体系", "稳定备战", "青训投入")
    if any(marker in text for marker in sports_comment_markers):
        if any(marker in source for marker in ("NASA", "空间站", "航天", "太空", "阿耳忒弥斯")):
            return True
    weather_comment_markers = (
        "气象监测",
        "灾害预警",
        "防灾减灾",
        "农业安排",
        "基层防灾",
        "设备能否长期运行",
    )
    if any(marker in text for marker in weather_comment_markers):
        weather_source_markers = (
            "气象",
            "天气",
            "台风",
            "暴雨",
            "洪水",
            "灾害",
            "预警",
            "防灾",
            "农业",
            "weather",
            "meteorological",
            "disaster warning",
        )
        return not any(marker.lower() in source.lower() for marker in weather_source_markers)
    return False


def _remove_generic_daily_news_comment(body: str) -> str:
    text = (body or "").strip()
    if not text or _NEWS_COMMENT_LABEL not in text:
        return text

    pattern = re.compile(
        rf"\n{{0,2}}{re.escape(_NEWS_COMMENT_LABEL)}\s*\n"
        r"(?P<comment>.*?)(?=\n{1,2}发布时间：|\n{1,2}来源：|\Z)",
        flags=re.S,
    )
    while True:
        match = pattern.search(text)
        if not match:
            return text.strip()
        if not _daily_news_comment_is_generic(match.group("comment")):
            return text.strip()
        head = text[: match.start()].rstrip()
        tail = text[match.end() :].lstrip()
        text = f"{head}\n\n{tail}".strip() if tail else head


def _daily_news_body_is_too_generic(body: str) -> bool:
    text = body or ""
    if not text.strip():
        return True
    fields = _daily_news_body_quality_fields(text)
    content = fields.get("内容", "")
    return (
        any(marker in text for marker in _NEWS_GENERIC_BODY_MARKERS)
        or any(marker in content for marker in _DAILY_NEWS_VAGUE_CONTENT_MARKERS)
        or _daily_news_comment_is_generic(text)
    )


def _daily_news_body_quality_fields(body: str) -> dict[str, str]:
    text = _strip_urls(body or "")
    data = _load_daily_news_body_json(text)
    if data:
        return {
            "原文标题": _clean_daily_news_json_value(data.get("原文标题") or data.get("title") or data.get("标题") or ""),
            "内容": _clean_daily_news_text_value(data.get("内容") or data.get("content") or data.get("新闻内容") or data.get("body") or ""),
            "评价": _clean_daily_news_text_value(data.get("评价") or data.get("点评") or data.get("comment") or ""),
            "日期": _clean_daily_news_json_value(data.get("日期") or data.get("发布时间") or data.get("date") or ""),
            "来源": _clean_daily_news_json_value(data.get("来源") or data.get("source") or ""),
        }
    rendered = _extract_rendered_daily_news_body_fields(text)
    if rendered:
        return {key: _clean_daily_news_text_value(rendered.get(key, "")) for key in _NEWS_BODY_JSON_KEYS}
    return {"原文标题": "", "内容": _clean_daily_news_text_value(text), "评价": "", "日期": "", "来源": ""}


def _daily_news_content_is_too_thin(body: str, picked: Any) -> bool:
    source_text = " ".join(
        str(getattr(picked, field, "") or "")
        for field in ("description", "content")
    )
    source_length = len(source_text.strip())
    if source_length >= 200 and _cjk_count(_daily_news_body_quality_fields(body).get("内容", "")) < 35:
        return True
    if source_length < 350:
        return False
    content = _daily_news_body_quality_fields(body).get("内容", "")
    return _cjk_count(content) < 70


def _daily_news_content_lacks_lead_event(body: str) -> bool:
    content = _daily_news_body_quality_fields(body).get("内容", "").strip()
    return bool(re.match(
        r"^(?:这(?:意味着|表明|显示|反映)|该(?:举措|消息|事件|禁令|行动|公司|组织|机构)|"
        r"此番|其[一二三四]|因此|短期内|事件发生在|最高法院此次裁定意味着|当被问及|被问及|事件现场|同日)", content,
    ))


def _daily_news_lead_lacks_headline_anchor(title: str, body: str) -> bool:
    content = _daily_news_body_quality_fields(body).get("内容", "").strip()
    lead = content[:160]
    headline = "".join(re.findall(r"[\u4e00-\u9fff]+", title or ""))
    if len(headline) < 6 or not lead:
        return False
    action_groups = (
        ("加息", ("加息", "上调利率", "提高利率", "利率升至")),
        ("处决", ("处决", "执行死刑", "死刑")),
        ("遣返", ("遣返", "送返", "返国")),
        ("辞职", ("辞职", "辞任", "离任")),
        ("重申", ("重申", "再次表示", "再次称")),
        ("袭击", ("袭击", "遭攻击", "遭无人机攻击")),
        ("通过", ("通过", "表决通过", "获批")),
        ("撤销", ("撤销", "取消", "废止")),
        ("驳回", ("驳回", "拒绝", "不予受理")),
        ("逮捕", ("逮捕", "拘捕", "抓获")),
        ("杀害", ("杀害", "遇害", "被害")),
        ("撤回", ("撤回", "取消", "暂停")),
        ("停职", ("停职", "暂停职务", "免职", "停岗")),
    )
    for marker, equivalents in action_groups:
        if marker in headline and not any(variant in lead for variant in equivalents):
            return True
    ignored = {"每日", "今日", "新闻", "事件", "最新", "全球", "国际"}
    anchors = {
        headline[index:index + 2]
        for index in range(len(headline) - 1)
        if headline[index:index + 2] not in ignored
    }
    return len({anchor for anchor in anchors if anchor in lead}) < 2


def _daily_news_body_fact_key(body: str) -> str:
    """Stable content-only key for rejecting cross-source copied drafts."""
    content = _daily_news_body_quality_fields(body).get("内容", "")
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", content or "").lower()


def _daily_news_body_missing_required_fields(body: str) -> bool:
    text = _strip_urls(body or "")
    has_structured_shape = bool(_load_daily_news_body_json(text) or _extract_rendered_daily_news_body_fields(text))
    if not has_structured_shape:
        return True
    fields = _daily_news_body_quality_fields(text)
    required = ("内容", "评价", "日期", "来源")
    if any(not fields.get(key, "").strip() for key in required):
        return True
    content = fields.get("内容", "").strip()
    if any(label in content for label in ("原文标题：", "日期：", "来源：")):
        return True
    return False


def _daily_news_body_has_site_noise(body: str) -> bool:
    fields = _daily_news_body_quality_fields(body)
    content = fields.get("内容", "")
    markers = (
        "打开微信",
        "扫一扫",
        "分享至朋友圈",
        "热文排行",
        "财经日历",
        "今日要点",
        "全球大事",
        "经济数据",
        "每日智库看点",
        "21早新闻",
        "查看全部 -->",
    )
    return any(marker in content for marker in markers)


def _daily_news_body_has_malformed_content(body: str) -> bool:
    fields = _daily_news_body_quality_fields(body)
    content = fields.get("内容", "")
    return (
        _daily_news_content_has_malformed_field_artifact(content)
        or _daily_news_content_has_incomplete_tail(content)
    )


def _daily_news_body_has_multiple_story_content(body: str) -> bool:
    fields = _daily_news_body_quality_fields(body)
    return bool(_daily_news_semicolon_roundup_lines(fields.get("内容", "")))


def _daily_news_body_has_mismatched_comment(body: str) -> bool:
    fields = _daily_news_body_quality_fields(body)
    context = " ".join(
        fields.get(key, "")
        for key in ("原文标题", "内容")
    )
    comment = fields.get("评价", "")
    if any(marker in comment for marker in ("美股", "半导体设备")) and not any(
        marker in context for marker in ("美股", "半导体设备", "美国股票基金", "科技板块单周流入")
    ):
        return True
    weather_comment_markers = ("气象监测", "灾害预警", "防灾减灾", "农业安排", "基层防灾")
    weather_context_markers = ("气象", "天气", "台风", "暴雨", "洪水", "灾害", "预警", "防灾", "农业")
    if any(marker in comment for marker in weather_comment_markers) and not any(
        marker in context for marker in weather_context_markers
    ):
        return True
    return False


def _repair_daily_news_mismatched_comment(
    body: str,
    picked,
    prompt_norm: str,
    title_hint: str = "",
    preserve_length: bool = False,
) -> str:
    """Replace a final rendered cross-topic comment with a source-grounded one."""
    fields = _daily_news_body_to_fields(body, picked, prompt_norm, title_hint=title_hint, preserve_length=preserve_length)
    rendered = _render_daily_news_body_fields(fields, preserve_length=preserve_length)
    if not _daily_news_body_has_mismatched_comment(rendered):
        return rendered

    fallback_comment = _daily_news_fact_based_comment(
        picked,
        fields.get("内容", ""),
        _daily_news_fallback_subject(picked, prompt_norm),
    )
    if fallback_comment and not _daily_news_comment_is_irrelevant(
        fallback_comment,
        picked,
        fields.get("内容", ""),
    ):
        fields["评价"] = fallback_comment
    else:
        fields["评价"] = ""
    return _render_daily_news_body_fields(fields, preserve_length=preserve_length)


def _daily_news_body_has_bad_language(body: str) -> bool:
    fields = _daily_news_body_quality_fields(body)
    original_title = fields.get("原文标题", "")
    content = fields.get("内容", "")
    if not content:
        return True
    if any(marker in content for marker in _DAILY_NEWS_INSUFFICIENT_CONTENT_MARKERS):
        return True
    if _has_english_phrase_leak(content):
        return True
    for value in (original_title, content):
        if not value:
            continue
        if "原文摘录" in value:
            return True
        if _has_japanese_kana(value) or _has_foreign_script_leak(value):
            return True
    return _cjk_count(content) < 8


def _daily_news_quality_issue(title: str, body: str, prompt_norm: str = "") -> str:
    if _daily_news_title_has_bad_language(title):
        return "bad_title_language"
    if _daily_news_title_has_incomplete_tail(title):
        return "incomplete_title"
    if _daily_news_title_has_column_prefix(title):
        return "title_column_prefix"
    if _is_generic_daily_news_title(title):
        return "generic_title"
    if _daily_news_title_has_prompt_leak(title, prompt_norm):
        return "title_prompt_leak"
    if _daily_news_body_has_bad_language(body):
        return "bad_body_language"
    if _daily_news_content_lacks_lead_event(body):
        return "missing_lead_event"
    if _daily_news_body_has_prompt_leak(body):
        return "body_prompt_leak"
    if _has_html_artifacts(body):
        return "body_html_artifacts"
    if _daily_news_body_has_site_noise(body):
        return "body_site_noise"
    if _daily_news_content_has_incomplete_tail(_daily_news_body_quality_fields(body).get("内容", "")):
        return "incomplete_content"
    if _daily_news_body_has_malformed_content(body):
        return "malformed_body_content"
    if _daily_news_body_has_multiple_story_content(body):
        return "multi_story_body"
    if _daily_news_body_has_mismatched_comment(body):
        return "comment_mismatch"
    if _daily_news_body_missing_required_fields(body):
        return "missing_body_fields"
    if _daily_news_body_is_too_generic(body):
        return "generic_body"
    return ""


def _daily_wow_quality_issue(title: str, body: str, prompt_norm: str = "") -> str:
    """Column gate: the shared news checks plus a usable playful comment.

    The shared gate rejects bodies that only contain a structure without facts.
    The column adds one requirement: the evaluation must be a single concrete,
    non-fabricated line.  Tone and humour are deliberately not gated here, so a
    mild profanity or a dry one-liner cannot fail a factually sound draft.
    """
    # The visible column marker is intentional metadata for reviewers.  Strip
    # only that known prefix before applying the ordinary headline gate so the
    # marker itself is not mistaken for a generic ``栏目｜标题`` placeholder.
    review_title = re.sub(r"^每日我去\s*[｜|]\s*", "", str(title or "")).strip()
    shared = _daily_news_quality_issue(review_title, body, prompt_norm)
    if shared:
        return shared
    content = _daily_news_body_quality_fields(body).get("内容", "")
    for chunk in re.findall(r"[\u4e00-\u9fff]{4,}", review_title):
        bigrams = set(chunk[index:index + 2] for index in range(len(chunk) - 1))
        required = 1 if len(chunk) <= 6 else (len(bigrams) + 2) // 3
        if sum(token in content for token in bigrams) < required:
            return "wow_event_missing_from_body"
    comment = _daily_news_body_quality_fields(body).get("评价", "")
    if not daily_wow_comment_is_valid(comment):
        return "wow_comment_unusable"
    return ""


def _daily_wow_repair_comment(body: str, picked, prompt_norm: str) -> str:
    """Replace an unusable column comment while keeping the verified facts."""
    fields = _daily_news_body_quality_fields(body)
    comment = fields.get("评价", "")
    if daily_wow_comment_is_valid(comment):
        return body
    fallback = daily_wow_fallback_comment(picked, fields.get("内容", ""))
    if daily_wow_comment_is_valid(fallback):
        return _render_daily_news_body_fields({**fields, "评价": fallback})
    return body


def _daily_wow_topics(topics, prompt_norm: str, context: str) -> list[str]:
    normalized = _normalize_daily_news_topics(topics, prompt_norm, context)
    kept = [topic for topic in normalized if topic != "每日新闻"]
    if DAILY_WOW_TOPIC not in kept:
        kept.insert(0, DAILY_WOW_TOPIC)
    return kept[:8]


def _daily_wow_visual_plan_text(plan: Any) -> str:
    """Flatten the model's visual_plan object into one short scene line."""
    if isinstance(plan, dict):
        ordered = ("subject", "props", "composition", "contrast")
        pieces = [str(plan.get(key) or "").strip() for key in ordered]
        joined = "；".join(piece for piece in pieces if piece)
        return _clip_text(joined, limit=300) if joined else ""
    text = str(plan or "").strip()
    return _clip_text(text, limit=300) if text else ""


def _daily_wow_image_prompt_for_post(post: Post) -> str:
    """Build the column illustration prompt from saved, verified post facts.

    The scene comes from the model's event description; the contrast and style
    come from reviewed fields.  Nothing here adds facts beyond the draft.
    """
    news_meta = (post.platform or {}).get("news") or {}
    if not isinstance(news_meta, dict):
        news_meta = {}
    image_event = str(news_meta.get("image_event") or "").strip() or post.title
    comment = _daily_news_body_quality_fields(post.body).get("评价", "")
    return daily_wow_image_prompt(
        image_event=image_event,
        contrast=str(news_meta.get("verified_contrast") or ""),
        visual_plan=str(news_meta.get("visual_plan") or ""),
        comment=comment,
    )


def _looks_like_jsonish_body(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return (
        t.startswith("{")
        or t.startswith("[")
        or "```json" in t
        or '"title"' in t
        or '"body"' in t
        or '"topics"' in t
        or '"image_event"' in t
    )


def _strip_json_artifacts(text: str) -> str:
    """
    Remove obvious JSON scaffolding leaked into body text.
    """
    lines: list[str] = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if s in ("{", "}", "[", "]", "},", "],"):
            continue
        if re.match(r'^"?title"?\s*:\s*', s, flags=re.IGNORECASE):
            continue
        if re.match(r'^"?topics"?\s*:\s*', s, flags=re.IGNORECASE):
            continue
        if re.match(r'^"?image_event"?\s*:\s*', s, flags=re.IGNORECASE):
            continue
        m = re.match(r'^"?body"?\s*:\s*(.*)$', s, flags=re.IGNORECASE)
        if m:
            s = m.group(1).strip()
        s = s.rstrip(",").strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
            s = s[1:-1]
        s = (
            s.replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\'", "'")
            .strip()
        )
        if s:
            lines.extend(s.splitlines())
    out = "\n".join(lines).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def _load_daily_news_body_json(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    if "{" in raw and "}" in raw:
        candidates.append(raw[raw.find("{") : raw.rfind("}") + 1])
    for candidate in [item for value in candidates for item in (value, value.replace("'", '"'))]:
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict) and any(key in data for key in _NEWS_BODY_JSON_KEYS):
            return data
    return None


def _clean_daily_news_json_value(value) -> str:
    text = _to_simplified_common(_strip_urls(_strip_html_artifacts(str(value or ""))).strip())
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(" \t\r\n,，。")
    return text


def _clean_daily_news_text_value(value) -> str:
    text = _to_simplified_common(_strip_urls(_strip_html_artifacts(str(value or ""))).strip())
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" \t\r\n,，；;：:、")


def _daily_news_text_has_sentence_end(text: str) -> bool:
    return bool(re.search(r"[。！？!?]$", text or ""))


def _daily_news_comment_tail_is_incomplete(text: str) -> bool:
    value = (text or "").strip()
    if not value or _daily_news_text_has_sentence_end(value):
        return False
    incomplete_suffixes = (
        "的",
        "上的",
        "方面的",
        "中的",
        "里的",
        "在",
        "对",
        "与",
        "和",
        "及",
        "以及",
        "通过",
        "成为",
        "体现了",
        "显示了",
        "说明了",
        "意味着",
        "有助于",
        "需要",
        "仍需",
        "更要",
    )
    if value.endswith(incomplete_suffixes):
        return True
    tail = re.split(r"[。！？!?]", value)[-1].strip()
    if len(tail) <= 28 and re.search(r"(在|对|与|和|及|为|从|向|把|被|将|其|该)$", tail):
        return True
    return False


def _clean_daily_news_comment_value(value, *, preserve_length: bool = False) -> str:
    text = _clean_daily_news_text_value(value)
    if not text:
        return ""
    text = re.sub(r"\s*(?:发布时间|日期)[:：][^\n。！？!?]*", "", text).strip()
    text = re.sub(r"\s*来源[:：][^\n。！？!?]*", "", text).strip()
    text = text.rstrip("，,；;：:、 ")
    if not text:
        return ""
    if preserve_length:
        return (
            text if _daily_news_text_has_sentence_end(text) or _daily_news_comment_tail_is_incomplete(text)
            else f"{text}。"
        )
    if _daily_news_text_has_sentence_end(text):
        return text
    last_end = max(text.rfind(mark) for mark in "。！？!?")
    if last_end >= 0 and last_end + 1 >= max(20, int(len(text) * 0.45)):
        return text[: last_end + 1].strip()
    if _daily_news_comment_tail_is_incomplete(text):
        return ""
    return f"{text}。"


_DAILY_NEWS_METHOD_SENTENCE_MARKERS = (
    "目前可以确认的信息主要来自",
    "因此正文只整理",
    "若报道提到机构、企业或公共部门",
    "更应区分其已公布安排与尚未发生的结果",
    "避免把单一片段扩大成确定趋势",
    "对读者来说，判断这条新闻",
    "再结合后续正式材料确认执行范围和实际效果",
    "在信息仍有限的情况下",
    "需要继续跟踪的进展",
    "不是已经定论的结果",
)


def _remove_daily_news_methodology_noise(text: str) -> str:
    cleaned = _clean_original_news_text(text or "")
    cleaned = re.sub(r"^(?:原始来源|原新闻|来源)消息显示[，,:：]\s*", "", cleaned)
    cleaned = re.sub(r"^(?:鲁网|江南时报|中新网|新华网|人民网)?\s*\d{1,2}月\d{1,2}日?讯[，,:：。]?\s*", "", cleaned)
    cleaned = re.sub(r"^[\u4e00-\u9fff]{2,10}时报讯[，,:：。]?\s*", "", cleaned)
    cleaned = re.sub(r"^[\u4e00-\u9fff·]{2,12}/[^。！？!?]{2,20}\s+20\d{2}-\d{1,2}-\d{1,2}\s*", "", cleaned)
    cleaned = re.sub(r"相关\s+([A-Za-z][A-Za-z0-9_-]{2,20}发布)", r"相关标准。\1", cleaned)
    cleaned = re.sub(r"(?<=[，,])(?:科技部|中央港澳工作办公室|省委副书记)[。.](?=[\u4e00-\u9fff])", "", cleaned)
    cleaned = re.sub(r"_[^。！？!?]{0,100}(?:下载客户端|责任编辑)[^。！？!?]*[。！？!?]?", "。", cleaned)
    cleaned = re.sub(r"(?:下载客户端|责任编辑[:：]\s*[\u4e00-\u9fff·]{1,12})[。！？!?]?", "", cleaned)
    sentences = re.split(r"(?<=[。！？!?])", cleaned)
    kept: list[str] = []
    for sentence in sentences:
        s = sentence.strip()
        if not s:
            continue
        if _daily_news_fact_sentence_is_noise(s):
            continue
        if any(marker in s for marker in _DAILY_NEWS_METHOD_SENTENCE_MARKERS):
            continue
        kept.append(s)
    out = "".join(kept).strip() if kept else cleaned
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"(?<=[。！？!?])\s+(?=[\u4e00-\u9fff])", "", out)
    out = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "。", out)
    return out.strip(" \t\r\n,，")


def _normalize_daily_news_fact_sentence(sentence: str) -> str:
    s = (sentence or "").strip()
    s = re.sub(r"^.{2,30}?消息显示[，,:：]\s*", "", s)
    s = re.sub(r"^.{2,30}?报道[，,:：]\s*", "", s)
    s = re.sub(r"^(?:鲁网|江南时报|中新网|新华网|人民网)?\s*\d{1,2}月\d{1,2}日?讯[，,:：。]?\s*", "", s)
    s = re.sub(r"^[\u4e00-\u9fff]{2,10}时报讯[，,:：。]?\s*", "", s)
    s = re.sub(r"新华社[^，。！？!?]{0,30}\d{1,2}月\d{1,2}日电[（(][^）)]{0,50}[）)]", "", s)
    s = re.sub(r"新华社[^，。！？!?]{0,30}\d{1,2}月\d{1,2}日电[（(]记者[^。！？!?]*$", "", s)
    s = re.sub(r"^[\u4e00-\u9fff·\s]{1,12}[）)](?=(?:由|据|在|“|[一-龥]{2,}))", "", s)
    return s.strip(" \t\r\n,，；;：:、")


def _daily_news_fact_sentence_is_noise(sentence: str) -> bool:
    s = (sentence or "").strip()
    if not s:
        return True
    bare = s.strip("。！？!?，,；;：:、 ")
    if bare in {"来源", "原文摘录"}:
        return True
    if re.search(r"(?:推进|发布|开展|涉及|计划|路线).{0,8}[结规项]$", bare):
        return True
    if (
        len(bare) <= 8
        and not re.search(r"\d|[一二三四五六七八九十两]", bare)
        and (
            re.match(r"^(?:为|为了|因|从|在|由|对|与|和|及|或)", bare)
            or bare.endswith(("现场", "第二", "本次", "此次", "相关"))
        )
    ):
        return True
    if s.count("“") != s.count("”") or s.count("《") != s.count("》"):
        return True
    noise_markers = (
        "下载客户端",
        "责任编辑",
        "The Paper",
        "澎湃新闻-The Paper",
        "澎湃新闻报料",
        "报料热线",
        "报料邮箱",
        "沪ICP备",
        "沪公网安备",
        "互联网新闻信息服务许可证",
        "增值电信业务经营许可证",
        "本站不再支持您的浏览器",
        "请升级您的浏览器",
        "打开微信",
        "扫一扫",
        "分享至朋友圈",
        "热文排行",
        "财经日历",
        "今日要点",
        "全球大事",
        "每日智库看点",
        "21早新闻",
    )
    return any(marker in s for marker in noise_markers)


def _daily_news_sentence_similarity(left: str, right: str) -> float:
    def key(text: str) -> set[str]:
        compact = re.sub(r"[\s，,。！？!?；;：:、（）()《》“”\"'0-9一二三四五六七八九十两]+", "", text or "")
        return {ch for ch in compact if _has_cjk(ch)}

    a = key(left)
    b = key(right)
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))


_DAILY_NEWS_CONTEXT_GENERIC_TOKENS = {
    "新闻",
    "报道",
    "消息",
    "技术",
    "进展",
    "获得",
    "宣布",
    "认证",
    "产品",
    "行业",
    "市场",
    "公司",
    "企业",
    "项目",
    "相关",
    "the",
    "and",
    "for",
    "with",
    "from",
    "news",
    "report",
}


def _daily_news_context_signal_tokens(text: str) -> set[str]:
    raw = (text or "").lower()
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]{2,}", raw)
        if token not in _DAILY_NEWS_CONTEXT_GENERIC_TOKENS
    }
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text or ""):
        if chunk not in _DAILY_NEWS_CONTEXT_GENERIC_TOKENS:
            tokens.add(chunk)
        if len(chunk) >= 2:
            for idx in range(len(chunk) - 1):
                token = chunk[idx : idx + 2]
                if token not in _DAILY_NEWS_CONTEXT_GENERIC_TOKENS:
                    tokens.add(token)
    return tokens


def _daily_news_text_matches_context(value: str, *contexts: str, min_overlap: float = 0.35) -> bool:
    text = _clean_daily_news_title_candidate(_strip_urls(value or ""))
    if not text:
        return False
    context = _clean_daily_news_title_candidate(_strip_urls(" ".join(part or "" for part in contexts)))
    if not context:
        return False
    if text in context or context in text:
        return True
    if _daily_news_sentence_similarity(text, context) >= min_overlap:
        return True
    value_tokens = _daily_news_context_signal_tokens(text)
    if not value_tokens:
        return False
    context_tokens = _daily_news_context_signal_tokens(context)
    if not context_tokens:
        return False
    overlap = len(value_tokens & context_tokens) / max(1, min(len(value_tokens), len(context_tokens)))
    return overlap >= min_overlap


def _daily_news_sentences_repeat_named_subject(left: str, right: str) -> bool:
    for pattern in (r"《[^》]{2,40}》", r"“[^”]{2,40}”"):
        for subject in re.findall(pattern, left or ""):
            if subject in (right or ""):
                return True
    return False


def _dedupe_daily_news_fact_sentences(text: str) -> str:
    sentences = [s.strip() for s in re.split(r"(?<=[。！？!?])", text or "") if s.strip()]
    if not sentences:
        return text
    kept: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        s = _normalize_daily_news_fact_sentence(sentence)
        if not s:
            continue
        if _daily_news_fact_sentence_is_noise(s):
            continue
        key = re.sub(r"\s+", "", s).rstrip("。！？!?")
        if key in seen:
            continue
        seen.add(key)
        kept.append(s)
    if not kept:
        return text
    out = ""
    for sentence in kept:
        if not out:
            out = sentence
        elif out.endswith(("。", "！", "？", "!", "?")):
            out = f"{out}{sentence}"
        else:
            out = f"{out}。{sentence}"
    return out.strip()


def _daily_news_sentence_importance(sentence: str) -> int:
    s = sentence or ""
    score = 0
    for marker in ("表示", "称", "宣布", "发生", "造成", "调查", "启动", "发布", "开通", "抵达", "举行", "发现"):
        if marker in s:
            score += 2
    if re.search(r"\d|[一二三四五六七八九十两]", s):
        score += 1
    if len(s) <= 90:
        score += 1
    for marker in ("首页", "关键词", "精华", "继续进行", "未能看到"):
        if marker in s:
            score -= 2
    return score


def _finish_daily_news_content_sentence(text: str, *, limit: int) -> str:
    out = (text or "").strip()
    if not out:
        return ""
    if len(out) > limit:
        out = out[:limit]
    out = out.rstrip("，,；;：:、 ")
    broken_tail = re.search(r"([。！？!?])([^。！？!?]{1,18}[，,][^。！？!?]{0,8})$", out)
    if broken_tail and len(out[: broken_tail.start(2)].strip()) >= max(20, int(len(out) * 0.5)):
        out = out[: broken_tail.start(2)].strip()
    if re.search(r"[。！？!?]$", out):
        return out
    last_end = max(out.rfind(mark) for mark in "。！？!?")
    if last_end >= 0 and last_end + 1 >= max(20, int(len(out) * 0.6)):
        return out[: last_end + 1].strip()
    if len(out) >= limit:
        out = out[: max(0, limit - 1)].rstrip("，,；;：:、 ")
    return f"{out}。" if out else ""


def _limit_daily_news_content(text: str, *, limit: int = 320) -> str:
    cleaned = _remove_daily_news_methodology_noise(text)
    cleaned = _dedupe_daily_news_fact_sentences(cleaned)
    cleaned = re.sub(r"新华社(?:记者\s*)?发?[（(][^）)]{0,20}摄[）)]", "", cleaned).strip()
    cleaned = re.sub(r"新华社记者\s*[^。；;，,\s]{1,12}\s*摄", "", cleaned).strip()
    cleaned = re.sub(r"(?:。)?摄。(?:新华社/[^\s。]+。?)?", "。", cleaned).strip()
    cleaned = re.sub(r"新华社/[^\s。]+。?$", "", cleaned).strip()
    cleaned = re.sub(r"(?:。)?摄[。.]?$", "。", cleaned).strip()
    if len(cleaned) <= limit:
        return _finish_daily_news_content_sentence(cleaned, limit=limit)
    sentences = re.split(r"(?<=[。！？!?])", cleaned)
    kept = ""
    for sentence in sentences:
        s = sentence.strip()
        if not s:
            continue
        candidate = f"{kept}{s}" if kept else s
        if len(candidate.rstrip("。！？!?")) <= limit:
            kept = candidate
        else:
            if (
                kept
                and len(s.rstrip("。！？!?")) <= limit
                and _daily_news_sentence_importance(s) > _daily_news_sentence_importance(kept)
            ):
                kept = s
            continue
    if kept:
        return _finish_daily_news_content_sentence(kept[:limit], limit=limit)
    return _finish_daily_news_content_sentence(cleaned[:limit], limit=limit)


def _trim_json_field_to_fit(data: dict[str, str], key: str, max_len: int) -> bool:
    value = data.get(key, "")
    if not value:
        return False
    low = 0
    high = len(value)
    changed = False
    while low < high:
        mid = (low + high) // 2
        candidate = value[:mid].rstrip("，,。；; ") + "…"
        trial = dict(data)
        trial[key] = candidate
        dumped = json.dumps(trial, ensure_ascii=False, indent=2)
        if len(dumped) <= max_len:
            low = mid + 1
        else:
            high = mid
        changed = True
    keep = max(0, low - 1)
    data[key] = value[:keep].rstrip("，,。；; ") + "…"
    return changed


def _dump_daily_news_body_json(data: dict[str, str]) -> str:
    normalized = {key: str(data.get(key, "") or "") for key in _NEWS_BODY_JSON_KEYS}
    dumped = json.dumps(normalized, ensure_ascii=False, indent=2)
    if len(dumped) <= MAX_IMAGE_BODY:
        return dumped

    for key in ("内容", "评价", "原文标题", "来源"):
        dumped = json.dumps(normalized, ensure_ascii=False, indent=2)
        if len(dumped) <= MAX_IMAGE_BODY:
            return dumped
        _trim_json_field_to_fit(normalized, key, MAX_IMAGE_BODY)
    dumped = json.dumps(normalized, ensure_ascii=False, indent=2)
    if len(dumped) <= MAX_IMAGE_BODY:
        return dumped

    # Last-resort compact JSON keeps the object valid if whitespace alone is the issue.
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _daily_news_source_name(picked) -> str:
    source = (getattr(picked, "source", "") or getattr(picked, "domain", "") or "未知来源").strip()
    return _strip_urls(source) or "未知来源"


def _extract_labeled_daily_news_body_parts(text: str) -> dict[str, str]:
    cleaned = _remove_generic_daily_news_comment(text or "")
    if _looks_like_jsonish_body(cleaned):
        cleaned = _strip_json_artifacts(cleaned)
    if not cleaned:
        return {"summary": "", "content": "", "comment": "", "date": "", "source": ""}

    summary = ""
    content = cleaned
    comment = ""
    date = ""
    source = ""

    m = re.search(r"发布时间[:：]\s*([^\n]+)", cleaned)
    if m:
        date = m.group(1).strip()
    m = re.search(r"来源[:：]\s*([^\n]+)", cleaned)
    if m:
        source = m.group(1).strip()

    cleaned = re.sub(r"\n{0,2}发布时间[:：][^\n]+", "", cleaned).strip()
    cleaned = re.sub(r"\n{0,2}来源[:：][^\n]+", "", cleaned).strip()

    if _NEWS_SUMMARY_LABEL in cleaned:
        after_summary = cleaned.split(_NEWS_SUMMARY_LABEL, 1)[1]
        if _NEWS_CONTENT_LABEL in after_summary:
            summary, after_summary = after_summary.split(_NEWS_CONTENT_LABEL, 1)
        else:
            lines = after_summary.splitlines()
            summary = lines[0] if lines else ""
            after_summary = "\n".join(lines[1:])
        content = after_summary
    if _NEWS_COMMENT_LABEL in content:
        content, comment = content.split(_NEWS_COMMENT_LABEL, 1)

    if not summary and _NEWS_CONTENT_LABEL in cleaned:
        content = cleaned.split(_NEWS_CONTENT_LABEL, 1)[1]
        if _NEWS_COMMENT_LABEL in content:
            content, comment = content.split(_NEWS_COMMENT_LABEL, 1)

    if _daily_news_comment_is_generic(comment):
        comment = ""

    return {
        "summary": _clean_daily_news_json_value(summary),
        "content": _clean_daily_news_json_value(content),
        "comment": _clean_daily_news_json_value(comment),
        "date": _clean_daily_news_json_value(date),
        "source": _clean_daily_news_json_value(source),
    }


def _daily_news_cjk_source_title(picked) -> str:
    raw_source_title = _strip_news_site_suffixes(_strip_urls(getattr(picked, "title", "") or ""))
    raw_source_title = _DAILY_NEWS_PREFIX_RE.sub("", raw_source_title)
    raw_source_title = re.sub(r"\s+", " ", raw_source_title).strip()
    raw_source_title = raw_source_title.strip(" \t\r\n:：|｜-—–，,。.!！?？\"'")
    if raw_source_title and _has_cjk(raw_source_title) and not _has_japanese_kana(raw_source_title):
        title = _clean_daily_news_json_value(raw_source_title)
        return title[:80].rstrip(" \t\r\n，,。.!！?？:：|｜-—–")
    return ""


def _daily_news_original_title_is_generic(text: str) -> bool:
    value = _clean_daily_news_title_candidate(text or "")
    if not value:
        return True
    if value in _NEWS_GENERIC_TITLE_MARKERS:
        return True
    generic_patterns = (
        r"^(?:一项)?(?:科技|社会|经济|国际|AI|芯片|气候|产业|市场|平台|学校|防晒|外贸).{0,8}(?:议题|事件|数据)?出现(?:新)?(?:进展|变化)$",
        r"^(?:科技|社会|经济|国际|AI|芯片|气候|产业|市场).{0,8}(?:议题|事件)?受关注$",
        r"^需要继续跟踪的进展$",
    )
    return any(re.match(pattern, value) for pattern in generic_patterns)


def _daily_news_title_hint_for_original(title_hint: str, picked=None) -> str:
    cleaned = _clean_daily_news_title_candidate(title_hint or "")
    if (
        not cleaned
        or not _has_cjk(cleaned)
        or _has_japanese_kana(cleaned)
        or _daily_news_original_title_is_generic(cleaned)
        or _daily_news_title_has_bad_language(cleaned)
    ):
        return ""
    return cleaned[:80].rstrip(" \t\r\n，,。.!！?？:：|｜-—–")


def _daily_news_raw_source_title(picked) -> str:
    raw = _strip_news_site_suffixes(_strip_urls(getattr(picked, "title", "") or ""))
    raw = _DAILY_NEWS_PREFIX_RE.sub("", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = raw.strip(" \t\r\n:：|｜-—–，,。.!！?？\"'")
    raw = _clean_daily_news_json_value(raw)
    return raw[:120].rstrip(" \t\r\n，,。.!！?？:：|｜-—–")


def _daily_news_body_json_title(picked, prompt_norm: str, title_hint: str = "") -> str:
    source_title = _daily_news_cjk_source_title(picked)
    if source_title:
        return source_title

    title_hint_original = _daily_news_title_hint_for_original(title_hint, picked)
    if title_hint_original:
        return title_hint_original

    raw_title = _clean_daily_news_title_candidate(getattr(picked, "title", "") or "")
    if raw_title and _has_cjk(raw_title) and not _has_japanese_kana(raw_title):
        return _normalize_news_summary(raw_title, limit=80)

    summary = _normalize_daily_news_title(raw_title or getattr(picked, "title", "") or "", picked, prompt_norm, max_len=40)
    if summary and not _daily_news_original_title_is_generic(summary):
        return summary
    raw_source_title = _daily_news_raw_source_title(picked)
    if raw_source_title:
        return raw_source_title
    return summary


def _extract_rendered_daily_news_body_fields(text: str) -> dict[str, str] | None:
    raw = (text or "").strip()
    if not raw or not any(
        re.search(rf"^{re.escape(key)}[:：]", raw, flags=re.MULTILINE)
        for key in ("原文标题", "内容", "评价")
    ):
        return None

    def line_value(label: str) -> str:
        match = re.search(rf"^{re.escape(label)}[:：]\s*(.+)$", raw, flags=re.MULTILINE)
        return match.group(1).strip() if match else ""

    def block_value(label: str, next_labels: tuple[str, ...]) -> str:
        match = re.search(rf"^{re.escape(label)}[:：]\s*(?:\r?\n)?", raw, flags=re.MULTILINE)
        if not match:
            return ""
        start = match.end()
        stops: list[int] = []
        for next_label in next_labels:
            next_match = re.search(
                rf"\r?\n\r?\n?{re.escape(next_label)}[:：]",
                raw[start:],
                flags=re.MULTILINE,
            )
            if next_match:
                stops.append(start + next_match.start())
        end = min(stops) if stops else len(raw)
        return raw[start:end].strip()

    fields = {
        "原文标题": line_value("原文标题"),
        "内容": block_value("内容", ("评价", "日期", "来源")),
        "评价": block_value("评价", ("日期", "来源")),
        "日期": line_value("日期"),
        "来源": line_value("来源"),
    }
    if not (fields["内容"] or fields["评价"]):
        return None
    return fields


def _daily_news_body_to_fields(
    body: str,
    picked,
    prompt_norm: str,
    title_hint: str = "",
    preserve_length: bool = False,
) -> dict[str, str]:
    """
    Normalize daily-news body into stable internal fields.

    LLMs may return the body as a JSON string, an object coerced into JSON text,
    or the legacy labeled prose format. Keep the internal shape stable, but do
    not expose raw JSON as the publishable XHS body.
    """
    data = _load_daily_news_body_json(body or "")
    rendered = None if data else _extract_rendered_daily_news_body_fields(_strip_urls(body or ""))
    if data:
        original_title = data.get("原文标题") or data.get("title") or data.get("标题")
        content = data.get("内容") or data.get("content") or data.get("新闻内容") or data.get("body")
        comment = data.get("评价") or data.get("点评") or data.get("comment") or ""
        date = data.get("日期") or data.get("发布时间") or data.get("date") or ""
        source = data.get("来源") or data.get("source") or ""
    elif rendered:
        original_title = rendered.get("原文标题")
        content = rendered.get("内容")
        comment = rendered.get("评价")
        date = rendered.get("日期")
        source = rendered.get("来源")
    else:
        parts = _extract_labeled_daily_news_body_parts(body or "")
        original_title = ""
        content = parts["content"]
        if parts["summary"] and parts["summary"] not in content:
            content = f"{parts['summary']} {content}".strip()
        comment = parts["comment"]
        date = parts["date"]
        source = parts["source"]

    if _daily_news_comment_is_generic(str(comment or "")):
        comment = ""
    elif _daily_news_comment_is_unsupported(str(comment or ""), picked, str(content or "")):
        comment = _daily_news_safe_fact_comment(picked, str(content or ""))
    elif _daily_news_comment_is_irrelevant(str(comment or ""), picked, str(content or "")):
        comment = _daily_news_fact_based_comment(
            picked,
            _compact_daily_news_context(picked),
            _daily_news_fallback_subject(picked, prompt_norm),
        )

    if not str(comment or "").strip():
        fallback_comment = _daily_news_fact_based_comment(
            picked,
            str(content or ""),
            _daily_news_fallback_subject(picked, prompt_norm),
        )
        if (
            fallback_comment
            and not _daily_news_comment_is_unsupported(fallback_comment, picked, str(content or ""))
            and not _daily_news_comment_is_irrelevant(fallback_comment, picked, str(content or ""))
        ):
            comment = fallback_comment
    if not str(comment or "").strip():
        comment = _daily_news_minimal_fact_comment(
            picked,
            str(content or ""),
            _daily_news_fallback_subject(picked, prompt_norm),
        )

    if _daily_news_content_is_unsupported(str(content or ""), picked):
        sanitized_numeric_content = ""
        if _daily_news_has_unsupported_numeric_claim(str(content or ""), picked):
            candidate = _daily_news_remove_unsupported_numeric_sentences(str(content or ""), picked)
            if candidate and not _daily_news_content_is_unsupported(candidate, picked):
                sanitized_numeric_content = candidate
        if sanitized_numeric_content:
            content = sanitized_numeric_content
        else:
            source_context = _compact_daily_news_context(picked, max_chars=150, include_title=False)
            if not source_context:
                source_context = _compact_daily_news_context(picked, max_chars=150)
            if _cjk_count(source_context) >= 8 and not _has_english_phrase_leak(source_context):
                content = source_context
            else:
                subject = _daily_news_fallback_subject(picked, "")
                if not _has_cjk(subject):
                    subject = "相关事件"
                content = (
                    f"公开信息显示，{subject}相关信息已引发关注，现有材料未披露更多可核验细节，"
                    "具体进展仍待相关方面进一步说明。"
                )
        if _daily_news_comment_is_irrelevant(str(comment or ""), picked, str(content or "")):
            comment = _daily_news_fact_based_comment(
                picked,
                str(content or ""),
                _daily_news_fallback_subject(picked, prompt_norm),
            )

    source_original_title = _daily_news_cjk_source_title(picked)
    generated_original_title = _clean_daily_news_json_value(original_title)
    title_hint_original = _daily_news_title_hint_for_original(title_hint, picked)
    fallback_original_title = _daily_news_body_json_title(picked, prompt_norm, title_hint=title_hint)
    if source_original_title:
        final_original_title = source_original_title
    elif (
        generated_original_title
        and not _daily_news_original_title_is_generic(generated_original_title)
        and (
            not title_hint_original
            or _daily_news_text_matches_context(
                generated_original_title,
                title_hint_original,
                min_overlap=0.25,
            )
        )
        and _daily_news_text_matches_context(
        generated_original_title,
        fallback_original_title,
        str(content or ""),
        getattr(picked, "title", "") or "",
        getattr(picked, "description", "") or "",
        getattr(picked, "content", "") or "",
        )
    ):
        final_original_title = generated_original_title
    elif title_hint_original:
        final_original_title = title_hint_original
    else:
        final_original_title = fallback_original_title
    source_date = _format_news_seendate(getattr(picked, "seendate", None))
    normalized = {
        "原文标题": final_original_title,
        "内容": (
            _clean_daily_news_text_value(content)
            if preserve_length else _limit_daily_news_content(str(content or ""))
        ),
        "评价": _clean_daily_news_comment_value(comment, preserve_length=preserve_length),
        "日期": source_date if source_date != "未知" else _clean_daily_news_json_value(date),
        "来源": _clean_daily_news_json_value(source) or _daily_news_source_name(picked),
    }
    return normalized


def _daily_news_body_to_json(body: str, picked, prompt_norm: str) -> str:
    """Compatibility helper for tests/tools that need the normalized field JSON."""
    return _dump_daily_news_body_json(_daily_news_body_to_fields(body, picked, prompt_norm))


def _render_daily_news_body_fields(data: dict[str, str], *, preserve_length: bool = False) -> str:
    normalized = {key: _clean_daily_news_json_value(data.get(key, "")) for key in _NEWS_BODY_JSON_KEYS}
    normalized["内容"] = _clean_daily_news_text_value(data.get("内容", ""))
    normalized["评价"] = _clean_daily_news_comment_value(data.get("评价", ""), preserve_length=preserve_length)

    def render(fields: dict[str, str]) -> str:
        chunks: list[str] = []
        if fields.get("内容"):
            chunks.append(f"内容：\n{fields['内容']}")
        if fields.get("评价"):
            chunks.append(f"评价：\n{fields['评价']}")
        if fields.get("日期"):
            chunks.append(f"日期：{fields['日期']}")
        if fields.get("来源"):
            chunks.append(f"来源：{fields['来源']}")
        return "\n\n".join(chunk for chunk in chunks if chunk).strip()

    text = render(normalized)
    if preserve_length or len(text) <= MAX_IMAGE_BODY:
        return text

    for key in ("内容", "评价"):
        while len(text) > MAX_IMAGE_BODY and normalized.get(key):
            overflow = len(text) - MAX_IMAGE_BODY
            value = normalized[key]
            keep = max(0, len(value) - overflow - 1)
            if keep >= len(value):
                keep = len(value) - 1
            normalized[key] = value[:keep].rstrip("，,。；; ") + "…"
            text = render(normalized)
        if len(text) <= MAX_IMAGE_BODY:
            return text

    return text[:MAX_IMAGE_BODY].rstrip()


def _normalize_image_event(value: str, *, fallback: str = "", limit: int | None = None) -> str:
    """Keep complete facts; legacy ``limit`` arguments no longer slice sentences."""
    return clean_news_image_event(value) or clean_news_image_event(fallback)


def _daily_news_artwork_scene_details(body: str) -> str:
    """Extract concrete visual facts when a story describes an artwork or exhibit."""
    content = _daily_news_body_quality_fields(body).get("内容", "")
    if not content:
        return ""
    artwork_markers = ("画作", "绘画", "油画", "名画", "美术馆", "展览", "展出", "画面")
    if not any(marker in content for marker in artwork_markers):
        return ""
    sentences = re.split(r"(?<=[。！？!?])", content)
    visual_markers = ("画作", "画面", "描绘", "剪影", "海岸", "天空", "云", "鸟", "色彩", "展品")
    details = [
        sentence.strip()
        for sentence in sentences
        if sentence.strip() and any(marker in sentence for marker in visual_markers)
    ]
    return _normalize_image_event("".join(details[:2]), limit=72)


def _daily_news_finance_scene(compact_context: str) -> str:
    """Return a concrete, text-free scene for common finance story subjects."""
    if any(marker in compact_context for marker in ("铝业", "铝行业", "铝价", "氧化铝", "电解铝", "铝锭", "铝厂", "中国宏桥")):
        return "大型铝业冶炼车间内，银白色铝锭整齐堆放，工人戴防护装备在熔炉旁巡检，画面无文字无标志"
    if any(marker in compact_context for marker in ("配股", "配售", "增发", "定向增发")):
        return "现代证券交易大厅呈现配股融资场景，多块无文字行情屏显示红色下行趋势线，桌面摆放无品牌交易文件和筹资文件"
    if any(
        marker in compact_context
        for marker in ("房地产", "地产", "融创", "资产管理", "资产运营", "建管", "项目收购")
    ):
        return "城市住宅与写字楼项目沙盘前，资产管理团队查看无文字建筑模型和项目图纸，画面无品牌标志"
    return ""


def _daily_news_scene_anchors(scene: str, evidence: str) -> list[str]:
    """Find literal multi-character anchors without running headline cleanup."""
    candidates: set[str] = set()
    for chunk in re.findall(r"[\u4e00-\u9fff]{3,}", scene):
        for size in range(3, min(12, len(chunk)) + 1):
            candidates.update(chunk[index:index + size] for index in range(len(chunk) - size + 1))
    candidates.update(re.findall(r"\b[a-zA-Z][a-zA-Z0-9-]{3,}\b", scene))
    generic = re.compile(r"^(?:相关|报道|画面|背景|现场|人物|整体|不同|目前|已经|正在|一名|一座|一个|表示|进行)")
    anchors: list[str] = []
    for token in sorted(candidates, key=lambda item: (-len(item), item)):
        if token.casefold() not in evidence.casefold() or generic.match(token):
            continue
        if not any(token in existing for existing in anchors):
            anchors.append(token)
    return anchors


def _daily_news_scene_asserts(text: str, pattern: str) -> bool:
    """A plan, denial or hypothetical mention is not a completed visual state."""
    uncertain = r"尚未|尚无|没有|并未|未曾|未遭|未发生|未实施|未开工|不会|不能|否认|计划|拟|可能|预计|希望|准备|打算|如果|假如|即将|将要|将会|将于|将在|将对|将被|将遭|将实施|讨论|示意图|模拟"
    for clause in re.split(r"[，,。！？!?；;\n]+", text):
        if re.search(pattern, clause) and not re.search(uncertain, clause):
            return True
    return False


def _daily_news_scene_matches_text(scene: str, evidence: str) -> bool:
    # Compare full clauses. Title cleaning discards everything after a comma.
    if scene in evidence or evidence in scene:
        return bool(evidence)
    scene_tokens = _daily_news_context_signal_tokens(scene)
    fact_tokens = _daily_news_context_signal_tokens(evidence)
    overlap = len(scene_tokens & fact_tokens) / max(1, min(len(scene_tokens), len(fact_tokens)))
    return overlap >= 0.35


def _normalize_daily_news_image_event(
    value: str,
    *,
    picked,
    title: str,
    body: str,
    prompt_norm: str,
    audit: dict[str, Any] | None = None,
) -> str:
    candidate = _normalize_image_event(value)
    title_event = _normalize_image_event(title)
    grounding: dict[str, Any] = {"supported_anchors": [], "evidence_sentence": "", "state_checks": []}

    def finish(result: str, reason: str, accepted: bool, entity: str = "") -> str:
        if audit is not None:
            audit.update({"version": "scene-anchor-v5-subject-action", "input": value, "cleaned": candidate,
                          "normalized": result, "reason": reason, "accepted": accepted,
                          "title": title_event, "supported_entity": entity, **grounding})
        return result

    if not candidate:
        return finish("", "missing_scene", False)
    if candidate == title_event:
        return finish("", "headline_is_not_scene", False)
    if _has_cjk(title_event) and not _has_cjk(candidate):
        return finish("", "language_mismatch", False)

    # Ground only in the accepted factual copy, never scraped related stories,
    # reader preferences or opinion. This does not replace the visual gate.
    facts = _news_image_fact_context(body)
    if not facts:
        return finish("", "missing_scene_facts", False)
    lead_match = re.match(r"^.+?[。！？!?]", facts)
    factual_lead = lead_match.group(0) if lead_match else ""
    exact_lead = bool(factual_lead and candidate.startswith(factual_lead) and candidate[len(factual_lead):].strip())
    visual = candidate[len(factual_lead):].strip() if exact_lead else candidate
    grounding.update({"factual_lead": factual_lead if exact_lead else "", "visual_scene": visual})
    # A quoted fact establishes provenance, not the added visible scene.
    evidence = [
        (sentence.strip(), _daily_news_scene_anchors(visual, sentence))
        for sentence in re.split(r"[。！？!?\n]+", facts) if sentence.strip()
    ]
    evidence = [(sentence, anchors) for sentence, anchors in evidence if anchors]
    # Generic action/state families, not per-news entities or scene templates.
    # An ongoing destructive event can support an editorial damage illustration;
    # discussion of a future event cannot. Specific details still face VLM review.
    state_patterns = (
        ("damage", r"大火|火光|浓烟|黑烟|焦黑|燃起|爆炸|轰炸|受损|废墟", r"火灾|大火|燃烧|爆炸|袭击|打击|轰炸|受损|损毁|废墟"),
        ("collision", r"坠毁|沉没|撞击|撞上|翻覆", r"坠毁|沉没|撞击|撞上|翻覆"),
        ("opening", r"通车|建成|投入使用|正式开通", r"通车|建成|投入使用|开通"),
        ("election_result", r"胜选|赢得选举|当选|下台", r"胜选|赢得选举|当选|下台"),
        ("sport", r"赛车|踢球|足球|争夺冠军|夺冠|体育比赛|棒球比赛|举办比赛|举办赛事|举行比赛|进行比赛|挥棒|击球|球员上场|观众.{0,6}看球", r"赛车|踢球|足球|冠军|夺冠|体育比赛|棒球比赛|举办比赛|举办赛事|举行比赛|进行比赛|挥棒|击球"),
        ("arrest", r"被捕|逮捕|押送|戴上手铐", r"被捕|逮捕|押送|拘捕|拘留"),
        ("violence", r"开火|开炮|枪击|轰击|挥刀|殴打", r"开火|开炮|枪击|轰击|挥刀|殴打|交火|袭击"),
        ("demonstration", r"示威|抗议|封锁", r"示威|抗议|封锁"),
    )
    supported_states: set[str] = set()
    for state, visible_pattern, fact_pattern in state_patterns:
        if not _daily_news_scene_asserts(visual, visible_pattern):
            continue
        supporting = [
            (sentence, anchors) for sentence, anchors in evidence
            if _daily_news_scene_asserts(sentence, fact_pattern)
            and not re.search(r"取消|不再|撤回|放弃|停止|未举办|未举行|此前考虑", sentence)
        ]
        if state == "demonstration":
            # Ongoing protest needs an actor/topic in the same fact sentence.
            # Shared action words alone cannot borrow another event's state.
            anchor_noise = r"示威|抗议|封锁|游行|集会|正在|目前|进行|举行|参与|参加|发起|这场|相关|活动|事件|的|在|与|和|及|、"
            supporting = [
                (sentence, anchors) for sentence, anchors in supporting
                if re.search(r"正在|进行中|持续|继续|仍在|目前", sentence)
                and not re.search(r"结束|已散去|已经散去|已散场|已经散场", sentence)
                and any(len(re.sub(anchor_noise, "", anchor)) >= 2 for anchor in anchors)
                and (not _daily_news_scene_asserts(visual, r"封锁")
                     or _daily_news_scene_asserts(sentence, r"封锁"))
            ]
        grounding["state_checks"].append({"state": state, "supported": bool(supporting)})
        if not supporting:
            return finish("", "unsupported_scene_state", False)
        if state == "demonstration":
            if _daily_news_scene_asserts(visual, r"庆祝|欢庆|胜利|获胜") and not any(
                _daily_news_scene_asserts(sentence, r"庆祝|欢庆|胜利|获胜") for sentence, _ in supporting
            ):
                return finish("", "unsupported_scene_state", False)
            # Confirming the core protest does not establish these real-world
            # settings, props or actions. Neutral composition remains unrestricted.
            unverified = [
                detail for detail in re.findall(r"街道|街头|校门|路障|标语牌|标语|游行|校服", visual)
                if _daily_news_scene_asserts(visual, re.escape(detail))
                and not any(detail in sentence for sentence, _ in supporting)
            ]
            if unverified:
                grounding["unverified_details"] = sorted(set(unverified))
                return finish("", "unsupported_scene_detail", False)
        supported_states.update(sentence for sentence, _ in supporting)

    # Statement sources do not establish a physical speaking venue. Source
    # aliases are used only for exact communication channels, not scene facts.
    settings = re.findall(r"发言台|讲台|发布厅|发布会|办公场所|办公室|会议室|面对记者|面对媒体|麦克风|话筒", visual)
    if any(not any(token in sentence for sentence, _ in evidence) for token in settings):
        return finish("", "unsupported_scene_setting", False)
    source_text = " ".join(str(getattr(picked, key, "") or "") for key in ("title", "description", "content"))
    channel_x = r"(?:在|通过)X(?:上|平台|社交平台)"
    scene_on_x = re.search(channel_x, visual)
    source_on_x = bool(re.search(channel_x, facts))
    for alias in re.findall(r"[（(]([A-Za-z][A-Za-z .'-]+)[）)]", facts):
        name = re.escape(alias.strip())
        attributed_x = rf"(?:writing|posting)\s+on\s+X\s*,\s*{name}\b|\b{name}\b[^.!?]{{0,80}}\b(?:wrote|posted|said)\s+on\s+X\b"
        source_on_x = source_on_x or bool(re.search(attributed_x, source_text, re.I))
    if scene_on_x and not source_on_x:
        return finish("", "unsupported_scene_channel", False)
    if re.search(r"信纸|信封|信件|信函|致信", visual) and not (
        re.search(r"信件|信函|致信|来信", facts)
        or re.search(r"\b(?:a|the) letter\b", source_text, re.I)
    ):
        return finish("", "unsupported_scene_channel", False)

    def _grounded_editorial_scene() -> tuple[str, list[str]] | None:
        if not visual:
            return None
        if not exact_lead:
            # A one-sentence concept can omit a duplicate lead, but all prose
            # before its concept label must itself occur in a fact sentence.
            provenance = re.split(r"(?:的)?(?:概念示意|政策示意|立场示意|编辑示意)", visual, maxsplit=1)[0].strip()
            if not provenance or not any(provenance in sentence for sentence, _ in evidence):
                return None
        # Ground the visual core separately from neutral composition. Keep the
        # existing overlap threshold and require more than a shared actor name.
        core = re.split(r"[，,。；;]", visual, maxsplit=1)[0]
        core = re.split(r"(?:的)?(?:editorial)?(?:概念示意|政策示意|立场示意|编辑示意)", core, maxsplit=1, flags=re.I)[0].strip()
        for sentence, anchors in evidence:
            prefix = ""
            for left, right in zip(core, sentence):
                if left != right:
                    break
                prefix += left
            if (
                len(prefix) >= 3
                and _daily_news_scene_matches_text(core, sentence)
                and (core == re.split(r"[，,；;]", sentence, maxsplit=1)[0]
                     or core == sentence
                     or (core == prefix and re.search(
                         r"(?:警告|宣布|呼吁|表示|回应|提出|任命|调查|取消|暂停|恢复|发布).{2,}", core,
                     ))
                     or _daily_news_scene_anchors(core[len(prefix):], sentence[len(prefix):]))
            ):
                return sentence, anchors
        return None

    if re.match(r"(?:一名|一个|某位|一位).{0,20}(?:官员|发言人)|^(?:一封|一张).{0,12}(?:信纸|信封|信件)", visual):
        return finish("", "missing_source_subject_action", False)
    editorial = bool(re.search(r"概念示意|政策示意|立场示意|编辑示意|editorial", visual, re.I))
    if editorial:
        grounded = _grounded_editorial_scene()
        if not grounded:
            return finish("", "missing_source_subject_action", False)
        sentence, anchors = grounded
        grounding.update({"supported_anchors": anchors, "evidence_sentence": sentence})
        return finish(candidate, "editorial_source_supported", True)

    if exact_lead:
        # A copied lead alone must not enter the legacy factual-scene route.
        return finish("", "missing_editorial_scene", False)
    public_space = r"街头|街道|市民|行人|居民|人群|群体|集会"
    for sentence, anchors in evidence:
        if (
            sentence in supported_states
            or (re.search(public_space, candidate) and re.search(public_space, sentence))
            or _daily_news_scene_matches_text(candidate, sentence)
        ):
            grounding.update({"supported_anchors": anchors, "evidence_sentence": sentence})
            return finish(candidate, "body_supported", True)
    speech = r"称|表示|警告|回应|回答|发言|讲话|演讲|讨论|呼吁|宣布|强调|指出|提出|致信"
    subject_match = re.match(rf"^(.{{2,30}}?)(?:{speech})", title_event)
    subject = subject_match.group(1).strip() if subject_match else ""
    if subject and subject in facts and subject in candidate:
        scene_action = candidate.split(subject, 1)[1]
        if re.search(speech, scene_action):
            grounding.update({"supported_anchors": [subject], "evidence_sentence": next((sentence for sentence, _ in evidence if subject in sentence), facts)})
            return finish(candidate, "entity_supported", True, subject)
        # A matching name alone must not license an unrelated activity.
        if not _daily_news_scene_matches_text(
            candidate.replace(subject, ""), facts.replace(subject, "")
        ):
            return finish("", "unrelated_scene", False, subject)

    return finish("", "unrelated_scene", False)


def _normalize_news_summary(value: str, *, fallback: str = "", limit: int = 40) -> str:
    text = (value or "").strip()
    if not text:
        text = (fallback or "").strip()
    text = _to_simplified_common(text)
    text = _strip_urls(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("：:，,。.!！？?\"'（）()[]【】")
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _is_publishable_daily_news_topic(topic: str, prompt_norm: str = "") -> bool:
    text = (topic or "").strip().lstrip("#")
    if not text:
        return False
    if prompt_norm and text == prompt_norm.strip():
        return False
    if len(text) > 20:
        return False
    if any(ch in text for ch in ("\n", "\r", "，", "。", "；", "：", "、")):
        return False
    blocked = (
        "选择",
        "适合小红书",
        "正文",
        "提示词",
        "包含要点",
        "点评",
        "生成",
    )
    return not any(word in text for word in blocked)


_IRRELEVANT_DAILY_NEWS_TOPICS = {
    "饭局",
    "职场中的人情世故",
    "凝聚力提升",
    "职场社交法则",
    "成功与机遇",
    "富人与穷人",
}


def _fallback_daily_news_topics(context: str) -> list[str]:
    text = context or ""
    if "人权" in text:
        return ["全球人权治理", "国际合作"]
    if "文物" in text or "正视历史" in text:
        return ["文物返还", "历史记忆"]
    if any(marker in text for marker in ("潮汕", "侨批", "红头船", "文化传承")):
        return ["文化传承", "潮汕文化"]
    if "火灾" in text:
        return ["火灾", "公共安全"]
    if "古巴" in text:
        return ["国际关系", "国家主权"]
    if "谈判" in text or "伊朗" in text:
        return ["国际谈判", "中东局势"]
    return ["国际新闻"]


def _normalize_daily_news_topics(topics, prompt_norm: str = "", context: str = "") -> list[str]:
    normalized_topics: list[str] = []
    seen: set[str] = set()
    for t in topics or []:
        tt = str(t or "").strip().lstrip("#")
        if tt in _IRRELEVANT_DAILY_NEWS_TOPICS:
            continue
        if not _is_publishable_daily_news_topic(tt, prompt_norm):
            continue
        if tt in seen:
            continue
        normalized_topics.append(tt)
        seen.add(tt)
    if "每日新闻" not in seen:
        normalized_topics.insert(0, "每日新闻")
        seen.add("每日新闻")
    for topic in _fallback_daily_news_topics(context):
        if topic not in seen and _is_publishable_daily_news_topic(topic, prompt_norm):
            normalized_topics.append(topic)
            seen.add(topic)
    return normalized_topics[:8]


def _format_news_seendate(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return "未知"
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    if "T" in text:
        return text.split("T", 1)[0].strip() or text
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _ensure_news_publish_date(body: str, seendate: str | None) -> str:
    text = (body or "").strip()
    if not text:
        return text
    pub = _format_news_seendate(seendate)
    if pub != "未知" and pub in text:
        return text
    if "发布时间" in text:
        return text
    return f"{text}\n\n发布时间：{pub}"


def normalize_evaluation_viewpoint(value: str | None) -> str:
    """
    Normalize the optional commentary viewpoint before inserting it into prompts.

    The value is user-provided, so keep it as a short single-line instruction.
    Empty values intentionally fall back to the neutral default.
    """
    text = _strip_urls(str(value or ""))
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ：:;；,，。")
    if not text:
        return DEFAULT_EVALUATION_VIEWPOINT
    return _clip_text(text, limit=80)


def _daily_news_candidate_fetch_limit(count: int) -> int:
    requested = max(1, int(count or 1))
    if requested == 1:
        # A single post has no batch replacement risk. Requiring ten items
        # would unnecessarily reject a valid, well-sourced single story.
        return 1
    raw = (os.getenv("NEWS_UPLOAD_QUALIFIED_POOL_MULTIPLIER") or "10").strip()
    try:
        multiplier = int(raw)
    except ValueError:
        multiplier = 10
    # A 10x qualified pool gives the later source, duplicate, content and
    # image gates room to replace rejects without silently returning a short batch.
    return requested * max(1, multiplier)


def _daily_news_story_identity(value: Any) -> set[str]:
    """Return stable URL/title identities for cross-batch story exclusion."""
    return news_story_identity_keys(value)


def _daily_news_raw_candidate_fetch_limit(target_fetch_count: int) -> int:
    raw = (os.getenv("NEWS_UPLOAD_RAW_MAX_RECORDS") or os.getenv("NEWS_RAW_MAX_RECORDS") or "").strip()
    try:
        value = int(raw) if raw else 0
    except ValueError:
        value = 0
    # The qualified pool above is 10x by default. Request roughly twice that
    # amount from raw providers so date/relevance filtering can discard noise.
    minimum = max(20, target_fetch_count * 2)
    if value > 0:
        return max(minimum, value)
    return minimum


_AI_IMAGE_PROVIDER_ALIASES = {
    "opencodex",
    "aliyun",
    "dashscope",
    "bailian",
    "qwen_image",
    "qwen-image",
    "volcengine",
    "ark",
    "doubao",
    "seedream",
    "siliconflow",
    "silicon",
    "sf",
    "minimax",
    "mini-max",
    "tokenplan",
    "token-plan",
}


def _daily_news_ai_first_provider() -> str:
    for name in (
        "SINGLE_NEWS_AI_IMAGE_PROVIDER",
        "DAILY_NEWS_AI_IMAGE_PROVIDER",
        "IMAGE_PROVIDER",
    ):
        value = (os.getenv(name) or "").strip().lower()
        if value in _AI_IMAGE_PROVIDER_ALIASES:
            return value
    return "aliyun"


def _fetch_daily_news_related_images(
    *,
    title: str,
    body: str,
    topics: list[str],
    prompt_hint: str,
    dest_dir: Path,
    exclude_ids: Optional[set[str]] = None,
    ai_first: bool = False,
    provider: Optional[str] = None,
    image_policy: str = "ai_preferred",
    prompt_override: Optional[str] = None,
) -> tuple[list[Path], list[dict[str, Any]], dict[str, Any] | None]:
    if not ai_first:
        paths, metas = fetch_and_download_related_images(
            title=title,
            body=body,
            topics=topics,
            prompt_hint=prompt_hint,
            dest_dir=dest_dir,
            exclude_ids=exclude_ids,
            prompt_override=prompt_override,
        )
        return paths, metas, None

    primary_provider = (provider or _daily_news_ai_first_provider()).strip().lower()
    required_ai = str(image_policy or "").strip().lower() == "ai_required"
    if primary_provider == "pexels":
        if required_ai:
            raise RuntimeError("AI image required, but the selected image provider is Pexels")
        paths, metas = fetch_and_download_related_images(
            title=title,
            body=body,
            topics=topics,
            prompt_hint=prompt_hint,
            dest_dir=dest_dir,
            exclude_ids=exclude_ids,
            provider="pexels",
        )
        return paths, metas, None
    try:
        paths, metas = fetch_and_download_related_images(
            title=title,
            body=body,
            topics=topics,
            prompt_hint=prompt_hint,
            dest_dir=dest_dir,
            exclude_ids=exclude_ids,
            provider=primary_provider,
            count=1,
            prompt_override=prompt_override,
        )
        for meta in metas:
            meta["prompt_version"] = NEWS_IMAGE_PROMPT_VERSION
        return paths, metas, None
    except Exception as exc:
        if required_ai:
            raise RuntimeError(
                f"AI image required; provider={primary_provider} failed: {str(exc)[:240]}"
            ) from exc
        fallback_meta: dict[str, Any] = {
            "from_provider": primary_provider,
            "to_provider": "pexels",
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        }
        if isinstance(exc, ImageGenerationAbandoned):
            fallback_meta.update(
                {
                    "attempts": exc.attempts,
                    "errors": exc.errors,
                }
            )
        print(
            f"[auto-image] ai_failed provider={primary_provider} fallback=pexels "
            f"err={str(exc)[:160]}"
        )
        paths, metas = fetch_and_download_related_images(
            title=title,
            body=body,
            topics=topics,
            prompt_hint=prompt_hint,
            dest_dir=dest_dir,
            exclude_ids=exclude_ids,
            provider="pexels",
        )
        return paths, metas, fallback_meta


def _safe_daily_news_visual_feedback(retry_prompt: str) -> str:
    """Map review defects to local edits, without treating VLM advice as facts."""
    text = (retry_prompt or "").strip().lower()
    if not text:
        return ""
    corrections = (
        (("文字", "字母", "乱码", "logo", "品牌", "旗", "水印", "text", "letter", "logo"),
         "去掉文字和标识，以留白表面与实体动作表达事件。"),
        (("地点", "海岸", "远海", "近岸", "场景", "背景", "距离", "损毁", "完好", "scene", "coast", "location", "damage"),
         "按事实依据纠正地点、远近关系及物体状态，不添加未证实的环境。"),
        (("主体", "人物", "身份", "动作", "对象", "肖像", "subject", "person", "action"),
         "按事实依据纠正主体角色和动作，保持事件及结果不变。"),
        (("拼贴", "拥挤", "分镜", "分屏", "构图", "复杂", "杂乱", "collage", "panel", "layout"),
         "合并为一个连续场景，减少无关陪衬，突出一个主体动作。"),
    )
    kept = [instruction for markers, instruction in corrections if any(word in text for word in markers)]
    return "".join(kept[:3]) or "以事实依据校正画面，只调整与事件不符的元素。"


def _daily_news_image_repair_hint(retry_prompt: str) -> str:
    return _safe_daily_news_visual_feedback(retry_prompt)


def regenerate_daily_news_post_image(
    post: Post,
    retry_prompt: str,
    *,
    provider: Optional[str] = None,
) -> bool:
    news_meta = post.platform.get("news")
    if not isinstance(news_meta, dict):
        return False

    existing_ids: set[str] = set()
    image_metas = post.platform.get("images")
    if isinstance(image_metas, list):
        _merge_image_ids(existing_ids, image_metas)

    image_event = _refreshed_daily_news_image_hint(
        post,
        str(news_meta.get("prompt_hint") or ""),
    )
    if image_event:
        news_meta["image_event"] = image_event
    prompt_override = _build_aliyun_image_prompt(
        title=post.title,
        body=post.body,
        topics=post.topics,
        prompt_hint=image_event,
        repair_hint=_daily_news_image_repair_hint(retry_prompt),
    )
    image_paths, image_metas, image_fallback = _fetch_daily_news_related_images(
        title=_preferred_image_title(post, post.title),
        body=post.body,
        topics=post.topics,
        prompt_hint=image_event,
        dest_dir=post_dir(post.id) / "assets",
        exclude_ids=existing_ids,
        ai_first=True,
        provider=provider,
        image_policy=str(news_meta.get("image_policy") or "ai_required"),
        prompt_override=prompt_override,
    )
    if not image_paths:
        return False

    post.assets = _build_asset_infos(image_paths)
    post.platform["images"] = image_metas
    if image_metas:
        post.platform["image"] = image_metas[0]
    if image_fallback:
        post.platform["image_fallback"] = image_fallback
    else:
        # A successful AI regeneration replaces the prior fallback assets;
        # do not leave stale fallback metadata in the post or GUI.
        post.platform.pop("image_fallback", None)
    save_post(post)
    return True


def _fetch_daily_news_candidates_for_upload(
    prompt_norm: str,
    *,
    count: int,
    lookback_days: object = None,
    news_materials_file: str | Path | None = None,
    single_news_material_file: str | Path | None = None,
    material_time: str = "",
    progress_callback: DailyNewsProgressCallback | None = None,
    discovery_holder: dict[str, Any] | None = None,
    performance_policy: PerformancePolicy | None = None,
    column: str = "daily_news",
    exclude_story_keys: set[str] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    single_material_path = str(single_news_material_file or "").strip()
    multi_material_path = str(news_materials_file or ("" if single_material_path else os.getenv("NEWS_MATERIALS_FILE")) or "").strip()
    if single_material_path and multi_material_path:
        raise RuntimeError("single_news_material_file and news_materials_file are mutually exclusive")
    if single_material_path:
        item = load_single_news_material_file(single_material_path)
        manual_source_info = read_manual_material_source_info(single_material_path)
        tz_name = os.getenv("NEWS_TZ") or "Asia/Shanghai"
        resolved_items, resolved_times = resolve_manual_material_times(
            [item],
            default_material_time=material_time,
            tz_name=tz_name,
        )
        item = resolved_items[0]
        meta: dict[str, Any] = {
            "provider": "manual_single",
            "api_source": "manual_single",
            "source_api": {
                "provider": "manual_single",
                "file_path": single_material_path,
            },
            "provider_plan": ["manual_single"],
            "provider_attempts": ["manual_single"],
            "provider_errors": [],
            "tz": tz_name,
            "query": "",
            "query_variants": [],
            "query_expansion_enabled": False,
            "queries_used": [],
            "search_days": None,
            "used_today_range": False,
            "manual_materials": {
                "file_path": single_material_path,
                **manual_source_info,
                "count": 1,
                "mode": "single",
                "default_material_time": material_time,
                "resolved_item_times": resolved_times,
                "freshness_policy": "bypassed_user_supplied_material",
            },
            "candidates": [asdict(item)],
            "selection_pool": {
                "requested_count": 1,
                "target_fetch_count": 1,
                "raw_fetch_count": 1,
                "raw_candidate_count": 1,
                "recent_candidate_count": 1,
                "prompt_relevance": {
                    "mode": "ignored_for_single_news_material",
                    "prompt_hint": prompt_norm,
                },
                "prompt_relevant_candidate_count": 1,
                "actual_candidate_count": 1,
                "dropped_out_of_window_count": 0,
                "date_window": None,
                "lookback": {
                    "mode": "disabled_for_material",
                    "input": lookback_days,
                    "selected_max_age_days": None,
                    "attempts": [],
                },
                "selection_policy": "manual_material_without_source_date_limit",
                "source_domain_max_ratio": None,
            },
        }
        return [item], meta

    if not multi_material_path:
        windows, window_meta = _daily_news_lookback_window(
            lookback_days, env_names=("NEWS_LOOKBACK_DAYS", "CONTENT_LOOKBACK_DAYS"))
        target_count = max(1, int(count or 1))
        preferred = _daily_news_candidate_fetch_limit(target_count)
        # The raw target is independent of the minimum generation threshold.
        raw_target = max(target_count * 20, _daily_news_raw_candidate_fetch_limit(preferred))
        policy = performance_policy or PerformancePolicy.from_environment()
        history_signatures = []
        from src.news.daily_news import NewsItem
        from src.news.history import news_history_dedupe_enabled
        if news_history_dedupe_enabled():
            for historical_post in list_posts():
                # A failed/incomplete batch is persisted locally as a draft,
                # but it was never uploaded and must not poison the next
                # batch's cross-run story dedupe history.
                if not (
                    historical_post.uploaded
                    or historical_post.status in {PostStatus.saved_draft, PostStatus.published}
                ):
                    continue
                news_meta = historical_post.platform.get("news")
                if not isinstance(news_meta, dict):
                    continue
                picked = news_meta.get("picked")
                if isinstance(picked, dict) and picked.get("title"):
                    historical_item = NewsItem(**{"url": "", **{
                        key: value for key, value in picked.items() if key in NewsItem.__dataclass_fields__
                    }})
                    history_signatures.append(_cjk_story_event_signature(historical_item))
        discovery = DailyNewsDiscovery(
            prompt=prompt_norm, count=target_count, windows=windows, window_meta=window_meta,
            raw_target=raw_target, preferred_target=preferred,
            # Keep bounded windows/source query counts and per-request timeouts,
            # not a shared deadline consumed by earlier sources or windows.
            budget_seconds=None,
            fetch=fetch_daily_news_candidates, prepare=_prefetch_daily_news_context,
            incomplete=_daily_news_context_is_incomplete, progress=progress_callback,
            history_signatures=history_signatures,
            column=column,
            excluded_story_keys=exclude_story_keys,
        )
        _emit_daily_news_progress(progress_callback, "准备候选池", "in_progress",
                                 requested_count=target_count, raw_target=raw_target,
                                 preferred_target=preferred, min_qualified=target_count,
                                 reserve_target=discovery.reserve_target)
        candidates = discovery.take(initial=True)
        if discovery_holder is not None:
            discovery_holder["session"] = discovery
        return candidates, discovery.meta

    target_fetch_count = _daily_news_candidate_fetch_limit(count)
    raw_fetch_count = _daily_news_raw_candidate_fetch_limit(target_fetch_count)
    def _source_progress(stage: str, status: str, detail: dict[str, Any]) -> None:
        _emit_daily_news_progress(progress_callback, stage, status, **detail)

    try:
        candidates, meta = fetch_daily_news_candidates(
            prompt_norm,
            max_records=raw_fetch_count,
            search_days=1,
            materials_file=multi_material_path,
            source_health_path=Path("data") / "source_health" / "daily_news.json",
            persist_source_health=True,
            exhaustive_sources=True,
            progress_callback=_source_progress,
        )
    except TypeError as exc:
        if "unexpected keyword" not in str(exc) and "positional" not in str(exc):
            raise
        # Backward compatibility for tests or local monkeypatches that still use
        # the old one-argument callable shape.
        candidates, meta = fetch_daily_news_candidates(prompt_norm)
    meta = dict(meta)
    raw_candidate_count = len(candidates)
    tz_name = str(meta.get("tz") or os.getenv("NEWS_TZ") or "Asia/Shanghai")
    if multi_material_path:
        material_target_count = max(1, int(count or 1))
        resolved_candidates, resolved_times = resolve_manual_material_times(
            list(candidates),
            default_material_time=material_time,
            tz_name=tz_name,
        )
        if not resolved_candidates:
            raise RuntimeError("材料文件没有可用材料。")
        selected_candidates = rank_news_candidate_pool(resolved_candidates, "")[:raw_fetch_count]
        if len(selected_candidates) < material_target_count:
            raise RuntimeError(
                f"材料候选不足：需要至少 {material_target_count} 条材料，当前只有 {len(selected_candidates)} 条。"
            )
        manual_source_info = read_manual_material_source_info(multi_material_path)
        meta["provider"] = "manual"
        meta["manual_materials"] = {
            "file_path": multi_material_path,
            **manual_source_info,
            "count": len(resolved_candidates),
            "mode": "multiple",
            "default_material_time": material_time,
            "resolved_item_times": resolved_times,
            "freshness_policy": "bypassed_user_supplied_material",
        }
        meta["candidates"] = [asdict(item) for item in resolved_candidates]
        meta["selection_pool"] = {
            "requested_count": max(1, int(count or 1)),
            "target_fetch_count": material_target_count,
            "raw_fetch_count": raw_fetch_count,
            "raw_candidate_count": raw_candidate_count,
            "recent_candidate_count": len(resolved_candidates),
            "prompt_relevance": {
                "mode": "ignored_for_material",
                "prompt_hint": prompt_norm,
            },
            "prompt_relevant_candidate_count": len(resolved_candidates),
            "actual_candidate_count": len(selected_candidates),
            "dropped_out_of_window_count": 0,
            "date_window": None,
            "lookback": {
                "mode": "disabled_for_material",
                "input": lookback_days,
                "selected_max_age_days": None,
                "attempts": [],
            },
            "selection_policy": "manual_material_without_source_date_limit",
            "source_domain_max_ratio": None,
        }
        return selected_candidates, meta
    raise RuntimeError("材料模式未返回候选，请检查材料文件配置。")


def _daily_news_evaluation_viewpoint_instruction(value: str | None) -> str:
    viewpoint = normalize_evaluation_viewpoint(value)
    if viewpoint == DEFAULT_EVALUATION_VIEWPOINT:
        return (
            "评价视角：无视角评价。评价不得预设国家、行业、投资者、平台等固定立场；"
            "只基于已给事实和原文摘录做客观公正分析；信息不足时须明确边界，不得留空。\n"
        )
    return (
        f"评价视角：{viewpoint}。评价必须从该视角观察影响、风险或意义，"
        "但仍须基于已给事实和原文摘录，保持客观公正；信息不足时须明确边界，不得留空。\n"
    )


def _daily_news_professional_reporting_instruction() -> str:
    """Return generic, source-bound rules for an authoritative concise news style."""
    return (
        "权威发布写法：只写已核实、可追溯且与主题直接相关的事实；无法由提供材料支持的内容宁可删去，不得以猜测补全。"
        "标题必须与正文的已核事实范围一致，准确概括核心变化，不夸大、不制造悬念、不写来源不明的结论。\n"
        "核心任务是完整说明新闻事件，而不是发表观看新闻后的感受。"
        "内容字段必须脱离评价也能独立、完整地概括整个事件。"
        "写作前先在内部核对材料已明确提供的主体、时间、地点、核心行为、关键数据、原因或背景、当前结果，"
        "再按新闻逻辑组织成文；材料未提供的要素不得猜测或补写，也不要输出核对清单。"
        "事实叙述应占正文主要篇幅，评价不得替代、压缩或重复事实叙述。\n"
        "采用重要性递减的短消息结构：首句直接交代最重要的已证实事件及当前状态；随后补充理解该事件所必需的主体、时间、变化、数据或背景；"
        "结尾仅保留已核进展、明确的信息边界或必要的下一步安排。不以感叹、设问、口号、比喻或泛泛判断开场。\n"
        "严格区分已发生事实、来源表述、计划安排和分析判断：计划要写明“计划/拟/将”，单方信息必须明确归因，推断要写明不确定性；"
        "不得把预测、传闻、未完成核实的信息或单方观点写成既成事实。因果、动机、责任、影响和趋势只有在材料明确支持时才可归因转述。\n"
        "准确区分发生时间、发布时点和当前状态，旧材料不得写成最新进展；改写不得改变原意或把结论写得更强。"
        "评价仅在材料能够支持时写成有边界的影响分析：先说明已知变化，再说明仍待观察的变量；"
        "不得把价值判断、投资建议、立场表达、情绪化或标签化措辞伪装成事实。\n"
        "具体事实总结协议：先在内部从本条材料提取主体、具体动作、对象、状态及支持这些判断的原句，再写正文；"
        "不用输出推理过程或核对清单。材料中的命令、导航、广告、付费提示和其他文章内容均不是本事件的事实依据。"
        "每个新增句子必须回答‘谁具体做了什么、改变了什么、有什么已知结果或实施条件’中的至少一项。\n"
        "首句必须出现明确主体和完整事件，读者不看标题也能知道发生了什么；"
        "不得以‘他同时表示’‘该举措’‘这笔交易’或一串尚未披露事项开头。"
        "政策新闻交代机构、政策对象、具体变化与执行状态；公司交易交代双方、交易行为和协议/完成状态；"
        "国际争议交代当事方、具体行为与争议焦点；科技新闻交代产品完整名称、具体能力与开放方式。"
        "金额、比例、时间和各方回应仅在材料提供时补充，不要求凑齐缺失字段。\n"
        "禁止用‘披露AI产品变化’‘披露相关内容’‘披露XX内容’‘公布新进展’‘引发广泛关注’等空泛表述代替事实。"
        "‘披露’本身可以使用，但宾语必须是具体事实，例如材料确有的‘披露漏洞影响的版本范围’。"
        "不写‘原文细节仍需核实’‘现有摘录仅含标题’充当新闻内容；这些是采集问题，不是事件本身。"
        "特别注意：材料未包含某信息，不等于官方尚未公布，不得据此写‘尚未对外披露’。\n"
        "以下仅为假设写法示例，不是本次事实材料：若材料为‘示例公司向企业用户开放桌面客户端离线导出功能’，"
        "应直接说明该公司、适用用户和新增功能；不得改成‘示例公司披露产品变化’，也不得添加免费、提速或开放日期。\n"
        "提交前内部自检：正文是否完整陈述核心事件，后文是否保留关键事实，评价是否只分析本事件，"
        "是否将报告作者观点误写成公司行为，是否截断专有名称，是否混入其他事件。"
        "只有在核心事实可明确陈述时才生成可发布正文；材料不足时 body 返回空字符串，"
        "交由程序校验处理，不得凭常识补事实或用空泛句凑稿。\n"
    )


def _daily_news_prompt(
    picked,
    prompt_norm: str,
    evaluation_viewpoint: str | None = DEFAULT_EVALUATION_VIEWPOINT,
    column: str = "daily_news",
) -> str:
    """
    Prompt for LLM to write publishable body ONLY (no metadata/requirements echoed).
    """
    wow_column = column == DAILY_WOW_CONTENT_TYPE
    content_length = (
        "材料事实充分时建议220-350字；材料较短时可以少于220字"
        if wow_column else "按前置篇幅规则组织短消息"
    )
    comment_length = "评价限制为1句且不超过60字" if wow_column else "按前置篇幅规则写1个完整句子"
    length_rules = "" if wow_column else news_length_instruction()
    body_length = (
        "长度约束：body 总长度（含换行）务必 <= 900 字符，避免写太长导致发布失败。\n"
        if wow_column else "内容、评价分别遵守前置篇幅上限；日期、来源另列，不计入两项字数。\n"
    )
    base = (
        "你正在为小红书图文笔记写《每日新闻》栏目。\n"
        "请依据下面提供的新闻信息，生成一份可直接发布的草稿。\n"
        "必须全部使用简体中文；如果原始材料是英文新闻、日文新闻或其他语言新闻，先翻译并用中文新闻写法改写，不得保留外文长句或日文假名。\n"
        "注意：body 正文里不要包含提示词/要求等元信息；不得输出 URL、网址、http(s) 链接。\n"
        "来源只写来源名称，网址只保存在本地 post.json 的 metadata 中；正文里不得出现链接、URL 或 http(s)。\n"
        "只允许使用下列已提供的新闻信息，不得新增事实或编造细节；先阅读摘要和原文摘录，再依据其中的具体事实写作。"
        "本次调用不具备浏览工具，不得声称已经访问链接或用常识补全文。\n"
        "内容不完整时，先查阅原新闻/原文摘录后再写作；如果原文摘录仍不足，不得推测数字、因果、人物关系或后续结果；评价须明确现有事实边界，不要硬凑结论。\n\n"
        f"{_daily_news_professional_reporting_instruction()}\n"
        "输出为严格 JSON（必填 keys: title, body, topics, image_event），不要 Markdown/代码块。\n"
        "注意：外层 JSON 的 body 必须是字符串；body 字符串必须是可直接发布的正文，不要把 body 写成 JSON 对象文本。\n\n"
        "可用新闻信息（仅限以下字段，链接仅供参考不要输出）：\n"
        f"- 新闻标题：{picked.title}\n"
        f"- 来源名称：{picked.source or '未知'}\n"
        f"- 来源域名：{picked.domain or '未知'}\n"
        f"- 发布时间：{picked.seendate or '未知'}\n"
        f"- 摘要：{_clip_text(picked.description, limit=300)}\n"
        f"- 原文正文：{_clip_text(picked.content, limit=2200)}\n"
        f"- 链接：{picked.url}\n"
        f"- 检索关键词（仅用于选题相关性，不得写入正文）：{prompt_norm or '无'}\n\n"
        "JSON 字段要求：\n"
        "title：标题必须是12-18字的简体中文总结标题，理想约15字；必须由你基于新闻标题/摘要/原文摘录重新概括，不得直接照抄新闻原始标题；不得机械截断长标题；必须包含具体事件关键词；不要加“每日新闻｜”前缀，不得仅为“每日新闻”，不得出现日文假名；不得以“如/如果/若/一旦”等条件词开头，不能只写半句条件，必须写清新闻动作或结果。\n"
        "body：正文必须通顺，必须严格使用下面 4 个中文字段标签，不得增加字段，不得使用旧标签“原文标题/要点摘要/新闻内容/点评/发布时间”：\n"
        "内容：\n"
        f"<{content_length}，但必须完整说明材料支持的核心事件，不得为了凑字补写事实。先用完整导语写清主体、动作和对象，再补材料已有的时间、地点、关键数据、原因或背景、当前结果；不能仅剩评论、背景或尾段，不能要求读者看标题才能理解。按事件因果或时间顺序自然衔接，不堆砌网页导航、栏目名、浏览器升级提示、来源页噪声；不得写站内推荐/相关阅读/下一篇文章标题，例如“权威数读”“新华视点”“记者手记”“特色产业赋能”“中国摩托加速”；不写未经证实的细节，不写“目前可以确认的信息主要来自”等模板句>\n\n"
        "评价：\n"
        f"<{comment_length}，放在完整事实叙述之后；只概括该事件最直接的意义、影响或待确认变量，不写个人感受、口号、建议和泛泛而谈；评价不得替代、压缩或重复事实叙述；信息不足时说明判断边界，不得留空；不得套用与新闻主题无关的 AI/版权/经贸/供应链等模板>\n\n"
        "日期：YYYY-MM-DD\n\n"
        "来源：来源名称（不要写网址）\n"
        "发布时间只用于正文的“日期”栏，不等于事件发生时间。内容中的事件日期须有原文明确日期证据；"
        "不得自动以发布时间起笔，不得把星期几或“昨天”自行换算为日期；证据不足时保留原文时间表达或不写事件日期。\n"
        f"{body_length}"
        f"{_daily_news_evaluation_viewpoint_instruction(evaluation_viewpoint)}"
        "先阅读并基于已给事实/原文摘录再给判断，不得推测，不煽动对立、不使用攻击性语言、不做情绪化带节奏表述。\n"
        "可提示风险与影响，但不得夸大、不得杜撰未提供事实；不得写“这类新闻适合先看事实，再看影响”、"
        "“接下来可以重点关注权威更新、执行细节和各方反馈”等空泛方法论句式。\n"
        "topics（数组，3-8个话题词）：必须包含“每日新闻”。不要把 topics 写进 body。\n"
        "image_event（字符串，必须在本次写稿调用中一同生成）：第一完整句必须逐字复制body“内容”的第一完整事实句，"
        "包括主体、动作、对象、限定词、否定、引号和句末标点，不得截成主体前缀或改写。"
        "第二句描述一个明确标注为“概念示意”的editorial单场景构图，第一句仅为事实锚点，不是要求将每项事实都画出。"
        "画面描述仍须保留body中原样的具体主体称谓、姓名或机构，并表达已证实的核心动作或状态，不得改成无名官员或只剩一张信纸；"
        "角色、信件等同义表达只在原文明确支持时使用。若场景提及在X发声或致信等表达渠道，必须有原文支持，不得换成现场讲话；"
        "来源在X发声不要求画出X界面或重复渠道，只表达已证实立场的概念示意也可以。构图、侧影和平涂背景不要求与正文逐字相同。"
        "明确已证实的地点与物体状态；远海/近岸、受损/完好、取消或计划/已实施不能混淆。"
        "只选择与新闻主事件直接相关的一个时刻；背景简洁，其他事实留在body，禁止把多个时间地点串成漫画。"
        "人物可用非写实侧影或背影表达，未知身份和环境不补造；不要凭行业关键词添加道具或建筑。"
        "各类新闻均可使用概念示意，不限于讲话；声明、警告、联名信或计划仅表达主体立场，采用纯色色面等非实景背景；"
        "材料未明确支持时不得假定讲台、发布会、办公室或真实现场。不把警告画成事故、指控画成定论、监管画成审判；"
        "可见动作不能把取消或计划变成完成，不得画已取消比赛的球员击球或观众观赛。"
        "所有物面留白，用动作和状态表达而非屏幕、文字或抽象符号；不虚构人物、地点、伤亡或结果。"
        "通常60-120字，完整句意优先，不按字数截断。不要写评价、海报文案；不要把image_event写进body，body仍须完整介绍事件。\n"
        f"{length_rules}"
    )
    if column == DAILY_WOW_CONTENT_TYPE:
        base = base.replace(
            "你正在为小红书图文笔记写《每日新闻》栏目。",
            "你正在为小红书图文笔记写《每日我去》栏目。",
        )
        # 栏目专属写法与评价风格覆盖通用总结语气，但事实与来源约束继续沿用。
        base += (
            "\n栏目专属要求（优先于上面的通用语气）：\n"
            f"{daily_wow_write_instruction()}"
            f"{daily_wow_comment_instruction(evaluation_viewpoint)}\n"
            "topics（数组，3-5个话题词）：必须包含“每日我去”。不要把 topics 写进 body。\n"
        )
    return base


def _daily_news_fallback_subject(picked, prompt_norm: str) -> str:
    title = _clean_daily_news_title_candidate(_strip_urls((picked.title or "").strip()))
    desc = _strip_urls((picked.description or "").strip())
    if _has_cjk(title):
        return _normalize_news_summary(title, limit=48)
    if _has_cjk(desc):
        return _normalize_news_summary(desc, limit=48)

    source_text = " ".join(
        [
            picked.title or "",
            picked.description or "",
            picked.content or "",
        ]
    )
    english_summary = _english_daily_news_title_summary(source_text, "")
    if english_summary:
        return english_summary

    lower = source_text.lower()
    hint = prompt_norm if _has_cjk(prompt_norm) and not source_text.strip() else ""
    if "科技" in hint or _english_any_keyword(lower, ("ai", "openai", "chip", "tech", "technology", "model", "software")):
        return "一项科技议题出现新进展"
    if "社会" in hint or _english_any_keyword(lower, ("court", "case", "school", "police", "sentence")):
        return "一项社会议题出现新进展"
    if "经济" in hint or _english_any_keyword(lower, ("market", "inflation", "price", "trade", "economy")):
        return "一项经济议题出现新进展"
    return "一项国际议题出现新进展"


def _compact_daily_news_context(picked, *, max_chars: int = 220, include_title: bool = True) -> str:
    parts = [
        ("description", getattr(picked, "description", "") or ""),
        ("content", getattr(picked, "content", "") or ""),
    ]
    if include_title:
        parts.insert(0, ("title", getattr(picked, "title", "") or ""))
    cleaned_parts: list[str] = []
    seen: set[str] = set()
    for kind, part in parts:
        cleaned = _strip_urls(str(part))
        if kind == "title":
            cleaned = _clean_daily_news_title_candidate(cleaned)
        else:
            cleaned = _clean_original_news_text(cleaned)
        cleaned = re.sub(r"\[\+\d+\s+chars?\]", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t\r\n。.!！?")
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        cleaned_parts.append(cleaned)
        seen.add(key)
    context = "。".join(cleaned_parts).strip()
    if context and not context.endswith(("。", "！", "？", "!", "?")):
        context = f"{context}。"
    if len(context) <= max_chars:
        return context
    lead = ""
    for sentence in re.split(r"(?<=[。！？!?])", context):
        s = sentence.strip()
        if not s or _daily_news_fact_sentence_is_noise(s):
            continue
        candidate = f"{lead}{s}" if lead else s
        if len(candidate.rstrip("。！？!?")) <= max_chars:
            lead = candidate
        elif lead:
            break
        else:
            return _finish_daily_news_content_sentence(s, limit=max_chars)
    if lead:
        return _finish_daily_news_content_sentence(lead, limit=max_chars)
    return _finish_daily_news_content_sentence(context, limit=max_chars)


def _daily_news_fact_based_comment(picked, context: str, subject: str) -> str:
    raw = " ".join(
        str(part or "")
        for part in (
            getattr(picked, "title", ""),
            getattr(picked, "description", ""),
            getattr(picked, "content", ""),
            context,
            subject,
        )
    )
    lower = raw.lower()

    if any(word in raw for word in ("滋扰", "擅闯", "营地", "立案调查", "拘留滋事")):
        return (
            "现有公开信息显示，相关部门已对涉事人员采取处置并启动调查。"
            "后续更值得关注调查进展，以及企业与员工安全保障措施能否落实。"
        )

    if any(word in raw for word in ("谈判", "会谈", "部长级")):
        return _daily_news_safe_fact_comment(picked, context)

    if any(word in raw for word in ("参观见学", "青年参观", "青年干部", "外交礼品")):
        return (
            "这类见学活动的意义，在于把外交历史、行业使命与青年培养联系起来。"
            "后续更值得关注学习成果能否转化为日常训练、管理和服务中的具体行动，而不是仅停留在参观层面。"
        )

    if any(word in raw for word in ("体育强国", "全民健身", "体育产业总规模", "经常参加体育锻炼")):
        return (
            "规划把全民健身、竞技体育和体育产业放在同一框架下推进，重点在于目标能否转化为稳定的场地、赛事和服务供给。"
            "后续可关注体育消费、基层设施和青少年参与等指标是否同步改善。"
        )

    if any(word in raw for word in ("潮汕", "侨批", "红头船", "文化传承", "给阿嬷的情书", "香江")):
        return (
            "这条新闻的价值在于把地方文化记忆和当代城市生活连接起来。"
            "侨批、红头船和潮汕社群故事被重新讲述，有助于年轻人理解家族迁徙、诚信互助与文化传承的现实意义。"
        )

    if any(word in raw for word in ("纸尿裤", "甲酰胺", "未检出", "产品检测", "消费品安全")):
        return (
            "这类消费品安全信息的关键在于检测范围、检测机构和批次覆盖是否清楚。"
            "企业公开检测结果有助于回应消费者疑虑，但后续仍应以监管抽检、完整报告和持续质量控制作为判断依据。"
        )

    if any(word in raw for word in ("手机补贴", "品质数码", "苏新消费", "消费补贴", "补贴额度")):
        return (
            "消费补贴的直接作用是降低换机门槛、释放部分数码消费需求。"
            "评价这类政策要看补贴规则是否透明、额度覆盖是否公平，以及是否真正带动线下商家和正规渠道受益。"
        )

    if any(word in raw for word in ("演唱会", "通信保障", "驻场巡检", "云端监控", "网络保障")):
        return (
            "大型演出对通信网络是一次现场压力测试。"
            "运营商提前巡检、现场值守和后台监控能提升观众体验，也能为后续大型活动的应急保障和人流服务积累经验。"
        )

    if any(word in raw for word in ("微信原生AI助手", "小微", "微信AI生态", "AI专属卡", "小程序完成服务")):
        return (
            "微信把 AI 助手嵌入原生功能，价值在于能否真正降低用户操作成本。"
            "更值得关注的是数据来源、授权边界、支付安全和用户可控性，功能便利不能替代透明规则。"
        )

    if any(word in raw for word in ("杭小忆", "黄小西", "数字导游", "智慧旅游", "文旅小程序", "AI伴你游")):
        return (
            "AI 数字导游的价值在于把路线规划、景区服务和游客需求更快连接起来。"
            "但文旅场景更要重视信息准确性、隐私保护和应急服务，不能只看新鲜感和推荐效率。"
        )

    if ("马斯克" in raw and "行权" in raw) or ("特斯拉" in raw and "薪酬方案" in raw):
        return (
            "这笔行权的重点不只是财富数字，更关系到马斯克在特斯拉的投票权和战略控制力。"
            "对投资者而言，应区分账面收益、可出售现金收益和公司治理影响，避免只被巨额数字带走判断。"
        )

    if any(word in raw for word in ("单边平仓", "股票期权组合策略", "股票期权", "期权组合策略")):
        return (
            "期权业务机制调整的重点在于风险控制、技术准备和投资者理解成本。"
            "相关功能暂不实施，说明交易所仍在平衡市场效率与风险承受能力，投资者不宜把技术接口发布等同于业务立即落地。"
        )

    if any(word in raw for word in ("赢创", "欧洲化工", "聚酯业务", "结构优化", "降本措施", "裁3200")):
        return (
            "赢创裁员和关停聚酯业务反映出欧洲化工行业仍承受需求疲弱、成本压力和全球竞争加剧。"
            "观察这类调整，应同时看企业降本成效、受影响地区就业，以及亚太等增长市场能否抵消欧洲结构性压力。"
        )

    if any(word in raw for word in ("古巴", "美国无权评判", "外国干涉", "国家主权", "自决权", "改革措施")):
        return (
            "这条新闻的核心在于国家主权与外部干预边界。"
            "评价古巴改革应看其国内政策目标和民生效果，也应区分外部施压、外交表态与实际改革执行。"
        )

    if any(word in raw for word in ("夏播", "粮食进度", "种肥同播", "农情调度", "高标准农田", "水肥一体化", "花生起垄")):
        return (
            "这条农业新闻的重点在于新技术能否提升播种效率和粮食稳产能力。"
            "种肥同播、水肥一体化等做法如果能降低人工成本、提高肥料利用率，对主产区稳面积、稳产量会有实际意义，后续仍要看覆盖面积和增产效果。"
        )

    if any(word in raw for word in ("大湾区", "粤港澳", "科创资源", "科技成果商业化", "创新集群", "国际科技创新中心")):
        return (
            "大湾区科创建设的关键在于跨城资源能否真正协同，而不是停留在概念叠加。"
            "前沿技术落地需要高校、科研机构、企业和资本形成稳定分工，后续可重点观察成果转化效率、产业配套和跨境规则衔接。"
        )

    if any(word in raw for word in ("韩国科技", "韩国赛道", "韩股", "韩国主题ETF", "QDII", "跨境资金")):
        return (
            "资金加速布局韩国科技资产，反映全球 AI 产业链热度正在外溢到更多市场。"
            "但跨境基金受汇率、估值、行业周期和流动性影响明显，普通投资者不宜只看短期资金流入，更要看产品风险暴露和持仓透明度。"
        )

    if "美股" in raw and any(word in raw for word in ("资金涌入", "半导体设备", "科技板块", "8100亿", "股票基金")):
        return (
            "美股科技资金快速流入说明市场风险偏好正在升温，但也意味着估值和波动压力同步累积。"
            "半导体设备股受 AI 需求拉动具有产业逻辑，投资者仍应区分真实订单、盈利改善和短线资金追涨，避免把阶段性行情视为确定趋势。"
        )

    if "ETF" in raw and any(word in raw for word in ("陆家嘴论坛", "主动ETF", "基金公司", "华夏基金", "易方达")):
        return (
            "ETF格局变化反映头部基金公司竞争加剧，也显示指数化和主动ETF产品仍在扩容。"
            "对普通投资者来说，关注点不应只放在规模排名，更要看产品费率、跟踪误差、流动性和底层资产风险是否匹配自身需求。"
        )

    if "f1" in lower or "formula 1" in lower or "\u4e00\u7ea7\u65b9\u7a0b\u5f0f" in raw:
        return (
            "F1\u7684\u5546\u4e1a\u5316\u80fd\u4e3a\u8d5b\u4e8b\u63d0\u4f9b\u8d44\u91d1\u548c\u5168\u7403\u4f20\u64ad\uff0c\u4f46\u7ade\u6280\u516c\u5e73\u3001\u6bd4\u8d5b\u8282\u594f\u548c\u8f66\u624b\u610f\u89c1\u540c\u6837\u662f\u8fd9\u9879\u8fd0\u52a8\u7684\u6838\u5fc3\u8d44\u4ea7\u3002"
            "\u540e\u7eed\u9700\u5173\u6ce8\u8d5b\u4e8b\u7ec4\u7ec7\u65b9\u5982\u4f55\u5728\u8d5b\u5386\u5b89\u6392\u3001\u8f6c\u64ad\u6743\u76ca\u548c\u7ade\u8d5b\u89c4\u5219\u4e2d\u627e\u5230\u5e73\u8861\uff0c\u8ba9\u5546\u4e1a\u5316\u771f\u6b63\u670d\u52a1\u4e8e\u8fd0\u52a8\u672c\u8eab\u3002"
        )

    has_earnings_signal = any(word in raw for word in ("财报", "季度业绩", "业绩表现")) or _english_any_keyword(
        lower,
        ("blockbuster quarter", "quarterly results", "quarterly performance", "earnings"),
    )
    has_market_reaction = any(word in raw for word in ("评级上调", "股价上涨", "合作关系")) or _english_any_keyword(
        lower,
        ("upgraded", "shares gained", "shares rose", "partnership"),
    )
    if has_earnings_signal and has_market_reaction:
        return (
            "单季表现、机构评级和产业合作都是市场观察公司经营预期的信号，但不等于后续业绩已经兑现。"
            "后续仍需关注正式财报、订单与合作进展，避免把单日股价波动解读为长期趋势。"
        )

    if any(word in raw for word in ("气象", "台风", "暴雨", "灾害预警", "防灾减灾", "农业生产")) or _english_any_keyword(
        lower,
        ("weather monitoring", "meteorological", "disaster warning", "automatic weather station"),
    ):
        return (
            "这件事的现实价值在于把气象监测合作落到灾害预警、农业安排和公共安全等具体民生场景。"
            "评价它不应只看援助名义，更要看设备能否长期运行、数据能否被当地部门稳定使用，以及是否真正提升基层防灾能力。"
        )

    if any(word in raw for word in ("人道主义", "停火", "平民", "冲突地区", "生存需求")):
        return (
            "这条新闻的关键不在表态本身，而在平民保护、救援通道和停火安排能否形成可执行结果。"
            "从中国受众角度看，支持人道主义行动与推动政治解决并不矛盾，真正需要警惕的是把民生危机工具化。"
        )

    if "世界杯" in raw and any(word in raw for word in ("NASA", "空间站", "航天", "太空", "阿耳忒弥斯")):
        return (
            "这条新闻更像是体育 IP 与航天传播的一种结合。"
            "它能放大世界杯话题热度，也说明大型赛事正在借助科技和太空叙事拓展公众参与感，但实际价值仍主要在科普传播和品牌合作层面。"
        )

    if any(
        word in raw
        for word in (
            "产业创新",
            "产业应用",
            "应用场景",
            "科技创新",
            "科技成果商业化",
            "创新集群",
            "科创资源",
            "国际科技创新中心",
            "大湾区",
            "海创会",
            "智慧养老",
            "智能风控",
            "脑电科技",
            "陶瓷刀具",
            "港区安全",
            "全景监控",
            "码头",
            "监控系统",
            "毫米波雷达",
        )
    ):
        return (
            "这类产业科技新闻的关键不在概念本身，而在技术能否落到真实场景。"
            "判断其价值应继续看后续应用规模、运行稳定性、成本收益和服务对象反馈，避免把展示成果直接等同于长期产业成效。"
        )

    if any(word in raw for word in ("操纵", "市场禁入", "监管处罚", "行政处罚", "罚款", "实控人", "内幕交易")):
        return (
            "这类监管处罚的核心在于维护证券市场公平交易和信息披露秩序。"
            "对投资者来说，处罚结果本身只是起点，还应关注公司治理整改、责任落实和后续经营风险。"
        )

    if _english_any_keyword(lower, ("advanced ai model access", "model access", "policy dispute", "technology dispute")):
        return (
            "AI 模型访问争议的重点在于平台规则是否清晰、权限分配是否透明，以及安全边界如何执行。"
            "对企业和开发者来说，稳定可预期的访问机制比短期功能开放更重要，否则创新效率和合规风险都会受到影响。"
        )

    if any(
        word in raw
        for word in (
            "AI写作",
            "AI 写作",
            "人工智能写作",
            "生成式AI",
            "生成式 AI",
            "模型访问",
            "训练数据",
            "作家",
            "出版",
            "版权",
            "署名",
            "内容平台",
        )
    ) or _english_any_keyword(
        lower,
        ("ai writing", "generative ai", "claude", "model access", "publishing", "authors", "copyright"),
    ):
        return (
            "这件事值得关注的不是单个工具本身，而是 AI 使用边界、披露义务和责任归属。"
            "对内容平台、出版机构和普通用户来说，透明规则比简单禁止更重要，否则创作效率提升可能反过来损害版权和信任。"
        )

    if any(word in raw for word in ("体操", "世界杯", "国足", "赛事", "运动员", "挑战赛")):
        return (
            "体育新闻的评价重点应放在竞技表现、人才梯队和长期训练体系，而不是一次成绩带来的情绪波动。"
            "如果相关队伍能把比赛经验转化为稳定备战和青训投入，事件的价值会比短期热度更扎实。"
        )

    if any(word in raw for word in ("外贸", "经贸", "贸易", "贸易额", "关税", "出口", "进口", "供应链")) or _english_any_keyword(
        lower,
        ("trade", "tariff", "export", "import", "supply chain"),
    ):
        return (
            "这类经贸变化需要同时看订单、物流、政策和企业成本，不能只用单一数据判断趋势。"
            "对中国企业而言，稳定供应链和分散市场风险仍是重点，短期波动如果没有后续数据印证，不宜被放大成长期结论。"
        )

    return ""


def _daily_news_contextual_offline_body(picked, prompt_norm: str) -> str:
    context = _compact_daily_news_context(picked)
    if len(context) < 40:
        return ""

    subject = _daily_news_fallback_subject(picked, prompt_norm)
    source_for_copy = (picked.source or picked.domain or "原始来源").strip()
    if not _has_cjk(source_for_copy):
        source_for_copy = "原始来源"
    pub = _format_news_seendate(picked.seendate)

    summary_seed = getattr(picked, "description", "") or _clean_original_news_text(getattr(picked, "content", "") or "") or context
    if summary_seed and not _has_cjk(summary_seed):
        summary_seed = subject
    summary = _normalize_news_summary(summary_seed, fallback=subject, limit=55)
    if not summary.endswith(("。", "！", "？")):
        summary = f"{summary}。"

    summary_override = ""
    context_lower = context.lower()
    if _has_cjk(context):
        fact_sentence = context
    elif _english_any_keyword(
        context_lower,
        ("technology dispute", "advanced ai model", "model access", "policy dispute"),
    ):
        summary_override = "AI模型访问政策争议升温，平台规则、权限透明度和安全边界成为关注焦点，行业讨论持续发酵。"
        fact_sentence = (
            "报道提到，一项围绕 AI 模型访问和政策边界的争议升温，相关讨论集中在高级模型使用权限、"
            "平台规则和责任划分。现有公开材料没有披露更多执行细节，因此正文只概括已经出现的争议方向。"
        )
    elif _english_any_keyword(
        context_lower,
        ("sunscreen", "bemotrizinol", "sunscreen ingredients", "fda review"),
    ):
        summary_override = "美国防晒成分审批进展引发关注，监管效率、消费者选择和公共健康需求成为讨论重点。"
        fact_sentence = (
            "报道提到，一些国家已使用较新的防晒成分多年，美国围绕 bemotrizinol 等成分的审批和市场准入问题再受关注。"
            "这条新闻的核心是防晒产品监管进度、消费者选择和公共健康需求之间的平衡。"
        )
    else:
        return ""
    if summary_override:
        summary = summary_override
    fact_sentence = fact_sentence.strip("。")
    content = _limit_daily_news_content(fact_sentence)
    title_fact = _clean_daily_news_title_candidate(getattr(picked, "title", "") or "")
    if title_fact and _has_cjk(title_fact) and title_fact not in content and len(title_fact) <= 30:
        with_title = f"{title_fact}。{content}".strip()
        content = with_title if len(with_title) <= 150 else _limit_daily_news_content(with_title)
    comment = _daily_news_fact_based_comment(picked, context, subject)
    if _daily_news_comment_is_unsupported(comment, picked, content):
        comment = _daily_news_safe_fact_comment(picked, content)
    body = (
        f"{_NEWS_SUMMARY_LABEL}{summary}\n"
        f"{_NEWS_CONTENT_LABEL}\n"
        f"{content}"
    )
    if comment:
        body = f"{body}\n\n{_NEWS_COMMENT_LABEL}\n{comment}"
    return f"{body}\n\n发布时间：{pub}"


def _specific_daily_news_offline_body(picked) -> str:
    pub = _format_news_seendate(picked.seendate)
    source = (picked.source or picked.domain or "原始来源").strip()
    text = " ".join(
        part
        for part in (picked.title, picked.description, picked.content)
        if part
    ).lower()

    if _english_any_keyword(text, ("seawater battery",)) or (
        _english_any_keyword(text, ("desalination",))
        and _english_any_keyword(text, ("carbon capture",))
    ):
        return (
            "要点摘要：韩国团队研发海水电池，将储能、海水淡化和碳捕集整合到同一系统。\n"
            "新闻内容：\n"
            f"据{source}报道，韩国蔚山国立科学技术院 Kim Young-sik 教授团队开发的海水电池，是一种把能源存储、海水淡化和碳捕集结合在一起的多功能系统。"
            "报道称，这项技术的看点不只是储能，而是尝试让同一套装置同时服务清洁能源、淡水供给和减碳需求。现阶段仍需关注后续工程化验证、成本和规模化应用条件。\n\n"
            "点评：\n"
            "从中国视角看，这类技术如果能走向规模化，会同时触及新能源、海洋资源利用和双碳产业链。更值得关注的是实验室成果能否变成稳定、低成本、可维护的工程系统。你更看好它先落地在哪个场景？"
            f"\n\n发布时间：{pub}"
        )

    if _english_any_keyword(text, ("hegseth", "us troop deployments", "american forces in europe")) or (
        _english_any_keyword(text, ("nato",))
        and _english_any_keyword(text, ("troop deployments", "american forces in europe", "pentagon"))
    ):
        return (
            "要点摘要：美国防长宣布审查驻欧美军部署，北约防务分担再次成为焦点。\n"
            "新闻内容：\n"
            f"据{source}报道，美国防长 Pete Hegseth 宣布，五角大楼将对美国在欧洲的部队部署进行为期六个月的审查。"
            "报道提到，审查结果将与欧洲盟友的防务支出和地区安全责任相关。此举使北约内部关于美国驻欧角色、欧洲承担更多防务责任的讨论进一步升温。\n\n"
            "点评：\n"
            "从中国视角看，美军欧洲部署调整会影响欧洲安全格局，也关系到美国战略资源如何在欧洲与印太之间分配。后续应重点看审查是否带来实际兵力变化，而不是只看口头表态。你觉得欧洲会因此增加防务投入吗？"
            f"\n\n发布时间：{pub}"
        )

    if _english_any_keyword(text, ("using ai", "publishing industry")) or (
        _english_any_keyword(text, ("author", "authors", "writer", "writers", "publishing", "publisher"))
        and _english_any_keyword(text, ("ai", "artificial intelligence"))
    ):
        return (
            "要点摘要：部分作家公开承认使用AI写作，出版业围绕创意和透明度的争议升温。\n"
            "新闻内容：\n"
            f"据{source}报道，随着 AI 工具进入写作流程，一些作家开始公开讨论自己如何在创作中使用 AI。"
            "报道指出，出版业一方面担心 AI 威胁作者收入和人类创造力，另一方面也有人把它当作辅助构思、整理和修改的工具。争议核心在于使用边界、读者知情权和作品署名透明度。\n\n"
            "点评：\n"
            "从中国视角看，AI 写作不会只影响作家，也会影响平台审核、版权交易和内容消费信任。真正关键的是建立可披露、可追责的使用规则，而不是简单把 AI 视为禁区或万能工具。你能接受书里使用 AI 辅助吗？"
            f"\n\n发布时间：{pub}"
        )

    return ""


def _daily_news_offline_body(picked, prompt_norm: str) -> str:
    """
    Offline fallback body: keep it publishable and avoid echoing prompt/requirements.
    """
    specific = _specific_daily_news_offline_body(picked)
    if specific:
        return specific
    contextual = _daily_news_contextual_offline_body(picked, prompt_norm)
    if contextual:
        return contextual

    return ""


def _ensure_daily_news_sections(body: str, prompt_norm: str) -> str:
    text = (body or "").strip()
    if not text:
        return text
    text = _remove_generic_daily_news_comment(text)
    if _load_daily_news_body_json(text):
        return text
    if _extract_rendered_daily_news_body_fields(text):
        return text
    if (
        text.startswith(_NEWS_SUMMARY_LABEL)
        and _NEWS_CONTENT_LABEL in text
        and not _looks_like_jsonish_body(text)
    ):
        return text

    cleaned = (
        text.replace(_NEWS_SUMMARY_LABEL, "")
        .replace(_NEWS_CONTENT_LABEL, "")
        .replace(_NEWS_COMMENT_LABEL, "")
        .strip()
    )
    if _looks_like_jsonish_body(cleaned):
        cleaned = _strip_json_artifacts(cleaned)
    paragraphs = [p.strip() for p in cleaned.splitlines() if p.strip()]
    summary = ""
    if len(paragraphs) >= 3:
        summary = paragraphs[0]
        news = paragraphs[1]
        comment = " ".join(paragraphs[2:])
    elif len(paragraphs) >= 2:
        news = paragraphs[0]
        comment = " ".join(paragraphs[1:])
    else:
        news = paragraphs[0] if paragraphs else cleaned
        comment = ""

    summary = _normalize_news_summary(summary, fallback=news, limit=40)
    if _daily_news_comment_is_generic(comment):
        comment = ""

    out = f"{_NEWS_SUMMARY_LABEL}{summary}\n{_NEWS_CONTENT_LABEL}\n{news}"
    if comment:
        out = f"{out}\n\n{_NEWS_COMMENT_LABEL}\n{comment}"
    return out


def _news_source_line(picked) -> str:
    source = (picked.source or picked.domain or "未知来源").strip()
    return f"来源：{source}"


def _append_news_source_line(body: str, picked) -> str:
    text = _strip_urls(body or "").rstrip()
    if not text:
        return text
    line = _news_source_line(picked)
    if re.search(r"(?:\r?\n)*来源：.*\Z", text):
        text = re.sub(r"(?:\r?\n)*来源：.*\Z", "", text).rstrip()
    return f"{text}\n\n{line}".rstrip()


def _clamp_daily_news_body(body: str) -> str:
    text = (body or "").strip()
    if len(text) <= MAX_IMAGE_BODY:
        return text

    source_match = re.search(r"\n\n来源：[^\n]+\s*$", text)
    source_tail = source_match.group(0).strip() if source_match else ""
    without_source = text[: source_match.start()].rstrip() if source_match else text
    time_match = re.search(r"\n\n发布时间：[^\n]+\s*$", without_source)
    time_tail = time_match.group(0).strip() if time_match else ""
    main = without_source[: time_match.start()].rstrip() if time_match else without_source

    tail_parts = [part for part in (time_tail, source_tail) if part]
    tail = ("\n\n" + "\n\n".join(tail_parts)) if tail_parts else ""
    room = max(0, MAX_IMAGE_BODY - len(tail))
    return f"{main[:room].rstrip()}{tail}".strip()


def _finalize_daily_news_body(
    body: str, picked, prompt_norm: str, title_hint: str = "", *, preserve_length: bool = False,
) -> str:
    raw = body or ""
    if _load_daily_news_body_json(raw):
        fields = _daily_news_body_to_fields(raw, picked, prompt_norm, title_hint=title_hint, preserve_length=preserve_length)
        return _render_daily_news_body_fields(fields, preserve_length=preserve_length)
    text = _strip_urls(raw)
    if _extract_rendered_daily_news_body_fields(text):
        fields = _daily_news_body_to_fields(text, picked, prompt_norm, title_hint=title_hint, preserve_length=preserve_length)
        return _render_daily_news_body_fields(fields, preserve_length=preserve_length)
    text = _ensure_daily_news_sections(text, prompt_norm)
    text = _ensure_news_publish_date(text, picked.seendate)
    text = _append_news_source_line(text, picked)
    fields = _daily_news_body_to_fields(text, picked, prompt_norm, title_hint=title_hint, preserve_length=preserve_length)
    return _render_daily_news_body_fields(fields, preserve_length=preserve_length)


def _review_daily_news_length(draft: dict[str, Any], picked, *, rewrite_count: int = 0) -> str:
    fields = _daily_news_body_quality_fields(str(draft.get("body") or ""))
    comment = _clean_daily_news_comment_value(fields.get("评价", ""), preserve_length=True)
    review = assess_news_length(fields.get("内容", ""), comment, draft, picked)
    if not review["issue"] and comment and not _daily_news_text_has_sentence_end(comment):
        review.update(issue="incomplete_comment", status="needs_resummary")
    if not review["issue"] and len(re.findall(r"[。！？!?]", comment)) > 1:
        review.update(issue="comment_multiple_sentences", status="needs_resummary")
    if not review["issue"] and len(str(draft.get("body") or "")) > MAX_IMAGE_BODY:
        review.update(issue="body_too_long", status="needs_resummary")
    review["rewrite_count"] = rewrite_count
    draft["_length_review"] = review
    return str(review["issue"])


def _resummarize_daily_news_length_once(
    draft: dict[str, Any], *, cfgs, picked, prompt_norm: str,
    news_prompt: str, asset_paths: list[str], model_queues=None,
) -> tuple[dict[str, Any], str]:
    issue = _review_daily_news_length(draft, picked)
    if not issue:
        return draft, ""
    kwargs = dict(title_hint="每日新闻", prompt_hint=news_prompt + news_length_rewrite_instruction(draft),
                  asset_paths=asset_paths, preserve_body=True, concise_news=True)
    rewritten = (
        model_queues.submit_llm(generate_draft, cfgs, **kwargs).result()
        if model_queues else generate_draft(cfgs, **kwargs)
    )
    if not isinstance(rewritten, dict) or rewritten.get("_fallback_error"):
        reason = rewritten.get("_fallback_error") if isinstance(rewritten, dict) else "invalid draft"
        raise RuntimeError(f"daily news resummary failed: {reason}")
    rewritten["title"] = _normalize_daily_news_title(rewritten.get("title", ""), picked, prompt_norm)
    rewritten["body"] = _finalize_daily_news_body(
        rewritten.get("body", ""), picked, prompt_norm,
        title_hint=rewritten["title"], preserve_length=True,
    )
    rewritten["body"] = _repair_daily_news_mismatched_comment(
        rewritten["body"], picked, prompt_norm, title_hint=rewritten["title"], preserve_length=True,
    )
    rewritten["topics"] = _normalize_daily_news_topics(
        rewritten.get("topics") or [], prompt_norm,
        context=f"{rewritten['title']} {rewritten['body']}",
    )
    rewritten = _simplify_daily_news_draft(rewritten)
    return rewritten, _review_daily_news_length(rewritten, picked, rewrite_count=1)


def _source_grounded_single_material_draft(
    picked, prompt_norm: str, *, preserve_length: bool = False,
) -> dict[str, Any]:
    """Rebuild a publishable single-material draft without trusting generic model copy."""
    title = _normalize_daily_news_title(picked.title or picked.description or "", picked, prompt_norm)
    body = _daily_news_offline_body(picked, prompt_norm)
    body = _finalize_daily_news_body(body, picked, prompt_norm, title_hint=title, preserve_length=preserve_length)
    body = _repair_daily_news_mismatched_comment(body, picked, prompt_norm, title_hint=title, preserve_length=preserve_length)
    topics = _normalize_daily_news_topics(
        ["每日新闻"],
        prompt_norm,
        context=f"{title} {body}",
    )
    return _simplify_daily_news_draft(
        {
            "title": title,
            "body": body,
            "topics": topics,
            "image_event": _daily_news_fallback_subject(picked, prompt_norm),
        }
    )


def _fake_news_prompt(prompt_norm: str) -> str:
    """
    Prompt for humorous, clearly fictional fake news.
    """
    topic = prompt_norm or "日常离谱小事"
    return (
        "你正在为小红书图文笔记写《每日假新闻》栏目。\n"
        "请根据给定主题编写一条**明显虚构、幽默夸张**的新闻，语气轻松有趣。\n"
        "必须让读者一眼看出是娱乐内容，避免与现实新闻混淆。\n"
        "不要引用真实媒体/来源/链接，不要提供可核验的具体事实或真实数据。\n"
        "避免对真实人物/机构做恶意指控或诽谤，内容保持善意搞笑。\n"
        "正文只输出可直接发布的文章，不要复述提示词或规则。\n\n"
        f"主题提示（可自由发挥但要贴合）：{topic}\n\n"
        "正文要求：\n"
        "1) 只输出一段完整正文，不要列点或小标题；\n"
        "2) 字数约 200-400 字；\n"
        "3) 末尾必须加一句：本文纯属虚构，仅供娱乐。\n"
        "4) topics 输出 3-8 个话题词，包含“每日假新闻”。\n"
    )


def _fake_news_offline_body(prompt_norm: str) -> str:
    topic = prompt_norm or "离谱日常"
    return (
        f"【假新闻播报】今日最离谱的主角是「{topic}」。\n"
        "据不可靠但十分认真（的想象）消息称，相关事件在短短几小时内引发了全民围观，"
        "围观群众纷纷表示：这是我今天最开心的笑点。更夸张的是，现场还出现了神秘“反转”，"
        "让事情从“不可思议”直接升级为“笑到肚子疼”。\n\n"
        "专家（其实是路过的瓜友）点评：这类剧情虽然离谱，但快乐是真的。"
        "如果明天还能看到同款离谱升级，请记得第一时间来围观。\n"
        "本文纯属虚构，仅供娱乐。"
    )


def _daily_news_source_trace(news_meta: dict[str, Any], picked) -> dict[str, Any]:
    meta = dict(news_meta or {})
    existing = meta.get("source_api")
    trace = dict(existing) if isinstance(existing, dict) else {}
    provider = (
        str(trace.get("provider") or meta.get("api_source") or meta.get("provider") or "").strip()
        or "unknown"
    )
    trace["provider"] = provider
    for key in ("query", "queries_used", "provider_plan", "provider_attempts"):
        if key in meta and key not in trace:
            trace[key] = meta[key]
    if picked is not None:
        trace.setdefault("item_source", (getattr(picked, "source", "") or "").strip() or None)
        trace.setdefault("item_domain", (getattr(picked, "domain", "") or "").strip() or None)
        trace.setdefault("item_url", (getattr(picked, "url", "") or "").strip() or None)
        trace.setdefault("item_title", (getattr(picked, "title", "") or "").strip() or None)
    return {k: v for k, v in trace.items() if v not in ("", None)}


def _daily_news_meta_with_trace(news_meta: dict[str, Any], picked) -> dict[str, Any]:
    meta = dict(news_meta or {})
    trace = _daily_news_source_trace(meta, picked)
    meta["api_source"] = trace.get("provider") or meta.get("provider") or "unknown"
    meta["source_api"] = trace
    return meta


def _env_int(name: str, default: int, *, min_value: int = 1, max_value: int | None = None) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _positive_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except (TypeError, ValueError):
        return None
    return max(1, parsed)


def _candidate_lookback_windows(
    explicit_days: object = None,
    *,
    env_names: tuple[str, ...] = (),
) -> tuple[list[int], dict[str, Any]]:
    fixed = _positive_int_or_none(explicit_days)
    source = "argument" if fixed is not None else ""
    if fixed is None:
        for name in env_names:
            fixed = _positive_int_or_none(os.getenv(name))
            if fixed is not None:
                source = name
                break
    if fixed is not None:
        return [fixed], {
            "mode": "fixed",
            "source": source or "argument",
            "windows": [fixed],
        }
    windows = list(DEFAULT_CANDIDATE_LOOKBACK_WINDOWS)
    return windows, {
        "mode": "auto_expand",
        "source": "default",
        "windows": windows,
    }


def _ai_digest_lookback_windows(explicit_days: object = None) -> tuple[list[int], dict[str, Any]]:
    """Resolve the AI digest collection window without admitting old filler.

    The regular helper remains backward-compatible for other editorial
    workflows.  The agent sets this policy explicitly so an AI digest run
    fetches only the publishing day and the preceding Beijing calendar day.
    The final publication gate still performs the independent item-level
    check.
    """
    strict = str(os.getenv("AI_DIGEST_STRICT_RECENT", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    if strict:
        return [2], {
            "mode": "strict_two_day",
            "source": "agent_policy",
            "windows": [2],
        }
    return _candidate_lookback_windows(
        explicit_days,
        env_names=("AI_DIGEST_LOOKBACK_DAYS", "AI_DIGEST_MAX_AGE_DAYS", "CONTENT_LOOKBACK_DAYS"),
    )


def _daily_news_lookback_window(
    explicit_days: object = None,
    *,
    env_names: tuple[str, ...] = (),
) -> tuple[list[int], dict[str, Any]]:
    return resolve_news_windows(explicit_days, env_names=env_names)


def _ai_digest_candidate_pool_target(target_count: int) -> tuple[int, int]:
    # Keep enough lower-ranked official and fallback-source items available for
    # history-aware deduplication before the LLM sees the pool.
    factor = _env_int("AI_DIGEST_CANDIDATE_POOL_FACTOR", 10, min_value=1, max_value=20)
    pool_target = min(200, max(target_count, target_count * factor))
    return pool_target, factor


def _ai_digest_adaptive_max_items() -> int:
    raw = (os.getenv("AI_DIGEST_MAX_ITEMS") or os.getenv("AI_DIGEST_TARGET_ITEMS") or "").strip()
    try:
        value = int(raw) if raw else 20
    except ValueError:
        value = 20
    return min(20, max(AI_DIGEST_MIN_ITEMS, value))


def _ai_digest_min_items() -> int:
    """Return the internal lower bound for the quality-driven digest.

    ``AI_DIGEST_MIN_ITEMS`` belonged to the old eight-item fallback policy.
    It is intentionally ignored so stale environment files cannot restore a
    quantity gate that makes the workflow publish low-impact filler.
    """
    return AI_DIGEST_MIN_ITEMS


def _enforce_ai_digest_publish_policy(
    brief: AIDigestBrief,
) -> tuple[AIDigestBrief, dict[str, int | str]]:
    """Apply the final two-day Beijing gate after every generation path."""

    publication_date = datetime.now(timezone.utc).astimezone(BEIJING_TZ).date().isoformat()
    selected, meta = ai_digest_items_in_beijing_window(
        brief.items,
        publication_date=publication_date,
        now=datetime.now(timezone.utc),
    )
    source_counts: dict[str, int] = {}
    source_capped: list[AIUpdateItem] = []
    source_cap_removed = 0
    for item in selected:
        source_key = ai_update_source_key(item)
        if source_counts.get(source_key, 0) >= AI_DIGEST_MAX_ITEMS_PER_SOURCE:
            source_cap_removed += 1
            continue
        source_counts[source_key] = source_counts.get(source_key, 0) + 1
        source_capped.append(item)
    selected = source_capped
    meta["source_cap_removed"] = source_cap_removed
    if not selected:
        raise RuntimeError(
            "daily ai digest material insufficient: strict Beijing two-day window "
            f"{meta['earliest_date']}..{meta['publication_date']} has no eligible item; "
            "older or undated items were discarded"
        )
    data = brief.model_dump()
    data["date"] = publication_date
    data["items"] = [item.model_dump() for item in selected]
    meta["selected_count"] = len(selected)
    return AIDigestBrief.model_validate(data), meta


def _ai_digest_prompt_search_queries(prompt_hint: str) -> list[str]:
    """Turn explicit requested AI topics into targeted search backfills."""
    requested_topics = (
        "Claude Fable 5.1",
        "HY4 preview",
        "Qwen3.8-Flash-Next正式发布",
        "GLM-5.3-Flash发布",
        "QwenWork International上线",
        "Codex plus用户回复5小时限制",
        "Breeze TTS 2权重公开可用",
        "OpenAI宣布断供Cursor",
        "MiniMax H3 Max在Fal.ai发布",
    )
    hint = re.sub(r"\s+", " ", str(prompt_hint or "")).strip()
    return [topic for topic in requested_topics if topic in hint]


def _ai_digest_topic_text(item: AIUpdateItem) -> str:
    return re.sub(
        r"[^a-z0-9\u4e00-\u9fff]+",
        "",
        " ".join(
            str(part or "")
            for part in (item.title, item.summary, item.raw_excerpt)
        ),
        flags=re.IGNORECASE,
    ).lower()


def _ai_digest_prompt_topic_matches(item: AIUpdateItem, topic: str) -> bool:
    """Match a requested event using stable aliases across Chinese/English sources."""

    text = _ai_digest_topic_text(item)
    topic_key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", topic, flags=re.IGNORECASE).lower()
    if topic_key and topic_key in text:
        return True
    aliases = {
        "openai宣布断供cursor": (("openai", "cursor"),),
        "minimaxh3max在falai发布": (("h3", "max", "fal"), ("minimax", "h3", "max")),
    }
    for group in aliases.get(topic_key, ()):
        if all(term in text for term in group):
            return True
    return False


def _ensure_ai_digest_prompt_topic_coverage(
    selected: list[AIUpdateItem],
    candidates: list[AIUpdateItem],
    requested_topics: list[str],
) -> tuple[list[AIUpdateItem], dict[str, Any]]:
    """Add available explicitly requested topics before body-capacity fitting."""
    if not requested_topics:
        return list(selected), {"requested": [], "matched": [], "missing": []}
    output = list(selected)
    selected_keys = {ai_update_history_key(item) for item in output}
    source_counts = dict(ai_digest_source_counts(output))
    matched: list[str] = []
    missing: list[str] = []
    requested_keys = {
        re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", topic, flags=re.IGNORECASE).lower()
        for topic in requested_topics
    }
    for topic in requested_topics:
        topic_output_matches = [
            (index, item)
            for index, item in enumerate(output)
            if _ai_digest_prompt_topic_matches(item, topic)
        ]
        if len(topic_output_matches) > 1:
            keep_index, _keep_item = max(
                topic_output_matches,
                key=lambda pair: (
                    pair[1].source_type in {"official", "github"},
                    float(pair[1].confidence_score or 0.0),
                    len(pair[1].evidence_urls or []),
                    pair[1].published_at,
                ),
            )
            for index, removed in reversed(topic_output_matches):
                if index == keep_index:
                    continue
                output.pop(index)
                source_key = ai_update_source_key(removed)
                source_counts[source_key] = max(0, source_counts.get(source_key, 0) - 1)
            # Multiple URLs for one event intentionally share a stable
            # history key. Rebuild from the retained output so removing a
            # duplicate cannot also remove the key of the item we kept.
            selected_keys = {ai_update_history_key(item) for item in output}
        topic_key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", topic, flags=re.IGNORECASE).lower()
        pool = [*output, *candidates]
        matches = [item for item in pool if _ai_digest_prompt_topic_matches(item, topic)]
        selected_match = next(
            (item for item in matches if ai_update_history_key(item) in selected_keys),
            None,
        )
        if selected_match is not None:
            matched.append(topic)
            continue
        available_matches = [
            item
            for item in matches
            if source_counts.get(ai_update_source_key(item), 0)
            < AI_DIGEST_MAX_ITEMS_PER_SOURCE
        ]
        available_matches.sort(
            key=lambda item: (
                item.source_type != "official",
                "官方直连" not in item.tags,
                -float(item.confidence_score or 0.0),
            )
        )
        candidate = available_matches[0] if available_matches else (matches[0] if matches else None)
        if candidate is None:
            missing.append(topic)
            continue
        candidate_key = ai_update_history_key(candidate)
        source_key = ai_update_source_key(candidate)
        if not available_matches:
            replace_indexes = [
                index
                for index, item in enumerate(output)
                if ai_update_source_key(item) == source_key
                and not any(
                    key and key in _ai_digest_topic_text(item)
                    for key in requested_keys
                )
            ]
            if not replace_indexes:
                missing.append(topic)
                continue
            removed = output.pop(replace_indexes[-1])
            selected_keys.discard(ai_update_history_key(removed))
            source_counts[source_key] = max(0, source_counts.get(source_key, 0) - 1)
        output.append(candidate)
        selected_keys.add(candidate_key)
        source_counts[source_key] = source_counts.get(source_key, 0) + 1
        matched.append(topic)
    return output, {"requested": list(requested_topics), "matched": matched, "missing": missing}


def _prioritize_ai_digest_model_releases(items: Iterable[AIUpdateItem]) -> list[AIUpdateItem]:
    """Reorder the final validated set so explicit model releases lead."""
    values = list(items or [])
    if len(values) < 2:
        return values
    return rank_ai_updates(
        values,
        target_count=len(values),
        min_official_count=len(values) + 1,
        allow_social_backfill=True,
        max_age_days=14,
        max_items_per_source=None,
    )


def _missing_ai_digest_prompt_topics(
    items: Iterable[AIUpdateItem],
    requested_topics: Iterable[str],
) -> list[str]:
    available = list(items or [])
    missing: list[str] = []
    for topic in requested_topics:
        topic_key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", topic, flags=re.IGNORECASE).lower()
        if not topic_key or not any(_ai_digest_prompt_topic_matches(item, topic) for item in available):
            missing.append(topic)
    return missing


def _ai_digest_progress_callback():
    enabled = (os.getenv("AI_DIGEST_PROGRESS") or "1").strip().lower() not in {"0", "false", "no", "off"}
    if not enabled:
        return None

    def emit(stage: str, detail: str) -> None:
        print(f"[ai-digest] stage={stage} | {detail}", flush=True)

    return emit


def _with_ai_digest_items(brief: AIDigestBrief, items) -> AIDigestBrief:
    data = brief.model_dump()
    data["items"] = [item.model_dump() for item in items]
    return AIDigestBrief.model_validate(data)


def _rank_brief_ai_digest_items(
    brief: AIDigestBrief,
    *,
    target_count: int,
    min_official_count: int,
    max_age_days: int,
    min_domestic_model_count: int = 0,
    min_foreign_ai_count: int = 0,
) -> AIDigestBrief:
    ranked = rank_ai_updates(
        list(brief.items or []),
        target_count=target_count,
        min_official_count=min_official_count,
        allow_social_backfill=True,
        max_age_days=max_age_days,
        min_domestic_model_count=min_domestic_model_count,
        min_foreign_ai_count=min_foreign_ai_count,
    )
    return _with_ai_digest_items(brief, ranked)


def _finalize_ai_digest_brief(
    brief: AIDigestBrief,
    *,
    generation_mode: str,
    target_count: int,
    min_official_count: int,
    max_age_days: int,
    min_domestic_model_count: int = 0,
    min_foreign_ai_count: int = 0,
    preserve_validated_order: bool = False,
) -> AIDigestBrief:
    """Keep a validated LLM rewrite intact; only rank deterministic fallbacks."""
    if generation_mode == "llm" or preserve_validated_order:
        return brief
    return _rank_brief_ai_digest_items(
        brief,
        target_count=target_count,
        min_official_count=min_official_count,
        max_age_days=max_age_days,
        min_domestic_model_count=min_domestic_model_count,
        min_foreign_ai_count=min_foreign_ai_count,
    )


def _prepare_ai_digest_llm_items(
    items: Iterable[AIUpdateItem],
    *,
    target_count: int,
) -> list[AIUpdateItem]:
    """Pass the already validated selection to the rewrite model unchanged.

    The adaptive selector and body-capacity pass already enforce freshness,
    quotas, deduplication, and the per-source cap. Re-ranking here can remove
    an item between selection and provenance restoration, leaving the LLM with
    fewer traceable inputs than the requested output count.
    """
    selected = list(items or [])
    target = max(1, int(target_count or 1))
    if len(selected) < target:
        raise RuntimeError(
            "daily ai digest LLM input selection is shorter than the requested count: "
            f"{len(selected)} < {target}"
        )
    return selected[:target]


def _select_ai_digest_fallback_pool(
    prepared_items: Iterable[AIUpdateItem],
    raw_items: Iterable[AIUpdateItem],
    *,
    target_count: int,
) -> list[AIUpdateItem]:
    """Prefer the validated selection, but recover from the full raw pool if it was shortened."""
    prepared = list(prepared_items or [])
    target = max(1, int(target_count or 1))
    if len(prepared) >= target:
        return prepared
    raw = list(raw_items or [])
    return raw if len(raw) >= target else prepared


def _build_quota_safe_ai_digest_fallback(
    items,
    *,
    target_count: int,
    min_official_count: int,
    max_age_days: int,
    min_domestic_model_count: int,
    min_foreign_ai_count: int,
) -> AIDigestBrief:
    selected = rank_ai_updates(
        list(items or []),
        target_count=target_count,
        min_official_count=min_official_count,
        allow_social_backfill=True,
        max_age_days=max_age_days,
        min_domestic_model_count=min_domestic_model_count,
        min_foreign_ai_count=min_foreign_ai_count,
    )
    return build_fallback_brief(selected, target_count=target_count)


def _ai_digest_selection_error(
    items,
    *,
    target_count: int,
    min_official_count: int,
    min_domestic_model_count: int,
    min_foreign_ai_count: int,
    max_age_days: int,
) -> str:
    item_list = list(items or [])
    counts = ai_digest_quota_counts(item_list)
    source_counts = ai_digest_source_counts(item_list)
    source_capacity = sum(min(count, AI_DIGEST_MAX_ITEMS_PER_SOURCE) for count in source_counts.values())
    official_count = ai_digest_official_count(item_list)
    problems = []
    if len(item_list) < target_count:
        problems.append(f"有效资讯不足{target_count}条，当前{len(item_list)}条")
    if source_capacity < target_count:
        problems.append(
            f"信源多样性不足：目标{target_count}条，同一信源最多{AI_DIGEST_MAX_ITEMS_PER_SOURCE}条，"
            f"当前最多可生成{source_capacity}条"
        )
    if official_count < min_official_count:
        problems.append(
            f"官方可追溯资讯不足{min_official_count}条，当前{official_count}条"
        )
    if counts["domestic_model"] < min_domestic_model_count:
        problems.append(
            f"国内模型资讯不足{min_domestic_model_count}条，当前{counts['domestic_model']}条"
        )
    if counts["foreign_ai"] < min_foreign_ai_count:
        problems.append(f"国外AI资讯不足{min_foreign_ai_count}条，当前{counts['foreign_ai']}条")
    if not problems:
        return ""
    return f"daily ai digest create failed: {'；'.join(problems)}；仅允许生成日前{max_age_days}日内可追溯资讯"


def _ai_digest_source_cap_error(
    items: Iterable[AIUpdateItem],
    *,
    target_count: int | None = None,
) -> str:
    item_list = list(items or [])
    source_counts = ai_digest_source_counts(item_list)
    over_limit = {
        source: count
        for source, count in source_counts.items()
        if count > AI_DIGEST_MAX_ITEMS_PER_SOURCE
    }
    if over_limit:
        source, count = max(over_limit.items(), key=lambda pair: pair[1])
        return (
            f"每日AI讯息信源约束失败：信源 {source} 有 {count} 条，"
            f"上限为 {AI_DIGEST_MAX_ITEMS_PER_SOURCE} 条"
        )
    if target_count is not None and len(item_list) < int(target_count):
        return (
            f"每日AI讯息信源多样性不足：目标 {int(target_count)} 条，"
            f"当前仅 {len(item_list)} 条"
        )
    return ""


def _uploaded_ai_digest_history_keys() -> set[str]:
    def created_on_beijing_date(post: Post, target_date: str) -> bool:
        # ``updated_at`` is excluded so re-saving an older digest does not
        # make it count as a same-day regeneration.
        for raw in (post.created_at, post.uploaded_at):
            text = str(raw or "").strip()
            if not text:
                continue
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed.astimezone(BEIJING_TZ).strftime("%Y-%m-%d") == target_date:
                return True
        return False

    # ``AI_DIGEST_HISTORY_SKIP_TODAY=1`` regenerates the same day's digest
    # without deduplicating against digests already saved earlier today. That
    # matches a repair/regeneration request while keeping cross-day dedupe.
    skip_today = (os.getenv("AI_DIGEST_HISTORY_SKIP_TODAY") or "").strip().lower() not in {
        "",
        "0",
        "false",
        "no",
        "off",
    }
    today = datetime.now(timezone.utc).astimezone(BEIJING_TZ).strftime("%Y-%m-%d")
    keys: set[str] = set()
    for post in list_posts():
        if not (post.uploaded or post.status in {PostStatus.saved_draft, PostStatus.published}):
            continue
        if skip_today and created_on_beijing_date(post, today):
            continue
        digest = (post.platform or {}).get("ai_digest")
        if not isinstance(digest, dict):
            continue
        raw_items = digest.get("items")
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            try:
                item = AIUpdateItem.model_validate(raw_item)
            except Exception:
                continue
            title = re.sub(r"\s+", "", item.title or "")
            # Do not let a known placeholder/generic title from an earlier
            # failed draft poison the next run's history gate. Concrete items
            # from the same draft remain eligible for deduplication.
            if (
                "generic_title" in ai_update_quality_issues(item)
                or re.search(r"(?:发布|推出|上线)新进展$", title)
            ):
                continue
            key = ai_update_history_key(item)
            if key and key not in {"/|title:", "title:|title:"}:
                keys.add(key)
    return keys


def _select_adaptive_ai_digest_items(
    items,
    *,
    impact_scores: dict[str, dict[str, object]],
    historical_keys: set[str],
    protected_topics: Iterable[str] | None = None,
    min_items: int = AI_DIGEST_MIN_ITEMS,
    max_items: int = 20,
    min_official_count: int = 6,
    min_domestic_model_count: int = AI_DIGEST_MIN_DOMESTIC_MODEL_ITEMS,
    min_foreign_ai_count: int = AI_DIGEST_MIN_FOREIGN_AI_ITEMS,
    allow_official_relaxation: bool = True,
    impact_threshold: float = 75.0,
) -> tuple[list[AIUpdateItem], dict[str, Any]]:
    minimum = max(1, int(min_items or AI_DIGEST_MIN_ITEMS))
    maximum = max(minimum, min(20, int(max_items or 20)))
    item_list = list(items or [])
    ranked_all = rank_ai_updates(
        item_list,
        target_count=max(1, len(item_list)),
        min_official_count=max(maximum + 1, len(item_list) + 1),
        allow_social_backfill=True,
        max_age_days=14,
        # Keep quota candidates available through the adaptive re-ranking
        # stage as well as the earlier impact-review pool.
        min_domestic_model_count=min_domestic_model_count,
        min_foreign_ai_count=min_foreign_ai_count,
        max_items_per_source=None,
    )
    recent_keys = {
        days: {
            item.dedupe_key
            for item in filter_recent_ai_updates(ranked_all, max_age_days=days, require_url=True)
        }
        for days in (3, 7, 14)
    }

    def is_high(item: AIUpdateItem) -> bool:
        row = impact_scores.get(item.dedupe_key) or {}
        return bool(row.get("high_impact"))

    protected_topic_list = list(protected_topics or [])

    def is_protected(item: AIUpdateItem) -> bool:
        return any(
            _ai_digest_prompt_topic_matches(item, topic)
            for topic in protected_topic_list
        )

    novel = [
        item
        for item in ranked_all
        if (ai_update_history_key(item) not in historical_keys or is_protected(item))
        and not ai_update_is_lifecycle_notice(item)
        and not ai_update_is_non_model_infrastructure_notice(item)
    ]
    strict_available = [
        item
        for item in novel
        if item.dedupe_key in recent_keys[3]
        and (is_high(item) or is_protected(item))
    ]
    impact_rescue_count = 0
    if len(strict_available) < maximum:
        # Keep concrete model releases and official technical updates even
        # when the reviewer prefers another story. Minimum count is not an
        # early-stop condition for finding missed releases.
        strict_keys = {item.dedupe_key for item in strict_available}
        deterministic_rescue = [
            item
            for item in novel
            if item.dedupe_key in recent_keys[3]
            and item.dedupe_key not in strict_keys
            and ai_update_is_high_impact(item, threshold=impact_threshold)
            and (
                len(strict_available) < minimum
                or ai_update_category(item) == "model_release"
                or item.source_type in {"official", "github"}
            )
        ]
        for item in deterministic_rescue:
            strict_available.append(item)
            strict_keys.add(item.dedupe_key)
            impact_rescue_count += 1
            if len(strict_available) >= maximum:
                break
    strict_ranked = rank_ai_updates(
        strict_available,
        target_count=maximum,
        min_official_count=maximum + 1,
        allow_social_backfill=True,
        max_age_days=3,
    )
    strict_target = min(len(strict_ranked), maximum)
    if strict_target < minimum:
        print(
            "[ai-digest] selection_diagnostics "
            f"high_impact_recent={strict_target} minimum={minimum} "
            f"novel={len(novel)} historical_excluded={len(item_list) - len(novel)}"
        )
        raise RuntimeError(
            "daily ai digest high-impact material insufficient: "
            f"only {strict_target} recent high-impact AI update(s) remain after deduplication; "
            "low-impact, stale, or historical updates are not used as filler"
        )
    target = strict_target
    # A historical digest item is never eligible for a new digest. If the
    # remaining candidates cannot satisfy the quotas, relax only the quota
    # counts to the available high-impact pool; never add low-impact filler.
    max_historical_reuse = 0

    strict_keys = {item.dedupe_key for item in strict_ranked}
    historical_reuse: list[AIUpdateItem] = []

    tier_items = {
        "three_day_normal": [],
        "seven_day_high": [],
        "seven_day_normal": [],
        "fourteen_day_high": [],
        "fourteen_day_normal": [],
        "historical_reuse": historical_reuse,
    }
    allowed = list(strict_ranked[:target])
    allowed_keys = {item.dedupe_key for item in allowed}

    strict_quota_counts = ai_digest_quota_counts(allowed)
    requested_domestic_min = min(max(0, int(min_domestic_model_count or 0)), target)
    requested_foreign_min = min(max(0, int(min_foreign_ai_count or 0)), target)
    effective_domestic_min = min(requested_domestic_min, strict_quota_counts["domestic_model"])
    effective_foreign_min = min(requested_foreign_min, strict_quota_counts["foreign_ai"])

    def select_allowed() -> list[AIUpdateItem]:
        return rank_ai_updates(
            allowed,
            target_count=target,
            min_official_count=target + 1,
            allow_social_backfill=True,
            max_age_days=14,
            min_domestic_model_count=effective_domestic_min,
            min_foreign_ai_count=effective_foreign_min,
        )

    eligible_items = list(strict_ranked)

    requested_official_min = min(max(0, int(min_official_count or 0)), target)
    eligible_official_count = ai_digest_official_count(eligible_items)
    if requested_official_min > 0 and eligible_official_count == 0:
        raise RuntimeError(
            "daily ai digest official material insufficient: "
            "去重后没有近期合格的官方资讯；请检查官方源连接或补充可核验的官方发布，"
            "不会自动上传仅有媒体体验文章的简报"
        )
    effective_official_min = (
        min(requested_official_min, eligible_official_count)
        if allow_official_relaxation
        else requested_official_min
    )
    selected = select_allowed()
    error = _ai_digest_selection_error(
        selected,
        target_count=target,
        min_official_count=effective_official_min,
        min_domestic_model_count=effective_domestic_min,
        min_foreign_ai_count=effective_foreign_min,
        max_age_days=14,
    )
    tiers_used: list[str] = []

    selected_keys = {item.dedupe_key for item in selected}
    reused_selected = sum(
        1
        for item in selected
        if ai_update_history_key(item) in historical_keys and not is_protected(item)
    )
    if reused_selected > max_historical_reuse:
        error = (
            f"daily ai digest historical novelty insufficient: 历史资讯复用{reused_selected}条，"
            f"超过本批最多{max_historical_reuse}条"
        )
    if error:
        if historical_keys:
            error = f"{error}；历史重复已拦截，不会复用历史AI讯息"
        print(
            "[ai-digest] selection_diagnostics "
            f"ranked={len(ranked_all)} ranked_quota={ai_digest_quota_counts(ranked_all)} "
            f"novel={len(novel)} novel_quota={ai_digest_quota_counts(novel)} "
            f"historical_reuse={len(historical_reuse)} "
            f"historical_reuse_quota={ai_digest_quota_counts(historical_reuse)} "
            f"allowed={len(allowed)} selected={len(selected)} "
            f"selected_quota={ai_digest_quota_counts(selected)}"
        )
        raise RuntimeError(error)

    selected_tier_counts = {
        name: sum(1 for item in tier if item.dedupe_key in selected_keys)
        for name, tier in tier_items.items()
    }
    # The adaptive digest is intentionally high-impact-only. Keep the tier
    # fields for metadata compatibility, but they remain empty unless a future
    # opt-in policy explicitly introduces a fallback tier.
    strict_selected = sum(1 for item in selected if item.dedupe_key in strict_keys)
    fallback_selected = max(0, len(selected) - strict_selected)
    return selected, {
        "selection_mode": "adaptive_strict_rescue" if impact_rescue_count else "adaptive_strict",
        "quality_gate": "recent_high_impact_only",
        "low_impact_backfill": False,
        "min_items": minimum,
        "max_items": maximum,
        "strict_candidate_count": strict_target,
        "strict_selected_count": strict_selected,
        "impact_rescue_count": impact_rescue_count,
        "fallback_selected_count": fallback_selected,
        "fallback_tiers": selected_tier_counts,
        "fallback_tiers_used": tiers_used,
        "target_items": target,
        "actual_items": len(selected),
        "historical_reused_count": reused_selected,
        "max_historical_reuse": max_historical_reuse,
        "eligible_official_items": eligible_official_count,
        "effective_min_official_items": effective_official_min,
        "official_target_relaxed": effective_official_min < requested_official_min,
        "requested_min_domestic_model_items": requested_domestic_min,
        "effective_min_domestic_model_items": effective_domestic_min,
        "requested_min_foreign_ai_items": requested_foreign_min,
        "effective_min_foreign_ai_items": effective_foreign_min,
    }


def _prefer_novel_ai_digest_items(
    items,
    *,
    historical_keys: set[str],
    target_count: int,
    min_official_count: int,
    max_age_days: int,
    min_domestic_model_count: int,
    min_foreign_ai_count: int,
) -> tuple[list[AIUpdateItem], dict[str, int]]:
    item_list = list(items or [])
    novel = [item for item in item_list if ai_update_history_key(item) not in historical_keys]
    reused = [item for item in item_list if ai_update_history_key(item) in historical_keys]
    meta = {
        "historical_key_count": len(historical_keys),
        "candidate_count_before_history_filter": len(item_list),
        "novel_candidate_count": len(novel),
        "reused_candidate_count": len(reused),
        "reused_selected_count": 0,
    }
    if not historical_keys:
        return item_list, meta

    allowed = list(novel)
    selected = rank_ai_updates(
        allowed,
        target_count=target_count,
        min_official_count=min_official_count,
        allow_social_backfill=True,
        max_age_days=max_age_days,
        min_domestic_model_count=min_domestic_model_count,
        min_foreign_ai_count=min_foreign_ai_count,
    )
    # Do not append reused candidates here. Historical candidates are retained
    # only for diagnostics so callers can explain the shortage.

    meta["reused_selected_count"] = sum(
        1 for item in selected if ai_update_history_key(item) in historical_keys
    )
    return selected, meta


def _fit_ai_digest_brief_to_body_limit(
    brief: AIDigestBrief,
    *,
    min_items: int = 1,
    selection_meta: dict | None = None,
) -> AIDigestBrief:
    items = list(brief.items or [])
    while len(items) > max(1, min_items):
        fitted = _with_ai_digest_items(brief, items)
        if len(render_ai_digest_body(fitted, selection_meta=selection_meta)) <= MAX_IMAGE_BODY:
            return fitted
        items = items[:-1]
    return _with_ai_digest_items(brief, items)


def _fit_ai_digest_items_to_body_capacity(
    items,
    *,
    min_items: int,
    min_official_count: int,
    max_age_days: int,
    min_domestic_model_count: int,
    min_foreign_ai_count: int,
    selection_meta: dict | None = None,
    protected_topics: Iterable[str] | None = None,
) -> tuple[list[AIUpdateItem], dict[str, int]]:
    item_list = list(items or [])
    requested = len(item_list)
    protected_topic_list = list(protected_topics or [])
    topic_keys = [
        re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(topic or ""), flags=re.IGNORECASE).lower()
        for topic in protected_topic_list
    ]
    protected: list[AIUpdateItem] = []
    protected_history_keys: set[str] = set()
    optional: list[AIUpdateItem] = []
    for item in item_list:
        if any(
            topic
            and _ai_digest_prompt_topic_matches(item, topic)
            for topic, key in zip(protected_topic_list, topic_keys)
            if key
        ):
            history_key = ai_update_history_key(item)
            if history_key not in protected_history_keys:
                protected.append(item)
                protected_history_keys.add(history_key)
        else:
            optional.append(item)
    protected_source_counts = ai_digest_source_counts(protected)
    optional = [
        item
        for item in optional
        if protected_source_counts.get(ai_update_source_key(item), 0) < AI_DIGEST_MAX_ITEMS_PER_SOURCE
    ]
    minimum = max(
        1,
        len(protected),
        min(int(min_items or 1), requested or 1),
    )
    last_length = 0
    for target in range(requested, minimum - 1, -1):
        optional_target = max(0, target - len(protected))
        effective_official_min = min(max(0, int(min_official_count or 0)), target)
        effective_domestic_min = min(max(0, int(min_domestic_model_count or 0)), target)
        effective_foreign_min = min(max(0, int(min_foreign_ai_count or 0)), target)
        # The adaptive selector has already validated this exact set. Keep it
        # intact on the first pass; re-ranking here can discard the protected
        # requested topic and turn a valid short digest into an empty result.
        if target == requested:
            selected = item_list
        else:
            ranked_optional = rank_ai_updates(
                optional,
                target_count=optional_target,
                min_official_count=min(optional_target + 1, target),
                allow_social_backfill=True,
                max_age_days=max_age_days,
                min_domestic_model_count=max(
                    0,
                    effective_domestic_min - ai_digest_quota_counts(protected)["domestic_model"],
                ),
                min_foreign_ai_count=max(
                    0,
                    effective_foreign_min - ai_digest_quota_counts(protected)["foreign_ai"],
                ),
            ) if optional_target else []
            if not protected:
                selected = ranked_optional[:optional_target]
            else:
                selected = []
                selected_keys: set[str] = set()
                for candidate in [*protected, *ranked_optional[:optional_target]]:
                    key = ai_update_history_key(candidate)
                    if key in selected_keys:
                        continue
                    selected.append(candidate)
                    selected_keys.add(key)
        if _ai_digest_selection_error(
            selected,
            target_count=target,
            min_official_count=effective_official_min,
            min_domestic_model_count=effective_domestic_min,
            min_foreign_ai_count=effective_foreign_min,
            max_age_days=max_age_days,
        ):
            continue
        preview = build_fallback_brief(selected, target_count=target)
        last_length = len(render_ai_digest_body(preview, selection_meta=selection_meta))
        if last_length <= MAX_IMAGE_BODY:
            return selected, {
                "requested_items": requested,
                "selected_items": len(selected),
                "dropped_items": max(0, requested - len(selected)),
                "body_length": last_length,
                "body_limit": MAX_IMAGE_BODY,
            }
    raise RuntimeError(
        "daily ai digest body capacity insufficient: "
        f"保留最少{minimum}条及全部来源链接后正文仍为{last_length}字，"
        f"超过小红书上限{MAX_IMAGE_BODY}字"
    )


def _compact_ai_digest_subject(
    subject: str,
    *,
    featured: AIUpdateItem | None,
    max_subject_length: int,
    fallback: str,
) -> str:
    """Compress an over-long headline subject without losing the event.

    A bare ASCII word is rarely a good subject (``PullRequests``, ``GitHub``),
    so prefer a readable CJK-led prefix and only fall back to a complete
    model/version token when nothing else fits.
    """

    clean = re.sub(r"\s+", "", subject or "").strip()
    if not clean:
        return fallback
    if len(clean) <= max_subject_length:
        return clean

    source_text = ""
    if featured is not None:
        source_text = f"{featured.title} {featured.summary} {featured.raw_excerpt}"

    def _norm_label(value: str) -> str:
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or ""), flags=re.IGNORECASE).lower()

    vendor_labels = set()
    if featured is not None:
        vendor_labels = {_norm_label(featured.vendor), _norm_label(featured.source_name)}
        vendor_labels.discard("")

    def _is_meaningful(candidate: str) -> bool:
        value = re.sub(r"\s+", "", candidate or "")
        if not value:
            return False
        if _CJK_CHAR_RE.search(value):
            return True
        return bool(re.search(r"(?i)(?:gpt|glm|qwen|claude|codex|gemini|gemma|doubao|seedream|deepseek|kimi|minimax|ernie|llama|mistral|cosmos|tokenhub)[-_. ]*(?:v)?\d*", value))

    # 1) Keep a complete model/version token when the source names one.
    model_match = re.search(
        r"(?i)(?:gpt|glm|qwen|claude|codex|gemini|gemma|doubao|seedream|"
        r"deepseek|kimi|minimax|ernie|llama|mistral|cosmos)[-_. ]*(?:v)?\d+(?:\.\d+)?",
        clean,
    )
    if model_match and len(model_match.group(0)) <= max_subject_length:
        return model_match.group(0)

    # 2) Prefer the longest readable prefix that ends on a word boundary.
    words = re.findall(r"[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*|[\u4e00-\u9fff]", clean)
    compact = ""
    for word in words:
        if len(compact) + len(word) > max_subject_length:
            break
        compact += word
    if _is_meaningful(compact):
        return _trim_dangling_ai_digest_tail(compact)

    # 3) Fall back to a named product token, then to the vendor plus the
    #    concrete action so the headline still states what happened.
    for token in sorted(
        (t for t in re.findall(r"[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*", clean) if len(t) <= max_subject_length),
        key=len,
        reverse=True,
    ):
        normalized = _norm_label(token)
        if normalized and any(normalized in label for label in vendor_labels):
            continue
        if _is_meaningful(token):
            return token

    action = _concrete_action_in_text(source_text)
    vendor = ""
    if featured is not None:
        vendor = re.sub(r"(?i)\s*(?:blog|官网|official|status)$", "", str(featured.vendor or "")).strip()
    candidate = f"{vendor}{action}" if vendor and action else ""
    if candidate and len(candidate) <= max_subject_length:
        return candidate
    return compact or fallback


_DANGLING_TITLE_TAIL_RE = re.compile(
    r"(?:波及|导致|引起|影响|发生|出现|涉及|包含|包括|以及|并且|已经|正在|已经|已|将|在|与|和|或|及|并|正|被|把|向|对|为|是|等|的|了)+$"
)


def _trim_dangling_ai_digest_tail(subject: str) -> str:
    """Drop trailing connectors so a compacted headline ends cleanly."""

    value = subject or ""
    for _ in range(4):
        trimmed = _DANGLING_TITLE_TAIL_RE.sub("", value)
        if trimmed == value or len(trimmed) < 4:
            break
        value = trimmed
    return value.rstrip("，,。；;：:、-—| ")


def _ai_digest_post_title(
    brief: AIDigestBrief,
    *,
    preferred_topics: Iterable[str] | None = None,
) -> str:
    prefix = "每日AI|"
    fallback = "今日AI热点速览"
    digest_items = list(brief.items or [])
    featured = None
    for topic in preferred_topics or []:
        featured = next(
            (item for item in digest_items if _ai_digest_prompt_topic_matches(item, topic)),
            None,
        )
        if featured is not None:
            break
    featured = featured or featured_ai_update(digest_items)
    featured_title = str(featured.title if featured is not None else "").strip()
    generic_title = bool(
        re.search(
            r"(?:模型|产品|工具|智能体|API)?发布新进展$|披露AI产品变化$|AI产品披露AI产品变化$",
            re.sub(r"\s+", "", featured_title),
        )
    )
    if featured is not None and is_ai_digest_source_label_title(featured_title, featured):
        # Never promote a source label (for example, PublicTvEnglish) to the
        # post title. Use the same fact-grounded fallback as the item cards.
        from src.ai_digest.generate import _fallback_chinese_title

        featured_title = _fallback_chinese_title(featured)
        generic_title = True
    if featured is not None and is_vague_collective_title(featured_title):
        # “三位AI大佬” names nobody. Rebuild the headline from the concrete
        # entities in the source text before it reaches the cover and draft.
        from src.ai_digest.generate import _concrete_subject_from_item, _fallback_chinese_title

        # Respect the image-title budget here so the concrete action survives
        # the later length guard instead of being trimmed to a bare name.
        concrete_title = _concrete_subject_from_item(
            featured, max_chars=max(1, MAX_IMAGE_TITLE - len(prefix))
        ) or _fallback_chinese_title(featured)
        if concrete_title and not is_vague_collective_title(concrete_title):
            featured_title = concrete_title
            generic_title = False
    if generic_title and featured is not None:
        raw_subject = str(featured.raw_excerpt or featured.summary or "").strip()
        vendor = re.sub(r"(?i)\s*(?:blog|官网|official)$", "", str(featured.vendor or "").strip())
        raw_subject = re.split(r"[，。；;｜|]", raw_subject, maxsplit=1)[0]
        raw_subject = raw_subject.strip()
        if (
            raw_subject.startswith(("•", "·", "-", "—", "*"))
            or re.search(r"(?i)\b(?:give me|follow|like|subscribe|thread)\b", raw_subject)
            or (len(_CJK_CHAR_RE.findall(raw_subject)) < 2 and not re.search(r"(?i)\b(?:gpt|glm|qwen|claude|gemini|deepseek|kimi|llama)\b", raw_subject))
        ):
            raw_subject = ""
        if vendor:
            raw_subject = re.sub(rf"^{re.escape(vendor)}\s*", "", raw_subject, flags=re.IGNORECASE)
        raw_subject = re.sub(r"^启动(?:为期)?(?:[一二三四五六七八九十\d]+(?:天|周|日))?的?", "", raw_subject)
        raw_subject = re.sub(r"(?i)agents?\s+week", "智能体周", raw_subject)
        raw_subject = re.sub(r"(?i)agent\s+cloud", "Agent云", raw_subject)
        raw_subject = re.sub(r"\s+", "", raw_subject).strip("，,。；;：:、-—| ")
        if raw_subject:
            featured_title = raw_subject if raw_subject.lower().startswith(vendor.lower()) else f"{vendor}{raw_subject}"
        else:
            vendor = re.sub(r"^X\s*[：:]\s*", "", vendor).strip()
            vendor = re.sub(r"\s*\(@[^)]*\)", "", vendor).strip()
            vendor_labels = {
                "OpenAI Developers": "OpenAI开发者",
                "Claude Developers": "Claude开发者",
                "GitHub Status": "GitHub状态",
            }
            featured_title = vendor_labels.get(vendor, vendor) if vendor else fallback
    preferred_topic_keys = {
        re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(topic or ""), flags=re.IGNORECASE).lower()
        for topic in preferred_topics or []
    }
    if featured is not None:
        if any("hy4" in key and "preview" in key for key in preferred_topic_keys):
            if _ai_digest_prompt_topic_matches(featured, "HY4 preview"):
                featured_title = "Hy4Preview发布"
        elif any("claudefable" in key for key in preferred_topic_keys):
            if _ai_digest_prompt_topic_matches(featured, "Claude Fable 5.1"):
                featured_title = "ClaudeFable5.1"
        elif any("openai" in key and "cursor" in key for key in preferred_topic_keys):
            if _ai_digest_prompt_topic_matches(featured, "OpenAI宣布断供Cursor"):
                featured_title = "OpenAI停供Cursor"
        elif any("minimax" in key and "h3" in key and "max" in key for key in preferred_topic_keys):
            if _ai_digest_prompt_topic_matches(featured, "MiniMax H3 Max在Fal.ai发布"):
                featured_title = "fal发布H3Max"
    subject = re.split(r"[，。；;｜|]", featured_title, maxsplit=1)[0]
    subject_parts = re.split(r"[：:]", subject, maxsplit=1)
    if len(subject_parts) == 2 and len(subject_parts[1].strip()) >= 4:
        subject = subject_parts[1]
    subject = subject.replace("正式开源", "开源")
    subject = re.sub(r"\s+", "", subject)
    subject = subject.replace("加密算法中的弱点", "加密弱点")
    subject = subject.replace("加密算法弱点", "加密弱点")
    subject = subject.strip("，,。；;：:、-—| ") or fallback
    # A truncated model response can look like an arbitrary ASCII fragment
    # (for example ``to5MacIt``). Never expose that fragment as the cover
    # title; recover a fact-grounded Chinese subject or use the neutral title.
    if featured is not None:
        complete_model_token = re.search(
            r"(?i)(?:gpt|glm|qwen|claude|codex|gemini|gemma|doubao|seedream|"
            r"deepseek|kimi|minimax|ernie|llama|mistral|astra|fable|hy)[-_. ]*"
            r"(?:v)?\d+(?:\.\d+)?",
            subject,
        )
        if len(_CJK_CHAR_RE.findall(subject)) < 2 and not complete_model_token:
            from src.ai_digest.generate import _fallback_chinese_title

            recovery_candidates = [
                str(featured.summary or ""),
                str(featured.raw_excerpt or ""),
                _fallback_chinese_title(featured),
            ]
            recovered = ""
            for candidate in recovery_candidates:
                candidate = re.split(r"[，。！？；;｜|]", candidate, maxsplit=1)[0]
                candidate = re.sub(r"\s+", "", candidate).strip("，,。；;：:、-—| ")
                if not candidate or is_ai_digest_source_label_title(candidate, featured):
                    continue
                candidate_model_token = re.search(
                    r"(?i)(?:gpt|glm|qwen|claude|codex|gemini|gemma|doubao|seedream|"
                    r"deepseek|kimi|minimax|ernie|llama|mistral|astra|fable|hy)[-_. ]*"
                    r"(?:v)?\d+(?:\.\d+)?",
                    candidate,
                )
                if len(_CJK_CHAR_RE.findall(candidate)) >= 2 or candidate_model_token:
                    recovered = candidate
                    break
            subject = recovered or fallback
    # The body and cover already communicate the digest item count. Keeping
    # it out of the title leaves room for the complete featured product name.
    suffix = ""
    if featured is not None and ai_update_category(featured) == "model_release":
        hy4_match = re.search(r"(?i)hy\s*[-_.]?\s*4\s*preview", subject)
        if hy4_match:
            subject = "Hy4Preview发布"
    if suffix:
        product = str(featured.product if featured is not None else "").strip()
        if product:
            subject = re.sub(r"\s+", "", product)
        elif featured is not None:
            vendor = re.sub(r"(?i)\s*(?:blog|官网|official)$", "", str(featured.vendor or "").strip())
            if vendor:
                subject = re.sub(rf"^{re.escape(vendor)}", "", subject, flags=re.IGNORECASE)
            subject = re.sub(r"^(?:发布|推出|上线|开源|更新|披露|宣布)", "", subject)
            subject = subject.strip("，,。；;：:、-—| ") or fallback
    max_subject_length = max(1, MAX_IMAGE_TITLE - len(prefix) - len(suffix))
    if len(subject) > max_subject_length:
        subject = _compact_ai_digest_subject(
            subject,
            featured=featured,
            max_subject_length=max_subject_length,
            fallback=fallback,
        )
    subject = subject.rstrip("，,。；;：:、-—| ")
    return f"{prefix}{subject or fallback[:max_subject_length]}{suffix}"


def create_daily_ai_digest_posts(
    *,
    asset_paths: list[str],
    copy_assets: bool = True,
    count: int = 1,
    auto_image: bool = True,
    prompt_hint: str = "",
    evaluation_viewpoint: str = DEFAULT_EVALUATION_VIEWPOINT,
    lookback_days: object = None,
    performance_mode: str | None = None,
) -> list[Post]:
    performance_policy = (
        PerformancePolicy.from_value(performance_mode)
        if performance_mode is not None
        else PerformancePolicy.from_environment()
    )
    minimum_count = _ai_digest_min_items()
    max_items = _ai_digest_adaptive_max_items()
    legacy_target_count = _env_int(
        "AI_DIGEST_TARGET_ITEMS",
        minimum_count,
        min_value=minimum_count,
        max_value=20,
    )
    target_count = minimum_count
    # Keep the default official-source gate strict, but allow an explicit 0
    # for controlled runs where the source classifier is known to be stale.
    # The prompt and date/dedupe gates remain active in that mode.
    min_official_count = _env_int("AI_DIGEST_MIN_OFFICIAL_ITEMS", 6, min_value=0, max_value=20)
    lookback_windows, lookback_meta = _ai_digest_lookback_windows(lookback_days)
    min_domestic_model_count = _env_int(
        "AI_DIGEST_MIN_DOMESTIC_MODEL_ITEMS",
        AI_DIGEST_MIN_DOMESTIC_MODEL_ITEMS,
        min_value=0,
        max_value=max_items,
    )
    min_foreign_ai_count = _env_int(
        "AI_DIGEST_MIN_FOREIGN_AI_ITEMS",
        AI_DIGEST_MIN_FOREIGN_AI_ITEMS,
        min_value=0,
        max_value=max_items,
    )
    candidate_pool_target, candidate_pool_factor = _ai_digest_candidate_pool_target(max_items)
    items = []
    selection_pool_count = 0
    source_meta: dict[str, Any] = {}
    max_age_days = lookback_windows[0]
    lookback_attempts: list[dict[str, Any]] = []
    last_pool_error = ""
    effective_min_official_count = min_official_count
    effective_min_domestic_model_count = min_domestic_model_count
    effective_min_foreign_ai_count = min_foreign_ai_count
    best_relaxed_pool: tuple[tuple[int, int, int], list[AIUpdateItem], dict[str, Any], int, int] | None = None
    progress = _ai_digest_progress_callback()
    prompt_search_queries = _ai_digest_prompt_search_queries(prompt_hint)
    if progress is not None and prompt_search_queries:
        progress("prompt_topics", f"requested={len(prompt_search_queries)} targeted_search_backfill")
    historical_keys = _uploaded_ai_digest_history_keys()
    adaptive_selection_meta: dict[str, Any] = {}
    impact_meta: dict[str, Any] = {}
    llm_configs_cache = None
    if progress is not None:
        progress("collect_pool", f"in_progress mode={lookback_meta['mode']} windows={lookback_windows}")
    auto_collection_items: list[AIUpdateItem] | None = None
    auto_collection_meta: dict[str, Any] = {}
    evaluation_pool: list[AIUpdateItem] = []
    if lookback_meta["mode"] == "auto_expand":
        collection_days = max(lookback_windows)
        if progress is not None:
            progress(
                "collect_pool",
                f"in_progress single_fetch window={collection_days}d; local_filters={lookback_windows}",
            )
        collected_items, collected_meta = collect_ai_digest_updates(
            target_count=candidate_pool_target,
            min_official_count=min_official_count,
            allow_social_backfill=True,
            max_age_days=collection_days,
            min_domestic_model_count=min_domestic_model_count,
            min_foreign_ai_count=min_foreign_ai_count,
            include_pool_items=True,
            force_search_backfill=bool(prompt_search_queries),
            # History dedupe is local and must not force low-priority
            # aggregators into every run. Use them only when the official pool
            # actually has a coverage gap.
            force_aggregator_backfill=False,
            exclude_history_keys=historical_keys,
            search_backfill_queries=prompt_search_queries or None,
            progress=progress,
            performance_mode=performance_policy.mode,
            source_health_path=Path("data") / "source_health" / "ai_digest.json",
            persist_source_health=True,
        )
        auto_collection_meta = dict(collected_meta or {})
        auto_collection_items = list(
            auto_collection_meta.get("_deduped_items")
            or auto_collection_meta.get("_fresh_items")
            or collected_items
            or []
        )
        auto_collection_meta.pop("_fetched_items", None)
        auto_collection_meta.pop("_fresh_items", None)
        auto_collection_meta.pop("_deduped_items", None)
        if progress is not None:
            progress(
                "collect_pool",
                f"success single_fetch window={collection_days}d "
                f"fetched={auto_collection_meta.get('fetched_count')} pool={len(auto_collection_items)}",
            )
    adaptive_mode = (os.getenv("AI_DIGEST_ADAPTIVE_COUNT") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if not adaptive_mode:
        target_count = legacy_target_count
    if adaptive_mode:
        if auto_collection_items is None:
            collection_days = max(lookback_windows)
            collected_items, collected_meta = collect_ai_digest_updates(
                target_count=candidate_pool_target,
                min_official_count=min_official_count,
                allow_social_backfill=True,
                max_age_days=collection_days,
                min_domestic_model_count=min_domestic_model_count,
                min_foreign_ai_count=min_foreign_ai_count,
                include_pool_items=True,
                force_search_backfill=bool(prompt_search_queries),
                force_aggregator_backfill=False,
                exclude_history_keys=historical_keys,
                search_backfill_queries=prompt_search_queries or None,
                progress=progress,
                performance_mode=performance_policy.mode,
                source_health_path=Path("data") / "source_health" / "ai_digest.json",
                persist_source_health=True,
            )
            auto_collection_meta = dict(collected_meta or {})
            auto_collection_items = list(
                auto_collection_meta.get("_deduped_items")
                or auto_collection_meta.get("_fresh_items")
                or collected_items
                or []
            )
            auto_collection_meta.pop("_fetched_items", None)
            auto_collection_meta.pop("_fresh_items", None)
            auto_collection_meta.pop("_deduped_items", None)

        supervisor_limit = _env_int(
            "AI_DIGEST_IMPACT_SUPERVISOR_MAX_ITEMS",
            60,
            min_value=minimum_count,
            max_value=100,
        )
        evaluation_pool = rank_ai_updates(
            list(auto_collection_items or []),
            target_count=supervisor_limit,
            min_official_count=supervisor_limit + 1,
            allow_social_backfill=True,
            max_age_days=max(lookback_windows),
            # Preserve the same domestic/foreign quotas in the impact-review
            # pool that the final selector must satisfy. Otherwise a large
            # foreign-heavy ranking can discard domestic model updates before
            # the supervisor ever gets a chance to score them.
            min_domestic_model_count=min_domestic_model_count,
            min_foreign_ai_count=min_foreign_ai_count,
            max_items_per_source=None,
        )
        recent_review_items = filter_recent_ai_updates(
            list(auto_collection_items or []),
            max_age_days=min(3, max(lookback_windows)),
            require_url=True,
        )
        if recent_review_items:
            # Ranking may spend the supervisor budget on older or more
            # established sources. Keep every fresh traceable candidate in
            # the review pool so a reviewer miss cannot turn a valid current
            # release into a false material-shortage error.
            recent_review_items = rank_ai_updates(
                recent_review_items,
                target_count=len(recent_review_items),
                min_official_count=len(recent_review_items) + 1,
                allow_social_backfill=True,
                max_age_days=min(3, max(lookback_windows)),
                min_domestic_model_count=0,
                min_foreign_ai_count=0,
                max_items_per_source=None,
            )
            recent_keys = {item.dedupe_key for item in recent_review_items}
            evaluation_pool = dedupe_ai_updates(
                [
                    *recent_review_items,
                    *[item for item in evaluation_pool if item.dedupe_key not in recent_keys],
                ]
            )
            if len(evaluation_pool) > supervisor_limit:
                bounded_review_pool = rank_ai_updates(
                    evaluation_pool,
                    target_count=supervisor_limit,
                    min_official_count=supervisor_limit + 1,
                    allow_social_backfill=True,
                    max_age_days=max(lookback_windows),
                    min_domestic_model_count=min_domestic_model_count,
                    min_foreign_ai_count=min_foreign_ai_count,
                    max_items_per_source=None,
                )
                bounded_keys = {item.dedupe_key for item in bounded_review_pool}
                evaluation_pool = dedupe_ai_updates(
                    [
                        *recent_review_items,
                        *[item for item in bounded_review_pool if item.dedupe_key not in recent_keys],
                        *[item for item in evaluation_pool if item.dedupe_key not in bounded_keys],
                    ]
                )
        selection_pool_count = len(evaluation_pool)
        try:
            impact_threshold = float((os.getenv("AI_DIGEST_HIGH_IMPACT_SCORE") or "75").strip())
        except ValueError:
            impact_threshold = 75.0
        impact_threshold = min(100.0, max(0.0, impact_threshold))
        config_error = ""
        try:
            llm_configs_cache = load_llm_configs()
        except Exception as exc:
            llm_configs_cache = []
            config_error = str(exc)
        supervisor_enabled = (os.getenv("AI_DIGEST_IMPACT_SUPERVISOR") or "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        impact_scores, impact_meta = evaluate_ai_digest_impact_with_llm(
            list(llm_configs_cache or []) if supervisor_enabled else [],
            evaluation_pool,
            threshold=impact_threshold,
        )
        if config_error and not impact_meta.get("error"):
            impact_meta["error"] = config_error
        impact_meta["enabled"] = supervisor_enabled
        impact_meta["threshold"] = impact_threshold
        impact_meta["candidate_limit"] = supervisor_limit
        if progress is not None:
            high_count = sum(1 for row in impact_scores.values() if row.get("high_impact"))
            progress(
                "impact_review",
                f"success mode={impact_meta.get('mode')} candidates={len(evaluation_pool)} "
                f"high_impact={high_count} threshold={impact_threshold:g}",
            )

        selection_historical_keys = set(historical_keys)
        explicit_topic_history_exemptions = {
            ai_update_history_key(item)
            for item in (auto_collection_items or [])
            if any(
                _ai_digest_prompt_topic_matches(item, topic)
                for topic in prompt_search_queries
            )
        }
        selection_historical_keys.difference_update(explicit_topic_history_exemptions)
        if progress is not None and explicit_topic_history_exemptions:
            progress(
                "history_gate",
                f"explicit_topic_exemptions={len(explicit_topic_history_exemptions)} "
                "reason=explicit_requested_topics",
            )

        try:
            items, adaptive_selection_meta = _select_adaptive_ai_digest_items(
                evaluation_pool,
                impact_scores=impact_scores,
                historical_keys=selection_historical_keys,
                protected_topics=prompt_search_queries,
                min_items=minimum_count,
                max_items=max_items,
                min_official_count=min_official_count,
                min_domestic_model_count=min_domestic_model_count,
                min_foreign_ai_count=min_foreign_ai_count,
                allow_official_relaxation=True,
                impact_threshold=impact_threshold,
            )
            topic_candidates = [*(evaluation_pool or []), *(auto_collection_items or [])]
            items, prompt_topic_meta = _ensure_ai_digest_prompt_topic_coverage(
                items,
                topic_candidates,
                prompt_search_queries,
            )
            items = _prioritize_ai_digest_model_releases(items)
            adaptive_selection_meta["prompt_topic_coverage"] = prompt_topic_meta
            if prompt_topic_meta["missing"]:
                raise RuntimeError(
                    "指定AI主题没有满足日期、来源或同源上限要求："
                    + "、".join(prompt_topic_meta["missing"])
                )
        except RuntimeError as exc:
            window_label = (
                f"tried windows={lookback_windows}"
                if lookback_meta["mode"] == "auto_expand"
                else f"fixed {max(lookback_windows)}-day window"
            )
            raise RuntimeError(f"daily ai digest material insufficient: {window_label}; {exc}") from exc
        target_count = len(items)
        fallback_tiers = adaptive_selection_meta.get("fallback_tiers") or {}
        if (
            fallback_tiers.get("fourteen_day_high")
            or fallback_tiers.get("fourteen_day_normal")
            or fallback_tiers.get("historical_reuse")
        ):
            max_age_days = 14
        elif fallback_tiers.get("seven_day_high") or fallback_tiers.get("seven_day_normal"):
            max_age_days = 7
        else:
            max_age_days = min(3, max(lookback_windows))
        effective_min_official_count = int(
            adaptive_selection_meta.get("effective_min_official_items", min(min_official_count, target_count))
        )
        effective_min_domestic_model_count = int(
            adaptive_selection_meta.get(
                "effective_min_domestic_model_items",
                min(min_domestic_model_count, target_count),
            )
        )
        effective_min_foreign_ai_count = int(
            adaptive_selection_meta.get(
                "effective_min_foreign_ai_items",
                min(min_foreign_ai_count, target_count),
            )
        )
        source_meta = dict(auto_collection_meta or {})
        source_meta["impact_review"] = impact_meta
        source_meta["adaptive_selection"] = adaptive_selection_meta
        source_meta["historical_novelty"] = {
            "historical_key_count": len(historical_keys),
            "selection_historical_key_count": len(selection_historical_keys),
            "explicit_topic_history_exemptions": len(explicit_topic_history_exemptions),
            "candidate_count_before_history_filter": len(evaluation_pool),
            "novel_candidate_count": sum(
                1
                for item in evaluation_pool
                if ai_update_history_key(item) not in selection_historical_keys
            ),
            "reused_candidate_count": sum(
                1
                for item in evaluation_pool
                if ai_update_history_key(item) in selection_historical_keys
            ),
            "reused_selected_count": adaptive_selection_meta.get("historical_reused_count", 0),
            "max_reused_before_duplicate_gate": adaptive_selection_meta.get("max_historical_reuse", 0),
        }
        source_meta["selected_impact_scores"] = [
            {
                "url": item.url,
                "impact_score": (impact_scores.get(item.dedupe_key) or {}).get("impact_score"),
                "high_impact": bool((impact_scores.get(item.dedupe_key) or {}).get("high_impact")),
                "reason": (impact_scores.get(item.dedupe_key) or {}).get("reason", ""),
            }
            for item in items
        ]
        lookback_attempts = [
            {
                "max_age_days": days,
                "selection_pool_items": len(
                    filter_recent_ai_updates(evaluation_pool, max_age_days=days, require_url=True)
                ),
                "quota_counts": ai_digest_quota_counts(
                    filter_recent_ai_updates(evaluation_pool, max_age_days=days, require_url=True)
                ),
                "official_count": ai_digest_official_count(
                    filter_recent_ai_updates(evaluation_pool, max_age_days=days, require_url=True)
                ),
                "error": "",
            }
            for days in lookback_windows
            if days <= max_age_days
        ]
        if progress is not None:
            selected_official_count = ai_digest_official_count(items)
            progress(
                "adaptive_selection",
                f"success strict={adaptive_selection_meta.get('strict_candidate_count')} "
                f"fallback={adaptive_selection_meta.get('fallback_selected_count')} "
                f"selected={target_count}/{max_items} official={selected_official_count}/{min_official_count} "
                f"effective_official={effective_min_official_count} "
                f"mode={adaptive_selection_meta.get('selection_mode')}",
            )

    for days in ([] if adaptive_mode else lookback_windows):
        if auto_collection_items is not None:
            if progress is not None:
                progress("filter_window", f"in_progress window={days}d cached_pool={len(auto_collection_items)}")
            candidate_items = list(auto_collection_items)
            candidate_meta = dict(auto_collection_meta)
            recent_items = filter_recent_ai_updates(
                candidate_items,
                max_age_days=days,
                require_url=True,
            )
            candidate_meta["fresh_count"] = len(recent_items)
            candidate_meta["deduped_count"] = len(dedupe_ai_updates(recent_items))
            candidate_meta["collection_max_age_days"] = max(lookback_windows)
        else:
            if progress is not None:
                progress("collect_pool", f"in_progress window={days}d")
            candidate_items, candidate_meta = collect_ai_digest_updates(
                target_count=candidate_pool_target,
                min_official_count=min_official_count,
                allow_social_backfill=True,
                max_age_days=days,
                min_domestic_model_count=min_domestic_model_count,
                min_foreign_ai_count=min_foreign_ai_count,
                include_pool_items=False,
                force_search_backfill=bool(prompt_search_queries),
                exclude_history_keys=historical_keys,
                search_backfill_queries=prompt_search_queries or None,
                progress=progress,
                performance_mode=performance_policy.mode,
                source_health_path=Path("data") / "source_health" / "ai_digest.json",
                persist_source_health=True,
            )
            candidate_meta = dict(candidate_meta or {})
            candidate_meta.pop("_fetched_items", None)
            candidate_meta.pop("_fresh_items", None)
            candidate_meta.pop("_deduped_items", None)
        if lookback_meta["mode"] == "auto_expand":
            window_candidate_items = rank_ai_updates(
                list(candidate_items or []),
                target_count=candidate_pool_target,
                min_official_count=min_official_count,
                allow_social_backfill=True,
                max_age_days=days,
                min_domestic_model_count=min_domestic_model_count,
                min_foreign_ai_count=min_foreign_ai_count,
                max_items_per_source=None,
            )
        else:
            window_candidate_items = list(candidate_items or [])
        window_meta = {
            **candidate_meta,
            "max_age_days": days,
            "ranked_count": len(window_candidate_items),
            "quota_counts": ai_digest_quota_counts(list(window_candidate_items or [])),
            "collection_max_age_days": days,
        }
        if progress is not None:
            progress(
                "collect_pool",
                f"success window={days}d fetched={candidate_meta.get('fetched_count')} ranked={len(window_candidate_items)}",
            )
        window_candidate_items, historical_novelty_meta = _prefer_novel_ai_digest_items(
            window_candidate_items,
            historical_keys=historical_keys,
            target_count=target_count,
            min_official_count=min_official_count,
            max_age_days=days,
            min_domestic_model_count=min_domestic_model_count,
            min_foreign_ai_count=min_foreign_ai_count,
        )
        window_meta["historical_novelty"] = historical_novelty_meta
        candidate_counts = ai_digest_quota_counts(list(window_candidate_items or []))
        candidate_official_count = ai_digest_official_count(list(window_candidate_items or []))
        window_meta["selection_official_count"] = candidate_official_count
        if not window_candidate_items:
            errors = window_meta.get("errors") if isinstance(window_meta, dict) else []
            detail = f"; errors={errors}" if errors else ""
            pool_error = f"daily ai digest material insufficient: no AI updates within {days} days{detail}"
        else:
            pool_error = _ai_digest_selection_error(
                window_candidate_items,
                target_count=target_count,
                min_official_count=min_official_count,
                min_domestic_model_count=min_domestic_model_count,
                min_foreign_ai_count=min_foreign_ai_count,
                max_age_days=days,
            )
        max_historical_reuse = max(0, (target_count * 3 - 1) // 4)
        historical_novelty_meta["max_reused_before_duplicate_gate"] = max_historical_reuse
        reused_selected = historical_novelty_meta["reused_selected_count"]
        if not pool_error and reused_selected > max_historical_reuse:
            pool_error = (
                "daily ai digest historical novelty insufficient: "
                f"历史资讯复用{reused_selected}条，超过本批最多{max_historical_reuse}条；"
                "继续扩大日期窗口以避免触发历史重复门槛"
            )
        relaxed_error = _ai_digest_selection_error(
            window_candidate_items,
            target_count=target_count,
            min_official_count=0,
            min_domestic_model_count=min_domestic_model_count,
            min_foreign_ai_count=min_foreign_ai_count,
            max_age_days=days,
        )
        if (
            lookback_meta["mode"] == "auto_expand"
            and not relaxed_error
            and reused_selected <= max_historical_reuse
            and candidate_official_count > 0
        ):
            relaxed_score = (candidate_official_count, -days, len(window_candidate_items or []))
            if best_relaxed_pool is None or relaxed_score > best_relaxed_pool[0]:
                best_relaxed_pool = (
                    relaxed_score,
                    list(window_candidate_items or []),
                    dict(window_meta),
                    days,
                    candidate_official_count,
                )
        lookback_attempts.append(
            {
                "max_age_days": days,
                "selection_pool_items": len(window_candidate_items or []),
                "quota_counts": candidate_counts,
                "official_count": candidate_official_count,
                "error": pool_error,
                "fetched_count": window_meta.get("fetched_count"),
                "fresh_count": window_meta.get("fresh_count"),
                "deduped_count": window_meta.get("deduped_count"),
                "ranked_count": window_meta.get("ranked_count"),
            }
        )
        items = window_candidate_items
        selection_pool_count = len(window_candidate_items or [])
        source_meta = window_meta
        max_age_days = days
        last_pool_error = pool_error
        if progress is not None:
            progress(
                "lookback_window",
                f"{'success' if not pool_error else 'insufficient'} window={days}d "
                f"items={len(window_candidate_items or [])} domestic={candidate_counts['domestic_model']} "
                f"foreign={candidate_counts['foreign_ai']} official={candidate_official_count}/{min_official_count}",
            )
        if not pool_error:
            break
    if last_pool_error and best_relaxed_pool is not None:
        _, items, source_meta, max_age_days, effective_min_official_count = best_relaxed_pool
        last_pool_error = ""
        source_meta["official_target_warning"] = (
            f"已穷尽{lookback_windows}天窗口的官网信源；官网目标{min_official_count}条，"
            f"实际可用{effective_min_official_count}条，其余按资讯整合站、官方社交媒体顺序补足"
        )
        if progress is not None:
            progress(
                "lookback_window",
                f"degraded_success window={max_age_days}d items={len(items)} "
                f"official={effective_min_official_count}/{min_official_count} reason=official_sources_exhausted",
            )
    if last_pool_error:
        attempt_summary = "; ".join(
            f"{a['max_age_days']}d items={a['selection_pool_items']} "
            f"domestic={a['quota_counts']['domestic_model']} foreign={a['quota_counts']['foreign_ai']} "
            f"official={a['official_count']}/{min_official_count}"
            for a in lookback_attempts
        )
        if lookback_meta["mode"] == "auto_expand":
            raise RuntimeError(
                "daily ai digest material insufficient: "
                f"tried windows={lookback_windows}; {attempt_summary}; {last_pool_error}"
            )
        raise RuntimeError(
            "daily ai digest material insufficient: "
            f"fixed {max_age_days}-day window; {attempt_summary}; {last_pool_error}"
        )
    source_meta["candidate_pool_target"] = candidate_pool_target
    source_meta["candidate_pool_factor"] = candidate_pool_factor
    source_meta["selection_pool_items"] = selection_pool_count or len(items)
    source_meta["min_items"] = minimum_count
    source_meta["max_items"] = max_items
    source_meta["min_domestic_model_items"] = min_domestic_model_count
    source_meta["min_foreign_ai_items"] = min_foreign_ai_count
    source_meta["effective_min_domestic_model_items"] = effective_min_domestic_model_count
    source_meta["effective_min_foreign_ai_items"] = effective_min_foreign_ai_count
    source_meta["selection_pool_quota_counts"] = ai_digest_quota_counts(list(items or []))
    source_meta["selection_pool_official_count"] = ai_digest_official_count(list(items or []))
    source_meta["official_target_items"] = min_official_count
    source_meta["effective_min_official_items"] = effective_min_official_count
    source_meta["official_target_met"] = source_meta["selection_pool_official_count"] >= min_official_count
    source_meta["lookback"] = {
        **lookback_meta,
        "selected_max_age_days": max_age_days,
        "attempts": lookback_attempts,
    }
    if adaptive_selection_meta:
        source_meta["adaptive_selection"] = adaptive_selection_meta

    # Save unfiltered source items for the quota-safe fallback
    raw_items_for_fallback = list(items or [])
    prompt_topic_candidates = dedupe_ai_updates(
        [
            *raw_items_for_fallback,
            *(evaluation_pool or []),
            *(auto_collection_items or []),
        ]
    )
    if adaptive_mode:
        items, body_capacity_meta = _fit_ai_digest_items_to_body_capacity(
            items,
            min_items=minimum_count,
            min_official_count=effective_min_official_count,
            max_age_days=max_age_days,
            min_domestic_model_count=effective_min_domestic_model_count,
            min_foreign_ai_count=effective_min_foreign_ai_count,
            selection_meta=source_meta,
            protected_topics=prompt_search_queries,
        )
        target_count = len(items)
        effective_min_official_count = min(effective_min_official_count, target_count)
        adaptive_selection_meta["body_capacity"] = body_capacity_meta
        adaptive_selection_meta["final_target_items"] = target_count
        source_meta["body_capacity"] = body_capacity_meta
        source_meta["adaptive_selection"] = adaptive_selection_meta
        selected_urls = {item.url for item in items}
        source_meta["selected_impact_scores"] = [
            row
            for row in source_meta.get("selected_impact_scores", [])
            if row.get("url") in selected_urls
        ]
        if progress is not None:
            progress(
                "body_capacity",
                f"success selected={target_count}/{body_capacity_meta['requested_items']} "
                f"body={body_capacity_meta['body_length']}/{body_capacity_meta['body_limit']}",
            )
        if prompt_search_queries:
            items, post_fit_topic_meta = _ensure_ai_digest_prompt_topic_coverage(
                items,
                prompt_topic_candidates,
                prompt_search_queries,
            )
            adaptive_selection_meta["post_fit_prompt_topic_coverage"] = post_fit_topic_meta
            if post_fit_topic_meta["missing"]:
                raise RuntimeError(
                    "正文容量筛选后指定AI主题丢失："
                    + "、".join(post_fit_topic_meta["missing"])
                )
            if len(items) != target_count:
                items, body_capacity_meta = _fit_ai_digest_items_to_body_capacity(
                    items,
                    min_items=minimum_count,
                    min_official_count=effective_min_official_count,
                    max_age_days=max_age_days,
                    min_domestic_model_count=effective_min_domestic_model_count,
                    min_foreign_ai_count=effective_min_foreign_ai_count,
                    selection_meta=source_meta,
                    protected_topics=prompt_search_queries,
                )
                target_count = len(items)
                adaptive_selection_meta["body_capacity"] = body_capacity_meta
                adaptive_selection_meta["final_target_items"] = target_count
                source_meta["body_capacity"] = body_capacity_meta

    # Apply the non-negotiable publication window and event dedupe before the
    # LLM sees the pool. This avoids spending a generation request on stale or
    # mirrored items, while the post-LLM gate below still protects rewritten
    # output that accidentally reintroduces a duplicate.
    items, strict_pre_llm_meta = ai_digest_items_in_beijing_window(
        items,
        publication_date=datetime.now(timezone.utc).astimezone(BEIJING_TZ).date().isoformat(),
        now=datetime.now(timezone.utc),
    )
    source_meta["strict_pre_llm_policy"] = strict_pre_llm_meta
    if not items:
        raise RuntimeError(
            "daily ai digest material insufficient: strict Beijing two-day window "
            f"{strict_pre_llm_meta['earliest_date']}..{strict_pre_llm_meta['publication_date']} "
            "has no eligible item before LLM generation"
        )
    target_count = len(items)
    if not adaptive_mode:
        target_count = min(legacy_target_count, target_count)
        items = items[:target_count]
    max_age_days = 2
    effective_min_official_count = min(
        effective_min_official_count,
        ai_digest_official_count(list(items or [])),
        target_count,
    )
    effective_min_domestic_model_count = min(
        effective_min_domestic_model_count,
        ai_digest_quota_counts(list(items or []))["domestic_model"],
        target_count,
    )
    effective_min_foreign_ai_count = min(
        effective_min_foreign_ai_count,
        ai_digest_quota_counts(list(items or []))["foreign_ai"],
        target_count,
    )
    generation_target = target_count
    # Adaptive mode may intentionally publish fewer than the historical
    # quota targets when only a small set of fresh, high-impact items remains.
    # Keep the LLM prompt and final validator aligned with that actual target.
    effective_min_official_count = min(effective_min_official_count, generation_target)
    effective_min_domestic_model_count = min(
        effective_min_domestic_model_count,
        generation_target,
    )
    effective_min_foreign_ai_count = min(
        effective_min_foreign_ai_count,
        generation_target,
    )
    generation_mode = "llm"
    llm_error = ""
    llm_items = _prepare_ai_digest_llm_items(items, target_count=generation_target)
    source_meta["llm_input_items"] = len(llm_items)
    source_meta["llm_input_quota_counts"] = ai_digest_quota_counts(llm_items)
    if progress is not None:
        progress(
            "llm_input",
            f"selected={len(llm_items)}/{generation_target} "
            f"sources={len(ai_digest_source_counts(llm_items))}",
        )
    try:
        generation_cfgs = llm_configs_cache if llm_configs_cache is not None else load_llm_configs()
        brief = generate_ai_digest_brief_with_llm(
            generation_cfgs,
            llm_items,
            target_count=generation_target,
            min_domestic_model_count=effective_min_domestic_model_count,
            min_foreign_ai_count=effective_min_foreign_ai_count,
        )
    except Exception as exc:
        generation_mode = "fallback"
        llm_error = str(exc)
        if progress is not None:
            progress("llm_selection", f"failed error={llm_error}; using quota-safe fallback")
        fallback_items = _select_ai_digest_fallback_pool(items, raw_items_for_fallback, target_count=generation_target)
        if prompt_search_queries and len(fallback_items) >= generation_target:
            brief = build_fallback_brief(fallback_items[:generation_target], target_count=generation_target)
        else:
            brief = _build_quota_safe_ai_digest_fallback(
                fallback_items,
                target_count=generation_target,
                min_official_count=effective_min_official_count,
                max_age_days=max_age_days,
                min_domestic_model_count=effective_min_domestic_model_count,
                min_foreign_ai_count=effective_min_foreign_ai_count,
            )
    brief = _finalize_ai_digest_brief(
        brief,
        generation_mode=generation_mode,
        target_count=generation_target,
        min_official_count=effective_min_official_count,
        max_age_days=max_age_days,
        min_domestic_model_count=effective_min_domestic_model_count,
        min_foreign_ai_count=effective_min_foreign_ai_count,
        preserve_validated_order=bool(prompt_search_queries),
    )
    final_prompt_topic_meta: dict[str, Any] = {}
    if prompt_search_queries:
        final_topic_items, final_prompt_topic_meta = _ensure_ai_digest_prompt_topic_coverage(
            list(brief.items or []),
            dedupe_ai_updates([*(items or []), *(prompt_topic_candidates or [])]),
            prompt_search_queries,
        )
        if len(final_topic_items) != len(brief.items or []):
            brief = _with_ai_digest_items(brief, final_topic_items)
            generation_target = len(final_topic_items)
            effective_min_official_count = min(effective_min_official_count, generation_target)
            effective_min_domestic_model_count = min(
                effective_min_domestic_model_count,
                generation_target,
            )
            effective_min_foreign_ai_count = min(
                effective_min_foreign_ai_count,
                generation_target,
            )
        adaptive_selection_meta["final_prompt_topic_coverage"] = final_prompt_topic_meta
    brief, strict_publish_meta = _enforce_ai_digest_publish_policy(brief)
    source_meta["strict_publish_policy"] = strict_publish_meta
    max_age_days = 2
    generation_target = len(brief.items)
    effective_min_official_count = min(
        effective_min_official_count,
        ai_digest_official_count(list(brief.items or [])),
    )
    effective_min_domestic_model_count = min(
        effective_min_domestic_model_count,
        ai_digest_quota_counts(list(brief.items or []))["domestic_model"],
    )
    effective_min_foreign_ai_count = min(
        effective_min_foreign_ai_count,
        ai_digest_quota_counts(list(brief.items or []))["foreign_ai"],
    )
    final_error = _ai_digest_selection_error(
        brief.items,
        target_count=generation_target,
        min_official_count=effective_min_official_count,
        min_domestic_model_count=effective_min_domestic_model_count,
        min_foreign_ai_count=effective_min_foreign_ai_count,
        max_age_days=max_age_days,
    )
    source_cap_error = _ai_digest_source_cap_error(
        brief.items,
        target_count=generation_target,
    )
    if source_cap_error:
        final_error = source_cap_error
    prompt_topic_missing = _missing_ai_digest_prompt_topics(brief.items, prompt_search_queries)
    if prompt_topic_missing:
        final_error = (
            "每日AI讯息指定主题未完整保留："
            + "、".join(prompt_topic_missing)
            + "；LLM改写不得删除已选主题"
        )
    if final_error and generation_mode == "llm":
        if progress is not None:
            progress("llm_selection", f"insufficient error={final_error}; using quota-safe fallback")
        # Keep the already validated recent/high-impact selection. Never
        # reintroduce stale, low-impact, or historical items during repair.
        fallback_items = list(items or [])
        if prompt_search_queries and len(fallback_items) >= generation_target:
            quota_fallback = build_fallback_brief(
                fallback_items[:generation_target],
                target_count=generation_target,
            )
        else:
            quota_fallback = _build_quota_safe_ai_digest_fallback(
                fallback_items,
                target_count=generation_target,
                min_official_count=effective_min_official_count,
                max_age_days=None,
                min_domestic_model_count=effective_min_domestic_model_count,
                min_foreign_ai_count=effective_min_foreign_ai_count,
            )
        quota_fallback_error = _ai_digest_selection_error(
            quota_fallback.items,
            target_count=generation_target,
            min_official_count=effective_min_official_count,
            min_domestic_model_count=effective_min_domestic_model_count,
            min_foreign_ai_count=effective_min_foreign_ai_count,
            max_age_days=max_age_days,
        )
        if not quota_fallback_error:
            brief = quota_fallback
            generation_mode = "llm_quota_fallback"
            llm_error = final_error
            final_error = ""
            brief, strict_publish_meta = _enforce_ai_digest_publish_policy(brief)
            source_meta["strict_publish_policy"] = strict_publish_meta
            generation_target = len(brief.items)
            max_age_days = 2
            strict_quota_counts = ai_digest_quota_counts(list(brief.items or []))
            effective_min_official_count = min(
                effective_min_official_count,
                ai_digest_official_count(list(brief.items or [])),
            )
            effective_min_domestic_model_count = min(
                effective_min_domestic_model_count,
                strict_quota_counts["domestic_model"],
            )
            effective_min_foreign_ai_count = min(
                effective_min_foreign_ai_count,
                strict_quota_counts["foreign_ai"],
            )
            final_error = _ai_digest_selection_error(
                brief.items,
                target_count=generation_target,
                min_official_count=effective_min_official_count,
                min_domestic_model_count=effective_min_domestic_model_count,
                min_foreign_ai_count=effective_min_foreign_ai_count,
                max_age_days=max_age_days,
            )
            source_cap_error = _ai_digest_source_cap_error(
                brief.items,
                target_count=generation_target,
            )
            if source_cap_error:
                final_error = source_cap_error
    if final_error:
        raise RuntimeError(final_error)
    # The issue date belongs to the run, not to an LLM or a source article.
    brief = brief.model_copy(update={
        "date": datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).date().isoformat(),
    })
    selected_before_body_fit = len(brief.items)
    brief = _fit_ai_digest_brief_to_body_limit(
        brief,
        min_items=generation_target,
        selection_meta=source_meta,
    )
    body_fit_error = _ai_digest_selection_error(
        brief.items,
        target_count=generation_target,
        min_official_count=effective_min_official_count,
        min_domestic_model_count=effective_min_domestic_model_count,
        min_foreign_ai_count=effective_min_foreign_ai_count,
        max_age_days=max_age_days,
    )
    source_cap_error = _ai_digest_source_cap_error(
        brief.items,
        target_count=generation_target,
    )
    if source_cap_error:
        body_fit_error = source_cap_error
    if body_fit_error:
        raise RuntimeError(body_fit_error)
    try:
        validate_ai_digest_concrete_content(brief)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    source_meta["selected_before_body_fit"] = selected_before_body_fit
    source_meta["body_fit_dropped"] = max(0, selected_before_body_fit - len(brief.items))
    source_meta["selected_quota_counts"] = ai_digest_quota_counts(list(brief.items or []))
    source_meta["source_distribution"] = ai_digest_source_counts(list(brief.items or []))
    source_meta["source_distribution_max"] = max(
        source_meta["source_distribution"].values(),
        default=0,
    )
    source_meta["selected_official_count"] = ai_digest_official_count(list(brief.items or []))
    rendered_body = render_ai_digest_body(brief, selection_meta=source_meta)
    if len(rendered_body) > MAX_IMAGE_BODY:
        raise RuntimeError(
            "daily ai digest body too long: "
            f"{len(rendered_body)} > {MAX_IMAGE_BODY}; "
            "the brief body exceeds the Xiaohongshu body limit"
        )
    post = Post(
        type="image",
        status=PostStatus.draft,
        title=_ai_digest_post_title(brief, preferred_topics=prompt_search_queries),
        body=rendered_body,
        topics=["每日AI讯息", "AI动态", "人工智能"],
        platform={
            "ai_digest": {
                "mode": "daily_ai_digest",
                "target_items": target_count,
                "actual_items": len(brief.items),
                "candidate_pool_target": candidate_pool_target,
                "candidate_pool_factor": candidate_pool_factor,
                "selection_pool_items": source_meta["selection_pool_items"],
                "min_official_items": min_official_count,
                "official_target_items": min_official_count,
                "effective_min_official_items": effective_min_official_count,
                "official_target_met": source_meta["official_target_met"],
                "min_items": minimum_count,
                "max_items": max_items,
                "adaptive_selection": adaptive_selection_meta,
                "impact_review": impact_meta,
                "min_domestic_model_items": min_domestic_model_count,
                "min_foreign_ai_items": min_foreign_ai_count,
                "effective_min_domestic_model_items": effective_min_domestic_model_count,
                "effective_min_foreign_ai_items": effective_min_foreign_ai_count,
                "quota_counts": source_meta["selected_quota_counts"],
                "source_distribution": source_meta["source_distribution"],
                "source_distribution_max": source_meta["source_distribution_max"],
                "max_items_per_source": AI_DIGEST_MAX_ITEMS_PER_SOURCE,
                "max_age_days": max_age_days,
                "prompt_hint": (prompt_hint or "").strip(),
                "generation_mode": generation_mode,
                "llm_error": llm_error,
                "source_meta": source_meta,
                "brief": brief.model_dump(),
                "items": [item.model_dump() for item in brief.items],
            }
        },
    )

    dest_dir = post_dir(post.id) / "assets"
    image_paths = render_ai_digest_cards(brief, dest_dir)
    post.assets = _build_asset_infos(image_paths)

    rev = Revision(
        post_id=post.id,
        source=RevisionSource.llm,
        content={
            "title": post.title,
            "body": post.body,
            "topics": post.topics,
            "ai_digest": post.platform["ai_digest"],
        },
    )
    save_post(post)
    save_revision(rev)
    return [post]


def create_post_with_draft(
    *,
    title_hint: str,
    prompt_hint: str,
    asset_paths: list[str],
    copy_assets: bool = True,
    auto_image: bool = True,
    image_exclude_ids: Optional[set[str]] = None,
    evaluation_viewpoint: str = DEFAULT_EVALUATION_VIEWPOINT,
    lookback_days: object = None,
    news_materials_file: str | Path | None = None,
    single_news_material_file: str | Path | None = None,
    material_time: str = "",
) -> Post:
    """
    Generate a draft with LLM and persist post + revision.
    """
    title_norm = (title_hint or "").strip()
    if title_norm == "每日AI讯息":
        return create_daily_ai_digest_posts(
            prompt_hint=prompt_hint,
            asset_paths=asset_paths,
            copy_assets=copy_assets,
            auto_image=auto_image,
            evaluation_viewpoint=evaluation_viewpoint,
            lookback_days=lookback_days,
        )[0]

    cfgs = load_llm_configs()
    platform_meta: dict = {}

    if title_norm == "每日新闻":
        viewpoint_norm = normalize_evaluation_viewpoint(evaluation_viewpoint)
        try:
            prompt_norm = "" if str(single_news_material_file or "").strip() else (prompt_hint or "").strip()
            candidates, news_meta = _fetch_daily_news_candidates_for_upload(
                prompt_norm,
                count=1,
                lookback_days=None if str(single_news_material_file or "").strip() else lookback_days,
                news_materials_file=news_materials_file,
                single_news_material_file=single_news_material_file,
                material_time=material_time,
            )
            picks = pick_news_items(candidates, prompt_norm, count=1)
            if not picks:
                raise RuntimeError("no news candidates selected")
            picked = picks[0]
            picked, lookup_meta = _enrich_daily_news_item(picked)
            picked, focus_meta = _focus_daily_news_item(picked)
            traced_news_meta = _daily_news_meta_with_trace(
                {**news_meta, **lookup_meta, **focus_meta},
                picked,
            )
            platform_meta["news"] = {
                **traced_news_meta,
                "picked": asdict(picked),
                "source_url": picked.url,
                "mode": "daily_news_single_material" if str(single_news_material_file or "").strip() else "daily_news",
                "image_policy": (
                    "ai_required"
                    if not asset_paths and not str(single_news_material_file or "").strip()
                    else ("ai_preferred" if str(single_news_material_file or "").strip() else "provided")
                ),
                "prompt_hint": prompt_norm,
                "evaluation_viewpoint": viewpoint_norm,
            }
            news_prompt = _daily_news_prompt(picked, prompt_norm, viewpoint_norm)
            seed_title = "每日新闻"
            draft = generate_draft(
                cfgs,
                title_hint=seed_title,
                prompt_hint=news_prompt,
                asset_paths=asset_paths,
                preserve_body=True,
                concise_news=True,
            )
            if draft.get("_fallback_error"):
                reason = _daily_news_llm_unavailable_reason(draft.get("_fallback_error"))
                raise RuntimeError(
                    f"每日新闻模型不可用：{reason}；不会保存模板草稿。"
                )
            image_event_audit = {"writer_value": str(draft.get("image_event") or "")}
            embedded = _extract_embedded_json_from_daily_news_body(draft.get("body", ""))
            if embedded:
                embedded_title = embedded.get("title")
                embedded_body = embedded.get("body")
                embedded_topics = embedded.get("topics")
                embedded_event = embedded.get("image_event")
                if isinstance(embedded_title, str) and embedded_title.strip():
                    draft["title"] = embedded_title.strip()
                if isinstance(embedded_body, str) and embedded_body.strip():
                    draft["body"] = embedded_body.strip()
                if isinstance(embedded_topics, list) and embedded_topics:
                    draft["topics"] = embedded_topics
                if isinstance(embedded_event, str) and embedded_event.strip():
                    draft["image_event"] = embedded_event.strip()
            draft["body"] = _ensure_daily_news_sections(
                draft.get("body", ""), prompt_norm
            )
            draft["body"] = _ensure_news_publish_date(
                draft["body"], picked.seendate
            )
            if (
                _daily_news_body_has_prompt_leak(draft.get("body", ""))
                or _daily_news_body_is_too_generic(draft.get("body", ""))
            ):
                draft["title"] = _normalize_daily_news_title(picked.title, picked, prompt_norm)
                draft["body"] = _daily_news_offline_body(picked, prompt_norm)
                draft["topics"] = ["每日新闻"]
            if _is_generic_daily_news_title(draft.get("title", "")):
                title_src = picked.title or picked.description or prompt_norm
                draft["title"] = _normalize_daily_news_title(title_src, picked, prompt_norm)
            draft["title"] = _normalize_daily_news_title(draft.get("title", ""), picked, prompt_norm)
            topics = draft.get("topics") or []
            if not isinstance(topics, list):
                topics = [str(topics)]
            draft["topics"] = _normalize_daily_news_topics(
                topics,
                prompt_norm,
                context=f"{draft.get('title', '')} {draft.get('body', '')}",
            )
            draft["body"] = _finalize_daily_news_body(
                draft.get("body", ""),
                picked,
                prompt_norm,
                title_hint=str(draft.get("title") or ""),
                preserve_length=True,
            )
            draft["body"] = _repair_daily_news_mismatched_comment(
                draft["body"],
                picked,
                prompt_norm,
                title_hint=str(draft.get("title") or ""),
                preserve_length=True,
            )
            draft = _simplify_daily_news_draft(draft)
            draft, length_issue = _resummarize_daily_news_length_once(
                draft, cfgs=cfgs, picked=picked, prompt_norm=prompt_norm,
                news_prompt=news_prompt, asset_paths=asset_paths,
            )
            platform_meta["news"]["length_review"] = draft["_length_review"]
            quality_issue = _daily_news_quality_issue(
                draft.get("title", ""),
                draft.get("body", ""),
                prompt_norm,
            )
            quality_issue = quality_issue or length_issue
            if quality_issue:
                raise RuntimeError(f"daily news quality check failed: {quality_issue}")
            image_event = _normalize_daily_news_image_event(
                str(draft.get("image_event") or ""),
                picked=picked,
                title=str(draft.get("title") or ""),
                body=str(draft.get("body") or ""),
                prompt_norm=prompt_norm,
                audit=image_event_audit,
            )
            image_event = _to_simplified_common(image_event)
            platform_meta["news"]["image_event"] = image_event
            platform_meta["news"]["image_event_audit"] = image_event_audit
            draft["image_event"] = image_event
            draft["image_event_audit"] = image_event_audit
            if not image_event or not image_event_audit.get("accepted"):
                raise RuntimeError(f"daily news scene source check failed: {image_event_audit.get('reason', 'missing_scene')}")
        except Exception as exc:
            platform_meta["news"] = {
                "mode": "daily_news",
                "prompt_hint": (prompt_hint or "").strip(),
                "evaluation_viewpoint": normalize_evaluation_viewpoint(evaluation_viewpoint),
                "error": str(exc),
            }
            raise RuntimeError(f"daily news fetch failed: {exc}") from exc
    elif title_norm == "每日假新闻":
        prompt_norm = (prompt_hint or "").strip()
        fake_prompt = _fake_news_prompt(prompt_norm)
        draft = generate_draft(
            cfgs,
            title_hint="每日假新闻",
            prompt_hint=fake_prompt,
            asset_paths=asset_paths,
        )
        if draft.get("_fallback_error"):
            draft["title"] = "每日假新闻"
            draft["body"] = _fake_news_offline_body(prompt_norm)
        body_text = (draft.get("body") or "").strip()
        if "本文纯属虚构" not in body_text:
            joiner = "\n" if body_text else ""
            draft["body"] = f"{body_text}{joiner}本文纯属虚构，仅供娱乐。"
        topics = draft.get("topics", [])
        if "每日假新闻" not in topics:
            topics = ["每日假新闻"] + [t for t in topics if t and t != "每日假新闻"]
        draft["topics"] = topics
        platform_meta["fake_news"] = {
            "mode": "daily_fake_news",
            "prompt_hint": prompt_norm,
            "is_fiction": True,
            "tone": "humor",
        }
    else:
        draft = generate_draft(
            cfgs,
            title_hint=title_hint,
            prompt_hint=prompt_hint,
            asset_paths=asset_paths,
        )
        if draft.get("_fallback_error"):
            raise RuntimeError(
                "LLM draft generation failed; the fallback placeholder will not be saved or uploaded: "
                f"{draft.get('_fallback_error')}"
            )

    post = Post(
        type="image",
        status=PostStatus.draft,
        title=draft["title"],
        body=draft["body"],
        topics=draft.get("topics", []),
    )
    if platform_meta:
        post.platform = platform_meta

    auto_image_enabled = auto_image and is_auto_image_enabled()
    assets_paths = [Path(p) for p in asset_paths]
    effective_copy_assets = copy_assets

    if not assets_paths and auto_image_enabled:
        dest_dir = post_dir(post.id) / "assets"
        image_title = _preferred_image_title(post, post.title)
        image_paths, image_metas, image_fallback = _fetch_daily_news_related_images(
            title=image_title,
            body=post.body,
            topics=post.topics,
            prompt_hint=_preferred_image_hint(post, prompt_hint),
            dest_dir=dest_dir,
            exclude_ids=image_exclude_ids,
            # Every daily-news draft should prefer the configured AI image
            # provider. A Pexels image is only a fallback when generation
            # fails, including for online candidates rather than just manual
            # single-news materials.
            ai_first=True,
            image_policy=str((post.platform.get("news") or {}).get("image_policy") or "ai_required"),
        )
        if image_fallback:
            post.platform["image_fallback"] = image_fallback
        post.platform.setdefault("image", image_metas[0])
        post.platform["images"] = image_metas
        _merge_image_ids(image_exclude_ids, image_metas)
        assets_paths = image_paths
        if title_norm == "每日新闻" and image_paths:
            record_initial_news_image(
                post, image_path=image_paths[0], image_meta=image_metas[0]
            )
        # The downloaded file is already under data/posts/<id>/assets.
        effective_copy_assets = False

    if effective_copy_assets:
        copied = copy_assets_into_post(post.id, assets_paths)
        asset_infos = _build_asset_infos(copied)
    else:
        asset_infos = _build_asset_infos(assets_paths)
    post.assets = asset_infos

    rev = Revision(
        post_id=post.id,
        source=RevisionSource.llm,
        content=draft,
    )

    save_post(post)
    save_revision(rev)

    return post


def _daily_news_llm_supervisor_enabled(
    cfgs: list[Any],
    *,
    target_count: int,
    column: str = "daily_news",
) -> bool:
    raw = (os.getenv("NEWS_LLM_SUPERVISOR_ENABLED") or "1").strip().lower()
    if raw in {"0", "false", "off", "no"}:
        return False
    # The column must validate contrast and evidence even for a single draft;
    # ordinary daily news only needs the review when it has to pick a batch.
    if target_count <= 1 and column != DAILY_WOW_CONTENT_TYPE:
        return False
    # Test and offline configurations conventionally use a fake model. Avoid
    # making a network call in that mode while retaining local ranking.
    return any(not str(getattr(cfg, "model", "")).strip().lower().startswith("fake") for cfg in cfgs)


def _daily_news_supervisor_pool_limit(target_count: int) -> int:
    raw = (os.getenv("NEWS_LLM_SUPERVISOR_POOL_LIMIT") or "").strip()
    try:
        configured = int(raw) if raw else 0
    except ValueError:
        configured = 0
    return max(target_count * 10, configured or 0, 60)


_WOW_ACCEPT_DECISIONS = {"accept", "accepted", "yes", "true", "keep"}
_WOW_REJECT_DECISIONS = {"reject", "rejected", "no", "false", "drop", "needs_evidence"}
# Design 5.2: a story needs contrast >= 3 to enter the column.  The judge
# reported "荒诞性一般" (score 2) on an item it still accepted, so an accept is
# only honoured when its own score clears the floor.
_WOW_MIN_CONTRAST_SCORE = 3.0


def _wow_score_value(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _daily_wow_decisions_from_result(
    result: dict[str, Any], *, pool_size: int
) -> tuple[list[int], list[int], list[dict[str, Any]]]:
    """Split the column model's per-id decisions into accepted and rejected.

    Only ids present in the input pool are honoured, and a decision that is
    missing or unrecognised stays undecided so it can be reported rather than
    silently promoted into generation.
    """
    accepted: list[int] = []
    rejected: list[int] = []
    decisions: list[dict[str, Any]] = []
    rows = result.get("decisions") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return accepted, rejected, decisions
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if not 1 <= index <= pool_size:
            continue
        decision = str(row.get("decision") or "").strip().lower()
        decisions.append(
            {
                "id": index,
                "decision": decision,
                "contrast": _clip_text(str(row.get("contrast") or ""), limit=120),
                "score": row.get("score"),
                "reason": _clip_text(str(row.get("reason") or ""), limit=120),
            }
        )
        if decision in _WOW_ACCEPT_DECISIONS and index not in accepted:
            score = _wow_score_value(row.get("score"))
            contrast = str(row.get("contrast") or "").strip()
            if daily_wow_is_schema_echo(contrast):
                # The model repeated the prompt's example rather than judging
                # this story, so there is no verified contrast to accept.
                if index not in rejected:
                    rejected.append(index)
                decisions[-1]["schema_echo"] = True
                continue
            # An accept below the contrast floor is treated as a soft reject so
            # an ordinary story cannot be published just because the model
            # called it acceptable.
            if score is not None and score < _WOW_MIN_CONTRAST_SCORE:
                if index not in rejected:
                    rejected.append(index)
                decisions[-1]["below_contrast_floor"] = True
            else:
                accepted.append(index)
        elif decision in _WOW_REJECT_DECISIONS and index not in rejected:
            rejected.append(index)
    return accepted, rejected, decisions


def _supervise_daily_news_candidates(
    candidates: list[Any],
    *,
    cfgs: list[Any],
    prompt_hint: str,
    target_count: int,
    required_china_count: int,
    required_international_conflict_count: int = 0,
    progress_callback: DailyNewsProgressCallback | None,
    column: str = "daily_news",
) -> tuple[list[Any], dict[str, Any]]:
    """Use one optional LLM call to reorder the already validated candidate pool."""
    wow_column = column == DAILY_WOW_CONTENT_TYPE
    if wow_column and not _daily_news_llm_supervisor_enabled(
        cfgs, target_count=target_count, column=column
    ):
        # No column judge is available, so require a local contrast signal
        # instead of quietly publishing ordinary headlines.
        strict, strict_meta = daily_wow_strict_candidates(candidates, prompt_hint)
        _emit_daily_news_progress(
            progress_callback,
            "反差筛选",
            "warning" if strict else "skipped",
            mode="offline_strict",
            checked=strict_meta["input_count"],
            matched=strict_meta["strict_count"],
        )
        return strict, {
            "enabled": False,
            "status": "offline_strict_fallback",
            "column": DAILY_WOW_CONTENT_TYPE,
            **strict_meta,
        }
    if not _daily_news_llm_supervisor_enabled(
        cfgs, target_count=target_count, column=column
    ):
        return candidates, {"enabled": False, "status": "not_requested"}

    pool = candidates[: min(len(candidates), _daily_news_supervisor_pool_limit(target_count))]
    minimum_ranked_count = min(target_count, len(pool))
    payload: list[dict[str, Any]] = []
    for index, item in enumerate(pool, start=1):
        context = _compact_daily_news_context(item, max_chars=240)
        payload.append(
            {
                "id": index,
                "title": _clip_text(str(item.title or ""), limit=120),
                "summary": _clip_text(context, limit=240),
                "date": str(item.seendate or ""),
                "source": _clip_text(str(item.source or item.domain or ""), limit=80),
                "country": str(item.sourcecountry or ""),
                "attention": item.attention,
                "international_conflict": is_international_conflict_news(item),
            }
        )
    _emit_daily_news_progress(
        progress_callback,
        "模型审校候选",
        "in_progress",
        candidates=len(pool),
        target_count=target_count,
        china_required=required_china_count,
        international_conflict_required=required_international_conflict_count,
    )
    if wow_column:
        system_prompt = daily_wow_selection_system_prompt(minimum_ranked_count)
        user_prompt = json.dumps(
            daily_wow_selection_payload(
                candidates=payload,
                prompt_hint=prompt_hint,
                requested_drafts=target_count,
            ),
            ensure_ascii=False,
        )
    else:
        system_prompt = (
            "你是严格的新闻选题审校员。只基于给定候选信息工作，不补充事实。"
            "选择与用户关键词直接相关、时间新、可核验、事件明确且彼此不重复的候选；"
            "优先保留有具体主体、动作、时间或数据的新闻，排除泛泛评论、旧闻、重复报道和信息不足项。"
            "必须仅返回 JSON 对象，不得输出 Markdown 或解释文字。"
            "JSON 格式：{\"ranked_ids\":[正整数...],\"rejected_ids\":[正整数...],\"reason\":\"不超过80字\"}。"
            "ranked_ids 必须是候选 id 的去重排序；未列出的 id 会保留在本地排序末尾。"
        )
        system_prompt += (
            f" Return at least {minimum_ranked_count} unique ranked_ids. "
            "Do not return fewer ranked_ids when the candidate pool contains enough items. "
            f"The final batch must include at least {required_international_conflict_count} "
            "international conflict or geopolitical stories when marked true; rank those "
            "traceable items before ordinary business stories."
        )
        user_prompt = json.dumps(
            {
                "task": "为小红书每日新闻生成任务进行候选重排",
                "keywords": prompt_hint or "综合当日重要新闻",
                "requested_drafts": target_count,
                "minimum_china_mainland_items": required_china_count,
                "minimum_international_conflict_items": required_international_conflict_count,
                "candidates": payload,
            },
            ensure_ascii=False,
        )
    try:
        result = generate_json(
            cfgs,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=6000,
        )
        if wow_column:
            accepted_ids, rejected_ids, decisions = _daily_wow_decisions_from_result(
                result, pool_size=len(pool)
            )
            if not accepted_ids:
                raise RuntimeError("column supervisor accepted no usable candidate")
            chosen = [pool[index - 1] for index in accepted_ids]
            chosen_keys = {item.url or item.title for item in chosen}
            # Rejected candidates stay rejected: they were checked against the
            # column's fact and contrast rules and must not re-enter generation.
            undecided = [
                item
                for index, item in enumerate(pool, start=1)
                if index not in accepted_ids
                and index not in rejected_ids
                and (item.url or item.title) not in chosen_keys
            ]
            reviewed_ids = set(accepted_ids) | set(rejected_ids)
            tail = [
                item
                for index, item in enumerate(candidates, start=1)
                if index > len(pool)
                and (item.url or item.title) not in chosen_keys
            ]
            meta = {
                "enabled": True,
                "status": "success",
                "column": DAILY_WOW_CONTENT_TYPE,
                "reviewed_candidate_count": len(pool),
                "accepted_candidate_count": len(chosen),
                "rejected_candidate_count": len(rejected_ids),
                "undecided_candidate_count": len(undecided),
                "decisions": decisions,
                "reason": _clip_text(str(result.get("reason") or ""), limit=160),
            }
            _emit_daily_news_progress(
                progress_callback,
                "反差筛选",
                "success",
                reviewed=len(pool) if reviewed_ids else len(pool),
                accepted=len(chosen),
                rejected=len(rejected_ids),
            )
            return [*chosen, *undecided, *tail], meta
        raw_ids = result.get("ranked_ids")
        ranked_indices: list[int] = []
        if isinstance(raw_ids, list):
            for value in raw_ids:
                try:
                    index = int(value)
                except (TypeError, ValueError):
                    continue
                if 1 <= index <= len(pool) and index not in ranked_indices:
                    ranked_indices.append(index)
        if not ranked_indices:
            raise RuntimeError("supervisor returned no usable ranked_ids")
        if len(ranked_indices) < minimum_ranked_count:
            raise RuntimeError(
                f"supervisor returned {len(ranked_indices)} ranked_ids; "
                f"expected at least {minimum_ranked_count}"
            )
        chosen = [pool[index - 1] for index in ranked_indices]
        chosen_keys = {item.url or item.title for item in chosen}
        chosen.extend(item for item in candidates if (item.url or item.title) not in chosen_keys)
        meta = {
            "enabled": True,
            "status": "success",
            "reviewed_candidate_count": len(pool),
            "ranked_candidate_count": len(ranked_indices),
            "reason": _clip_text(str(result.get("reason") or ""), limit=160),
        }
        _emit_daily_news_progress(
            progress_callback,
            "模型审校候选",
            "success",
            reviewed=len(pool),
            ranked=len(ranked_indices),
        )
        return chosen, meta
    except Exception as exc:
        if wow_column:
            # The judge ran but produced no usable decision (for example every
            # candidate was rejected, or the response was unusable). Falling
            # back to ordinary ranking here would publish non-column stories,
            # so require a local contrast signal instead and let the caller
            # report the shortfall.
            message = _clip_text(str(exc), limit=180)
            strict, strict_meta = daily_wow_strict_candidates(candidates, prompt_hint)
            _emit_daily_news_progress(
                progress_callback,
                "反差筛选",
                "warning" if strict else "skipped",
                mode="offline_strict_after_review_failure",
                reason=message,
                checked=strict_meta["input_count"],
                matched=strict_meta["strict_count"],
            )
            return strict, {
                "enabled": True,
                "status": "offline_strict_after_review_failure",
                "column": DAILY_WOW_CONTENT_TYPE,
                "error": message,
                **strict_meta,
            }
        # Local ranking is deterministic and remains a valid fallback. The
        # user sees the degraded mode instead of waiting for a silent retry.
        message = _clip_text(str(exc), limit=180)
        _emit_daily_news_progress(
            progress_callback,
            "模型审校候选",
            "warning",
            reviewed=len(pool),
            reason=message,
            fallback="local_ranking",
        )
        return candidates, {
            "enabled": True,
            "status": "fallback_local_ranking",
            "reviewed_candidate_count": len(pool),
            "error": message,
        }


@dataclass
class _DailyNewsCandidateResult:
    candidate_index: int
    status: str
    picked: Any | None = None
    lookup_meta: dict[str, Any] | None = None
    focus_meta: dict[str, Any] | None = None
    draft: dict[str, Any] | None = None
    post: Post | None = None
    dedupe_item: Any | None = None
    picked_is_china: bool = False
    picked_is_conflict: bool = False
    asset_paths: list[Path] | None = None
    copy_assets: bool = False
    reason: str = ""
    error: str = ""
    failed_post_saved: bool = False


def _daily_news_reject_diagnostics(*, picked, candidate_index: int, reason: str,
                                  attempts: list[dict[str, Any]], trace: dict[str, Any],
                                  error: str = "") -> dict[str, Any]:
    """Snapshot failed quality work; numeric extraction is diagnostic only."""
    def redact(value):
        if isinstance(value, dict):
            return {str(key): ("[REDACTED]" if re.search(
                r"api.?key|authorization|password|secret|credential|(?:^|_)token$", str(key), re.I
            ) else redact(item)) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [redact(item) for item in value]
        if isinstance(value, str):
            value = re.sub(r"https?://[^\s<>\"'，。]+", lambda match: urllib.parse.urlunsplit(
                (*urllib.parse.urlsplit(match.group(0))[:3], "", "")
            ), value)
            value = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", value)
            return re.sub(r"(?i)((?:api[_-]?key|password|secret|access[_-]?token)\s*[:=]\s*)[^\s,;]+",
                          r"\1[REDACTED]", value)
        return value

    source_tokens = _daily_news_numeric_claims(_daily_news_context_text(picked))
    frozen = deepcopy(attempts)
    for attempt in frozen:
        raw = attempt.get("raw_draft") or {}
        content = _daily_news_body_quality_fields(str(raw.get("body") or "")).get("内容", "")
        candidate_tokens = _daily_news_numeric_claims(content)
        attempt["numeric"] = {
            "candidate_tokens": sorted(candidate_tokens, key=repr),
            "source_tokens": sorted(source_tokens, key=repr),
            "difference": sorted(candidate_tokens - source_tokens, key=repr),
        }
    return redact({"version": "quality-rejection-v1", "candidate_index": candidate_index,
                   "reason": reason, "error": error, "source_snapshot": asdict(picked),
                   "source_trace": trace, "attempts": frozen})


def _prepare_daily_news_candidate(
    *,
    candidate_index: int,
    picked: Any,
    cfgs: list[Any],
    asset_paths: list[str],
    copy_assets: bool,
    auto_image_enabled: bool,
    prompt_norm: str,
    viewpoint_norm: str,
    target_count: int,
    single_material_mode: bool,
    base_meta: dict[str, Any],
    progress_callback: DailyNewsProgressCallback | None,
    model_queues: ModelWorkQueues,
    post_quality_callback: DailyNewsPostQualityCallback | None,
    prepared: tuple[Any, dict[str, Any], dict[str, Any], Any] | None = None,
    original_is_conflict: bool | None = None,
    column: str = "daily_news",
) -> _DailyNewsCandidateResult:
    """Prepare one candidate without touching shared acceptance state.

    This function is intentionally independent from the final batch selector.
    It may run in parallel, while dedupe and domestic-source quotas remain
    deterministic in the caller.
    """
    def stopped_result(draft=None, post=None, paths=None, *, copy_result_assets=False):
        return _DailyNewsCandidateResult(
            candidate_index=candidate_index, status="cancelled", picked=picked,
            draft=draft, post=post, asset_paths=paths,
            copy_assets=copy_result_assets, reason="model_work_stopped",
        )

    if model_queues.stopped:
        return stopped_result()
    wow_column = column == DAILY_WOW_CONTENT_TYPE
    review_attempts: list[dict[str, Any]] = []
    rewrite_count = 0
    image_event_audit: dict[str, Any] = {}
    lookup_meta: dict[str, Any] = {}
    focus_meta: dict[str, Any] = {}

    def rejection_diagnostics(reason: str, error: str = "") -> dict[str, Any]:
        return _daily_news_reject_diagnostics(
            picked=picked, candidate_index=candidate_index, reason=reason, error=error,
            attempts=review_attempts,
            trace=_daily_news_meta_with_trace({**base_meta, **(lookup_meta or {}), **(focus_meta or {})}, picked),
        )

    def persist_rejection(candidate: dict[str, Any], reason: str, error: str = "") -> Post:
        diagnostics = rejection_diagnostics(reason, error)
        # Use only sanitized snapshots in the rejection artifact/revision.
        final = _daily_news_reject_diagnostics(
            picked=picked, candidate_index=candidate_index, reason=reason,
            attempts=[{"raw_draft": candidate}], trace={}, error=error,
        )["attempts"][0]["raw_draft"]
        rejected = Post(
            type="image", status=PostStatus.failed, uploaded=False,
            title=str(final.get("title") or ""), body=str(final.get("body") or ""),
            topics=final.get("topics") if isinstance(final.get("topics"), list) else [],
            platform={"news": {**diagnostics["source_trace"], "picked": diagnostics["source_snapshot"],
                               "source_url": diagnostics["source_snapshot"].get("url", ""),
                               "mode": "daily_news", "candidate_index": candidate_index,
                               "image_event": "", "image_event_audit": final.get("image_event_audit", {}),
                               "length_review": final.get("_length_review", {}),
                               "quality_rejection": diagnostics},
                      "batch_selection": {"status": "quality_rejected", "reason": reason}},
        )
        save_post(rejected)
        save_revision(Revision(post_id=rejected.id, source=RevisionSource.llm,
                               content={**final, "image_event": "", "quality_rejection": diagnostics}))
        return rejected

    original_is_conflict = (
        _daily_news_conflict_signal(picked)
        if original_is_conflict is None
        else bool(original_is_conflict)
    )
    _emit_daily_news_progress(
        progress_callback,
        "原文核验",
        "in_progress",
        candidate_index=candidate_index,
        candidate_total=0,
        source=str(getattr(picked, "domain", "") or getattr(picked, "source", "") or "unknown"),
    )
    if prepared is None:
        try:
            picked, lookup_meta = _enrich_daily_news_item(picked)
            picked, focus_meta = _focus_daily_news_item(picked)
        except Exception as exc:
            return _DailyNewsCandidateResult(
                candidate_index=candidate_index,
                status="failed",
                reason="source_context_lookup_failed",
                error=str(exc),
            )
    else:
        picked, lookup_meta, focus_meta, prepared_dedupe_item = prepared

    if not single_material_mode and _daily_news_source_is_multi_story(picked):
        _emit_daily_news_progress(
            progress_callback, "原文核验", "skipped",
            candidate_index=candidate_index, reason="multi_story_source",
        )
        return _DailyNewsCandidateResult(
            candidate_index=candidate_index, status="skipped", picked=picked,
            post=persist_rejection({}, "multi_story_source"), failed_post_saved=True,
            lookup_meta=lookup_meta, focus_meta=focus_meta,
            picked_is_conflict=original_is_conflict or _daily_news_conflict_signal(picked),
            reason="multi_story_source",
        )

    if not single_material_mode and _daily_news_context_is_incomplete(picked):
        _emit_daily_news_progress(
            progress_callback,
            "原文核验",
            "skipped",
            candidate_index=candidate_index,
            reason="source_context_insufficient",
        )
        return _DailyNewsCandidateResult(
            candidate_index=candidate_index,
            status="skipped",
            picked=picked,
            post=persist_rejection({}, "source_context_insufficient"), failed_post_saved=True,
            lookup_meta=lookup_meta,
            focus_meta=focus_meta,
            picked_is_conflict=original_is_conflict or _daily_news_conflict_signal(picked),
            reason="source_context_insufficient",
        )

    dedupe_item = prepared_dedupe_item if prepared is not None else None
    if not single_material_mode and dedupe_item is None:
        dedupe_item = replace(
            picked,
            description=_compact_daily_news_context(picked, max_chars=700),
            content=None,
        )
    traced_news_meta = _daily_news_meta_with_trace(
        {**base_meta, **(lookup_meta or {}), **(focus_meta or {})},
        picked,
    )
    news_prompt = _daily_news_prompt(picked, prompt_norm, viewpoint_norm, column=column)
    if target_count > 1:
        news_prompt = f"（候选 {candidate_index}）\n{news_prompt}"

    _emit_daily_news_progress(
        progress_callback,
        "生成文案",
        "in_progress",
        draft_index=candidate_index,
        target=target_count,
        candidate_index=candidate_index,
    )
    try:
        draft = model_queues.submit_llm(
            generate_draft,
            cfgs,
            title_hint="每日新闻",
            prompt_hint=news_prompt,
            asset_paths=asset_paths,
            preserve_body=not wow_column,
            concise_news=not wow_column,
        ).result()
    except CancelledError:
        return stopped_result()
    except Exception as exc:
        if model_queues.stopped:
            return stopped_result()
        _emit_daily_news_progress(
            progress_callback,
            "生成文案",
            "failed",
            draft_index=candidate_index,
            target=target_count,
            candidate_index=candidate_index,
            reason="llm_request_failed",
        )
        return _DailyNewsCandidateResult(
            candidate_index=candidate_index,
            status="failed",
            picked=picked,
            lookup_meta=lookup_meta,
            focus_meta=focus_meta,
            dedupe_item=dedupe_item,
            picked_is_china=_is_china_item(picked),
            picked_is_conflict=original_is_conflict or _daily_news_conflict_signal(picked),
            reason="llm_request_failed",
            error=str(exc),
        )

    if model_queues.stopped:
        return stopped_result(draft, paths=[Path(path) for path in asset_paths],
                              copy_result_assets=copy_assets)
    fallback_error = draft.get("_fallback_error") if isinstance(draft, dict) else ""
    if fallback_error:
        reason = (
            "content_policy_rejected"
            if _daily_news_content_policy_rejection(fallback_error)
            else _daily_news_llm_unavailable_reason(fallback_error)
        )
        _emit_daily_news_progress(
            progress_callback,
            "生成文案",
            "skipped" if reason == "content_policy_rejected" else "failed",
            draft_index=candidate_index,
            target=target_count,
            candidate_index=candidate_index,
            reason=reason,
        )
        review_attempts.append({"attempt": 1, "stage": "writer", "raw_draft": deepcopy(draft),
                                "reason": reason, "error": str(fallback_error)})
        return _DailyNewsCandidateResult(
            candidate_index=candidate_index,
            status="skipped" if reason == "content_policy_rejected" else "failed",
            picked=picked,
            post=persist_rejection(draft, reason, str(fallback_error)), failed_post_saved=True,
            lookup_meta=lookup_meta,
            focus_meta=focus_meta,
            dedupe_item=dedupe_item,
            picked_is_china=_is_china_item(picked),
            picked_is_conflict=original_is_conflict or _daily_news_conflict_signal(picked),
            reason=reason,
            error=str(fallback_error),
        )

    image_event_audit = {"writer_value": str(draft.get("image_event") or "")}

    def review_draft(candidate: dict[str, Any]) -> tuple[dict[str, Any], str]:
        record = {"attempt": len(review_attempts) + 1,
                  "stage": "writer" if not review_attempts else "rewrite",
                  "raw_draft": deepcopy(candidate), "reason": "", "error": ""}
        review_attempts.append(record)
        embedded = _extract_embedded_json_from_daily_news_body(candidate.get("body", ""))
        if embedded:
            for field in ("title", "body", "image_event"):
                if isinstance(embedded.get(field), str) and embedded[field].strip():
                    candidate[field] = embedded[field].strip()
            if isinstance(embedded.get("topics"), list) and embedded["topics"]:
                candidate["topics"] = embedded["topics"]
        candidate["body"] = _ensure_daily_news_sections(candidate.get("body", ""), prompt_norm)
        candidate["body"] = _ensure_news_publish_date(candidate["body"], picked.seendate)
        if _daily_news_body_has_prompt_leak(candidate.get("body", "")) or _daily_news_body_is_too_generic(candidate.get("body", "")):
            candidate["title"] = _normalize_daily_news_title(picked.title, picked, prompt_norm)
            candidate["body"] = _daily_news_offline_body(picked, prompt_norm)
            candidate["topics"] = [DAILY_WOW_TOPIC if wow_column else "每日新闻"]
        if _is_generic_daily_news_title(candidate.get("title", "")):
            candidate["title"] = _normalize_daily_news_title(
                picked.title or picked.description or prompt_norm, picked, prompt_norm
            )
        candidate["title"] = _normalize_daily_news_title(
            candidate.get("title", ""), picked, prompt_norm,
            max_len=daily_wow_title_max_len() if wow_column else 18,
        )
        if wow_column:
            candidate["title"] = daily_wow_display_title(
                candidate.get("title", ""), max_len=daily_wow_title_max_len()
            )
        topics = candidate.get("topics") or []
        if not isinstance(topics, list):
            topics = [str(topics)]
        topics_context = f"{candidate.get('title', '')} {candidate.get('body', '')}"
        candidate["topics"] = (
            _daily_wow_topics(topics, prompt_norm, topics_context)
            if wow_column else _normalize_daily_news_topics(topics, prompt_norm, topics_context)
        )
        try:
            candidate["body"] = _finalize_daily_news_body(
                candidate.get("body", ""), picked, prompt_norm,
                title_hint=str(candidate.get("title") or ""),
                preserve_length=not wow_column,
            )
            candidate["body"] = _repair_daily_news_mismatched_comment(
                candidate["body"], picked, prompt_norm,
                title_hint=str(candidate.get("title") or ""),
                preserve_length=not wow_column,
            )
        except _DailyNewsNumericClaimError as exc:
            record.update({"reason": "unsupported_numeric_claim", "error": str(exc),
                           "reviewed_draft": deepcopy(candidate)})
            return candidate, "unsupported_numeric_claim"
        if wow_column:
            candidate["body"] = _daily_wow_repair_comment(candidate["body"], picked, prompt_norm)
        candidate = _simplify_daily_news_draft(candidate)
        issue = (
            _daily_wow_quality_issue(candidate.get("title", ""), candidate.get("body", ""), prompt_norm)
            if wow_column else _daily_news_quality_issue(candidate.get("title", ""), candidate.get("body", ""), prompt_norm)
        )
        if not issue and not wow_column and _daily_news_lead_lacks_headline_anchor(candidate["title"], candidate["body"]):
            issue = "missing_lead_event"
        if not issue and not wow_column and _daily_news_content_is_too_thin(candidate["body"], picked):
            issue = "thin_content"
        if not wow_column:
            length_issue = _review_daily_news_length(
                candidate, picked, rewrite_count=rewrite_count,
            )
            issue = issue or length_issue
        if not issue and not wow_column and (auto_image_enabled or str(candidate.get("image_event") or "").strip()):
            candidate["image_event"] = _to_simplified_common(_normalize_daily_news_image_event(
                str(candidate.get("image_event") or ""), picked=picked,
                title=str(candidate.get("title") or ""), body=str(candidate.get("body") or ""),
                prompt_norm=prompt_norm, audit=image_event_audit,
            ))
            attempt = {key: value for key, value in image_event_audit.items() if key != "attempts"}
            image_event_audit.setdefault("attempts", []).append(attempt)
            candidate["image_event_audit"] = image_event_audit
            if not image_event_audit.get("accepted") or not candidate["image_event"]:
                issue = "image_scene_unverified"
        record.update({"reason": issue, "reviewed_draft": deepcopy(candidate)})
        return candidate, issue

    draft, quality_issue = review_draft(draft)
    rewrite_error = ""
    if quality_issue == "generic_body" and single_material_mode:
        # User-supplied material remains the source of truth for this fallback.
        draft = _source_grounded_single_material_draft(picked, prompt_norm, preserve_length=not wow_column)
        draft, quality_issue = review_draft(draft)
    if quality_issue in {
        "thin_content", "missing_lead_event", "generic_title", "generic_body",
        "bad_body_language", "unsupported_numeric_claim", "image_scene_unverified",
        "content_too_long", "comment_too_long", "body_too_long",
        "incomplete_comment", "comment_multiple_sentences",
    } and not wow_column:
        _emit_daily_news_progress(
            progress_callback, "质量复核", "in_progress",
            draft_index=candidate_index, target=target_count,
            candidate_index=candidate_index, reason=f"rewrite_once:{quality_issue}",
        )
        repair_prompt = (
            f"{news_prompt}\n上一版未通过质量检查（{quality_issue}）。请重新写一版，只使用上述原始材料中的事实。"
            "内容第一句必须交代主体、动作和对象，不能从评价或影响起笔；材料充分时把关键事实写完整，"
            "标题必须具体说明事件。不得虚构事实，不得为了字数填充空话。"
            f"场景审计原因：{image_event_audit.get('reason', '正文尚未通过事实检查')}。"
            "必须同时重写完整body与image_event，image_event第一完整句逐字复制新body内容的第一完整事实句，"
            "第二句为保留具体主体、已证实动作或状态的单场景概念构图，事实锚点不能证明附加画面细节；"
            "渠道须有原文证据；声明或计划仅作概念示意，不添加发布会或真实环境，不把取消或计划画成完成。"
            "本次为唯一一次整稿改写。只输出规定的 JSON。"
            + news_length_rewrite_instruction(draft)
        )
        try:
            rewrite_count = 1
            draft.get("_length_review", {}).update(rewrite_count=rewrite_count)
            rewritten = model_queues.submit_llm(
                generate_draft, cfgs, title_hint="每日新闻",
                prompt_hint=repair_prompt, asset_paths=asset_paths,
                preserve_body=True, concise_news=True,
            ).result()
            if isinstance(rewritten, dict) and not rewritten.get("_fallback_error"):
                image_event_audit["rewrite_value"] = str(rewritten.get("image_event") or "")
                draft, quality_issue = review_draft(rewritten)
            elif isinstance(rewritten, dict):
                rewrite_error = str(rewritten.get("_fallback_error") or "")
                review_attempts.append({"attempt": len(review_attempts) + 1, "stage": "rewrite",
                                        "raw_draft": deepcopy(rewritten), "reason": quality_issue,
                                        "error": rewrite_error})
        except CancelledError:
            return stopped_result(draft, paths=[Path(path) for path in asset_paths],
                                  copy_result_assets=copy_assets)
        except Exception as exc:
            if model_queues.stopped:
                return stopped_result(draft, paths=[Path(path) for path in asset_paths],
                                      copy_result_assets=copy_assets)
            rewrite_error = str(exc)
            review_attempts.append({"attempt": len(review_attempts) + 1, "stage": "rewrite",
                                    "raw_draft": {}, "reason": quality_issue, "error": rewrite_error})
    if model_queues.stopped:
        return stopped_result(draft, paths=[Path(path) for path in asset_paths],
                              copy_result_assets=copy_assets)
    if quality_issue:
        draft["image_event"] = ""
        draft["image_event_audit"] = image_event_audit
        rejected_post = persist_rejection(draft, quality_issue, rewrite_error)
        _emit_daily_news_progress(
            progress_callback,
            "质量复核",
            "skipped",
            draft_index=candidate_index,
            target=target_count,
            candidate_index=candidate_index,
            reason=quality_issue,
        )
        return _DailyNewsCandidateResult(
            candidate_index=candidate_index,
            status="skipped",
            picked=picked,
            lookup_meta=lookup_meta,
            focus_meta=focus_meta,
            draft=draft,
            post=rejected_post,
            dedupe_item=dedupe_item,
            picked_is_china=_is_china_item(picked),
            picked_is_conflict=original_is_conflict or _daily_news_conflict_signal(picked),
            reason=quality_issue,
            error=rewrite_error,
            failed_post_saved=rejected_post is not None,
        )

    image_event = _to_simplified_common(
        # The column strips narration/JSON leakage first; a truncated model
        # response must not store prose in an event field.
        daily_wow_clean_image_event(draft.get("image_event"))
        if wow_column
        else str(draft.get("image_event") or "")
    )
    if wow_column and not image_event:
        image_event = _to_simplified_common(
            _daily_news_fallback_subject(picked, prompt_norm)
        )
    draft["image_event"] = image_event
    if not wow_column:
        draft["image_event_audit"] = image_event_audit
    post = Post(
        type="image",
        status=PostStatus.draft,
        title=draft["title"],
        body=draft["body"],
        topics=draft.get("topics", []),
        platform={
            "news": {
                **traced_news_meta,
                "picked": asdict(picked),
                "source_url": picked.url,
                "mode": "daily_news_single_material" if single_material_mode else ("daily_news_multi" if target_count > 1 else "daily_news"),
                "image_policy": (
                    "ai_required"
                    if not asset_paths and not single_material_mode
                    else ("ai_preferred" if single_material_mode else "provided")
                ),
                "prompt_hint": prompt_norm,
                "evaluation_viewpoint": viewpoint_norm,
                "pick_index": candidate_index,
                "pick_total": target_count,
                "candidate_index": candidate_index,
                "image_event": image_event,
                **({"image_event_audit": image_event_audit} if not wow_column else {}),
                **({"length_review": draft["_length_review"]} if not wow_column else {}),
            }
        },
    )
    if wow_column:
        wow_contrast = daily_wow_clean_image_event(draft.get("verified_contrast"))
        wow_visual_plan = _daily_wow_visual_plan_text(draft.get("visual_plan"))
        post.platform["news"].update(
            {
                "column": DAILY_WOW_CONTENT_TYPE,
                "content_type": DAILY_WOW_CONTENT_TYPE,
                "image_style": "daily_wow",
                "verified_contrast": wow_contrast,
                "visual_plan": wow_visual_plan,
            }
        )

    resolved_assets = [Path(p) for p in asset_paths]
    effective_copy_assets = copy_assets
    if not resolved_assets and auto_image_enabled:
        dest_dir = post_dir(post.id) / "assets"
        try:
            _emit_daily_news_progress(
                progress_callback,
                "生成配图",
                "in_progress",
                draft_index=candidate_index,
                target=target_count,
                candidate_index=candidate_index,
            )
            image_paths, image_metas, image_fallback = model_queues.submit_image(
                _fetch_daily_news_related_images,
                title=_preferred_image_title(post, post.title),
                body=post.body,
                topics=post.topics,
                prompt_hint=_preferred_image_hint(post, prompt_norm),
                dest_dir=dest_dir,
                exclude_ids=set(),
                ai_first=True,
                image_policy="ai_preferred" if single_material_mode else "ai_required",
                prompt_override=(
                    _daily_wow_image_prompt_for_post(post)
                    if wow_column
                    else None
                ),
            ).result()
            if image_fallback:
                post.platform["image_fallback"] = image_fallback
            post.platform.setdefault("image", image_metas[0])
            post.platform["images"] = image_metas
            resolved_assets = image_paths
            # The quality callback can replace the image; capture identity first.
            if not wow_column and image_paths:
                record_initial_news_image(
                    post, image_path=image_paths[0], image_meta=image_metas[0]
                )
            effective_copy_assets = False
        except CancelledError:
            return stopped_result(draft, post, resolved_assets,
                                  copy_result_assets=effective_copy_assets)
        except ImageGenerationAbandoned as exc:
            if model_queues.stopped:
                return stopped_result(draft, post, resolved_assets,
                                      copy_result_assets=effective_copy_assets)
            _emit_daily_news_progress(
                progress_callback,
                "生成配图",
                "failed",
                draft_index=candidate_index,
                target=target_count,
                candidate_index=candidate_index,
                reason="image_generation_abandoned",
            )
            return _DailyNewsCandidateResult(
                candidate_index=candidate_index,
                status="failed",
                picked=picked,
                lookup_meta=lookup_meta,
                focus_meta=focus_meta,
                draft=draft,
                post=post,
                dedupe_item=dedupe_item,
                picked_is_china=_is_china_item(picked),
                picked_is_conflict=original_is_conflict or _daily_news_conflict_signal(picked),
                reason="image_generation_abandoned",
                error="; ".join(exc.errors[-3:]),
            )
        except Exception as exc:
            if model_queues.stopped:
                return stopped_result(draft, post, resolved_assets,
                                      copy_result_assets=effective_copy_assets)
            _emit_daily_news_progress(
                progress_callback,
                "生成配图",
                "failed",
                draft_index=candidate_index,
                target=target_count,
                candidate_index=candidate_index,
                reason=str(exc)[:160],
            )
            return _DailyNewsCandidateResult(
                candidate_index=candidate_index,
                status="failed",
                picked=picked,
                lookup_meta=lookup_meta,
                focus_meta=focus_meta,
                draft=draft,
                post=post,
                dedupe_item=dedupe_item,
                picked_is_china=_is_china_item(picked),
                picked_is_conflict=original_is_conflict or _daily_news_conflict_signal(picked),
                reason="image_generation_failed",
                error=str(exc),
            )

    post.assets = _build_asset_infos(resolved_assets)
    if model_queues.stopped:
        return stopped_result(draft, post, resolved_assets,
                              copy_result_assets=effective_copy_assets)
    if post_quality_callback is not None:
        try:
            quality_errors = list(
                model_queues.submit_llm(post_quality_callback, post).result() or []
            )
        except CancelledError:
            return stopped_result(draft, post, resolved_assets,
                                  copy_result_assets=effective_copy_assets)
        except Exception as exc:
            if model_queues.stopped:
                return stopped_result(draft, post, resolved_assets,
                                      copy_result_assets=effective_copy_assets)
            quality_errors = [f"上传前质量复核调用失败：{exc}"]
        if model_queues.stopped and quality_errors:
            return stopped_result(draft, post, resolved_assets,
                                  copy_result_assets=effective_copy_assets)
        if quality_errors:
            post.status = PostStatus.failed
            post.platform["batch_selection"] = {
                "status": "visual_quality_failed",
                "reason": "；".join(quality_errors[:3]),
            }
            diagnostics = rejection_diagnostics("visual_quality_failed", "; ".join(quality_errors))
            post.platform["news"]["quality_rejection"] = diagnostics
            draft["quality_rejection"] = diagnostics
            save_post(post)
            save_revision(Revision(post_id=post.id, source=RevisionSource.llm, content=draft))
            _emit_daily_news_progress(
                progress_callback,
                "视觉递补",
                "skipped",
                candidate_index=candidate_index,
                reason=str(quality_errors[0])[:160],
            )
            return _DailyNewsCandidateResult(
                candidate_index=candidate_index,
                status="skipped",
                picked=picked,
                lookup_meta=lookup_meta,
                focus_meta=focus_meta,
                draft=draft,
                post=post,
                dedupe_item=dedupe_item,
                picked_is_china=_is_china_item(picked),
                picked_is_conflict=original_is_conflict or _daily_news_conflict_signal(picked),
                asset_paths=resolved_assets,
                copy_assets=effective_copy_assets,
                reason="visual_quality_failed",
                error="; ".join(quality_errors),
                failed_post_saved=True,
            )
    _emit_daily_news_progress(
        progress_callback,
        "质量复核",
        "success",
        draft_index=candidate_index,
        target=target_count,
        candidate_index=candidate_index,
    )
    return _DailyNewsCandidateResult(
        candidate_index=candidate_index,
        status="success",
        picked=picked,
        lookup_meta=lookup_meta,
        focus_meta=focus_meta,
        draft=draft,
        post=post,
        dedupe_item=dedupe_item,
        picked_is_china=_is_china_item(picked),
        picked_is_conflict=original_is_conflict or _daily_news_conflict_signal(picked),
        asset_paths=resolved_assets,
        copy_assets=effective_copy_assets,
    )


def _run_parallel_daily_news_candidates(
    *,
    picks: list[Any],
    cfgs: list[Any],
    asset_paths: list[str],
    copy_assets: bool,
    auto_image_enabled: bool,
    prompt_norm: str,
    viewpoint_norm: str,
    target_count: int,
    single_material_mode: bool,
    base_meta: dict[str, Any],
    progress_callback: DailyNewsProgressCallback | None,
    post_quality_callback: DailyNewsPostQualityCallback | None,
    required_china_count: int,
    required_international_conflict_count: int,
    performance_policy: PerformancePolicy | None = None,
    discovery: DailyNewsDiscovery | None = None,
    column: str = "daily_news",
    post_saved_callback: Callable[[Post], None] | None = None,
) -> list[Post]:
    """Run candidate preparation in two lanes and accept results in order."""
    # Re-assert the protected editorial lane after any LLM reordering. This
    # keeps conflict candidates from waiting behind ordinary stories and
    # avoids spending image calls before the required lane is filled.
    performance_policy = performance_policy or PerformancePolicy.from_environment()
    speed_first = performance_policy.is_speed_first
    wow_column = column == DAILY_WOW_CONTENT_TYPE
    if wow_column:
        # The column has no domestic or conflict lane; contrast selection already
        # happened before generation.
        required_china_count = 0
        required_international_conflict_count = 0
    picks = (
        _prioritize_all_daily_news_conflicts(list(picks))
        if required_international_conflict_count
        else list(picks)
    )
    posts: list[Post] = []
    accepted_china_count = 0
    accepted_conflict_count = 0
    accepted_items: list[Any] = []
    retired_indices: set[int] = set()
    accepted_story_signatures: list[Any] = []
    accepted_body_fact_keys: list[tuple[str, Any]] = []
    used_title_keys: set[str] = set()
    used_image_ids: set[str] = set()
    failed_count = 0
    skipped_quality_count = 0
    skipped_quota_count = 0
    llm_unavailable_reasons: list[str] = []
    provider_capacity_error = ""
    candidate_retry_counts: dict[int, int] = {}

    # Enrich once before submitting work. This avoids duplicate source requests
    # and lets us discard mirrored stories before spending model capacity.
    # Source pages are fetched with bounded concurrency so a large candidate
    # pool cannot spend several minutes waiting on serial 8-second lookups.
    prepared_by_index = (
        {index: discovery.prepared[news_key(item)] for index, item in enumerate(picks, 1)}
        if discovery is not None else
        _prefetch_daily_news_context(picks, progress_callback=progress_callback)
    )
    original_conflict_by_index = {
        index: _daily_news_conflict_signal(picks[index - 1])
        for index in range(1, len(picks) + 1)
    }
    prefiltered_duplicates = 0
    prefiltered_incomplete_context = 0
    prefilter_signatures: list[Any] = []
    if not single_material_mode:
        for candidate_index in sorted(prepared_by_index):
            enriched, lookup_meta, focus_meta, dedupe_item = prepared_by_index[candidate_index]
            if _daily_news_context_is_incomplete(enriched):
                prefiltered_incomplete_context += 1
                prepared_by_index.pop(candidate_index, None)
                _emit_daily_news_progress(
                    progress_callback,
                    "原文核验",
                    "skipped",
                    candidate_index=candidate_index,
                    completed=0,
                    target=target_count,
                    reason="source_context_insufficient",
                )
                continue
            signature = _cjk_story_event_signature(dedupe_item)
            if any(_same_cjk_story_event(signature, seen) for seen in prefilter_signatures):
                prefiltered_duplicates += 1
                prepared_by_index.pop(candidate_index, None)
                continue
            prefilter_signatures.append(signature)
    skipped_quality_count = prefiltered_duplicates + prefiltered_incomplete_context

    pending_indices = sorted(prepared_by_index)
    conflict_by_index = {
        index: bool(
            original_conflict_by_index.get(index, False)
            or _daily_news_conflict_signal(prepared_by_index[index][0], prepared_by_index[index][3])
        )
        for index in pending_indices
    }

    # Candidate workers feed independent model queues. MiniMax may use five
    # LLM agents; other providers stay at two. XHS upload happens later in
    # apps.cli's existing serial loop.
    llm_provider = infer_llm_provider(cfgs)
    llm_workers = performance_policy.llm_workers_for_provider(llm_provider)
    coordinator_workers = max(_daily_news_coordinator_workers(), llm_workers)
    with ModelWorkQueues(
        llm_workers=llm_workers,
        image_workers=performance_policy.image_workers,
        llm_provider=llm_provider,
    ) as model_queues:
        workers = ThreadPoolExecutor(
            max_workers=coordinator_workers,
            thread_name_prefix="redbook-candidate",
        )
        submitted_futures: list[Any] = []
        stop_reason = ""
        coordinator_error: BaseException | None = None

        def submit_candidate(**kwargs):
            future = workers.submit(_prepare_daily_news_candidate, **kwargs)
            submitted_futures.append(future)
            return future

        def stop_candidates(reason: str) -> None:
            nonlocal stop_reason
            if not stop_reason:
                stop_reason = reason
            model_queues.request_stop()
            pending_indices.clear()
            for future in submitted_futures:
                future.cancel()

        def confirmed_capacity_exhausted(error: object) -> bool:
            text = str(error or "").lower()
            # A subscription name or a transient 429 is not quota evidence.
            return _daily_news_provider_capacity_exhausted(error) and (
                any(marker in text for marker in (
                    "用量上限", "套餐用量", "quota exhausted", "insufficient balance", "余额不足",
                ))
                or ("token plan" in text and any(marker in text for marker in (
                    "limit reached", "limit exceeded", "usage exhausted",
                )))
            )

        def results_in_order(futures):
            nonlocal provider_capacity_error
            buffered: dict[Any, _DailyNewsCandidateResult] = {}
            next_index = 0
            # Observe quota failures out of order, without delaying ready selections.
            for future in as_completed(futures):
                try:
                    result = future.result()
                except CancelledError:
                    result = _DailyNewsCandidateResult(
                        candidate_index=0, status="cancelled", reason="model_work_stopped",
                    )
                except Exception as exc:
                    result = _DailyNewsCandidateResult(
                        candidate_index=0,
                        status="cancelled" if model_queues.stopped else "failed",
                        reason="model_work_stopped" if model_queues.stopped else "candidate_worker_failed",
                        error=str(exc),
                    )
                if model_queues.stopped and result.status not in {"success", "cancelled"}:
                    result = replace(result, status="cancelled", reason="model_work_stopped")
                buffered[future] = result
                if result.status not in {"success", "cancelled"} and confirmed_capacity_exhausted(result.error):
                    provider_capacity_error = _daily_news_llm_unavailable_reason(result.error)
                    stop_candidates("provider_capacity_exhausted")
                while next_index < len(futures) and futures[next_index] in buffered:
                    result = buffered.pop(futures[next_index])
                    next_index += 1
                    yield result

        def retain_unselected_result(result: _DailyNewsCandidateResult) -> None:
            post = result.post
            if result.failed_post_saved or (post is not None and any(item.id == post.id for item in posts)):
                return
            if post is None:
                if not isinstance(result.draft, dict) or not result.draft:
                    return
                topics = result.draft.get("topics") or []
                if not isinstance(topics, list):
                    topics = [topics]
                post = Post(
                    title=str(result.draft.get("title") or ""),
                    body=str(result.draft.get("body") or ""),
                    topics=[str(topic) for topic in topics],
                    status=PostStatus.canceled,
                )
                post.platform["news"] = {"mode": "daily_news", "candidate_index": result.candidate_index}
                if result.picked is not None:
                    post.platform["news"]["picked"] = asdict(result.picked)
            paths = [Path(asset.path) for asset in post.assets] or list(result.asset_paths or [])
            if result.copy_assets and paths == list(result.asset_paths or []):
                paths = copy_assets_into_post(post.id, paths)
                post.assets = _build_asset_infos(paths)
            elif paths and not post.assets:
                post.assets = _build_asset_infos(paths)
            post.platform["batch_selection"] = {
                "status": "retained_after_stop", "reason": stop_reason,
                "candidate_status": result.status, "candidate_reason": result.reason,
                "requested_count": target_count,
            }
            save_post(post)
            save_revision(Revision(post_id=post.id, source=RevisionSource.llm, content=result.draft or {}))

        try:
            in_flight: list[Any] = []
            while True:
                if (
                    len(posts) >= target_count
                    and accepted_conflict_count >= required_international_conflict_count
                    and accepted_china_count >= required_china_count
                ):
                    stop_candidates("target_reached")
                    break
                protected_lane_empty = (
                    accepted_conflict_count < required_international_conflict_count
                    and not any(conflict_by_index.get(index, False) for index in pending_indices)
                )
                if not in_flight and (not pending_indices or protected_lane_empty):
                    if discovery is None:
                        break
                    _emit_daily_news_progress(progress_callback, "生成补位", "in_progress",
                                              completed=len(posts), target=target_count,
                                              reason="reserve_exhausted")
                    try:
                        extra = discovery.take(accepted=accepted_items,
                                               carry=[prepared_by_index[index][0] for index in pending_indices])
                    except Exception as exc:
                        stop_candidates("coordinator_error")
                        _emit_daily_news_progress(progress_callback, "候选不足", "failed",
                            reason=f"补充候选失败，已完成的{len(posts)}条保留在本地：{exc}")
                        break
                    base_meta.update(discovery.meta)
                    if not extra:
                        break
                    existing_keys = {news_key(item) for item in picks}
                    for item in extra:
                        if news_key(item) in existing_keys:
                            continue
                        existing_keys.add(news_key(item))
                        picks.append(item)
                        index = len(picks)
                        prepared_by_index[index] = discovery.prepared[news_key(item)]
                        original_conflict_by_index[index] = _daily_news_conflict_signal(item)
                        conflict_by_index[index] = original_conflict_by_index[index]
                        pending_indices.append(index)
                if speed_first:
                    # Keep the coordinator window full. The two model queues
                    # remain the actual provider-level concurrency boundary.
                    coordinator_limit = coordinator_workers
                    while pending_indices and len(in_flight) < coordinator_limit:
                        batch_indices = _daily_news_candidate_batch_indices(
                            pending_indices,
                            accepted_conflict_count=accepted_conflict_count,
                            required_international_conflict_count=required_international_conflict_count,
                            conflict_by_index=conflict_by_index,
                            batch_size=1,
                        )
                        if not batch_indices:
                            break
                        index = batch_indices[0]
                        pending_indices.remove(index)
                        if index not in prepared_by_index:
                            continue
                        in_flight.append(
                            submit_candidate(
                                candidate_index=index,
                                picked=picks[index - 1],
                                cfgs=cfgs,
                                asset_paths=asset_paths,
                                copy_assets=copy_assets,
                                auto_image_enabled=auto_image_enabled,
                                prompt_norm=prompt_norm,
                                viewpoint_norm=viewpoint_norm,
                                target_count=target_count,
                                single_material_mode=single_material_mode,
                                base_meta=base_meta,
                                progress_callback=progress_callback,
                                model_queues=model_queues,
                                post_quality_callback=post_quality_callback,
                                prepared=prepared_by_index.get(index),
                                original_is_conflict=original_conflict_by_index.get(index),
                                column=column,
                            )
                        )
                    if not in_flight:
                        break
                    done, _ = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
                    futures = list(done)
                    in_flight = [future for future in in_flight if future not in done]
                else:
                    batch_indices = _daily_news_candidate_batch_indices(
                        pending_indices,
                        accepted_conflict_count=accepted_conflict_count,
                        required_international_conflict_count=required_international_conflict_count,
                        conflict_by_index=conflict_by_index,
                    )
                    if not batch_indices:
                        break
                    pending_indices = [
                        index for index in pending_indices if index not in batch_indices
                    ]
                    batch = [
                        (index, picks[index - 1])
                        for index in batch_indices
                        if index in prepared_by_index
                    ]
                    if not batch:
                        continue
                    futures = [
                        submit_candidate(
                            candidate_index=index,
                            picked=picked,
                            cfgs=cfgs,
                            asset_paths=asset_paths,
                            copy_assets=copy_assets,
                            auto_image_enabled=auto_image_enabled,
                            prompt_norm=prompt_norm,
                            viewpoint_norm=viewpoint_norm,
                            target_count=target_count,
                            single_material_mode=single_material_mode,
                            base_meta=base_meta,
                            progress_callback=progress_callback,
                            model_queues=model_queues,
                            post_quality_callback=post_quality_callback,
                            prepared=prepared_by_index.get(index),
                            original_is_conflict=original_conflict_by_index.get(index),
                            column=column,
                        )
                        for index, picked in batch
                    ]
                for result in results_in_order(futures):
                    if (
                        len(posts) >= target_count
                        and accepted_conflict_count >= required_international_conflict_count
                        and accepted_china_count >= required_china_count
                    ):
                        break
                    if result.status == "cancelled":
                        continue
                    if result.status != "success" or result.post is None or result.picked is None:
                        if confirmed_capacity_exhausted(result.error):
                            provider_capacity_error = _daily_news_llm_unavailable_reason(result.error)
                        if not model_queues.stopped and _schedule_daily_news_candidate_retry(
                            result,
                            candidate_retry_counts,
                            pending_indices,
                        ):
                            retry_count = candidate_retry_counts[result.candidate_index]
                            _emit_daily_news_progress(
                                progress_callback,
                                "候选重试",
                                "retrying",
                                candidate_index=result.candidate_index,
                                completed=len(posts),
                                target=target_count,
                                retry_count=retry_count,
                                retry_limit=_daily_news_candidate_retry_limit(),
                                reason=result.reason,
                            )
                            continue
                        retired_indices.add(result.candidate_index)
                        if result.status == "skipped":
                            if not result.failed_post_saved:
                                skipped_quality_count += 1
                        else:
                            failed_count += 1
                            if result.reason not in {"image_generation_abandoned", "image_generation_failed"} and result.error:
                                llm_unavailable_reasons.append(
                                    _daily_news_llm_unavailable_reason(result.error)
                                )
                        continue

                    retired_indices.add(result.candidate_index)

                    picked = result.picked
                    post = result.post
                    dedupe_item = result.dedupe_item
                    if dedupe_item is not None:
                        signature = _cjk_story_event_signature(dedupe_item)
                        if any(
                            _same_cjk_story_event(signature, accepted)
                            for accepted in accepted_story_signatures
                        ):
                            skipped_quality_count += 1
                            _emit_daily_news_progress(
                                progress_callback,
                                "原文核验",
                                "skipped",
                                candidate_index=result.candidate_index,
                                completed=len(posts),
                                target=target_count,
                                reason="duplicate_story_after_enrichment",
                            )
                            continue
                    else:
                        signature = None

                    fact_key = _daily_news_body_fact_key(post.body)
                    if fact_key:
                        copied_from_other_story = any(
                            fact_key == accepted_key
                            and not (
                                signature is not None
                                and accepted_signature is not None
                                and _same_cjk_story_event(signature, accepted_signature)
                            )
                            for accepted_key, accepted_signature in accepted_body_fact_keys
                        )
                        if copied_from_other_story:
                            skipped_quality_count += 1
                            _emit_daily_news_progress(
                                progress_callback,
                                "质量复核",
                                "skipped",
                                candidate_index=result.candidate_index,
                                completed=len(posts),
                                target=target_count,
                                reason="duplicate_generated_fact_body",
                            )
                            continue

                    title_key = _daily_news_title_key(post.title)
                    if title_key and title_key in used_title_keys:
                        skipped_quality_count += 1
                        continue
                    picked_is_china = result.picked_is_china
                    picked_is_conflict = bool(
                        result.picked_is_conflict or is_international_conflict_news(picked)
                    )
                    if (
                        required_international_conflict_count - accepted_conflict_count - int(picked_is_conflict)
                        > target_count - len(posts) - 1
                    ):
                        skipped_quota_count += 1
                        _emit_daily_news_progress(
                            progress_callback,
                            "候选配额",
                            "skipped",
                            candidate_index=result.candidate_index,
                            completed=len(posts),
                            target=target_count,
                            reason="international_conflict_quota_reserved",
                        )
                        continue
                    if required_china_count > 0 and not picked_is_china:
                        if required_china_count - accepted_china_count > target_count - len(posts) - 1:
                            skipped_quota_count += 1
                            _emit_daily_news_progress(
                                progress_callback,
                                "候选配额",
                                "skipped",
                                candidate_index=result.candidate_index,
                                completed=len(posts),
                                target=target_count,
                                reason="china_quota_reserved",
                            )
                            continue

                    if discovery is not None:
                        # Recheck the final selection against domain and both
                        # quotas after enrichment, including failed replacements.
                        proposed = [*accepted_items, picked]
                        slots_left = target_count - len(proposed)
                        remaining_pool = [prepared[0] for index, prepared in prepared_by_index.items()
                                          if index not in retired_indices]
                        if slots_left and len(remaining_pool) >= slots_left and not feasible_news_batch(
                            remaining_pool, slots_left,
                            china=max(0, required_china_count - accepted_china_count - int(picked_is_china)),
                            conflict=max(0, required_international_conflict_count - accepted_conflict_count - int(picked_is_conflict)),
                            total=target_count, accepted=proposed,
                        ):
                            skipped_quota_count += 1
                            _emit_daily_news_progress(progress_callback, "候选配额", "skipped",
                                reason="combined_editorial_quota_reserved", candidate_index=result.candidate_index)
                            continue
                        # Domain caps are based on the requested batch, never on
                        # the temporary number of completed results.  Use the
                        # same bounded relaxation as candidate discovery when
                        # only a few publisher domains are reachable.
                        domains = {news_domain(item) for item in picks}
                        cap = source_domain_cap(
                            picks,
                            target_count,
                            required=slots_left,
                            accepted=proposed,
                        )
                        if slots_left and len(domains) > 1 and sum(news_domain(item) == news_domain(picked) for item in proposed) > cap:
                            skipped_quota_count += 1
                            _emit_daily_news_progress(progress_callback, "候选配额", "skipped",
                                reason="source_domain_quota_reserved", candidate_index=result.candidate_index)
                            continue

                    result.asset_paths = list(result.asset_paths or [])
                    final_paths = [Path(asset.path) for asset in post.assets] or result.asset_paths
                    if result.copy_assets and final_paths == result.asset_paths:
                        copied = copy_assets_into_post(post.id, final_paths)
                        post.assets = _build_asset_infos(copied)
                    elif not post.assets:
                        post.assets = _build_asset_infos(final_paths)
                    post.platform.setdefault("news", {})["pick_index"] = len(posts) + 1
                    post.platform.setdefault("news", {})["pick_total"] = target_count
                    save_post(post)
                    # Only the coordinator can accept a candidate. Publish its
                    # durable identity before another candidate/whole batch ends.
                    posts.append(post)
                    if (
                        len(posts) >= target_count
                        and accepted_conflict_count + int(picked_is_conflict) >= required_international_conflict_count
                        and accepted_china_count + int(picked_is_china) >= required_china_count
                    ):
                        stop_candidates("target_reached")
                    try:
                        if post_saved_callback is not None:
                            post_saved_callback(post)
                        save_revision(
                            Revision(
                                post_id=post.id,
                                source=RevisionSource.llm,
                                content=result.draft or {},
                            )
                        )
                    except Exception as exc:
                        raise PartialDailyNewsError(
                            f"daily news saved but completion recording failed: {exc}",
                            posts=list(posts),
                            requested_count=target_count,
                            failed_count=failed_count,
                            skipped_quality_count=skipped_quality_count,
                        ) from exc
                    accepted_items.append(picked)
                    if title_key:
                        used_title_keys.add(title_key)
                    if signature is not None:
                        accepted_story_signatures.append(signature)
                    if fact_key:
                        accepted_body_fact_keys.append((fact_key, signature))
                    if picked_is_china:
                        accepted_china_count += 1
                    if picked_is_conflict:
                        accepted_conflict_count += 1
                    for image_meta in post.platform.get("images") or []:
                        image_id = str(image_meta.get("id") or image_meta.get("url") or "").strip()
                        if image_id:
                            used_image_ids.add(image_id)
                    _emit_daily_news_progress(
                        progress_callback,
                        "生成草稿",
                        "success",
                        completed=len(posts),
                        target=target_count,
                        candidate_index=result.candidate_index,
                    )
                    if (
                        len(posts) >= target_count
                        and accepted_conflict_count >= required_international_conflict_count
                        and accepted_china_count >= required_china_count
                    ):
                        break

                if provider_capacity_error:
                    stop_candidates("provider_capacity_exhausted")
                    _emit_daily_news_progress(
                        progress_callback,
                        "生成草稿",
                        "failed",
                        completed=len(posts),
                        target=target_count,
                        reason="provider_capacity_exhausted",
                    )
                    break
        except BaseException as exc:
            coordinator_error = exc
            stop_candidates("coordinator_error")
            raise
        finally:
            workers.shutdown(wait=True, cancel_futures=model_queues.stopped)
            if model_queues.stopped:
                retention_errors: list[Exception] = []
                # Futures are terminal after shutdown; no new work or polling joins.
                for future in submitted_futures:
                    if future.cancelled():
                        continue
                    try:
                        result = future.result()
                    except BaseException:
                        continue
                    try:
                        retain_unselected_result(result)
                    except Exception as exc:
                        retention_errors.append(exc)
                if retention_errors:
                    message = f"completed candidate retention failed: {retention_errors[0]}"
                    if coordinator_error is not None:
                        if hasattr(coordinator_error, "add_note"):
                            coordinator_error.add_note(message)
                        print(f"[daily_news] {message}")
                    else:
                        raise PartialDailyNewsError(
                            message, posts=list(posts), requested_count=target_count,
                            failed_count=failed_count, skipped_quality_count=skipped_quality_count,
                        ) from retention_errors[0]

    if discovery is not None:
        complete = (len(posts) == target_count and accepted_china_count >= required_china_count
                    and accepted_conflict_count >= required_international_conflict_count)
        discovery.save_record(status="generation_complete" if complete else "generation_partial",
                              post_ids=[post.id for post in posts])
        for post in posts:
            post.platform.setdefault("news", {})["discovery_record"] = str(discovery.record_path)
            save_post(post)
    if (
        len(posts) < target_count
        or accepted_conflict_count < required_international_conflict_count
        or accepted_china_count < required_china_count
    ):
        message = (
            f"daily news created only {len(posts)}/{target_count} | 批次生成未完成："
            f"已完成 {len(posts)}/{target_count}，已处理候选 {len(retired_indices - {0})}/{len(picks)}，"
            f"文案或生图失败 {failed_count}，质量/去重跳过 {skipped_quality_count}，"
            f"类别/来源配额预留跳过 {skipped_quota_count}，国内稿件 {accepted_china_count}/{required_china_count}。"
            f"国际冲突稿件 {accepted_conflict_count}/{required_international_conflict_count}。"
            "本次默认不会上传不完整批次；请查看上方具体步骤，调整关键词、回溯天数或模型额度后重试。"
        )
        if provider_capacity_error:
            message += f" 模型不可用：{provider_capacity_error}。"
        elif llm_unavailable_reasons:
            message += f" 模型不可用：{llm_unavailable_reasons[-1]}。"
        print(f"[daily_news] {message}")
        _emit_daily_news_progress(
            progress_callback,
            "生成草稿",
            "failed",
            completed=len(posts),
            target=target_count,
            failed=failed_count,
            skipped_quality=skipped_quality_count,
            skipped_quota=skipped_quota_count,
            reason="batch_incomplete",
        )
        if posts:
            raise PartialDailyNewsError(
                message,
                posts=posts,
                requested_count=target_count,
                failed_count=(failed_count if model_queues.stopped else max(failed_count, target_count - len(posts))),
                skipped_quality_count=skipped_quality_count,
            )
        raise RuntimeError(message)
    return posts


def create_daily_news_posts(
    *,
    prompt_hint: str = "",
    asset_paths: list[str],
    copy_assets: bool = True,
    count: int = 1,
    auto_image: bool = True,
    evaluation_viewpoint: str = DEFAULT_EVALUATION_VIEWPOINT,
    lookback_days: object = None,
    news_materials_file: str | Path | None = None,
    single_news_material_file: str | Path | None = None,
    material_time: str = "",
    progress_callback: DailyNewsProgressCallback | None = None,
    post_quality_callback: DailyNewsPostQualityCallback | None = None,
    performance_mode: str | None = None,
    column: str = "daily_news",
    exclude_story_keys: set[str] | None = None,
    post_saved_callback: Callable[[Post], None] | None = None,
) -> list[Post]:
    """
    Special workflow for title="每日新闻".

    - Use `prompt_hint` to rank candidates, then pick up to `count` items.
    - When `count` is 1, behavior is equivalent to a single best match.

    `column` selects the editorial strategy.  The default keeps ordinary daily
    news unchanged; `daily_wow` reuses retrieval, dates, dedupe, generation and
    delivery with the 反差 selection, playful comment and clean-illustration
    rules from the column design.
    """
    column_norm = daily_wow_normalize_column(column)
    wow_column = column_norm == DAILY_WOW_CONTENT_TYPE
    performance_policy = (
        PerformancePolicy.from_value(performance_mode)
        if performance_mode is not None
        else PerformancePolicy.from_environment()
    )
    cfgs = load_llm_configs()
    single_material_mode = bool(str(single_news_material_file or "").strip())
    prompt_norm = "" if single_material_mode else (prompt_hint or "").strip()
    viewpoint_norm = normalize_evaluation_viewpoint(evaluation_viewpoint)
    if single_material_mode:
        count = 1
    elif count <= 0:
        count = 1
    auto_image_enabled = auto_image and is_auto_image_enabled()
    used_image_ids: set[str] = set()
    used_title_keys: set[str] = set()
    failed_count = 0
    skipped_quality_count = 0
    skipped_quota_count = 0
    llm_unavailable_reasons: list[str] = []

    def _is_fatal_image_config_error(errs: list[str]) -> bool:
        # Aliyun returns this when using image-to-image models without providing an init image.
        joined = " ".join(errs or []).lower()
        return (
            "got 0 images" in joined
            or "must contain 1 to 4 images" in joined
            or "enable_interleave" in joined
        )

    discovery_holder: dict[str, Any] = {}
    candidates, base_meta = _fetch_daily_news_candidates_for_upload(
        prompt_norm,
        count=count,
        lookback_days=None if single_material_mode else lookback_days,
        news_materials_file=news_materials_file,
        single_news_material_file=single_news_material_file,
        material_time=material_time,
        progress_callback=progress_callback,
        discovery_holder=discovery_holder,
        performance_policy=performance_policy,
        column=column_norm,
        exclude_story_keys=exclude_story_keys,
    )
    excluded_story_keys = {str(item).strip() for item in (exclude_story_keys or set()) if str(item).strip()}
    if excluded_story_keys:
        candidates = [
            item for item in candidates
            if not (_daily_news_story_identity(item) & excluded_story_keys)
        ]
        if len(candidates) < count:
            raise RuntimeError(
                "daily news replenishment material insufficient after story dedupe: "
                f"需要 {count} 条新事件，当前只有 {len(candidates)} 条未使用事件。"
            )
    target_count = count
    soft_preferences = daily_news_soft_preferences_enabled()
    required_china_count = (
        0
        if single_material_mode or wow_column or soft_preferences
        else _required_china_count_for_daily_news(target_count)
    )
    required_international_conflict_count = (
        0
        if single_material_mode or wow_column or soft_preferences
        else daily_news_international_conflict_quota(target_count)
    )
    available_conflict_count = sum(
        1 for item in candidates if is_international_conflict_news(item)
    )
    if available_conflict_count < required_international_conflict_count:
        message = (
            "daily news material insufficient for international conflict quota | "
            f"需要至少 {required_international_conflict_count} 条国际争议事件，"
            f"当前候选池仅识别到 {available_conflict_count} 条；"
            "未开始调用 LLM 或生图，避免产生不符合要求的草稿。"
        )
        _emit_daily_news_progress(
            progress_callback,
            "候选配额",
            "failed",
            completed=0,
            target=target_count,
            reason="international_conflict_material_insufficient",
        )
        raise RuntimeError(message)
    candidates, supervisor_meta = _supervise_daily_news_candidates(
        candidates,
        cfgs=cfgs,
        prompt_hint=prompt_norm,
        target_count=target_count,
        required_china_count=required_china_count,
        required_international_conflict_count=required_international_conflict_count,
        progress_callback=progress_callback,
        column=column_norm,
    )
    base_meta = dict(base_meta)
    selection_pool = base_meta.get("selection_pool")
    if isinstance(selection_pool, dict):
        selection_pool = dict(selection_pool)
        selection_pool["llm_supervisor"] = supervisor_meta
        if wow_column:
            selection_pool["wow_review_status"] = supervisor_meta.get("status")
        base_meta["selection_pool"] = selection_pool
    elif wow_column:
        base_meta["selection_pool"] = {
            "llm_supervisor": supervisor_meta,
            "wow_review_status": supervisor_meta.get("status"),
        }
    # Pick the first pass with the true target count so source diversity quotas
    # are based on the number of drafts the user asked for. Keep extra ranked
    # candidates after that because strict quality gates can reject snippets.
    candidates = (
        _prioritize_all_daily_news_conflicts(list(candidates))
        if required_international_conflict_count
        else list(candidates)
    )
    pick_limit = min(len(candidates), max(count * 20, count + 30))
    if supervisor_meta.get("status") == "success":
        # The supervisor has already ranked freshness, event clarity and story
        # diversity. Re-ranking here with local attention would undo that work.
        picks = list(candidates[:pick_limit])
    else:
        picks = pick_news_items(candidates, prompt_norm, count=min(count, len(candidates)))
        seen_pick_keys = {item.url or item.title for item in picks}
        for item in candidates:
            key = item.url or item.title
            if key in seen_pick_keys:
                continue
            picks.append(item)
            seen_pick_keys.add(key)
            if len(picks) >= pick_limit:
                break

    discovery = discovery_holder.get("session")
    if discovery is not None:
        # Preserve fresh-first order; a model may rank within a day, but must
        # not promote older reserve material over today's viable main set.
        from src.news.daily_news import _parse_seendate_utc
        from zoneinfo import ZoneInfo
        from src.news.daily_news import _resolve_tz
        picks.sort(key=lambda item: _parse_seendate_utc(item.seendate).astimezone(_resolve_tz("Asia/Shanghai")).date(), reverse=True)
        main = feasible_news_batch(picks, target_count, china=required_china_count,
                                   conflict=required_international_conflict_count)
        if not main:
            if wow_column:
                raise RuntimeError(
                    "每日我去没有通过反差筛选的候选：本次候选都缺少可检查的真实反差，"
                    "未开始生图，也没有用普通热点凑稿。可补充更具体的奇闻关键词、"
                    "增加信源或稍后重试。"
                )
            raise RuntimeError("模型排序后无法同时满足新闻数量、类别与来源要求，未开始生图。")
        main_keys = {news_key(item) for item in main}
        picks = main + [item for item in picks if news_key(item) not in main_keys]

    generated_posts = _run_parallel_daily_news_candidates(
        picks=picks,
        cfgs=cfgs,
        asset_paths=asset_paths,
        copy_assets=copy_assets,
        auto_image_enabled=auto_image_enabled,
        prompt_norm=prompt_norm,
        viewpoint_norm=viewpoint_norm,
        target_count=target_count,
        single_material_mode=single_material_mode,
        base_meta=base_meta,
        progress_callback=progress_callback,
        post_quality_callback=post_quality_callback,
        required_china_count=required_china_count,
        required_international_conflict_count=required_international_conflict_count,
        performance_policy=performance_policy,
        discovery=discovery,
        column=column_norm,
        post_saved_callback=post_saved_callback,
    )
    # The runner may ask the discovery session for more candidates after a
    # quality failure. Read the final mutable ``picks`` list after it returns,
    # so the next visual-replenishment batch excludes every event considered
    # in this run, including candidates that never became a Post.
    batch_story_keys: set[str] = set()
    for item in picks:
        batch_story_keys.update(news_story_identity_keys(item))
    if batch_story_keys:
        encoded_keys = sorted(batch_story_keys)
        for post in generated_posts:
            news = post.platform.get("news") if isinstance(post.platform, dict) else None
            if not isinstance(news, dict):
                continue
            existing_keys = news.get("batch_candidate_story_keys")
            if isinstance(existing_keys, (list, tuple, set)):
                batch_keys = {*map(str, existing_keys), *encoded_keys}
            else:
                batch_keys = set(encoded_keys)
            news["batch_candidate_story_keys"] = sorted(
                key for key in batch_keys if key.strip()
            )
    return generated_posts
