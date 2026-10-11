from backend.progress import build_activity


def test_candidate_stages_are_deduplicated_and_repairs_are_current():
    checkpoint = {"jobs": [{"kind": "daily_news", "count": 10}], "job_index": 0}
    values = [("a", "text_approved"), ("a", "text_approved"), ("a", "repairing"),
              ("a", "image_ready"), ("b", "repairing"), ("b", "rejected")]
    run = {"status": "running", "events": [
        {"id": i, "at": i + 1, "message": f"[auto] stage=pipeline_item | success | kind=daily_news item={item} phase={phase}"}
        for i, (item, phase) in enumerate(values)]}
    result = build_activity(run, checkpoint, now=10)
    assert result["pipeline"] == {"text_approved": 1, "image_ready": 1, "repairing": 0, "rejected": 1}
    assert result["counts"]["saved"] == 0
    assert "图片就绪" in str(result["timeline"])


def test_old_run_does_not_invent_pipeline_counts():
    assert build_activity({"status": "running"}, {}, now=10)["pipeline"] is None
