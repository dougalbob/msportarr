# Calendar changes, cancellations and weekend formats

MotoGP calendars change after they are published: races get postponed, dropped,
added, and the race weekend format itself gets revised. This document describes
how the code is built so those changes are **data edits, not code edits**, and
what to do when one happens.

The 2026 Qatar postponement is the worked example throughout.

---

## 1. The one rule that matters: round numbers are part of your filenames

Every session folder on disk and every file in Plex is named from a prefix:

```
26R20E21 MotoGP Race
│  │  │  └── E21 = session number (MotoGP Race)
│  │  └───── R20 = round number
│  └──────── 26  = season
└─────────── folder: "Round 20 Qatar"
```

That round number is simultaneously:

* a **sort key** so Plex lists sessions in running order, and
* the **unique identity** of the episode inside Plex.

So renumbering a round means renaming the folder, the session folder and the
video file. Plex treats a renamed file as a *new* item: it loses its watched
state, and if the library is scanned while the old and new paths both exist you
can end up with duplicates.

**Therefore: decide the numbering for a season once, before you download
anything.** If you must renumber mid-season, see §7.

Folder names are `Round NN Venue` with a zero padded number (`Round 20 Qatar`).
That padding matters: the library scanner identifies a round by the number in
the folder name, so `Round 05 France` is France even when the calendar now has a
different round at 05.

---

## 2. What lives in the calendar file

`config/<series>_<season>.json` - one file **per series**, never one shared file
(§9 says why a merged calendar cannot work). Each round carries:

| Field | Meaning |
|---|---|
| `round` | Championship round number. Drives folders, prefixes, search queries, Discord, Plex collections. |
| `status` | `scheduled` (default), `cancelled`, `postponed` or `replaced`. Inactive rounds are skipped everywhere. |
| `status_note` | Free text explaining the status, e.g. why Qatar moved. |
| `round_aliases` | Other numbers this event has been known by, so release searches still find uploads named to the original calendar. |
| `weekend_format` | Key into the top level `weekend_formats` block. |
| `<series>_sessions` | Which feature-class sessions this weekend actually runs: `motogp_sessions`, `f1_sessions`. |
| `classes` | Classes racing (`motogp`, `moto2`, `moto3`; `f1`). Leave out a class for a MotoGP-only event. |
| `sessions` | Per-day session times, `local` and `gmt`, with `duration_mins`. |
| `folder` | Library folder name, always `Round NN Venue` (zero padded). |
| `assets` / `flag` | Artwork and flag files. |

`weekend_formats` is shared by the whole season, so a format change is described
once. Both the 2026 format (`motogp_standard`) and the 2027 format
(`motogp_2027`) are already defined in `config/motogp_2026.json` as a template.

### Session names used by `<series>_sessions`

`fp1`, `practice`, `fp2`, `fp3`, `qualifying`, `sprint_qualifying`, `sprint`,
`warmup`, `race`.

These are release-name vocabulary, not enum values, so the same names work
across format revisions. `qualifying` covers both Q1 and Q2 (`MOTOGP_Q1` and
`MOTOGP_Q2`).

The key is prefixed rather than a bare `sessions` because `sessions` is already
the round's *timetable* (`{"friday": {"motogp_fp1": {...}}}`). Two meanings for
one word in one file is the kind of thing a hand edit gets wrong once and then
nobody trusts the validator again.

### The two series' vocabularies

| | MotoGP | Formula 1 |
|---|---|---|
| Classes | `motogp`, `moto2`, `moto3` | `f1` |
| Calendar keys | `motogp_fp1`, `moto3_race`, ... | `f1_fp1`, `f1_race`, ... |
| Formats | `motogp_standard`, `motogp_2027` | `f1_standard`, `f1_sprint` |
| Session list | `motogp_sessions` | `f1_sessions` |
| Desired sessions by default | the timed sessions | the timed sessions **plus** Ted's Notebook, Jolyon Palmer's Analysis, the press conference and the pre-race show |

Calendar keys are **class-prefixed in both series**, including the one-class one.
The WebUI derives a timetable label from the part after the first underscore, so
the prefix costs nothing and gives one key namespace across a combined UI - and
it is already there if a support series ever appears at an F1 weekend.

`f1_standard` runs FP1/FP2/FP3/Qualifying/Race; `f1_sprint` runs
FP1/Sprint Qualifying/Sprint/Qualifying/Race. They are two formats rather than
one format with a flag because FP2 and FP3 genuinely do not run at a sprint
weekend - it is a different weekend, not a different label.

A session the weekend format **has no name for** is not described by it, and
`round_runs_session()` says so rather than answering "not run". That is what
keeps F1's support shows (which never appear in `f1_sessions`) searchable.

---

## 3. How the code uses it

| Behaviour | Helper (in `motogp_common.py`) |
|---|---|
| Skip cancelled/postponed rounds entirely | `is_round_active()` |
| Skip sessions a weekend does not run (e.g. no warm-up in 2027) | `round_runs_session()` |
| Which classes are racing | `get_round_classes()` |
| The format definition for a round | `get_weekend_format()` |
| Which sessions a round runs, for any series | `round_session_list()` |
| Sanity check a hand-edited calendar | `validate_calendar()` |

`utils/validate_calendar.py` names no championship: it discovers
`config/<prefix>_<year>.json` from the declarations in `series/` and reads the
keys, format names and artwork directories from them. `python3
utils/validate_calendar.py` therefore validates every calendar of every series,
and `--series` / `--season` narrow it.

Wired into:

* `motogp_grabber.py` - scheduled mode, upcoming list and backfill all skip
  inactive rounds and non-existent sessions, so nothing hunts for a race that
  will never happen.
* `motogp_scheduler.py` - a cancelled round no longer triggers "race weekend"
  mode on its old dates.
* `motogp_webui.py` - the calendar shows round status and the weekend format;
  library status only counts sessions that are actually run.
* `tests/test_calendar_formats.py` - locks all of this down.

---

## 4. Worked example: the 2026 Qatar postponement

Qatar was round 4 (10-12 April). Because of the conflict in the Middle East it
was postponed to 6-8 November, which renumbered everything after it:

```
        before                                after (official 2026)
 1 Thailand                              1 Thailand
 2 Brazil                                2 Brazil
 3 Americas                              3 Americas
 4 Qatar (April)   <-- postponed        4 Spain      <- was 5
 5 Spain           ...                  5 France     <- was 6
 ...                                     ...
20 Malaysia                            19 Malaysia   <- was 20
21 Portugal                            20 Qatar      (November)
22 Valencia                            21 Portugal
                                       22 Valencia
```

Result: Qatar is `Round 20 Qatar`, 6-8 November, and the November triple header
is Australia (18) -> Malaysia (19) -> Qatar (20).

Its published timetable is stored with the real Lusail session times (local
`Asia/Qatar`, UTC+3), including the Q1/Q2 split inside `motogp_qualifying`, and
`round_aliases: [4]` keeps "Round 4" uploads findable.

Rebuild/regenerate this calendar with:

```bash
python3 utils/build_calendar_2026.py --stdout   # preview
python3 utils/build_calendar_2026.py            # write config/motogp_2026.json
```

---

## 5. Checklist for the next calendar change

1. **Decide the numbering first.** Prefer official championship numbering. If a
   race is postponed to later in the season, MotoGP renumbers, so the rounds in
   between shift by one.
2. **Decide how it reaches your container.** Calendars in `/config` are not
   replaced by an image update. Either copy the new file in yourself, or rely on
   the startup check (see §8) which refreshes it automatically when it
   recognises the same season, keeping a `.bak`.
3. **Edit the JSON** (or the builder script) - set `round`, `folder`, `date`,
   `weekend_start`, `weekend_end`, `sessions`, plus `status`, `status_note` and
   `round_aliases` where relevant.
4. **Validate:**

   ```bash
   python3 utils/validate_calendar.py --season 2027
   python3 utils/validate_calendar.py --season 2027 --library /path/to/MotoGP
   ```

   The `--library` check is the important one: it compares round folders on disk
   against the calendar and catches a round whose number moved, which is what
   makes the library scan think a new round is already complete.

5. **Clear stale state** for rounds that moved, so the grabber re-searches:

   ```bash
   python3 motogp_grabber.py clear --session 2026_R04_E21
   ```

6. Run a dry run of the affected mode before the weekend:

   ```bash
   python3 motogp_grabber.py scheduled --dry-run --verbose
   ```

---

## 6. If you really do need to renumber mid-season

Renaming is not required by the code - nothing depends on the numbers being
*consecutive* or on matching the championship. Only the round number in the JSON,
the folder name and the `R<NN>` prefix have to agree.

If you renumber anyway, do it in this order:

1. **Empty the Plex recycle bin / disable automatic trash emptying.**
2. Export watched state per file path (Plex API: `viewCount`, `lastViewedAt`).
3. Rename round folders, session folders and files, all through a mapping, and
   keep a log so it can be reversed.
4. Trigger a scan, wait for it to finish, *then* restore watched state by path.
5. Re-apply tags/collections (`python3 motogp_plex_tagger.py --all`).

An alternative that avoids all of this: leave the existing folders alone and
give the new races the next unused numbers, with `round_aliases` recording the
official number. `round` is a library key, not a law - but it is shown to you in
Discord and Plex collections, so it is a trade-off.

---

## 7. The 2027 MotoGP weekend format

2027 changes the MotoGP weekend (`motogp_2027` in `weekend_formats`):

| Day | Session | 2026 | 2027 |
|---|---|---|---|---|
| Fri | FP1 | 45m | 45m |
| Fri | Practice | 60m, top 10 into Q2 | **gone** |
| Fri | FP2 | Sat, 30m | **Fri, 30m** |
| Fri | Sprint Qualifying | - | **new, 15m** (sets Sprint grid) |
| Sat | FP3 | 30m | **20m** |
| Sat | GP Qualifying | 30m, top 10 + Q1 top 2 | **Q1 15m all riders, Q2 15m top 12** |
| Sat | Sprint | 25m, grid from GP qualifying | 25m, **grid from Sprint Qualifying** |
| Sun | Warm-up | 10m | **removed** |
| Sun | Race | grid from GP qualifying | unchanged |

What the code does with that:

* **No warm-up hunting.** `round_runs_session()` sees `warmup` missing from
  `motogp_sessions` and the grabber/scheduler skip it. No more "no candidates"
  failures every Sunday morning.
* **Sprint Qualifying is classified correctly.** `Sprint Qualifying` is matched
  before `Sprint` (which now excludes anything containing `qualifying`/`quali`),
  so it is not mistaken for the Sprint race or for GP qualifying.
* **FP3 is its own session** (`MOTOGP_FP3`), matched before FP2/FP1.
* **Friday FP2** maps to the existing `MOTOGP_FP2`. In 2026, `FP2` only ever
  appears on Saturday, so there is no collision with the old `Practice`.
* **Support classes are untouched** - the format only applies to MotoGP.

### Session numbers: allocated in blocks, not appended

The number in `E<NN>` is part of the identity, so it cannot simply be appended
whenever a session appears. The old numbering was dense (1-24, no free slots),
which forced FP3 and Sprint Qualifying onto the end as 23 and 24 - *after* the
Race at 21. A 2027 round therefore listed FP3 and Sprint Qualifying after the
Grand Prix in every folder and title listing, and no display change could fix
that without renaming files.

`SESSION_ELEMENTS` in `motogp_common.py` now allocates in **blocks of ten**:

```
10s FP1   20s Practice   30s FP2 / Sprint Qualifying   40s FP3
50s Q1    60s Q2         70s Sprint   78 Warm-up   80s Races   90 Chequered
```

This is chronological for the 2026 weekend, the 2027 weekend and both support
classes, and it leaves 50+ free slots, so a future session is a new number in a
gap - never a renumber. `tests/test_session_numbering.py` asserts those two
properties (chronological, collision-free with room to insert) rather than
recording the current values, so a future format change cannot be mis-numbered
silently.

**The cutover is now, not at the 2027 boundary.** `LEGACY_ELEMENT_SEASON` marks
the last season written with the dense numbering. 2025 and 2026 library content
is being deleted and re-fetched under the new scheme, so there is exactly one
numbering from 2026 onwards. `SESSION_ELEMENTS_LEGACY` is frozen and is used
only to *read* older seasons; it never names a file, and it exists so leftovers
are recognised rather than misparsed.

Two consequences worth knowing:

* The two schemes share numbers - old `E24` was Sprint Qualifying, new `E24` is
  Moto2 Practice - and their folder names have an identical shape, so a lookup
  with no season **refuses** rather than guessing.
* `find_legacy_folders()` names leftovers the current scheme cannot read, and the
  library scan warns that they will be treated as missing. It cannot detect a
  stale number that is valid now, which is exactly why the media is removed
  rather than converted in place.

### What the user sees

The on-disk name and the Plex title are deliberately the same short label:

```
26R20E22 MotoGP Practice      (2026 - the session really is called Practice)
27R02E32 MotoGP FP2           (2027 - the Friday session is no longer Practice)
27R02E36 MotoGP Sprint Qualifying
```

`SessionType.full_short_name` produces that label, and `utils/plexlabels.py`
writes it to Plex as the item `title` **together with** `titleSort` (the
`YYRREE` prefix). Both are needed: title alone would make every "MotoGP FP2" in
the season sort together. Keeping the folder name identical to the Plex title
means a library re-scan, which re-derives the title from the filename, is a
no-op instead of quietly reverting to the long name.

To switch a season to the 2027 format, set `"weekend_format": "motogp_2027"`
and the matching `motogp_sessions` list on each MotoGP round, and give the
sessions block the right keys (`motogp_fp3`, `motogp_sprint_qualifying`, no
`motogp_warmup`). The tests already simulate this.

### Not yet proven end to end

A real 2027 calendar now exists (`config/motogp_2027.json`, 22 provisional
rounds) and validates clean; the WebUI renders a 2027 round in running order.
The session times in it are provisional and must be replaced with the published
circuit timetable. The Plex title/sort behaviour still depends on the object type
an "Other Videos" section returns - see `/diagnostics` → `plex_labels`. Treat the
first 2027 weekend as the verification.

Release vocabulary is data too: the 2027 calendar carries `release_aliases`
(`sq`/`spq` → `sprint_qualifying`), so a group that shortens the session name can
be taught without a code release. An ambiguous title (`SQ` without an alias, a
bare `FP`) is refused rather than filed as the Race; use
`python -m motogp_common --report-unknown --season 2027` to see how a candidate
would be classified before the weekend.

---

## 8. How a calendar reaches your container

Season calendars are baked into the image at `/app/config/`, but the running app
reads them from `/config`, a persistent volume. The very first start copies them
across; after that they are yours.

That is the gap that made the 2026 Qatar fix easy to miss: the image contained
the corrected calendar, but `/config/motogp_2026.json` still held the April one,
and nothing said so.

`entrypoint.sh` now reconciles them on every start using
`utils/calendar_sync.py`:

```
  Calendars (auto):
    = motogp_2025.json: up to date
    ^ motogp_2026.json: updated from image
        dates changed: Qatar 2026-04-12 -> 2026-11-08; 19 rounds renumbered
        previous copy saved as motogp_2026.json.bak
```

Rounds are compared **by venue**, not by round number. When a championship is
renumbered almost every round changes number, which would produce an unreadable
diff; keying on venue turns the Qatar postponement into one clear line.

What it will and will not do:

| Situation | `auto` behaviour |
|---|---|
| Config equals the image | nothing |
| Same venues, dates/numbers differ (postponement, renumbering) | refresh, keep `.bak` |
| Venues differ (different championship under the same filename) | warn, do not touch |
| You hand-edited a calendar | refresh and keep `.bak` - use `warn` to prevent this |

Override with `MOTOGPARR_CALENDAR_UPDATE=warn` or `=off`.

## 9. Known limits

* **Mid-week schedule changes.** Session times are read from the JSON; if a
  session moves by an hour on the day, edit the JSON rather than relying on the
  search delay.
* **Renumbering.** Folders on disk win: the library scan reads the round number
  from the folder name, so stale folders must be removed (see §5 step 4).
* **`E<NN>` is permanent.** A session type's number can never be reused or
  renumbered without orphaning files.
* **Season discovery is automatic.** `motogp_webui.py` lists any
  `config/motogp_<year>.json`, so adding 2027 is a file drop, not a code change.
  `motogp_grabber.py status --season 2027` works the same way.
* **F1 2025 has no timetable.** f1arr never carried session times for that
  season, and inventing them a year later would be fabricating data. The app
  treats "no timetable" as backfill-by-date, the same way it treats MotoGP 2025,
  and the validator does not report the missing times as problems.
* **F1 sprint rounds come from the calendar, not `f1_sprints.json`.** f1arr
  shipped both, and they disagreed on three 2026 rounds in each direction. The
  sidecar was not ported.
