# Canonical Agent Run Session Lease

## Scope

`AgentArtifactStore.lease(run_id)` is a synchronous context manager. It opens a
dedicated PostgreSQL app session and uses `SELECT pg_try_advisory_lock(%s)`.
Its context value is a callable `assert_alive()`. The callable checks the same
session with `SELECT 1`; it never reconnects or reacquires the advisory lock.
It does not use a transaction lock, a pooled session, a file lock, or an in-memory
production fallback. No schema migration is required. Artifact save/load methods
are unchanged.

The key is the first eight bytes of SHA-256 of
`redbook:agent-run:<canonical-run-id>`, interpreted as a signed big-endian bigint.
It is stable across processes, unlike Python's built-in hash. SHA truncation can
theoretically cause a false busy result; it cannot permit concurrent owners of
the same run key.

## Entry Contract

For PostgreSQL resume, identity precedence is audit `original_thread`, original
audit `run_id`, then the original checkpoint path identity. A new caller/Web
attempt `run_id` does not override these. The graph thread, artifact context,
audit directory, and lease use that canonical identity. A mismatched durable
state `run_id` raises `AGENT_RUN_ID_MISMATCH` before revalidation or graph invoke.

The lease is acquired before directory writes, checkpointer setup, graph
construction, revalidation, and tool callbacks. It remains held through all
scheduling rounds, checkpointer cleanup, result construction, and exception
unwinding. Closing the dedicated session releases the lock; `KeyboardInterrupt`
also follows that cleanup path. No elapsed run budget or lease expiry is added.

- Busy: `RuntimeError("AGENT_RUN_ALREADY_ACTIVE: <canonical-run-id>")`.
- Connection, query, or missing lock evidence: `AGENT_RUN_LEASE_UNAVAILABLE`.
- Lost/closed session or failed alive probe: `AGENT_RUN_LEASE_LOST`.
- PostgreSQL errors are not included in those messages, to avoid leaking credentials.
- JSON/manual runs keep their existing path and do not open a lease session.

Connection establishment has a five-second timeout and lock SQL has a
30-second statement timeout, matching the existing checkpointer's connection
policy. These are operation timeouts, not a total run budget.

The entry probes before its first write, before and after retained-work
revalidation, before and after each `graph.invoke` scheduling round, and before
returning a result. A failed probe aborts without starting the next round or
reconnecting. JSON entry supplies a no-op probe without opening PostgreSQL.

## Analysis And Limits

GitNexus only indexes the older `auto_redbook` workflow directory, not this
independent tools directory. Entry impact returned LOW with one CLI caller;
`AgentArtifactStore` returned UNKNOWN. Actual tools-directory references were
checked. The effective risk is higher than the stale graph suggests: PostgreSQL
entry now requires a second dedicated session and lock permission. Lock failure
blocks the run rather than falling back. CLI wiring, map verification, repair
journals, and shared collection budgets are outside this change.

The guarantee applies to cooperating callers using this entry point in the
same PostgreSQL database while its lease session is alive. An externally killed
session, database restart, or server idle-session timeout can release a lock
while external work is still in flight. The boundary probe detects loss at the
next safe boundary, not continuously. It cannot prevent a connection loss
immediately after a successful probe or cancel an in-flight model/upload call.
One synchronous graph round can include several external calls; they are not
individually fenced. This change is not a fencing-token
protocol and does not claim exactly-once platform delivery. Configure the app
role without an idle-session timeout shorter than a healthy run. Independent
run IDs, direct CLI workflows, and other delivery writers still require their
own upload idempotency controls. Do not put the lease connection through a
transaction-pooling proxy.

## Verification Status

Tests were authored before implementation and were NOT executed, as requested.
No PostgreSQL connection, model call, platform call, worker stop, or dependency
installation was performed. Static inspection checked the entry wrapper and
the untouched graph body; no runtime pass result is claimed.

`tests/test_agent_run_lease.py` covers independent-session exclusion, stable key,
busy-before-work, fail-closed/redacted errors, exception/interrupt cleanup,
canonical resume, state mismatch, lifecycle coverage, same-session alive probes,
loss-before-next-round, and unchanged JSON entry. No probe reconnect is allowed.
Existing in-memory PostgreSQL test harnesses explicitly stub only the lease
with `nullcontext(lambda: None)` (a no-op probe), scoped to those fixtures;
the real-PG branch keeps the real lease. This is test isolation, not production
downgrade.

`tests/test_agent_run_lease_postgres.py` is an opt-in real PostgreSQL spawn-process
check (`REDBOOK_TEST_POSTGRES=1`). It uses unique run IDs and no artifact/schema,
model, or platform operations. It requires a separately authorized test DB and
was not run. Future test artifacts must remain under
`E:/AI/codex/redbook_runtime/data/runs/tests/20261002-agent-run-lease`, with Python
bytecode and pytest cache disabled. The retained map-review test from the
earlier, paused task is unrelated to lease verification.
