from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
import re
from typing import Any

from .basemap import resolve_basemap_path

@dataclass(frozen=True)
class CountryLocation:
    country: str
    latitude: float
    longitude: float
    precision: str = "country"
    method: str = "explicit_country_name"


# Country-level label anchors are not event locations. They are used only when
# the source text explicitly names a country and no precise coordinates exist.
_COUNTRY_LABEL_ANCHORS: dict[str, tuple[tuple[str, ...], float, float]] = {
    "Iran": (("iran", "伊朗"), 32.4279, 53.6880),
    "Iraq": (("iraq", "伊拉克"), 33.2232, 43.6793),
    "Poland": (("poland", "波兰"), 51.9194, 19.1451),
    "Ukraine": (("ukraine", "乌克兰", "kyiv", "kiev", "基辅"), 48.3794, 31.1656),
    "North Korea": (("north korea", "dprk", "pyongyang", "朝鲜", "平壤"), 40.3399, 127.5101),
    "Russia": (("russia", "俄罗斯"), 61.5240, 105.3188),
    "China": (("china", "中国"), 35.8617, 104.1954),
    "India": (("india", "印度"), 22.9734, 78.6569),
    "Nepal": (("nepal", "尼泊尔"), 28.3949, 84.1240),
    "Turkey": (("turkey", "土耳其"), 38.9637, 35.2433),
    "South Korea": (("south korea", "republic of korea", "韩国", "南韩"), 35.9078, 127.7669),
    "Yemen": (("yemen", "也门"), 15.5527, 48.5164),
    "Saudi Arabia": (("saudi arabia", "沙特", "沙特阿拉伯"), 23.8859, 45.0792),
    "Canada": (("canada", "加拿大"), 56.1304, -106.3468),
    "Mexico": (("mexico", "墨西哥"), 23.6345, -102.5528),
    "Morocco": (("morocco", "摩洛哥"), 31.7917, -7.0926),
    "Nigeria": (("nigeria", "尼日利亚"), 9.0820, 8.6753),
    "South Africa": (("south africa", "南非"), -30.5595, 22.9375),
    "Fiji": (("fiji", "斐济"), -17.7134, 178.0650),
    "France": (("france", "法国"), 46.2276, 2.2137),
    "Bangladesh": (("bangladesh", "孟加拉国"), 23.6850, 90.3563),
    "Pakistan": (("pakistan", "巴基斯坦"), 30.3753, 69.3451),
    "Sweden": (("sweden", "瑞典"), 60.1282, 18.6435),
    "Hungary": (("hungary", "匈牙利"), 47.1625, 19.5033),
    "Colombia": (("colombia", "哥伦比亚"), 4.5709, -74.2973),
    "Brazil": (("brazil", "巴西"), -14.2350, -51.9253),
    "United Kingdom": (("united kingdom", "uk", "u.k.", "英国"), 55.3781, -3.4360),
    "United States": (("united states", "united states of america", "u.s.", "usa", "美国"), 37.0902, -95.7129),
    "Israel": (("israel", "以色列"), 31.0461, 34.8516),
    "Japan": (("japan", "日本"), 36.2048, 138.2529),
    "Australia": (("australia", "澳大利亚"), -25.2744, 133.7751),
    "Germany": (("germany", "德国"), 51.1657, 10.4515),
}


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


@lru_cache(maxsize=8)
def _country_names_from_geojson(path: str, mtime_ns: int, size: int) -> tuple[str, ...]:
    """Read names only; polygon geometry is not evidence of an event location."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("type") != "FeatureCollection":
        return ()
    names = []
    features = raw.get("features")
    if not isinstance(features, list):
        return ()
    for feature in features:
        properties = feature.get("properties") if isinstance(feature, dict) else None
        name = properties.get("name") if isinstance(properties, dict) else None
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return tuple(names)


def resolve_explicit_country_location(text: str, *, basemap_path: str | Path | None = None) -> CountryLocation | None:
    """Resolve an explicitly named country to a country-level map label."""
    haystack = _normalise_text(text)
    if not haystack:
        return None
    try:
        path = resolve_basemap_path(basemap_path)
        stat = path.stat()
        catalog = _country_names_from_geojson(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError):
        return None
    if not catalog:
        return None
    aliases: dict[str, str] = {}
    for country, (names, _, _) in _COUNTRY_LABEL_ANCHORS.items():
        for name in names:
            aliases[_normalise_text(name)] = country
    for country in catalog:
        aliases.setdefault(_normalise_text(country), country)
    # Longest overlapping name wins (e.g. South Sudan versus Sudan), but
    # separate country mentions remain ambiguous even without label anchors.
    spans: list[tuple[int, int]] = []
    unique: set[str] = set()
    for name, country in sorted(aliases.items(), key=lambda pair: len(pair[0]), reverse=True):
        for match in re.finditer(rf"(?<![a-z]){re.escape(name)}(?![a-z])", haystack):
            start, end = match.span()
            if any(start < right and end > left for left, right in spans):
                continue
            spans.append((start, end))
            unique.add(country)
    if len(unique) != 1:
        return None
    country = next(iter(unique))
    anchor = _COUNTRY_LABEL_ANCHORS.get(country)
    if anchor is None:
        return None
    _, latitude, longitude = anchor
    return CountryLocation(country, latitude, longitude)


def verified_location(
    latitude: Any,
    longitude: Any,
    location_name: str = "",
    precision_hint: str = "",
) -> tuple[float | None, float | None, str]:
    """Accept only explicit coordinates; never infer a capital or use 0,0."""
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return None, None, "unknown"
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return None, None, "country" if location_name else "unknown"
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180 or (lat == 0 and lon == 0):
        return None, None, "country" if location_name else "unknown"
    return lat, lon, precision_hint or "city"


def is_flight_incident_title(title: str) -> bool:
    """A route or passenger's nationality is not an incident location."""
    return bool(
        re.search(r"\b(?:flight|plane|aircraft|jet)\b", title, re.I)
        and re.search(
            r"\b(?:divert\w*|landed|stab\w*|crash\w*|attack|distress|terrifying|bound|en route|to|from)\b",
            title, re.I,
        )
    )


def resolve_event_country_location(
    title: str, summary: str = "", *, basemap_path: str | Path | None = None,
) -> CountryLocation | None:
    """Infer only an unambiguous source headline or a confirmed flight landing."""
    if not is_flight_incident_title(title):
        return resolve_explicit_country_location(title, basemap_path=basemap_path)
    text = f"{title}. {summary}"
    landings = []
    for landing in re.finditer(
        r"\b(?:diverted|landed)\s+(?:\w+\s+){0,3}?(?:to|in|at)\s+([^,.;]+)", text, re.I,
    ):
        prefix = re.split(r"[.!?;]", text[:landing.start()])[-1]
        if re.search(r"\b(?:not|never|no|may|might|could|would|will|if|planned|expected)\b", prefix, re.I):
            return None
        resolved = resolve_explicit_country_location(landing.group(1), basemap_path=basemap_path)
        if resolved is None:
            return None
        landings.append(resolved)
    if not landings or len({place.country for place in landings}) != 1:
        return None
    return landings[0]
