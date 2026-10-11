"""PostgreSQL stage artifacts and fenced work leases; no local DB fallback."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from psycopg.types.json import Jsonb

from src.knowledge.store import KnowledgeStore
from .news_contracts import CONTRACT_VERSION, content_hash


@dataclass(frozen=True)
class StageKey:
    namespace: str
    root_run_id: str
    job_key: str
    candidate_id: str
    stage: str
    input_hash: str
    contract_version: str = CONTRACT_VERSION

    def cache_args(self):
        return (self.namespace, self.stage, self.input_hash, self.contract_version)

    def work_args(self):
        return (self.namespace, self.root_run_id, self.job_key, self.candidate_id, self.stage, self.input_hash)


@dataclass(frozen=True)
class StageLease:
    key: StageKey
    owner: str
    epoch: int


_WORK_WHERE = "namespace=%s AND root_run_id=%s AND job_key=%s AND candidate_id=%s AND stage=%s AND input_hash=%s"
_CACHE_WHERE = "namespace=%s AND stage=%s AND input_hash=%s AND contract_version=%s"


class NewsStageStore:
    def __init__(self, store: KnowledgeStore | None = None):
        self.store = store or KnowledgeStore.from_env()

    def ensure_schema(self):
        with self.store.connection("migration") as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('news-stage-schema-v1'))")
            conn.execute("CREATE SCHEMA IF NOT EXISTS agent")
            conn.execute("""CREATE TABLE IF NOT EXISTS agent.news_stage_cache (
                namespace text NOT NULL, stage text NOT NULL, input_hash text NOT NULL,
                contract_version text NOT NULL, output_hash text NOT NULL, payload jsonb NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz,
                PRIMARY KEY(namespace,stage,input_hash,contract_version))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS agent.news_work_items (
                namespace text NOT NULL, root_run_id text NOT NULL, job_key text NOT NULL,
                candidate_id text NOT NULL, stage text NOT NULL, input_hash text NOT NULL,
                status text NOT NULL, artifact_ref text NOT NULL DEFAULT '', attempt_count integer NOT NULL DEFAULT 0,
                lease_owner text NOT NULL, lease_until timestamptz NOT NULL, lease_epoch integer NOT NULL,
                next_attempt_at timestamptz, last_error text NOT NULL DEFAULT '', version integer NOT NULL DEFAULT 1,
                external boolean NOT NULL DEFAULT false, updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY(namespace,root_run_id,job_key,candidate_id,stage,input_hash))""")
            conn.execute("CREATE INDEX IF NOT EXISTS news_work_pending ON agent.news_work_items(namespace,root_run_id,status)")
            conn.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON agent.news_stage_cache,agent.news_work_items TO redbook_app")

    def get_cached(self, key: StageKey) -> dict[str, Any] | None:
        with self.store.connection() as conn:
            row = conn.execute("SELECT payload FROM agent.news_stage_cache WHERE " + _CACHE_WHERE +
                               " AND (expires_at IS NULL OR expires_at>now())", key.cache_args()).fetchone()
            return row["payload"] if row else None

    def _put(self, conn, key, payload, ttl_seconds=None):
        expected = content_hash(payload)
        conn.execute("""INSERT INTO agent.news_stage_cache(namespace,stage,input_hash,contract_version,output_hash,payload,expires_at)
            VALUES(%s,%s,%s,%s,%s,%s,CASE WHEN %s::double precision IS NULL THEN NULL
                ELSE now()+make_interval(secs=>%s::double precision) END) ON CONFLICT DO NOTHING""",
            (*key.cache_args(), expected, Jsonb(payload), ttl_seconds, ttl_seconds))
        row = conn.execute("SELECT output_hash FROM agent.news_stage_cache WHERE " + _CACHE_WHERE, key.cache_args()).fetchone()
        if not row or row["output_hash"] != expected:
            raise ValueError("stage cache output conflict; input key must describe immutable work")
        return expected

    def put_cached(self, key: StageKey, payload: dict[str, Any], *, ttl_seconds=None):
        with self.store.connection() as conn, conn.transaction():
            return self._put(conn, key, payload, ttl_seconds)

    def claim_stage(self, key: StageKey, *, owner: str, lease_seconds: float, external: bool = False) -> StageLease | None:
        if not owner or not key.namespace or lease_seconds <= 0:
            raise ValueError("invalid stage lease identity/duration")
        with self.store.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("news-stage:"+content_hash(key.cache_args()),))
            if conn.execute("SELECT 1 FROM agent.news_stage_cache WHERE " + _CACHE_WHERE +
                            " AND (expires_at IS NULL OR expires_at>now())", key.cache_args()).fetchone():
                return None
            # A lost worker does not prove that an external request stopped.
            busy = conn.execute("""SELECT 1 FROM agent.news_work_items WHERE namespace=%s AND stage=%s AND input_hash=%s
                AND status IN ('running','unknown') AND (external OR lease_until>now()) LIMIT 1""",
                (key.namespace,key.stage,key.input_hash)).fetchone()
            if busy:
                return None
            # Fence expired local work across roots before another worker owns the cache key.
            conn.execute("""UPDATE agent.news_work_items SET status='superseded',version=version+1
                WHERE namespace=%s AND stage=%s AND input_hash=%s AND status IN ('running','unknown')
                AND NOT external AND lease_until<=now()""",(key.namespace,key.stage,key.input_hash))
            row = conn.execute("""INSERT INTO agent.news_work_items(namespace,root_run_id,job_key,candidate_id,stage,input_hash,
                status,lease_owner,lease_until,lease_epoch,attempt_count,external)
                VALUES(%s,%s,%s,%s,%s,%s,'running',%s,now()+make_interval(secs=>%s),1,1,%s)
                ON CONFLICT(namespace,root_run_id,job_key,candidate_id,stage,input_hash) DO UPDATE
                SET status='running',lease_owner=EXCLUDED.lease_owner,lease_until=EXCLUDED.lease_until,
                    lease_epoch=agent.news_work_items.lease_epoch+1,attempt_count=agent.news_work_items.attempt_count+1,
                    version=agent.news_work_items.version+1,external=EXCLUDED.external,updated_at=now()
                WHERE agent.news_work_items.status<>'completed'
                RETURNING lease_epoch""", (*key.work_args(),owner,lease_seconds,external)).fetchone()
            return StageLease(key, owner, row["lease_epoch"]) if row else None

    def commit_stage(self, lease: StageLease, *, payload: dict[str, Any], output_hash: str = "", artifact_ref: str = "") -> bool:
        if output_hash and output_hash != content_hash(payload):
            raise ValueError("stage output hash mismatch")
        with self.store.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("news-stage:"+content_hash(lease.key.cache_args()),))
            row = conn.execute("SELECT * FROM agent.news_work_items WHERE " + _WORK_WHERE + " FOR UPDATE",
                               lease.key.work_args()).fetchone()
            if not row or row["lease_owner"] != lease.owner or row["lease_epoch"] != lease.epoch or row["status"] not in {"running","unknown"}:
                return False
            digest = self._put(conn, lease.key, payload)
            conn.execute("UPDATE agent.news_work_items SET status='completed',artifact_ref=%s,version=version+1,updated_at=now() WHERE " + _WORK_WHERE,
                         (artifact_ref or digest,*lease.key.work_args()))
            return True

    def fail_stage(self, lease: StageLease, *, code: str, uncertain: bool = False):
        with self.store.connection() as conn:
            conn.execute("UPDATE agent.news_work_items SET status=%s,last_error=%s,version=version+1,updated_at=now() WHERE " +
                         _WORK_WHERE + " AND lease_owner=%s AND lease_epoch=%s AND status='running'",
                         ("unknown" if uncertain else "failed", code, *lease.key.work_args(), lease.owner, lease.epoch))

    def pending(self, root_run_id: str, stage: str, *, namespace: str):
        with self.store.connection() as conn:
            return list(conn.execute("""SELECT * FROM agent.news_work_items WHERE namespace=%s AND root_run_id=%s
                AND stage=%s AND status<>'completed' ORDER BY updated_at""",(namespace,root_run_id,stage)).fetchall())
