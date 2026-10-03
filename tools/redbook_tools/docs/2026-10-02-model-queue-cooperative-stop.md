# Model Queue Cooperative Stop

Date: 2026-10-02

## Scope And Authorization

Implement cooperative stop only in `src/workflow/model_queues.py`, the daily
news parallel coordinator and its candidate model-submit boundaries in
`src/workflow/create_post.py`. Do not change scene normalization, CLI, model
prompts, image modules, review cache, API keys, or production services. The
parent owns those areas, including repair journaling and discovery budgets.

The original full-regression request was revoked before pytest was started.
No tests, collection, builds, compilation, model calls, quota synchronization,
generation, or platform calls were run. This change was checked by reading
files and call sites only. New regression cases are authored, not executed.

## Impact Evidence And Risk

GitNexus impact was queried before production edits. The only registered
repository was `auto_redbook`, at `E:/AI/codex/redbook_workflow`, with an index
dated 2026-09-24. The actual target `E:/AI/codex/redbook_tools` is not registered
and is not a Git worktree. It must not be treated as covered by that index.

- Exact UID queries for `_prepare_daily_news_candidate`,
  `_run_parallel_daily_news_candidates`, and `ModelWorkQueues.close` were not
  resolved: UNKNOWN, not evidence of no callers.
- Queries for `submit_llm` and `submit_image` reported CRITICAL. The refreshed
  `submit_llm` walk at depth 3 reported 139 upstream symbols, 79 processes,
  and 20 modules. Direct impact was the `create_post.py` file; higher levels
  included CLI generation/review/upload, web service, and GUI flows.
- A file-level coordinator proxy also reported CRITICAL, with 94 direct
  items and 53 processes. This is not an exact coordinator caller list.
- Representative indexed flows included `post_quality_callback`, `review`,
  `generate`, `generate_more`, `publish_drafts`, and the daily-wow coordinator
  flow. Truncated graph details are not a clean or complete impact check.
- Text fallback in the actual target's `src` and `apps` confirmed that the
  only production queue owner is this daily-news coordinator. Its four model
  submit sites are draft, rewrite, image, and quality callback. The coordinator
  is reached from `create_daily_news_posts`, including daily-wow selection.

Residual impact risk remains unresolved outside the read call sites. No
reindexing, installation, commit, or graph-clean claim was made.

## Stop Protocol

One `Event` belongs to each `ModelWorkQueues` instance. `request_stop()` sets
it under an admission lock. Both submit lanes use the same protocol:

1. Reject submission after stop with `ModelWorkStopped`, a `CancelledError`.
2. Recheck the Event inside the executor wrapper immediately before admitting
   the callable. A previously queued wrapper cannot start new model work after
   stop has won admission.
3. Release the lock before executing the callable. Already admitted work runs
   to completion; there is no forced thread termination or socket interruption.
4. On stopped close, cancel pending futures in BOTH lanes before waiting on
   either running lane. With no stop, close still drains pending work.
5. An exceptional queue-context exit requests stop before close.

Admission is the linearization boundary, not the physical first network byte.
A callable admitted just before stop is running work even if the transport
has not yet sent its request. Holding the lock over network IO would prevent
prompt stopping and is intentionally avoided.

The coordinator signals stop when the accepted count AND existing editorial
quotas are met, a completed result confirms provider exhaustion, or coordinator
control raises an exception. It clears unsubmitted candidates and cancels
candidate futures that have not started. The target signal is sent after the
last accepted post is durable, before its potentially slow recording callback.

Balanced mode buffers completion-order results but yields ready candidates in
submission order. It no longer waits for the entire batch before accepting the
first ready result. Quota failures can signal stop even while an earlier
candidate is still running. Speed mode retains its bounded completion window.
Both stop accepting at the existing single-batch target; surplus results are
not appended to the returned success list.

## Cancellation And Failure Accounting

Candidate submit boundaries catch `CancelledError` separately. They return a
`cancelled` result with whatever draft, post, and image references have already
been obtained. A failure settled after the stop signal is treated as stopped
work, not as another retryable request failure. Failed results first observed
before stop retain their ordinary failure meaning.

Cancelled results do not enter candidate retries, failed counts, quality-skip
counts, editorial quota skips, or provider-error diagnostics. Retries are
disabled once the queues are stopped. On stopped partial completion, missing
target slots are not fabricated into `failed_count`. Without stop, the prior
partial-error count fallback, RuntimeError/PartialDailyNewsError behavior,
selection order, and worker limits remain.

The coordinator requires exhaustion/balance evidence rather than a bare
`Token Plan` name or transient HTTP 429. Rewrite fallback errors and quality
callback errors are carried to the coordinator without changing ordinary
non-capacity skip behavior. The classifier remains message-based; unknown
provider wording can be missed. Lower-level provider retry/fallback policies
are not changed here.

## Completed Artifact Retention

The candidate executor uses its existing wait-on-shutdown behavior. After
shutdown its futures are terminal; the coordinator inspects them once, with
no polling loop, retry join, detached worker, or new model invocation.

For stopped runs, nonaccepted results with a post or completed draft are saved
locally along with a revision. A draft-only snapshot is `canceled`. Existing
worker-saved quality-failure posts and accepted posts are not rewritten by this
retention pass. Other retained snapshots get
`platform.batch_selection.status = retained_after_stop`, the first stop
reason, candidate status/reason, and requested count.

Prefer the worker's current `post.assets` over an earlier `result.asset_paths`:
a quality callback may have replaced AND saved the image. Preserve its asset
validation/identity metadata and image-lineage/review fields. Copy supplied
assets only when those original paths are still the post's final paths. The
same rule is used when accepting a selected result, avoiding replacement of
callback-produced final images with stale pre-callback paths.

Retained extras do not change `posts`, acceptance counters, callback counts,
discovery selected IDs, or the returned target count. The existing
`post_saved_callback` stays an acceptance-only hook. Therefore this patch
provides LOCAL durability for extras, NOT a claim of PostgreSQL/PGretain
completion. A worker that already called `save_post` keeps that identity and
completed image; adding PG retention for nonselected snapshots requires a
separate parent-owned retention interface, not rewriting the success list.

Retention attempts continue after an individual save failure. With an existing
coordinator exception, report the retention problem without replacing the
original exception/cause. Otherwise raise a partial-completion error containing
only accepted posts. Disk failures can still prevent durable storage; they are
not reported as successful retention.

## Static Review Boundaries

- An admitted model function or quality callback may contain its own retries,
  fallback providers, or redraw calls. The queue Event blocks the next queued
  stage, not internal calls inside an already running function. Passing stop
  through provider/image/CLI internals is outside this change's authorization.
- Existing provider timeouts determine shutdown latency. A hung admitted
  callable can still delay return, as with the prior executor context. No new
  deadline, infinite join loop, or promise of immediate return is introduced.
- Balanced acceptance can still wait for an earlier candidate before accepting
  a later successful one; preserving that ordering is intentional.
- Unexpected worker exceptions before a result is returned cannot recover
  objects held only in that worker's local variables. Already saved files remain
  on disk; this patch does not infer or regenerate lost in-memory artifacts.
- Artifact retention runs during cooperative shutdown, not after a hard process
  kill. Crash-before-shutdown recovery and PG repair journals remain parent work.
- Existing callback tests previously equated total local saves/revisions with
  the accepted count (`test_callback_failure_retains_the_just_saved_post` and
  `test_default_callback_remains_optional`). The parent updated these pending
  assertions to distinguish accepted results from retained extras, preserving
  callback ordering and selected-list guarantees. This is a contract update
  caused by THIS change, not an observed test failure. Neither old nor new
  callback cases were run.

## Authored Verification Cases

`tests/test_model_queue_cooperative_stop.py` covers both model lanes, stopped
close, normal draining, target stop before another candidate's next lane,
completed surplus retention without acceptance callbacks, worker-saved final
image identity during acceptance/retention, capacity stop during an admitted
request, late worker exceptions excluded from retries/counts, ordinary
non-capacity failures, and coordinator callback failure retaining accepted and
completed nonselected posts.

The cases mock model work, deny network connections, and use pytest temporary
paths for future authorized execution. They were not run or collected. Any
future invocation must explicitly place pytest temporary/cache/log output in an
exclusive E-drive run directory; pytest's default temp location is not permitted.

The earlier exclusive run directory
`E:/AI/codex/redbook_runtime/data/runs/tests/20261002-tools-regression` still
contains only its unexecuted `isolated_pytest.py`; there is no pytest exit code,
result file, or failure log to report, and no owned test process to terminate.
