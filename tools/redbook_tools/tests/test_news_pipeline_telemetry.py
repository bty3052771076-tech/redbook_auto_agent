from src.workflow.performance import RunContext, summarize_performance


def span(request, start, end, *, execution="a", attempt=1):
    return {"request_id": request, "execution_id": execution, "stage": "text",
            "started_at": start, "finished_at": end, "service_ms": (end-start)*1000,
            "queue_ms": 0, "attempt": attempt, "status": "success"}


def test_overlapping_spans_do_not_sum_into_wall_time():
    result = summarize_performance([span("one", 0, 10), span("two", 5, 12)])
    assert result["active_seconds"] == 12
    assert result["service_seconds"] == 17


def test_resume_wait_is_not_active_runtime():
    result = summarize_performance([span("one", 0, 10), span("two", 100, 105, execution="b")])
    assert result["active_seconds"] == 15
    assert result["wall_seconds"] == 105
    assert result["inactive_seconds"] == 90


def test_retry_and_request_counts_are_distinct():
    first, retry = span("one", 0, 1), span("two", 2, 3, attempt=2)
    result = summarize_performance([first, retry, retry])
    assert result["requests"] == 2
    assert result["retries"] == 1


def test_secrets_are_redacted_but_token_counts_remain(tmp_path):
    import json
    context = RunContext.create(telemetry_dir=tmp_path)
    context.record("text", "success", api_key="PRIVATE", input_tokens=10, output_tokens=20)
    event = json.loads(context.telemetry_path.read_text(encoding="utf-8").splitlines()[-1])
    assert "PRIVATE" not in str(event)
    assert event["details"]["input_tokens"] == 10
    assert event["details"]["output_tokens"] == 20


def test_deadline_change_cannot_change_elapsed_clock(tmp_path):
    import json
    context = RunContext.create(telemetry_dir=tmp_path)
    context.deadline += 10000
    context.record("text", "success")
    event = json.loads(context.telemetry_path.read_text(encoding="utf-8").splitlines()[-1])
    assert 0 <= event["elapsed_s"] < 5


def test_unfinished_request_not_counted_as_success_or_offline_service():
    result = summarize_performance([span("one", 0, 1), {"request_id":"lost", "started_at":2, "status":"started"}])
    assert result["unfinished_requests"] == 1
    assert result["service_seconds"] == 1
