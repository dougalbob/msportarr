# AGENTS.md — read this first

Kickoff document for an agentic contributor working on MotoGParr. It describes
the system, its invariants, the current implementation status and the next
recommended work. Keep status and plans current when code or priorities change.

**Latest repository release: v2.10.0** · **Last verified here: 509 tests passing (2026-11-07)** · **Single user, self-hosted**

---

## 1. What this app is

A single-container Docker app that automates MotoGP/Moto2/Moto3 session retrieval
into a Plex TV library. It is not a general-purpose arr app: it exists for one
household and one library.

```
calendar (JSON)  ->  Prowlarr search  ->  SABnzbd download
                                              |
                                     watcher detects completed folder
                                              |
                       sort into Round NN Venue/ + artwork/tags
                                              |
                                    Plex library scan
```

Full detail — runtime topology, configuration precedence, naming rules, the WebUI
route map and which routes are dangerous — is in
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**. Read it before touching
routing, config or the library layout.

Sandbox/tooling problems already solved are in **[SANDBOX.md](SANDBOX.md)**.
Check it before debugging the environment; it will save you an hour.

Release history: GitHub Releases (`gh release list`). Per-release notes:
`RELEASE_NOTES_vX.Y.Z.md` at the repo root, starting with v2.4.2.

---

## 2. Read in this order

| Order | Document | Why |
|---|---|---|
| 1 | `AGENTS.md` (this file) | Status and plan |
| 2 | `docs/ARCHITECTURE.md` | How the app actually fits together |
| 3 | `SANDBOX.md` | Environment gotchas |
| 4 | `README.md` | User-facing behaviour, install, env vars |
| 5 | `docs/CALENDAR_CHANGES.md` | The numbering rules you must not break |
| 6 | `docs/HYBRID_FEASIBILITY.md` | Investigation into combining this app with F1arr into one series-aware app (MotoGP + F1) |
| 7 | `docs/IMPROVEMENT_PLAN.md` | Prioritised next work and reviewable PR boundaries |

`aicontextual_notes/` is **historical and superseded**. Do not treat it as
current; it described work up to v2.0 and surviving facts were folded into
`docs/ARCHITECTURE.md`.

---

## 3. Non-negotiable rules

These come from real outages. Breaking one causes silent data problems, not
visible errors.

1. **Round numbers and session numbers are permanent identifiers** — for any
   season that still has files on disk. They are part of folder names, filenames
   and Plex identity, so renumbering a live season renames files and Plex treats
   them as new items (lost watched state, possible duplicates). See
   `docs/CALENDAR_CHANGES.md`.
   Session numbers come from `SESSION_ELEMENTS`, **not** from the enum value, and
   are allocated in blocks of ten so a new session takes a free number instead of
   being appended after the Race. Never renumber an existing value; allocate into
   a gap. `LEGACY_ELEMENT_SEASON` marks the last season written with the old dense
   numbering (`SESSION_ELEMENTS_LEGACY`, read-only). A number that means different
   sessions in the two schemes must never be resolved by guessing — the season
   decides, and `tests/test_session_numbering.py` enforces both invariants.
2. **Env var > `settings.yml` > default.** An env var set but *empty* counts as
   unset. Never add a non-blank default for a path in the Unraid template.
3. **`ENV_SPEC` in `utils/config_loader.py` is the single source of truth** for
   which env vars are read. A variable not in that table is not read by
   anything — that class of bug (documented but dead `PLEX_PATH_CONTAINER`) has
   happened and is what the table exists to prevent. Adding an env var means
   adding it to `ENV_SPEC`, and the Unraid template test will check it.
4. **Fail visibly, never silently.** The recurring theme of this codebase is
   things dying quietly: a dead WebUI behind a healthy container, a path mapping
   that never matched, a calendar that was never refreshed. Prefer refusing to
   start, or a loud warning, over doing nothing.
5. **One user.** Do not build user accounts, roles, sign-up, or a household
   registry. Identity is Cloudflare Access' job; see §5.
6. **A marked mismatch blocks a release, it does not delete media.** The block
   is what makes "download it again" behave differently; the folder is *moved*
   to `<library>/.mismatched/`. Never turn the mismatch path into a delete, and
   never widen the title match beyond the normalised exact comparison - a fuzzy
   block silently discards good releases and the symptom is a session that
   never downloads. See §6b.

---

## 4. Change workflow

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q          # last checked: 509 passed (2026-11-07)
```

Always use `pytest tests` — **not** bare `pytest`, which also collects the
root-level `test_*.py` scratch scripts and errors out. See `SANDBOX.md`.

- Keep the applicable CI workflows green: `pytest tests`, calendar validation
  for calendar changes, and the container smoke test for application/runtime
  changes. Check each workflow's path filters and triggers rather than assuming
  every workflow runs on every PR.
- Keep the style of the existing code: docstrings explain *why* a rule exists,
  quoting the failure it prevents. Comments that restate the code are not useful.
- Prefer one concern per PR. Follow the dependency-aware slices in
  [`docs/IMPROVEMENT_PLAN.md`](docs/IMPROVEMENT_PLAN.md).
- Never commit `*.backup*` files.
- **A release is a sequence, and it is checked.** Bump the Unraid
  `TemplateURL` pin, merge, *then* tag that merge commit; the release job
  refuses to publish a tag whose own tree does not pin it, and does not report
  success until the versioned image and `:latest` both resolve. Run
  `python scripts/check_release_readiness.py --version vX.Y.Z` before tagging
  (`release-prep.yml` runs the same script on every PR to `main` in
  `--mode check`, because on an ordinary PR the pin names the *last* release and
  the prepare-mode tag check would fail every PR). Never move a tag to
  repair a failed check - cut a new patch version. The full checklist is in
  `docs/IMPROVEMENT_PLAN.md` under "Release checklist".

---

## 5. Security model (the one paragraph that matters)

External access is via **Cloudflare Zero Trust (Access)** with an email policy.
Since v2.5.0 the app **independently verifies** the `Cf-Access-Jwt-Assertion`
JWT — signature against Cloudflare's published keys, issuer, audience, expiry —
and requires the email to be in `AUTH_ALLOWED_EMAILS`. A verified email that is
not on the allowlist is denied. Cloudflare is the outer gate; the app is the
inner one, because a LAN host or another container on the Docker network never
goes through Cloudflare at all. Since Slice 5 (2026-11-07) writes also carry
an origin check (`utils/origin_guard.py`, installed after the auth guard):
a browser write whose `Origin`/`Referer` host is not this app's host gets a
403, so a malicious page in another tab cannot trigger downloads with the
user's Cloudflare session. Headerless writes (curl, scripts) are allowed
through to auth - do not "tighten" that without a plan for non-browser
clients, and do not compare schemes (Cloudflare terminates TLS, so the
browser says https while the app sees http). The CSP header is report-only
by design; never flip it to enforcing while templates use inline scripts.

**There is one user and there will only ever be one.** No accounts, no sign-up,
no roster, no roles. The allowlist is the entire permission model.

Mechanics you must not break:

- The guard is a **`before_request` hook** (`utils/web_auth.py`), not a
  per-route decorator, so a new route cannot be born unauthenticated. Anything
  that must stay public is named in `PUBLIC_ENDPOINTS`, and two tests enforce
  that: one enumerates `url_map`, one forbids data routes in the allowlist.
- `utils/auth.py` never imports Flask and never echoes the attempted email into
  an exception or log line.
- `AUTH_*` live in environment variables only, never `settings.yml`: an
  allowlist in a file the UI can rewrite is not a security control.
- `/healthz` must stay public or every container reports unhealthy.
- A **partial** `AUTH_*` config fails *closed* (starts, refuses everything,
  says why). It must never fall back to open.

Still true, and still worth knowing:

- `/app-config` returns the **Plex token and Prowlarr/SABnzbd API keys** — now
  behind auth, so it matters that the auth cannot be bypassed.
- `/run` used to spawn a subprocess from a client-supplied string. Since
  Slice 3 (2026-11-07) it accepts only a named action with typed fields and
  `build_job()` constructs the argv server-side; nothing a client sends is
  split or shell-interpreted. `/download-selected` and the mismatch
  redownload build argv lists the same way (their response "commands" are
  display strings only — never execute those). All three claim the one job
  slot synchronously via `utils/webui_jobs.JobManager` before any thread
  starts: do not reintroduce a `running` check that the worker sets later,
  that was the duplicate-launch race. Job state is process-local, which is
  why Slice 6 (WSGI workers) is gated on it. Both routes remain
  authenticated writes: treat payloads as untrusted and keep validation in
  `build_job()`.
- `scripts/cleanup_utility.py` **deletes library folders and rewrites grabber
  state**. It is no longer a test, no longer at the repo root, no longer in the
  image, and requires `--confirm`. Keep it that way: `tests/test_build_hygiene.py`
  asserts it cannot be copied in.

---

## 6. Priority list and status

Historical security/design review findings and their recorded dispositions.
The detailed issues below remain useful context, but their statuses are not a
release ledger; verify current behaviour in code and tests. New follow-up work is
prioritised in [`docs/IMPROVEMENT_PLAN.md`](docs/IMPROVEMENT_PLAN.md).

| # | Issue | Status |
|---|---|---|
| 1 | No authentication on the WebUI; secrets exposed | **DONE (v2.5.0)** — Cloudflare Access JWT verification |
| 2 | No `.dockerignore`; 6 stale `.backup` files tracked in git | **DONE (v2.5.0)** — `.dockerignore` added (context 1.23 GB → 754 MB); backups removed and gitignored |
| 3 | Container runs as root (no `USER` directive) | **DONE (v2.5.0)** — drops to PUID:PGID (99:100) via setpriv, checked at startup |
| 4 | `test_cleanup_utility.py` deletes files and ships in the image | **DONE (v2.5.0)** — moved to `scripts/`, `--confirm` gate, not in the image |
| 5 | No schema validation for `settings.yml` | **DONE (v2.5.0)** — `utils/settings_schema.py`, reported at startup and in the UI |
| 6 | Plex library root is never checked | **DONE (v2.5.0)** — `utils/plexcheck.py`, surfaced at startup and in `/diagnostics` |
| 7 | Release notes written by hand | **DONE (v2.5.0)** — generated from merged PRs, categorised by label |
| 8 | No container integration test in CI | **DONE (v2.5.0)** — `container-smoke.yml` builds, runs, and asserts non-root; green on PR #11 |
| + | Cloudflare Access JWT verification (`AUTH_*`) | **DONE (v2.5.0)** |

### Findings added during the documentation review

These were not in the original list. They are cheap, concrete and in the same
risk family as item 1.

| # | Finding | Where |
|---|---|---|
| A | **`POST /app-config` silently destroys config.** It rebuilt `settings.yml` from a fixed dict of six sections, so any key not in that dict — `grabber.*`, `uiadminpassword`, anything added later — was dropped on every save from the UI. **Fixed:** `update_settings()` deep-merges, and `_build_app_config_updates()` allowlists what the form may write. | `motogp_webui.py`, `utils/config_loader.py` |
| B | `Access-Control-Allow-Origin: *` on every response, including `/app-config`. **Fixed:** wildcard removed (the UI is same-origin); `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` added. | `motogp_webui.py` |
| C | `uiadminpassword` was plaintext, `==`-compared, invisible to `config_loader`, and gated only the client. **Fixed:** route and key removed; identity is server-side. | — |
| D | `/logs/stream` and `/logs/recent` expose log lines; URLs containing API keys have appeared in these logs before. **Mitigated:** both now require a verified identity. Logs are still log lines — do not add secrets to them. | `motogp_webui.py` |
| E | `grabber.allowed_groups`, `grabber.required_quality`, `grabber.search_delay_hours` were read from config but unreachable and undocumented. **Fixed:** documented in `settings.example.yaml`, with getters; still YAML/env only. | `utils/config_loader.py` |
| J1 | **Two settings stores shadow each other for `max_grabs_per_run`.** The UI displayed the JSON copy while the grabber read `settings.yml`. **Fixed:** `settings.yml` owns `grabber.*`; `webui_settings.json` holds sessions only; `/settings` reads `max_grabs_per_run` from `config_loader` at presentation time; a legacy JSON value is migrated once into `settings.yml` (settings.yml wins on disagreement); a corrupt or missing JSON file logs a warning naming the path and falls back to defaults. | `motogp_webui.py`, `utils/config_loader.py`, `tests/test_settings_store.py` |
| J2 | **The permission hint printed container paths as host-runnable chown commands.** **Fixed:** `utils/permissions.hint()` names the container path and tells the user to chown whatever they mounted on the host; `MOTOGPARR_HOST_MAP` prints the exact host command when known; CLI and entrypoint print identical text. | `utils/permissions.py`, `utils/config_loader.py`, `unraid/motogparr.xml`, `tests/test_permissions.py`, `README.md` |
| J3 | **Unraid TemplateURL floated on `main`, so "Apply" could silently replace a user's edited template.** **Decision (b):** pinned `TemplateURL` to the release tag (`.../v2.5.0/unraid/motogparr.xml`) and added a rot test (`tests/test_unraid_template.py`) asserting the pinned tag equals the newest tag in the repo. Trade-off: old installs don't receive template updates until the user re-points it, so **"bump TemplateURL"** is a release-checklist item on every release. | `unraid/motogparr.xml`, `tests/test_unraid_template.py` |
| J5 | **Release notes were generated as an artifact but never reached the repo.** **Fixed:** the release workflow now commits `RELEASE_NOTES_vX.Y.Z.md` to a `release-notes/vX.Y.Z` branch and opens (or updates) a pull request to `main`, so the notes reach the repository without pushing to a protected branch directly. Adds `pull-requests: write`; idempotent on re-runs. Verification requires a real tag push (maintainer's action) — stated in the PR rather than claimed. | `.github/workflows/release.yml`, `tests/test_workflows.py` |
| J4 | **GitHub Actions Node 20 deprecation.** Every run warned \"Node.js 20 is deprecated… forced to run on Node.js 24\" for 7 actions. **Fixed:** bumped each to its node24 major — `actions/checkout@v5`, `actions/setup-python@v6`, `docker/setup-buildx-action@v4`, `docker/build-push-action@v7`, `docker/login-action@v4`, `docker/metadata-action@v6`, `actions/upload-artifact@v6` — keeping permissions minimal (`contents: read`, `packages: write`, plus `pull-requests: write` for release). No version-pinning test (rots); existing workflow invariant tests cover tag patterns, non-root and auth assertions. | `.github/workflows/*.yml`, `tests/test_workflows.py` |
| L1 | **PRs to main could be merged with no labels, producing a single \"Other changes\" line in release notes.** **Fixed:** cheap CI check `.github/workflows/pr-label-check.yml` fails when a PR to main has zero labels (exempts `dependabot[bot]`), printing the categorisation map. Pure shell + `jq`, no checkout or JS action, so no Node deprecation surface. | `.github/workflows/pr-label-check.yml`, `tests/test_workflows.py` |

### Issue #25 — artwork bloat (v2.5.4)

The 23 venue placeholders for each of three classes were byte-identical copies
of two master JPEGs: 69 posters and 69 fanarts. They are now **relative symlinks**
to `assets/motogpPoster.jpg` and `assets/motogpFanart.jpg`. The calendar still
names each venue file, so custom artwork can replace any link without changing
calendar numbering or runtime code. The unused `assets/external/2025/` tree
(314 files, ~535 MiB) was removed after confirming it has no users. Together
these changes shrink `assets/` from ~753 MiB to ~4 MiB in the checkout/build
context. Tests cover every calendar reference and check that the sorter still
writes *real* images to the library; CI must also verify the links inside the
built image (no Docker daemon in the sandbox).

**Do not write through a venue symlink** when adding custom art: that overwrites
the shared master for every venue. Unlink it first, then install a real JPEG at
the same filename.

### Issue #30 — calendar popup per-day session times (v2.5.6)

`/calendar` defaults to the current season (`default_calendar_year()`) rather
than hard-coded 2025, and `build_round_schedule()` emits per-day session blocks
with exact UTC timestamps derived from track-local date + `local` + `utc_offset`
(preserving midnight-crossing flyaway times such as Phillip Island Friday morning
landing on Thursday UTC, and reading directly from `round["sessions"][day]` so
the 2027 format — Friday FP2 + Sprint Qualifying, Saturday FP3, no Sunday
warm-up — works from JSON data alone). In the modal (`templates/calendar.html`),
clicking a date opens that specific day's timetable converted to the browser's
local timezone (with `Fri`/`Sat`/`Sun` switcher pills), replaces `"days to go"`
on a live weekend with **`NOW`** (when a session is underway) or the **next
session's start time**, and keeps past rounds focused solely on library status
and missing-session downloads.

### Issue #51 — home page cards

The home tab was a welcome card listing the other tabs. It now answers the
questions the tab exists for: what did the machine last add, what is it about
to do, and is it still alive. A weekend summary reports progress the way the
Download tab counts sessions, not by files.

**`utils/home_summary.py` is pure and holds the rules.** It takes loaded data
and an explicit `now`, returns dicts, and does no I/O, so every rule is tested
without a running app or a fake HTTP layer. `motogp_webui.index()` is the only
caller and stays a wiring layer.

- **Grab mode is recorded, not inferred.** `GrabberState.mark_grabbed()` takes
  `mode` and stores it; `GRAB_MODE_SCHEDULED`/`GRAB_MODE_BACKFILL` live in
  `motogp_common` so the grabber and the home page cannot disagree about the
  vocabulary. The hero card shows only `scheduled` grabs. **Entries written
  before this field existed carry no mode and are skipped** — a timestamp that
  looks like a race weekend is not evidence of which command made the grab, and
  the user chose an honest empty state over a plausible one. Backfills never
  count: a correction run is not the machine keeping pace. This is the same
  refusal-to-guess rule as an ambiguous element number.
- **Presence in the library is the durable proof of "added".** The activity
  history is capped at ten events, so it can timestamp an addition but must
  never be the record of one. The card reads the library scan, then prefers a
  COMPLETE activity event for the timestamp and says which time it is showing
  ("Added to Plex" vs "Grabbed").
- **The weekend window and session times come from the grabber.** `find_weekend_round()`
  imports `WINDOW_DAYS_BEFORE_WEEKEND`/`WINDOW_DAYS_AFTER_RACE` and
  `next_session()` calls `get_session_times()`, so "searchable from" is the
  exact instant the scheduled search fires rather than a second implementation
  that will drift. If the grabber's window moves, the card moves with it.
- **One televised block, one name.** Q1 and Q2 share the timetable block and
  the same start and end, so the card names the block "MotoGP Qualifying"
  instead of whichever part sorted first. Progress counts them separately,
  matching the Download tab's unit — the number must agree with the tab that
  lets you download the missing one.
- **Scheduler staleness is derived from the scheduler's own intervals.**
  `scheduler_health()` reads `CONFIG` from `motogp_scheduler` and calls the
  scheduler stale at twice the applicable interval plus an hour of grace: wide
  enough that quiet hours (01–06, 120 min) never cry wolf, tight enough that a
  dead scheduler is visible the same day.
- **Static by decision.** The page is rendered on load; the only browser
  fetches are one-shot health chips hitting routes that already existed
  (`/test-connections`, `/api/sabnzbd/queue`). The deeper Plex library check
  stays in `/diagnostics` because its 10-second timeouts have no place in a
  page render. `/api/sabnzbd` joined the service-worker bypass list: a cached
  answer about whether SABnzbd is alive is a wrong answer.
- **A torn state file renders an empty page, not a 500.** The grabber writes
  `grabber_state.json` without a lock; `_load_grabber_state()` logs a warning
  naming the path and returns `{}`, and a test pins the corrupt-file path.

### Issue #33 — 2027 race weekend format (code complete; live verification outstanding)

The 2027 format (Friday FP1/FP2/Sprint Qualifying, Saturday FP3/Q1/Q2/Sprint, no
Sunday warm-up) is data, not code: `weekend_formats` in the calendar, with each
round carrying `weekend_format` and `motogp_sessions`, and `round_runs_session()`
gating every search. The code work is now done on this branch; what remains is
live confirmation (see Outstanding).

**Done**

- **Session numbering reworked.** `SESSION_ELEMENTS` allocates in blocks of ten
  instead of using the enum value. The old table was dense (1-24, full), so FP3
  and Sprint Qualifying had to be appended as 23/24 — *after* the Race — and a
  2027 round listed out of order in every folder, title and CLI listing. Ordering
  is now a property of the identifier, not something patched in the UI.
- **Cutover is immediate** (`LEGACY_ELEMENT_SEASON = 2025`): 2025/2026 media is
  deleted and re-fetched under the new scheme, so one numbering applies from 2026
  onwards. `SESSION_ELEMENTS_LEGACY` is frozen and read-only. A number that means
  different sessions in the two schemes is refused rather than guessed, and
  `find_legacy_folders()` makes the library scan warn about leftovers it cannot
  read instead of silently re-downloading them.
- **Short canonical names.** `SessionType.full_short_name` gives `MotoGP FP2`
  rather than `MotoGP Free Practice 2`; it is used for the on-disk folder name
  and written to Plex as the item title. `utils/plexlabels.py` writes `title` and
  `titleSort` together (title alone would clump every same-named session), and
  reuses the `YYRREE` prefix as the sort key. On-disk and Plex names are kept
  identical so a library re-scan is a no-op rather than a silent revert.
- **`get_session_times()` reads the day from the calendar.** It searches the
  round's own `sessions` day blocks for the key and only falls back to
  `CALENDAR_KEY_TO_DAY` for legacy calendars. A 2027 Friday FP2 now gets a real
  start and end; before this it found nothing in the Saturday block and the
  grabber skipped the session at DEBUG level. It also anchors on
  `local` + `utc_offset` (like the WebUI), so a flyaway Friday morning lands on
  Thursday UTC instead of 24 hours late. `CALENDAR_DAYS` is one shared tuple for
  the grabber, the WebUI and the validator, so Thursday is not rejected in one
  place and supported in another.
- **Refuse-to-guess release classification.** `classify_session()` returns
  `UNKNOWN` for an ambiguous token (`SQ`/`SPQ` without an alias, a bare `FP`) or
  a title with no session token; it no longer falls through to the Race.
  `filter_releases()` rejects an `UNKNOWN` title with a warning naming the full
  title, so it cannot be filed as the Grand Prix. `classify_release()` explains
  the refusal, and `python -m motogp_common --report-unknown --season 2027` is
  the dry-run operator tool. The 2027 calendar carries `release_aliases`
  (`sq`/`spq` → `sprint_qualifying`), so the vocabulary is data, not a token
  table. A 2027 `Practice` title resolves to FP2 from the round's own session
  list; the same title in 2026 is still `MOTOGP_PRACTICE`.
- **The validator can see format mistakes.** `utils/validate_calendar.py` now
  cross-checks `weekend_format` ↔ `motogp_sessions` ↔ the sessions block, in
  both directions and per day. It rejects a typo in `weekend_format`, a stale
  `motogp_sessions` list (e.g. `practice`/`warmup` in 2027), a session declared
  with no times, and a MotoGP key in a day block the format does not run. It
  accepts Thursday, and validates `release_aliases`. `calendar-check.yml` runs it
  on every calendar PR.
- **`config/motogp_2027.json` exists.** 22 rounds, the 2027 format, the
  provisional timetable and the release aliases. `utils/build_calendar_2027.py`
  regenerates it; Adelaide assets were added. The 2026 builder's stale comment
  (which called Sprint Qualifying a Saturday session) is fixed.
- **Release engineering.** The release-notes step no longer swallows
  `gh pr create`/`gh pr edit` failures, the branch push is not allowed to fail
  silently, and the step verifies the pull request exists. A green job with no
  release-notes PR is now impossible to miss.

**Outstanding**

- **Live Plex confirmation.** `plex_labels.supports_title_edits` is still
  unverified against a real "Other Videos" section, and no human has looked at
  the resulting Plex titles for a real 2027 weekend. This cannot be done from the
  sandbox; it is the first thing to check after deploy.
- **The 2027 session times are provisional.** They follow the 2027 weekend shape
  and the standard slot times, not a published circuit timetable. The calendar
  validates and renders, but replace the times when the official timetable is
  released.
- **The 2027 calendar is provisional.** Buenos Aires and Adelaide require FIM
  homologation and COTA is subject to contract; re-check the round list before
  the season starts.
- **`find_legacy_folders()` cannot detect a stale number that is valid now.**
  This is a documented limit, not a bug to "improve" into a converter; the
  pre-cutover media was removed rather than migrated.
- **The enum is nearly vestigial for naming.** `int(SessionType.X)` is only a
  fallback inside `element_number()`. Any code deriving a folder number or
  episode key from `int(session_type)` is a bug.

---

### 6b. Release mismatches (2026-11-07)

`filter_releases()` is deterministic: a release the rules wrongly accept is
picked again on every retry, so "delete it and download it again" could not fix
a bad match. This is now a first-class correction path, not a code change:

- **Record** — `utils/release_mismatches.py` owns
  `/config/release_mismatches.json` (sidecar lock, atomic replace, reload while
  held). Entries are session-scoped by default and globally scoped on request.
  `filter_releases(excluded_titles=...)` drops them before scoring, so the next
  `backfill --force` selects the next candidate. The grabber reads the store
  itself (`_blocked_titles`), so a scheduled run is covered too.
- **Explain** — `motogp_common.explain_release()` runs the *real* filter over one
  title and returns each check with the value that matched. It shares the
  implementation with `filter_releases()` (`_filter_releases_impl`), because a
  second description of the rules would drift and then explain the wrong thing.
  This is the material for a pattern fix: the dialog's "Copy report" emits it.
- **Replace** — the dialog moves the existing folder to
  `<library>/.mismatched/<season>/` (dot-prefixed so Plex ignores it) and starts
  the ordinary `backfill --season S --round R --sessions E<element> --force`
  job. There is deliberately no second download implementation, and no delete.
- **Sort safety** — `motogp_sort._clear_existing_session_folder()` moves an
  existing session folder aside before a replacement is moved in. Without it
  `shutil.move(src, dst)` nests the new folder inside the old one and Plex keeps
  showing the wrong file with nothing visibly failing.

`tests/test_release_mismatch.py` covers the exclusion, its scope and
normalisation, concurrent writers, the quarantine guards (prefix boundary,
symlink refusal, stay-inside-the-library check), the sorter behaviour, the
explanation, and the rendered dialog/Download-tab badge through node.

**Known limit:** the block list is keyed on the indexer's title. If the same
wrong content is re-uploaded under a new name, it is a new candidate again -
that is a pattern problem, which is why the dialog shows the evidence.

**Trap:** `/release-mismatch*` is in `sw.js`'s network-bypass list. Those
responses must never come from the service-worker cache - a cached `blocked: []`
makes the dialog claim a release would still be picked when it would not.
Templates are cached by Jinja with debug off, so restart the app after editing
them (or you will debug HTML that is no longer on disk).

## 7. Current improvement plan

The pre-v2.5 implementation sequence below was completed over several releases
and is no longer an active plan. Its incident notes and rationale remain above.
For new work, follow [`docs/IMPROVEMENT_PLAN.md`](docs/IMPROVEMENT_PLAN.md): it
starts with the documentation refresh completed alongside the review, then
proposes small, dependency-aware slices for state integrity, WebUI job execution,
integration coverage and later operational hardening.

Before starting a slice, confirm it is still relevant against the current code,
add or update regression tests, and keep each PR focused on one independently
reviewable behaviour change. Update this plan and the architecture document in
the same PR when implementation changes their claims.

## 7a. Incidents

Record security incidents here with dates and required actions, so a later
session does not re-discover them or assume they were handled.

### 2026-11-05 — live Prowlarr API key committed to a public repository

`test_prowlarr.py` at the repository root contained a working Prowlarr URL and
API key:

```python
PROWLARR_URL = "http://192.168.1.241:9696"
PROWLARR_API_KEY = "<redacted>"
```

**The repository is public** (`gh repo view --json isPrivate` → `false`), and the
file arrived in the `cac8dc5` merge, so the key was world-readable from then
until it was deleted. It is still recoverable from git history
(`git log --all -S<key>`), and this branch has been pushed, so deleting the file
does not un-publish it.

**Required action (cannot be done from the sandbox — no Prowlarr access):**

1. Rotate the Prowlarr API key: Prowlarr → Settings → General → API Key → Reset.
2. Update `PROWLARR_API_KEY` wherever it is set (Unraid template, `settings.yml`).
3. Rotate any other key that was stored alongside it if they share a values file.

The file was deleted in v2.5.0 and `tests/test_build_hygiene.py` now scans the
working tree for credential literals. It cannot scan history; if a key is ever
suspected, assume it is compromised and rotate rather than rewriting history —
rewriting a public repo does not recall clones.

The same file also leaked the LAN address `192.168.1.241`; treat the layout of
your network as public knowledge from that date.

## 8. Known traps (learned the hard way)

- The WebUI, scheduler, grabber and watcher are separate processes sharing
  `/config` JSON state. Since the combined Slices 1+2 change (2026-11-07) the
  four shared stores (`activity_events.json`, `grabber_state.json`,
  `watcher_state.json`, `webui_settings.json`, plus `release_mismatches.json`)
  all persist through **`utils/json_store.py`**: a sidecar lock held across the
  whole load-mutate-commit, a unique temp file, and atomic replacement, so a
  reader now sees the old file or the new one, never a half-written one. Do
  not write a shared JSON store with a bare `open(..., "w")` again — use
  `update_json` for mutations, or `three_way_merge` on save for a long-lived
  in-memory state object (that is how the grabber and watcher keep another
  process's committed entries without resurrecting their own deletions).
  Corrupt JSON is logged at ERROR naming the path before defaults are used;
  keep it that way. `discord_rate_limit.json` is the deliberate exception
  (one writer, disposable).
- `discover_seasons()` reads `/config`, not the image, so a season only appears
  in the UI once the entrypoint has copied the calendar into `/config`.
- `WEBUI_CONFIG_FILE` (`webui_settings.json`) holds desired sessions only.
  `max_grabs_per_run` is a `grabber.*` setting owned by `settings.yml` and read
  by the grabber via `config_loader.get_max_grabs_per_run()`; the UI reads it
  from there at presentation time. A legacy value left in the JSON is migrated
  once on first load, and `settings.yml` wins on disagreement — never assume the
  two stores agree.
- `grab_stats.py` and friends derive stats from `grabber_state.json`; deleting
  that file (as the cleanup utility does) rewrites the app's history.
- Flask dev server is used in production (`app.run`). Fine on a LAN behind
  Cloudflare, but do not increase worker count or add threading without
  revisiting the shared-state assumption above.
- **A failure must say which stage failed.** `grabber_state.json`'s `failed`
  entries and the watcher's `FailedItem` carry a `stage`, `hint` and
  `actionable` flag from `utils/failure_reasons.py`. "No results" used to mean
  a 401, an unreachable Prowlarr *and* an empty search; the difference decides
  whether the user waits or fixes something. Every stored reason passes through
  `failure_reasons.sanitise()` first - a `requests` exception carries the full
  URL, and Prowlarr/SABnzbd put their key in the query string (see §7a). Do not
  add a fourth ad-hoc reason string; add a stage. Old entries have no stage and
  are shown as unknown rather than labelled by guesswork.
- **The library scan is measured, not optimised.** `/diagnostics` reports
  `scan_cost` from `utils/scan_metrics.py` (median/p95/max per walk, plus any
  scan over two seconds). Do not add an index, a database or a caching change on
  the strength of a hunch: the numbers exist so the decision can be made on
  evidence from a real library. Instrumentation wrappers must return the scanned
  value untouched.
- **Liveness is not progress.** The Docker probe only proves the WebUI answers
  `/healthz`. The scheduler writes `last_loop` on every iteration and the
  watcher writes `last_scan` after every scan to `background_health.json`
  (`utils/background_health.py`); the home page and `/diagnostics` judge those
  against the scheduler's own intervals. Keep `/healthz` free of file reads and
  network calls, and keep a stale heartbeat a *warning* rather than a Docker
  failure - a slow Prowlarr search must not restart the container. See
  `docs/ARCHITECTURE.md` §8b.
