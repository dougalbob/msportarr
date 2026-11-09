#!/usr/bin/env python3
"""
Home page summary (issue #51)
=============================

Pure view-model builders for the WebUI home tab. Everything here takes
already-loaded data - calendar dicts, grabber state, library scans, activity
history, an explicit ``now`` - and returns plain dicts for the template. No
Flask, no file I/O and no network calls, so the rules can be tested without a
running app and the route stays a thin wiring layer.

The home page answers the three questions the issue asked, plus one:

1. What did the machine last add to Plex *on schedule* (not a backfill)?
2. During a race weekend, which session is next and when will it be searched?
3. How much of the weekend so far is already in Plex?
4. Is the machine alive? (scheduler heartbeat; queue and service chips are
   assembled by the WebUI route and the browser, not here)

Deliberate rules, all decided in the issue discussion:

* A grabbed entry counts as scheduled only when it *says* ``mode=scheduled``.
  Entries written before mode recording existed carry no mode, and the
  timestamp of a grab does not reveal which command made it - so they are
  skipped, never inferred. The card prefers an honest empty state over a
  guess, exactly like an ambiguous element number is refused rather than
  resolved (AGENTS.md rule 1).
* "In Plex" means the session folder exists in the library. The activity
  history is capped at ten events, so it can *timestamp* an addition but can
  never be the durable record of one.
* The weekend window and the session times come from the grabber itself
  (``WINDOW_DAYS_*``, ``get_session_times``) so "searchable from" on the card
  is the exact instant the grabber will act. A second time implementation
  here would drift and then explain the wrong thing.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from motogp_common import (
    GRAB_MODE_SCHEDULED,
    SESSION_TO_CALENDAR_KEY,
    SessionType,
    element_number,
    get_round_by_number,
    get_session_from_element,
    is_round_active,
    round_runs_session,
)
from motogp_grabber import (
    WINDOW_DAYS_AFTER_RACE,
    WINDOW_DAYS_BEFORE_WEEKEND,
    get_session_times,
)
from motogp_scheduler import CONFIG as SCHEDULER_CONFIG

logger = logging.getLogger("msportarr.home_summary")

#: grabber_state.json key / activity episode key: ``YYYY_RNN_ENN``.
SESSION_KEY_RE = re.compile(r"^(\d{4})_R(\d+)_E(\d+)$")


# =============================================================================
# SMALL FORMATTERS
# =============================================================================

def _iso(dt: datetime) -> str:
    """UTC-labelled ISO instant, matching build_round_schedule's convention.

    Session times from the calendar are naive GMT datetimes; the ``Z`` suffix
    lets the browser convert them to local time exactly like the calendar
    modal does.
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _label(dt: datetime) -> str:
    """Server-rendered fallback time text (the browser replaces it locally)."""
    return dt.strftime("%a %H:%M UTC")


def _day_label(dt: datetime) -> str:
    return dt.strftime("%a %d %b %H:%M UTC")


def format_delta(delta: timedelta) -> str:
    """Human countdown, e.g. ``2 h 05 m`` / ``18 m`` / ``1 d 03 h``."""
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes < 1:
        return "under a minute"
    days, remainder = divmod(total_minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    if days:
        return f"{days} d {hours:02d} h"
    if hours:
        return f"{hours} h {minutes:02d} m"
    return f"{minutes} m"


def format_age(minutes: int) -> str:
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    return f"{format_delta(timedelta(minutes=minutes))} ago"


# =============================================================================
# WEEKEND WINDOW
# =============================================================================

def find_weekend_round(calendar: Optional[dict], today: date) -> Optional[dict]:
    """
    The round whose operational window contains ``today``, or None.

    Mirrors ``get_sessions_ready_for_search`` exactly - one day before the
    weekend starts (Thursday running at flyaways) to two days after the race -
    by importing the same constants, so the home page never claims a weekend
    the grabber is not working, or hides one it is. Cancelled or postponed
    rounds never match: ``is_round_active`` is the same gate the grabber uses.
    """
    for round_info in (calendar or {}).get("rounds", []):
        if not is_round_active(round_info):
            continue

        race_date_str = round_info.get("date")
        if not race_date_str:
            continue

        try:
            race_date = datetime.strptime(race_date_str, "%Y-%m-%d").date()
            weekend_start_str = round_info.get("weekend_start")
            weekend_start = (
                datetime.strptime(weekend_start_str, "%Y-%m-%d").date()
                if weekend_start_str
                else race_date - timedelta(days=2)
            )
        except ValueError:
            continue

        window_start = weekend_start - timedelta(days=WINDOW_DAYS_BEFORE_WEEKEND)
        window_end = race_date + timedelta(days=WINDOW_DAYS_AFTER_RACE)
        if window_start <= today <= window_end:
            return round_info

    return None


# =============================================================================
# TIMED CANDIDATES
# =============================================================================

#: The vocabulary the view-model is being built against: the series' calendar-key
#: map, its element-number table and its round-session rule. ``None`` means "the
#: process binding", which is what every pre-hybrid caller gets and is why this
#: module reads exactly as it did when there was one series.
#:
#: It is a parameter rather than an import because the WebUI serves both series
#: in one process and the binding is the default series: rendering F1's home tab
#: from MotoGP's tables would name F1's sessions wrongly, count them against the
#: wrong element numbers, and ask the wrong round-session question.
_Vocabulary = Optional[Dict[str, Any]]


def _vocabulary(series: Optional[str]) -> Dict[str, Any]:
    """The tables one series' view-model is built from."""
    from series import default_series, runtime

    rt = runtime(series or default_series().key)
    return {
        "runtime": rt,
        "series": rt.key,
        "session_type": rt.session_type,
        "session_to_calendar_key": rt.session_to_calendar_key,
        "round_runs_session": None,  # the spine function, called with series=
    }


def _session_key(vocabulary: _Vocabulary, season: int, round_num: int,
                 session_type) -> str:
    """The grabber-state key for one session, numbered by its own series."""
    if vocabulary and vocabulary.get("runtime") is not None:
        number = vocabulary["runtime"].element_number(session_type, season)
    else:
        number = element_number(session_type, season)
    return f"{season}_R{round_num:02d}_E{number:02d}"


def _session_from_element(vocabulary: _Vocabulary, element: int,
                          season: int):
    """
    Which session an element number means, in the right series.

    The two series' numbers are different namespaces that share a format, so
    decoding an F1 key with MotoGP's table returns a *different real session*
    rather than None - and the card would then name the wrong programme.
    """
    if vocabulary and vocabulary.get("runtime") is not None:
        return vocabulary["runtime"].get_session_from_element(
            element, season=season
        )
    return get_session_from_element(element, season=season)


def _timed_candidates(round_info: dict, calendar: Optional[dict],
                      desired: Iterable[SessionType],
                      vocabulary: _Vocabulary = None
                      ) -> List[Tuple[datetime, datetime, SessionType]]:
    """
    Desired, schedulable sessions this round runs, with their GMT times.

    Sessions with no calendar time are dropped rather than shown without one:
    a card that cannot say when the session airs cannot say when it will be
    searched, and the grabber skips it too.
    """
    calendar_keys = (vocabulary or {}).get("session_to_calendar_key") \
        or SESSION_TO_CALENDAR_KEY
    series = (vocabulary or {}).get("series")

    candidates = []
    for session_type in desired:
        if session_type not in calendar_keys:
            continue
        if not round_runs_session(round_info, session_type, calendar,
                                  series=series):
            continue
        start, end = get_session_times(round_info, session_type, series=series)
        if not start or not end:
            continue
        candidates.append((start, end, session_type))
    return candidates


def _block_display_name(tied: List[SessionType],
                        vocabulary: _Vocabulary = None) -> Tuple[str, str]:
    """
    Display name for the session(s) occupying one timetable block.

    Q1 and Q2 share the ``*_qualifying`` block and the same start/end, so a
    tie is one televised session, not two: it is named "MotoGP Qualifying"
    (what the calendar modal shows) rather than whichever part sorted first.
    """
    first = tied[0]
    race_class = first.race_class
    class_label = race_class.display_name if race_class else ""

    if len(tied) == 1:
        return first.full_short_name, class_label

    calendar_keys = (vocabulary or {}).get("session_to_calendar_key") \
        or SESSION_TO_CALENDAR_KEY
    key = calendar_keys.get(first, "")
    same_block = [t for t in tied if calendar_keys.get(t) == key]
    if len(same_block) < 2 or "_" not in key:
        return first.full_short_name, class_label

    label = key.split("_", 1)[1].replace("_", " ").title()
    return (f"{class_label} {label}" if class_label else label), class_label


# =============================================================================
# NEXT SESSION
# =============================================================================

def next_session(round_info: dict, calendar: Optional[dict],
                 desired: Iterable[SessionType], now: datetime,
                 search_delay_hours: float,
                 vocabulary: _Vocabulary = None) -> Optional[dict]:
    """
    The next session the grabber will care about, and when it becomes
    searchable.

    States:

    ``live``      a desired session is underway now (start <= now < end);
    ``upcoming``  the next desired session has not started;
    ``complete``  every desired session at this round has aired - the card
                  then says the scheduler is finishing its searches.

    Returns None when the round runs nothing desired with times, so the
    template hides the block instead of rendering an empty promise.

    ``search_after`` is ``end + search_delay_hours`` - the same formula
    ``get_sessions_ready_for_search`` uses, so the card names the exact
    instant the scheduled search will fire, not an approximation of it.
    """
    candidates = _timed_candidates(round_info, calendar, desired, vocabulary)
    if not candidates:
        return None

    live = [c for c in candidates if c[0] <= now < c[1]]
    upcoming = [c for c in candidates if c[0] > now]

    if live:
        state = "live"
        pool = live
    elif upcoming:
        state = "upcoming"
        pool = upcoming
    else:
        return {"state": "complete"}

    pool.sort(key=lambda c: (c[0], c[1], c[2].name))
    start, end, _first = pool[0]
    tied = [c[2] for c in pool if c[0] == start and c[1] == end]
    name, class_label = _block_display_name(
        sorted(tied, key=lambda s: s.name), vocabulary
    )

    search_after = end + timedelta(hours=search_delay_hours)
    result = {
        "state": state,
        "name": name,
        "class_label": class_label,
        "start": start,
        "end": end,
        "search_after": search_after,
        "start_utc": _iso(start),
        "end_utc": _iso(end),
        "search_after_utc": _iso(search_after),
        "start_label": _label(start),
        "end_label": _label(end),
        "search_after_label": _label(search_after),
        "search_delay_hours": search_delay_hours,
    }
    if state == "upcoming":
        result["countdown"] = format_delta(start - now)
    return result


# =============================================================================
# WEEKEND PROGRESS
# =============================================================================

def weekend_progress(round_info: dict, calendar: Optional[dict], season: int,
                     desired: Iterable[SessionType], now: datetime,
                     library_sessions: Dict[SessionType, Any],
                     grabbed: dict, failed: dict,
                     vocabulary: _Vocabulary = None) -> dict:
    """
    "N of M aired sessions now available in Plex for Round R Venue".

    Counts the same unit as the Download tab: each desired SessionType the
    round's format runs (Q1 and Q2 count separately because they are separate
    searches and separate library folders). The denominator only grows as
    sessions air, so mid-Saturday it reads "8 of 10", not "8 of 18" - the
    number the issue asked for.

    A session that has aired but is not in the library is classified from the
    grabber state rather than left ambiguous: grabbed (downloading or being
    sorted), failed (no release found yet - the scheduler retries), or
    awaiting (not searchable yet, or the search has not run).
    """
    round_num = round_info.get("round")
    aired = in_plex = downloading = not_found = awaiting = 0
    missing: List[dict] = []

    for start, end, session_type in _timed_candidates(
            round_info, calendar, desired, vocabulary):
        if end > now:
            continue  # has not aired yet - not part of "so far"
        aired += 1
        if session_type in (library_sessions or {}):
            in_plex += 1
            continue

        key = _session_key(vocabulary, season, round_num, session_type)
        if key in (grabbed or {}):
            downloading += 1
            state = "downloading"
        elif key in (failed or {}):
            not_found += 1
            state = "not found yet"
        else:
            awaiting += 1
            state = "awaiting search"
        missing.append({"name": session_type.full_short_name, "state": state})

    return {
        "round": round_num,
        "venue": round_info.get("venue", ""),
        "aired": aired,
        "in_plex": in_plex,
        "downloading": downloading,
        "not_found": not_found,
        "awaiting": awaiting,
        "missing": missing,
    }


# =============================================================================
# LATEST SCHEDULED ADDITION (hero card)
# =============================================================================

def seasons_with_scheduled_grabs(grabbed: dict) -> List[int]:
    """
    Seasons that could possibly feed the hero card.

    The route uses this to scan only the library folders that matter. History
    from backfills and unlabelled entries is excluded here as well as in the
    card itself, so a long-lived state file does not turn a page load into a
    walk of every season ever grabbed.
    """
    seasons = set()
    for key, entry in (grabbed or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("mode") != GRAB_MODE_SCHEDULED:
            continue
        match = SESSION_KEY_RE.match(str(key))
        if match:
            seasons.add(int(match.group(1)))
    return sorted(seasons)


def latest_scheduled_addition(grabbed: dict, activity_history: List[dict],
                              library_by_season: Dict[int, dict],
                              calendars_by_season: Optional[Dict[int, dict]] = None,
                              vocabulary: _Vocabulary = None,
                              ) -> Optional[dict]:
    """
    The most recent session the *scheduler* put into Plex.

    Only entries recorded with ``mode=scheduled`` count (issue #51 decision):
    a backfill is a correction, and an entry from before mode recording says
    nothing about which path found it - both are skipped rather than guessed
    from timestamps. Presence in the library is the durable proof of "added";
    the capped activity history only supplies a better timestamp (the
    tagger's COMPLETE event, "Ready in Plex") when it still has one, and the
    card says which kind of time it is showing.
    """
    calendars_by_season = calendars_by_season or {}
    best: Optional[dict] = None

    for key, entry in (grabbed or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("mode") != GRAB_MODE_SCHEDULED:
            continue

        match = SESSION_KEY_RE.match(str(key))
        if not match:
            continue
        season = int(match.group(1))
        round_num = int(match.group(2))
        element = int(match.group(3))

        session_type = _session_from_element(vocabulary, element, season)
        if session_type is None:
            continue  # unknown number - refuse to guess what it was

        round_library = (library_by_season.get(season) or {}).get(round_num) or {}
        if session_type not in round_library:
            continue  # grabbed but not (yet) in Plex: the card reports additions

        try:
            grabbed_at = datetime.fromisoformat(entry.get("timestamp", ""))
        except (ValueError, TypeError):
            continue

        added_at, source = grabbed_at, "grabbed"
        for event in activity_history or []:
            if not isinstance(event, dict):
                continue
            if event.get("id") == key and event.get("stage") == "complete":
                try:
                    added_at = datetime.fromisoformat(event.get("timestamp", ""))
                    source = "added"
                except (ValueError, TypeError):
                    pass  # keep the grab timestamp rather than lose the card
                break

        venue = ""
        calendar = calendars_by_season.get(season)
        if calendar:
            venue_round = get_round_by_number(calendar, round_num)
            if venue_round:
                venue = venue_round.get("venue", "")

        if best is None or grabbed_at > best["grabbed_at"]:
            best = {
                "name": session_type.full_short_name,
                "season": season,
                "round": round_num,
                "venue": venue,
                "release": entry.get("release", ""),
                "source": source,
                "timestamp": added_at,
                "timestamp_utc": _iso(added_at),
                "timestamp_label": _day_label(added_at),
                "grabbed_at": grabbed_at,
            }

    return best


# =============================================================================
# SCHEDULER HEARTBEAT
# =============================================================================

def scheduler_health(last_check: Optional[str], now: datetime,
                     in_weekend: bool) -> dict:
    """
    Report the last scheduled grabber check (not scheduler loop liveness).

    ``last_check`` is grabber_state.json's ``last_scheduled_check`` (ISO). The
    grabber checks every ``race_weekend_interval_minutes`` during a weekend
    but does not run outside one. During a weekend the stale threshold is
    twice the interval plus an hour of grace: late enough never to cry wolf over quiet hours or a slow
    run, early enough that a dead scheduler is visible the same day. The
    intervals are read from the scheduler's own CONFIG rather than restated.
    """
    expected = (
        SCHEDULER_CONFIG["race_weekend_interval_minutes"] if in_weekend
        else SCHEDULER_CONFIG["off_weekend_interval_minutes"]
    )
    threshold_minutes = expected * 2 + 60

    result = {
        "never": True,
        "last_check": None,
        "age_minutes": None,
        "age_label": "never",
        "stale": False,
        "expected_minutes": expected,
    }

    if not last_check:
        return result

    try:
        checked_at = datetime.fromisoformat(str(last_check))
    except (ValueError, TypeError):
        logger.warning("Could not parse last_scheduled_check: %r", last_check)
        return result

    age_minutes = int((now - checked_at).total_seconds() // 60)
    result.update({
        "never": False,
        "last_check": checked_at,
        "age_minutes": age_minutes,
        "age_label": format_age(age_minutes),
        "stale": in_weekend and age_minutes > threshold_minutes,
    })
    return result
