import json

import pytest

from backend.progress import build_activity, is_status_question, read_checkpoint


RID = "a" * 32
P1 = "b" * 32
P2 = "c" * 32


def event(index, stage, status, detail, at=100):
    return {"id": index, "at": at, "message": f"[agent] stage={stage} | {status} | {detail}"}


def test_running_progress_translates_stages_without_claiming_upload():
    run = {"id": RID, "status": "running", "started_at": 50, "events": [
        event(1, "sync_context", "success", "daily_news"),
        event(2, "generate", "success", "daily_news posts=2 attempt=1"),
        {"id": 3, "at": 110, "message": "[auto] stage=生成配图 | in_progress | 生成配图：第 2/2 条"},
    ]}
    cp = {"jobs": [{"kind": "daily_news", "title": "每日新闻", "count": 2}], "job_index": 0}
    result = build_activity(run, cp, now=120)
    assert result["stage"] == "生成配图"
    assert result["counts"]["generated"] == 2
    assert result["counts"]["saved"] == 0
    assert result["elapsed_seconds"] == 70
    assert result["last_update"] == 110
    assert "stage=" not in result["headline"]
    assert "保存" not in result["headline"]


def test_counts_use_unique_post_ids_and_readback_evidence():
    run = {"id": RID, "status": "completed", "started_at": 50, "ended_at": 130,
           "post_rows": [{"id": P1, "readback": "verified"}, {"id": P2, "readback": "unverified"}]}
    cp = {"jobs": [{"kind": "daily_news", "title": "每日新闻", "count": 2}], "job_index": 1,
          "uploaded_post_ids": [P1, P1, P2], "item_status": {
              f"0:{P1}:first": "saved", f"0:{P1}:second": "saved", f"0:{P2}:first": "saved"}}
    result = build_activity(run, cp, now=999)
    assert result["counts"]["saved"] == 2
    assert result["counts"]["verified"] == 1
    assert result["elapsed_seconds"] == 80
    assert "未确认" in result["summary"]


def test_partial_completion_preserves_success_and_explains_failure():
    run = {"id": RID, "status": "partial_success", "events": [
        event(10, "generate", "success", "daily_news posts=2 attempt=1"),
        event(11, "upload", "success", f"daily_news post={P1} xhs"),
        event(12, "sync_context", "success", "daily_global_map"),
        event(13, "generate", "failed", "generation_error: MAP_TRANSLATION_INVALID: model did not return a JSON event list"),
        event(14, "finish", "partial", "uploaded=1"),
    ]}
    cp = {"jobs": [{"kind": "daily_news", "title": "每日新闻", "count": 2},
                   {"kind": "daily_global_map", "title": "全球事件关注图", "count": 1}],
          "job_index": 2, "failed_jobs": [1], "item_status": {f"0:{P1}:v": "saved"}}
    result = build_activity(run, cp, now=120)
    assert result["status_label"] == "部分完成"
    assert result["counts"]["saved"] == 1
    assert result["jobs"][1]["status"] == "failed"
    assert "地图" in result["issues"][0]["message"]
    assert result["issues"][0]["action"]


def test_local_delivery_never_claims_remote_success():
    run = {"id": RID, "status": "completed"}
    cp = {"jobs": [{"kind": "daily_ai_digest", "title": "每日AI讯息", "count": 1}],
          "job_index": 1, "item_status": {f"0:{P1}:cafe": "skipped_local"}}
    result = build_activity(run, cp, now=120)
    assert result["counts"]["local"] == 1
    assert result["counts"]["saved"] == 0
    assert "仅在本地" in result["summary"]


def test_missing_checkpoint_is_not_fake_completion_and_waiting_is_visible():
    result = build_activity({"id": RID, "status": "running", "started_at": 50}, {}, now=120)
    assert result["requested"] is None
    assert result["counts"]["generated"] is None
    assert result["last_update"] == 50
    assert result["active"] is True
    assert "等待" in result["headline"]


def test_upload_counts_update_from_saved_post_records_before_batch_finishes():
    result = build_activity({"id": RID, "status": "running", "post_rows": [
        {"id": P1, "status": "saved_as_draft", "readback": "verified"},
        {"id": P2, "status": "approved", "readback": "unverified"},
    ]}, {}, now=120)
    assert result["counts"]["saved"] == 1
    assert result["counts"]["verified"] == 1


def test_controller_explanation_is_visible_without_config_fields():
    result = build_activity({"id": RID, "status": "running", "events": [
        event(1, "plan", "success", "jobs=2 index=0 provider=minimax summary=先生成新闻，再整理全球事件地图。")
    ]}, {}, now=120)
    assert result["timeline"][-1]["text"] == "任务安排：先生成新闻，再整理全球事件地图。"


def test_error_is_actionable_and_interrupted_is_not_retried():
    result = build_activity({"id": RID, "status": "interrupted", "message": "服务曾中断。先核对平台草稿，不会自动重传。"}, {}, now=120)
    assert result["status_label"] == "运行中断"
    assert "核对" in result["issues"][0]["action"]


def test_duplicate_retry_events_do_not_inflate_counts_or_timeline():
    run = {"id": RID, "status": "running", "events": [
        event(1, "generate", "success", "daily_news posts=2 attempt=1"),
        event(2, "generate", "success", "daily_news reused=2"),
        event(3, "upload", "success", f"daily_news post={P1} xhs"),
        event(4, "upload", "skipped", f"daily_news post={P1} already_complete"),
    ]}
    result = build_activity(run, {"jobs": [{"kind": "daily_news", "title": "每日新闻", "count": 2}]}, now=120)
    assert result["counts"]["generated"] == 2
    assert result["counts"]["saved"] == 1
    assert len(result["timeline"]) <= 4


def test_checkpoint_reader_rejects_paths_and_tolerates_partial_json(tmp_path):
    path = tmp_path / "data/runs/agent" / RID / "checkpoint.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    assert read_checkpoint(tmp_path, RID) == {}
    path.write_text(json.dumps({"jobs": []}), encoding="utf-8")
    assert read_checkpoint(tmp_path, RID) == {"jobs": []}
    assert read_checkpoint(tmp_path, "../bad") == {}


@pytest.mark.parametrize("text", ["进度如何？", "现在进行到哪一步了", "每日AI讯息完成了吗？", "卡在哪里？", "还要多久", "查看当前任务状态"])
def test_status_questions_are_read_only(text):
    assert is_status_question(text)


@pytest.mark.parametrize("text", ["生成10条每日新闻", "重新生成每日AI讯息并上传", "生成1条每日新闻，完成后告诉我进度", "停止当前任务", "删除草稿"])
def test_new_task_or_control_command_is_not_a_status_question(text):
    assert not is_status_question(text)
