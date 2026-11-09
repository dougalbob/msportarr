#!/usr/bin/env python3
"""
Grab statistics, derived from ``grabber_state.<series>.json``
============================================================

Two things here were wrong once there was more than one series, and both were
wrong *silently* - the page rendered, with plausible numbers, and they were the
wrong ones.

**The class was guessed from the release title.** ``get_class_from_release()``
was a chain of `if 'MOTO3' in release` tests, with MotoGP as the fall-through.
For a Formula 1 release every test misses, so every F1 grab was counted as
MotoGP - and a series whose entire vocabulary is one class can never match a
MotoGP token, so it could not be fixed by adding one. The class is now the
session's own, read from the registry, and the parse refuses rather than
guessing: an entry from another series counts toward no class at all.

**The series was never part of the question.** One ``grabber_state.json``
existed, so "the stats" meant "the only stats". Now each series has its own
state file, its own classes and its own Plex library, and a summary that mixes
them reports one championship's seasons against another's classes.

The shape of the returned dict is unchanged - the stats template and
``/stats`` both read it - and the default series' numbers are what the page
showed before. What is new is ``by_series``, ``active_series`` and the
``effective_failed`` count below.

**On the failure count.** The grabber records a failed session and later
*removes* it when the session is subsequently grabbed (``clear_session``), so
``len(failed)`` is the number still outstanding, not the number that ever
failed. Dividing by it reported a 100% success rate after a weekend in which
every session had failed once before succeeding. The grabber now keeps an
append-only ``failures`` record that a successful retry does not erase, and
``failure_metric`` says whether ``success_rate`` was built from that or from the
older outstanding count, so a reader never has to guess.
"""

import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))

from series import get_series, runtime  # noqa: E402
from utils.state_files import state_filename  # noqa: E402

_STATE_FILE = None


def __getattr__(name):
    """
    ``STATE_FILE`` resolved on first use, not at import.

    The path depends on the series registry, and the round tests (and the CLI)
    point CONFIG_DIR at a temp directory after import. Resolving it lazily
    keeps ``STATE_FILE.exists()`` honest, and keeps the compiled-in default
    honest with it.
    """
    global _STATE_FILE
    if name == "STATE_FILE":
        if _STATE_FILE is None:
            _STATE_FILE = (Path(os.environ.get('CONFIG_DIR', '/config'))
                           / state_filename('grabber_state'))
        return _STATE_FILE
    raise AttributeError(name)


# ===========================================================================
# Series identity
# ===========================================================================

def _default_series_key() -> str:
    from series import default_series

    return default_series().key


def _series_keys() -> list:
    """Series that have a grabber state file on disk, plus the default one.

    Statistics are a report about what happened, so a series with no state file
    contributes nothing and is not named in the output - an empty F1 panel on an
    install that has never grabbed an F1 release is noise, not information.
    """
    from series import all_series

    config_dir = Path(os.environ.get('CONFIG_DIR', '/config'))
    present = [
        spec.key for spec in all_series()
        if (config_dir / state_filename('grabber_state', series=spec.key)).exists()
    ]
    default = _default_series_key()
    return [default] + [key for key in present if key != default]


def _state_file_for(series_key: str) -> Path:
    config_dir = Path(os.environ.get('CONFIG_DIR', '/config'))
    return config_dir / state_filename('grabber_state', series=series_key)


def load_grabber_state(series: str = None) -> dict:
    """Load one series' grabber state file, or an empty state."""
    empty = {"searched": {}, "grabbed": {}, "failed": {}}
    if series is None:
        path = STATE_FILE
    else:
        path = _state_file_for(series)
    if path.exists():
        try:
            with open(path, 'r') as f:
                return json_load(f, empty)
        except Exception:
            pass
    return empty


def json_load(handle, fallback):
    """``json.load`` that reports the fallback rather than raising."""
    import json

    try:
        return json.load(handle) or fallback
    except Exception:                      # noqa: BLE001 - corrupt state is not fatal
        return fallback


def _recorded_failures(state: dict) -> int:
    """
    How many grab attempts have failed, over the life of this state file.

    ``len(state["failed"])`` is the wrong number and always was. The grabber
    treats ``failed`` as an outstanding-work set: when a retry finally succeeds,
    ``clear_session()`` deletes the entry. A weekend in which all 22 sessions
    failed once and then succeeded therefore showed *zero* failures and a 100%
    success rate.

    ``state["failures"]`` is the append-only record added for this - one entry
    per failed attempt, keyed by session and timestamp, never removed by a
    retry. Installs that predate it have no such key and fall back to the
    outstanding count, which is exactly what the page used to show, so nothing
    gets worse for them.
    """
    history = state.get("failures")
    if isinstance(history, dict):
        return len(history)
    return len(state.get("failed", {}))


# ===========================================================================
# Naming, from the registry
# ===========================================================================

def parse_session_key(key: str):
    """Parse session key like '2025_R01_E05' into components."""
    match = re.match(r'(\d{4})_R(\d+)_E(\d+)', key)
    if match:
        return {
            "year": int(match.group(1)),
            "round": int(match.group(2)),
            "episode": int(match.group(3))
        }
    return None


def _entry_session(series_key: str, key: str, release: str):
    """
    The session a state entry belongs to, from the registry, or None.

    The episode number in the key *is* the session's element number, which the
    series' own table resolves. That is authoritative in a way the release title
    is not: the title is free text from an indexer, and the element number is
    what this app wrote into the folder name.

    Returns None for an entry that does not resolve - a key from a previous
    numbering scheme, or a month of other data. The caller counts it as
    "unknown" rather than as the feature class.
    """
    parsed = parse_session_key(key)
    if not parsed:
        # No prefix to read: fall back to the title, which is what the pre-hybrid
        # code did for everything. It is only reached by entries written by
        # something other than this grabber.
        return _session_from_release(series_key, release)

    try:
        return runtime(series_key).get_session_from_element(
            parsed["episode"], season=parsed["year"]
        )
    except Exception:                      # noqa: BLE001 - unresolvable is a valid answer
        return None


def _release_names_series(series_key: str, release: str) -> bool:
    """
    Does this title mention the series at all?

    The guard that stops one series' vocabulary matching another series' titles.
    Without it "Formula1.2026.Silverstone.Race" matches MotoGP's *generic*
    session name "Race" and is reported as a Moto3 or MotoGP race - which is the
    original bug wearing a different hat.

    A title qualifies when it contains the series' search prefix or one of its
    class tokens, compared with separators removed (indexers write
    "Formula 1", "Formula1" and "Formula.1").
    """
    normalised = re.sub(r'[.\-_ ]+', '', (release or '').lower())
    if not normalised:
        return False
    spec = get_series(series_key)
    tokens = {spec.search_prefix.lower().replace(' ', '')}
    for cls in spec.classes:
        tokens.update(token.lower() for token in cls.name_tokens)
    return any(token and token in normalised for token in tokens)


def _session_from_release(series_key: str, release: str):
    """
    Best-effort session from a release title, or None.

    Deliberately narrow: it first requires the title to *name the series*, then
    matches the series' own display names and gives up otherwise. It does not
    reuse ``classify_session()``, because that is tuned for scoring candidates
    and its refusals are part of that contract; a statistics page that disagreed
    with the grabber about what was grabbed would be worse than one that says
    "unknown".
    """
    if not _release_names_series(series_key, release):
        return None
    text = (release or "").upper()
    try:
        runtime(series_key).session_type
    except Exception:                      # noqa: BLE001
        return None
    # Longest display name first: "Sprint Qualifying" contains "Qualifying".
    candidates = sorted(
        ((name.upper(), member) for member, name in
         runtime(series_key).session_display_names.items()),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    for name, member in candidates:
        if name and name in text:
            return member
    return None


def get_class_from_release(release: str, series: str = None) -> str:
    """
    The class a release title names, or "Unknown".

    **Retained for callers that only have a title, and it is a guess.** Prefer
    :func:`session_class_name`, which reads the session the app resolved. The
    old version returned "MotoGP" as its fall-through, which is how every
    Formula 1 grab was reported as MotoGP.
    """
    series_key = series or _default_series_key()
    session = _session_from_release(series_key, release)
    if session is None:
        return "Unknown"
    return session_class_name(series_key, session)


def session_class_name(series_key: str, session) -> str:
    """The display name of a session's class, e.g. "Moto2" or "F1"."""
    try:
        return session.race_class.display_name
    except Exception:                      # noqa: BLE001 - no class is legitimate
        return "Unknown"


def get_session_name_from_release(release: str, series: str = None) -> str:
    """
    A readable session name for a release title.

    Retained for the same reason as :func:`get_class_from_release`; the summary
    uses the resolved session's display name instead.
    """
    series_key = series or _default_series_key()
    session = _session_from_release(series_key, release)
    if session is None:
        return "Session"
    try:
        return runtime(series_key).session_display_names.get(session, "Session")
    except Exception:                      # noqa: BLE001
        return "Session"


# ===========================================================================
# The summary
# ===========================================================================

def get_stats_summary(series: str = None):
    """
    Summary statistics from one series' grabber state.

    ``series`` defaults to the default series, so every existing caller and the
    /stats page keep working. Pass a key for another series, or read
    ``by_series`` in the result for all of them.
    """
    series_key = series or _default_series_key()
    summary = _summarise_series(series_key)

    summary["by_series"] = {
        key: (summary if key == series_key else _summarise_series(key))
        for key in _series_keys()
    }
    summary["active_series"] = list(summary["by_series"])
    summary["series"] = series_key
    summary["series_display_name"] = get_series(series_key).display_name
    return summary


def get_combined_stats_summary(series_keys) -> dict:
    """Aggregate one view across series while retaining a full breakdown.

    Counts and time-series are summed. Success rate is recalculated from the
    combined grab/failure totals (weighted by attempts), never averaged from
    per-series percentages.
    """
    keys = list(dict.fromkeys(series_keys or [_default_series_key()]))
    by_series = {}
    for key in keys:
        data = _summarise_series(key)
        data["series"] = key
        data["series_display_name"] = get_series(key).display_name
        for grab in data.get("recent_grabs", []):
            grab["series"] = key
            grab["series_name"] = data["series_display_name"]
        by_series[key] = data

    totals = {
        field: sum(summary.get(field, 0) for summary in by_series.values())
        for field in ("total_grabbed", "total_failed", "recorded_failures",
                      "effective_failed", "grabs_today", "grabs_this_week",
                      "grabs_this_month")
    }
    denominator = totals["total_grabbed"] + totals["effective_failed"]
    metrics = {summary.get("failure_metric", "outstanding")
               for summary in by_series.values()}

    def combine_rows(field, identity):
        rows = {}
        for summary in by_series.values():
            for row in summary.get(field, []):
                key = row.get(identity)
                if key is None:
                    continue
                if key not in rows:
                    rows[key] = dict(row, count=0)
                rows[key]["count"] += row.get("count", 0)
        return [rows[key] for key in sorted(rows)]

    by_class = {}
    by_session = defaultdict(int)
    by_year = defaultdict(int)
    recent_grabs = []
    for key, summary in by_series.items():
        for name, count in summary.get("by_class", {}).items():
            by_class[name] = by_class.get(name, 0) + count
        for name, count in summary.get("by_session", {}).items():
            by_session[name] += count
        for year, count in summary.get("by_year", {}).items():
            by_year[year] += count
        recent_grabs.extend(summary.get("recent_grabs", []))

    recent_grabs.sort(key=lambda grab: grab.get("timestamp", ""), reverse=True)
    by_day = combine_rows("by_day", "date")
    by_month = combine_rows("by_month", "month")
    return {
        **totals,
        "failure_metric": metrics.pop() if len(metrics) == 1 else "mixed",
        "success_rate": round(totals["total_grabbed"] / denominator * 100)
                        if denominator else 100,
        "by_class": by_class,
        "by_session": dict(sorted(by_session.items(), key=lambda item: -item[1])),
        "by_day": by_day,
        "by_month": by_month,
        "by_year": dict(by_year),
        "recent_grabs": recent_grabs[:15],
        "last_run": max((s.get("last_run") for s in by_series.values()
                         if s.get("last_run")), default=None),
        "last_scheduled": max((s.get("last_scheduled") for s in by_series.values()
                               if s.get("last_scheduled")), default=None),
        "max_daily": max((row["count"] for row in by_day), default=0),
        "max_monthly": max((row["count"] for row in by_month), default=0),
        "by_series": by_series,
        "active_series": keys,
        "series": "all",
        "series_display_name": "All series",
    }


def _summarise_series(series_key: str) -> dict:
    """The per-series half of :func:`get_stats_summary`."""
    state = load_grabber_state(series_key)
    grabbed = state.get("grabbed", {})
    failed = state.get("failed", {})

    # The classes this series actually has, in declaration order. Built from the
    # registry so a series with one class is not reported as "MotoGP: 0, Moto2:
    # 0, Moto3: 0" beside its own real numbers.
    class_names = [spec.display_name for spec in get_series(series_key).classes]
    by_class = {name: 0 for name in class_names}

    if not grabbed:
        return {
            "total_grabbed": 0,
            "total_failed": len(failed),
            "recorded_failures": _recorded_failures(state),
            "effective_failed": _effective_failed(state),
            "failure_metric": ("recorded" if "failures" in state
                               else "outstanding"),
            "success_rate": 0 if _effective_failed(state) else 100,
            "grabs_today": 0,
            "grabs_this_week": 0,
            "grabs_this_month": 0,
            "by_class": by_class,
            "by_session": {},
            "by_day": _day_series({}),
            "by_month": _month_series({}),
            "by_year": {},
            "recent_grabs": [],
            "last_run": state.get("last_run"),
            "last_scheduled": state.get("last_scheduled_check"),
            "max_daily": 0,
            "max_monthly": 0,
        }

    now = datetime.now()
    today = now.date()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    grabs_today = 0
    grabs_this_week = 0
    grabs_this_month = 0

    by_session = defaultdict(int)
    by_day = defaultdict(int)
    by_month = defaultdict(int)
    by_year = defaultdict(int)

    grabs_list = []

    for key, data in grabbed.items():
        try:
            timestamp = data.get("timestamp", "")
            release = data.get("release", "Unknown")
            grab_time = datetime.fromisoformat(timestamp)
            grab_date = grab_time.date()

            parsed = parse_session_key(key)
            session = _entry_session(series_key, key, release)
            race_class = (session_class_name(series_key, session)
                          if session is not None else "Unknown")
            session_name = _session_display(series_key, session)

            # By class. A session that did not resolve counts toward no class:
            # attributing it to the feature class is how F1's grabs became
            # MotoGP's.
            if race_class in by_class:
                by_class[race_class] += 1
            by_session[session_name] += 1

            if parsed:
                by_year[parsed["year"]] += 1

            if grab_date >= today - timedelta(days=14):
                day_key = grab_date.strftime("%Y-%m-%d")
                by_day[day_key] += 1

            if grab_date >= today - timedelta(days=180):
                month_key = grab_time.strftime("%Y-%m")
                by_month[month_key] += 1

            if grab_date == today:
                grabs_today += 1
            if grab_date >= week_ago:
                grabs_this_week += 1
            if grab_date >= month_ago:
                grabs_this_month += 1

            grabs_list.append({
                "key": key,
                "timestamp": timestamp,
                "grab_time": grab_time,
                "release": release,
                "race_class": race_class,
                "session_name": session_name,
                "round": parsed["round"] if parsed else 0,
                "year": parsed["year"] if parsed else 0,
            })

        except (ValueError, KeyError, TypeError):
            continue

    grabs_list.sort(key=lambda x: x["grab_time"], reverse=True)
    recent_grabs = []
    for grab in grabs_list[:15]:
        recent_grabs.append({
            "time": grab["grab_time"].strftime("%d %b %H:%M"),
            "timestamp": grab["grab_time"].isoformat(),
            "session": f"{grab['race_class']} {grab['session_name']}",
            "round": grab["round"],
            "year": grab["year"],
            "release": grab["release"][:50] + "..." if len(grab["release"]) > 50
                       else grab["release"],
            "success": True,
        })

    day_data = _day_series(by_day, today)
    month_data = _month_series(by_month, today)

    total = len(grabbed)
    total_failed = len(failed)              # still outstanding
    effective_failed = _effective_failed(state)

    return {
        "total_grabbed": total,
        "total_failed": total_failed,
        "recorded_failures": _recorded_failures(state),
        "effective_failed": effective_failed,
        "failure_metric": ("recorded" if "failures" in state
                           else "outstanding"),
        "success_rate": round(total / (total + effective_failed) * 100)
                        if (total + effective_failed) > 0 else 100,
        "grabs_today": grabs_today,
        "grabs_this_week": grabs_this_week,
        "grabs_this_month": grabs_this_month,
        "by_class": by_class,
        "by_session": dict(sorted(by_session.items(), key=lambda p: -p[1])),
        "by_day": day_data,
        "by_month": month_data,
        "by_year": dict(by_year),
        "recent_grabs": recent_grabs,
        "last_run": state.get("last_run"),
        "last_scheduled": state.get("last_scheduled_check"),
        "max_daily": max((d["count"] for d in day_data), default=0),
        "max_monthly": max((m["count"] for m in month_data), default=0),
    }


def _effective_failed(state: dict) -> int:
    """
    The denominator for the success rate.

    The recorded failure count where the state file has one, because it survives
    a successful retry and the outstanding set does not. Never less than the
    outstanding count: a state file whose history was edited must not report a
    better rate than the failures it still lists.
    """
    return max(_recorded_failures(state), len(state.get("failed", {})))


def _session_display(series_key: str, session) -> str:
    if session is None:
        return "Unknown"
    try:
        return runtime(series_key).session_display_names.get(session, "Unknown")
    except Exception:                      # noqa: BLE001
        return "Unknown"


def _day_series(by_day: dict, today=None) -> list:
    """The last 14 days, including the empty ones, for the bar chart."""
    today = today or datetime.now().date()
    data = []
    for i in range(13, -1, -1):
        day = today - timedelta(days=i)
        key = day.strftime("%Y-%m-%d")
        data.append({
            "date": key,
            "label": day.strftime("%d %b"),
            "count": by_day.get(key, 0),
        })
    return data


def _month_series(by_month: dict, today=None) -> list:
    """The last 6 months, including the empty ones, for the trend chart."""
    today = today or datetime.now().date()
    data = []
    for i in range(5, -1, -1):
        month = (today.replace(day=1) - timedelta(days=i * 30))
        key = month.strftime("%Y-%m")
        data.append({
            "month": key,
            "label": month.strftime("%b %Y"),
            "count": by_month.get(key, 0),
        })
    return data


# ===========================================================================
# CLI
# ===========================================================================

def main() -> int:
    """``python grab_stats.py [--series KEY]`` - print one series' summary."""
    args = sys.argv[1:]
    series_key = None
    if "--series" in args:
        index = args.index("--series")
        try:
            series_key = args[index + 1]
        except IndexError:
            print("--series needs a value")
            return 2

    summary = get_stats_summary(series_key)
    print(f"{summary['series_display_name']} ({summary['series']})")
    print(f"  Grabbed:      {summary['total_grabbed']}")
    print(f"  Failed:       {summary['total_failed']} outstanding, "
          f"{summary['recorded_failures']} recorded attempts")
    print(f"  Success rate: {summary['success_rate']}% "
          f"(from {summary['failure_metric']} failures)")
    print(f"  This month:   {summary['grabs_this_month']}")
    for name, count in summary["by_class"].items():
        print(f"    {name:<10} {count}")
    if len(summary["by_series"]) > 1:
        print("  Other series:")
        for key, other in summary["by_series"].items():
            if key != summary["series"]:
                print(f"    {other['series_display_name']}: "
                      f"{other['total_grabbed']} grabbed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
