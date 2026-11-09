#!/usr/bin/env python3
"""
Calendar validator
==================

Run this after editing any ``config/<series>_<season>.json`` - especially after a
cancellation, postponement or reschedule, which is exactly when hand editing
goes wrong.

It checks the things that silently break the grabber or the Plex library:

    * duplicate / missing round numbers
    * folder names that disagree with the round number
    * unparseable or out-of-order dates
    * rounds with no session times (2026+ calendars only)
    * session keys the code does not understand
    * asset files referenced but not present on disk
    * local/GMT times that do not agree with the round's utc_offset
    * round numbers that no longer match what is already on disk

Nothing here names a championship. The session keys, the format names, the
artwork directories and the release-alias vocabulary all come from the series
declaration in ``series/``, reached through the registry - which is why adding
Formula 1 needed no new rule, only a second declaration.

Usage:
    python3 utils/validate_calendar.py                       # every calendar
    python3 utils/validate_calendar.py --series f1
    python3 utils/validate_calendar.py --season 2026
    python3 utils/validate_calendar.py --series motogp --season 2026 \\
                                       --library /path/to/MotoGP --strict
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from motogp_common import (  # noqa: E402
    CALENDAR_DAYS,
    get_round_status,
    has_session_times,
    is_round_active,
    round_session_list,
    validate_calendar,
)
from series import all_series, get_series, runtime  # noqa: E402

CONFIG_DIR = REPO_ROOT / "config"

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _add_minutes(hhmm: str, minutes: int) -> str:
    h, m = map(int, hhmm.split(":"))
    total = (h * 60 + m + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _offset_minutes(utc_offset: str) -> int:
    sign = -1 if utc_offset.startswith("-") else 1
    oh, om = map(int, utc_offset.lstrip("+-").split(":"))
    return sign * (oh * 60 + om)


def feature_class(spec):
    """
    The series' top class - the one whose weekend format a calendar describes.

    This used to be the literal ``RaceClass.MOTOGP``. It is the class the
    ``weekend_formats`` block speaks for: Moto2 and Moto3 run their own
    schedules and are deliberately *not* gated by it, which is why the check
    below filters rather than requiring every key to appear in a format.

    It comes from the declaration's class order (declared so that the feature
    class is last, because ``order`` is the weekend running order) rather than
    from a series-key test, so a second one-class series needs no change here.
    """
    return max(spec.classes, key=lambda declared: declared.order)


def _keys_for_format_name(rnd, name: str) -> set:
    """Calendar keys that carry a given weekend-format session name.

    The bridge between a format (which speaks ``fp1``) and a round's timetable
    (which speaks ``f1_fp1``). Derived from the declaration, where it used to be
    a hand-written MotoGP-only table in this file - a second copy of a mapping
    the registry already owns, which is exactly how the two drift apart.

    Empty for a name no calendar key carries (``chequered_flag`` is a release
    pattern, not a scheduled slot), and callers skip those.
    """
    return {
        key for key, session_type in rnd.calendar_key_to_session.items()
        if session_type.format_session_name == name
    }


def check_round_format(entry: dict, calendar: dict, spec=None) -> list:
    """
    Cross-check a round against the weekend format it claims to use.

    The three 2027 misconfigurations this exists to catch all passed the old
    validator with zero problems:

      * a typo in weekend_format ("motogp_2028") resolves to {} and the warm-up
        is hunted all season;
      * a 2027 format with a stale <series>_sessions list still containing
        "practice"/"warmup" hunts sessions that never exist;
      * <series>_sessions declares "fp3" but the sessions block has no
        motogp_fp3 times, so the round passes the gate and the session is
        silently never searched.

    The format is data, so the check is data-vs-data: format <-> declared
    sessions <-> timetable, in both directions and per day.
    """
    problems = []
    formats = calendar.get("weekend_formats") or {}
    if not formats:
        return problems

    if spec is None:
        from series import default_series
        spec = default_series()
    rnd = runtime(spec.key)
    gated_class = rnd.class_of(feature_class(spec).key)
    declared_key = f"{spec.key}_sessions"

    round_num = entry.get("round")
    venue = entry.get("venue", "?")
    label = f"Round {round_num} {venue}"

    fmt_name = entry.get("weekend_format")
    if not fmt_name:
        problems.append(f"{label}: no weekend_format, but the calendar has weekend_formats")
        return problems

    fmt = formats.get(fmt_name)
    if fmt is None:
        problems.append(
            f"{label}: weekend_format {fmt_name!r} is not in weekend_formats "
            f"(known: {', '.join(sorted(formats))})"
        )
        return problems

    fmt_days = {
        day: {item.get("session") for item in (fmt.get(day) or [])
              if isinstance(item, dict) and item.get("session")}
        for day in CALENDAR_DAYS
    }
    fmt_sessions = {name for names in fmt_days.values() for name in names}

    declared = entry.get(declared_key)
    if declared is None:
        problems.append(
            f"{label}: no {declared_key}, cannot check against {fmt_name!r}"
        )
    elif not isinstance(declared, list):
        problems.append(f"{label}: {declared_key} must be a list")
    elif not all(isinstance(name, str) for name in declared):
        problems.append(f"{label}: {declared_key} must contain only strings")
    else:
        declared_set = set(declared)
        for name in sorted(fmt_sessions - declared_set):
            problems.append(
                f"{label}: format {fmt_name!r} runs {name!r} but {declared_key} "
                f"does not list it"
            )
        for name in sorted(declared_set - fmt_sessions):
            problems.append(
                f"{label}: {declared_key} lists {name!r} but format "
                f"{fmt_name!r} does not run it"
            )
        if len(declared_set) != len(declared):
            problems.append(f"{label}: {declared_key} has duplicate entries")

    sessions = entry.get("sessions") or {}

    # No timetable at all is a different statement from a timetable that is
    # missing a session: backfill-by-date seasons (MotoGP 2025, F1 2025) have
    # never carried times, and reporting every format session as "missing" for
    # every round buries the one problem that does exist - if the calendar
    # said it had times, check_round() already says so once.
    if not sessions:
        return problems

    for day in CALENDAR_DAYS:
        day_sessions = sessions.get(day) or {}
        if not isinstance(day_sessions, dict):
            problems.append(f"{label}: {day} sessions must be an object")
            continue

        # A feature-class key in a day block the format does not run it on: the
        # "stale sessions block" mistake. Support classes are not described by
        # the feature class' format, so they are left alone.
        for key, info in day_sessions.items():
            session_type = rnd.calendar_key_to_session.get(key)
            if session_type is None or session_type.race_class != gated_class:
                continue
            name = session_type.format_session_name
            if name not in fmt_days[day]:
                problems.append(
                    f"{label}: {day} has {gated_class.display_name} key {key!r} "
                    f"({name}) but format {fmt_name!r} does not run it on {day}"
                )

        # A session the format runs on this day with no times: the
        # "declared but never searched" mistake.
        for name in sorted(fmt_days[day]):
            keys = _keys_for_format_name(rnd, name)
            if keys and not keys & set(day_sessions):
                problems.append(
                    f"{label}: format {fmt_name!r} runs {name!r} on {day} but "
                    f"the sessions block has no "
                    f"{' / '.join(repr(key) for key in sorted(keys))} times"
                )

    # Release aliases are per-season vocabulary. Validate the shape so a typo
    # cannot silently make a whole season's releases unclassifiable.
    aliases = calendar.get("release_aliases")
    if aliases is not None:
        if not isinstance(aliases, dict):
            problems.append("release_aliases must be an object mapping token -> session name")
        else:
            for token, session_name in aliases.items():
                if not isinstance(token, str) or not token.strip():
                    problems.append(f"release_aliases: bad token {token!r}")
                if (not isinstance(session_name, str)
                        or session_name not in rnd.format_name_to_session):
                    problems.append(
                        f"release_aliases: {token!r} -> {session_name!r} is not "
                        f"a known {spec.display_name} session name"
                    )

    return problems


def check_round(entry: dict, season: int, require_sessions: bool = True,
                calendar: dict = None, spec=None) -> list:
    """
    Per-round checks that validate_calendar() does not cover.

    require_sessions is False for legacy calendars (pre-2026) that carry no
    session times; those seasons are handled by backfill by date, so a missing
    sessions block is expected rather than a problem.
    """
    if spec is None:
        from series import default_series
        spec = default_series()
    rnd = runtime(spec.key)
    problems = []
    round_num = entry.get("round")
    venue = entry.get("venue", "?")
    label = f"Round {round_num} {venue}"

    status = get_round_status(entry)
    if status not in {"scheduled", "cancelled", "canceled", "postponed", "replaced"}:
        problems.append(f"{label}: unknown status {status!r}")

    # Sessions
    sessions = entry.get("sessions") or {}
    if not sessions:
        if require_sessions:
            problems.append(f"{label}: no session times, grabber cannot schedule it")
    else:
        for day, day_sessions in sessions.items():
            if day not in CALENDAR_DAYS:
                problems.append(f"{label}: unexpected day {day!r}")
            for key, info in (day_sessions or {}).items():
                if key not in rnd.calendar_key_to_session:
                    problems.append(f"{label}: unknown session key {key!r}")
                local = info.get("local")
                gmt = info.get("gmt")
                if local and not TIME_RE.match(local):
                    problems.append(f"{label}/{key}: bad local time {local!r}")
                if gmt and not TIME_RE.match(gmt):
                    problems.append(f"{label}/{key}: bad gmt time {gmt!r}")

                utc_offset = entry.get("utc_offset")
                if local and gmt and utc_offset:
                    expected = _add_minutes(local, -_offset_minutes(utc_offset))
                    if expected != gmt:
                        problems.append(
                            f"{label}/{key}: local {local} {utc_offset} should be "
                            f"{expected} UTC, not {gmt}"
                        )

    # Assets
    assets = entry.get("assets") or {}
    for class_key, files in assets.items():
        dirs = rnd.asset_dirs.get(class_key)
        if not dirs:
            problems.append(f"{label}: unknown class in assets {class_key!r}")
            continue
        for asset_kind, filename in (files or {}).items():
            directory = dirs.get(asset_kind)
            if not directory:
                problems.append(f"{label}: unknown asset kind {asset_kind!r}")
                continue
            if not (directory / filename).exists():
                problems.append(f"{label}: missing asset {filename}")

    # Round aliases (other numbers this event has been known by)
    aliases = entry.get("round_aliases") or []
    for alias in aliases:
        if not isinstance(alias, int) or alias == round_num:
            problems.append(f"{label}: bad round_aliases entry {alias!r}")

    flag = entry.get("flag")
    if flag and not (REPO_ROOT / "assets" / "flag_assets_svg" / flag).exists():
        problems.append(f"{label}: missing flag {flag}")

    if calendar is not None:
        problems.extend(check_round_format(entry, calendar, spec))

    return problems


def check_library_matches(calendar: dict, library_root: Path) -> list:
    """
    Compare the calendar against the season folder on disk.

    With official numbering, a round that moved (e.g. Qatar 4 -> 20) leaves the
    old folder behind. That folder is picked up by the library scan as the round
    number in its own name, so the grabber thinks the new round is complete.
    """
    problems = []
    season = calendar.get("season")
    season_path = library_root / str(season)
    if not season_path.is_dir():
        return problems

    calendar_by_number = {
        r.get("round"): r for r in calendar.get("rounds", []) if r.get("round")
    }

    for folder in sorted(season_path.iterdir()):
        if not folder.is_dir():
            continue
        match = re.match(r"Round\s*(\d+)", folder.name)
        if not match:
            continue
        round_num = int(match.group(1))
        entry = calendar_by_number.get(round_num)

        if entry is None:
            problems.append(
                f"Library: '{folder.name}' has no matching round in the calendar"
            )
            continue

        expected = entry.get("folder")
        if expected and folder.name != expected:
            problems.append(
                f"Library: folder '{folder.name}' should be '{expected}' "
                f"(round {round_num} is now {entry.get('venue')})"
            )

    return problems


def discover_calendars(config_dir: Path = None, series_keys=None) -> list:
    """
    Every ``<prefix>_<year>.json`` calendar, as (spec, season, path).

    Discovered from the declarations rather than from a glob of one series'
    prefix: the prefixes are what make the two series' refresh cycles safe (an
    F1 calendar can never be picked up as a MotoGP one), so the discovery and
    the safety rule have to be the same statement.

    A prefix that is also the start of another prefix would make the globs
    overlap, so that is refused rather than silently double-counted.
    """
    config_dir = Path(config_dir or CONFIG_DIR)
    specs = [get_series(key) for key in series_keys] if series_keys else all_series()

    prefixes = [spec.calendar_prefix for spec in specs]
    for prefix in prefixes:
        for other in prefixes:
            if prefix != other and other.startswith(prefix):
                raise ValueError(
                    f"calendar prefixes {prefix!r} and {other!r} overlap; a "
                    f"file could belong to two series"
                )

    found = []
    for spec in specs:
        for path in sorted(config_dir.glob(f"{spec.calendar_prefix}_*.json")):
            stem = path.stem[len(spec.calendar_prefix) + 1:]
            if not stem.isdigit():
                raise ValueError(
                    f"{path.name} does not name a season; the pattern is "
                    f"{spec.calendar_prefix}_<year>.json"
                )
            found.append((spec, int(stem), path))
    return found


def validate_one(spec, season: int, path: Path, library: Path = None) -> list:
    """Validate one calendar file, returning human-readable problems."""
    with open(path, "r", encoding="utf-8") as handle:
        calendar = json.load(handle)

    problems = validate_calendar(calendar)
    requires_sessions = has_session_times(calendar)
    for entry in calendar.get("rounds", []):
        problems.extend(
            check_round(entry, season, requires_sessions, calendar, spec)
        )

    if library:
        problems.extend(check_library_matches(calendar, Path(library)))

    return problems


def _report(spec, season, path, calendar, problems) -> None:
    rounds = calendar.get("rounds", [])
    active = [r for r in rounds if is_round_active(r)]
    requires_sessions = has_session_times(calendar)

    print(f"\n📅 {spec.display_name} {season} calendar: {len(rounds)} rounds "
          f"({len(active)} active, {len(rounds) - len(active)} inactive)")
    if not requires_sessions:
        print("   Legacy format: no session times (backfill by date only)")
    print(f"   File: {path}")

    for entry in [r for r in rounds if not is_round_active(r)]:
        print(f"   ⏸  Round {entry.get('round')} {entry.get('venue')}: "
              f"{get_round_status(entry)}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate season calendars for every registered series"
    )
    parser.add_argument("--season", type=int, default=None,
                        help="only this season (default: every calendar)")
    parser.add_argument("--series", type=str, default=None,
                        choices=[spec.key for spec in all_series()],
                        help="only this series (default: every series)")
    parser.add_argument("--library", type=str, default=None,
                        help="library root to compare against; requires exactly "
                             "one calendar to be selected")
    parser.add_argument("--strict", action="store_true",
                        help="Treat warnings as failures")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    keys = [args.series] if args.series else None
    try:
        calendars = discover_calendars(CONFIG_DIR, keys)
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1

    if args.season is not None:
        calendars = [c for c in calendars if c[1] == args.season]

    if not calendars:
        wanted = f"{args.series or 'any series'} {args.season or 'any season'}"
        print(f"❌ No calendar found for {wanted} in {CONFIG_DIR}")
        return 1

    if args.library and len(calendars) != 1:
        print(f"❌ --library needs exactly one calendar selected, "
              f"{len(calendars)} matched; add --series and --season")
        return 1

    results = {}
    failed = 0
    for spec, season, path in calendars:
        problems = validate_one(spec, season, path, args.library)
        with open(path, "r", encoding="utf-8") as handle:
            calendar = json.load(handle)
        results[str(path)] = {
            "series": spec.key,
            "season": season,
            "problems": problems,
        }
        if not args.json:
            _report(spec, season, path, calendar, problems)
            if problems:
                print(f"\n❌ {len(problems)} problem(s):")
                for problem in problems:
                    print(f"   - {problem}")
            else:
                print("\n✅ No problems found")
        failed += 1 if problems else 0

    if args.json:
        print(json.dumps({"calendars": results,
                          "problems": [p for r in results.values()
                                       for p in r["problems"]]}, indent=2))

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
