# Web Durable Recovery - 2026-10-02

## Scope and Verification Status

Production changes are limited to `apps/web_service.py`. The CLI, agent core,
PostgreSQL schema, UI, request timeouts, startup process detection, and worker
leases are unchanged. No model, platform, quota, database, application, build,
or test execution was performed for this change. New regression cases are
pending execution; they must not be reported as passing.

## Impact Evidence

The available GitNexus index is `auto_redbook` at
`E:/AI/codex/redbook_workflow`, indexed at commit
`3f6f1a8be871a4369f074d79aed9834851d0ae4a`. It does not index this standalone
`redbook_tools` directory, so its results are legacy structural evidence only.

- `Workbench.plan`: HIGH risk; direct caller `submit`, with
  `execute_agent_plan` and `resume_agent_run` upstream. HIGH was reported before
  editing; the separate LOW shared-axis value was not used to waive it.
- `resume_agent_run` and `Workbench._run`: UNKNOWN, not an unused-symbol result.
  Actual standalone source confirms the resume callers in
  `apps/web_gui.py` and `E:/AI/codex/redbook_agent/backend/app.py`.
  `submit` launches `threading.Thread(target=self._run, ...)`.
- `Workbench`: the legacy graph resolves an import from `apps/web_gui.py`.

These actual-source checks address the unresolved dynamic call sites, but do
not establish complete standalone graph coverage. No commit is made, and no
old-repository graph result is presented as a clean standalone change scan.

## Recovery Contract

Both Web recovery endpoints retain their existing authentication and request
guards. `resume_agent_run` still validates:

1. The conversation and attempt ID formats.
2. Attempt membership in the supplied conversation, and `kind == agent`.
3. That the selected attempt is not in an active Web state.
4. The resolved checkpoint pointer remains under `data/runs/agent`, including
   resolved-path checks that reject directory links escaping that root.
5. A corresponding plan with jobs belongs to that conversation.

Only after these checks does `_agent_checkpoint_state` read the canonical
PostgreSQL thread using the existing `postgres_checkpointer` adapter and
`PostgresSaver.get_tuple`:

```python
{"configurable": {"thread_id": agent_run_id, "checkpoint_ns": ""}}
```

The returned tuple must identify that same root thread and namespace, and its
state must contain the same `run_id`, a nonempty job list, and a status.
Missing, unavailable, or invalid PostgreSQL state rejects recovery before
submission. Error codes are `POSTGRES_CHECKPOINT_NOT_FOUND`,
`POSTGRES_CHECKPOINT_UNAVAILABLE`, and `POSTGRES_CHECKPOINT_INVALID`.
Connection error details are not copied into the client-facing error message.

This reader does not initialize schemas, update checkpoints, invoke a graph,
or fall back to JSON or an in-memory production backend. It uses the
conversation store's existing knowledge store when available.

The audit `checkpoint.json` may be absent, corrupt, or stale. Neither its
existence nor its claimed completion status authorizes or blocks recovery.
A durable PostgreSQL `completed` state still rejects unnecessary recovery.
The pointer is passed to the CLI with the original `--run-id`; a new Web
attempt must never create a replacement PostgreSQL thread. `resume_of` remains
the preceding Web attempt ID. Existing submit idempotency and idle checks stay
in place. The separate frozen plan JSON requirement remains unchanged.

## Budget Compatibility

New plans and recovery requests already use `budget_minutes=0.0`.
`Workbench.plan` now accepts legacy finite, nonnegative values, validates them,
and emits `--budget-minutes 0.0` regardless of the accepted value. Negative
numbers, booleans, NaN, infinity, null, and nonnumeric values remain invalid.
This retains old-client compatibility without reinstating a shared task
deadline. It does not change discovery budgets or individual operation
timeouts owned by the CLI/core.

## Agent Terminal States

Process cancellation and nonzero exit codes take precedence. For an agent
worker that exits zero, the Web adapter reads the original PostgreSQL thread
outside the Workbench lock and records its business status as `agent_status`:

| Durable business status | Web status |
| --- | --- |
| `completed` | `completed` |
| `partial` | `partial_success` |
| `blocked`, `failed`, active or unknown state | `failed` |
| PostgreSQL result cannot be confirmed | `failed`, with a checkpoint error |

Historical `has_warnings` and event logs remain intact, but no longer turn an
agent's durable completed result into partial success. A zero process exit
alone is insufficient to claim completion. Non-agent job classification is
unchanged. A failure to confirm completion does not trigger automatic
generation, upload, or recovery.

## Pending Regression Cases

`tests/test_web_durable_recovery.py` uses the existing in-memory conversation
fixture, a replaced PostgreSQL transport, and replaced worker subprocesses.
It exercises real Web adapter methods and local persistence; it does not
require a live database, HTTP server, model, or platform.

- Missing, corrupt, and stale-completed audit JSON with an unfinished PG thread.
- Repeated recovery and legacy attempt identities retaining the original thread.
- Missing/unavailable PG and mismatched thread identity rejecting valid audit JSON.
- Durable completion rejecting recovery even without audit JSON.
- Conversation, active-state, path, and plan guards preceding the PG read.
- Legacy positive budgets emitting an unlimited worker command; invalid values rejected.
- Completed/partial/blocked/active durable results after historical failure events.
- Zero exit without a confirmed PG result not claiming completion.
- Nonzero exit and cancellation overriding a durable completed state.

The parent updated the standalone-agent `test_run_resume_contract.py` fixture
to provide an explicit, isolated PostgreSQL-state reader double, separate from
the audit JSON file. Its legacy positive-budget assertion now expects `0.0`.
These cases were not executed. The production reader has no in-memory fallback;
actual PG integration remains pending separate authorization.

## Deferred Process-Ownership Design

Startup still marks persisted active Web jobs interrupted without verifying
the old child process. This patch does not establish that it is safe to resume
an orphaned worker. An operator must confirm the original worker has ended
before recovery; an interrupted Web label is not that evidence.

The separately integrated canonical-run PostgreSQL lease is documented in
`2026-10-02-agent-run-lease.md`. It blocks a cooperating second worker before
tool execution while the original lease session is alive. Startup labels alone
still do not establish process death, and a lost lease does not cancel external
work already in flight. Process-identity reconciliation remains deferred.
