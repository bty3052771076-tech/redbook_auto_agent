from backend.dialogue_goal import goal_evidence, settle_goal


def test_goal_counts_only_distinct_current_evidence_and_positive_receipts():
    goal = {"started_at": 5, "turns": 0, "stage": "saved", "target": 2, "max_turns": 4}
    def row(tool, created_at, status="succeeded", **result):
        return dict(tool=tool, created_at=created_at, status=status, result=result)
    rows = [
        row("generate_news", 4, post_ids=["old-post"]),
        row("generate_news", 5, post_ids=["new-post", "new-post"]),
        row("save_draft", 6, "uncertain", post_ids=["unknown"], platform_receipt_confirmed=True),
        row("save_draft", 7, post_ids=["no-receipt"]),
        row("save_draft", 8, post_ids=["new-post"], platform_receipt_confirmed=True),
    ]
    assert goal_evidence(goal, rows)[0] == {"local": 1, "reviewed": 0, "saved": 1}
    current = settle_goal(goal, rows)
    assert current["state"] == "active"
    assert settle_goal(current, rows)["state"] == "blocked"
    rows.append(row("save_draft", 9, post_ids=["other-post"], platform_receipt_confirmed=True))
    complete = settle_goal(current, rows)
    assert complete["state"] == "completed"
    rows.append(row("save_draft", complete["ended_at"] + 1, post_ids=["later-turn"], platform_receipt_confirmed=True))
    assert goal_evidence(complete, rows)[0]["saved"] == 2


def test_goal_turn_limit_is_not_completion():
    goal = {"started_at": 0, "turns": 1, "stage": "local", "target": 2, "max_turns": 2}
    row = dict(tool="generate_news", created_at=1, status="succeeded", result={"post_ids": ["one"]})
    next_goal = settle_goal(goal, [row])
    assert next_goal["state"] == "blocked" and next_goal["turns"] == 2
