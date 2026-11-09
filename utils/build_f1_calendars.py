#!/usr/bin/env python3
"""
Port F1arr's season calendars to the shared calendar schema.

One-off, run against a checkout of https://github.com/dougalbob/f1arr::

    python3 utils/build_f1_calendars.py --source /home/user/f1arr
    # writes config/f1_2025.json and config/f1_2026.json

It is committed rather than thrown away for the same reason
``build_calendar_2026.py`` is: the *transformation* is the interesting part, and
a reviewer needs to be able to see that the port changed only the shape. Every
rule below is a shape change, never a value change - if a round's venue, date,
flag or artwork filename differs from f1arr's, this script is wrong.

The schema it produces is MotoGP's, generalised
------------------------------------------------

f1arr and MotoGParr describe a season differently in exactly four places. Three
of them are f1arr being *older*, and one is a real difference of vocabulary:

1. **Weekend format.** f1arr has ``sprint_weekend: true/false`` per round and
   derives the session list in code (``get_excluded_sessions_for_round``).
   MotoGP has a ``weekend_formats`` block plus a per-round ``weekend_format``
   name, and cross-checks the two against the round's timetable in both
   directions. MotoGP's model is strictly more expressive - the format is data -
   so F1 becomes two entries, ``f1_standard`` and ``f1_sprint``. The boolean is
   *not* carried over: two descriptions of one fact drift, and the validator
   would then have to know which one wins.

2. **The declared session list** is ``motogp_sessions`` in MotoGP and does not
   exist in f1arr. The name is the only MotoGP-specific thing about it, so the
   generic key is ``<series>_sessions`` = ``f1_sessions``. See
   ``docs/CALENDAR_CHANGES.md`` for why the value is not simply
   ``sessions``.

3. **Session keys are class-prefixed.** MotoGP's timetable uses
   ``motogp_fp1``/``moto2_fp1``.. because three classes share a weekend; f1arr
   uses bare ``fp1``. The app resolves a key through ``CALENDAR_KEY_TO_SESSION``
   and derives the timetable's display label from the part after the first
   underscore, so a prefixed key needs no code change at all. F1 gets ``f1_fp1``
   .. ``f1_race``. This is what makes the pre-existing one-class-per-key lookup
   work unchanged for a one-class series.

4. **Artwork is per class**, not per round. f1arr's flat ``fanart``/``poster``
   become ``assets: {"f1": {"fanart": ..., "poster": ...}}``, which is the shape
   ``motogp_sort.apply_round_artwork()`` already reads.

Two references in f1arr point at files that do not exist (``fanart_lasvegas``,
``fanart_usa_cota_f1``); the artwork it actually ships under the other name is
used and the substitution is reported. Nothing is renamed on disk, so the
calendar stays honest about which file it uses.
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: f1arr's weekend shape, expressed as MotoGP-style format names.
FORMAT_FOR_SPRINT = {False: "f1_standard", True: "f1_sprint"}

#: The generic session list each format runs, in championship order.
SESSIONS_BY_FORMAT = {
    "f1_standard": ["fp1", "fp2", "fp3", "qualifying", "race"],
    "f1_sprint": ["fp1", "sprint_qualifying", "sprint", "qualifying", "race"],
}

WEEKEND_FORMATS = {
    "f1_standard": {
        "friday": [
            {"session": "fp1", "label": "FP1", "duration_mins": 60},
            {"session": "fp2", "label": "FP2", "duration_mins": 60},
        ],
        "saturday": [
            {"session": "fp3", "label": "FP3", "duration_mins": 60},
            {"session": "qualifying", "label": "Qualifying", "duration_mins": 60},
        ],
        "sunday": [
            {"session": "race", "label": "Race", "duration_mins": 120},
        ],
    },
    # The sprint format is not the standard one with sessions swapped: FP2 and
    # FP3 genuinely do not run. It is a different weekend, which is why it is a
    # different format rather than a flag on one.
    "f1_sprint": {
        "friday": [
            {"session": "fp1", "label": "FP1", "duration_mins": 60},
            {"session": "sprint_qualifying", "label": "Sprint Qualifying",
             "duration_mins": 45},
        ],
        "saturday": [
            {"session": "sprint", "label": "Sprint", "duration_mins": 30},
            {"session": "qualifying", "label": "Qualifying", "duration_mins": 60},
        ],
        "sunday": [
            {"session": "race", "label": "Race", "duration_mins": 120},
        ],
    },
}

#: f1arr ships these and references them under a second name. Keyed by what the
#: calendar says, valued by what is on disk.
DANGLING_ARTWORK = {
    "fanart_lasvegas.jpg": "fanart_usa_las_vegas_f1.jpg",
    "poster_lasvegas.jpg": "poster_usa_las_vegas_f1.jpg",
    "fanart_usa_cota_f1.jpg": "fanart_usa_austin_cota_f1.jpg",
    "poster_usa_cota_f1.jpg": "poster_usa_austin_cota_f1.jpg",
}

#: f1arr's own release vocabulary, kept as data because the shared classifier
#: reads ``release_aliases`` from the calendar rather than from the series
#: declaration (a token that is ambiguous in one season is a per-season fact).
#: ``SQ``/``SPQ`` are the two short forms F1 releases actually use for Sprint
#: Qualifying; they are also what the sprint weekend introduced.
RELEASE_ALIASES = {
    "sq": "sprint_qualifying",
    "spq": "sprint_qualifying",
}

KEY_ORDER = (
    "round", "venue", "official_name", "circuit", "aliases", "date",
    "weekend_start", "weekend_end", "status", "status_note", "round_aliases",
    "classes", "weekend_format", "f1_sessions", "folder", "local_timezone",
    "utc_offset", "sessions", "search_delay_hours", "assets", "flag",
)


def _ordered(entry: dict) -> dict:
    """Emit the keys in the same order MotoGP's calendars use.

    Same order in both files means a diff of the two calendars shows *values*,
    not key churn - which is the only way a reviewer can spot a real change.
    """
    ordered = {key: entry[key] for key in KEY_ORDER if key in entry}
    for key, value in entry.items():        # anything unanticipated survives
        ordered.setdefault(key, value)
    return ordered


def _prefix_session_keys(sessions: dict) -> dict:
    """``{"friday": {"fp1": {...}}}`` -> ``{"friday": {"f1_fp1": {...}}}``."""
    return {
        day: {f"f1_{key}": value for key, value in (day_sessions or {}).items()}
        for day, day_sessions in (sessions or {}).items()
    }


def build_round(entry: dict, season: int, problems: list) -> dict:
    label = f"{season} round {entry.get('round')} {entry.get('venue')}"

    sprint = bool(entry.get("sprint_weekend", False))
    fmt_name = FORMAT_FOR_SPRINT[sprint]

    artwork = {}
    for kind in ("fanart", "poster"):
        filename = entry.get(kind)
        if not filename:
            continue
        if filename in DANGLING_ARTWORK:
            replacement = DANGLING_ARTWORK[filename]
            print(f"    {label}: {filename} is not on disk, using {replacement}")
            filename = replacement
        artwork[kind] = filename

    result = {
        "round": entry.get("round"),
        "venue": entry.get("venue"),
        "flag": entry.get("flag"),
        "aliases": list(entry.get("aliases") or []),
        "date": entry.get("date"),
        "status": "scheduled",
        "status_note": None,
        "round_aliases": [],
        "classes": ["f1"],
        "weekend_format": fmt_name,
        "f1_sessions": SESSIONS_BY_FORMAT[fmt_name],
        "folder": entry.get("folder"),
        # f1arr's 2025 file carried no timetable. Keep that: inventing session
        # times a year after the fact would be fabricating data, and the app
        # already distinguishes "no timetable" from "incomplete timetable".
        **({"sessions": _prefix_session_keys(entry["sessions"])}
           if entry.get("sessions") else {}),
        "assets": {"f1": artwork} if artwork else {},
    }

    for key in ("official_name", "circuit", "local_timezone", "utc_offset",
                "weekend_start", "weekend_end", "search_delay_hours"):
        if entry.get(key) is not None:
            result[key] = entry[key]

    # Cross-check what the port produced against what f1arr declared: the
    # timetable must contain exactly the sessions the format says it runs.
    if entry.get("sessions"):
        present = {k.split("_", 1)[1] for d in result["sessions"].values()
                   for k in d}
        expected = set(SESSIONS_BY_FORMAT[fmt_name])
        if present != expected:
            problems.append(
                f"{label}: timetable has {sorted(present)} but "
                f"{fmt_name} runs {sorted(expected)}"
            )
        if sprint and "sprint_weekend" not in entry:
            problems.append(f"{label}: timetable has sprint sessions but "
                            f"f1arr did not mark it a sprint weekend")
    if result["folder"] != f"Round {result['round']} {result['venue']}":
        problems.append(
            f"{label}: f1arr folder {result['folder']!r} is not "
            f"'Round <n> <venue>'; the shared sorter builds folder names from "
            f"the round and venue, so this would be a second naming scheme"
        )

    return _ordered(result)


def build_calendar(source: Path, season: int) -> dict:
    path = source / "config" / f"f1_{season}.json"
    if not path.exists():
        raise SystemExit(f"no f1arr calendar at {path}")

    original = json.loads(path.read_text(encoding="utf-8"))
    problems = []

    rounds = [build_round(entry, season, problems) for entry in original["rounds"]]

    calendar = {
        "season": season,
        "series": original.get("series", "Formula 1 World Championship"),
        "classes": ["f1"],
        "timezone_note": original.get(
            "timezone_note",
            "All session times are in GMT/UTC. Local times vary by venue.",
        ),
        "your_timezone": original.get("your_timezone", "GMT+0"),
        "weekend_formats": WEEKEND_FORMATS,
        "release_aliases": RELEASE_ALIASES,
        "rounds": rounds,
    }

    if problems:
        raise SystemExit(
            "the port would have changed the calendar's meaning:\n  "
            + "\n  ".join(problems)
        )
    return calendar


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True,
                        help="checkout of the f1arr repository")
    parser.add_argument("--seasons", type=int, nargs="+", default=[2025, 2026])
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "config")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    for season in args.seasons:
        print(f"{season}:")
        calendar = build_calendar(args.source, season)
        target = args.out / f"f1_{season}.json"
        target.write_text(
            json.dumps(calendar, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        sprints = [r["round"] for r in calendar["rounds"]
                   if r["weekend_format"] == "f1_sprint"]
        print(f"    wrote {target} - {len(calendar['rounds'])} rounds, "
              f"{len(sprints)} sprint weekends {sprints}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
