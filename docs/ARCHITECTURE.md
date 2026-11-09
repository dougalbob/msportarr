# MotoGParr — Architecture

Agent-facing architecture reference, last refreshed against repository release
**v2.9.0** on 2026-11-07. It describes the current implementation, not a promise
that every historic note or release-specific detail remains accurate. When code
and this document disagree, verify the code and tests, then update this document
in the same change.

Intended audience: an agent about to change something. If you are only installing
the app, read `README.md` instead.

---

## 1. Shape of the system

One Docker image, one container, **two long-lived processes** supervised by a
bash entrypoint, plus short-lived child processes the WebUI spawns on demand.

```
entrypoint.sh (bash, PID 1)                       ← supervises, never exits
├── motogp_webui.py        Flask on 0.0.0.0:5679  ← restarted up to 5x if it dies
└── motogp_scheduler.py    loop, no port         ← NOT restarted; its death stops the container
        └── motogp_watcher.py (thread)
                └── motogp_sort.py, motogp_plex_tagger.py   (subprocess per download)
```

The WebUI additionally spawns `motogp_grabber.py` and `motogp_overlay_utility.py`
as children of itself. Everything talks to everything else through **files in
`/config`** — there is no queue, no broker, no database.

Why the asymmetry: a dead WebUI behind a healthy container was a real outage
(blank page, scheduler happily running). So the WebUI is supervised and probed by
`HEALTHCHECK`. A dead *scheduler* means the container has stopped doing its job,
so stopping is honest.

### 1a. The series registry

MotoGParr began as a MotoGP-only program and grew three classes inside it. Adding
Formula 1 is the same move one level up, so the vocabulary a championship needs is
now **data in `series/`**, not code in the spine.

```
series/
|-- __init__.py   registry: register / get_series / all_series / runtime;
|                 imports every series module, which is what registers them
|-- base.py       Series, ClassSpec, SessionPatternSpec declarations,
|                 SeriesRuntime (materialisation), the numbering rules
|-- motogp.py     MotoGP's vocabulary: enums, element tables, patterns,
|                 calendar-key maps, asset dirs, session defaults
`-- f1.py         Formula 1's, ported from f1arr
```

Two series are registered: `motogp` (the default) and `f1`. Registration order is
the order of the imports in `series/__init__.py`, and `all_series()` returns it, so
a UI listing series does not have its order depend on which module happened to be
imported first.

Each series module calls `register()` at import time. `motogp_common.py` imports
`series` and binds **MotoGP's** vocabulary - it is still the MotoGP spine, and F1
does not read through it yet:

```python
_MGP = _series_runtime("motogp")
SERIES_KEY = _MGP.key
RaceClass   = _MGP.race_class
SessionType = _MGP.session_type
SESSION_ELEMENTS = _MGP.session_elements      # and every other table
```

**A series is data plus a vocabulary, never a code path.** Adding one means
writing one module in `series/` and nothing else. If that stops being true, the
abstraction is wrong - fix the abstraction rather than special-casing a series
inside shared code, because the second special case is how a hybrid turns back
into two apps.

What the registry owns, moved out of `motogp_common.py`: the `RaceClass` and
`SessionType` enums, both element tables, the display/short/class maps, the core
and feature-race sets, the desired-session defaults, `SESSION_PATTERNS`, all four
calendar-key maps, `CLASS_ASSET_DIRS`, and the numbering functions
(`element_number`, `legacy_element_number`, `_element_tables`,
`get_session_from_element`, `element_is_ambiguous`, `build_prefix`,
`parse_prefix`, `find_legacy_folders`).

What deliberately stayed in the spine: every algorithm - `classify_session`,
`detect_race_class`, `_remap_session_for_class`, `filter_releases`,
`explain_release`, `get_session_times`, `round_runs_session`,
`validate_calendar`. They read the vocabulary through the module-level names they
always used, so they are unchanged, and they will be parameterised once there is
a second series to parameterise against. Moving an algorithm before its second
caller exists produces an abstraction shaped by one example.

**Two invariants the registry enforces, both tested in
`tests/test_series_registry.py`:**

1. **Element numbers are permanent.** They are baked into folder names, file
   names and Plex identity, so renumbering a live season renames files and Plex
   treats them as new items. They are allocated in blocks of ten so a new session
   takes a free number instead of being appended after the Race, and the legacy
   table is read-only.
2. **Nothing is guessed.** A number the two schemes disagree about returns
   `None` rather than a best guess. `SeriesRuntime.state_file()` namespaces every
   `/config` file to its series (`grabber_state.motogp.json`) because two series
   writing `grabber_state.<series>.json` into one volume would overwrite each
   other's
   entries within a day.

`tests/test_series_registry.py` asserts the registry's tables are the *same
objects* the spine binds, that every element number round-trips through
`build_prefix`/`parse_prefix` for 2025/2026/2027, and that the numbering is
chronological for both the 2026 and 2027 weekend formats.
**Series identity travels with the enum.** `series_key` and `feature_class`
are attributes of the materialised `SessionType` class, so shared code that
receives a session from either series can ask which series it came from and
which class its weekend format governs. Both used to be answered by naming
MotoGP: `round_runs_session()` compared the member's class against
`RaceClass.MOTOGP`, which is never equal to a *different* enum class - so every
F1 session skipped the weekend-format gate entirely and looked runnable on every
round. The feature class is derived from declaration order (the weekend running
order, so the feature class is declared last) rather than named.

**Three things a second series forced into shared code**, all of them removals
of a MotoGP-shaped assumption rather than additions:

| Was | Now |
|---|---|
| `round_runs_session()` compared against `RaceClass.MOTOGP` | compares against `type(session_type).feature_class()` |
| A session with no weekend-format name counted as "not run" | counts as "not described by the format", so it runs - which is what keeps F1's support shows searchable |
| `get_round_motogp_sessions()` read a hardcoded key | `round_session_list(..., series=)` reads `<series>_sessions`; the old name remains as a default-series shim |

`tests/test_f1_series.py` covers F1: that it is registered and is *not* the
default, that its tables are a faithful port of f1arr's, and two constraints that
are easy to break and silent when broken -

* **an element number must fit the two-digit prefix.** `_PREFIX_RE` is
  `(\d{2})R(\d{2})E(\d{2})`, so `E100` parses as `E10` and names the wrong
  session. F1's first draft ran to 110 and did exactly that.
* **a new number must not collide with a superseded scheme unless it means the
  same session.** `session_members` is f1arr's original dense table - it wrote
  the enum value into the folder name - so `E10` was Sprint Qualifying. Four of
  the first draft's numbers named a *different* session under the new scheme,
  which would have misread an old folder with nothing reporting it. 4-29 is now
  deliberately unresolvable, and `test_no_old_number_is_silently_misread` checks
  every number 1-99.

**Calendars are per series.** `config/<calendar_prefix>_<year>.json` -
`motogp_2026.json`, `f1_2026.json`. The prefixes are disjoint by contract (the
validator refuses overlapping ones), so neither series' refresh can touch the
other's files. Both series use one schema; see `docs/CALENDAR_CHANGES.md` §2 for
the field list, the `<series>_sessions` key and the two F1 weekend formats.
`utils/validate_calendar.py` names no championship - it discovers the files from
the declarations and reads keys, format names and artwork directories from them,
so a new series is validated by existing.

**Artwork is per series and per class**, declared in `asset_dirs` and resolved
through the registry. MotoGP's venue placeholders are relative symlinks to two
shared masters (issue #25 shrank the image from 753 MiB to ~4 MiB); F1's are
real per-venue JPEGs because they genuinely differ. The two schemes are
deliberate opposites and both are tested: `tests/test_artwork_assets.py` asserts
MotoGP's links are links *and* that F1's are not.

The plan for combining this app with F1arr is in
`docs/HYBRID_FEASIBILITY.md`.

### Components

| File | Role |
|---|---|
| `entrypoint.sh` | Banner/version, calendar sync into `/config`, starter `settings.yml`, config + path validation, path severity gate, start and supervise both processes |
| `motogp_webui.py` | Flask UI + JSON API; route inventory in §5. Spawns grabber/overlay |
| `motogp_scheduler.py` | Sleep loop. Decides whether it is a race weekend and how often to poll |
| `motogp_watcher.py` | Watches the downloads dir, waits for folder stability, drives sort → tag → Discord |
| `motogp_grabber.py` | Prowlarr search, release filtering, send to SABnzbd. `backfill` / `scheduled` / `status` / `clear` |
| `motogp_sort.py` | Moves a completed download into `Round NN Venue/` structure, writes artwork |
| `motogp_plex_tagger.py` | Labels, genres, collections in Plex. Uses **exact path matching** |
| `motogp_plex_tagger.py` + `motogp_overlay_utility.py` | Artwork overlay/poster generation |
| `motogp_common.py` | Shared spine: logging, calendar access, release parsing/filtering, prefix build/parse. **Binds its enums and tables from `series/`; declares none of them** (§1a) |
| `series/` | The per-series vocabulary: one declaration module per championship plus the registry that materialises them (§1a) |
| `activity_emitter.py` | JSON event bus for the Activity tab (`ActivityStage` enum); every emit is one locked transaction via `utils/json_store.py` (§4) |
| `utils/json_store.py` | The one persistence contract for shared `/config` JSON: sidecar lock held across load-mutate-commit, unique temp file, atomic replace, loud corruption (§4) |
| `utils/webui_jobs.py` | The WebUI's one-at-a-time background job: synchronous slot reservation, bounded output, exit code, timeout. Process-local by design — the reason Slice 6 cannot add WSGI workers yet |
| `utils/origin_guard.py` | Browser-write defence-in-depth: write methods need an `Origin`/`Referer` host matching the request host (headerless non-browser clients pass to auth). CSP is report-only in `motogp_webui.add_security_headers` |
| `grab_stats.py` | Stats derived from `grabber_state.<series>.json` |
| `motogp_discord.py` | Webhook notifications, has its own rate-limit file |
| `utils/config_loader.py` | **`ENV_SPEC`**, settings precedence, validation, startup report |
| `utils/calendar_sync.py` | Same-season check + safe replacement of `/config` calendars |
| `utils/validate_calendar.py` | CLI calendar/library checker |
| `utils/healthcheck.py` | stdlib-only `HEALTHCHECK` probe for `/healthz` |
| `utils/background_health.py` | Scheduler-loop and watcher-thread progress records plus the staleness rule (§8b). Reported to the user; deliberately not part of the Docker probe |
| `utils/state_files.py` | Namespaced `/config` filenames per series, and the one-time migration of the pre-hybrid names (§4) |
| `utils/failure_reasons.py` | The closed vocabulary for "why did that session not download", the wait-vs-fix distinction, and the redaction every stored reason passes through (§8c) |
| `utils/scan_metrics.py` | In-process timing of the library walks, surfaced in `/diagnostics`. Measurement only - no index, no cache change, nothing persisted (§8d) |
| `utils/settings_schema.py` | Permitted shape of `settings.yml`; unknown/wrong-typed keys |
| `utils/plexcheck.py` | Is Plex actually scanning the library folder? |
| `utils/permissions.py` | Can PUID:PGID write where it must? |
| `utils/release_mismatches.py` | Marked-wrong releases: the block list, the atomic store and the library quarantine move |

---

## 2. The data pipeline

```
config/<series>_<year>.json
    │  rounds: number, venue, dates, classes, weekend format
    ▼
grabber ── Prowlarr /api/v1/search ──► filter_releases() ──► SABnzbd addurl
    │                                          │
    │                                          └─ desired sessions from
    │                                   webui_settings.<series>.json
    ▼
/downloads/complete/motogp/<folder>
    │  watcher: folder must be stable 15s, checked every 10s
    ▼
motogp_sort.py ──► /media/.../2026/Round 07 France/26R07E21 MotoGP Race/
    │                  └─ folder + file naming carries season/round/episode
    ▼
motogp_plex_tagger.py ── Plex API ──► labels/genres/collections
    │                       ▲
    │                       └─ needs path translation (§3)
    ▼
motogp_discord.py ──► webhook
```

### Naming is identity

`26R07E21 MotoGP Race` — season, round, session-as-episode. The round number is
simultaneously a sort key and the episode's identity in Plex, so renaming a round
renames files and Plex sees new items. See `docs/CALENDAR_CHANGES.md`; do not
invent new naming schemes.

### Two clocks

- **Race weekend** (detected from calendar dates): poll every 60 min.
- **Off-weekend**: every 360 min. Quiet hours: 120 min.

The watcher polls independently every 10 s and only processes a folder once it
has been unchanged for 15 s.

---

## 3. Configuration

Three layers, resolved per key:

```
environment variable   >   settings.yml   >   built-in default
```

An env var that is set but **empty** counts as unset. That is deliberate: the
Unraid template ships blank path fields so `settings.yml` stays authoritative.

`ENV_SPEC` in `utils/config_loader.py` is the registry. `--env-help` renders it;
the Unraid template test cross-checks it. Anything not in `ENV_SPEC` is not read.

Three env vars are read by `entrypoint.sh`, not Python, and are listed separately
(`ENTRYPOINT_ENV`): `MOTOGPARR_CALENDAR_UPDATE`, `MOTOGPARR_SKIP_PATH_CHECK`,
plus `CONFIG_DIR`. `MOTOGPARR_VERSION` is informational.

### Two settings stores (important, easy to get wrong)

| Store | Written by | Read by | Holds |
|---|---|---|---|
| `/config/settings.yml` | entrypoint (template), WebUI `/app-config` + `/settings` | `config_loader` | services, paths, plex mapping, `grabber.*` (including `max_grabs_per_run` and `search_delay_minutes`), `webui.quick_status_log_lines`, `timezone` |
| `/config/webui_settings.<series>.json` | WebUI `/settings` (sessions only) | `motogp_common.get_desired_sessions()`, WebUI | desired sessions only |

The desired-sessions setting **only** exists in JSON. `max_grabs_per_run` is a
`grabber.*` setting: it lives in settings.yml, which is what the grabber reads
via `config_loader.get_max_grabs_per_run()`. The UI merges it in at
presentation time (`/settings` and the settings page), so there is exactly one
source of truth. On first load, a legacy `max_grabs_per_run` still sitting in
`webui_settings.<series>.json` is migrated once into settings.yml and then dropped from
the JSON; settings.yml wins on disagreement. Neither store may silently shadow
the other.

### Plex path mapping

Plex matches by **exact string** against paths as *Plex* sees them. If Plex runs
in its own container the prefixes differ (`/media` vs `/data/media`), nothing
matches, and tagging fails silently. `plex.path_mapping.container` and
`.plex` — both halves required — or `PLEX_PATH_CONTAINER` / `PLEX_PATH_PLEX`.

This is the single most common cause of "tagging stopped working".

---

## 4. Runtime state in `/config`

**Every shared state file carries its series.** `<name>.<series>.json`, for
example `grabber_state.motogp.json` or, once F1 writes state,
`grabber_state.f1.json`. Two series writing `grabber_state.json` into one volume
overwrite each other's entries within a day, and nothing reports it - history
just stops accumulating. The rule itself lives on
`SeriesRuntime.state_file()`; `utils/state_files.py` is the helper call sites
use, and `state_path()` in the spine re-exports it for the MotoGP-bound modules.

| File (`<series>` = `motogp` today) | Purpose | Safe to delete? |
|---|---|---|
| `settings.yml` | Main config. **Not** namespaced: it is one installation's configuration, not one series' state | No |
| `webui_settings.<series>.json` | Desired sessions only | No (resets to defaults) |
| `grabber_state.<series>.json` | `searched` / `grabbed` maps; also the stats source | No — rewrites history |
| `watcher_state.<series>.json` | processed / pending / failed, retry backoff | Loses retry state |
| `activity_events.<series>.json` | Activity tab events (file-locked) | Yes |
| `discord_rate_limit.json` | Webhook pacing. **Not** namespaced: one writer, disposable | Yes |
| `release_mismatches.<series>.json` (+ `.lock`) | Releases marked as wrong, with the matcher's reasoning. Written by the WebUI under a sidecar lock, read by the grabber | No — un-blocks every marked release |
| `background_health.<series>.json` (`background_health.motogp.json`) (+ `.lock`) | Scheduler-loop and watcher progress timestamps (§8b). Written by the scheduler and the watcher thread, read by the WebUI | Yes — resets both components to "unknown", which is reported as such |
| `logs/motogp.log` (+ debug) | `RotatingFileHandler`, 50 MB debug cap | Yes |
| `<series>_<year>.json` | Season calendars, refreshed from image | No — re-copied on start |
| `*.bak` | Pre-refresh calendar backup | Yes |
| `*.lock` | Sidecar locks for the JSON stores above (`utils/json_store.py`). The lock follows the data file's name, so a namespaced store gets a namespaced lock and two series never serialise against each other's sidecar | Leave them: empty, tiny, and deleting one a process holds open breaks its locking |

**The one-time migration.** A pre-hybrid install has the old unprefixed names.
`utils/state_files.migrate_default_series_state()` renames them at startup,
before the WebUI and the scheduler exist (after that, a migration is a race
rather than a rename). It is idempotent; it is a rename, never a copy; it only
applies to the **default series**, because a series added later never wrote an
unprefixed file and inheriting MotoGP's would resurrect the collision; and if
the namespaced file already exists it wins while the legacy file is left
untouched - two files disagreeing is a data question, not something a rename
should settle. A failed rename is logged and skipped: the app runs, with state
that starts empty and says so, rather than refusing to start over a filename.

Most state is plain JSON shared by separate processes, but the four stores
below no longer write it directly. They all go through `utils/json_store.py`,
which is the single implementation of the persistence contract: a stable
sidecar lock (`<name>.lock`, never the data file or a temp file) held across
the whole load-mutate-commit transaction, a unique temporary file in the same
directory, flush + fsync, then `os.replace`. A reader therefore sees the old
file or the new one, never a truncated mixture, and two writers cannot read
the same snapshot and overwrite each other's updates. Corrupt JSON is logged
at ERROR naming the path before the caller's default is used - silently
treating corrupt state as empty state is how "the file is broken" used to
become an evening of "why did it do that again".

| Store | Transaction model |
|---|---|
| `activity_events.<series>.json` | `update_json`: reloaded under the lock for every emit/progress/clear |
| `release_mismatches.<series>.json` | Same contract (the original implementation, now delegating to the helper) |
| `grabber_state.<series>.json` | Long-lived in-memory state; `save()` reloads under the lock and three-way merges against the load-time snapshot, so overlapping scheduled/manual grabbers keep each other's entries and this process's deletions still apply |
| `watcher_state.<series>.json` | Same three-way merge on `_save()` (`processed` merged as a set) |
| `webui_settings.<series>.json` | Locked atomic whole-document write; callers submit the complete document. The one-time `max_grabs_per_run` migration re-reads and writes under the same lock |

Still outside the contract, deliberately: `discord_rate_limit.json` (one
writer, disposable, not namespaced), `settings.yml` (owned by
`utils/config_loader.py`, which has its own write path), the startup migration
(nothing else is running, and a rename cannot half-happen), and
`scripts/cleanup_utility.py` (offline, `--confirm`-gated, not in the image -
it resolves the namespaced name, because rewriting a file the app no longer
reads would look like it worked and change nothing). New code must use `utils.json_store`
for any new shared JSON state and must handle malformed state visibly rather
than silently replacing useful data with defaults.

---

### Correcting a mismatch (the loop this exists to break)

`filter_releases()` is a set of rules, so a release the rules wrongly accept is
selected every single time the session is searched. Deleting the file and asking
again therefore does nothing. Since this feature, the download tab's session row
carries a 🚫 button that opens a dialog with two jobs:

1. **show the evidence** - the release title the grabber recorded
   (`grabber_state.<series>.json`), and `explain_release()`, which runs the *real* filter
   over that title and returns every check in order with the value that matched
   (venue alias, round pattern, resolved session, score). This is the part that
   makes a pattern fix possible: "it matched" is what has to be corrected, and
   a second description of the rules in the UI would drift and then explain the
   wrong thing.
2. **record the block** - `utils/release_mismatches.py` stores
   `release_mismatches.<series>.json` with the title, scope, target and evidence.
   `filter_releases(excluded_titles=...)` drops it before scoring, so the next
   `backfill --force` selects the next-best candidate instead.

Scope defaults to the one session; a global entry is offered for a title that is
wrong wherever it appears (an off-by-one round number). Matching is on a
normalised title (case, dots and separators folded) and nothing fuzzier - a
looser rule would block a good release because it resembles a bad one, and the
symptom would be a session that silently never downloads.

Two details matter and are covered by tests:

- **The old folder is moved, never deleted.** If the session is already sorted,
  a replacement download's destination exists, and `shutil.move(src, dst)` would
  nest the new folder *inside* the old one - Plex keeps showing the wrong file
  and nothing has visibly failed. Both the dialog (opt-in, default on) and
  `motogp_sort._clear_existing_session_folder()` move it to
  `<library>/.mismatched/<season>/`. The leading dot keeps it out of Plex's
  scanner and out of every folder walk in this app; the media is still on disk.
  Deleting media from a web request is not something this app does.
- **The replacement is not a second download path.** It runs the existing
  `backfill --season S --round R --sessions E<element> --force` command, built
  server-side from a resolved `SessionType`. `--force` is required because the
  grabber's 30-minute search cooldown would otherwise skip the session and the
  UI would report a download that never started.

## 5. WebUI surface

The route inventory below is descriptive, not a route-count invariant; use
`app.url_map` and `tests/test_webui_auth.py` for the current route set. Nav tabs:
Home, Download, Settings, Stats, Calendar, Activity; a bottom nav for mobile;
`/logs` is reachable but not in the top nav.

| Route | Method | What it does | Risk |
|---|---|---|---|
| `/` | GET | Dashboard | low |
| `/library` | GET | Download/library view | low |
| `/settings-page` | GET | Settings + admin sub-tab | low; server-side auth applies to the route |
| `/logs` | GET | Log viewer | **exposes log text** |
| `/run` | POST | Starts one typed job (`{action, season, round, sessions, scale, dry_run, force}`); `build_job()` validates fields and builds the argv server-side | **write — authenticated action; singleton slot claimed synchronously under a lock, output bounded, 1h timeout, exit code recorded (utils/webui_jobs.py)** |
| `/settings` | GET/POST | Reads/writes sessions in `webui_settings.<series>.json`; `max_grabs_per_run` is read from and written to `settings.yml` via `config_loader` | **write** |
| `/status` | GET | Background command output | low |
| `/api/activity-events` | GET | Activity events | low |
| `/api/activity-events/clear` | POST | Clears events | write |
| `/library-status` | GET | Scans media library for a season | medium (filesystem walk) |
| `/app-config` | GET/POST | **Returns Plex token + Prowlarr/SAB keys**; POST rewrites `settings.yml` | **highest** |
| `/test-connections` | GET | Probes Prowlarr/SAB/Plex with stored keys | medium |
| `/logs/stream` | GET | SSE log tail | **exposes log text** |
| `/logs/recent` | GET | Recent log lines | **exposes log text** |
| `/stats` | GET | Stats page | low |
| `/failed-sessions` | GET | Failed session searches, normalised with the stage that failed, whether waiting can help, and a next-step hint (§8c) | low |
| `/download-selected` | POST | Triggers downloads | **high — action** |
| `/mismatch` | GET | Release-mismatch log (blocked releases, evidence, reason) | low |
| `/api/release-mismatch` | GET | Evidence for one session: the recorded release and *why* the matcher accepted it | low (release titles only) |
| `/api/release-mismatch` | POST | Records a block; optionally moves the wrong library folder into `<library>/.mismatched/` | **write — changes future selection and moves media** |
| `/api/release-mismatch/<id>` | DELETE | Unblocks a release | write |
| `/api/release-mismatches` | GET | All recorded mismatches (badge counts) | low |
| `/api/release-mismatch/redownload` | POST | Starts `backfill --force` for one session via the existing grabber | **high — action** |
| `/activity`, `/activity/stream` | GET | Activity feed + SSE | low |
| `/calendar`, `/calendar-data` | GET | Calendar views | low |
| `/api/sabnzbd/queue` | GET | Proxies SABnzbd queue with the API key | medium |
| `/diagnostics` | GET | Schema issues, credential provenance, Plex library check, background-progress heartbeat (§8b) | medium (no values) |
| `/flags/<path>`, `/assets/<path>` | GET | Static files | low (path must be checked) |
| `/manifest.json`, `/sw.js` | GET | PWA — public | — |
| `/healthz` | GET | Liveness for the Docker HEALTHCHECK — public | — |

Every route requires a verified Cloudflare identity except endpoints in
`PUBLIC_ENDPOINTS` (static assets, PWA plumbing and `/healthz`). With
`AUTH_MODE=none`, the guard is deliberately disabled and the port must only be
reachable on a trusted network. `app.after_request` adds `X-Content-Type-Options: nosniff`,
`X-Frame-Options: SAMEORIGIN` and `Referrer-Policy: no-referrer`; it does **not**
set `Access-Control-Allow-Origin` (that wildcard was removed in v2.5.0 — the UI
is same-origin and the header let any LAN page read `/app-config`).

---

## 6. Authentication model

Since **v2.5.0**. Before that, anything that reached 5679 got full access.

```
utils/auth.py       AccessVerifier — signature (RS256 against CF JWKS),
                    issuer, audience, expiry, email claim, allowlist.
                    No Flask import; no network in tests.
utils/web_auth.py   AuthGate (mode resolution) + install() -> before_request
                    hook + denial page/JSON. Owns PUBLIC_ENDPOINTS.
```

Modes: `cloudflare` (default when `AUTH_ISSUER` is set), `none` (explicit,
warns loudly, banner in UI). A partial `AUTH_*` config **fails closed** — gate
enabled, every request 401, reason printed at startup.

`PUBLIC_ENDPOINTS` is the whole exception list: `static`, `static_files`,
`flag_files`, `manifest`, `service_worker`, `health`. Everything else is
guarded. The guard is a `before_request` hook precisely so a new route cannot be
added unauthenticated by omission; `tests/test_webui_auth.py` enumerates
`url_map` to enforce it.

Denial is 401 with an HTML page for browsers (it must not extend `base.html` —
at `before_request` time there is no view context and rendering raises) and JSON
for `/api/*`.

The session cookie carries only the verified email, for display. It is not the
auth mechanism; `SESSION_COOKIE_SECURE` is off by default because the container
is reached over plain HTTP inside the LAN and TLS terminates at Cloudflare.

`uiadminpassword` and `/verify-admin` were **removed**. The old check compared a
plaintext value with `==`, was unknown to `config_loader`, gated only the
client-side tab while `/app-config` stayed open, and was silently deleted by the
next save from the UI.

---

## 7. Tests and CI

- **423 tests** passed in the last local verification on 2026-11-07 with
  `python -m pytest tests -q`. Treat this as a dated snapshot; rerun the suite
  rather than assuming the count is current.
- `tests/test_webui_security.py` now contains regression tests for the fixed
  `/app-config` merge, CORS removal and route security behaviour. They assert
  safe behaviour; route enumeration is in `tests/test_webui_auth.py`.
- `tests/test_container_lifecycle.py` extracts shell blocks from `entrypoint.sh`
  and runs them against a throwaway `/config`. Good, but it is string/block
  extraction — it does not prove the image runs. Hence the smoke-test item.
- `tests/test_unraid_template.py` cross-checks `unraid/motogparr.xml` against
  `ENV_SPEC`.
- `tests/test_config_loader.py` covers precedence and the empty-means-unset rule.
- CI: `docker-publish.yml` (tests → build → push), `calendar-check.yml`
  (calendar validation, duplicate JSON key rejection), `container-smoke.yml`
  (build and run the image, assert non-root + auth), `release.yml` (generate
  notes, publish, open release-notes PR, then wait for the versioned image and
  `:latest` to resolve before calling the release shipped), `release-prep.yml`
  (run `scripts/check_release_readiness.py` on every PR to `main`: tag format,
  Unraid `TemplateURL` pin, Dockerfile `COPY` sources, tag not already
  published), and `pr-label-check.yml` (require at least one label on PRs to
  `main` so release notes are categorised). `release.yml` also refuses to
  publish a tag whose own tree does not pin it, which is the v2.11.0 ordering
  hazard made mechanical - the checklist order is in
  `docs/IMPROVEMENT_PLAN.md`. Their
  triggers differ: calendar checks are path-filtered, the container smoke test
  skips documentation-only PRs, and release publishing runs on version tags or
  manual dispatch. Workflows use Node 24 runtimes (`actions/checkout@v5`,
  `actions/setup-python@v6`, `docker/*@v4/v6/v7`, `actions/upload-artifact@v6`).

Run `pytest tests`, never bare `pytest` — the root scratch scripts break
collection (`SANDBOX.md`).

---

## 8. Container build

`python:3.11-slim`, venv at `/opt/venv`, `fonts-dejavu-core` for overlay text,
`EXPOSE 5679`. The WebUI launches child Python processes via `sys.executable`;
the scheduler's command configuration currently names `/opt/venv/bin/python3`.
`HEALTHCHECK` → `utils/healthcheck.py` (stdlib only; the image has no `curl`).

The Dockerfile copies an explicit list of files rather than the tree — so a new
top-level module is invisible to the image until you add a `COPY` line. The flip
side is that nothing ships by accident: `test_cleanup_utility.py` was never in
the image, contrary to an early review finding.

`.dockerignore` (added v2.5.0) first reduced the measured build context from
1.23 GB to 754 MB by excluding `.git`, tests and local config. Issue #25 reduced
the remaining asset payload from ~753 MiB to ~4 MiB: `assets/external/2025`
was unused and removed, and all byte-identical venue placeholders became
relative links to `assets/motogpPoster.jpg` and `assets/motogpFanart.jpg`.
The calendar filenames and `CLASS_ASSET_DIRS` are unchanged. The links are
*source* artwork inside `/app/assets`; the sorter renders session text and
writes regular JPEGs into the library. Do not link the library outputs — Plex
may run in another container and cannot see `/app/assets`. To replace a venue's
placeholder with custom art, **unlink it before writing** or the shared master
will be changed for every venue. `tests/test_artwork_assets.py` checks the
links, and the container smoke test verifies they survive `COPY assets/`.
`tests/test_build_hygiene.py` checks both directions: development files are
excluded, and no `COPY` source is.

`scripts/` is deliberately not copied. It holds utilities that delete media.

No `USER` directive, deliberately: `entrypoint.sh` drops root to `PUID:PGID`
(default 99:100) with `setpriv` and re-execs itself. A fixed `USER` would bake in
one uid, and on Unraid the right uid is per-installation. `UMASK` is applied and
the script refuses to continue if it is still uid 0.

`utils/permissions.py` then checks write access to `/config`, the library path
and the downloads path before any work starts. Only paths the app writes are
checked, so a read-only media share is legitimate. A failure names the folder,
the uid and the `chown` command, because the alternative symptom — settings that
revert, downloads that never sort — does not look like permissions.

New venue artwork should replace the appropriate symlink with a real JPEG;
keep the other links in place so unused venues do not inflate the image again.

---

## 8a. Activity feed (worth knowing before you touch it)

Events are emitted by the grabber, watcher, sorter and tagger into
`activity_events.<series>.json` under a file lock, and read by `/activity/stream` (SSE)
and `/api/activity-events`. Stages: `searching`, `candidates_found`,
`downloading`, `download_complete`, `processing`, `metadata`, `complete`,
`error`.

Four bugs were fixed here historically and the fixes are load-bearing:

1. **Match SABnzbd jobs by episode prefix, not release name.** SAB renames the
   job to `25R02E09 ...` while the release name is entirely different; prefix
   matching is the only stable join. Derive the prefix from the event id
   (`2025_R02_E09` → `25R02E09`).
2. **Keep SAB progress in a separate client-side cache.** Activity polling (3 s)
   and SAB polling (4 s) otherwise overwrite each other and the bar flickers.
3. **Start SAB polling because an event is `downloading`,** not on page load.
4. **No emits from the watcher after the tagger's `complete`.** That produced
   "moved to library" *after* "Complete"; the tagger's `complete` is final.

Session identity is also load-bearing (issue #24). Always emit
`session_type.full_display_name`, never the classless `display_name`, for an
Activity title. `emit_activity()` retains the existing round and venue when a
later stage cannot reconstruct them; the watcher only parses the folder prefix.
`tests/test_activity_feed.py` exercises the watcher, Plex metadata and completed
history for all three race classes.

One known cosmetic gap remains: the SAB progress gradient may not render in the
mobile PWA (fallback text is fine).

## 8b. Background progress vs. container health

Two different questions, deliberately answered separately:

| Question | Answered by | Used for |
|---|---|---|
| Can the WebUI serve a page? | `utils/healthcheck.py` → `/healthz` | Docker `HEALTHCHECK`, so a dead UI no longer hides behind a green "Up" |
| Is the scheduler still looping, and the watcher still scanning? | `background_health.<series>.json` (§4) | The home health strip and `/diagnostics` - **not** the Docker probe |

The scheduler records `last_loop` at the top of every iteration, `last_check`
when a scheduled search actually ran, and `started` once at startup; the
watcher records `last_scan` after every scan cycle (including one that found
nothing, or whose downloads path is missing) and `started` when the thread
starts. "Progress" is always the loop field, never `last_check`: off-weekend
the scheduler legitimately runs no searches for days, and a rule keyed on
searches would report a perfectly healthy scheduler as stale for that whole
time.

`utils/background_health.py` holds the question and the rule; the loops only
write. `component_health()` takes an explicit `now` and does no I/O, states are
`live` / `stale` / `never`, and **`never` is never reported as healthy** - a
fresh container has no evidence yet, so it says so. The stale threshold is
twice the scheduler's *own* interval (`motogp_scheduler.CONFIG`, including the
quiet-hours interval via `is_quiet_hours()`) plus an hour of grace; reading the
intervals from the scheduler is what stops a correct overnight run from being
called dead at 03:00.

A stale heartbeat is a **warning, not a restart** (decided in Candidate A):
`/healthz` stays a stateless probe with no file reads, no network calls and no
race-weekend awareness, because a slow-but-legitimate Prowlarr search must not
put the container into a restart loop. Because the record is a shared `/config`
file it goes through `utils/json_store.py` (§4) - one component per write, so
the scheduler and the watcher thread cannot erase each other's timestamps - and
a failed heartbeat write is logged and swallowed: a monitoring loop that dies
because monitoring failed is a worse outage than a missing heartbeat.

## 8c. Why a session did not download

`grabber_state.<series>.json`'s `failed` map used to hold one of three strings, and they
were not diagnoses: `"No results"` was recorded for a 401 from an indexer, an
unreachable Prowlarr, no enabled indexers and a genuinely empty search alike;
`"Grab failed"` covered an unreachable SABnzbd and a refused release alike.

`utils/failure_reasons.py` owns the replacement. Stages, in pipeline order:
`search_setup`, `search_indexer`, `no_candidates`, `rejected`, `blocked`,
`sab_submit`, `download`, `process`. Each has a label, a hint naming the next
step, and `actionable` - the field that matters, because it separates **wait**
(`no_candidates`, `rejected`, `blocked` are self-resolving or deliberate) from
**fix something** (a dead service never repairs itself).

Where the evidence comes from:

- `search_for_session(..., diagnostics=...)` fills a caller-supplied dict with
  indexer/candidate/filter counters. It is an out-parameter, not a second return
  value, so every existing caller is unchanged.
- `search_indexer()` returns `(releases, error)` and `grab_release()` returns
  `(nzo_id, error)`, so a refused key, a timeout and an empty answer stop being
  the same empty list. Neither error string contains a URL: Prowlarr and SABnzbd
  both put their key in the query string.
- `explain_search()` converts the counters to a stage, in pipeline order, so a
  setup error outranks an empty result (both yield zero candidates; only one
  needs a human). Partial indexer failure is reported but not called a failure.
- `GrabberState.mark_failed()` accepts the old bare string *and* a structured
  record; the watcher's `FailedItem` gained `stage`/`hint`/`actionable` with
  `from_dict` ignoring unknown keys, so an existing `watcher_state.<series>.json` still
  loads and retry state is never lost by adding a field.

**Every stored reason is redacted** by `failure_reasons.sanitise()`: configured
secret values verbatim, then any URL reduced to `scheme://host:port`, then named
credential parameters and bare 32-character tokens. These strings persist in
`/config`, render in the UI and get copied into reports, and a `requests`
exception renders the whole URL - see AGENTS.md §7a for why that is not a
hypothetical.

`/failed-sessions` normalises entries through `describe()`. An entry written
before the stages existed keeps its reason and reports **no** stage: labelling a
historic "No results" as "not published yet" would relabel a past outage as a
quiet weekend. The Download tab's warning dialog shows the stage-named reason, an
⚠️ for actionable faults and the hint, and escapes the text (it contains
indexer/downloader error strings, so it is data, not markup).

## 8d. Library scan cost is measured, not assumed

The watcher caches a library walk for five minutes and several WebUI routes walk
it per request, so "the scan might be slow" is a reasonable suspicion - and one
that has never been measured. `utils/scan_metrics.py` records duration and scope
for `library-season` (`motogp_webui.scan_library_for_season`, called on every
home page and Download tab render) and `watcher-library-cache`
(`LibraryChecker._refresh_cache_if_needed`).

`/diagnostics` reports `scan_cost`: per label, count, median, p95 and max in
milliseconds, the scope of the slowest sample (rounds and sessions walked), and
any sample over `SLOW_SCAN_SECONDS` named individually. Percentiles are
nearest-rank, so a single bad sample stays visible instead of being smoothed
into its neighbours.

Recorded limits, so a later session does not "finish the job" by building the
thing this deliberately avoids:

- **Nothing is persisted.** Samples live in the process and are lost on restart;
  a metrics file would be another locked `/config` store for information that
  only matters during an investigation.
- **Nothing is cached or indexed on the strength of the numbers.** A targeted
  scan or an incrementally refreshed index is a *possible outcome* of a
  measurement showing the walk is the problem - not part of taking one. Revisit
  only with evidence from a real library, not from this sandbox.
- **No timers in the sort/tagger path.** The library walk is the question.

## 8e. Scheduler decisions and submission reservations

The scheduler evaluates desired, timed sessions against actual video files on
its periodic pass. It launches the scheduled grabber only for due missing or
in-flight sessions; future readiness and retries set its next wake-up (bounded
by the normal heartbeat interval). A missing timetable cannot count as complete.
`scheduler_plan.json` records the decision for Home; the existing independent
heartbeat still detects a dead loop. The watcher and manual backfills remain
independent of the scheduled search decision.

Scheduled, backfill and clear commands first take the non-blocking cross-process
`grabber_run.lock`; an overlapping run fails visibly rather than silently
queuing a forced replacement. All SAB submissions also use
`submission_intents.json` and its sidecar lock. The lock
serializes the state recheck, write-ahead reservation, request and state commit,
including both keys for a combined qualifying release. Every reservation has a
random marker carried in the temporary SAB job name. On restart, queue and
history are queried for a unique matching ID, and only confirmed jobs are
recovered. A missing/ambiguous result remains reserved and is surfaced on Home
for explicit user review; `--force` never overrides it. A successful state
commit records the marker too, so a crash before intent deletion is recoverable
without relying on retained SAB history. This reduces duplicate risk, not a
claim of distributed exactly-once delivery. Never store NZB URLs or credentials
in the reservation file. Both stores must remain valid; corruption fails closed.

## 9. What the old notes said vs. what is true

`aicontextual_notes/` (5 files, V1.7→V2.0) is historical. Surviving facts:

- The blue palette (`#52aad8`) and "align with F1arr" visual conventions are
  still in force — the notes are the only record of *why* orange became blue.
- Tab names Home / Download / Settings / Stats / Calendar / Activity come from
  that refactor, as do `active_tab` values and the mobile bottom nav.
- The Activity feed was rewritten from log-parsing to `activity_emitter.py`
  events because log parsing was timing-unreliable and format-fragile. **Some
  log parsing still exists** in `motogp_webui.py` (`parse_log_line` for the
  activity stream), so the migration is not complete.
- The PWA/service worker was created but is not fully wired; `/sw.js` serves the
  file, caching behaviour is minimal.
- **An `auth.py` was written in the v1.4 session and explicitly "not deployed".**
  `bcrypt` was added to `requirements.txt` "for auth" and later removed. So
  app-level auth has been attempted before and deliberately abandoned in favour
  of Cloudflare Access. Anything you build now must either finish that job or
  replace it — check for half-finished auth before starting, and do not assume
  the absence of auth was an oversight.
- The clock in these notes is unreliable: files labelled 2025 sit next to
  release dates in 2026, and `docs/CALENDAR_CHANGES.md` was clearly written
  later. Trust git tags and code, not note dates.

Everything else in those files (line counts, file layouts, phase checklists,
`docker push` recipes) is stale. This document supersedes them.
