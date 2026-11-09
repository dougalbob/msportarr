# Agent improvement plan

This is an agent-facing work plan derived from the repository review on
2026-11-06. It is not a promise that every item is still necessary when a slice
starts: first verify the relevant code and tests at the branch tip. The goal is
to turn improvements into small, independently reviewable changes, not to bundle
a redesign into one PR.

Status labels: **DONE** means completed in the documentation-refresh change that
introduced this plan; **NEXT** is the recommended first code slice; **PLANNED**
means useful follow-up work, not a commitment to implement it regardless of what
new evidence shows; **LATER** is deliberately deferred until prerequisites land.

## Order at a glance

| Order | Slice | Status | Main reason |
|---|---|---|---|
| 0 | Refresh agent-facing documentation and establish this plan | **DONE** | Stale instructions are a source of incorrect code changes |
| 1 | Make activity-event updates one locked, atomic transaction | **DONE (2026-11-07)** | Events can be lost or state files corrupted by concurrent processes |
| 2 | Apply the same persistence contract to grabber, watcher and WebUI JSON state | **DONE (2026-11-07, combined with Slice 1)** | These files are also shared or written directly |
| 3 | Replace free-form WebUI command strings with typed, guarded jobs | **DONE (2026-11-07)** | Prevent argument misuse, duplicate launches and unbounded output |
| 4 | Add deterministic tests at Prowlarr/SABnzbd/Plex boundaries | **DEFERRED (2026-11-07)** | Valuable while the pipeline changes often; the app is nearing end of development, so the fakes' upkeep outweighs the insurance. Revive if the search/submit/tag paths change materially, an upstream API breaks compatibility, or unexplained download failures need safe reproduction |
| 5 | Add browser-write defense-in-depth (CSRF/Origin and CSP) | **DONE (2026-11-07, light version)** | Reduce reliance on the outer Access/browser assumptions |
| 6 | Move the WebUI to a production WSGI server | **DEFERRED (2026-11-07)** | Only worth it if the built-in server causes a real problem; Slice 3's job state is still process-local, so multi-worker remains unsafe |
| — | Release mismatches: block, explain, replace | **DONE (2026-11-07)** | A release the rules wrongly accept is chosen on every retry, so "download it again" could not fix it |
| — | Hybrid: F1 artwork, calendars and namespaced state (steps 1, 2, 4) | **DONE (2026-11-09)** | Two series in one `/config` overwrite each other, and F1 could not exist without its own vocabulary, calendars and art |

## Delivered outside the slice order — the hybrid data layer (2026-11-09)

Steps 1, 2 and 4 of `docs/HYBRID_FEASIBILITY.md`, delivered as two changes:
F1 artwork plus calendars in the shared schema, then the per-series `/config`
state files with a one-time migration. Both are described in that document's
progress log; the short version is that the calendars of both championships are
now validated by one championship-agnostic validator that discovers its input
from `series/`, and every shared state store carries its series.

It is recorded here because it changed three documents' claims (§4, the state
table, and the validator description), and because the remaining hybrid work -
steps 5-9, the F1 grabber/sort/watcher paths and the WebUI series switcher - is
now the largest outstanding item in either plan.

## Slice 0 — documentation baseline (**DONE**)

Completed with this plan:

- Updated `AGENTS.md` from the stale v2.6.0 / 377-test snapshot to the
  repository's v2.9.0 release and the 463-test result last verified on
  2026-11-06. Keep the date beside the test count; it is a snapshot, not a
  permanent invariant.
- Corrected `docs/ARCHITECTURE.md`'s stale release/test claims, route-security
  descriptions, test descriptions and Python executable description.
- Added this roadmap and linked it from `AGENTS.md` and `docs/ARCHITECTURE.md`.
- Replaced the README's obsolete v2.3.0 release example with a version pattern.

Follow-up documentation rule: when code changes alter a route, process boundary,
state format, auth behaviour, environment variable or validation requirement,
update the relevant architecture/agent documentation in that same PR. Avoid
copying a changing test count into multiple documents; this plan has no count by
design.

## Done outside the slice order — release mismatches (2026-11-07)

Delivered ahead of the queue because it was blocking use of the app rather than
hardening it. `motogp_common.filter_releases()` is deterministic, so an accepted
release that is the wrong session is re-selected on every retry; deleting the
file changed nothing and the only remedy was a code change.

The change proves the persistence pattern from Slice 1 on a small store:
`utils/release_mismatches.py` uses a sidecar lock held across the whole
read-modify-write, a unique temporary file plus `os.replace`, and a parse
failure is logged at ERROR rather than treated as "no mismatches" (silently
un-blocking every release would reopen the exact loop). Slice 1 should reuse it
rather than re-invent it.

It also did **not** add a second download path: the replacement runs the
existing `backfill --force` command, and the wrong library folder is moved (never
deleted) into `<library>/.mismatched/`. See `AGENTS.md` §6b and
`docs/ARCHITECTURE.md` §4.

## Slice 1 — activity event transaction (**DONE, 2026-11-07, combined with Slice 2**)

Delivered together with Slice 2 as one change, at the maintainer's request:
the two slices share one helper and one transaction model, so proving them
separately would have meant reviewing the same contract twice.
`utils/json_store.py` is that helper - sidecar lock held across
load-mutate-commit, unique temp file plus `os.replace`, corruption logged at
ERROR - generalised from `utils/release_mismatches.py`, which now delegates
to it. The activity emitter keeps its JSON shape and public API; a failed
write is logged at ERROR instead of printed or dropped silently.
`tests/test_json_store.py` covers concurrent writers in separate processes,
lock release on exceptions, a failed replacement leaving the previous file
readable, and loud corruption handling.

**Problem evidence:** `activity_emitter.py` reads the JSON before acquiring its
lock. The lock is taken on the temporary output file only while serialising,
then released before that file is renamed over the destination. That does not
protect the read-modify-write transaction. Multiple emitters can read the same
snapshot and overwrite each other's changes; a shared temporary filename also
makes concurrent writes fragile.

**Scope:** Correct the activity store only. Use a stable sidecar lock file (not
the data file or a temporary file) and hold an exclusive lock across load,
mutation and commit. Write the new JSON to a unique temporary file in the same
directory, flush it, then atomically replace the destination. Keep the existing
JSON shape and public emitter API unchanged. Make failed writes visible through
the existing logger rather than silently dropping the event.

A small reusable storage helper is acceptable if it has a narrow API that makes
it impossible to accidentally lock only the write phase.
`utils/release_mismatches.py` (added 2026-11-07) already implements this
contract for one small store and is the reference implementation to generalise. Do not migrate every
state file in this slice: first prove the transaction semantics on the activity
store.

**Tests / acceptance:**

- Existing `tests/test_activity_feed.py` remains green.
- Add tests showing concurrent independent updates are both retained, the lock
  is released on exceptions, and a failed/interrupted replacement leaves the
  previous valid file readable.
- Run `python -m pytest tests -q` and the normal CI workflows.
- No event schema, ordering, retention-limit or Activity UI behaviour changes.

## Slice 2 — other JSON state files (**DONE, 2026-11-07, combined with Slice 1**)

All three candidate stores were migrated in the same change (see Slice 1).
Transaction models differ by how the owner works, which is why the helper
offers both forms: `webui_settings.json` takes a locked atomic whole-document
write (callers submit the complete document; the `max_grabs_per_run`
migration also re-reads under the lock), while the grabber's and watcher's
long-lived in-memory state is reconciled on save with
`json_store.three_way_merge` against the load-time snapshot - entries another
process committed meanwhile survive, and this process's own deletions still
apply unless the other process changed that same key. File names, schemas and
the `settings.yml` ownership/migration behaviour are unchanged. Remaining
direct JSON writers are out of scope by design: `discord_rate_limit.json`
(single writer, disposable) and the offline `scripts/cleanup_utility.py`.

## Slice 2 (original plan, kept for reference)

After Slice 1, audit each state file's actual readers and writers. Candidate
stores identified in the review are:

- `motogp_grabber.py`: `grabber_state.json` is written directly; scheduled and
  manually requested grabbers can overlap and overwrite state.
- `motogp_watcher.py`: `watcher_state.json` is written directly and read by the
  WebUI as well as the watcher.
- `motogp_webui.py`: `webui_settings.json` is written directly; concurrent
  settings requests can race.

Migrate one store per PR, or group only stores that share the same transaction
model. The operation must reload current state while holding the sidecar lock;
atomic replacement alone prevents partial JSON but does not prevent lost updates
from stale in-memory snapshots. Preserve existing file names and schemas. Do not
change `settings.yml` ownership/precedence or the one-time migration behaviour
without a separate migration plan and tests.

**Tests / acceptance:** exercise simultaneous writers, corrupt-file recovery,
permission errors, and old-format compatibility where applicable. Ensure a
reader never sees partially written JSON and that the WebUI's settings-store
ownership remains as documented in `AGENTS.md`.

## Slice 3 — typed WebUI jobs (**DONE, 2026-11-07**)

Delivered: `/run` now takes `{action, season, round, sessions, scale,
dry_run, force}` for the actions `status`, `scheduled`, `backfill`,
`overlay-season` and `overlay-round`. `build_job()` in `motogp_webui.py`
validates every field (unknown actions, fields, seasons, rounds, session
tokens and out-of-range scales are 400s) and builds the argv list itself;
the overlay-round folder is still resolved from the calendar server-side.
`utils/webui_jobs.JobManager` owns the job slot: claimed synchronously under
a lock in the request handler (the old check-then-thread race is gone),
output capped at 100k chars, exit code recorded, and a 1-hour timeout that
kills and reports. `/download-selected` (which previously had no running
check at all) and the mismatch redownload claim the same slot and execute
argv lists; their response "commands" remain display strings, never an
execution path. The frontend (`static/js/app.js`, settings overlay form)
sends typed jobs. Auth coverage for write routes is unchanged.

Deliberately not done here, per the original scope: no cancellation
endpoint (the timeout is the defined ending), and job state stays
process-local, which keeps Slice 6 gated on externalising it.

## Slice 3 (original plan, kept for reference)

**Problem evidence:** `/run` accepts a command string and routes it to a
subprocess after `str.split()` (`motogp_webui.py`). `shell=True` is not used, so
do not describe this as shell injection; the remaining concerns are unexpected
CLI arguments, weak request validation, duplicate launches racing through the
`running` check, and output retained without a bound.

**Scope:** Replace the public string contract with a small set of named actions
and typed fields (for example action, season, round, session selection and
validated overlay options). Map each action to an explicit argv list. Validate
ranges and supported options before starting any work. Reserve the singleton
job synchronously under a lock before creating its worker. Store job status in a
bounded output buffer, report exit status, and define timeout/cancellation
behaviour. Keep the existing user workflows working; update `static/js/app.js`
and templates in the same PR as the endpoint contract.

Do not combine this with switching WSGI servers. The current process-local job
state is one of the reasons a multi-worker server is not yet a safe drop-in.

**Tests / acceptance:** reject malformed payloads and unsupported actions;
verify exactly one job can start under concurrent requests; assert the generated
argv for every supported action; test bounded output, nonzero exit and timeout;
retain auth coverage for every write route.

## Slice 4 — integration-boundary tests (**PLANNED**)

Add local deterministic fakes/fixtures for external services rather than tests
that require a real Prowlarr, SABnzbd or Plex server. Start with contract tests
for search/download submission and Plex metadata calls, then add a focused
pipeline test for the parts that can run without Docker or actual media services.

Cover success, authentication failure, malformed responses, timeouts, retry
behaviour, duplicate/idempotent processing, and service unavailability. Tests
must never contain live API keys, LAN addresses or dependencies on a user's
library. Reuse the project's existing `pytest tests` invocation and preserve the
real-image smoke test as a distinct CI layer.

Keep this slice focused: do not attempt to simulate the entire home network or
rewrite all service access behind a new framework just to get tests in place.

## Slice 5 — browser-write hardening (**DONE, 2026-11-07, light version**)

Delivered as the light version, proportionate to a single-user app behind
Cloudflare Access; no Cloudflare configuration was changed or is needed -
`Origin`/`Referer` pass through the tunnel and the decision is made in this
process. `utils/origin_guard.py` is a `before_request` hook registered after
the auth guard: for write methods it requires an `Origin` (or `Referer`
fallback) whose **host** matches the request host (host only - Cloudflare
terminates TLS, so schemes legitimately differ), and allows requests with
neither header, which are non-browser clients governed by auth rather than
this guard. Safe methods and the public endpoints are exempt. The CSP is
served as `Content-Security-Policy-Report-Only` only: the templates still
rely on inline scripts/styles, so enforcing now would break the UI; the
report-only policy is the evidence-gathering step for a later tightening.
No CSRF token machinery: the Origin check covers the browser threat without
per-form state, and JSON clients have no ambient credential to abuse beyond
the verified identity itself.

Revive the full version (tokens + enforcing CSP) only if the app ever gains
multiple users, cookie-based sessions carrying more authority, or a reason
to distrust the pages it serves.

## Slice 5 (original plan, kept for reference)

Review every mutating route and its browser call site. Add a consistent Origin
(or Referer fallback) policy and/or CSRF token for browser-originated writes;
maintain JSON clients and the Cloudflare Access verification model. Add a
Content Security Policy in report-only mode first if existing inline scripts or
styles prevent a strict policy, then tighten it with targeted template changes.

Test allowed same-origin writes, rejected cross-origin writes, token handling,
and the public health/static endpoints. Do not assume that adding CSRF controls
replaces server-side auth or that `AUTH_MODE=none` is safe outside a trusted
network.

## Slice 6 — production WSGI server (**LATER; prerequisite: Slice 3 and state audit**)

Flask's built-in server is currently started directly in `motogp_webui.py`. A
production WSGI server is a reasonable operational improvement, but do not add
multiple workers until process-local state (especially job status) is either
externalised or the worker model is proven safe. First document the required
SSE behaviour, signal/shutdown handling, container healthcheck and restart
semantics. Then make the smallest deployment change and exercise it in the real
container smoke workflow.

## Rules for every slice

1. Re-read the target code and relevant tests; this plan records review findings,
   not immutable facts.
2. Keep persistent formats, calendar/session identifiers and library paths
   unchanged unless the slice explicitly includes a tested migration.
3. Add a regression test for the behaviour being changed; do not weaken an
   existing security or correctness assertion to make a change pass.
4. Run `.venv/bin/python -m pytest tests -q` (never bare root-level `pytest`;
   see `SANDBOX.md`). If dependencies are missing, install
   `requirements-dev.txt` in the repository's ignored `.venv` first.
5. For Dockerfile, entrypoint, dependency or runtime changes, rely on and inspect
   `.github/workflows/container-smoke.yml`; unit tests do not prove the built
   image works.
6. Update `AGENTS.md` and `docs/ARCHITECTURE.md` when their claims change, and
   mark the relevant slice complete only after tests/CI verify it.

## Additional review candidates (2026-11-07)

These candidates came from a follow-up review of the scheduler, watcher, WebUI,
health probe and release workflow. They are proposals, not commitments: re-check
the current implementation and user impact before starting a slice. Prefer the
operational visibility work first; do not turn these into a broad redesign.

### Candidate A — background-process health and progress (**DONE 2026-11-07**)

Delivered as **Slice A**. `utils/background_health.py` owns the record and the
rule: the scheduler writes `last_loop` at the top of every iteration, `last_check`
when a scheduled search ran and `started` once; the watcher thread writes
`last_scan` after every scan (including an empty one and one whose downloads
path is missing) and `started` when it starts. Both go through
`utils/json_store.py` as `background_health.json`, one component per write, so
the scheduler and the watcher cannot erase each other, and a failed write is
logged rather than raised - the caller is a sleep loop.

The rule is pure and takes an explicit `now`. States are `live` / `stale` /
`never`; **`never` is never healthy**, because a fresh container has no
evidence and must not claim otherwise. The stale threshold is twice the
scheduler's *own* interval plus an hour of grace, read from
`motogp_scheduler.CONFIG` (including quiet hours, via a new injectable
`is_quiet_hours(now)`), so a correct overnight run is not called dead at 03:00.
"Progress" is always the loop field, never `last_check`: off-weekend the
scheduler runs no search for days, and a search-keyed rule would call a healthy
scheduler stale for that whole time.

Surfaced on the home health strip ("Loop: last pass …", "Watcher: scanned …")
and in `/diagnostics` under `background`, including per-component reasons for
`never`. The pre-existing chip there was renamed "Last search: …" - it is the
grabber's `last_scheduled_check`, which only moves when a session is actually
searched, so on an off-weekend it is legitimately days old and sat next to a
fresh "Loop" chip looking like a contradiction. Both are rendered on page load from one small JSON read - no network
call, keeping the home tab's existing "static by design" rule.

**Decision recorded, not left implicit:** a stale heartbeat is a warning, never
a Docker failure. `/healthz` still performs no file reads and no network calls,
and `tests/test_background_health.py` asserts `utils/healthcheck.py` never
learns about the heartbeat, so a slow-but-legitimate Prowlarr search cannot put
the container into a restart loop. `tests/test_background_health.py` covers the
thresholds and their boundary, quiet hours, concurrent writers, a failing
write, corrupt state, the watcher's empty-scan and missing-path beats, one real
scheduler iteration, and the diagnostics route.

The container health probe checks the WebUI `/healthz`. `entrypoint.sh`
supervises the scheduler process and stops the container if that process exits,
but process liveness alone cannot show that work is progressing; the watcher is
also a thread inside the scheduler. A stuck scheduler or stopped watcher thread
could leave an apparently healthy WebUI without the expected background work.

Consider a small heartbeat/status record for scheduler loop progress and watcher
liveness, plus timestamps for the last scheduler check and grabber run. Expose
these in diagnostics and a compact dashboard status panel, with a clear stale
threshold and a visible “unknown” state when no heartbeat exists yet. Keep
`/healthz` lightweight and deterministic; do not make it depend on Prowlarr,
SABnzbd, Plex, or a race weekend being active. Decide separately whether a stale
heartbeat should affect Docker health or only produce a warning, so a slow but
legitimate search does not cause restart loops.

**Acceptance:** tests prove timestamps advance in normal operation, stale state
is reported without a network call, and initialization/shutdown do not show a
false healthy status. Preserve the current process supervision semantics unless
there is evidence for a specific change.

### Candidate B — make the release sequence hard to get wrong (**DONE 2026-11-07**)

The release flow is split across the Unraid template's pinned `TemplateURL`, a
version tag, the container image workflow, and release notes. During v2.11.0,
the tag was attempted before the template pin had been updated; the template CI
check caught the mismatch and required a follow-up PR before a working release
could be rebuilt. This is a sequencing hazard, not a reason to weaken the test.

Document or automate a release-preparation step that updates the Unraid
`TemplateURL` to the candidate version and verifies the template before tagging.
After the tag, verify that the versioned image and `latest` were published before
calling the release shipped. Keep the image build tied to the exact tagged
commit, and make retries safe when a release or image already exists. Consider
whether the release workflow should block publication when prerequisite checks
or the template pin are inconsistent.

**Acceptance:** a dry-run or workflow test covers the order: template change is
merged first, tag points to that merged tree, image tags resolve to that commit,
and release metadata refers to the same tag. Do not create/move a release tag as
a workaround for a failed check without documenting and validating the exact
resulting image and release state.

**Delivered as Slice B (2026-11-07).** Three layers, cheapest first:

* `scripts/check_release_readiness.py` is the pre-flight a maintainer can run
  before pushing a tag. It checks the version is `vX.Y.Z` (the `V.2.2.1`
  failure), that `unraid/motogparr.xml` pins *that* tag, that every `Dockerfile`
  `COPY` source exists, and that the tag is not already on the remote. It
  reports and exits non-zero; it never edits the template or creates a tag - a
  check that repairs its own input cannot catch the mistake it exists to catch.
  With no git remote the tag check warns rather than passing silently.
* `.github/workflows/release-prep.yml` runs it on **every PR to main** in
  `--mode check`, reading the candidate version from the template pin itself, so
  there is no second place to keep in step. The mode matters and was found by
  the workflow's first real run: on an ordinary PR the template is pinned to the
  last *released* tag, so `--mode prepare`'s "the tag must not exist yet" rule
  failed every PR to main. `--mode check` asks the question CI can answer - the
  version format, that the pin names a version, and that every `Dockerfile` COPY
  source exists - and treats an unpublished *next* version as a warning, because
  that is exactly what the bump PR looks like before its tag is pushed.
  `--mode prepare` stays strict for the maintainer, for whom a tag that already
  exists is the failure mode being prevented. Known limit: in check mode nothing
  forces a merged pin to correspond to a real tag, because the tag does not exist
  until after the merge - publishing is the backstop, since `release.yml` refuses
  a tag its own tree does not pin.
* `release.yml` now gates publishing: the tagged tree must pin its own tag
  *before* the release step, and the job does not call the release shipped
  until `ghcr.io/<repo>:<version>` **and** `:latest` both resolve. It polls the
  package tags (the build runs in a different workflow on the same tag push) and
  names which one is missing. It needs `packages: read`, and cannot start or
  repair a build. Retry safety is unchanged and still tested.

`tests/test_release_readiness.py` covers each decision and the negative cases,
the step ordering inside `release.yml`, and the readiness workflow's trigger and
permissions.

### Release checklist

The order is the whole point, so it is written down here as well as enforced:

1. **Bump the pin.** `unraid/motogparr.xml`'s `TemplateURL` → the candidate tag;
   merge that PR. (`release-prep.yml` must be green on it first.)
2. **Tag the merged commit.** `git tag vX.Y.Z <merge commit> && git push origin
   vX.Y.Z` - tag the merge commit of step 1, never an earlier one. Never tag a
   tree whose template pin points somewhere else; that is what forced the
   v2.11.0 rebuild.
3. **Confirm the image.** The release job waits for `:X.Y.Z` and `:latest`
   (Unraid installs track `:latest`, so a versioned tag alone is not shipped).
4. **Merge the generated notes PR** (`release-notes/vX.Y.Z`) so the notes reach
   the repository.
5. **Never move a tag** to repair a failed check: the published image would come
   from a tree the pre-flight never saw. Cut a new patch version instead.

### Candidate C — explain ordinary search and processing failures end to end (**DONE 2026-11-07**)

The mismatch workflow already explains why a release title matched the filter.
A related improvement would make ordinary unsuccessful sessions easier to
diagnose: distinguish service connection/authentication errors, no search
results, candidates rejected by filtering, SAB submission failures, stalled or
failed downloads, and sorter/Plex-processing failures.

First audit the existing activity events and logs to identify which causes are
already recorded and where context is lost. Then add only the missing structured
reason fields and a focused UI presentation (for example, a per-session
“why it did not download” detail). Reuse the existing pipeline and mismatch
explanation; do not add a second search/download implementation or duplicate
release-filter logic in the browser.

**Acceptance:** representative failure cases have deterministic tests, secrets
and full API URLs are not exposed, and a user can distinguish “nothing ready”
from “the work failed” without searching several unrelated log sections.

**Audit first, as the candidate required.** What was already recorded: the
activity feed carried ERROR events, the watcher had a retry state with
`last_error`, and the logs had a line per cause. What was **lost**: the reason
itself. `grabber_state.json` stored `"No results"` for a 401 from the indexer, an
unreachable Prowlarr, no enabled indexers and a genuinely empty search alike;
`"Grab failed"` covered an unreachable SABnzbd, a rejected key and a refused
release; the watcher stored `"Processor returned False"` and, in the exception
path, no activity event at all. The structured fields, not new logging, are what
was missing.

**Delivered as Slice C.** `utils/failure_reasons.py` is the whole vocabulary: a
closed set of stages in pipeline order (`search_setup`, `search_indexer`,
`no_candidates`, `rejected`, `blocked`, `sab_submit`, `download`, `process`),
each with a label and a next-step hint, and an `actionable` flag that is the
distinction the candidate asked for - **wait** ("nothing published yet" retries
itself) versus **fix something** (a dead service never does).

Wiring, kept as small as possible:

- `search_for_session()` takes an optional `diagnostics` **out-parameter**
  rather than a new return value, so every existing caller and test is
  unchanged. It counts indexers, failed indexers, candidates and filtered
  results, and notes whether every candidate was dropped because the user
  blocked it.
- `search_indexer()` returns `(releases, error)` so a refused key, a timeout, an
  unreachable host and an empty answer are no longer the same empty list. The
  error never contains the URL - Prowlarr's search URL has `apikey=` in its
  query string.
- `grab_release()` returns `(nzo_id, error)` for the same reason on the SABnzbd
  side.
- `explain_search()` maps the counters to a stage, ordered so that a setup error
  outranks an empty result: both produce zero candidates and only one needs a
  human. Partial indexer failure is reported but **not** called a failure,
  because incomplete results on a half-working indexer should not send the user
  hunting every weekend.
- `GrabberState.mark_failed()` still accepts the old bare string (every
  existing install has that shape on disk) *and* a structured record. The
  watcher's `FailedItem` gained `stage`/`hint`/`actionable`, with `from_dict`
  tolerating old entries so retry state is never lost by adding a field.

**Redaction is the security half of this change.** Every stored reason passes
through `failure_reasons.sanitise()`, which removes configured secret values,
keeps only `scheme://host:port` of any URL, and masks named credential
parameters and bare 32-character tokens. These strings are persisted in
`/config`, rendered in the UI and copied into reports; AGENTS.md §7a records
that this repository has already published a live Prowlarr key once, and a
requests exception renders the full URL.

**UI.** `/failed-sessions` normalises each entry (`describe()`), and the
Download tab's pre-download warning shows the stage-named reason, an ⚠️ for
actionable faults and the hint beneath it. Entries written before the stages
existed keep their reason and show **no** stage - labelling a historic "No
results" as "not published yet" would relabel an outage as a quiet weekend. The
reason is escaped, not interpolated as markup (same decision as the mismatch
dialog).

`tests/test_failure_reasons.py` covers the vocabulary, the actionable
classification, redaction, each `explain_search` branch including the ambiguity
cases, both grabber seams, the legacy string/entry paths, and the route and the
dialog. The three existing fakes were updated to the new seam shapes without
weakening an assertion.

### Candidate D — measure library scan cost before optimising (**DONE 2026-11-07, measurement only**)

The watcher caches a library scan for five minutes, while UI routes also inspect
library state. For a larger library these walks may affect response time or
watcher responsiveness, but the review did not establish that they are currently
slow enough to justify a new index.

Instrument scan duration and scope, surface unusually slow scans in diagnostics,
and add a representative temporary-library benchmark/test. If measurements
show a real bottleneck, consider a targeted scan or incrementally refreshed
index with a full-rescan correctness fallback. Avoid a database or persistent
index until measurements demonstrate that the simpler scan is the problem.

**Delivered as Slice D (measurement only).** `utils/scan_metrics.py` times the
two walks that could plausibly be slow - `library-season`
(`scan_library_for_season`, called on every home page and Download tab render)
and `watcher-library-cache` (`LibraryChecker._refresh_cache_if_needed`) - and
records duration plus scope (rounds and sessions visited). `/diagnostics`
reports `scan_cost` with count, median, p95, max, the scope of the slowest
sample and any sample over two seconds named individually. Percentiles are
nearest-rank, deliberately: a single bad sample must stay visible instead of
being smoothed into its neighbours. A mean would have hidden it entirely.

The wrapper returns the scanned value untouched - instrumentation that changes
the answer is a bug wearing a metric - and the context-manager form records a
raising scan, because a walk that fails after four seconds is the interesting
sample.

**No optimisation was done, and that is the deliverable.** Nothing is persisted,
nothing is cached differently and no index exists. `tests/test_scan_metrics.py`
includes a representative temporary library (160 session folders over two
seasons, using real `SESSION_ELEMENTS` numbers so the app can actually read it),
proves the WebUI and watcher walks are measured with the right scope, and proves
the watcher still caches for five minutes. Acting on the numbers is a separate
decision that needs a real library, which this sandbox does not have.

### Candidate E — small Activity and diagnostics UX improvements

Once the structured failure reasons and background health data exist, consider
adding focused Activity filters (season, round, session, stage) and links from a
failure to the corresponding session/library view. A compact diagnostics
summary could show service reachability, scheduler/watcher progress, and recent
failure counts while keeping credential values and sensitive URLs hidden.

Treat this as a follow-on to A and C: do not add filters that merely reformat
unstructured log text, and keep the current simple UI usable on mobile.

### Revisit the deferred integration tests only when triggered

Slice 4 remains deferred as recorded above. Revive a narrow set of deterministic
Prowlarr/SABnzbd/Plex boundary tests if those paths change materially, an upstream
API compatibility issue occurs, or unexplained failures need safe reproduction.
Do not build a full fake home network just for coverage metrics.

### Grouping the candidates into reviewable slices (2026-11-07)

The candidates above are grouped into four dependency-ordered slices. Each slice
is one branch and one PR, so a reviewer can judge one behaviour change at a time.
Ordering is by user-visible value per unit of risk: visibility first, then the
release process, then the diagnostics that depend on the new data, then the
performance work that needs measurements before any optimisation is justified.

| Slice | Candidates | Status | Why grouped this way |
|---|---|---|---|
| **A** | Candidate A | **DONE (2026-11-07)** | Adds the heartbeat records the later visibility work reads. Pure, testable rules; no dependency on anything else. |
| **B** | Candidate B | PLANNED | Independent of A. Workflow and template only; no runtime code. |
| **C** | Candidate C (+ the data A produces) | **DONE (2026-11-07)** | Needs A's records to tell "the machine is alive and has nothing to do" apart from "the work failed". |
| **D** | Candidate E (Activity filters, diagnostics summary) | PLANNED | Pure presentation over A and C. Explicitly a follow-on: filters over unstructured text are not worth building. |
| **D** | Candidate D (library scan cost) | **DONE (2026-11-07, measurement only)** | Instrument now; no index, no database, no caching change until a measurement on a real library shows the scan is the problem. |

Deliberately not scheduled with the others:

- **Candidate D** stays a measurement task. Changing the scan without evidence
  would be optimising the wrong thing, and the watcher already caches for five
  minutes.
- **Slice 4 (integration-boundary tests)** remains deferred as recorded above;
  none of these candidates triggers the revival condition.

Status labels: **DONE** means completed and verified by tests in the same change;
**NEXT** is the recommended first code slice; **PLANNED** means useful follow-up
work, not a commitment to implement it regardless of what new evidence shows;
**DEFERRED** means explicitly not now, with the trigger that would revive it.
