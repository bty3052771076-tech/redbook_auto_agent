"""Read-only presentation of persisted execution evidence; never invokes a model."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path


ACTIVE = {"queued", "running", "waiting_user", "stopping"}
LABELS = {
    "queued": "等待启动", "running": "执行中", "waiting_user": "等待处理",
    "stopping": "正在停止", "completed": "执行完成", "success": "执行完成",
    "partial_success": "部分完成", "partial": "部分完成", "failed": "执行失败",
    "cancelled": "已取消", "interrupted": "运行中断", "paused": "已暂停",
}
STAGES = {
    "sync_context": "同步账号与知识库", "plan": "规划任务", "generate": "生成内容与配图",
    "review": "审查内容与配图", "upload": "保存平台草稿", "upload_batch": "保存平台草稿",
    "recover": "处理异常", "finish": "汇总结果",
    "wool_result": "AI福利核验结果",
}
KINDS = {
    "daily_news": "每日新闻", "daily_ai_digest": "每日AI讯息", "daily_global_map": "全球事件关注图",
    "daily_wow": "每日我去", "daily_wool": "每日羊毛",
}
LINE = re.compile(r"^\[([^]]+)\]\s+stage=([^|]+)\|\s*([^|]+)(?:\|\s*(.*))?$")
ITEM = re.compile(r"^(\d+):([a-f0-9]{32})(?::[a-f0-9]+)?$")


def is_status_question(text: str) -> bool:
    text = text.strip()
    if len(text) > 160 or re.search(r"生成|重做|重新|上传|发布|删除|停止|取消|恢复|执行|添加|修改", text):
        return False
    return bool(re.search(r"进度|状态|到哪|哪一步|卡在|还要多久|用了多久|完成了|完成没|成功了|结束了|怎么样了", text))


def read_checkpoint(root: Path, run_id: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        return {}
    try:
        value = json.loads((root / "data/runs/agent" / run_id / "checkpoint.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def checkpoint_job_records(checkpoint: dict) -> list[dict]:
    """Combine durable per-job records with the latest active-job snapshot."""
    current = int(checkpoint.get("job_index") or 0)
    states = checkpoint.get("job_states") or {}
    records = []
    for index, _ in enumerate(checkpoint.get("jobs") or []):
        record = dict(states.get(str(index)) or {})
        if index == current:
            for key in ("post_ids", "reviewed_post_ids"):
                if key in checkpoint:
                    record[key] = checkpoint[key]
        records.append(record)
    return records


def run_overview(run: dict) -> dict:
    status = str(run.get("status") or "unknown")
    stage = STAGES.get(str(run.get("stage") or ""), str(run.get("stage") or "等待运行记录"))
    return {"status_label": LABELS.get(status, "状态未记录"), "display_message": stage}


def _issue(text: str) -> dict:
    replacements = {
        "MAP_TRANSLATION_INVALID": ("地图事件整理失败：模型没有返回有效的事件列表。", "检查模型响应和地图整理日志；只修复失败栏目，不重复上传成功稿件。"),
        "XHS_WRITE_UNCERTAIN": ("平台保存结果不确定，尚不能确认成功。", "先在专用浏览器核对平台草稿，再决定是否恢复，避免重复上传。"),
        "XHS_LOGIN": ("小红书登录状态需要确认。", "到连接与模型页面打开项目专用浏览器完成登录。"),
        "429": ("模型或信源请求受到限流。", "等待服务端限流解除；检查订阅额度或请求日志后再恢复。"),
        "retry budget exhausted": ("本栏目已达到重试上限。", "检查此前错误并修复失败栏目，已完成的栏目不需要重做。"),
    }
    for code, (message, action) in replacements.items():
        if code in text:
            return {"message": message, "action": action}
    clean = text
    for kind, label in KINDS.items():
        clean = clean.replace(kind, label)
    clean = re.sub(r"^(?:agent issue:|generation_error:|upload_error:|error:)\s*", "", clean)
    return {"message": clean or "任务没有记录具体错误原因。", "action": "查看技术日志定位失败步骤；核对平台已有草稿后再恢复任务，不要直接重复提交。"}


def _events(run: dict, checkpoint: dict) -> list[dict]:
    structured = [dict(e, source="checkpoint") for e in checkpoint.get("events", []) if isinstance(e, dict)]
    events = list(structured)
    for raw in run.get("events", []):
        if not isinstance(raw, dict):
            continue
        match = LINE.match(str(raw.get("message") or ""))
        if not match:
            continue
        command, node, status, detail = [part.strip() if part else "" for part in match.groups()]
        at = float(raw.get("at") or 0)
        if any(e.get("node") == node and e.get("status") == status and e.get("detail") == detail
               and abs(float(e.get("at") or 0) - at) < 2 for e in structured):
            continue
        events.append({"id": raw.get("id"), "at": at, "node": node, "status": status,
                       "detail": detail, "source": command, "agent": command == "agent"})
    return sorted(events, key=lambda e: float(e.get("at") or 0))


def build_activity(run: dict, checkpoint: dict, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    status = str(run.get("status") or "unknown")
    active = status in ACTIVE
    started = float(run.get("started_at") or run.get("created_at") or 0)
    ended = float(run.get("ended_at") or 0)
    elapsed = max(0, (now if active or not ended else ended) - started) if started else 0
    jobs = [{"kind": str(j.get("kind") or ""), "title": str(j.get("title") or KINDS.get(j.get("kind"), "采编任务")),
             "requested": int(j.get("count") or 1), "generated": None, "reviewed": None,
             "saved": 0, "verified": 0, "local": 0, "retained": 0, "status": "pending"}
            for j in checkpoint.get("jobs", []) if isinstance(j, dict)]
    ids = [{"saved": set(), "local": set()} for _ in jobs]
    events = _events(run, checkpoint)
    cursor = 0
    stage = "等待运行记录"
    timeline = []
    failures = []
    wool_notice = next((str(record.get("wool_notice"))
                        for record in checkpoint_job_records(checkpoint) if record.get("wool_notice")), "")
    for e in events:
        node, detail, state = str(e.get("node") or ""), str(e.get("detail") or ""), str(e.get("status") or "")
        kind = next((k for k in KINDS if re.search(rf"\b{re.escape(k)}\b", detail)), "")
        if kind:
            cursor = next((i for i, j in enumerate(jobs) if j["kind"] == kind), cursor)
        job = jobs[cursor] if cursor < len(jobs) else None
        stage = STAGES.get(node, node)
        label = job["title"] if job else KINDS.get(kind, "任务")
        agent = e.get("source") == "checkpoint" or e.get("agent")
        count = re.search(r"\b(?:posts|reused)=(\d+)", detail)
        if agent and job and state == "success" and count and node in {"generate", "review"}:
            field = "generated" if node == "generate" else "reviewed"
            job[field] = max(job[field] or 0, int(count[1]))
        post = re.search(r"\bpost=([a-f0-9]{32})\b", detail)
        if agent and job and node == "upload" and post:
            if state == "success":
                ids[cursor]["saved"].add(post[1])
            elif "local_only" in detail:
                ids[cursor]["local"].add(post[1])
        if state in {"failed", "warning"} and agent:
            failures.append(_issue(detail))
        if node == "wool_result" and "message=" in detail:
            wool_notice = detail.split("message=", 1)[1].strip()
            sentence = wool_notice
        elif node == "finish":
            sentence = "运行结束，正在汇总交付结果。"
        elif node == "plan" and "summary=" in detail:
            sentence = "任务安排：" + detail.split("summary=", 1)[1].strip()
        elif state == "failed":
            sentence = f"{label} · {stage}未完成：{_issue(detail)['message']}"
        elif node == "recover":
            sentence = f"{label}出现异常，{'正在尝试恢复' if state == 'retry' else '已记录处理结果'}。"
        elif agent and count and state == "success" and node in {"generate", "review"}:
            sentence = f"{label}：{'生成' if node == 'generate' else '通过审查'} {count[1]} 条。"
        elif node == "upload" and state == "success":
            sentence = f"{label}：一条草稿已保存到平台。"
        elif agent:
            sentence = f"{label} · {stage}{'已完成' if state == 'success' else '进行中'}。"
        else:
            # Chinese workflow details already describe the action. Hide transport/config fields.
            sentence = detail if detail and not re.search(r"(?:[a-z_]+=|https?://|[A-Z]:\\)", detail) else stage
            if state == "failed":
                sentence = f"{stage}遇到异常，正在检查后续步骤。"
        entry = {"id": f"{e.get('source')}:{e.get('id')}", "at": float(e.get("at") or 0),
                 "text": sentence, "status": state, "stage": stage}
        if timeline and timeline[-1]["text"] == sentence:
            timeline[-1] = entry
        else:
            timeline.append(entry)
    for key, value in checkpoint.get("item_status", {}).items():
        match = ITEM.match(key)
        if not match or int(match[1]) >= len(jobs):
            continue
        index, post_id = int(match[1]), match[2]
        if value in {"saved", "skipped_local"}:
            ids[index]["saved" if value == "saved" else "local"].add(post_id)
    verified = {row.get("id") for row in run.get("post_rows", []) if row.get("readback") == "verified"}
    current = int(checkpoint.get("job_index") or 0)
    for index, record in enumerate(checkpoint_job_records(checkpoint)):
        for key, field in (("post_ids", "generated"), ("reviewed_post_ids", "reviewed")):
            if key in record:
                jobs[index][field] = len(set(record[key] or []))
        retained = set(record.get("reviewed_post_ids") or [])
        retained -= set(checkpoint.get("uploaded_post_ids") or [])
        retained -= ids[index]["saved"] | ids[index]["local"]
        jobs[index]["retained"] = len(retained)
    failed = checkpoint.get("failed_jobs") or []
    for index, job in enumerate(jobs):
        saved, local = ids[index]["saved"], ids[index]["local"]
        job.update(saved=len(saved), verified=len(saved & verified), local=len(local))
        delivered = len(saved | local)
        for field in ("generated", "reviewed"):
            if delivered:
                job[field] = max(job[field] or 0, delivered)
        if index in failed:
            job["status"] = "failed"
        elif delivered >= job["requested"]:
            job["status"] = "completed"
        elif active and index == current:
            job["status"] = "running"
        elif index < current or (not active and delivered):
            job["status"] = "partial"
        elif not active:
            job["status"] = "not_completed"
    counts = {field: (sum(j[field] or 0 for j in jobs) if any(j[field] is not None for j in jobs) else None)
              for field in ("generated", "reviewed")}
    all_saved = set(checkpoint.get("uploaded_post_ids") or []) | set().union(*(s["saved"] for s in ids))
    all_saved.update(row["id"] for row in run.get("post_rows", [])
                     if row.get("status") == "saved_as_draft" or row.get("readback") == "verified")
    counts.update(saved=len(all_saved), verified=len(all_saved & verified), local=sum(j["local"] for j in jobs),
                  retained=sum(j["retained"] for j in jobs))
    requested = sum(j["requested"] for j in jobs) if jobs else None
    last_update = max([started] + [float(e.get("at") or 0) for e in run.get("events", []) if isinstance(e, dict)]
                      + [float(e.get("at") or 0) for e in events]) or None
    current_job = jobs[current]["title"] if current < len(jobs) else ""
    if active:
        headline = f"{current_job + ' · ' if current_job else ''}{stage}" if events else "等待任务启动或下一条运行记录"
        summary = f"正在执行；已保存平台草稿 {counts['saved']} 条，回读确认 {counts['verified']} 条。"
        issues = failures[-1:] if events and events[-1].get("status") in {"failed", "warning", "retry"} else []
    else:
        headline = LABELS.get(status, "状态未记录")
        summary = f"本次运行已结束：保存平台草稿 {counts['saved']} 条，平台回读确认 {counts['verified']} 条。"
        if counts["saved"] > counts["verified"]:
            summary += f"其中 {counts['saved'] - counts['verified']} 条的回读未确认。"
        if counts["local"]:
            summary += f"另有 {counts['local']} 条仅在本地，未上传平台。"
        if failed:
            summary += " 未完成：" + "、".join(jobs[i]["title"] for i in failed if isinstance(i, int) and i < len(jobs)) + "。"
        issues = failures[-3:] if status in {"partial_success", "partial", "failed", "interrupted", "paused", "waiting_user"} else []
        if not issues and status in {"failed", "interrupted", "paused"}:
            issues = [_issue(str(run.get("message") or checkpoint.get("last_failure") or "未记录具体错误原因"))]
    if counts["retained"]:
        summary += f" 已保留通过审查的稿件 {counts['retained']} 条，待继续上传。"
    if wool_notice:
        summary += " " + wool_notice
    issues = list({issue["message"]: issue for issue in issues}.values())
    return {"status": status, "status_label": LABELS.get(status, "状态未记录"), "active": active,
            "headline": headline, "summary": summary, "stage": stage, "current_job": current_job,
            "started_at": started or None, "ended_at": ended or None, "elapsed_seconds": elapsed,
            "last_update": last_update, "requested": requested, "counts": counts, "jobs": jobs,
            "timeline": timeline[-16:], "issues": issues}


def activity_reply(activity: dict) -> str:
    elapsed = int(activity["elapsed_seconds"])
    lines = [f"{activity['headline']}。已用时 {elapsed // 60} 分 {elapsed % 60} 秒。", activity["summary"]]
    for job in activity["jobs"]:
        lines.append(f"{job['title']}：目标 {job['requested']} 条，通过审查 {job['reviewed'] if job['reviewed'] is not None else '待记录'} 条，"
                     f"保存平台 {job['saved']} 条，回读确认 {job['verified']} 条。")
    for issue in activity["issues"]:
        lines.extend([issue["message"], "下一步：" + issue["action"]])
    if activity["active"]:
        lines.append("预计剩余时间尚无可靠依据；我会继续显示实际运行进度。")
    return "\n".join(lines)
