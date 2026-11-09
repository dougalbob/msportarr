# Hybrid MotoGP + F1 app — feasibility investigation

**Written:** 2026-11-09 · **Basis:** `msportarr` @ `7d6f767` (v2.13.0, 662 tests passing)
compared against `f1arr` @ `a678199` (v1.9.6, `main`).

This document answers one question: *is a single combined app that handles both
MotoGP and Formula 1 a good idea, and if so how?* It records what was measured,
what collides, and what has to be decided before any code is written.

It is an investigation, not an implementation plan — no code has been changed.

---

## 0. Decisions taken (2026-11-09)

The five open questions in §10 have been answered. They are recorded here
because they change the shape of the work, not just its order:

| # | Question | Decision | Consequence |
|---|---|---|---|
| 1 | Name | **Motorsportarr** ("msportarr" was not a typo) | The repo name already matches. The app's *display* name, the Dockerfile labels, the Unraid template and the README still say MotoGParr; those are a rename slice, not this one |
| 2 | F1 element numbering | **Renumber into blocks of ten and re-fetch** | F1 folders get new names, exactly as MotoGP's did at the 2026 cutover. No migration, no legacy table for F1 |
| 3 | Authentication | **Yes — F1 gains Cloudflare Access** | Largest behaviour change for the F1 half. `AUTH_MODE=none` still preserves LAN-only behaviour |
| 4 | Containers | **One container** | One scheduler loop, one WebUI, one `/config` — so §6.1 (state collisions) must be fixed, not avoided |
| 5 | F1 `/config` | **Start fresh** | No migration of F1arr's state; the filenames collide anyway |
| 6 | Calendar storage | **Per-series files, one unified view** | `config/motogp_<year>.json` + `config/f1_<year>.json`, merged for display in one Calendar tab with an All / MotoGP / F1 filter. See §6.6 |

**Implementation has started.** Slice 1 — the series registry — and slice 2 — the
F1 declaration — are complete: `series/` holds both championships' vocabularies as
data, `motogp_common.py` binds MotoGP's from it, and 88 new tests prove nothing
changed. See §12.

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

### 6.6 One calendar file, or two? (decision 6)

This arrived as a design question — *should the calendar be one shared file with
a UI championship filter, rather than per-series files?* — so it was answered by
building the shared calendar and running the current code against it. It breaks
in three places.

**Venue names collide.** Eight venue names appear in both 2026 calendars:
Australia, Austria, Brazil, Great Britain, Hungary, Japan, Netherlands, Qatar.
The two championships number rounds from 1 independently (22 MotoGP rounds, 24 F1
rounds), so a merged file holds 46 rounds of which 8 share a venue key.

**`match_venue()` returns the first match and has no idea which series a release
came from.** With F1's rounds listed first, a MotoGP release is filed into an F1
folder:

| Release title | MotoGP rounds first | F1 rounds first |
|---|---|---|
| `MotoGP 2026 Austria 1080p VERUM` | round 15 Austria (motogp) | **round 10 Austria (f1)** |
| `MotoGP 2026 Japan 1080p VERUM` | round 16 Japan (motogp) | **round 3 Japan (f1)** |
| `MotoGP 2026 Qatar 1080p VERUM` | round 20 Qatar (motogp) | **round 23 Qatar (f1)** |

Getting it right would mean ordering the rounds in a particular way — a rule
nothing in the codebase states or tests.

**`calendar_sync._by_venue()` silently stops working.** It indexes rounds by venue
and derives its "is this the same season?" test from the venue set. In a merged
file, 46 rounds collapse to 38 keys:

```
rounds in file      : 46
distinct venue keys : 38
silently dropped    : 8
```

The check still says "same season", but it is now comparing 38 keys instead of 46
— so a refresh could overwrite or drop rounds and still pass the test the module
docstring and the docs tell users to rely on when hand-editing calendars.

**The decision.** Per-series files, one unified view. The filter is a *display*
concern and does not require shared *storage*. Per-series files keep round-number
identity unambiguous inside each file, keep `match_venue()`'s first-match-wins
behaviour correct because it only ever sees one series' rounds, keep `_by_venue()`
sound, and make F1 calendars purely additive to an existing install — no merge,
and no risk to a hand-edited MotoGP round.

The honest counter-argument, recorded rather than dismissed: a shared file is one
sync unit and one validator target, and it matches the new "Motorsportarr" name
better. If that shape is ever wanted, the cost is threading a series argument
through `match_venue`, `identify_round` and `_by_venue` and keying the venue dict
on `(series, venue)` — which is the per-series design with extra steps and a
broken intermediate state.

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
| `session_members` | 24 sessions + `UNKNOWN` | 25 sessions + `UNKNOWN` |
| `session_elements` | existing `SESSION_ELEMENTS` | new, gaps, capped at 99 |
| `session_patterns` | MotoGP patterns + `release_aliases` | F1's pattern table (ported) |
| `weekend_formats` | `motogp_standard`, `motogp_2027` | `f1_standard`, `f1_sprint` |
| `classes` | MotoGP/Moto2/Moto3 | **F1 — one class, not none** |
| `excluded_terms` | *(none)* | academy, f2, formula 2, f3, formula 3, porsche supercup |
| `asset_dirs` | `CLASS_ASSET_DIRS` (symlinks) | `f1_posters`, `f1_fanart` (real) — not yet ported |
| `search_prefix` | `{class} {season} {venue}` | `Formula1 {season} {venue}` |
| `state_prefix` | `motogp` | `f1` |
| `legacy_session_elements` | the frozen dense table | *(empty — no cutover, so no ambiguity)* |

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
its replacement exists. Status is as of 2026-11-09.

| Step | What | Status |
|---|---|---|
| **0** | Delete f1arr's dead files (`test.py`, `f1arr_webui.py.save`, `f1_webui.py`'s inline template, `f1_sprints.json`) | Not started (other repo) |
| **1** | Port F1's artwork + 5 flags into `assets/`; add `f1_posters` / `f1_fanart` asset dirs | Not started |
| **2** | Add `config/f1_2025.json` and `config/f1_2026.json` with `weekend_formats: {f1_standard, f1_sprint}` | Not started |
| **3** | Introduce the `series` registry and make `motogp_common` read its vocabulary from it. MotoGP must produce byte-identical output before and after | **DONE (2026-11-09)** |
| **4** | Namespace state files per series (`grabber_state.<series>.json` etc.) with a one-time migration that renames a single-series file | Not started — now unblocked, because `SeriesRuntime.state_file()` already produces the namespaced name |
| **5** | Per-series `plex.library_id` / `library_name` / `path_mapping` and per-series `library.<series>_path` / downloads path | Not started |
| **6** | Add the F1 series: session enum, `SESSION_ELEMENTS` in blocks of ten, pattern table, `excluded_terms` | **DONE (2026-11-09)** — `series/f1.py`; calendars and artwork are steps 1–2 |
| **7** | Merge F1's richer `f1_grab_stats.py` into the shared stats module | Not started |
| **8** | WebUI series switcher; per-series nav state | Not started |
| **9** | Run the test suite's series-specific fixtures against both series | Not started |

Steps 1–2 are pure additions and could be a PR each. Step 3 was the only
architectural one; its "no behaviour change for MotoGP" assertion is
`tests/test_series_registry.py`.

### The F1 element-numbering decision (answered: renumber and re-fetch)

MotoGParr's `SESSION_ELEMENTS` rule exists precisely because a dense table ran
out of room. Porting F1's dense 1–25 table reproduces the problem, so F1 gets
its own blocks-with-gaps table. What it actually landed as, and why, is in §12
slice 2 — the sketch below was written before the two hard constraints were
measured, and both of them changed the shape:

```
 1-3    the pre-event shows, at f1arr's own numbers
 30s FP1        40s FP2        50s FP3
 60s Sprint Qualifying, Sprint, then Qualifying
 70s Race, Race Uncut, Full Event
 76-88  the weekend's programming, gaps of one
```

**Consequence:** every existing F1 folder on disk gets a new name. MotoGParr has
already been through exactly this once and chose to delete and re-fetch the
library rather than migrate it (issue #33, `LEGACY_ELEMENT_SEASON = 2025`); the
same choice applies, and the user has confirmed it. F1 therefore declares **no
legacy table and no cutover season** — `legacy_session_elements` is empty and
`legacy_element_season` is `None`, so every season uses the one scheme and no F1
number can ever be ambiguous.

### Naming convention for F1 (follows from decision 2)

F1arr names a session folder `{prefix} {venue} {session}`; MotoGParr names it
`{prefix} {class} {short session}`. The hybrid adopts **MotoGParr's**, because the
round folder already carries the venue and the folder name is what Plex shows as
the episode title — "Formula 1 Race" is more informative than "Australia Race".
F1 folders change shape either way, so there is no extra cost.

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

## 10. Questions — answered in §0

All five are decided; see the table at the top of this document. They are kept
here as a record of what was open when the investigation was written, and
because each one still has follow-up work that is not done:

- **Name.** The repo is already `msportarr`. The app still calls itself
  MotoGParr in the Dockerfile labels, the Unraid template, the README and
  `entrypoint.sh`'s banner. That rename is a mechanical slice of its own and is
  deliberately not bundled into the series work.
- **Authentication.** Decided yes. `utils/auth.py` and `utils/web_auth.py` are
  already series-agnostic, so F1 inherits them for free once the WebUI is
  series-aware.
- **Containers.** Decided one. The entrypoint currently starts exactly two
  processes (WebUI, scheduler); a second series changes how many calendars the
  scheduler evaluates per pass, not how many processes run.
- **F1 `/config`.** Decided fresh. Nothing to migrate.

---

## 12. Progress log

### Slice 1 — the series registry (2026-11-09, DONE)

**What changed**

```
series/__init__.py     registry + accessors; imports every series module
series/base.py         Series / ClassSpec / SessionPatternSpec declarations,
                       SeriesRuntime (materialisation) and the numbering rules
series/motogp.py       MotoGP's entire vocabulary, moved verbatim
motogp_common.py       now BINDS its enums and tables from the registry
tests/test_series_registry.py   38 tests
Dockerfile             COPY series/ ./series/
```

**What the registry owns** (moved out of `motogp_common.py`): the `RaceClass` and
`SessionType` enums, `SESSION_ELEMENTS` and `SESSION_ELEMENTS_LEGACY`,
`SESSION_DISPLAY_NAMES`, `SESSION_SHORT_NAMES`, `SESSION_RACE_CLASS`,
`CORE_SESSIONS`, `FEATURE_RACE_SESSIONS`, `DEFAULT_DESIRED_SESSIONS`,
`SESSION_PATTERNS`, `CALENDAR_KEY_TO_SESSION`, `SESSION_TO_CALENDAR_KEY`,
`SESSION_TO_MOTOGP_SESSION_NAME`, `MOTOGP_SESSION_NAME_TO_TYPE`,
`CALENDAR_KEY_TO_DAY`, `CLASS_ASSET_DIRS`, and the numbering functions
(`element_number`, `legacy_element_number`, `_element_tables`,
`get_session_from_element`, `element_is_ambiguous`, `build_prefix`,
`parse_prefix`, `find_legacy_folders`).

**What deliberately did *not* move.** The algorithms stay in `motogp_common.py`:
`classify_session`, `detect_race_class`, `_remap_session_for_class`,
`filter_releases`, `explain_release`, `get_session_times`,
`round_runs_session`, `validate_calendar`. They read the series' vocabulary
through the module-level names they always used, so they are unchanged and will
be parameterised in a later slice once there is a second series to parameterise
against. Moving an algorithm before its second caller exists is how you get an
abstraction shaped by one example.

**Proof it is a no-op.** 662 tests passed before and after, and 38 new tests
assert the registry's tables are *identical objects* to what the spine binds,
that every element number round-trips through `build_prefix`/`parse_prefix` for
2025/2026/2027, that the numbering is chronological for both the 2026 and 2027
weekend formats, and that a number the two schemes disagree about is refused
rather than guessed.

**One API rename, with an alias.** `SessionType.motogp_session_name` is now
`format_session_name` (the concept is not MotoGP-specific); the old name is kept
as a property alias so `motogp_common.py` and `utils/validate_calendar.py` are
untouched.

**What is now cheap.** `SeriesRuntime.state_file("grabber_state")` already
returns `grabber_state.motogp.json`, so step 4 is a rename at the call sites
rather than a design problem. A second series is one new file in `series/` plus
its calendars and artwork.

---

### Slice 2 — the F1 series declaration (2026-11-09, DONE)

**What changed**

```
series/f1.py               Formula 1's entire vocabulary, ported from f1arr
series/__init__.py         imports series.f1, so it registers
tests/test_f1_series.py    50 tests
```

`motogp_common.py` is untouched. F1 is a declaration and nothing else, so the
MotoGP app is unaffected by its presence — which is the whole point of the
registry, and the reason this slice could land without touching a single line of
the working app.

**Proof it is a faithful port.** `f1arr/f1_common.py` was parsed with `ast` and
every table compared against the declaration: the 26-member enum and its values,
26 display names, 7 core sessions, 25 default-desired flags, all 25 session
patterns (session, priority, and every `must_have`/`must_not_have` term), the
7 calendar-key mappings, the 7 key-to-day entries, and the junior-series
exclusion list. Zero values dropped.

Three deliberate differences, each recorded in a comment at the site:

1. `UNKNOWN`'s label is **"Unknown"**, not f1arr's "Unknown Session" — the
   registry's convention, set by `motogp.py`. A combined UI with an All / MotoGP
   / F1 filter would otherwise show two labels for one state.
2. The patterns and exclusions gain **dotted variants** (`ted's.notebook`,
   `formula.2`). f1arr's `classify_session()` rewrites `[.\-_]+` to spaces before
   matching and MotoGP's does not, so a pattern with only the spaced form is
   unreachable under one of the two normalisations. The data now works under
   either; the shared spine will pick one rule.
3. `excluded_terms` is **data in the declaration**, where f1arr hardcoded it
   inside `classify_session()` as `global_exclusions`. It is per-series, which
   matters: "f2" is a junior series to exclude for F1 and meaningless to MotoGP.

**Two bugs found while writing the tests, both fixed, both now guarded.**

The first draft of the element table was wrong twice over, and both errors were
invisible to a prefix round-trip test because a round trip only compares against
what `build_prefix` produced.

*It ran past the two-digit prefix.* Allocating the support shows into the 80s at
gaps of two reached 110. `_PREFIX_RE` is `(\d{2})R(\d{2})E(\d{2})`, so
`26R07E100` parsed as `E10` — Practice 1 — and `26R07E110` did not parse at all.
Fixed by re-allocating under 99, and guarded by
`test_every_element_number_fits_the_two_digit_prefix`, which walks **every**
series.

*It silently misread old F1 folders.* `session_members` *is* f1arr's original
dense table — it wrote the enum value straight into the folder name, so E10 was
Sprint Qualifying. Four of the new numbers (3, 5, 10, 20) named a *different*
session under the new scheme, so `26R02E10 Sprint Qualifying` would have been
read back as Practice 1 with nothing reporting it. Fixed by leaving 4–29
deliberately unresolvable and reusing only 1, 2 and 3 — the pre-event shows'
original numbers, so an old folder still resolves to the session it always
named. Guarded by `test_no_old_number_is_silently_misread`, which checks every
number 1–99.

**The resulting numbering.**

```
 1-3    the pre-event shows, at f1arr's own numbers
 30s FP1        40s FP2        50s FP3
 60s Sprint Qualifying, Sprint, then Qualifying
 70s Race, Race Uncut, Full Event
 76-88  the weekend's programming, in the order a viewer watches it
```

The 76+ block uses gaps of **one**, not two, because thirteen programmes do not
fit in 76–99 at gaps of two. Eleven slots remain free. F1's support-show family
is what forces this: MotoGP has one such session and F1 has sixteen, so the two
series' tables do not have the same shape and pretending otherwise would have
produced a table that quietly ran off the end.

**What is deliberately not here.** No F1 calendars (`config/f1_*.json`), no F1
artwork (86 MB of per-venue JPEGs — a packaging decision, not a vocabulary one),
and no parameterised algorithms. `classify_session`, `filter_releases` and
`get_session_times` still live in `motogp_common.py` and still read MotoGP's
vocabulary through the module-level names they always used; they get a `series`
argument when there is a second caller to shape it against.

---

## 13. What was *not* done

No code was changed in either repository. `f1arr` was cloned read-only into
`/home/user/f1arr` for comparison and is not part of this repository. The
MotoGParr test suite was run once to confirm the baseline: **662 passed**.
