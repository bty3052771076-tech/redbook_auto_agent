# Capability Frontend Report

Workspace: `E:/AI/codex/redbook_agent`.
Requirements: `2026-10-08-capability-frontend-brief.md` and matching UI acceptance document.

## Execution Rules

- Loaded `subagent-driven-development` and `frontend-design` in full. The explicit no-subagent instruction overrides dispatching; tasks, implementation, specification review and quality review run inline. No commits, worktree creation, installation or publishing.
- Only the permitted frontend paths, browser test and this report are authored here. Other shared-tree changes belong to the backend agent and are not reverted.
- All builds, screenshots, browser temporary profiles and pytest basetemp directories use `E:/AI/codex/redbook_runtime/data/tmp`.
- Design tokens reuse existing white `#ffffff`, surface `#f5f7f6`, text `#20292d`, muted `#5e6b70`, teal `#176f68`, fault `#cb5a48`; existing Geist/Inter/Microsoft YaHei, 24px heading, 19px drawer, 13px controls, zero tracking. Layout: navigation | tabs + compact table | 420px detail. Below 1280px detail overlays; below 1024px it fills the viewport. No illustrative asset is appropriate to this operational management surface.

## Preflight And Progress

| Task / shared contract | Check | State |
| --- | --- | --- |
| Browser test / missing entry | First test must fail for absent capability entry, not a compiler or harness failure | Verified |
| Center / App | React entry and callbacks need confirmation after UNKNOWN impact | `main.tsx` imports and renders App; confirm button calls confirmPlan |
| Shared request errors / existing consumers | Preserve successful data and same-origin request behavior | Only error extraction changed |
| Forms / resource APIs | Revision checks, no optimistic success, independent read/diagnostic operations | Implemented; regression in progress |
| Plan selection / confirm | PUT must update parent plan.version; confirm must preserve server skill selection | Implemented; regression in progress |
| Verification / evidence | Build and browser tests; report unsupported contracts honestly | In progress |

GitNexus impact: `api`, upstream, 17 direct callers, 20 affected symbols, 5 processes, CRITICAL. Warning was issued before edit; only error parsing and structured error metadata changed. `confirmPlan`: UNKNOWN, no graph callers; actual JSX callback confirmed. App's previously supplied UNKNOWN is not treated as low risk.

## Test Evidence So Far

1. RED, before product edits: `python -m pytest tests/test_capability_browser.py::test_capability_center_readonly_navigation_and_deep_link -q -p no:cacheprovider --basetemp E:/AI/codex/redbook_runtime/data/tmp/capability-browser/pytest`. Result: **1 failed in 12.69s**. Failure was missing `button[name=能力中心]`, Locator.click timeout 3000ms. Baseline build succeeded.
2. First product build: `npm.cmd run build -- --outDir E:/AI/codex/redbook_runtime/data/tmp/capability-frontend-build`. **Exit 0**, TypeScript + Vite; 1598 modules; CSS 37.58kB, JS 361.05kB. No dependency installation.
3. Browser round 1, unique basetemp `capability-pytest-20261009-01`: 3 failures. Two were a fixture URL-decoding omission; corrected with `urllib.parse.unquote`. One was a genuine unsaved guard race after successful MCP save.
4. Browser round 2, unique basetemp `capability-pytest-20261009-02`: **2 passed, 1 failed in 21.20s**. MCP success raced React's dirty-state commit; fixed by an explicit post-success navigation path, never used for user-initiated departure.

Browser/API fixtures isolate external side effects; they are not shipped data or proof of real PG/MCP/platform integration. Real backend integration remains separately identified.

## Pending Alignment / Review

- Backend `SkillService` now exposes `PATCH /api/skills/defaults`, using list `policy_revision`; frontend has aligned without changing the brief's route pattern.
- Tool purpose enum follows actual MCP service: evidence, duplicate_reference, style_reference, operations. MCP policies are connection-scoped and schema-hash guarded.
- Brief contains no trial-run endpoint, builtin-skill copy endpoint, date-range filter contract, process-exit endpoint or call-detail-by-ID endpoint. No fake action is added for these. Calls deep-linked outside the current page need backend ID filtering/detail retrieval; currently they report absence rather than fabricate history.
- Current backend API implementation is still in progress. Knowledge namespaces/purpose enum, context policy fields, call rows and plan/run snapshot shape require final end-to-end confirmation.

Final test results, modified paths, screenshots and unresolved gaps will be appended after verification.

## Stop Boundary / Latest Evidence

2026-10-09: work stopped on the latest design-only request; existing changes retained without commit or deployment.

Latest browser round: **6 passed, 2 failed in 59.47s**. The skill import test has two identically named import buttons and did not reach the import/security assertions. The knowledge test did not close its annotation drawer on ESC, leaving an overlay before search. Both remain unresolved. Build succeeded; real backend/context/recovery/platform integration and full visual acceptance remain unverified.
