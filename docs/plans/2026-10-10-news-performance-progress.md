# SDD ledger - plan: docs/plans/2026-10-10-news-generation-review-performance-implementation-test-plan.md

## Baseline

- Agent baseline pushed: `bd113e2c34e82fbb81db5b74ff3fb1537fab6061`.
- Previous agent main backed up: `backup/main-before-performance-20261010-144913`, `b7ba98c`.
- Workflow main already current: `9ad02c5`; backup `backup/main-before-performance-20261010-144918`.
- Credential scan: 141 staged files, no local credential matches. Initial pattern matched `task-...` inside a directory name, corrected token boundary and rescanned.
- Baseline tests: 66 passed using E-drive basetemp; frontend build passed. Initial run used default C-drive pytest temp and 12 runtime-path checks failed; rerun on E passed without code changes.
- Graph all: 1376 symbols / 351 flows, CRITICAL. Listing capped at 1000; exhaustive staged groups rechecked: tools 345, UI/docs 621, tests 410, none partial/truncated. Existing Markdown hard line breaks retained.

## Decisions and Interface Checks

- Ruling: implement in existing workspaces on main, no new worktree, per explicit user instruction. Baseline remote backups precede all optimization edits.
- Ruling: use this durable Windows-compatible ledger instead of bash scratch helpers; keep records with project documents. No new dependencies, no C-drive installation.
- Ruling: baseline upload is authorized; implementation commits/pushes and production model/platform runs are not required for this task. Test without new public posts or drafts.
- Tasks 1/2/3/4: versioned stage context and hashes feed evidence, repair, cache; no credentials in persisted contracts.
- Tasks 4/5/6: short PostgreSQL transactions and fencing required; stage locks and remote request lifetime are distinct.
- Tasks 5/6/7: legacy environment mutation prevents safe column parallelism; keep disabled until explicit runtime isolation is verified.
- Tasks 6/7/8: READY is not a platform receipt; per-item failure must preserve independent successful items and report deficits.

## Task Status

1. Partial: stage/request telemetry, fixed-input offline candidate benchmark and historical manifest implemented. Full cold/warm end-to-end/live benchmark is not implemented or run. SDK-internal retries remain opaque and are labelled accordingly.
2. Partial: immutable snapshots, exact evidence spans, Decimal unit alignment and entity/role/planned/negative counterexamples implemented. Historical 54-rejection gold set is NOT manually labelled; cross-language entity resolution and targeted evidence adjudication remain pending. Legacy hard gates remain in effect.
3. Implemented: one text patch and one scene patch, base-hash/allowed-field checks, readonly accepted body during comment/scene review; focused regressions pass.
4. Partial: real PostgreSQL cache, single-flight claims, epoch fencing, namespace isolation, retained image hash verification, retryable terminal failures and unknown remote outcome preservation implemented. Source 304 cache, negative cache policy and self-service reconciliation remain pending.
5. Partial: frozen pipeline version and conservative shared PostgreSQL capacity implemented; two-process 5-text/2-image tests pass. Explicit per-dispatch runtime, account alias grouping, fair 3:1 scheduling and cross-column parallelism remain pending.
6. Partial: current per-post retention and serial delivery preserved, stage checkpoints added. Durable READY consumer, cross-process platform lock and streaming delivery remain pending. No platform writes in this implementation run.
7. Partial: AI per-item cache, ID-bound local repair, two bounded concurrent batches and separate text/image/repair/rejection progress implemented. Shared source-fetch cache and whole-pipeline timing UI remain pending.
8. Partial: agent/tools full suites, UI build/screenshots, real PostgreSQL tests and old-workflow compatibility suite run. W port and live delivery/performance acceptance remain pending.

## Review and Rulings

- One fresh-context review identified important cache identity, orphan capacity, terminal error, configuration grouping, readonly patch and telemetry issues. Fixes were reproduced with failing tests before repair. No second reviewer was dispatched.
- Final: fixed root-scoped post identities and account/permission cache separation; namespaces are conservatively root-scoped until authenticated platform identity is available.
- Final: fixed orphan request heartbeats; expired remote work becomes unknown and pauses admission, never silently frees a slot. The state transition commits before the pause exception.
- Final: fixed completed model response followed by local JSON RuntimeError being classified as a still-running remote request. Queue invocation now carries the observation context through admission; subsequent remote timeout still remains unknown. RED 1 failed / 1 passed, GREEN 45 passed including the real-PG and cooperative-stop groups.
- Final: fixed mixed-provider bypass with conservative shared-unclassified grouping; this is NOT full account-aware isolation or fair scheduling.
- Final: fixed scoped comment review altering approved text and stage spans inflating model request counts. Adapter invocations and local stages are now distinct metrics; SDK retries are explicitly unobserved.
- Ruling: retain legacy as the default and keep cross-column execution disabled. Existing environment-mutating adapters have not passed explicit-runtime isolation tests; enabling parallel columns now risks model/account configuration leakage. Cost: no cross-column overlap benefit yet.
- Ruling: do not copy the embedded tool tree over W. Its pre-existing CLI, agent and workflow APIs differ substantially; the old workflow passed its own unchanged suite. Cost: W does not yet receive the new optimizations.
- Ruling: the historical run is mixed failure/resume evidence, not a speed baseline. Offline fixtures test calls and patches but cannot establish provider or platform time savings. Cost: no claimed end-to-end speed percentage yet.
- Ruling: stage A and stage B acceptance gates are NOT complete. The opt-in implementation is a review checkpoint, not a production-default release; detailed pending work is retained in the implementation report.

## Verification

- Agent full suite after final capacity context fix: 494 passed, 1 skipped (227.58s); embedded tool suite: 1079 passed, 35 skipped (103.78s). PostgreSQL opt-in tests are run separately, not claimed as passed skips.
- Old workflow full suite: 1487 passed, 8 skipped, 483.36 seconds. No workflow business code changed.
- Real PostgreSQL stage/cache/capacity/audit plus cooperative stop group: 45 passed, 5.90 seconds. Tests use isolated namespaces and clean only their own rows.
- Frontend TypeScript/Vite build passed. Mocked desktop/mobile GUI tests: 2 passed; screenshots under R/data/tests/perf-browser-final2. No default/profile browser automation or model usage.
- Offline replay: all ten fixture candidates succeed in both modes; whole-draft writer calls 14 -> 10 plus 4 small scene patches; total calls remain 14. Response characters 4138 -> 3058. Extra local validation makes fixture CPU time longer; no live speed claim.
- GitNexus final tracked-change analysis: 69 symbols / 85 affected flows / 12 files, CRITICAL, no partial/truncated flags. New untracked modules require their separate impact/test/manual review; this graph result does not cover them as changed files. Existing graph has a stale-commit warning and documented receiver-resolution gaps.
- Existing web durable-recovery fixture was missing frozen execution context. Reproduced 11 failures against baseline; corrected test fixture only, not production confirmation gates.
- Final metric correction: two normal adapter calls no longer imply one retry. A failing audit assertion reproduced the overcount; adapters without explicit retry identity are reported as unknown. The final PostgreSQL/telemetry/model-platform/queue selection passed 100 tests (12.04s); agent observation/context/plan/progress selection passed 41 tests. Frontend rebuilt successfully after this correction.
- Final credential check: 39 changed/new files, zero matches against local configured credentials. Remote main and both backup refs were read back and match the baseline table. No optimization commit or push performed.
