#!/usr/bin/env python3
"""
MotoGP Grabber - Automated Session Downloader
===============================================

Searches for MotoGP, Moto2, and Moto3 sessions via Prowlarr and sends them to SABnzbd.

Commands:
    backfill  - Find and grab missing sessions for a season
    scheduled - Check for recently aired sessions (used by scheduler)
    status    - Show status and upcoming sessions
    clear     - Clear grabber state

Usage:
    python3 motogp_grabber.py backfill --season 2025 --dry-run
    python3 motogp_grabber.py backfill --season 2025
    python3 motogp_grabber.py scheduled
    python3 motogp_grabber.py status
"""

import copy
import os
import sys
import json
import re
import argparse
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Add script directory to path
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

# Import from common module - including centralized logging
from motogp_common import (
    setup_logging, get_logger,
    SessionType, RaceClass,
    build_prefix, parse_prefix, get_session_from_element,
    load_calendar, get_round_by_number, has_session_times,
    identify_round, classify_session, detect_race_class,
    filter_releases, parse_release_title,
    DEFAULT_DESIRED_SESSIONS, CORE_SESSIONS,
    get_desired_sessions,
    SESSION_TO_CALENDAR_KEY, CALENDAR_KEY_TO_SESSION, CALENDAR_KEY_TO_DAY,
    CALENDAR_DAY_OFFSETS,
    parse_utc_offset,
    get_config_dir, get_library_path,
    is_round_active, round_runs_session, get_round_status,
    element_number, find_legacy_folders,
    GRAB_MODE_SCHEDULED, GRAB_MODE_BACKFILL,
    # Namespaced /config filenames: every shared state file carries its
    # series, because two series writing grabber_state.json into one volume
    # overwrite each other within a day.
    state_path,
)
from activity_emitter import emit_activity, ActivityStage, mark_error
from utils import failure_reasons
from utils.json_store import atomic_write_json, file_lock, read_json, three_way_merge
from utils.submission_guard import (intent_path, load_intents, make_intent,
                                    find_sab_job, validate_grabber_state, save_intents,
                                    try_run_lock)

# Import config
from utils.config_loader import (
    get_prowlarr_url, get_prowlarr_api_key,
    get_sabnzbd_url, get_sabnzbd_api_key,
    get_motogp_library_path,
    get_allowed_groups, get_required_quality,
    get_max_grabs_per_run, get_search_delay_hours,
    get_current_season,
)

# =============================================================================
# CONFIGURATION
# =============================================================================

SABNZBD_CATEGORY = "motogp"
CONFIG_DIR = get_config_dir()
STATE_FILE = state_path(CONFIG_DIR, "grabber_state")

# Sessions that have calendar times (can be scheduled)
SCHEDULABLE_SESSIONS = set(SESSION_TO_CALENDAR_KEY.keys())

# The operational window around a round: from one day before the weekend
# starts (Thursday running at flyaways) to two days after the race, so
# post-race searches still see the round. The home page (utils/home_summary)
# imports these rather than restating the numbers: a window that is described
# twice is a window the UI and the grabber eventually disagree about.
WINDOW_DAYS_BEFORE_WEEKEND = 1
WINDOW_DAYS_AFTER_RACE = 2

# =============================================================================
# LOGGING SETUP
# =============================================================================

# Module logger - will be configured by init_logging()
logger = get_logger("grabber")


def init_logging(verbose: bool = False):
    """Initialize logging for grabber script."""
    setup_logging(debug_mode=verbose)
    logger.info("MotoGP Grabber initialized")


# =============================================================================
# STATE MANAGEMENT
# =============================================================================

def _empty_grabber_state() -> dict:
    return {
        "searched": {},
        "grabbed": {},
        "failed": {},
        "last_run": None,
        "last_scheduled_check": None,
    }


class GrabberState:
    """Manages persistent state for the grabber."""

    def __init__(self, state_file: Path = STATE_FILE):
        self.state_file = state_file
        self.state = self._load()
        # Snapshot of what was loaded, so save() can tell which entries this
        # process changed and merge only those into the current file.
        self._snapshot = copy.deepcopy(self.state)

    def _load(self) -> dict:
        """Load state from file."""
        data = read_json(self.state_file, _empty_grabber_state(), log=logger)
        if not isinstance(data, dict):
            logger.error(
                "Grabber state in %s is not a JSON object; starting from empty state",
                self.state_file,
            )
            return _empty_grabber_state()
        state = _empty_grabber_state()
        state.update(data)
        return state

    def save(self):
        """
        Save state as a locked merge, not an overwrite.

        Scheduled and manually requested grabbers are separate processes and
        can overlap. Reloading under the sidecar lock and three-way merging
        (see ``utils.json_store``) means an entry another grabber committed
        while this one was running survives this save, while entries this
        process added, changed or deleted are applied. Atomic replacement
        alone would only have prevented torn JSON, not the lost update.
        A failed write propagates: pretending the state was saved would let
        the run report success while its grabs were never recorded.
        """
        self.state["last_run"] = datetime.now().isoformat()

        def _merge(disk):
            if not isinstance(disk, dict):
                disk = _empty_grabber_state()
            merged = three_way_merge(self._snapshot, self.state, disk)
            return merged

        with file_lock(self.state_file):
            disk = read_json(self.state_file, _empty_grabber_state(), log=logger)
            merged = _merge(disk)
            atomic_write_json(self.state_file, merged)
        self.state = merged
        self._snapshot = copy.deepcopy(merged)

    @staticmethod
    def make_session_key(season: int, round_num: int, session_type: SessionType) -> str:
        """Create unique key for a session."""
        return f"{season}_R{round_num:02d}_E{element_number(session_type, season):02d}"

    def was_searched(self, session_key: str, max_age_days: int = 7) -> bool:
        """Check if session was recently searched."""
        if session_key not in self.state["searched"]:
            return False

        searched_time = datetime.fromisoformat(self.state["searched"][session_key])
        age = datetime.now() - searched_time
        return age.days < max_age_days

    def was_grabbed(self, session_key: str) -> bool:
        """Check if session was already grabbed."""
        return session_key in self.state["grabbed"]

    def mark_searched(self, session_key: str):
        """Mark session as searched."""
        self.state["searched"][session_key] = datetime.now().isoformat()

    def mark_grabbed(self, session_key: str, release_title: str, nzo_id: Optional[str] = None,
                     mode: Optional[str] = None):
        """
        Mark session as grabbed.

        `mode` records which path found the release (GRAB_MODE_SCHEDULED or
        GRAB_MODE_BACKFILL). The home page's "latest scheduled addition" card
        (issue #51) counts only scheduled grabs: a backfill is a correction or
        a catch-up and must never masquerade as the machine keeping pace with
        a live weekend. Entries written before this field existed carry no
        mode and are treated as unknown rather than guessed from timestamps.
        """
        entry = {
            "timestamp": datetime.now().isoformat(),
            "release": release_title,
        }
        if nzo_id:
            entry["nzo_id"] = nzo_id
        if mode:
            entry["mode"] = mode
        self.state["grabbed"][session_key] = entry

    def mark_failed(self, session_key: str, reason, **extra):
        """
        Mark session search as failed.

        ``reason`` is either the plain string this always accepted, or a
        structured record from :func:`utils.failure_reasons.make`. The record
        form carries the stage that failed and whether waiting can help, so the
        Download tab can say "Prowlarr is unreachable" instead of "No results" -
        the two used to be indistinguishable, and one of them needs a human.
        A bare string stays supported because the shape is already on disk in
        every existing installation and in other call sites.
        """
        if isinstance(reason, dict):
            entry = dict(reason)
            entry.setdefault("timestamp", datetime.now().isoformat())
        else:
            entry = {
                "timestamp": datetime.now().isoformat(),
                "reason": reason,
            }
        entry.update({k: v for k, v in extra.items() if v is not None})
        self.state["failed"][session_key] = entry

    def clear_session(self, session_key: str):
        """Clear all state for a session."""
        self.state["searched"].pop(session_key, None)
        self.state["grabbed"].pop(session_key, None)
        self.state["failed"].pop(session_key, None)

    def update_scheduled_check(self):
        """Update last scheduled check timestamp."""
        self.state["last_scheduled_check"] = datetime.now().isoformat()


# =============================================================================
# CALENDAR UTILITIES
# =============================================================================

def get_session_times(round_info: dict, session_type: SessionType) -> Tuple[Optional[datetime], Optional[datetime]]:
    """
    Get GMT (start, end) for a session from the calendar.

    Both come from the same source so callers such as "upcoming sessions" do not
    have to guess the start time. Returns (None, None) when the round does not
    run the session or has no time for it.
    """
    calendar_key = SESSION_TO_CALENDAR_KEY.get(session_type)
    if not calendar_key:
        return (None, None)

    sessions = round_info.get("sessions", {})
    if not isinstance(sessions, dict):
        return (None, None)

    # The day block in the calendar is authoritative. From 2027 FP2 moves from
    # Saturday to Friday and the warm-up disappears, so resolving the key
    # through CALENDAR_KEY_TO_DAY would look in the wrong block (or find
    # nothing) and the session would be skipped at DEBUG level - silently,
    # because "no time" and "not run" look the same to the caller. Search the
    # day blocks that are actually present first; CALENDAR_KEY_TO_DAY is only
    # a fallback for legacy calendars written before the sessions block.
    day = None
    session_info = None
    for day_name, day_sessions in sessions.items():
        if isinstance(day_sessions, dict) and calendar_key in day_sessions:
            day = day_name
            session_info = day_sessions[calendar_key]
            break

    if session_info is None:
        day = CALENDAR_KEY_TO_DAY.get(calendar_key)
        if not day:
            return (None, None)
        session_info = (sessions.get(day) or {}).get(calendar_key)

    if not session_info:
        return (None, None)

    weekend_start = round_info.get("weekend_start")
    race_date = round_info.get("date")

    if not weekend_start or not race_date:
        return (None, None)

    try:
        weekend_start_dt = datetime.strptime(weekend_start, "%Y-%m-%d")
        race_date_dt = datetime.strptime(race_date, "%Y-%m-%d")

        if day == "sunday":
            local_date = race_date_dt
        elif day in CALENDAR_DAY_OFFSETS:
            local_date = weekend_start_dt + timedelta(days=CALENDAR_DAY_OFFSETS[day])
        else:
            return (None, None)

        duration = session_info.get("duration_mins", 45)
        local_time = session_info.get("local")
        offset_mins = parse_utc_offset(round_info.get("utc_offset"))

        # Prefer local + utc_offset. It is the exact instant even when the UTC
        # time crosses midnight: Phillip Island Friday 09:45 (+11:00) is
        # Thursday 22:45 UTC, not Friday 22:45. The stored gmt time is a
        # cross-check: when the two disagree (a synthetic or hand-edited
        # calendar) the stored gmt value wins, exactly as the WebUI does.
        if local_time and offset_mins is not None:
            try:
                hour, minute = map(int, local_time.split(":"))
                expected_gmt = (hour * 60 + minute - offset_mins) % 1440
                gmt_hm = session_info.get("gmt")
                gmt_matches = True
                if gmt_hm:
                    gmt_hour, gmt_minute = map(int, gmt_hm.split(":"))
                    gmt_matches = (gmt_hour * 60 + gmt_minute) == expected_gmt
                if gmt_matches:
                    start_dt = datetime(
                        local_date.year, local_date.month, local_date.day,
                        hour, minute,
                    ) - timedelta(minutes=offset_mins)
                    return (start_dt, start_dt + timedelta(minutes=duration))
            except (ValueError, TypeError):
                pass

        gmt_time = session_info.get("gmt")
        if not gmt_time:
            return (None, None)

        # A flyaway Friday morning can land on Thursday UTC (Phillip Island),
        # and a late Sunday race in the Americas can land on Monday UTC. The
        # calendar records that as gmt_day; shift by the difference between
        # the two day indexes rather than a hard-coded -1 per pair.
        gmt_day = session_info.get("gmt_day")
        if gmt_day:
            if gmt_day not in CALENDAR_DAY_OFFSETS or day not in CALENDAR_DAY_OFFSETS:
                return (None, None)
            local_date += timedelta(
                days=CALENDAR_DAY_OFFSETS[gmt_day] - CALENDAR_DAY_OFFSETS[day]
            )

        hour, minute = map(int, gmt_time.split(":"))
        start_dt = local_date.replace(hour=hour, minute=minute)
        return (start_dt, start_dt + timedelta(minutes=duration))

    except Exception as e:
        logger.debug(f"Error parsing session datetime: {e}")
        return (None, None)


def get_session_end_time(round_info: dict, session_type: SessionType) -> Optional[datetime]:
    """Get GMT end time for a session from calendar."""
    return get_session_times(round_info, session_type)[1]


def get_session_start_time(round_info: dict, session_type: SessionType) -> Optional[datetime]:
    """Get GMT start time for a session from calendar."""
    return get_session_times(round_info, session_type)[0]


def get_sessions_ready_for_search(calendar: dict, search_delay_hours: float = None) -> List[dict]:
    """
    Find sessions that have finished and are ready for search.
    Only considers the current race weekend and schedulable sessions.
    """
    ready = []
    now = datetime.now()
    today = now.date()

    # Use config value if not overridden
    if search_delay_hours is None:
        search_delay_hours = get_search_delay_hours()

    logger.debug(f"Checking for ready sessions. Current time: {now}, Today: {today}")
    logger.debug(f"Schedulable sessions: {[s.full_display_name for s in SCHEDULABLE_SESSIONS]}")

    rounds_checked = 0
    rounds_in_window = 0

    for round_info in calendar.get("rounds", []):
        round_num = round_info.get("round", 0)
        venue = round_info.get("venue", "Unknown")
        rounds_checked += 1

        # A cancelled/postponed round must never be searched for, even if its
        # old dates still fall inside the active window.
        if not is_round_active(round_info):
            logger.info(
                f"Round {round_num} {venue}: status is "
                f"'{get_round_status(round_info)}', skipping"
            )
            continue

        # Only consider current race weekend
        race_date_str = round_info.get("date")
        weekend_start_str = round_info.get("weekend_start")

        if not race_date_str:
            logger.debug(f"Round {round_num} {venue}: No race date, skipping")
            continue

        try:
            race_date = datetime.strptime(race_date_str, "%Y-%m-%d").date()
            if weekend_start_str:
                weekend_start = datetime.strptime(weekend_start_str, "%Y-%m-%d").date()
            else:
                weekend_start = race_date - timedelta(days=2)

            # Only process rounds within the active window
            # From 1 day before weekend start to 2 days after race
            window_start = weekend_start - timedelta(days=WINDOW_DAYS_BEFORE_WEEKEND)
            window_end = race_date + timedelta(days=WINDOW_DAYS_AFTER_RACE)

            if not (window_start <= today <= window_end):
                logger.debug(
                    f"Round {round_num} {venue}: Outside window "
                    f"({window_start} to {window_end}), skipping"
                )
                continue

            rounds_in_window += 1
            logger.info(
                f"📅 Round {round_num} {venue} is within active window "
                f"({window_start} to {window_end})"
            )

        except ValueError as e:
            logger.debug(f"Round {round_num}: Date parse error: {e}")
            continue

        # Use config delay (not calendar override)
        delay = search_delay_hours
        logger.debug(f"Round {round_num} {venue}: Using search delay of {delay} hours")

        # Only iterate over sessions that have calendar times
        desired = get_desired_sessions()

        for session_type in SCHEDULABLE_SESSIONS:
            if not desired.get(session_type, False):
                logger.debug(f"  {session_type.full_display_name}: Not in desired sessions, skipping")
                continue

            # The weekend format decides which sessions exist (e.g. no MotoGP
            # warm-up from 2027, Sprint Qualifying added).
            if not round_runs_session(round_info, session_type, calendar):
                logger.debug(
                    f"  {session_type.full_display_name}: Not run at this round, skipping"
                )
                continue

            end_time = get_session_end_time(round_info, session_type)

            if not end_time:
                logger.debug(f"  {session_type.full_display_name}: Could not determine end time, skipping")
                continue

            search_after = end_time + timedelta(minutes=delay * 60)

            logger.debug(
                f"  {session_type.full_display_name}: Ended {end_time}, "
                f"search after {search_after}, now {now}"
            )

            if now >= search_after:
                logger.info(f"  ✅ {session_type.full_display_name}: READY for search (ended {end_time})")
                ready.append({
                    "round": round_num,
                    "round_info": round_info,
                    "session_type": session_type,
                    "ended_at": end_time,
                    "search_after": search_after,
                })
            else:
                time_until = search_after - now
                logger.debug(
                    f"  {session_type.full_display_name}: Not ready yet, "
                    f"{time_until} until search window"
                )

    logger.info(
        f"📊 Checked {rounds_checked} rounds, {rounds_in_window} in active window, "
        f"{len(ready)} sessions ready"
    )

    return ready


def get_upcoming_sessions(calendar: dict, hours_ahead: int = 48) -> List[dict]:
    """Find sessions happening in the next X hours."""
    upcoming = []
    now = datetime.now()
    cutoff = now + timedelta(hours=hours_ahead)

    # Only iterate over sessions that have calendar times
    desired = get_desired_sessions()

    for round_info in calendar.get("rounds", []):
        round_num = round_info.get("round", 0)
        venue = round_info.get("venue", "Unknown")

        if not is_round_active(round_info):
            continue

        for session_type in SCHEDULABLE_SESSIONS:
            if not desired.get(session_type, False):
                continue

            if not round_runs_session(round_info, session_type, calendar):
                continue

            start_time, end_time = get_session_times(round_info, session_type)
            if not start_time or not end_time:
                continue

            if now < start_time <= cutoff:
                upcoming.append({
                    "round": round_num,
                    "venue": venue,
                    "session_type": session_type,
                    "starts_at": start_time,
                    "ends_at": end_time,
                })

    upcoming.sort(key=lambda x: x["starts_at"])
    return upcoming


# =============================================================================
# LIBRARY SCANNING
# =============================================================================

def scan_library_for_season(season: int) -> Dict[int, Set[SessionType]]:
    """Scan library to find existing sessions for a season."""
    season_path = os.path.join(get_motogp_library_path(), str(season))

    if not os.path.exists(season_path):
        logger.warning(f"Season folder not found: {season_path}")
        return {}

    existing: Dict[int, Set[SessionType]] = {}
    unreadable: List[str] = []

    logger.debug(f"Scanning library: {season_path}")

    for round_folder in os.listdir(season_path):
        round_path = os.path.join(season_path, round_folder)

        if not os.path.isdir(round_path):
            continue

        round_match = re.match(r'Round\s*(\d+)', round_folder)
        if not round_match:
            continue

        round_num = int(round_match.group(1))
        existing[round_num] = set()

        for session_folder in os.listdir(round_path):
            session_path = os.path.join(round_path, session_folder)

            if not os.path.isdir(session_path):
                continue

            parsed = parse_prefix(session_folder)
            if parsed:
                folder_year, folder_round, session_type = parsed

                if folder_year == season and folder_round == round_num:
                    existing[round_num].add(session_type)
                    logger.debug(f"Library: Round {round_num} has {session_type.full_display_name}")

        unreadable.extend(find_legacy_folders(season, os.listdir(round_path)))

    # A session folder whose prefix this version cannot read is not "absent" -
    # it is present and unmanaged. Saying so turns a silent duplicate download
    # (the grabber thinks the session is missing) into an actionable message.
    if unreadable:
        logger.warning(
            f"⚠️  {len(unreadable)} session folder(s) in {season} use a numbering "
            f"this version does not read and will be treated as missing: "
            f"{', '.join(sorted(unreadable)[:5])}"
            + (" ..." if len(unreadable) > 5 else "")
        )
        logger.warning(
            "    If these are pre-cutover files, clear them out or re-fetch them; "
            "see docs/CALENDAR_CHANGES.md."
        )

    total_sessions = sum(len(s) for s in existing.values())
    logger.info(f"📚 Library scan: Found {total_sessions} sessions across {len(existing)} rounds")

    return existing


def identify_missing_sessions(
    season: int,
    calendar: dict,
    existing: Dict[int, Set[SessionType]],
    round_filter: Optional[int] = None,
    only_past: bool = True
) -> List[dict]:
    """Identify missing desired sessions."""
    missing = []
    today = datetime.now().date()

    for round_entry in calendar.get("rounds", []):
        round_num = round_entry.get("round", 0)

        if round_filter and round_num != round_filter:
            continue

        # Never hunt for sessions belonging to a cancelled/postponed round.
        if not is_round_active(round_entry):
            logger.info(
                f"Round {round_num} {round_entry.get('venue', '?')}: status is "
                f"'{get_round_status(round_entry)}', not looking for sessions"
            )
            continue

        if only_past:
            round_date_str = round_entry.get("date", "")
            if round_date_str:
                try:
                    round_date = datetime.strptime(round_date_str, "%Y-%m-%d").date()
                    if round_date > today:
                        continue
                except ValueError:
                    pass

        existing_for_round = existing.get(round_num, set())

        for session_type, wanted in get_desired_sessions().items():
            if not wanted:
                continue

            # Skip sessions this weekend format does not run.
            if not round_runs_session(round_entry, session_type, calendar):
                continue

            if session_type not in existing_for_round:
                missing.append({
                    "round": round_num,
                    "round_info": round_entry,
                    "session_type": session_type,
                })

    return missing


def filter_missing_by_session_numbers(missing: List[dict], session_nums: List[int],
                                      season: int) -> List[dict]:
    """
    Keep only missing sessions whose element number is in session_nums.

    The element number comes from SESSION_ELEMENTS, never from
    int(session_type). The enum value and the element number diverged when the
    numbering moved to blocks of ten, so comparing them directly selects the
    wrong session: Moto3 FP1 has enum value 2 but element 10, which is Moto2
    FP2's enum value; Moto3 Practice has enum value 5 but element 20, which is
    Moto2 Race's enum value. That sent a Moto3 FP1 request to the Moto2 FP2
    search and a Moto3 Practice request to the Moto2 Race search.
    """
    wanted = set(session_nums)
    return [m for m in missing
            if element_number(m["session_type"], season) in wanted]


# =============================================================================
# PROWLARR SEARCH
# =============================================================================

def get_indexers() -> List[dict]:
    """Get list of enabled Prowlarr indexers."""
    prowlarr_url = get_prowlarr_url()
    prowlarr_api_key = get_prowlarr_api_key()

    if not prowlarr_url or not prowlarr_api_key:
        logger.error("Prowlarr not configured")
        return []

    try:
        url = f"{prowlarr_url}/api/v1/indexer"
        headers = {"X-Api-Key": prowlarr_api_key}

        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code == 200:
            indexers = response.json()
            return [i for i in indexers if i.get('enable', False)]
        return []
    except Exception as e:
        logger.error(f"Error getting indexers: {e}")
        return []


def parse_newznab_xml(xml_text: str, indexer_name: str = "Unknown") -> List[dict]:
    """Parse Newznab XML response."""
    releases = []

    try:
        if isinstance(xml_text, bytes):
            xml_text = xml_text.decode('utf-8', errors='replace')

        root = ET.fromstring(xml_text)

        for item in root.findall('.//item'):
            release = {'indexer': indexer_name}

            title_elem = item.find('title')
            if title_elem is not None and title_elem.text:
                release['title'] = title_elem.text.strip()

            guid_elem = item.find('guid')
            if guid_elem is not None and guid_elem.text:
                release['guid'] = guid_elem.text.strip()

            link_elem = item.find('link')
            if link_elem is not None and link_elem.text:
                release['link'] = link_elem.text.strip()

            enclosure = item.find('enclosure')
            if enclosure is not None:
                try:
                    release['size'] = int(enclosure.get('length', 0))
                except ValueError:
                    release['size'] = 0
                release['downloadUrl'] = enclosure.get('url', '')

            if release.get('title'):
                releases.append(release)

    except ET.ParseError as e:
        logger.debug(f"XML parse error: {e}")
    except Exception as e:
        logger.debug(f"Error parsing XML: {e}")

    return releases


def search_indexer(query: str, indexer_id: int,
                   indexer_name: str) -> Tuple[List[dict], Optional[str]]:
    """
    Search one indexer. Returns ``(releases, error)``.

    The error is returned rather than only logged, because "this indexer
    refused the request" and "this indexer answered with nothing" produce the
    same empty list and used to be stored as the same failure. One of them
    needs a human; the other needs patience.

    The error text is a short sentence naming the indexer and the status or
    exception type - never the URL. Prowlarr's search URL carries the API key in
    its query string, and a requests exception renders the whole URL; the caller
    passes the text through ``failure_reasons.sanitise`` as a second line of
    defence, but not putting it here is the first.
    """
    prowlarr_url = get_prowlarr_url()
    prowlarr_api_key = get_prowlarr_api_key()

    if not prowlarr_url or not prowlarr_api_key:
        # No URL and no key is a configuration fault, not an unreachable host.
        return [], "Prowlarr is not configured"

    try:
        url = f"{prowlarr_url}/{indexer_id}/api"
        params = {
            "t": "search",
            "q": query,
            "apikey": prowlarr_api_key,
            "limit": 100,
        }

        response = requests.get(url, params=params, timeout=60)

        if response.status_code == 200:
            return parse_newznab_xml(response.text, indexer_name), None
        if response.status_code in (401, 403):
            return [], f"{indexer_name} rejected the API key (HTTP {response.status_code})"
        return [], f"{indexer_name} returned HTTP {response.status_code}"
    except requests.exceptions.Timeout:
        return [], f"{indexer_name} timed out"
    except requests.exceptions.ConnectionError:
        return [], "Prowlarr is unreachable from this container"
    except Exception as e:  # noqa: BLE001 - one indexer must not stop the search
        logger.debug(f"Error searching {indexer_name}: {e}")
        return [], f"{indexer_name}: {type(e).__name__}"


def build_search_queries(round_info: dict, session_type: SessionType, season: int) -> List[str]:
    """Build search queries for a session."""
    queries = []

    venue = round_info.get("venue", "")
    aliases = round_info.get("aliases", [])
    round_num = round_info.get("round", 0)

    race_class = session_type.race_class
    class_search = race_class.display_name if race_class else "MotoGP"

    # Venue-based queries
    queries.append(f"{class_search} {venue} {season}")
    queries.append(f"{class_search} {season} {venue}")

    # Alias-based queries
    for alias in aliases[:3]:
        if len(alias) > 2 and alias.lower() not in ['uk', 'gp', 'tt']:
            queries.append(f"{class_search} {season} {alias}")

    # Round-based queries. The calendar may carry alternative round numbers
    # for the same event (see "round_aliases"), which happens when the
    # championship is renumbered mid-season after a postponement.
    for alias in [round_num] + list(round_info.get("round_aliases") or []):
        if not alias:
            continue
        queries.append(f"{class_search} {season} Round{alias:02d}")
        queries.append(f"{class_search} {season} Round{alias}")
        queries.append(f"{class_search} {season} Round {alias}")

    # Remove duplicates
    seen = set()
    unique_queries = []
    for q in queries:
        q_lower = q.lower()
        if q_lower not in seen:
            seen.add(q_lower)
            unique_queries.append(q)

    return unique_queries


def _blocked_titles(
    season: int, round_num: int, session_type: SessionType
) -> Set[str]:
    """
    Releases the user has marked as a mismatch for this target.

    A failure here must not stop a search - the worst case is that a marked
    release is reconsidered - but it is logged, because "my mismatch was
    ignored" and "the indexer never offered that title again" look identical
    from the outside.
    """
    try:
        from utils.release_mismatches import blocked_titles

        return blocked_titles(season, round_num, session_type.name)
    except Exception as exc:  # noqa: BLE001 - a search must still run
        logger.warning(f"Could not read release mismatch state: {exc}")
        return set()


def _build_episode_key(season: int, round_num: int, session_type: SessionType) -> str:
    """Build episode key for activity tracking."""
    return f"{season}_R{round_num:02d}_E{element_number(session_type, season):02d}"


def _configured_secrets() -> List[str]:
    """
    Credential values that must never reach a stored reason string.

    The reason is written to grabber_state.json, rendered in the Download tab
    and copied into reports, so a requests exception carrying
    ``/1/api?apikey=...`` would leak the key exactly as the 2026-11-05 incident
    did (AGENTS.md §7a). Passing the real values in as well means the
    redaction does not depend on recognising the shape of the URL.
    """
    return [
        value for value in (get_prowlarr_api_key(), get_sabnzbd_api_key())
        if value
    ]


def _record_failure(state: "GrabberState", session_key: str, stage: str,
                    detail: str = "", **extra) -> dict:
    """Store a structured failure for a session and return the record."""
    record = failure_reasons.make(
        stage, detail, secrets=_configured_secrets(), **extra
    )
    state.mark_failed(session_key, record)
    return record


def search_for_session(
    round_info: dict,
    session_type: SessionType,
    season: int,
    indexers: List[dict],
    calendar: dict = None,
    diagnostics: Optional[dict] = None,
) -> List[dict]:
    """
    Search all indexers for a session.

    ``diagnostics`` is an out-parameter: when a dict is passed, it is filled with
    the counters that let :func:`utils.failure_reasons.explain_search` say *why*
    a search produced nothing. The return value is unchanged, so callers that do
    not care (including every existing test) are unaffected - a second return
    value would have broken them for no benefit.

    The distinction being recorded is the one that used to be lost: no indexer
    answered, versus every indexer answered "nothing published", versus
    candidates arrived and the rules rejected them. All three used to be stored
    as "No results".
    """
    queries = build_search_queries(round_info, session_type, season)

    # This list is built from indexers that get_indexers() already filtered, so
    # an empty list means no *enabled* indexer - a configuration problem, not an
    # absence of releases.
    if not indexers:
        if diagnostics is not None:
            diagnostics.update({
                "setup_error": "no enabled Prowlarr indexers",
                "indexers": 0, "indexers_failed": 0,
                "candidates": 0, "filtered": 0,
            })
        logger.error("🔍 No enabled indexers - cannot search for any session")
        return []

    all_results = []
    seen_guids = set()
    errors: List[str] = []
    failed_indexers = 0

    round_num = round_info.get("round", "?")
    venue = round_info.get("venue", "Unknown")

    logger.info(f"🔍 Searching: Round {round_num} {venue} - {session_type.full_display_name}")

    for indexer in indexers:
        idx_id = indexer.get('id')
        idx_name = indexer.get('name', 'Unknown')
        indexer_answered = False

        for query in queries[:5]:
            results, error = search_indexer(query, idx_id, idx_name)
            if error:
                errors.append(error)
                continue
            indexer_answered = True

            for release in results:
                guid = release.get('guid', '')
                if guid and guid not in seen_guids:
                    seen_guids.add(guid)
                    all_results.append(release)

        if not indexer_answered:
            failed_indexers += 1

    logger.info(
        f"📋 Found {len(all_results)} candidates for "
        f"Round {round_num} {venue} - {session_type.full_display_name}"
    )

    # Releases the user has marked as a mismatch for this session (or
    # globally) must never be selected again. This is why a replacement
    # download works: without it, the same rules see the same candidates and
    # pick the same wrong release every time.
    excluded = _blocked_titles(season, round_num, session_type)

    # Filter using common module function
    filtered = filter_releases(
        all_results,
        session_type,
        season,
        round_info,
        get_required_quality(),
        get_allowed_groups(),
        calendar,
        excluded_titles=excluded,
    )

    if excluded:
        logger.info(
            f"🚫 {len(excluded)} release(s) marked as a mismatch were excluded "
            f"from this search"
        )

    logger.info(f"🎯 After filtering: {len(filtered)} matching releases")

    if diagnostics is not None:
        diagnostics.update({
            "setup_error": None,
            "indexers": len(indexers),
            "indexers_failed": failed_indexers,
            "candidates": len(all_results),
            "filtered": len(filtered),
            "errors": errors[:3],
            # Every candidate was dropped because the user blocked it: a
            # deliberate dead end, not a rule failure.
            "all_blocked": bool(all_results) and not filtered and bool(excluded),
        })

    return filtered


# =============================================================================
# SABNZBD INTEGRATION
# =============================================================================

def test_sabnzbd_connection() -> bool:
    """Test SABnzbd connection."""
    sabnzbd_url = get_sabnzbd_url()
    sabnzbd_api_key = get_sabnzbd_api_key()

    if not sabnzbd_url or not sabnzbd_api_key:
        logger.error("SABnzbd not configured")
        return False

    try:
        params = {
            "mode": "version",
            "apikey": sabnzbd_api_key,
            "output": "json"
        }

        response = requests.get(f"{sabnzbd_url}/api", params=params, timeout=10)

        if response.status_code == 200:
            result = response.json()
            version = result.get('version', 'Unknown')
            logger.info(f"SABnzbd connected: v{version}")
            return True
        return False
    except Exception as e:
        logger.error(f"SABnzbd connection failed: {e}")
        return False


def grab_release(release: dict, session_info: dict,
                 season: int) -> Tuple[Optional[str], str]:
    """
    Send release to SABnzbd with enriched name.

    Returns ``(nzo_id, error)``. On success ``nzo_id`` is a string (empty if
    SABnzbd did not provide one) and ``error`` is empty; on failure ``nzo_id``
    is None and ``error`` says what went wrong, because "Grab failed" covered
    an unreachable SABnzbd, a rejected API key and a refused release alike.

    The error text names the *kind* of fault and never includes the request URL
    - SABnzbd's API puts the key in the query string.
    """
    sabnzbd_url = get_sabnzbd_url()
    sabnzbd_api_key = get_sabnzbd_api_key()

    download_url = release.get('downloadUrl') or release.get('link')

    if not download_url:
        logger.error("No download URL available")
        return None, "the release has no download link"

    if not sabnzbd_url or not sabnzbd_api_key:
        logger.error("SABnzbd not configured")
        return None, "SABnzbd is not configured"

    # Build enriched name with prefix
    round_num = session_info.get("round", 0)
    round_info = session_info.get("round_info", {})
    session_type = session_info.get("session_type")
    venue = round_info.get("venue", "Unknown")

    prefix = build_prefix(season, round_num, session_type)
    enriched_name = f"{prefix} {venue} {session_type.full_display_name}"
    if session_info.get("_submission_token"):
        enriched_name += f" mgp-{session_info['_submission_token']}"

    try:
        params = {
            "mode": "addurl",
            "name": download_url,
            "nzbname": enriched_name,
            "cat": SABNZBD_CATEGORY,
            "apikey": sabnzbd_api_key,
            "output": "json"
        }

        response = requests.get(f"{sabnzbd_url}/api", params=params, timeout=30)

        if response.status_code == 200:
            try:
                result = response.json()
            except Exception as parse_err:
                logger.error(f"SABnzbd response parse error: {parse_err}")
                return None, "SABnzbd returned a response that is not JSON"

            if isinstance(result, dict) and result.get('status') is True:
                # Extract nzo_id from response (SABnzbd returns a list)
                nzo_ids = result.get('nzo_ids') or []
                nzo_id = nzo_ids[0] if isinstance(nzo_ids, list) and nzo_ids else None
                if not isinstance(nzo_id, str):
                    nzo_id = None

                release_title = release.get('title', 'Unknown')
                release_score = release.get('_score', 0)
                logger.info(f"✅ Grabbed: {enriched_name}")
                logger.info(f"   📦 Release: {release_title[:70]}")
                logger.info(f"   📊 Score: {release_score}")
                if nzo_id:
                    logger.info(f"   🆔 nzo_id: {nzo_id}")
                return (nzo_id if nzo_id else ""), ""
            elif isinstance(result, dict) and result.get('status') is False:
                error = result.get('error') or "Unknown"
                logger.error(f"SABnzbd error: {error}")
                return None, f"SABnzbd refused the release: {error}"
            return None, "SABnzbd returned an ambiguous response"
        return None, f"SABnzbd returned HTTP {response.status_code}"
    except requests.exceptions.Timeout:
        return None, "SABnzbd timed out accepting the release"
    except requests.exceptions.ConnectionError:
        return None, "SABnzbd is unreachable from this container"
    except Exception as e:
        logger.error(f"Grab error: {e}")
        return None, f"SABnzbd submission failed: {type(e).__name__}"


def get_sabnzbd_job_status(nzo_id: str) -> str:
    """Check the status of a SABnzbd job by nzo_id.

    Returns one of: 'inprogress', 'completed', 'failed', 'unknown'.
    Checks the queue first, then history.
    """
    sabnzbd_url = get_sabnzbd_url()
    sabnzbd_api_key = get_sabnzbd_api_key()

    if not sabnzbd_url or not sabnzbd_api_key or not nzo_id:
        return "unknown"

    # Check active queue
    try:
        params = {
            "mode": "queue",
            "nzo_ids": nzo_id,
            "apikey": sabnzbd_api_key,
            "output": "json",
        }
        response = requests.get(f"{sabnzbd_url}/api", params=params, timeout=10)
        if response.status_code == 200:
            try:
                data = response.json()
            except Exception:
                data = {}
            queue = data.get("queue") or {}
            slots = queue.get("slots") or []
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                if slot.get("nzo_id") == nzo_id:
                    status = (slot.get("status") or "").lower()
                    if status in ("downloading", "queued", "grabbing", "verifying",
                                  "repairing", "extracting", "moving", "running"):
                        return "inprogress"
                    if status == "failed":
                        return "failed"
                    return "inprogress"
    except Exception as e:
        logger.debug(f"SABnzbd queue check error for {nzo_id}: {e}")

    # Check history
    try:
        params = {
            "mode": "history",
            "nzo_ids": nzo_id,
            "apikey": sabnzbd_api_key,
            "output": "json",
        }
        response = requests.get(f"{sabnzbd_url}/api", params=params, timeout=10)
        if response.status_code == 200:
            try:
                data = response.json()
            except Exception:
                data = {}
            history = data.get("history") or {}
            slots = history.get("slots") or []
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                if slot.get("nzo_id") == nzo_id:
                    status = (slot.get("status") or "").lower()
                    if status == "completed":
                        return "completed"
                    if status in ("failed", "bad"):
                        return "failed"
                    return "unknown"
    except Exception as e:
        logger.debug(f"SABnzbd history check error for {nzo_id}: {e}")

    return "unknown"


def reconcile_grabbed_jobs(state: "GrabberState") -> int:
    """Check SABnzbd status for all grabbed jobs that have an nzo_id.

    If SABnzbd reports a job as failed, move the session to failed state and
    clear the grabbed entry so the session can be retried on the next run.

    Returns the number of sessions that were reset to allow retry.
    """
    grabbed = state.state.get("grabbed", {})
    retried = 0
    changed = False

    for session_key, entry in list(grabbed.items()):
        if not isinstance(entry, dict):
            continue
        nzo_id = entry.get("nzo_id")
        if not nzo_id or entry.get("sab_status") == "completed":
            continue

        job_status = get_sabnzbd_job_status(nzo_id)
        logger.debug(f"SABnzbd job {nzo_id} ({session_key}): {job_status}")

        if job_status == "completed":
            entry["sab_status"] = "completed"
            changed = True
        elif job_status == "failed":
            release = entry.get("release", "Unknown")
            logger.warning(
                f"SABnzbd job failed for {session_key} ({release}), "
                f"clearing for retry"
            )
            state.state["failed"][session_key] = {
                "timestamp": datetime.now().isoformat(),
                "reason": f"SABnzbd reported failure (nzo_id={nzo_id})",
            }
            del state.state["grabbed"][session_key]
            retried += 1
            changed = True

    if changed:
        state.save()
    return retried


# =============================================================================
# DISPLAY HELPERS
# =============================================================================

def format_size(size_bytes: int) -> str:
    """Format bytes as human-readable."""
    if not size_bytes:
        return "Unknown"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def format_time_until(dt: datetime) -> str:
    """Format time remaining until datetime."""
    delta = dt - datetime.now()
    if delta.total_seconds() < 0:
        return "now"

    hours = int(delta.total_seconds() // 3600)
    minutes = int((delta.total_seconds() % 3600) // 60)

    if hours > 24:
        return f"{hours // 24}d {hours % 24}h"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_time_ago(dt: datetime) -> str:
    """Format time since datetime."""
    delta = datetime.now() - dt
    if delta.total_seconds() < 0:
        return "not yet"

    hours = int(delta.total_seconds() // 3600)
    minutes = int((delta.total_seconds() % 3600) // 60)

    if hours > 24:
        return f"{hours // 24}d {hours % 24}h ago"
    elif hours > 0:
        return f"{hours}h {minutes}m ago"
    return f"{minutes}m ago"


def print_missing_summary(missing: List[dict]):
    """Print summary of missing sessions."""
    print("\n" + "=" * 70)
    print("MISSING SESSIONS")
    print("=" * 70)

    if not missing:
        print("\n  ✅ No missing sessions found!")
        return

    by_round = {}
    for item in missing:
        round_num = item['round']
        if round_num not in by_round:
            by_round[round_num] = {
                'venue': item['round_info'].get('venue', 'Unknown'),
                'sessions': []
            }
        by_round[round_num]['sessions'].append(item['session_type'].full_display_name)

    for round_num in sorted(by_round.keys()):
        info = by_round[round_num]
        print(f"\n  Round {round_num} - {info['venue']}:")
        for session in sorted(info['sessions']):
            print(f"    ❌ {session}")

    print(f"\n  Total missing: {len(missing)} sessions")
    print("=" * 70)


def print_search_results(results: List[dict], limit: int = 5):
    """Print search results."""
    if not results:
        print("    No matching results found")
        return

    for i, r in enumerate(results[:limit], 1):
        title = r.get('title', 'Unknown')
        size = format_size(r.get('size', 0))
        indexer = r.get('indexer', 'Unknown')
        score = r.get('_score', 0)
        broadcaster = r.get('_broadcaster', '')
        group = r.get('_group', '')

        print(f"    [{i}] {title[:60]}{'...' if len(title) > 60 else ''}")

        info_parts = [indexer, size, f"Score: {score}"]
        if broadcaster:
            info_parts.append(f"📺 {broadcaster.upper()}")
        if group:
            info_parts.append(f"🏷️ {group}")

        print(f"        {' | '.join(info_parts)}")


# =============================================================================
# QUALIFYING HELPERS
# =============================================================================

def _get_other_quali_session_key(
    season: int,
    round_num: int,
    session_type: SessionType
) -> Optional[str]:
    """Get the session key for the other qualifying session (Q1↔Q2)."""
    race_class = session_type.race_class
    session_name = session_type.display_name

    if session_name == "Qualifying 1":
        q2_map = {
            RaceClass.MOTO3: SessionType.MOTO3_Q2,
            RaceClass.MOTO2: SessionType.MOTO2_Q2,
            RaceClass.MOTOGP: SessionType.MOTOGP_Q2,
        }
        other_session = q2_map.get(race_class)
    elif session_name == "Qualifying 2":
        q1_map = {
            RaceClass.MOTO3: SessionType.MOTO3_Q1,
            RaceClass.MOTO2: SessionType.MOTO2_Q1,
            RaceClass.MOTOGP: SessionType.MOTOGP_Q1,
        }
        other_session = q1_map.get(race_class)
    else:
        return None

    if other_session:
        return GrabberState.make_session_key(season, round_num, other_session)
    return None


def reconcile_submission_intents(state):
    """Recover confirmed jobs after interruption; retain anything ambiguous."""
    path = intent_path(state.state_file.parent)
    with file_lock(path):
        intents = load_intents(path)
        if not intents:
            return
        validate_grabber_state(state.state_file)
        state.save()
        for token, intent in list(intents.items()):
            keys = intent.get('keys', [])
            if keys and all(
                isinstance(state.state.get('grabbed', {}).get(key), dict)
                and state.state['grabbed'][key].get('submission_token') == token
                for key in keys
            ):
                del intents[token]
                save_intents(path, intents)
                continue
            job_id = find_sab_job(token, get_sabnzbd_url(), get_sabnzbd_api_key(), requests.get)
            if not job_id:
                logger.warning("Uncertain SABnzbd submission %s (%s): check the downloader before clearing it", token, ', '.join(intent.get('keys', [])))
                continue
            keys = intent.get('keys', [])
            if not keys:
                logger.error("Submission %s has no session keys; leaving it pending", token)
                continue
            for index, key in enumerate(keys):
                title = intent.get('release', 'Unknown') + (' (combined)' if index else '')
                state.mark_grabbed(key, title, nzo_id=job_id, mode=intent.get('mode'))
                state.state['grabbed'][key]['submission_token'] = token
                state.state['failed'].pop(key, None)
            state.save()
            del intents[token]
            save_intents(path, intents)
            logger.info("Recovered SABnzbd job %s for %s", job_id, ', '.join(keys))


def guarded_grab(state, release, item, season, keys, mode, force=False):
    """Serialize and reserve submissions, including both halves of combined Q.

    Force bypasses search cooldown, never an unresolved reservation or an
    active download. A concurrent process may have committed after this one
    searched, so re-read state while holding the submission lock.
    """
    path = intent_path(state.state_file.parent)
    observed = {key: copy.deepcopy(state.state.get("grabbed", {}).get(key)) for key in keys}
    with file_lock(path):
        validate_grabber_state(state.state_file)
        state.save()  # merges changes, then refreshes the snapshot from disk
        intents = load_intents(path)
        for intent in intents.values():
            if set(keys).intersection(intent.get("keys", [])):
                return None, "Submission outcome is uncertain; inspect SABnzbd before clearing the pending submission"
        for key in keys:
            grabbed = state.state.get("grabbed", {})
            if key not in grabbed:
                continue
            existing = grabbed[key]
            if not force or existing != observed[key]:
                return None, "Another grabber has already submitted this session"
            if not isinstance(existing, dict):
                return None, "An earlier download has an unreadable state; inspect SABnzbd before clearing its state"
            job_id = existing.get("nzo_id")
            if existing.get("sab_status") != "completed":
                if not job_id or get_sabnzbd_job_status(job_id) != "completed":
                    return None, (f"An earlier download is still active or its status is unknown; inspect SABnzbd, then if safe run "
                                  f"motogp_grabber.py clear --session {key} before retrying")
        if not force:
            existing = scan_library_for_season(season)
            if item['session_type'] in existing.get(item['round'], set()):
                return None, "The session appeared in the library while searching"
        token, intent = make_intent(keys, release.get('title', 'Unknown'), mode)
        intents[token] = intent
        save_intents(path, intents)
        # Never put the release URL or credentials into the durable intent.
        tagged_item = dict(item, _submission_token=token)
        nzo_id, error = grab_release(release, tagged_item, season)
        if nzo_id is None:
            # Only a definite refusal/preflight is safe to retry. Network,
            # malformed response and HTTP failures may have accepted the job.
            definite = error.startswith(("SABnzbd refused the release:",
                                         "the release has no download link",
                                         "SABnzbd is not configured"))
            if definite:
                del intents[token]
                save_intents(path, intents)
            else:
                error += "; submission outcome uncertain — check SABnzbd before clearing the pending submission"
            return None, error
        if not nzo_id:
            return None, "SABnzbd accepted the request without a job ID; submission outcome requires reconciliation before retry"
        for index, key in enumerate(keys):
            title = release.get('title', 'Unknown') + (" (combined)" if index else "")
            state.mark_grabbed(key, title, nzo_id=nzo_id or None, mode=mode)
            state.state['grabbed'][key]['submission_token'] = token
            state.state['failed'].pop(key, None)
        state.save()  # If this fails the durable intent deliberately remains.
        del intents[token]
        save_intents(path, intents)
        return nzo_id, ""


# =============================================================================
# MAIN COMMANDS
# =============================================================================

def cmd_backfill(args):
    """Backfill missing sessions for a season."""
    season = args.season
    dry_run = args.dry_run
    round_filter = args.round
    force = args.force

    print(f"\n{'=' * 60}")
    print(f"  MOTOGP GRABBER - BACKFILL MODE")
    print(f"{'=' * 60}")
    print(f"\n  Season:     {season}")
    print(f"  Dry Run:    {dry_run}")
    print(f"  Max Grabs:  {get_max_grabs_per_run()}")
    if round_filter:
        print(f"  Round:      {round_filter} only")
    print(f"  Time:       {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    state = GrabberState()

    # Reconcile previously grabbed jobs against SABnzbd status
    if not dry_run:
        validate_grabber_state(state.state_file)
        reconcile_submission_intents(state)
        reset_count = reconcile_grabbed_jobs(state)
        if reset_count > 0:
            print(f"\n  ♻️  Reconciled {reset_count} failed SABnzbd job(s) — will retry")
            state.save()

    calendar = load_calendar(season)
    if not calendar:
        print(f"\n❌ Could not load calendar for {season}")
        return 1

    print(f"\n  Calendar:   {len(calendar.get('rounds', []))} rounds loaded")

    # Connection tests
    print(f"\n{'─' * 40}")
    print("CONNECTION TESTS")
    print(f"{'─' * 40}")

    try:
        indexers = get_indexers()
        if indexers:
            print(f"  ✅ Prowlarr: {len(indexers)} indexers")
        else:
            print("  ❌ Prowlarr: No indexers found")
            return 1
    except Exception as e:
        print(f"  ❌ Prowlarr: {e}")
        return 1

    if not dry_run:
        if not test_sabnzbd_connection():
            print("  ❌ SABnzbd: Connection failed")
            return 1
    else:
        print("  ⏭️  SABnzbd: Skipped (dry run)")

    # Scan library
    print(f"\n{'─' * 40}")
    print("SCANNING LIBRARY")
    print(f"{'─' * 40}")

    existing = scan_library_for_season(season)
    total_existing = sum(len(sessions) for sessions in existing.values())
    print(f"  Found {total_existing} existing sessions across {len(existing)} rounds")

    missing = identify_missing_sessions(season, calendar, existing, round_filter)

    # Filter by specific sessions if provided
    sessions_filter = getattr(args, 'sessions', None)
    if sessions_filter:
        session_nums = []
        for s in sessions_filter.split(','):
            s = s.strip().upper()
            if s.startswith('E'):
                s = s[1:]
            try:
                session_nums.append(int(s))
            except ValueError:
                pass

        if session_nums:
            missing = filter_missing_by_session_numbers(missing, session_nums, season)
            print(f"\n  Filtered to {len(missing)} sessions: {sessions_filter}")

    print_missing_summary(missing)

    if not missing:
        state.save()
        return 0

    # Filter by state with 30-minute cooldown
    if not force:
        filtered_missing = []
        for item in missing:
            session_key = state.make_session_key(
                season, item['round'], item['session_type']
            )

            if state.was_grabbed(session_key):
                continue

            # 30-minute cooldown (matching f1arr behaviour)
            if session_key in state.state.get("searched", {}):
                try:
                    searched_time = datetime.fromisoformat(
                        state.state["searched"][session_key]
                    )
                    minutes_since = (datetime.now() - searched_time).total_seconds() / 60
                    if minutes_since < 30:
                        logger.debug(
                            f"Skipping {session_key}: searched {int(minutes_since)}m ago"
                        )
                        continue
                except (ValueError, KeyError):
                    pass  # Invalid timestamp, proceed with search

            filtered_missing.append(item)

        skipped = len(missing) - len(filtered_missing)
        if skipped > 0:
            print(f"\n  ⏭️  Skipping {skipped} recently searched sessions (within 30 mins)")
            print(f"     (use --force to search again)")

        missing = filtered_missing

    if not missing:
        print("\n  ℹ️  All missing sessions have been recently searched")
        state.save()
        return 0

    # Search and grab
    print(f"\n{'─' * 40}")
    print("SEARCHING & GRABBING")
    print(f"{'─' * 40}")

    grabs_this_run = 0
    grabbed_titles = set()
    sessions_satisfied_this_run = set()
    max_grabs = get_max_grabs_per_run()

    for item in missing:
        if grabs_this_run >= max_grabs:
            print(f"\n  ⚠️  Reached max grabs limit ({max_grabs})")
            break

        round_num = item['round']
        round_info = item['round_info']
        session_type = item['session_type']
        venue = round_info.get('venue', 'Unknown')

        session_key = state.make_session_key(season, round_num, session_type)

        if session_key in sessions_satisfied_this_run:
            print(f"\n  ⏭️  Round {round_num} {venue}: {session_type.full_display_name}")
            print(f"      Already covered by combined release grabbed earlier")
            continue

        logger.info(f"🔍 Searching: Round {round_num} {venue} - {session_type.full_display_name}")
        print(f"\n  🔍 Round {round_num} {venue}: {session_type.full_display_name}")

        # Emit SEARCHING activity
        episode_key = _build_episode_key(season, round_num, session_type)
        emit_activity(
            episode_key=episode_key,
            episode_name=session_type.full_display_name,
            stage=ActivityStage.SEARCHING,
            details=f"Round {round_num} {venue}",
            round_info={"round": round_num, "venue": venue}
        )

        diagnostics: dict = {}
        results = search_for_session(
            round_info, session_type, season, indexers, calendar,
            diagnostics=diagnostics,
        )

        state.mark_searched(session_key)

        if not results:
            # Why nothing came back decides what the user should do, so the
            # cause is recorded rather than flattened into "No results" (which
            # made a Prowlarr outage look exactly like "not published yet").
            cause = failure_reasons.explain_search(diagnostics)
            record = _record_failure(
                state, session_key, cause["stage"] or failure_reasons.STAGE_NO_CANDIDATES,
                cause.get("detail", ""),
            )
            print(f"    ❌ {record['reason']}")
            logger.info(
                f"❌ No candidates ({record['stage']}): Round {round_num} {venue} - "
                f"{session_type.full_display_name}"
            )
            mark_error(
                episode_key=episode_key,
                error_message=record["reason"],
            )
            continue

        available = [r for r in results if r.get('title', '').lower() not in grabbed_titles]

        if not available:
            print(f"    ⚠️  All results already grabbed this run")
            _record_failure(
                state, session_key, failure_reasons.STAGE_REJECTED,
                "every matching release was already grabbed in this run",
            )
            continue

        logger.info(
            f"📋 Found {len(available)} candidates for "
            f"Round {round_num} {venue} - {session_type.full_display_name}"
        )
        print(f"    Found {len(available)} matching releases:")
        print_search_results(available, limit=5)

        # Emit CANDIDATES_FOUND activity
        emit_activity(
            episode_key=episode_key,
            episode_name=session_type.full_display_name,
            stage=ActivityStage.CANDIDATES_FOUND,
            details=f"Found {len(available)} candidates",
            round_info={"round": round_num, "venue": venue}
        )

        best = available[0]
        best_title = best.get('title', 'Unknown')
        is_combined = best.get('_combined_quali', False)

        if dry_run:
            print(f"\n    [DRY-RUN] Would grab: {best_title[:50]}")
            grabbed_titles.add(best_title.lower())
            grabs_this_run += 1

            if is_combined:
                sessions_satisfied_this_run.add(session_key)
                other_key = _get_other_quali_session_key(season, round_num, session_type)
                if other_key:
                    sessions_satisfied_this_run.add(other_key)
        else:
            logger.info(f"⬇️ Sending to SABnzbd: {best_title[:60]}")
            print(f"\n    ⬇️  Sending to SABnzbd...")

            # Emit DOWNLOADING activity
            emit_activity(
                episode_key=episode_key,
                episode_name=session_type.full_display_name,
                stage=ActivityStage.DOWNLOADING,
                details=f"Sending to SABnzbd",
                round_info={"round": round_num, "venue": venue},
                release_name=best_title[:80]
            )

            other_key = _get_other_quali_session_key(season, round_num, session_type) if is_combined else None
            keys = [session_key] + ([other_key] if other_key else [])
            nzo_id, grab_error = guarded_grab(state, best, item, season, keys, GRAB_MODE_BACKFILL, force)
            if nzo_id is not None:
                print(f"    ✅ Grab successful!")
                grabbed_titles.add(best_title.lower())
                grabs_this_run += 1
                sessions_satisfied_this_run.update(keys)
            else:
                if grab_error in ("Another grabber has already submitted this session",
                                  "The session appeared in the library while searching"):
                    logger.info("Skipping %s: %s", session_key, grab_error)
                    continue
                record = _record_failure(
                    state, session_key, failure_reasons.STAGE_SAB_SUBMIT,
                    grab_error,
                )
                print(f"    ❌ {record['reason']}")
                logger.error(
                    f"Grab failed for {session_key}: {record['reason']}"
                )
                mark_error(
                    episode_key=episode_key,
                    error_message=record["reason"],
                )

    state.save()

    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Sessions searched:  {len(missing)}")
    print(f"  Releases grabbed:   {grabs_this_run}")
    print(f"{'=' * 60}\n")

    return 0


def cmd_scheduled(args):
    """Run scheduled check for current season."""
    season = args.season or get_current_season()
    dry_run = args.dry_run
    force = args.force
    search_delay = getattr(args, 'search_delay', None)

    logger.info(f"Starting scheduled check for season {season}")
    if search_delay is not None:
        logger.info(f"Using search delay override: {search_delay} hours")

    print(f"\n{'=' * 60}")
    print(f"  MOTOGP GRABBER - SCHEDULED MODE")
    print(f"{'=' * 60}")
    print(f"\n  Season:       {season}")
    print(f"  Dry Run:      {dry_run}")
    effective_delay = search_delay if search_delay is not None else get_search_delay_hours()
    print(f"  Search Delay: {effective_delay} hours")
    print(f"  Time (Local): {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    state = GrabberState()

    # Reconcile previously grabbed jobs against SABnzbd status
    if not dry_run:
        validate_grabber_state(state.state_file)
        reconcile_submission_intents(state)
        reset_count = reconcile_grabbed_jobs(state)
        if reset_count > 0:
            print(f"\n  ♻️  Reconciled {reset_count} failed SABnzbd job(s) — will retry")
            state.save()

    calendar = load_calendar(season)
    if not calendar:
        print(f"\n❌ Could not load calendar for {season}")
        logger.error(f"Could not load calendar for {season}")
        return 1

    if not has_session_times(calendar):
        print(f"\n❌ Calendar for {season} doesn't have session times")
        print("   Use 'backfill' mode for older seasons")
        logger.error(f"Calendar for {season} doesn't have session times")
        return 1

    print(f"\n  Calendar: {len(calendar.get('rounds', []))} rounds with session times")
    logger.info(f"Loaded calendar with {len(calendar.get('rounds', []))} rounds")

    # Get sessions ready for search
    logger.info("Checking for sessions ready to search...")
    ready = get_sessions_ready_for_search(calendar, search_delay_hours=search_delay)

    logger.info(f"get_sessions_ready_for_search returned {len(ready)} sessions")
    for r in ready:
        logger.info(
            f"  Ready: Round {r['round']} "
            f"{r['round_info'].get('venue', '?')} - "
            f"{r['session_type'].full_display_name}"
        )

    # Scan library for existing sessions
    logger.info("Scanning library for existing sessions...")
    existing = scan_library_for_season(season)

    # Filter out sessions already in library, already grabbed, or within rate limit
    filtered_ready = []
    for item in ready:
        round_num = item['round']
        session_type = item['session_type']
        venue = item['round_info'].get('venue', 'Unknown')

        # Skip if already in library
        if round_num in existing and session_type in existing[round_num]:
            logger.info(
                f"Skipping Round {round_num} {venue} "
                f"{session_type.full_display_name}: already in library"
            )
            continue

        session_key = state.make_session_key(season, round_num, session_type)

        # Skip if already grabbed (sent to downloader)
        if state.was_grabbed(session_key):
            logger.info(f"Skipping {session_key}: already grabbed (in state)")
            continue

        # Rate limit: don't search same session more than once per 30 minutes
        if not force and session_key in state.state.get("searched", {}):
            try:
                searched_time = datetime.fromisoformat(
                    state.state["searched"][session_key]
                )
                minutes_since = (datetime.now() - searched_time).total_seconds() / 60
                if minutes_since < 30:
                    logger.debug(
                        f"Skipping {session_key}: searched "
                        f"{int(minutes_since)}m ago (rate limit)"
                    )
                    continue
            except (ValueError, KeyError):
                pass  # Invalid timestamp, proceed with search

        logger.info(
            f"Will search for: Round {round_num} {venue} - "
            f"{session_type.full_display_name}"
        )
        filtered_ready.append(item)

    if not filtered_ready:
        print("\n  ℹ️  No new sessions to search for")
        logger.info("No new sessions to search for")
        state.update_scheduled_check()
        state.save()
        return 0

    print(f"\n  Ready sessions: {len(filtered_ready)}")

    # Connection tests
    print(f"\n{'─' * 40}")
    print("CONNECTION TESTS")
    print(f"{'─' * 40}")

    try:
        indexers = get_indexers()
        if indexers:
            print(f"  ✅ Prowlarr: {len(indexers)} indexers")
            logger.info(f"Prowlarr connected: {len(indexers)} indexers")
        else:
            print("  ❌ Prowlarr: No indexers found")
            logger.error("Prowlarr: No indexers found")
            return 1
    except Exception as e:
        print(f"  ❌ Prowlarr: {e}")
        logger.error(f"Prowlarr connection failed: {e}")
        return 1

    if not dry_run:
        if not test_sabnzbd_connection():
            print("  ❌ SABnzbd: Connection failed")
            return 1
    else:
        print("  ⏭️  SABnzbd: Skipped (dry run)")

    # Search and grab
    print(f"\n{'─' * 40}")
    print(f"SEARCHING & GRABBING (Season {season})")
    print(f"{'─' * 40}")

    grabs_this_run = 0
    max_grabs = get_max_grabs_per_run()

    for item in filtered_ready:
        if grabs_this_run >= max_grabs:
            print(f"\n  ⚠️  Reached max grabs limit ({max_grabs})")
            break

        round_num = item['round']
        round_info = item['round_info']
        session_type = item['session_type']
        venue = round_info.get('venue', 'Unknown')

        session_key = state.make_session_key(season, round_num, session_type)

        if state.was_grabbed(session_key):
            logger.info("Skipping %s: covered by an earlier combined grab", session_key)
            continue

        ended_ago = format_time_ago(item['ended_at'])
        print(f"\n  🔍 Round {round_num} {venue}: {session_type.full_display_name}")
        print(f"      Session ended: {ended_ago}")

        logger.info(
            f"Searching for Round {round_num} {venue} - "
            f"{session_type.full_display_name}"
        )

        # Emit the same grabber lifecycle as the backfill path. Without this,
        # a scheduled download's first activity event was the watcher's
        # DOWNLOAD_COMPLETE, so the activity tab appeared to jump straight to
        # "Sorting and renaming" and the session looked like it came from
        # nowhere. The download itself was never broken - only the reporting.
        episode_key = _build_episode_key(season, round_num, session_type)
        emit_activity(
            episode_key=episode_key,
            episode_name=session_type.full_display_name,
            stage=ActivityStage.SEARCHING,
            details=f"Round {round_num} {venue}",
            round_info={"round": round_num, "venue": venue},
        )

        diagnostics: dict = {}
        results = search_for_session(
            round_info, session_type, season, indexers, calendar,
            diagnostics=diagnostics,
        )

        state.mark_searched(session_key)

        if not results:
            cause = failure_reasons.explain_search(diagnostics)
            record = _record_failure(
                state, session_key, cause["stage"] or failure_reasons.STAGE_NO_CANDIDATES,
                cause.get("detail", ""),
            )
            print(f"    ❌ {record['reason']}")
            logger.info(
                f"No matching releases ({record['stage']}) for Round {round_num} "
                f"{venue} - {session_type.full_display_name}"
            )
            mark_error(
                episode_key=episode_key,
                error_message=record["reason"],
            )
            continue

        print(f"    Found {len(results)} matching releases:")
        print_search_results(results, limit=3)

        emit_activity(
            episode_key=episode_key,
            episode_name=session_type.full_display_name,
            stage=ActivityStage.CANDIDATES_FOUND,
            details=f"Found {len(results)} candidates",
            round_info={"round": round_num, "venue": venue},
        )

        best = results[0]

        if dry_run:
            prefix = build_prefix(season, round_num, session_type)
            print(f"\n    [DRY-RUN] Would grab as: {prefix} {venue} {session_type.full_display_name}")
            grabs_this_run += 1
        else:
            print(f"\n    ⬇️  Grabbing: {best.get('title', 'Unknown')[:50]}")

            emit_activity(
                episode_key=episode_key,
                episode_name=session_type.full_display_name,
                stage=ActivityStage.DOWNLOADING,
                details="Sending to SABnzbd",
                round_info={"round": round_num, "venue": venue},
                release_name=best.get("title", "Unknown")[:80],
            )

            other_key = _get_other_quali_session_key(season, round_num, session_type) if best.get('_combined_quali') else None
            keys = [session_key] + ([other_key] if other_key else [])
            nzo_id, grab_error = guarded_grab(state, best, item, season, keys, GRAB_MODE_SCHEDULED, False)
            if nzo_id is not None:
                grabs_this_run += 1
            else:
                if grab_error in ("Another grabber has already submitted this session",
                                  "The session appeared in the library while searching"):
                    logger.info("Skipping %s: %s", session_key, grab_error)
                    continue
                record = _record_failure(
                    state, session_key, failure_reasons.STAGE_SAB_SUBMIT,
                    grab_error,
                )
                print(f"    ❌ {record['reason']}")
                logger.error(
                    f"Grab failed for {session_key}: {record['reason']}"
                )
                mark_error(
                    episode_key=episode_key,
                    error_message=record["reason"],
                )

    state.update_scheduled_check()
    state.save()

    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Sessions ready:    {len(filtered_ready)}")
    print(f"  Releases grabbed:  {grabs_this_run}")
    print(f"{'=' * 60}\n")

    logger.info(f"Scheduled check complete: {grabs_this_run} grabbed")

    return 0


def cmd_status(args):
    """Show status and upcoming sessions."""
    season = args.season or get_current_season()

    print(f"\n{'=' * 60}")
    print(f"  MOTOGP GRABBER - STATUS")
    print(f"{'=' * 60}")

    state = GrabberState()

    print(f"\n  Last run:             {state.state.get('last_run', 'Never')}")
    print(f"  Last scheduled check: {state.state.get('last_scheduled_check', 'Never')}")
    print(f"\n  Searched sessions:    {len(state.state.get('searched', {}))}")
    print(f"  Grabbed sessions:     {len(state.state.get('grabbed', {}))}")
    print(f"  Failed sessions:      {len(state.state.get('failed', {}))}")

    calendar = load_calendar(season)
    if calendar and has_session_times(calendar):
        upcoming = get_upcoming_sessions(calendar, hours_ahead=72)
        if upcoming:
            print(f"\n  Upcoming sessions (next 72h):")
            for s in upcoming[:5]:
                starts = s['starts_at'].strftime("%a %H:%M")
                print(f"    ⏰ {starts} - {s['session_type'].full_display_name}")

    print(f"\n{'=' * 60}\n")
    return 0


def cmd_clear(args):
    """Clear grabber state."""
    state = GrabberState()

    if args.all:
        state.state = {
            "searched": {},
            "grabbed": {},
            "failed": {},
            "last_run": None,
            "last_scheduled_check": None,
        }
        print("✅ All state cleared")
    elif args.session:
        state.clear_session(args.session)
        print(f"✅ Cleared state for: {args.session}")
    elif args.searched:
        state.state["searched"] = {}
        print("✅ Cleared searched state")
    elif args.failed:
        state.state["failed"] = {}
        print("✅ Cleared failed state")

    state.save()
    return 0


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="MotoGP Grabber - Automated Session Downloader",
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # Backfill
    backfill_p = subparsers.add_parser('backfill', help='Find and grab missing sessions')
    backfill_p.add_argument('--season', type=int, required=True, help='Season year')
    backfill_p.add_argument('--round', type=int, help='Specific round number')
    backfill_p.add_argument('--dry-run', action='store_true', help='Preview only')
    backfill_p.add_argument('--force', action='store_true', help='Ignore search history')
    backfill_p.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    backfill_p.add_argument('--sessions', type=str, help='Comma-separated session numbers (e.g., E09,E11,E12)')

    # Scheduled
    scheduled_p = subparsers.add_parser('scheduled', help='Check for recently aired sessions')
    scheduled_p.add_argument('--season', type=int, help='Season year')
    scheduled_p.add_argument('--dry-run', action='store_true', help='Preview only')
    scheduled_p.add_argument('--force', action='store_true', help='Ignore search history')
    scheduled_p.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    scheduled_p.add_argument('--search-delay', type=float, help='Hours to wait after session ends before searching (fractional hours supported)')

    # Recovery does not need an active meeting, Prowlarr, or a calendar.
    subparsers.add_parser('reconcile', help='Reconcile uncertain SABnzbd submissions')

    # Status
    status_p = subparsers.add_parser('status', help='Show status and upcoming')
    status_p.add_argument('--season', type=int, help='Season year')

    # Clear
    clear_p = subparsers.add_parser('clear', help='Clear grabber state')
    clear_p.add_argument('--all', action='store_true', help='Clear all state')
    clear_p.add_argument('--searched', action='store_true', help='Clear searched only')
    clear_p.add_argument('--failed', action='store_true', help='Clear failed only')
    clear_p.add_argument('--session', type=str, help='Clear specific session key')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    verbose = getattr(args, 'verbose', False)
    init_logging(verbose)

    if args.command == 'status':
        return cmd_status(args)
    # The WebUI's job slot is process-local and does not include the
    # scheduler. Refuse overlapping whole runs rather than queue a hidden
    # forced backfill that could submit as soon as a scheduled job finishes.
    with try_run_lock(CONFIG_DIR) as acquired:
        if not acquired:
            logger.warning("Another grabber is running; wait for it to finish and retry")
            print("Another grabber is running; wait for it to finish and retry.")
            return 2
        if args.command == 'backfill':
            return cmd_backfill(args)
        if args.command == 'scheduled':
            return cmd_scheduled(args)
        if args.command == 'clear':
            return cmd_clear(args)
        if args.command == 'reconcile':
            state = GrabberState()
            validate_grabber_state(state.state_file)
            reconcile_submission_intents(state)
            reconcile_grabbed_jobs(state)
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())