"""Conservative cross-process model admission; expired remote work is not free."""
from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import CancelledError
from uuid import uuid4
from threading import Event, Thread
from .request_outcome import RequestNotDispatched, remote_outcome_unknown
from .model_observation import completed_request_count, failure_outcome_unknown


class ModelCapacityStore:
    def __init__(self, store):
        self.store = store

    def ensure_schema(self):
        with self.store.connection("migration") as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('model-capacity-schema-v1'))")
            conn.execute("CREATE SCHEMA IF NOT EXISTS agent")
            conn.execute("""CREATE TABLE IF NOT EXISTS agent.model_request_leases (
                group_key text NOT NULL, modality text NOT NULL, request_id text NOT NULL,
                status text NOT NULL, slot_limit integer NOT NULL CHECK(slot_limit>0),
                created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
                PRIMARY KEY(group_key,modality,request_id))""")
            conn.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON agent.model_request_leases TO redbook_app")
            conn.execute("ALTER TABLE agent.model_request_leases ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz NOT NULL DEFAULT now()")

    def acquire(self, group_key, request_id, *, modality, limit):
        if modality not in {"text", "image"} or not group_key or limit < 1:
            raise ValueError("invalid model capacity group")
        with self.store.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",("capacity:"+group_key+":"+modality,))
            # Orphan detection is not permission to free a remotely running slot.
            conn.execute("""UPDATE agent.model_request_leases SET status='unknown'
                WHERE group_key=%s AND modality=%s AND status='running'
                AND heartbeat_at < now()-interval '30 seconds'""", (group_key, modality))
            active=conn.execute("""SELECT count(*) AS total,min(slot_limit) AS strictest,
                count(*) FILTER (WHERE status='unknown') AS unknown
                FROM agent.model_request_leases WHERE group_key=%s AND modality=%s
                AND status IN ('running','unknown')""",(group_key,modality)).fetchone()
            effective=min(limit,active["strictest"] or limit)
            row = None
            if not active["unknown"] and active["total"] < effective:
                row=conn.execute("""INSERT INTO agent.model_request_leases(group_key,modality,request_id,status,slot_limit)
                    VALUES(%s,%s,%s,'running',%s) ON CONFLICT DO NOTHING RETURNING request_id""",
                    (group_key,modality,request_id,effective)).fetchone()
        # Raise only after committing the orphan's durable unknown state.
        if active["unknown"]:
            raise RequestNotDispatched("MODEL_CAPACITY_UNCERTAIN: remote requests need reconciliation before new admission")
        return row is not None

    def finish(self, group_key, request_id, *, modality, uncertain=False):
        with self.store.connection() as conn:
            conn.execute("""UPDATE agent.model_request_leases SET status=%s,finished_at=now()
                WHERE group_key=%s AND modality=%s AND request_id=%s AND status IN ('running','unknown')""",
                ("unknown" if uncertain else "finished",group_key,modality,request_id))

    @contextmanager
    def slot(self, group_key, *, modality, limit, stop_event):
        request_id=uuid4().hex
        while not self.acquire(group_key,request_id,modality=modality,limit=limit):
            if stop_event.wait(0.15):
                raise CancelledError("stopped while waiting for shared model capacity")
        started=False
        completed_before = completed_request_count()
        done = Event()
        def heartbeat():
            while not done.wait(5):
                try:
                    with self.store.connection() as conn:
                        conn.execute("""UPDATE agent.model_request_leases SET heartbeat_at=now()
                            WHERE group_key=%s AND modality=%s AND request_id=%s AND status='running'""",
                            (group_key, modality, request_id))
                except Exception:
                    # A lost DB connection must not turn an active remote call into
                    # an available slot. The next claimant will pause for review.
                    return
        keeper = Thread(target=heartbeat, name="model-capacity-heartbeat", daemon=True)
        keeper.start()
        try:
            if stop_event.is_set():
                raise CancelledError("stopped before model invocation")
            started=True
            yield request_id
        except BaseException as exc:
            # No deadline/TTL can prove that a remote request has stopped.
            self.finish(group_key,request_id,modality=modality,
                        uncertain=started and failure_outcome_unknown(exc, completed_before=completed_before))
            raise
        else:
            self.finish(group_key,request_id,modality=modality)
        finally:
            done.set()
            keeper.join(timeout=1)
