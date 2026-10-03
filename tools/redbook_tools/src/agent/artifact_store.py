"""Per-item progress committed independently of a long graph node."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import re
from typing import Any, Callable, Iterator

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from src.knowledge.store import KnowledgeStore
from src.storage.models import Post


class AgentArtifactStore:
    def __init__(self, store: KnowledgeStore | None = None):
        self.store = store or KnowledgeStore.from_env()

    @contextmanager
    def lease(self, run_id: str) -> Iterator[Callable[[], None]]:
        """Hold a dedicated session lock and yield its fail-closed alive probe."""
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
            raise ValueError("agent run lease requires a stable run identity")
        key = int.from_bytes(
            hashlib.sha256(f"redbook:agent-run:{run_id}".encode("ascii")).digest()[:8],
            "big", signed=True,
        )
        try:
            conninfo = make_conninfo(
                **self.store._credentials("app"), connect_timeout=5,
                options="-c statement_timeout=30000 -c lock_timeout=5000",
            )
            # Never return a session-level advisory lock to the shared pool.
            conn = psycopg.connect(conninfo, autocommit=True, prepare_threshold=0)
        except Exception:
            raise RuntimeError("AGENT_RUN_LEASE_UNAVAILABLE: cannot open run lock session") from None
        try:
            try:
                row = conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()
                if not row or not isinstance(row[0], bool):
                    raise RuntimeError("missing run lock evidence")
            except Exception:
                raise RuntimeError("AGENT_RUN_LEASE_UNAVAILABLE: cannot establish run lock") from None
            if row[0] is False:
                raise RuntimeError(f"AGENT_RUN_ALREADY_ACTIVE: {run_id}")

            def assert_alive() -> None:
                try:
                    if conn.closed or conn.execute("SELECT 1").fetchone() != (1,):
                        raise RuntimeError("run lock session is unavailable")
                except Exception:
                    raise RuntimeError("AGENT_RUN_LEASE_LOST: run lock session is unavailable") from None

            yield assert_alive
        finally:
            # Session close releases the lock even after rollback or interruption.
            conn.close()

    def ensure_schema(self) -> None:
        with self.store.connection("migration") as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('agent-task-artifacts'))")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent.task_artifacts (
                    run_id text NOT NULL, job_key text NOT NULL, post_id text NOT NULL,
                    phase text NOT NULL, payload jsonb NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (run_id, job_key, post_id)
                )
            """)
            conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON agent.task_artifacts TO redbook_app")

    def save(self, run_id: str, job_key: str, post: Post, *, phase: str) -> None:
        if not run_id or not job_key:
            raise ValueError("artifact progress requires a stable run and job identity")
        # Use the same redactor as graph checkpoints; model/provider credentials
        # must never be included in durable task snapshots.
        from src.agent.editorial_agent import _safe_value

        payload = _safe_value(post.model_dump(mode="json"))
        with self.store.connection() as conn:
            conn.execute("""
                INSERT INTO agent.task_artifacts (run_id, job_key, post_id, phase, payload)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (run_id, job_key, post_id) DO UPDATE
                SET phase=EXCLUDED.phase, payload=EXCLUDED.payload, updated_at=now()
            """, (run_id, job_key, post.id, phase, Jsonb(payload)))

    def load(self, run_id: str, job_key: str) -> list[Post]:
        with self.store.connection() as conn:
            rows = conn.execute("""
                SELECT payload FROM agent.task_artifacts
                WHERE run_id=%s AND job_key=%s ORDER BY created_at, post_id
            """, (run_id, job_key)).fetchall()
        return [Post.model_validate(row["payload"]) for row in rows]

    def summary(self, run_id: str) -> list[dict[str, Any]]:
        with self.store.connection() as conn:
            return list(conn.execute("""
                SELECT job_key, phase, count(*) AS count FROM agent.task_artifacts
                WHERE run_id=%s GROUP BY job_key, phase ORDER BY job_key, phase
            """, (run_id,)).fetchall())
