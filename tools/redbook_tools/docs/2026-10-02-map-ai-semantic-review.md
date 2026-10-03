# Stored Map and AI Digest Semantic Review

## Main Integration Update

The main controller completed CLI wiring after the domain handoff. Stored map
checks now run in the local render shortcut, deterministic quality gate,
non-news review, completed-job revalidation and agent upload before receipt reuse.
Completed-map reuse and agent upload also require readable, hash-matched map
assets. AI upload uses the existing current-copy source review. VLM-disabled or
missing-reviewer branches preserve deterministic errors rather than erasing them.

The positive local-map unit fixture now includes frozen source/date/coordinate
evidence. Added cases cover missing evidence, cached vision and disabled VLM.
They were authored only; no tests were started for this final integration.
The earlier "CLI wiring required" section below is the historical domain handoff,
not a current claim that this wiring is absent. Direct non-agent upload paths are
not newly wired by this change and are not covered by this integration claim.

## Scope and Stop State

The latest instruction is design/code only, with no additional tests, generation
or upload. The already-started offline test session was allowed to finish. No
further pytest invocation was started after that instruction. No production
draft, CLI, Web, artifact store, PG schema or user key was changed. No production
service was started or stopped. The generation module is frozen after the changes
listed below.

This work is not a completed 12-draft acceptance. The CLI semantic gate is still
the main controller's responsibility. Passing image/hash checks or old renderer
tests must not be interpreted as semantic approval.

## Validator Contract

```python
from src.global_map.review import stored_global_map_review_issues

issues: list[str] = stored_global_map_review_issues(
    post.platform.get("global_map"),
    basemap_path=None,  # optional explicit local GeoJSON path
)
```

Signature:

```python
def stored_global_map_review_issues(
    metadata: object, *, basemap_path: str | Path | None = None,
) -> list[str]:
    ...
```

Input is the stored map dictionary or `MapSnapshot`. It is not a `Post` or the
whole platform dictionary. Output is an ordered list of blocking diagnostics;
event indices refer to the original 1-based stored order. Nonempty output must
block approval and upload. Empty output means only these local semantic checks
passed, not image validity, publisher authenticity, remote save or delivery.

The validator is read-only. It does not repair coordinates, translate, render,
regenerate, update metadata, initialize storage, or call a model, API or PG. A
local country-name GeoJSON catalog is read when validating inferred anchors.
Diagnostics do not echo source URLs or query tokens.

Blocking codes:

- `MAP_METADATA_INVALID`: missing/malformed snapshot, dates, events or identity.
- `MAP_UPLOAD_BLOCKED`: `upload_allowed` is not the boolean `True`.
- `MAP_SOURCE_NOT_FRESH`: source state is not `ready` or `partial`.
- `MAP_EVIDENCE_UNVERIFIED: event N`: no traceable same-Beijing-day evidence at
  or before the frozen timezone-aware cutoff.
- `MAP_LOCATION_UNVERIFIED: event N`: coordinate or source-role mismatch,
  ambiguous country, unsupported country anchor, or unconfirmed flight landing.
- `MAP_EVENT_DUPLICATE: event N`: repeated event key or reused evidence identity.
- `MAP_COVERAGE_INSUFFICIENT`: recomputed coverage has fewer than 3 grounded
  events or 2 countries; saved counts cannot override this calculation.

Unknown-location audit events are excluded from coverage. Country anchors must
match the source-derived country and established anchor coordinates. Fresh
evidence articles that disagree or cannot support an inferred location block the
event; stale extra articles do not displace valid current evidence. For flight
incidents, destination/nationality is not a location: a confirmed, unambiguous
landing is required; negated, planned and conflicting landings are rejected.

Reliable nonflight `city` / `explicit_coordinates` rows retain their supplied
coordinates, including latitude zero. This relies on the upstream explicit
coordinate contract; it is not polygon containment or independent authentication
of the coordinate provenance. Country inference remains a conservative local
rule, not a general natural-language fact checker. Unresolvable events are
blocked rather than assigned a guessed country or capital.

The AI entry point already exists and was not changed in this task:

```python
from src.ai_digest.generate import stored_ai_digest_review_issues
issues: list[str] = stored_ai_digest_review_issues(post.platform.get("ai_digest"))
```

It also accepts `AIDigestBrief`. It checks stored copy without silently cleaning
or repairing it, preserves 1-based item indices, and ignores cached completion
or upload receipts as authorization. It detects the library-release/model-release
relationship error; it does not independently verify all facts or freshness.

## Domain Generation Wiring Completed

`src/global_map/workflow.py` now uses the shared source-role resolver from
`geography.py`. Ambiguous headlines do not become locations through raw country
hints or generated Chinese text. Reliable nonflight coordinates are marked with
their explicit-coordinate method.

`create_global_map_post` validates eligible snapshots before the translator and
again after it. An issue produces a replaced snapshot with `upload_allowed=False`,
`coverage_status="blocked"` and diagnostic warning. Existing audit image/JSON
output behavior is retained, but no publishable `Post` is returned. A source-side
semantic failure prevents even calling the translator. Stored old drafts are
never automatically rewritten by this validator.

## CLI Wiring Required From Main Controller

These are actual current `apps/cli.py` symbols, not proposed new UI endpoints:

1. `_local_global_map_vision_result`: call the map validator after reading
   `platform["global_map"]` and before any local-render success return. Any issue
   prevents the local success shortcut. Returning `None` alone is insufficient:
   the outer deterministic gate must also record the semantic rejection, or the
   draft could fall through to generic vision or cached approval.
2. `_run_auto_quality_gate`: add map and AI stored-copy diagnostics to each
   post's deterministic issues before cached vision, best-of-two or local renderer
   reuse. Mark `deterministic_ok=False` and preserve blocking errors. A cached
   score, hash match or explicit disabling of VLM must not erase semantic errors.
3. Agent `review_non_news.check`: add a `daily_global_map` branch that validates
   the stored map before `_run_auto_quality_gate`, just as `daily_ai_digest`
   already uses `_agent_ai_digest_review_issues`. Invalid maps must not enter the
   approved artifact list.
4. Agent `revalidate_completed`: currently only the AI branch is present. Add
   `daily_global_map` validation of active stored posts and requested count,
   before completed-job/artifact reuse. Completed PG state or retained approval
   must not override the current map semantics.
5. Shared upload preflight: call the same validators on the exact current stored
   copy before platform submission, even on direct upload or receipt-reuse paths.
   Keep replacement destination handling, original identity and PG durability;
   do not repair the old draft during validation or downgrade PG to memory.

Suggested local helper body, owned by the main controller:

```python
platform = post.platform if isinstance(post.platform, dict) else {}
return stored_global_map_review_issues(platform.get("global_map"))
```

Append this result to the existing blocking issue lists. Do not hide it in a
render report, a warning-only flag or a positive vision receipt.

`tests/test_global_map_local_review.py` is unchanged here. Its positive fixture
has only headings and counters, without source/coordinate/date evidence. That
fixture must be replaced with a genuinely grounded unit snapshot when CLI wiring
is implemented; its previous passing result does not prove semantic correctness.
An additional equal-image-bytes/wrong-source-location case remains pending. No
new test was added or run after the user's stop instruction.

## Actual Evidence and Run History

Offline runner, logs, JUnit and temp directories are all under:

`E:/AI/codex/redbook_runtime/data/runs/tests/20261002-map-ai-morning`

Interpreter: `E:/AI/codex/redbook_agent/.venv/Scripts/python.exe -B`.
The runner disables automatic pytest plugin loading and guards sockets, psycopg,
real model constructors and worker subprocesses. Model-generation coverage uses
a fake response only. Existing source clients are test doubles. No real PG, API,
model or platform call was made; every recorded run reports 0 blocked external
attempts. Nothing was installed.

| Log / label | Exit | Actual result | Pytest duration |
| --- | --- | --- | --- |
| `baseline.log` | 1 | 122 setup errors in the test runner | 7.52 s |
| `baseline-2.log` | 1 | 58 failed, 64 passed | 11.79 s |
| `generation-red.log` | 1 | 2 failed, 60 deselected | 5.04 s |
| `semantic-1.log` | 0 | 126 passed | 3.36 s |
| `edge-red.log` | 1 | 3 failed, 3 passed, 108 deselected | 5.24 s |
| `domain-all.log` | 0 | 157 passed, 2 warnings | 11.17 s |

The final process was session `85316`, already running when the user stopped
additional testing. It completed with exit 0; wrapper duration was 11.77 s.
Full failure stacks are retained in the respective logs. Important causes:

- First runner failure: `offline_pytest.py:48` patched a nonexistent
  `translate.init_chat_model` (`AttributeError`). The guard was corrected to
  the actual `openai.OpenAI` constructor; this was not a product failure.
- Baseline product failure: `test_stored_global_map_review.py:12` imports
  `src.global_map.review`, which did not exist (`ModuleNotFoundError`).
- Generation red: the old source gate called the forbidden translator, and the
  old post-translation path returned a publishable Post for the bad map.
- Edge red: nonhashable source states raised TypeError; huge coordinates raised
  OverflowError in `review.py:31`. The validator was fixed to return issues.
- Final warnings: LangGraph `allowed_objects` pending deprecation and Pillow
  `getdata` deprecation. Neither was suppressed or fixed outside scope.

The 157 tests cover the AI release-integrity suite plus stored map, country-role,
location-safety, event-cluster, translation, source-fallback, dedupe and legacy
local-render-review suites. They do not prove CLI semantic integration, real
model behavior, live retrieval, PG integration, platform delivery or 12-draft
acceptance. No additional regression was run after the stop instruction.

### Read-Only Real Artifact Reproductions

- AI post `e26ff2b590d24c97b33ab4002d5f4e44`: item 4 calls a Transformers
  v5.18.0 library upgrade a Qwen open-weight model release. The existing fixture
  is compared to the actual stored item; stored review rejects that relation.
  Fake-model generation repairs the library subject and retains provenance.
- Map post `34c5a4bdcfb9435aae6ca73a4126c13c`: source-grounded review rejects
  events 1, 2 and 5 (flight destinations and the US accuser anchor), and rejects
  recomputed coverage. `tests/fixtures/global_map_2026_10_01_stored.json` is a
  structural copy of that actual public metadata, not fabricated news.

Both production files were read only and their before/after SHA256 values match:

```text
map: DB90F1096B948D26867850AFCDE8333A3736687B5FE7858D9C276FCFA514B41B
AI:  08FA196A683F219D9046864BBA2F18526D2D04083F7F97CED4FD9EE447CD2603
```

## Impact and Changed Files

GitNexus only indexes `auto_redbook` at `E:/AI/codex/redbook_workflow`, commit
`3f6f1a8be871a4369f074d79aed9834851d0ae4a`; it does not index standalone tools.
Symbol-name upstream queries reported CRITICAL and were warned about before
editing. File-target queries returned geography -> evidence/workflow and
workflow -> service, but their LOW file axes do not waive the CRITICAL symbol
warning. New validator/test targets were UNKNOWN; actual `rg` references and
source bodies were checked, not treated as an unused-symbol all-clear. No commit
or clean standalone graph-change claim is made.

Changes in this task only:

- `src/global_map/review.py`: new stored semantic validator.
- `src/global_map/geography.py`: shared conservative event-country resolver;
  boolean/nonfinite coordinate rejection.
- `src/global_map/workflow.py`: shared resolver and pre/post-translation gates.
- `tests/test_stored_global_map_review.py`: real-fixture, generation-boundary and
  malformed-input regression cases.
- `tests/test_global_map_location_safety.py`: isolated deterministic country
  catalog so location tests do not depend on the machine's basemap default.
- `tests/test_ai_digest_release_integrity.py`: actual stored AI repro and fake
  model generation-boundary coverage; AI production modules unchanged.
- `tests/fixtures/global_map_2026_10_01_stored.json`: captured public map metadata.
- This document.
- Runtime-only `offline_pytest.py` and per-run logs/XML/summaries/temp outputs.

No further code/test/production actions are required from this child task after
this handoff. CLI integration and any later acceptance execution require the
main controller to follow the user's current authorization.
