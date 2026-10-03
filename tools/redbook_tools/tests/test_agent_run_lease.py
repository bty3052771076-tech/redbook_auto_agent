"""Session lease and entry-point contracts, without a database or tool service."""

from contextlib import contextmanager
import hashlib
import json
from types import SimpleNamespace

import pytest

from src.agent import artifact_store, editorial_agent as agent
from src.agent.artifact_store import AgentArtifactStore


@pytest.fixture
def database(monkeypatch):
    owners = {}
    sessions = []
    options = []
    credentials = dict(host="127.0.0.1", port=5432, dbname="lease_test", user="test", password="dummy")
    store = SimpleNamespace(_credentials=lambda role: credentials)
    monkeypatch.setattr(artifact_store.KnowledgeStore, "from_env", classmethod(lambda cls: store))

    class Session:
        closed = False

        def __init__(self):
            self.queries = []

        def execute(self, sql, params=None):
            self.queries.append((sql, params))
            if self.closed:
                raise OSError("session lost")
            if sql == "SELECT 1":
                assert params is None
                return SimpleNamespace(fetchone=lambda: (1,))
            assert sql == "SELECT pg_try_advisory_lock(%s)"
            key, = params
            self.key = key
            acquired = key not in owners
            if acquired:
                owners[key] = self
            return SimpleNamespace(fetchone=lambda: (acquired,))

        def close(self):
            self.closed = True
            for key, owner in list(owners.items()):
                if owner is self:
                    del owners[key]

    def connect(conninfo, **kwargs):
        options.append((conninfo, kwargs))
        session = Session()
        sessions.append(session)
        return session

    monkeypatch.setattr(artifact_store.psycopg, "connect", connect)
    return SimpleNamespace(store=store, sessions=sessions, owners=owners, options=options)


def test_same_run_is_busy_across_stores_and_different_run_can_enter(database):
    first = AgentArtifactStore(database.store)
    second = AgentArtifactStore(database.store)
    with first.lease("original-run"):
        with pytest.raises(RuntimeError, match="AGENT_RUN_ALREADY_ACTIVE"):
            with second.lease("original-run"):
                pytest.fail("busy lease entered its body")
        assert database.sessions[1].closed
        assert not database.sessions[0].closed
        with second.lease("different-run"):
            assert len(database.owners) == 2
    assert not database.owners
    with second.lease("original-run"):
        assert len(database.owners) == 1
    assert all(session.closed for session in database.sessions)


def test_lock_key_is_stable_namespaced_signed_bigint_and_session_is_not_pooled(database):
    with AgentArtifactStore(database.store).lease("canonical-42"):
        expected = int.from_bytes(
            hashlib.sha256(b"redbook:agent-run:canonical-42").digest()[:8], "big", signed=True,
        )
        assert database.sessions[0].key == expected
        conninfo, options = database.options[0]
        assert options == {"autocommit": True, "prepare_threshold": 0}
        assert "connect_timeout=5" in conninfo
        assert "statement_timeout=30000" in conninfo


@pytest.mark.parametrize("failure", [ValueError("tool failure"), KeyboardInterrupt()])
def test_exception_and_interrupt_release_session(database, failure):
    with pytest.raises(type(failure)) as caught:
        with AgentArtifactStore(database.store).lease("interrupted"):
            raise failure
    assert caught.value is failure
    assert database.sessions[0].closed
    assert not database.owners


def test_alive_probe_uses_same_session_without_reacquiring_lock_and_rejects_use_after_exit(database):
    with AgentArtifactStore(database.store).lease("probe") as assert_alive:
        assert_alive()
        assert_alive()
        assert database.sessions[0].queries == [
            ("SELECT pg_try_advisory_lock(%s)", (database.sessions[0].key,)),
            ("SELECT 1", None),
            ("SELECT 1", None),
        ]
    with pytest.raises(RuntimeError, match="AGENT_RUN_LEASE_LOST"):
        assert_alive()
    assert len(database.sessions) == 1
    assert not database.owners


@pytest.mark.parametrize("failure", ["closed", "query", "missing_evidence"])
def test_alive_probe_loss_fails_closed_redacts_and_closes_session(database, failure):
    with pytest.raises(RuntimeError, match="AGENT_RUN_LEASE_LOST") as caught:
        with AgentArtifactStore(database.store).lease("lost") as assert_alive:
            session = database.sessions[0]
            if failure == "closed":
                session.close()
            elif failure == "query":
                def fail(*args, **kwargs):
                    raise OSError("password=dummy-secret")

                session.execute = fail
            else:
                session.execute = lambda *args, **kwargs: SimpleNamespace(fetchone=lambda: None)
            assert_alive()
    assert "dummy-secret" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert database.sessions[0].closed
    assert len(database.sessions) == 1
    assert not database.owners


@pytest.mark.parametrize("response", [None, (), (None,), (1,)])
def test_missing_or_invalid_lock_evidence_fails_closed(database, monkeypatch, response):
    session = SimpleNamespace(closed=False)
    session.execute = lambda sql, params: SimpleNamespace(fetchone=lambda: response)
    session.close = lambda: setattr(session, "closed", True)
    monkeypatch.setattr(artifact_store.psycopg, "connect", lambda *args, **kwargs: session)
    with pytest.raises(RuntimeError, match="AGENT_RUN_LEASE_UNAVAILABLE"):
        with AgentArtifactStore(database.store).lease("no-evidence"):
            pytest.fail("lease entered without positive lock evidence")
    assert session.closed


@pytest.mark.parametrize("stage", ["connect", "query"])
def test_database_failure_is_redacted_and_never_falls_back(database, monkeypatch, stage):
    def fail(*args, **kwargs):
        raise RuntimeError("password=dummy-secret host=private")

    if stage == "connect":
        monkeypatch.setattr(artifact_store.psycopg, "connect", fail)
    else:
        session = SimpleNamespace(execute=fail, closed=False)
        session.close = lambda: setattr(session, "closed", True)
        monkeypatch.setattr(artifact_store.psycopg, "connect", lambda *args, **kwargs: session)
    with pytest.raises(RuntimeError, match="AGENT_RUN_LEASE_UNAVAILABLE") as caught:
        with AgentArtifactStore(database.store).lease("unavailable"):
            pytest.fail("unavailable lease entered its body")
    assert "dummy-secret" not in str(caught.value)
    assert caught.value.__suppress_context__
    if stage == "query":
        assert session.closed


@pytest.mark.parametrize("run_id", ["", "../other", "x" * 81, None])
def test_invalid_identity_never_connects(database, run_id):
    with pytest.raises(ValueError):
        with AgentArtifactStore(database.store).lease(run_id):
            pytest.fail("invalid identity entered its body")
    assert not database.sessions


def forbidden(*args, **kwargs):
    pytest.fail("busy/unavailable run started orchestration or a tool")


def unused_tools():
    return agent.EditorialAgentTools(forbidden, forbidden, forbidden, forbidden, plan=forbidden)


def test_busy_entry_has_no_directory_checkpoint_graph_or_tool_side_effect(database, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "_build_graph", forbidden)
    monkeypatch.setattr("src.agent.postgres_checkpoint.postgres_checkpointer", forbidden)
    with AgentArtifactStore(database.store).lease("canonical"):
        with pytest.raises(RuntimeError, match="AGENT_RUN_ALREADY_ACTIVE"):
            agent.run_editorial_agent(
                [agent.AgentJob("daily_news", "News")], tools=unused_tools(), run_id="canonical",
                config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, checkpoint_backend="postgres"),
                progress=forbidden,
            )
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("checkpoint", [
    {"original_thread": "canonical", "run_id": "web-attempt"},
    {"run_id": "canonical"},
    None,
])
def test_resume_locks_original_identity_not_new_attempt(database, tmp_path, monkeypatch, checkpoint):
    path = tmp_path / "canonical" / "checkpoint.json"
    if checkpoint is not None:
        path.parent.mkdir()
        path.write_text(json.dumps(checkpoint), encoding="utf-8")
    monkeypatch.setattr(agent, "_build_graph", forbidden)
    with AgentArtifactStore(database.store).lease("canonical"):
        with pytest.raises(RuntimeError, match="AGENT_RUN_ALREADY_ACTIVE"):
            agent.run_editorial_agent(
                [], tools=unused_tools(), run_id="new-web-attempt",
                config=agent.EditorialAgentConfig(
                    checkpoint_dir=tmp_path, checkpoint_backend="postgres", resume_from=path,
                ),
            )
    assert not (tmp_path / "web-attempt").exists()
    assert not (tmp_path / "new-web-attempt").exists()
    if checkpoint is not None:
        assert json.loads(path.read_text(encoding="utf-8")) == checkpoint


def test_connection_failure_at_entry_starts_no_work(database, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(artifact_store.psycopg, "connect", fail)
    monkeypatch.setattr(agent, "_build_graph", forbidden)
    with pytest.raises(RuntimeError, match="AGENT_RUN_LEASE_UNAVAILABLE"):
        agent.run_editorial_agent(
            [agent.AgentJob("daily_news", "News")], tools=unused_tools(), run_id="offline",
            config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, checkpoint_backend="postgres"),
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", [None, RuntimeError("graph failure"), KeyboardInterrupt()])
def test_lease_spans_graph_checkpointer_result_and_failure(database, tmp_path, monkeypatch, failure):
    def assert_held():
        with pytest.raises(RuntimeError, match="AGENT_RUN_ALREADY_ACTIVE"):
            with AgentArtifactStore(database.store).lease("full-run"):
                pytest.fail("run lock was released early")

    @contextmanager
    def checkpointer():
        assert_held()
        try:
            yield object()
        finally:
            assert_held()

    def builder(**kwargs):
        assert_held()

        def invoke(state, config):
            assert_held()
            assert config["configurable"]["thread_id"] == "full-run"
            if failure is not None:
                raise failure
            return dict(state, status="completed", job_index=1, completed_job_indices=[0])

        return SimpleNamespace(invoke=invoke, checkpointer=object(), get_state=lambda cfg: SimpleNamespace(next=()))

    monkeypatch.setattr("src.agent.postgres_checkpoint.postgres_checkpointer", checkpointer)
    monkeypatch.setattr(agent, "_build_graph", builder)
    result_type = agent.AgentRunResult

    def result_under_lease(**kwargs):
        assert_held()
        return result_type(**kwargs)

    monkeypatch.setattr(agent, "AgentRunResult", result_under_lease)
    kwargs = dict(
        tools=unused_tools(), run_id="full-run",
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, checkpoint_backend="postgres"),
    )
    if failure is None:
        result = agent.run_editorial_agent([agent.AgentJob("daily_news", "News")], **kwargs)
        assert result.status == "completed"
        assert result.run_id == "full-run"
    else:
        with pytest.raises(type(failure)):
            agent.run_editorial_agent([agent.AgentJob("daily_news", "News")], **kwargs)
    assert not database.owners
    assert all(session.closed for session in database.sessions)


@pytest.mark.parametrize("boundary", ["invoke", "snapshot"])
def test_lease_loss_stops_scheduling_before_next_graph_invocation(database, tmp_path, monkeypatch, boundary):
    calls = []

    @contextmanager
    def checkpointer():
        yield object()

    def invoke(state, config):
        calls.append("invoke")
        if boundary == "invoke":
            database.sessions[0].close()
        return dict(state, last_node="next_job")

    def snapshot(config):
        calls.append("snapshot")
        database.sessions[0].close()
        return SimpleNamespace(next=("sync",))

    graph = SimpleNamespace(checkpointer=object(), invoke=invoke, get_state=snapshot)
    monkeypatch.setattr(agent, "_build_graph", lambda **kwargs: graph)
    monkeypatch.setattr("src.agent.postgres_checkpoint.postgres_checkpointer", checkpointer)
    with pytest.raises(RuntimeError, match="AGENT_RUN_LEASE_LOST"):
        agent.run_editorial_agent(
            [agent.AgentJob("daily_news", "News")], tools=unused_tools(), run_id="rounds",
            config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, checkpoint_backend="postgres"),
        )
    assert calls == (["invoke"] if boundary == "invoke" else ["invoke", "snapshot"])
    assert all(session.closed for session in database.sessions)
    assert len(database.sessions) == 1
    assert not database.owners


def test_completed_result_cannot_hide_lease_loss(database, tmp_path, monkeypatch):
    @contextmanager
    def checkpointer():
        yield object()

    def invoke(state, config):
        database.sessions[0].close()
        return dict(state, status="completed", completed_job_indices=[0])

    graph = SimpleNamespace(invoke=invoke, checkpointer=object())
    monkeypatch.setattr(agent, "_build_graph", lambda **kwargs: graph)
    monkeypatch.setattr("src.agent.postgres_checkpoint.postgres_checkpointer", checkpointer)
    monkeypatch.setattr(agent, "AgentRunResult", forbidden)
    with pytest.raises(RuntimeError, match="AGENT_RUN_LEASE_LOST"):
        agent.run_editorial_agent(
            [agent.AgentJob("daily_news", "News")], tools=unused_tools(), run_id="completed-loss",
            config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, checkpoint_backend="postgres"),
        )
    assert not database.owners


@pytest.mark.parametrize("pending", [False, True])
def test_resume_revalidation_and_graph_share_the_canonical_lease(database, tmp_path, monkeypatch, pending):
    pointer = tmp_path / "web-attempt" / "checkpoint.json"
    pointer.parent.mkdir()
    pointer.write_text(json.dumps({"original_thread": "original", "run_id": "web-attempt"}), encoding="utf-8")
    state = dict(run_id="original", jobs=[], completed_job_indices=[], status="completed")

    def assert_held():
        with pytest.raises(RuntimeError, match="AGENT_RUN_ALREADY_ACTIVE"):
            with AgentArtifactStore(database.store).lease("original"):
                pytest.fail("resume released the canonical lease")

    def get_state(config):
        assert config["configurable"]["thread_id"] == "original"
        assert_held()
        return SimpleNamespace(values=state, next=("review",) if pending else ())

    def revalidate(value, tools, progress):
        assert_held()
        return dict(value)

    def update(config, value):
        assert_held()
        assert config["configurable"]["thread_id"] == "original"
        state.update(value)

    def invoke(value, config):
        assert_held()
        assert value is None
        return dict(state, status="completed")

    @contextmanager
    def checkpointer():
        yield object()

    graph = SimpleNamespace(checkpointer=object(), get_state=get_state, update_state=update, invoke=invoke)
    monkeypatch.setattr(agent, "_build_graph", lambda **kwargs: graph)
    monkeypatch.setattr(agent, "_revalidate_completed_jobs", revalidate)
    monkeypatch.setattr("src.agent.postgres_checkpoint.postgres_checkpointer", checkpointer)
    result = agent.run_editorial_agent(
        [], tools=unused_tools(), run_id="new-attempt",
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, checkpoint_backend="postgres", resume_from=pointer),
    )
    assert result.run_id == "original"
    assert result.checkpoint_path == tmp_path / "original" / "checkpoint.json"
    assert not database.owners


@pytest.mark.parametrize("pending", [False, True])
def test_loss_during_resume_revalidation_does_not_start_pending_graph(database, tmp_path, monkeypatch, pending):
    @contextmanager
    def checkpointer():
        yield object()

    state = dict(run_id="original", jobs=[], completed_job_indices=[], status="completed")
    graph = SimpleNamespace(
        get_state=lambda cfg: SimpleNamespace(values=state, next=("review",) if pending else ()),
        invoke=forbidden, update_state=forbidden,
    )

    def revalidate(value, tools, progress):
        database.sessions[0].close()
        return dict(value)

    monkeypatch.setattr(agent, "_build_graph", lambda **kwargs: graph)
    monkeypatch.setattr(agent, "_revalidate_completed_jobs", revalidate)
    monkeypatch.setattr("src.agent.postgres_checkpoint.postgres_checkpointer", checkpointer)
    with pytest.raises(RuntimeError, match="AGENT_RUN_LEASE_LOST"):
        agent.run_editorial_agent(
            [], tools=unused_tools(),
            config=agent.EditorialAgentConfig(
                checkpoint_dir=tmp_path, checkpoint_backend="postgres",
                resume_from=tmp_path / "original" / "checkpoint.json",
            ),
        )
    assert not database.owners


def test_resume_rejects_durable_identity_mismatch_before_revalidation(database, tmp_path, monkeypatch):
    @contextmanager
    def checkpointer():
        yield object()

    graph = SimpleNamespace(get_state=lambda cfg: SimpleNamespace(values={"run_id": "other"}, next=("review",)))
    monkeypatch.setattr(agent, "_build_graph", lambda **kwargs: graph)
    monkeypatch.setattr(agent, "_revalidate_completed_jobs", forbidden)
    monkeypatch.setattr("src.agent.postgres_checkpoint.postgres_checkpointer", checkpointer)
    with pytest.raises(RuntimeError, match="AGENT_RUN_ID_MISMATCH"):
        agent.run_editorial_agent(
            [], tools=unused_tools(),
            config=agent.EditorialAgentConfig(
                checkpoint_dir=tmp_path, checkpoint_backend="postgres",
                resume_from=tmp_path / "original" / "checkpoint.json",
            ),
        )
    assert not database.owners


def test_json_entry_keeps_existing_non_pg_path(database, tmp_path, monkeypatch):
    monkeypatch.setattr(artifact_store.psycopg, "connect", forbidden)
    graph = SimpleNamespace(
        checkpointer=None,
        invoke=lambda state, cfg: dict(state, status="completed", job_index=1, completed_job_indices=[0]),
    )
    monkeypatch.setattr(agent, "_build_graph", lambda **kwargs: graph)
    result = agent.run_editorial_agent(
        [agent.AgentJob("daily_news", "News")], tools=unused_tools(), run_id="manual-json",
        config=agent.EditorialAgentConfig(checkpoint_dir=tmp_path, checkpoint_backend="json"),
    )
    assert result.run_id == "manual-json"
    assert result.status == "completed"
    assert not database.sessions
