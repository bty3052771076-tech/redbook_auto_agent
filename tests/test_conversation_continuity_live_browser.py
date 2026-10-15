"""Real GUI/HTTP/PostgreSQL continuity and warm-request acceptance evidence.

Run with the agent's existing interpreter and PYTHONPATH=<worktree>;<tools>:
    python tests/test_conversation_continuity_live_browser.py

The entry point confines even pytest's disposable database bootstrap files to
the evidence directory. No model or platform endpoint is used.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import io
import json
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from types import MethodType, SimpleNamespace
from uuid import UUID, uuid4
import zipfile

import pytest


OUTPUT = Path("E:/AI/codex/redbook_runtime/data/tmp/conversation-continuity-20261015")
ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "5220fb64dccbb1d26a2481fcb96c9732afad4641"


def _baseline_child():
    """Fresh interpreter bridge; production imports must resolve inside git archive."""
    import inspect
    import httpx
    import uvicorn
    from apps.web_service import Workbench
    from backend import app as old
    from backend import task_recognition as recognition
    from src.agent.conversation_store import PostgresConversationStore
    from src.knowledge.store import KnowledgeStore

    seed = json.load(sys.stdin)
    source = Path(seed["source"])
    assert Path(old.__file__).resolve() == source / "backend/app.py"
    assert Path(inspect.getfile(Workbench)).resolve() == source / "tools/redbook_tools/apps/web_service.py"
    root = Path(os.environ["REDBOOK_RUNTIME_ROOT"]).resolve()
    allowed = Path(seed['isolated_root']).resolve()
    assert allowed.is_relative_to(OUTPUT.parent.resolve()) and allowed != OUTPUT.parent.resolve()
    assert root == allowed / 'legacy-runtime' and source == allowed / 'legacy-source'
    assert seed['database'].startswith('redbook_agent_test_')
    store = KnowledgeStore.from_env()
    with store.connection() as conn:
        assert conn.execute("SELECT current_database() AS name").fetchone()["name"] == seed["database"]
    current = Workbench(root, conversation_store=PostgresConversationStore(store, namespace=seed["namespace"]))
    counters = Counter({"llm_calls": 0, "worker_launches": 0, "generated_posts": 0, "external_connections": 0})

    def denied(name):
        def call(*args, **kwargs):
            counters[name] += 1
            raise AssertionError("Legacy baseline side effect forbidden: " + name)
        return call

    old.recognition_call = recognition.call_model = denied("llm_calls")
    current.compact_agent_conversation = denied("llm_calls")
    current.submit = current._run = denied("worker_launches")
    current.append_agent_message = denied("generated_posts")
    current.providers = lambda: {"bindings": {}}
    current.models = lambda: {"rows": []}
    current.environment = lambda: {}
    real_connect = socket.socket.connect

    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            return denied("external_connections")()
        return real_connect(sock, address)

    socket.socket.connect = local_connect
    saved = seed["conversation"]
    saved.update(id=uuid4().hex, _revision=0, _last_message_seq=0, messages=saved["messages"][:2])
    message_ids = {message["id"]: uuid4().hex for message in saved["messages"]}
    for message in saved["messages"]:
        message["id"] = message_ids[message["id"]]
    for plan in saved["plans"]:
        if plan.get("source_message_id") in message_ids:
            plan["source_message_id"] = message_ids[plan["source_message_id"]]
    current.conversation_store.save(saved)
    job = seed["job"]
    job["status"] = "running"
    current.jobs[job["id"]] = job
    current.persist(job)
    old.app.state.service = current
    spans = []
    route = next(route for route in old.app.routes if getattr(route, "path", "") ==
                 "/api/conversations/{conversation_id}/messages")
    real_handler = route.dependant.call

    def timed_handler(conversation_id, body):
        started = time.perf_counter_ns()
        try:
            return real_handler(conversation_id, body)
        finally:
            spans.append((time.perf_counter_ns() - started) / 1e6)

    route.dependant.call = timed_handler
    listener = socket.socket()
    for port in range(49152, 65001):
        try:
            listener.bind(("127.0.0.1", port))
            break
        except OSError:
            continue
    else:
        raise AssertionError("No free legacy baseline port")
    os.environ["REDBOOK_AGENT_PORT"] = str(port)
    server = uvicorn.Server(uvicorn.Config(old.app, host="127.0.0.1", port=port,
                                           log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "port": port,
              "namespace": seed["namespace"], "database": seed["database"], "samples": []}
    try:
        deadline = time.monotonic() + 15
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30) as client:
            assert client.post("/api/session", headers={"X-Workbench": "1"}).status_code == 200
            for index in range(110):
                started = time.perf_counter_ns()
                response = client.post(f"/api/conversations/{saved['id']}/messages",
                    headers={"X-Workbench": "1"}, json={"content": "现在到哪了？"})
                elapsed = (time.perf_counter_ns() - started) / 1e6
                assert response.status_code == 200, response.text
                data = response.json()
                assert data["plan"] is None and data["run"]["activity"]["counts"]["saved"] == 7
                if index >= 10:
                    result["samples"].append({"index": index - 10, "ack_ms": elapsed, "handler_ms": spans[-1]})
        assert len(current.conversation_store.get(saved["id"])["plans"]) == 1
        assert set(counters.values()) == {0}, counters
        result.update(status="passed", finished_at=datetime.now(timezone.utc).isoformat(),
                      prewarm=10, measured=100, counters=dict(counters),
                      active_continue={"status": "not_comparable", "reason": "Legacy continue creates a plan, not same-goal resume; not executed"})
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        assert not thread.is_alive()
    print(json.dumps(result))
    return 0


def _main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    source = Path(os.environ.get("REDBOOK_RUNTIME_ROOT", "E:/AI/codex/redbook_runtime"))
    credentials = source / "data/knowledge/postgresql-local/connection.json"
    with tempfile.TemporaryDirectory(prefix="scratch-", dir=OUTPUT) as scratch:
        root = Path(scratch)
        destination = root / "data/knowledge/postgresql-local/connection.json"
        destination.parent.mkdir(parents=True)
        shutil.copyfile(credentials, destination)
        os.environ.update(REDBOOK_RUNTIME_ROOT=str(root), REDBOOK_TEST_POSTGRES="1",
                          REDBOOK_AGENT_PORT="8786", TEMP=str(root), TMP=str(root),
                          PYTHONDONTWRITEBYTECODE="1")
        tempfile.tempdir = str(root)
        result = pytest.main([str(Path(__file__).resolve()), "-q", "-ra", "-p", "no:cacheprovider",
                              "--basetemp", str(root / "cases")])
    report_path = OUTPUT / "continuity-performance.json"
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report.update(pytest_exit_code=int(result), scratch_removed=not root.exists())
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(result)


if __name__ == "__main__":
    raise SystemExit(_baseline_child() if "--baseline-child" in sys.argv else _main())


import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright

from apps.web_service import Workbench
from backend import app as module
from backend import capabilities, task_recognition
from backend.conversation_service import ConversationService
from src.agent.capabilities.service import CapabilityService
from src.agent.capabilities.store import CapabilityStore
from src.agent.editorial_agent import AgentJob, EditorialAgentConfig, EditorialAgentTools, run_editorial_agent
from src.agent.goal_store import GoalStore
from src.publish.draft_delivery import content_revision_fingerprint, has_current_draft_receipt
from src.storage.files import load_post, save_post
from src.storage.models import Post
from test_conversation_continuity import pg_isolated  # noqa: F401
from test_run_resume_contract import isolated, start_plan  # noqa: F401


class _Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        path = attrs.get("src") if tag == "script" else attrs.get("href") if tag == "link" else None
        if path and path.startswith("/assets/"):
            self.paths.append(path)


def _dist_evidence():
    dist = ROOT / "frontend/dist"
    parser = _Assets()
    parser.feed((dist / "index.html").read_text(encoding="utf-8"))
    assert parser.paths, "Build frontend before running this test; this test never rebuilds it"
    rows = []
    for relative in parser.paths:
        path = (dist / relative.lstrip("/")).resolve()
        assert path.is_relative_to(dist.resolve()) and path.is_file()
        rows.append({"asset": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                     "bytes": path.stat().st_size, "mtime": path.stat().st_mtime})
    latest_source = max(p.stat().st_mtime for p in (ROOT / "frontend/src").rglob("*") if p.is_file())
    return {"assets": rows, "latest_source_mtime": latest_source, "rebuild_performed": False,
            "source_newer_than_dist": min(row["mtime"] for row in rows) < latest_source}


@pytest.fixture(scope="module")
def evidence():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "worktree": str(ROOT),
              "dist": _dist_evidence(), "tests": {}, "method": {
                  "transport": "real loopback HTTP, no TestClient for scenario requests, no browser routes",
                  "database": "root conftest creates/drops a disposable PostgreSQL database; unique fixture namespaces",
                  "checkpoint": "real LangGraph PostgreSQL graph, production Workbench checkpoint reader",
                  "adapters": "existing isolated fixture freezes offline model/capability settings; controlled local graph tools",
                  "classification": "handle_message wall time INCLUDING PG routing/persistence, a conservative upper bound",
                  "ack": "httpx monotonic round trip INCLUDING full response body; sequential keep-alive, no concurrency load",
                  "baseline": "fresh subprocess from git archive of base commit; status only, old continue not_comparable",
                  "platform_receipts": "synthetic current-content receipts, NOT actual platform saves",
              }}
    yield result
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    passed = all(row.get('status') == 'passed' for row in result['tests'].values())
    result['expected_cases'] = 3
    result['status'] = ('passed' if len(result['tests']) == 3 else 'partial') if passed else 'failed'
    (OUTPUT / "continuity-performance.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def _wait(predicate, timeout=15):
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "Timed out waiting for owned server/worker"
        time.sleep(.01)


@pytest.fixture
def live(pg_isolated, monkeypatch, evidence, request):
    current, _, commands = pg_isolated
    row = {"status": "failed", "namespace": current.conversation_store.namespace,
           "stage": "setup", "lifespan": "off"}
    evidence["tests"][request.node.name] = row
    assert current.root.resolve().drive.upper() == "E:"
    monkeypatch.setenv("AGENT_CONVERSATION_CONTINUITY", "1")
    monkeypatch.delenv("AGENT_WEB_ATTEMPT_ID", raising=False)
    monkeypatch.setenv("MODEL_PLATFORMS_DIR", str(current.root / "model-platforms"))
    monkeypatch.setenv("MODEL_PLATFORMS_NAMESPACE", "agent")
    counters = Counter({name: 0 for name in ("llm_calls", "external_connections", "generated_posts",
                                            "worker_launches", "receipt_short_circuits", "real_platform_writes")})

    def deny_model(*args, **kwargs):
        counters["llm_calls"] += 1
        raise AssertionError("Live model calls are forbidden")

    monkeypatch.setattr(module, "recognition_call", deny_model)
    monkeypatch.setattr(task_recognition, "call_model", deny_model)
    monkeypatch.setattr(current, "compact_agent_conversation", deny_model)
    real_connect = socket.socket.connect

    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            counters["external_connections"] += 1
            raise AssertionError("External socket connection is forbidden")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    routing = []
    real_handle = ConversationService.handle_message

    def timed_handle(self, cid, payload, key):
        started = time.perf_counter_ns()
        try:
            return real_handle(self, cid, payload, key)
        finally:
            routing.append({"key": key, "content": payload["content"],
                            "ms": (time.perf_counter_ns() - started) / 1e6})

    monkeypatch.setattr(ConversationService, "handle_message", timed_handle)
    release = threading.Event()
    ready = threading.Event()
    holder = {"finish": False}
    workers, deliveries, generated, reviews, worker_errors = [], [], [], [], []
    goals = GoalStore(current.conversation_store.knowledge_store)

    def controlled_worker(job, args, env):
        workers.append(threading.current_thread())
        counters["worker_launches"] += 1
        if "--resume-from" not in args:
            return
        try:
            with current.lock:
                job.update(status="running", started_at=time.time())
                current.persist(job)
            ready.set()
            assert release.wait(600), "Owned worker was never released"
            if not holder["finish"]:
                return
            thread_id = args[args.index("--run-id") + 1]
            config = EditorialAgentConfig(checkpoint_dir=current.root / "data/runs/agent",
                    checkpoint_backend="postgres", goal_context=holder["goal_context"], retry_delay_s=0,
                    no_progress_limit=2,
                resume_from=Path(args[args.index("--resume-from") + 1]))
            holder["result"] = run_editorial_agent(holder["jobs"], tools=holder["tools"],
                                                   config=config, run_id=thread_id)
            with current.lock:
                job.update(status=holder["result"].status, ended_at=time.time(), exit_code=0)
                current.persist(job)
        except BaseException as error:
            worker_errors.append({"error": repr(error), "traceback": traceback.format_exc()})

    monkeypatch.setattr(current, "_run", controlled_worker)
    listener = socket.socket()
    for port in range(49152, 65001):
        try:
            listener.bind(("127.0.0.1", port))
            break
        except OSError:
            continue
    else:
        listener.close()
        pytest.fail("No unused browser-safe loopback port")
    monkeypatch.setenv("REDBOOK_AGENT_PORT", str(port))
    row["port"] = port
    server = uvicorn.Server(uvicorn.Config(module.app, host="127.0.0.1", port=port,
                                           log_level="error", lifespan="off"))
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    server_thread.start()
    try:
        _wait(lambda: server.started or not server_thread.is_alive())
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30) as client:
            assert client.get("/api/conversations").status_code == 403
            assert client.post("/api/session", headers={"X-Workbench": "1"}).status_code == 200
            cid, plan, original = start_plan((current, client, commands))
            _wait(lambda: counters["worker_launches"] == 1)
            monkeypatch.setattr(current, "_agent_checkpoint_state", MethodType(Workbench._agent_checkpoint_state, current))
            frozen = current._read_agent_conversation(cid)["plans"][0]["frozen_execution"]
            context = frozen["goal_context"]
            jobs = [AgentJob(j["kind"], j["title"], j["count"], job_id=j["job_id"]) for j in plan["jobs"]]
            data = current.root / "data"

            def new_posts(kind, count):
                generated.append({"kind": kind, "count": count})
                counters["generated_posts"] += count
                posts = [Post(title=f"{kind} local continuity {uuid4().hex[:8]}",
                              body="Controlled source-supported fixture; no provider request") for _ in range(count)]
                for post in posts:
                    save_post(post, base=data)
                return posts

            def generate(job, _context):
                return new_posts(job.kind, 9 if job.kind == "daily_news" else 1)

            def review(job, posts, _context):
                reviews.append(job.kind)
                if job.kind == "daily_news" and reviews.count(job.kind) > 1 and len(posts) < job.count:
                    posts.extend(new_posts(job.kind, job.count - len(posts)))
                return {"approved_post_ids": [p.id for p in posts], "errors": []}

            def upload(job, post, _context):
                if has_current_draft_receipt(post, platform="xhs"):
                    counters["receipt_short_circuits"] += 1
                    return True, "existing current receipt: no platform write"
                assert post.id not in deliveries, "Duplicate delivery adapter invocation"
                post.platform["xhs_draft"] = {"revision_fingerprint": content_revision_fingerprint(post)}
                save_post(post, base=data)
                deliveries.append(post.id)
                if len(deliveries) == 7:
                    goals.enqueue_turn(context["namespace"], cid, uuid4().hex,
                        {"kind": "pause_goal", "goal_id": context["goal_id"]}, 1, "live-browser-pause")
                return True, "controlled local receipt, not a platform write"

            tools = EditorialAgentTools(lambda _job: {}, generate, review, upload,
                                        load_posts=lambda ids: [load_post(pid, base=data) for pid in ids])
            holder.update(goal_context=context, jobs=jobs, tools=tools)
            first = run_editorial_agent(jobs, tools=tools, run_id=original["id"],
                config=EditorialAgentConfig(checkpoint_dir=current.root / "data/runs/agent",
                        checkpoint_backend="postgres", goal_context=context, retry_delay_s=0, no_progress_limit=2))
            assert first.status == "paused"
            current.jobs[original["id"]]["status"] = "interrupted"
            current.persist(current.jobs[original["id"]])
            before = goals.snapshot(context["namespace"], context["goal_id"])
            assert [(j["requested"], j["delivered_verified"], j["retained_ready"], j["new_generation_needed"])
                    for j in before["jobs"]] == [(10, 7, 2, 1), (1, 0, 0, 1), (1, 0, 0, 1)]
            checkpoint = current._agent_checkpoint_state(original["id"])
            assert checkpoint["run_id"] == original["id"] and checkpoint["status"] == "paused"
            with goals.knowledge_store.connection() as conn:
                database = conn.execute("SELECT current_database() AS name").fetchone()["name"]
                assert database.startswith("redbook_agent_test_")
                assert conn.execute("SELECT count(*) AS n FROM checkpoints WHERE thread_id=%s",
                                    (original["id"],)).fetchone()["n"] > 0
            # Replace only the fixture's offline capability facade, not an HTTP endpoint.
            cap = CapabilityService(current.root, store=CapabilityStore(goals.knowledge_store,
                                    namespace=context["namespace"]), workbench=current)
            monkeypatch.setattr(capabilities, "manager", lambda service: cap if service is current else pytest.fail("Unexpected global service"))
            row.update(stage="ready", database=database, conversation_id=cid, goal_id=context["goal_id"],
                       original_thread=original["id"], before_jobs=deepcopy(before["jobs"]),
                       retained_post_ids=[a["post_id"] for a in before["artifacts"] if a["eligibility_state"] == "approved"])
            yield SimpleNamespace(current=current, client=client, cid=cid, plan=plan, original=original,
                goal=before, context=context, goals=goals, commands=commands, row=row, counters=counters,
                routing=routing, ready=ready, holder=holder, release=release, workers=workers,
                worker_errors=worker_errors, generated=generated, deliveries=deliveries,
                base_url=f"http://127.0.0.1:{port}")
    finally:
        release.set()
        if holder["finish"] and any(worker.is_alive() for worker in workers):
            goals.set_state(holder["goal_context"]["namespace"], holder["goal_context"]["goal_id"], "cancelled")
        for worker in workers:
            worker.join(timeout=30)
        server.should_exit = True
        server_thread.join(timeout=15)
        listener.close()
        row.update(counters=dict(counters), worker_errors=worker_errors,
                   generated_batches=generated, distinct_local_receipts=len(set(deliveries)),
                   owned_threads_stopped=not server_thread.is_alive() and not any(w.is_alive() for w in workers))
        if worker_errors or not row["owned_threads_stopped"]:
            row["status"] = "failed"
        assert row["owned_threads_stopped"], "Owned server or worker leaked"
        assert not worker_errors, worker_errors
        assert counters["llm_calls"] == counters["external_connections"] == 0


def _payload(live, content, attempt=None):
    return {"content": content, "client_message_id": str(uuid4()), "goal_id": live.goal["goal_id"],
            "expected_goal_revision": live.goal["revision"],
            "expected_attempt_id": attempt or live.original["id"]}


def _post(live, payload, key=None):
    return live.client.post(f"/api/conversations/{live.cid}/messages", json=payload,
                           headers={"X-Workbench": "1", "Idempotency-Key": key or str(uuid4())})


def _assert_active(live, response):
    assert response.status_code in {200, 202}, response.text
    data = response.json()
    assert data["action"] == "continue_goal"
    assert data["goal"]["goal_id"] == live.goal["goal_id"]
    assert data["plan"] is None
    assert live.ready.wait(5)
    assert len(live.commands) == live.counters["worker_launches"] == 2
    args = live.commands[-1]
    assert args[args.index("--run-id") + 1] == live.original["id"]
    assert Path(args[args.index("--resume-from") + 1]).parent.name == live.original["id"]
    assert len(live.current._read_agent_conversation(live.cid)["plans"]) == 1
    return data


def _finish(live):
    live.holder["finish"] = True
    live.release.set()
    for worker in live.workers:
        worker.join(timeout=30)
    if any(worker.is_alive() for worker in live.workers):
        live.goals.set_state(live.context["namespace"], live.goal["goal_id"], "cancelled")
        for worker in live.workers:
            worker.join(timeout=10)
        pytest.fail("Owned graph exceeded test watchdog; cancelled only its isolated PG goal")
    assert not any(worker.is_alive() for worker in live.workers)
    assert not live.worker_errors, live.worker_errors
    result = live.holder["result"]
    assert result.status == "completed" and result.run_id == live.original["id"]
    after = live.goals.snapshot(live.context["namespace"], live.goal["goal_id"])
    assert after["state"] == "completed"
    assert [j["delivered_verified"] for j in after["jobs"]] == [10, 1, 1]
    assert len(live.deliveries) == len(set(live.deliveries)) == 12
    assert Counter((batch["kind"], batch["count"]) for batch in live.generated) == Counter({
        ("daily_news", 9): 1, ("daily_news", 1): 1,
        ("daily_ai_digest", 1): 1, ("daily_global_map", 1): 1})
    assert set(live.row["retained_post_ids"]).issubset(live.deliveries)
    assert len(live.commands) == live.counters["worker_launches"] == 2
    live.row.update(after_jobs=after["jobs"], completed_thread=result.run_id)


def _assert_layout(page, *, mobile):
    if mobile:
        page.wait_for_function("() => document.querySelector('.sidebar').getBoundingClientRect().right <= 0")
    else:
        assert page.evaluate("document.querySelector('.main-shell').getBoundingClientRect().left >= document.querySelector('.sidebar').getBoundingClientRect().right")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    checked = []
    for selector in ("h1", "#goal-select", "#prompt", ".composer .primary-button", ".plan-pane h2"):
        element = page.locator(selector)
        element.scroll_into_view_if_needed()
        assert element.evaluate("""element => {
            const box = element.getBoundingClientRect();
            const x = box.left + box.width / 2, y = box.top + box.height / 2;
            const hit = document.elementFromPoint(x, y);
            return box.left >= 0 && box.right <= innerWidth && y >= 0 && y <= innerHeight
                && hit !== null && (hit === element || element.contains(hit));
        }"""), f"Element is clipped or obscured: {selector}"
        checked.append(selector)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_function("() => window.scrollY === 0")
    return {"viewport": page.viewport_size, "unobscured_selectors": checked,
            "document_width": page.evaluate("document.documentElement.scrollWidth"),
            "sidebar_right": page.locator(".sidebar").evaluate("element => element.getBoundingClientRect().right")}


def test_live_gui_continuation_uuid_original_pg_thread_and_refresh(live):
    row = live.row
    row["stage"] = "gui"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True,
            args=["--disable-background-networking", "--disable-component-update"])
        try:
            context = browser.new_context(viewport={"width": 1366, "height": 900})
            page = context.new_page()
            errors, failed_api = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: failed_api.append({"url": response.url, "status": response.status})
                    if "/api/" in response.url and response.status >= 400 else None)
            try:
                page.goto(live.base_url, wait_until="networkidle")
                row["served_script_urls"] = page.locator("script[src]").evaluate_all("nodes => nodes.map(node => node.src)")
                page.locator("#conversation-select").select_option(live.cid)
                expect(page.get_by_label("当前目标", exact=True)).to_have_value(live.goal["goal_id"])
                progress = page.get_by_label("目标实际进度", exact=True)
                expect(progress).to_contain_text("7 / 10")
                page.screenshot(path=str(OUTPUT / "01-before-continue-desktop.png"), full_page=True)
                page.locator("#prompt").fill("继续")
                with page.expect_response(lambda response: response.request.method == "POST"
                        and response.url.endswith(f"/api/conversations/{live.cid}/messages")) as event:
                    page.get_by_role("button", name="发送", exact=True).click()
                response = event.value
                payload = response.request.post_data_json
                row["gui_request"] = {"payload": payload,
                                      "idempotency_key": response.request.headers.get("idempotency-key")}
                row["gui_http_status"] = response.status
                assert str(UUID(payload["client_message_id"])) == payload["client_message_id"]
                assert payload["goal_id"] == live.goal["goal_id"]
                assert payload["expected_attempt_id"] == live.original["id"]
                assert response.status == 202, response.text()
                accepted = response.json()
                assert accepted["action"] == "continue_goal" and accepted["plan"] is None
                assert accepted["run"]["agent_run_id"] == live.original["id"]
                assert live.ready.wait(5)
                expect(page.locator("#prompt")).to_have_value("")
                expect(page.locator(".plan-pane")).to_contain_text("10 条")
                page.reload(wait_until="networkidle")
                expect(page.locator("#conversation-select")).to_have_value(live.cid)
                expect(page.get_by_label("当前目标", exact=True)).to_have_value(live.goal["goal_id"])
                expect(progress).to_contain_text("7 / 10")
                expect(page.locator(".plan-pane")).to_contain_text("10 条")
                row["desktop_layout"] = _assert_layout(page, mobile=False)
                page.screenshot(path=str(OUTPUT / "02-continued-refreshed-desktop.png"), full_page=True)
                for width, height in [(390, 844)]:
                    page.set_viewport_size({"width": width, "height": height})
                    expect(progress).to_contain_text("7 / 10")
                    row["mobile_layout"] = _assert_layout(page, mobile=True)
                    page.screenshot(path=str(OUTPUT / "03-continued-mobile.png"), full_page=True, animations="disabled")
                # Replay the actual GUI identity over real HTTP, not a synthetic mock response.
                replay = _post(live, payload, row["gui_request"]["idempotency_key"])
                data = _assert_active(live, replay)
                assert data["turn"]["turn_id"] == accepted["turn"]["turn_id"]
                assert data["run"]["id"] == accepted["run"]["id"]
                saved = live.current.conversation_store.get(live.cid)
                assert len(saved["plans"]) == 1 and len(saved["runs"]) == 2
                assert sum(m.get("turn_id") == accepted["turn"]["turn_id"] for m in saved["messages"]) == 2
                row.update(gui_response={"turn_id": accepted["turn"]["turn_id"],
                    "turn_state": accepted["turn"]["state"], "goal_id": accepted["goal"]["goal_id"],
                    "attempt_id": accepted["run"]["id"], "thread_id": accepted["run"]["agent_run_id"]},
                    plan_count_after_refresh=len(saved["plans"]), replay_same_turn=True,
                    original_thread_argument=live.commands[-1][live.commands[-1].index("--run-id") + 1])
                row["stage"] = "real_graph_resume"
                _finish(live)
                page.set_viewport_size({"width": 1366, "height": 900})
                page.reload(wait_until="networkidle")
                expect(progress).to_contain_text("10 / 10")
                expect(page.get_by_label("当前目标", exact=True)).to_have_value(live.goal["goal_id"])
                row["completed_desktop_layout"] = _assert_layout(page, mobile=False)
                page.screenshot(path=str(OUTPUT / "04-completed-desktop.png"), full_page=True)
                assert not errors, errors
                assert not failed_api, failed_api
                row.update(status="passed", stage="complete", page_errors=errors, failed_api=failed_api)
            except BaseException as error:
                row.update(error=repr(error), page_errors=errors, failed_api=failed_api,
                           reproduction="Open isolated seeded conversation, enter continue, send, then reload; see gui_request and failure screenshot")
                page.screenshot(path=str(OUTPUT / "gui-failure.png"), full_page=True)
                raise
            finally:
                context.close()
        finally:
            browser.close()


def test_live_unstarted_goal_and_saved_candidate_survive_refresh(live):
    baseline = dict(live.counters)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=True)
        try:
            page = browser.new_page(viewport={'width': 1366, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(live.base_url, wait_until='networkidle')
            page.locator('#prompt').fill('生成1条每日新闻，仅生成本地稿')
            page.get_by_role('button', name='发送', exact=True).click()
            expect(page.locator('.plan-pane .plan-list')).to_contain_text('1 条')
            cid = page.locator('#conversation-select').input_value()
            with page.expect_response(lambda response: response.request.method == 'POST'
                    and response.url.endswith('/messages')) as event:
                page.get_by_role('button', name='询问进度', exact=True).click()
            assert event.value.status == 200, event.value.text()
            assert 'expected_attempt_id' not in event.value.request.post_data_json
            page.locator('#prompt').fill('剩下改成科技新闻')
            page.get_by_role('button', name='发送', exact=True).click()
            panel = page.get_by_label('剩余任务修改')
            panel.get_by_role('button', name='编辑候选', exact=True).click()
            dialog = page.get_by_role('dialog', name='编辑剩余任务候选')
            dialog.get_by_label('每日新闻篇数', exact=True).fill('2')
            dialog.get_by_label('图片评分硬门槛', exact=True).uncheck()
            dialog.get_by_role('button', name='保存候选', exact=True).click()
            expect(dialog).not_to_be_visible()
            page.reload(wait_until='networkidle')
            expect(page.locator('.plan-pane .plan-list')).to_contain_text('2 条')
            panel.get_by_role('button', name='编辑候选', exact=True).click()
            expect(dialog.get_by_label('图片评分硬门槛', exact=True)).not_to_be_checked()
            saved = live.current.conversation_store.get(cid)
            assert saved['plans'][-1]['jobs'][0]['count'] == 2
            assert saved['plans'][-1]['image_score_required'] is False
            assert saved['runs'] == []
            assert live.counters == baseline
            assert not errors
            live.row.update(status='passed', stage='complete', followup_conversation_id=cid,
                            latest_candidate_id=saved['plans'][-1]['id'])
        finally:
            browser.close()


def _summary(samples):
    values = sorted(samples)
    return {"count": len(values), "p50_ms": values[math.ceil(len(values) * .50) - 1],
            "p95_ms": values[math.ceil(len(values) * .95) - 1], "max_ms": values[-1]}


def _legacy_baseline(live):
    checkout = live.current.root / "legacy-source"
    archive = subprocess.run(["git", "archive", "--format=zip", BASE_COMMIT], cwd=ROOT,
                             capture_output=True, check=True)
    with zipfile.ZipFile(io.BytesIO(archive.stdout)) as bundle:
        assert all((checkout / item.filename).resolve().is_relative_to(checkout.resolve()) for item in bundle.infolist())
        bundle.extractall(checkout)
    runtime = live.current.root / "legacy-runtime"
    credentials = runtime / "data/knowledge/postgresql-local/connection.json"
    credentials.parent.mkdir(parents=True)
    shutil.copyfile(os.environ["KNOWLEDGE_DB_CREDENTIALS"], credentials)
    shutil.copytree(live.current.root / "data/posts", runtime / "data/posts")
    checkpoint = Path("data/runs/agent") / live.original["id"] / "checkpoint.json"
    (runtime / checkpoint).parent.mkdir(parents=True)
    shutil.copyfile(live.current.root / checkpoint, runtime / checkpoint)
    env = {**os.environ, "PYTHONPATH": str(checkout) + os.pathsep + str(checkout / "tools/redbook_tools"),
           "REDBOOK_RUNTIME_ROOT": str(runtime), "KNOWLEDGE_DB_CREDENTIALS": str(credentials),
           "MODEL_PLATFORMS_DIR": str(runtime / "model-platforms"), "MODEL_PLATFORMS_NAMESPACE": "agent",
           "TEMP": str(runtime), "TMP": str(runtime), "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    seed = {"source": str(checkout), "isolated_root": str(live.current.root.resolve()), "database": live.row["database"],
            "namespace": "continuity_baseline_" + uuid4().hex,
            "conversation": live.current.conversation_store.get(live.cid),
            "job": deepcopy(live.current.jobs[live.original["id"]])}
    seed["conversation"]["runs"] = [live.original["id"]]
    result = subprocess.run(["E:/AI/codex/redbook_agent/.venv/Scripts/python.exe", "-B", str(Path(__file__).resolve()),
                             "--baseline-child"], cwd=checkout, env=env, input=json.dumps(seed),
                            capture_output=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    report.update(commit=BASE_COMMIT, archive_sha256=hashlib.sha256(archive.stdout).hexdigest(),
                  source_sha256={name: hashlib.sha256((checkout / name).read_bytes()).hexdigest() for name in
                      ("backend/app.py", "backend/progress.py", "tools/redbook_tools/apps/web_service.py",
                       "tools/redbook_tools/src/agent/conversation_store.py")},
                  statistics={"handler_with_run_projection": _summary([s["handler_ms"] for s in report["samples"]]),
                              "http_ack": _summary([s["ack_ms"] for s in report["samples"]])},
                  comparison_note="Legacy measures entire add_message including run projection; new routing includes goal/turn persistence. New status interleaves continue; old status does not. No speedup ratio claimed.")
    return report


def test_warm_local_status_and_active_continue_100_samples(live):
    row = live.row
    row["stage"] = "resume_ack"
    accepted = _assert_active(live, _post(live, _payload(live, "继续")))
    attempt = accepted["run"]["id"]
    baseline = live.counters.copy()
    delivery_baseline = list(live.deliveries)
    generated_baseline = deepcopy(live.generated)
    all_samples = {"status": [], "active_continue": []}
    try:
        row["stage"] = "legacy_baseline_subprocess"
        row["legacy_baseline"] = _legacy_baseline(live)
        for phase, count in [("prewarm", 10), ("measured", 100)]:
            row["stage"] = phase
            for index in range(count):
                for name, text, action in [("status", "现在到哪了？", "status"),
                                           ("active_continue", "继续", "continue_goal")]:
                    key = str(uuid4())
                    payload = _payload(live, text, attempt)
                    started = time.perf_counter_ns()
                    response = _post(live, payload, key)
                    ack_ms = (time.perf_counter_ns() - started) / 1e6
                    assert response.status_code == 200, response.text
                    data = response.json()
                    assert data["action"] == action and data["plan"] is data["run"] is None
                    assert data["goal"]["active_attempt_id"] == attempt
                    assert data["goal"]["jobs"][0]["requested"] == 10
                    assert data["goal"]["jobs"][0]["delivered_verified"] == 7
                    matching = [span for span in live.routing if span["key"] == key]
                    assert len(matching) == 1
                    if phase == "measured":
                        all_samples[name].append({"index": index, "key": key,
                            "client_message_id": payload["client_message_id"], "turn_id": data["turn"]["turn_id"],
                            "status": response.status_code, "routing_ms": matching[0]["ms"], "ack_ms": ack_ms})
        stats = {name: {"routing_with_persistence": _summary([s["routing_ms"] for s in samples]),
                        "http_ack": _summary([s["ack_ms"] for s in samples])}
                 for name, samples in all_samples.items()}
        delta = {key: live.counters[key] - baseline[key] for key in
                 ("llm_calls", "external_connections", "generated_posts", "worker_launches")}
        row.update(prewarm_per_operation=10, measured_per_operation=100, samples=all_samples,
                   statistics=stats, performance_side_effect_delta=delta,
                   threshold_ms={"routing_p95": 500, "ack_p95": 1000})
        assert set(delta.values()) == {0}, delta
        assert live.deliveries == delivery_baseline and live.generated == generated_baseline
        assert len(live.commands) == live.counters["worker_launches"] == 2
        saved = live.current.conversation_store.get(live.cid)
        assert len(saved["plans"]) == 1 and len(saved["runs"]) == 2
        assert len({s["turn_id"] for samples in all_samples.values() for s in samples}) == 200
        row["stage"] = "graph_resume_after_measurement"
        _finish(live)
        violations = [f"{name} {metric} p95={value['p95_ms']:.2f}ms >= {limit}ms"
                      for name, metrics in stats.items() for metric, value in metrics.items()
                      for limit in [500 if metric == "routing_with_persistence" else 1000]
                      if value["p95_ms"] >= limit]
        row["threshold_violations"] = violations
        assert not violations, "; ".join(violations)
        row.update(status="passed", stage="complete")
    except BaseException as error:
        row.update(error=repr(error), samples=all_samples)
        raise
