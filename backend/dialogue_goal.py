"""Evidence-based, conversation-scoped goal accounting."""

import time

GENERATION = {"generate_news", "generate_digest"}
STAGES = {"local": "本地生成", "reviewed": "审查通过", "saved": "平台草稿已确认"}


def goal_evidence(goal, actions):
    posts = {stage: set() for stage in STAGES}
    materials = set()
    actions_done = set()
    for row in actions:
        if (not goal["started_at"] <= row["created_at"] <= goal.get("ended_at", float("inf"))
                or row["status"] != "succeeded"):
            continue
        result = row["result"]
        if result.get("ok") is False or result.get("status") == "uncertain":
            continue
        tool = row["tool"]
        ids = {item for item in [*(result.get("post_ids") or []), result.get("post_id")]
               if isinstance(item, str) and item}
        if tool in GENERATION:
            posts["local"].update(ids)
        elif tool == "review_draft" and result.get("passed") is True:
            post_id = row.get("arguments", {}).get("post_id")
            posts["reviewed"].update(ids | ({post_id} if post_id else set()))
        elif tool == "save_draft" and result.get("platform_receipt_confirmed") is True:
            posts["saved"].update(ids)
        elif tool in {"news_search", "list_materials"} and not result.get("query_gap"):
            materials.update(item["material_id"] for item in result.get("materials", [])
                             if item.get("material_id"))
        elif tool == "create_image":
            actions_done.update((tool, post_id) for post_id in ids)
        elif tool in {"sync_account", "knowledge_search", "mcp_call"}:
            actions_done.add((tool,))
    counts = {stage: len(ids) for stage, ids in posts.items()}
    return counts, sum(counts.values()) + len(materials) + len(actions_done)


def settle_goal(goal, actions):
    counts, progress = goal_evidence(goal, actions)
    turns = goal["turns"] + 1
    stage = goal["stage"]
    if stage in counts and counts[stage] >= goal["target"]:
        state, reason = "completed", "已核对目标所需的交付记录。"
        goal = {**goal, "ended_at": time.time()}
    elif turns >= goal["max_turns"]:
        state, reason = "blocked", "已到轮次上限，现有产物保留。可调整上限后恢复。"
    elif progress <= goal.get("progress", 0):
        state = "blocked"
        reason = ("本轮没有新的可核验进展。补充要求后恢复，或核对产物并确认完成。"
                  if stage == "manual" else "本轮没有新的可核验进展，请检查结果或补充要求后恢复。")
    else:
        state, reason = "active", ""
    return {**goal, "state": state, "reason": reason, "turns": turns, "progress": progress}
