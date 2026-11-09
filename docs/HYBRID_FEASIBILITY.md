# Hybrid MotoGP + F1 app — feasibility investigation

**Written:** 2026-11-09 · **Basis:** `msportarr` @ `7d6f767` (v2.13.0, 662 tests passing)
compared against `f1arr` @ `a678199` (v1.9.6, `main`).

This document answers one question: *is a single combined app that handles both
MotoGP and Formula 1 a good idea, and if so how?* It records what was measured,
what collides, and what has to be decided before any code is written.

It is an investigation, not an implementation plan — no code has been changed.

---

## 1. Verdict

**Yes, it is feasible, and it is the right call.** The two apps are the same
program twice. A combined app is not a merge of two codebases; it is
**MotoGParr's architecture with one new concept — a *series* — threaded through
it**, plus the F1 session vocabulary, F1 calendars and F1 artwork ported in.

The reason it is low-risk is asymmetric: **MotoGParr is ~2 years and 13 minor
versions ahead**, and almost all of that head start is infrastructure that has
nothing to do with MotoGP. F1arr needs all of it. Nothing in MotoGParr's
infrastructure has to be re-derived for F1; it has to be *parameterised*.

The one genuinely hard part is not technical. It is that **F1arr has no
authentication, no test suite and no CI**, so adopting MotoGParr's security and
release model is a behaviour change for the F1 half that the F1 half's users have
never had. That is a decision, not a design problem — see §9.

---

## 2. What was actually compared

Both repositories were cloned and diffed module by module. Similarity figures are
`difflib` ratios on whole files, so they understate structural agreement (renames
score as differences) but they rank correctly:

| Module (f1arr → msportarr) | Lines f1 → mgp | Similar | Reading |
|---|---|---|---|
| `f1_overlay_utility.py` → `motogp_overlay_utility.py` | 699 → 696 | **84.8 %** | Effectively the same file |
| `f1_plex_tagger.py` → `motogp_plex_tagger.py` | 462 → 557 | **71.0 %** | Same design, mgp adds `utils/plexlabels.py` |
| `f1_scheduler.py` → `motogp_scheduler.py` | 414 → 681 | **50.7 %** | Same loop, mgp adds planning + heartbeat |
| `f1_grabber.py` → `motogp_grabber.py` | 1699 → 2192 | **35.4 %** | Same pipeline, mgp adds guards/mismatch/failure stages |
| `f1_sort.py` → `motogp_sort.py` | 550 → 1250 | **20.8 %** | Same shape, mgp adds multi-class + combined-quali |
| `f1_common.py` → `motogp_common.py` | 771 → 2678 | **16.3 %** | The divergence: mgp adds class, element numbering, format data |
| `f1_watcher.py` → `motogp_watcher.py` | 279 → 1078 | **25.5 %** | Same watcher, mgp adds backoff/failure records |
| `f1_grab_stats.py` → `grab_stats.py` | 352 → 215 | **6.0 %** | F1's stats are *more* elaborate than mgp's |
| `f1arr_webui.py` → `motogp_webui.py` | 1228 → 2634 | **31.7 %** | Same route set, mgp adds guards/mismatch/diagnostics |

Everything above the 50 % line is shared plumbing that should be **one file in
the hybrid**, not two. The genuinely series-specific code is concentrated in
`f1_common.py` / `motogp_common.py` (the session vocabulary and release
matching) and the calendars — exactly where you would expect it.

---

## 3. The four real differences

These are the only places the two apps genuinely disagree. Everything else in §4
and §5 is one side being ahead.

### 3.1 Weekend format

| | MotoGParr | F1arr |
|---|---|---|
| Model | `weekend_formats` block in the calendar; each round names its format and its session list | A boolean `sprint_weekend` per round |
| Sessions | `fp1 / practice / fp2 / qualifying / sprint / warmup / race` (2026) and `fp1 / fp2 / sprint_qualifying / fp3 / qualifying / sprint / race` (2027) | `fp1 / fp2 / fp3 / sprint_qualifying / sprint / qualifying / race` |
| Extra classes | Three classes (MotoGP / Moto2 / Moto3) run through the same weekend | One class |
| Who decides | `round_runs_session()` in `motogp_common` | `get_excluded_sessions_for_round()` in `f1_common` |

MotoGParr's model is strictly more expressive — a 2027 MotoGP round is *data*
(`weekend_format: "motogp_2027"`), and the validator cross-checks the format
against the round's session list in both directions. F1's boolean is the same
idea with one bit of freedom. **Adopt MotoGParr's model; F1 becomes two
`weekend_formats` entries (`f1_standard`, `f1_sprint`).**

### 3.2 Session vocabulary and numbering

This is the biggest divergence and the one with on-disk consequences.

| | MotoGParr | F1arr |
|---|---|---|
| Session types | 24 | 25 |
| Identity | `26R07E82` — element number from `SESSION_ELEMENTS`, allocated **in blocks of ten** with gaps | `26R01E19` — **the raw enum value** |
| Legacy handling | `SESSION_ELEMENTS_LEGACY` + `LEGACY_ELEMENT_SEASON`, frozen, read-only, ambiguity refused rather than guessed | None |
| Class in identity | Encoded in the session type itself (`MOTO3_RACE`, `MOTOGP_RACE`) | Not applicable |

MotoGParr's blocks-of-ten rule exists because the old dense 1–24 table was full,
so 2027's FP3 and Sprint Qualifying had to be appended *after* the Race and a
2027 round listed out of order in every folder, title and CLI listing. F1arr is
sitting on exactly that problem with a 25-member dense table and one free slot
(E99 = UNKNOWN). **The hybrid must give F1 its own `SESSION_ELEMENTS` table in
blocks of ten.** That renames F1 folders (`26R01E07 Practice 1` → something like
`26R01E10 Practice 1`) — see §8, risk R1.

F1 also carries a category of content MotoGP does not: **support shows**. Ted's
Notebook (three variants), The Paddock Show, Paddock Uncut, Team Principals,
Driver Press Conference, The F1 Show, Weekend Warm Up, pre/post-race shows,
Jolyon Palmer's Analysis, Full Event, Race Uncut. These are matched by
`SESSION_PATTERNS` — a priority-ordered `must_have` / `must_not_have` table — and
they are the most intricate part of F1arr's release matching. MotoGParr has
`classify_session()` with a refusal-to-guess rule and `release_aliases` in the
calendar, which is the better *mechanism*; F1arr's pattern table is the better
*vocabulary*. **Port F1's pattern table into MotoGParr's classifier, per series.**

### 3.3 Release matching

Both score releases by group (VERUM/MWR/DARKSPORT/BILLIE, F1 also playWEB),
require quality, require the season, and require a venue-alias or round-number
hit. The differences that matter:

| | MotoGParr | F1arr |
|---|---|---|
| Ambiguity | `classify_session()` returns `UNKNOWN` and `filter_releases()` **rejects** it, logging the title | Falls through to `RACE` when the title says "grand prix" with no other token |
| Series gate | `detect_race_class()` must match the target class | `re.search(r'formula\s*1\|f1', title)` |
| Junior series | N/A | Global exclusion list: academy / F2 / F3 / Porsche Supercup |
| Blocking | `utils/release_mismatches.py` — a marked title is excluded before scoring | None |
| Explanation | `explain_release()` runs the real filter and returns every check | None |

F1's fall-through-to-Race is precisely the bug class MotoGParr spent a release
eliminating, and F1's global exclusion list is a good idea that should survive
the port (as a per-series `excluded_terms` list).

**Stale data found:** `config/f1_sprints.json` claims 2026 sprint rounds are
`[2, 6, 11, 13, 16, 19]`, while `config/f1_2026.json` says `[2, 6, 7, 11, 14, 18]`.
They disagree on three rounds each way. The calendar is what the code reads;
`sprints.json` is a dead sidecar. **Delete it, do not port it.**

### 3.4 Artwork

| | MotoGParr | F1arr |
|---|---|---|
| Size | 4.3 MB | 87 MB |
| Content | Relative symlinks to two shared master JPEGs | Real per-venue poster + fanart per venue |
| Classes | `CLASS_ASSET_DIRS` per class | Single set |

F1arr's artwork is the thing worth taking. MotoGParr deliberately shrank its
assets (issue #25) and documents the "unlink before you write" rule so custom art
can replace a placeholder without clobbering the shared master. **Port F1's real
JPEGs as the F1 series' artwork and leave MotoGP's symlink scheme alone.**

---

## 4. What the hybrid gains from MotoGParr (i.e. what F1arr is missing)

Every item here is MotoGParr infrastructure with no MotoGP-specific content.
None of it has to be rebuilt for F1 — it has to be made series-aware.

**Security — the F1 app currently has none of this.**

| Finding in f1arr | MotoGParr's fix |
|---|---|
| No authentication on any route. `/app-config` returns the Plex token and Prowlarr/SABnzbd API keys to anyone who can reach port 5678 | Cloudflare Access JWT verification (`utils/auth.py`, `utils/web_auth.py`), `before_request` hook, `PUBLIC_ENDPOINTS`, route-enumeration test |
| `Access-Control-Allow-Origin: *` on **every** response, including `/app-config` | Wildcard removed; `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` added |
| `uiadminpassword` compared with `==` in plaintext, gating only the client-side tab while `/app-config` stays open | Route and key removed entirely |
| `POST /run` takes a client-supplied string and `args.split()`s it into argv | Typed action + `build_job()` constructs argv server-side; nothing client-supplied is split or shell-interpreted |
| No CSRF/origin defence | `utils/origin_guard.py` |
| A live Prowlarr API key was committed to *this* repo's history in 2026 (AGENTS.md §7a) and is still in git history | `tests/test_build_hygiene.py` scans the tree; the incident record tells you to rotate rather than rewrite history |

**State integrity.** F1arr writes `grabber_state.json` with a bare
`open(..., "w")`. MotoGParr's `utils/json_store.py` holds a sidecar lock across
the whole load-mutate-commit, writes a unique temp file and `os.replace`s it, so
a reader sees the old file or the new one, never a half-written mixture — and
logs corrupt JSON at ERROR naming the path instead of silently using defaults.

**Operability.** `utils/settings_schema.py` (unknown/misspelled keys reported at
startup), `utils/plexcheck.py` + `/diagnostics` ("is Plex actually scanning this
folder?"), `utils/permissions.py` (PUID:PGID write checks with the exact host
`chown`), `utils/calendar_sync.py` (same-season calendar refresh with `.bak`),
`utils/failure_reasons.py` + `/failed-sessions` (a closed stage vocabulary that
separates *wait* from *fix something*, with every stored reason redacted),
`utils/background_health.py` (scheduler-loop and watcher heartbeats, staleness
derived from the scheduler's own intervals), `utils/scan_metrics.py` (library
walk measured, not assumed), `utils/plexlabels.py` (title + titleSort so Plex
does not clump same-named sessions).

**The mismatch loop.** `utils/release_mismatches.py` + `/mismatch` + the 🚫
dialog: because `filter_releases()` is deterministic, a release the rules wrongly
accept is selected on *every* retry, so "delete it and download it again" never
worked. MotoGParr records the block, explains why the matcher accepted the title,
and moves the wrong folder to `<library>/.mismatched/` instead of deleting it.
F1arr has no equivalent.

**Engineering.** 662 tests across 37 files, 6 CI workflows, a release sequence
CI enforces (Unraid `TemplateURL` pinned before the tag, release-notes PR,
image verification), `AGENTS.md` / `docs/ARCHITECTURE.md` / `SANDBOX.md`, and a
`.dockerignore`. F1arr has **no tests directory, no `.github/`, no
`requirements.txt`, no docs**, and four tracked near-duplicate WebUI files
(`f1_webui.py` and `test.py` are byte-identical; `f1arr_webui.py.save` is a
backup; only `f1arr_webui.py` is in the Dockerfile).

**Housekeeping debt in f1arr that should not be carried over:**
`test.py` (1802 lines, a copy of `f1_webui.py`), `f1arr_webui.py.save`,
`f1_webui.py` (1802 lines, 1167 of which are one inline `HTML_TEMPLATE` string
superseded by `templates/`), `f1_sprints.json` (stale, see §3.3), and
`config/f1_discord_rate_limit.json` committed as a runtime file.

---

## 5. What the hybrid gains from F1arr

1. **The F1 session vocabulary** — 25 session types including the support-show
   family and the priority-ordered `SESSION_PATTERNS` table that matches them.
2. **Sprint weekends as data**, already expressed per round.
3. **F1 calendars** for 2025 and 2026 (24 rounds each).
4. **Real per-venue artwork** — 30 MB of posters and 56 MB of fanart.
5. **A richer stats page** — `f1_grab_stats.py` computes daily/monthly maxima and
   per-session breakdowns that MotoGParr's 215-line `grab_stats.py` does not.
6. **Five more flags** (`ae`, `az`, `mc`, `sa`, `sg`).
7. **`playWEB`** as a scored release group (present in F1's scoring branch but
   missing from its own default `ALLOWED_GROUPS` — a small bug to fix on port).

---

## 6. Hard collisions — what breaks if you naively merge

These are the concrete reasons a hybrid is a design job and not a `cp`.

### 6.1 State file names are identical

Both apps, in the same `/config` volume:

| File | Written by f1arr | Written by msportarr |
|---|---|---|
| `grabber_state.json` | `f1_grabber.py:78` | `motogp_grabber.py:78` |
| `webui_settings.json` | `f1arr_webui.py:48` | `motogp_webui.py:91` |
| `watcher_processed.txt` | `f1_watcher.py:272` | *(mgp uses `watcher_state.json`)* |

**Every key in these files would be overwritten by whichever process wrote last.**
This is the single biggest blocker and it forces the design: state must be
namespaced per series (`grabber_state.motogp.json`, `grabber_state.f1.json`) or
keyed inside one document (`{"motogp": {...}, "f1": {...}}`). Namespacing the
filenames is the smaller change and keeps the locking story unchanged.

### 6.2 One Plex library per series, but one `plex.*` config block

`settings.yml` has a single `plex.library_id` / `plex.library_name` /
`plex.path_mapping`. MotoGP and F1 are separate Plex sections with separate
paths (`/media/Motorsport/MotoGP` vs `/media/Motorsport/F1`). The hybrid needs
per-series Plex identity, and `utils/plexcheck.py` and the tagger need to resolve
the right one.

### 6.3 One downloads folder, two SABnzbd categories

`SABNZBD_CATEGORY = "f1"` in F1arr; MotoGParr uses its own. Two series watching
one downloads folder must not both claim the same completed folder.

### 6.4 Element numbers are not unique across the two vocabularies

MotoGP uses E10–E90 in blocks of ten; F1 uses E01–E25 densely. Even after
renumbering F1 into blocks of ten, `parse_prefix()` must know which series a
folder belongs to before it can resolve `E32`. MotoGParr already refuses to guess
when a number is ambiguous between two schemes (`element_is_ambiguous`); the
hybrid extends that to across-series ambiguity. **A folder's series must be
recoverable from the name or the path, never inferred from the element alone.**

### 6.5 One scheduler loop, two calendars

MotoGParr's scheduler polls at 60 min on a race weekend / 360 min off / 120 min
in quiet hours. F1arr uses 30 / 360 / 120. A hybrid has to evaluate *both*
calendars each pass and take the busiest interval. Watcher stability differs too
(15 s vs 30 s) — one value must win, and it should be the more patient one.

---

## 7. Recommended design

**One container, one codebase, a `series` abstraction.** Not two containers, and
not a fork.

```
sportarr (name TBD)
├── series/
│   ├── __init__.py        Series registry: name, calendar prefix, session table,
│   │                      pattern table, asset dirs, plex identity, paths
│   ├── motogp.py          MotoGP + Moto2 + Moto3
│   └── f1.py              Formula 1
├── <series>_common.py  →  sport_common.py       (series-agnostic spine)
├── sport_grabber.py / sport_sort.py / sport_watcher.py / sport_scheduler.py
├── sport_webui.py         (per-series views, shared chrome)
├── utils/                 (unchanged; series passed in)
└── config/<series>_<year>.json
```

The rule that keeps this small: **a series is data plus a vocabulary, never a
code path.** Concretely, a series object supplies:

| Field | MotoGP | F1 |
|---|---|---|
| `key` | `motogp` | `f1` |
| `calendar_prefix` | `motogp` | `f1` |
| `session_types` | 24-member enum | 25-member enum |
| `session_elements` | existing `SESSION_ELEMENTS` | new, blocks of ten |
| `session_patterns` | MotoGP patterns + `release_aliases` | F1's pattern table (ported) |
| `weekend_formats` | `motogp_standard`, `motogp_2027` | `f1_standard`, `f1_sprint` |
| `classes` | MotoGP/Moto2/Moto3 | *(none)* |
| `excluded_terms` | *(none)* | academy, f2, formula 2, f3, formula 3, porsche supercup |
| `asset_dirs` | `CLASS_ASSET_DIRS` (symlinks) | `f1_posters`, `f1_fanart` (real) |
| `search_prefix` | `{class} {season} {venue}` | `Formula1 {season} {venue}` |
| `state_prefix` | `motogp` | `f1` |

Everything in `utils/` keeps working with a `series` argument added. The WebUI
gains a series switcher in the nav; the Download, Calendar, Stats and Settings
tabs become per-series views over shared templates.

**Why not two containers sharing nothing?** Because then you maintain two copies
of `json_store`, `failure_reasons`, `plexcheck`, `release_mismatches`,
`origin_guard`, `settings_schema`, `background_health`, `scan_metrics`, the
release workflows and 662 tests — which is the situation you are trying to leave.
**Why not one container running both apps side by side?** Because of §6.1: they
would corrupt each other's state within a day.

---

## 8. Suggested sequence

Ordered so each step is independently shippable and nothing on disk breaks before
its replacement exists.

| Step | What | Why this order |
|---|---|---|
| **0** | Delete f1arr's dead files (`test.py`, `f1arr_webui.py.save`, `f1_webui.py`'s inline template, `f1_sprints.json`) | Nothing depends on them; shrinks the diff |
| **1** | Port F1's artwork + 5 flags into `msportarr/assets/` and add `f1_posters` / `f1_fanart` asset dirs | Pure addition, zero behaviour change |
| **2** | Add `config/f1_2025.json` and `config/f1_2026.json` with `weekend_formats: {f1_standard, f1_sprint}` and a `classes: []` field | Calendars are data; the validator already cross-checks format ↔ sessions |
| **3** | Introduce the `series` registry and make `motogp_common` read its vocabulary from it. MotoGP must produce byte-identical output before and after | The whole design rests on this being a no-op for MotoGP |
| **4** | Namespace state files per series (`grabber_state.<series>.json` etc.) with a one-time migration that renames a single-series file | Fixes §6.1 before a second series exists to collide |
| **5** | Per-series `plex.library_id` / `library_name` / `path_mapping` and per-series `library.<series>_path` / downloads path | Fixes §6.2/§6.3 |
| **6** | Add the F1 series: session enum, `SESSION_ELEMENTS` in blocks of ten, pattern table, `excluded_terms` | Now that the spine is series-aware |
| **7** | Merge F1's richer `f1_grab_stats.py` into the shared stats module | F1's version is the better one |
| **8** | WebUI series switcher; per-series nav state | Last, because it is the visible layer |
| **9** | Port the test suite's series-specific fixtures to run against both series; run `pytest tests` with a `--series` matrix | Guards every step above |

Steps 1–2 are pure additions and could be a PR each. Step 3 is the only
architectural one and should be its own PR with a "no behaviour change for
MotoGP" assertion in the tests.

### The F1 element-numbering decision (needs you)

MotoGParr's `SESSION_ELEMENTS` rule exists precisely because a dense table ran
out of room. Porting F1's dense 1–25 table reproduces the problem, so the
recommendation is to renumber F1 into blocks of ten:

```
10s FP1      20s FP2      30s FP3      40s Sprint Qualifying
50s Sprint   60s Qualifying             70s Race
80s support shows (Ted's Notebook, Paddock, press conferences, analysis…)
```

**Consequence:** every existing F1 folder on disk gets a new name
(`26R01E07 Practice 1` → `26R01E10 Practice 1`). MotoGParr has already been
through exactly this once and chose to delete and re-fetch the library rather
than migrate it (AGENTS.md issue #33, `LEGACY_ELEMENT_SEASON = 2025`). The same
choice is available here, or a one-off renamer could be written — but note
MotoGParr's own documentation records why it refused to write one: a stale number
that is still *valid* under the new scheme cannot be detected from the name
alone. **Recommendation: renumber, and re-fetch F1 content.** Decide before
step 6.

---

## 9. Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | F1 folder renaming orphans Plex items (lost watched state, duplicates) | **High** | See §8; re-fetch rather than migrate, as MotoGP did |
| R2 | F1 users gain Cloudflare Access auth they never had; a LAN-only user may not want it | Medium | `AUTH_MODE=none` is a supported, loudly-warned mode. Keep it |
| R3 | F1 users gain `MOTOGPARR_*`-style startup gates (missing library path stops the container) | Medium | These are the fail-visibly rules; the Unraid template ships blank path fields so `settings.yml` stays authoritative |
| R4 | Cross-series element ambiguity mis-reads a folder | Medium | Extend `element_is_ambiguous()`; never resolve a series from an element number alone |
| R5 | Two series in one scheduler loop doubles the work per pass | Low | Both loops are I/O-bound on Prowlarr; measure with `scan_metrics` before optimising |
| R6 | `f1_common`'s 25-session pattern table is the most intricate release logic in either app; a bad port silently files support shows as the Race | **High** | MotoGParr's refusal-to-guess + `--report-unknown` dry-run tool is the guard; port F1's patterns *into* it, do not reimplement |
| R7 | Asset bloat returns (F1's 87 MB of real JPEGs) | Low | F1's artwork is real and wanted; MotoGP's symlink scheme stays. Combined ~91 MB is acceptable |
| R8 | Scope creep — "while we're here, also…" | Medium | Steps 1–2 are shippable now; nothing else starts until 3 is agreed |

---

## 10. Open questions for you

1. **Name and repo.** A new repo (`sportarr`?) or rename `motogparr`? The
   container image, GHCR package, Unraid template URL and every documented path
   change if the name changes.
2. **The F1 element-numbering decision** in §8 — renumber and re-fetch, or keep
   F1's dense numbers and accept the 2027-style ordering problem arriving early?
3. **Do you want F1 to gain authentication?** It is the single largest behaviour
   change for the F1 half. `AUTH_MODE=none` preserves today's behaviour.
4. **One container or two?** §7 argues for one. If you would rather run two
   containers against one shared codebase, the series abstraction still works —
   only the entrypoint changes.
5. **Is F1arr's existing `/config` worth migrating**, or start F1 fresh? The
   state files collide (§6.1) either way, so a fresh F1 `/config` is the
   cheaper path.

---

## 11. What was *not* done

No code was changed in either repository. `f1arr` was cloned read-only into
`/home/user/f1arr` for comparison and is not part of this repository. The
MotoGParr test suite was run once to confirm the baseline: **662 passed**.
