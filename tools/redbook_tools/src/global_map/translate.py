from __future__ import annotations

import json
import re
from dataclasses import replace

from .models import MapSnapshot


def _validated_translations(snapshot: MapSnapshot, payload: dict) -> MapSnapshot:
    rows = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != len(snapshot.events):
        raise RuntimeError("MAP_TRANSLATION_INVALID: translated event count differs from source")
    translated = []
    for index, (event, row) in enumerate(zip(snapshot.events, rows), start=1):
        if not isinstance(row, dict) or row.get("index") != index:
            raise RuntimeError("MAP_TRANSLATION_INVALID: event order changed")
        title = str(row.get("title") or "").strip()
        summary = str(row.get("summary") or "").strip()
        summary = re.sub(r"[（(]图片来源[:：][^）)]*[）)]", "", summary).strip()
        summary = re.sub(r"(?:来源|图片来源)[:：]\s*[A-Za-z0-9_. -]+[。.]?$", "", summary).strip()
        if not title or len(title) > 36 or len(summary) > 65:
            raise RuntimeError("MAP_TRANSLATION_INVALID: translated text is empty or too long")
        for value in (title, summary):
            if re.search(r"https?://|<[^>]+>|[{}\[\]]|(?:[A-Za-z]{3,}\s+){3,}", value):
                raise RuntimeError("MAP_TRANSLATION_INVALID: markup, link or untranslated prose")
        if len(re.findall(r"[\u4e00-\u9fff]", title)) < 5:
            raise RuntimeError("MAP_TRANSLATION_INVALID: title is not Chinese")
        source_text = f"{event.title} {event.summary}"
        source_numbers = set(re.findall(r"\d+(?:\.\d+)?", source_text))
        word_numbers = {"two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                        "seven": "7", "eight": "8", "nine": "9", "ten": "10"}
        source_numbers.update(
            word_numbers[word.lower()]
            for word in re.findall(r"\b(?:two|three|four|five|six|seven|eight|nine|ten)\b", source_text, re.I)
        )
        result_text = f"{title} {summary}"
        result_numbers = set(re.findall(r"\d+(?:\.\d+)?", result_text))
        han_numbers = {"二": "2", "两": "2", "三": "3", "四": "4", "五": "5", "六": "6",
                       "七": "7", "八": "8", "九": "9", "十": "10"}
        result_numbers.update(
            han_numbers[character]
            for character in re.findall(r"[二三四五六七八九十两](?=[项个名起座支架次家人条年种辆所倍宗件笔场枚位艘])", result_text)
        )
        if not result_numbers.issubset(source_numbers):
            added = sorted(result_numbers - source_numbers)
            raise RuntimeError(f"MAP_TRANSLATION_INVALID: event {index} added numbers {added}")
        translated.append(replace(event, title=title, summary=summary))
    return replace(snapshot, events=translated)


def _translations_omitting_unsafe(snapshot: MapSnapshot, payload: dict) -> MapSnapshot:
    try:
        return _dedupe_translated_snapshot(_validated_translations(snapshot, payload))
    except RuntimeError:
        rows = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) != len(snapshot.events):
            raise
        if any(not isinstance(row, dict) or row.get("index") != index for index, row in enumerate(rows, start=1)):
            raise

    translated = []
    for event, row in zip(snapshot.events, rows):
        try:
            isolated = replace(snapshot, events=[event])
            one_row = {**row, "index": 1}
            translated.append(_validated_translations(isolated, {"events": [one_row]}).events[0])
        except RuntimeError:
            continue
    if not translated:
        raise RuntimeError("MAP_TRANSLATION_INVALID: no source-grounded translated events remain")

    from datetime import datetime

    from .select import select_events

    result = select_events(
        translated,
        max_events=len(translated),
        target_date=snapshot.target_date,
        cutoff=datetime.fromisoformat(snapshot.cutoff),
        raw_item_count=snapshot.raw_item_count,
        source_state=snapshot.source_state,
        map_mode=snapshot.map_mode,
    )
    excluded = len(snapshot.events) - len(translated)
    warning = f"已排除{excluded}条翻译内容无法核实的事件。"
    if result.warning:
        warning += result.warning
    return _dedupe_translated_snapshot(replace(
        result, warning=warning, independent_event_count=snapshot.independent_event_count,
    ))


def _dedupe_translated_snapshot(snapshot: MapSnapshot) -> MapSnapshot:
    seen: list = []
    for event in snapshot.events:
        title = "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", event.title))
        pairs = {title[index:index + 2] for index in range(len(title) - 1)}
        duplicate = False
        for previous in seen:
            if event.country != previous.country or not event.country:
                continue
            old_title = "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", previous.title))
            old_pairs = {old_title[index:index + 2] for index in range(len(old_title) - 1)}
            common = len(pairs & old_pairs)
            if common >= 4 and common / max(1, min(len(pairs), len(old_pairs))) >= 0.55:
                duplicate = True
                break
        if not duplicate:
            seen.append(event)
    removed = len(snapshot.events) - len(seen)
    if not removed:
        return snapshot

    from datetime import datetime

    from .select import select_events

    result = select_events(
        seen, max_events=len(seen), target_date=snapshot.target_date,
        cutoff=datetime.fromisoformat(snapshot.cutoff), raw_item_count=snapshot.raw_item_count,
        source_state=snapshot.source_state, map_mode=snapshot.map_mode,
    )
    warning = f"已合并{removed}条重复事件。"
    if snapshot.warning:
        warning = snapshot.warning + warning
    if result.warning and result.warning not in warning:
        warning += result.warning
    return replace(result, warning=warning, independent_event_count=snapshot.independent_event_count)


def translate_map_snapshot(snapshot: MapSnapshot) -> MapSnapshot:
    """Translate only supplied evidence; never let the model choose new events."""
    from src.config import load_llm_config
    from src.llm.generate import _parse_json_text, generate_json

    evidence = [
        {"index": index, "title": event.title, "snippet": event.summary}
        for index, event in enumerate(snapshot.events, start=1)
    ]
    system_prompt = (
            "你是严格的新闻翻译编辑。只把给出的标题和摘要译为简体中文，不新增事实、人物、数字、地点或因果。"
            "摘要只有片段时只翻译现有信息，不补全省略号。保持原有顺序和 index。"
            "不要复制图片署名、网页来源行、网站域名或编辑标记。"
            "返回严格 JSON：{\"events\":[{\"index\":1,\"title\":\"中文完整事件标题\",\"summary\":\"中文摘要或空字符串\"}]}。"
            "每条标题不超过36字，摘要不超过65字；不输出 URL、HTML、评价或多余字段。"
    )
    user_prompt = json.dumps({"events": evidence}, ensure_ascii=False)
    config = load_llm_config()
    if config.provider == "minimax" and config.model == "MiniMax-M3":
        from openai import OpenAI

        client = OpenAI(base_url=config.base_url, api_key=config.api_key, timeout=120, max_retries=1)
        response = None
        for attempt in range(2):
            prompt = user_prompt if attempt == 0 else (
                user_prompt + "\n上一次输出不符合格式。只返回一个含 events 数组的 JSON 对象，"
                "数组长度和输入完全一致，不要解释。"
            )
            completion = client.chat.completions.create(
                model=config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_completion_tokens=4000,
                extra_body={"thinking": {"type": "disabled"}, "reasoning_split": True},
            )
            choice = completion.choices[0]
            if choice.finish_reason != "stop" or not choice.message.content:
                continue
            response = _parse_json_text(choice.message.content)
            if isinstance(response, dict) and isinstance(response.get("events"), list):
                break
        if not isinstance(response, dict) or not isinstance(response.get("events"), list):
            raise RuntimeError("MAP_TRANSLATION_INVALID: model did not return a JSON event list")
    else:
        response = generate_json(
            config, system_prompt=system_prompt, user_prompt=user_prompt, max_tokens=6000,
        )
    return _translations_omitting_unsafe(snapshot, response)
