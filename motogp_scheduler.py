#!/usr/bin/env python3
"""
MotoGP Scheduler - Always-On Session Monitor with Download Watcher
====================================================================

Runs continuously inside a Docker container:
1. Monitors for race weekends and triggers grabber for missing sessions
2. Watches download folder for completed downloads and triggers sorter

Usage:
    python3 motogp_scheduler.py
    python3 motogp_scheduler.py --no-watcher   # Disable download watcher
    python3 motogp_scheduler.py --watcher-only # Only run watcher
    python3 motogp_scheduler.py --debug        # Enable debug logging
"""

import functools
import os
import sys
import time
import signal
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Set

# Add script directory to path
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

# Bind this process to a series BEFORE importing anything that compiles one in.
# Every table in motogp_common is the bound series' vocabulary, so this has to
# happen first: --series (or MOTOGPARR_SERIES, which the scheduler sets for each
# subprocess it starts) decides it, and the default series answers when neither
# is given. See series/binding.py.
from series.binding import bind_from_argv  # noqa: E402
BOUND_SERIES = bind_from_argv()

# Import centralized logging FIRST
from motogp_common import (
    setup_logging, get_logger, load_calendar, has_session_times, get_config_dir,
    is_round_active,
)
from motogp_watcher import create_watcher

from utils.config_loader import (
    get_series_library_path,
    get_series_downloads_path,
    get_downloads_path,
    get_current_season,
    get_search_delay_hours,
    reload_settings,
)
from utils.background_health import write_heartbeat

# =============================================================================
# CONFIGURATION
# =============================================================================

CONFIG = {
    "race_weekend_interval_minutes": 60,
    "off_weekend_interval_minutes": 360,
    "post_race_check_days": 2,
    "quiet_hours": {
        "enabled": True,
        "start": 1,
        "end": 6,
        "interval_minutes": 120,
    },
    "grabber_script": SCRIPT_DIR / "motogp_grabber.py",
    "sort_script": SCRIPT_DIR / "motogp_sort.py",
    "python_path": "/opt/venv/bin/python3",
}

CONFIG_DIR = get_config_dir()

# =============================================================================
# DOWNLOAD PROCESSOR
# =============================================================================

def process_download(folder_path: str, series: Optional[str] = None) -> bool:
    """
    Process a completed download folder.
    
    Called by the watcher when a new download is ready. The series is passed
    explicitly and forwarded as ``--series``: the sort runs in its own process
    and would otherwise bind the default series for every folder, sweeping both
    download queues into the MotoGP library.
    """
    logger = get_logger("scheduler")
    logger.info(f"📂 Processing download: {os.path.basename(folder_path)}")
    
    try:
        command = [
            CONFIG["python_path"],
            str(CONFIG["sort_script"]),
            "--source", folder_path
        ]
        if series:
            command += ["--series", series]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=600  # 10 minute timeout
        )
        
        if result.returncode == 0:
            logger.info(f"✅ Sort completed: {os.path.basename(folder_path)}")
            return True
        else:
            logger.error(f"❌ Sort failed (exit code {result.returncode})")
            if result.stderr:
                logger.error(f"   Error: {result.stderr[:500]}")
            if result.stdout:
                logger.debug(f"   Output: {result.stdout[-500:]}")
            return False
    
    except subprocess.TimeoutExpired:
        logger.error(f"❌ Sort timed out for: {folder_path}")
        return False
    except Exception as e:
        logger.error(f"❌ Error processing download: {e}")
        return False

# =============================================================================
# RACE WEEKEND DETECTION
# =============================================================================

def get_current_race_weekend(calendar: dict, now: Optional[datetime] = None) -> Optional[dict]:
    """Check if we're currently in a race weekend window."""
    now = now or datetime.now()
    today = now.date()
    
    for round_info in calendar.get("rounds", []):
        if not is_round_active(round_info):
            continue

        race_date_str = round_info.get("date")
        weekend_start_str = round_info.get("weekend_start")
        
        if not race_date_str:
            continue
        
        try:
            race_date = datetime.strptime(race_date_str, "%Y-%m-%d").date()
            
            if weekend_start_str:
                weekend_start = datetime.strptime(weekend_start_str, "%Y-%m-%d").date()
            else:
                weekend_start = race_date - timedelta(days=2)
            
            window_start = weekend_start - timedelta(days=1)
            window_end = race_date + timedelta(days=CONFIG["post_race_check_days"])
            
            if window_start <= today <= window_end:
                return {
                    "round": round_info.get("round"),
                    "venue": round_info.get("venue"),
                    "weekend_start": weekend_start,
                    "race_date": race_date,
                    "window_start": window_start,
                    "window_end": window_end,
                    "round_info": round_info,
                }
        
        except ValueError:
            continue
    
    return None


def get_next_race_weekend(calendar: dict, now: Optional[datetime] = None) -> Optional[dict]:
    """Get the next upcoming race weekend."""
    now = now or datetime.now()
    today = now.date()
    
    next_round = None
    next_date = None
    
    for round_info in calendar.get("rounds", []):
        if not is_round_active(round_info):
            continue

        race_date_str = round_info.get("date")
        weekend_start_str = round_info.get("weekend_start")
        
        if not race_date_str:
            continue
        
        try:
            race_date = datetime.strptime(race_date_str, "%Y-%m-%d").date()
            
            if weekend_start_str:
                weekend_start = datetime.strptime(weekend_start_str, "%Y-%m-%d").date()
            else:
                weekend_start = race_date - timedelta(days=2)
            
            window_start = weekend_start - timedelta(days=1)
            
            if window_start > today:
                if next_date is None or window_start < next_date:
                    next_date = window_start
                    next_round = {
                        "round": round_info.get("round"),
                        "venue": round_info.get("venue"),
                        "weekend_start": weekend_start,
                        "race_date": race_date,
                        "window_start": window_start,
                    }
        
        except ValueError:
            continue
    
    return next_round

def scan_available_sessions(season: int) -> dict:
    """Like the watcher, count only session folders with an actual video file."""
    from motogp_common import parse_prefix
    root = Path(get_series_library_path()) / str(season)
    if not root.exists():
        return {}
    result = {}
    for round_dir in root.iterdir():
        if not round_dir.is_dir():
            continue
        import re
        match = re.match(r'Round\s*(\d+)', round_dir.name)
        if not match:
            continue
        number = int(match.group(1))
        for folder in round_dir.iterdir():
            if not folder.is_dir():
                continue
            parsed = parse_prefix(folder.name)
            if not parsed or parsed[0] != season or parsed[1] != number:
                continue
            if any(file.is_file() and file.suffix.lower() in
                   {'.mkv', '.mp4', '.avi', '.m4v', '.ts', '.m2ts'}
                   for file in folder.iterdir()):
                result.setdefault(number, set()).add(parsed[2])
    return result


def plan_weekend(calendar: dict, season: int, now: datetime,
                 last_run: Optional[datetime], interval_seconds: int) -> dict:
    """Decide whether a grabber process is needed, without querying services.

    A completed SAB job is not completion: only a library folder counts. Jobs
    still downloading need periodic reconciliation; missing releases need
    retries; future sessions wake the loop at their search-after time. An
    incomplete timetable fails open to periodic checking, never completion.
    """
    from motogp_common import get_desired_sessions, round_runs_session
    from motogp_grabber import (SCHEDULABLE_SESSIONS, get_session_end_time,
                               GrabberState)
    active = [r for r in calendar.get("rounds", [])
              if get_current_race_weekend({"rounds": [r]}, now)]
    desired = get_desired_sessions()
    library = scan_available_sessions(season)
    state = GrabberState()
    grabbed = state.state.get("grabbed", {})
    from utils.submission_guard import intent_path, load_intents
    pending = load_intents(intent_path(state.state_file.parent))
    pending_keys = {key for intent in pending.values() for key in intent['keys']}
    future = []
    due = []
    expected = 0
    unknown = False
    delay = timedelta(hours=get_search_delay_hours())
    for round_info in active:
        round_num = round_info.get("round")
        for session in SCHEDULABLE_SESSIONS:
            if not desired.get(session, False) or not round_runs_session(round_info, session, calendar):
                continue
            expected += 1
            key = GrabberState.make_session_key(season, round_num, session)
            # Even an already sorted folder cannot tell whether an uncertain
            # replacement submission was accepted. Reconcile the reservation.
            if key in pending_keys:
                due.append((datetime.min, True))
                continue
            end = get_session_end_time(round_info, session)
            if end is None:
                unknown = True
                continue
            if session in library.get(round_num, set()):
                if end > now:
                    future.append(end)
                continue
            ready_at = end + delay
            if ready_at > now and key not in grabbed:
                future.append(ready_at)
            else:
                due.append((ready_at, key in grabbed))
    # Timetable problems need human attention and must never turn into a
    # false "all complete" claim. Keep the old retry cadence in that case.
    if unknown:
        logger = get_logger("scheduler")
        logger.warning("Some wanted sessions have no calendar time; continuing periodic checks")
        due.append((datetime.min, False))
    if due:
        interval = timedelta(seconds=interval_seconds)
        run_at = now if last_run is None else last_run + interval
        # A newly ready session should not wait for the retry interval.
        if last_run is not None and any(not in_flight and last_run < ready <= now for ready, in_flight in due):
            run_at = now
        if run_at <= now:
            return {"state": "checking", "run": True, "next": now}
        if future:
            run_at = min(run_at, min(future))
        return {"state": "waiting", "run": False, "next": run_at}
    if future:
        return {"state": "waiting", "run": False, "next": min(future)}
    return {"state": "complete" if expected else "nothing_scheduled", "run": False, "next": None}


# =============================================================================
# GRABBER EXECUTION
# =============================================================================

def run_grabber_scheduled() -> dict:
    """Run the grabber in scheduled mode."""
    logger = get_logger("scheduler")
    logger.info("🚀 Running grabber in scheduled mode...")
    
    # Build command with search delay from config
    cmd = [
        CONFIG["python_path"],
        str(CONFIG["grabber_script"]),
        "scheduled"
    ]
    
    # Add search delay from config
    search_delay = get_search_delay_hours()
    if search_delay is not None:
        cmd.extend(["--search-delay", str(search_delay)])
        logger.debug(f"Using search delay: {search_delay} hours")
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800
        )
        
        grabbed_count = 0
        
        # Log the grabber output for visibility
        for line in result.stdout.split('\n'):
            line = line.strip()
            if not line:
                continue
            # Log important lines from grabber
            if any(marker in line for marker in ['✅', '❌', '📅', '📊', '🔍', '📋', '🎯', 'READY', 'Outside window', 'within active window']):
                logger.info(f"  {line}")
            elif 'Releases grabbed:' in line or 'Grabbed:' in line:
                logger.info(f"  {line}")
                try:
                    grabbed_count = int(line.split(':')[-1].strip())
                except ValueError:
                    pass
        
        if result.returncode == 0:
            logger.info(f"✅ Grabber completed: {grabbed_count} grabbed")
            return {"success": True, "grabbed": grabbed_count}
        else:
            logger.error(f"❌ Grabber failed with code {result.returncode}")
            if result.stderr:
                logger.error(f"   Error: {result.stderr[:500]}")
            return {"success": False, "grabbed": 0, "busy": result.returncode == 2}
    
    except subprocess.TimeoutExpired:
        logger.error("❌ Grabber timed out")
        return {"success": False, "grabbed": 0}
    except Exception as e:
        logger.error(f"❌ Failed to run grabber: {e}")
        return {"success": False, "grabbed": 0}

def run_pending_reconciliation() -> bool:
    """Recover uncertain submissions even between meetings, without searching."""
    logger = get_logger("scheduler")
    try:
        result = subprocess.run(
            [CONFIG["python_path"], str(CONFIG["grabber_script"]), "reconcile"],
            capture_output=True, text=True, timeout=1800,
        )
        if result.returncode:
            logger.warning("Submission reconciliation failed (exit %s): %s",
                           result.returncode, result.stderr[:500])
            return False
        return True
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Submission reconciliation could not run: %s", type(exc).__name__)
        return False


# =============================================================================
# SCHEDULER STATE
# =============================================================================

class SchedulerState:
    """Track scheduler state."""
    
    def __init__(self):
        self.running = True
        self.last_check: Optional[datetime] = None
        self.last_grabber_run: Optional[datetime] = None
        self.last_reconciliation: Optional[datetime] = None
        self.current_weekend: Optional[dict] = None
        self.consecutive_no_grabs = 0
        self.total_grabbed_this_weekend = 0
    
    def reset_for_new_weekend(self, weekend: dict):
        """Reset state for a new race weekend."""
        logger = get_logger("scheduler")
        if self.current_weekend and self.current_weekend.get("round") != weekend.get("round"):
            logger.info(f"🏁 New race weekend: Round {weekend['round']} {weekend['venue']}")
            self.consecutive_no_grabs = 0
            self.total_grabbed_this_weekend = 0
        self.current_weekend = weekend

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def format_timedelta(td: timedelta) -> str:
    """Format timedelta as human readable string."""
    total_seconds = int(td.total_seconds())
    
    if total_seconds < 0:
        return "now"
    
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    
    if days > 0:
        return f"{days}d {hours}h"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"


def is_quiet_hours(now: Optional[datetime] = None) -> bool:
    """
    Check if we're in quiet hours.

    ``now`` is injectable so the WebUI can ask the same question about the
    same instant it renders, instead of reading the wall clock twice and
    getting two answers across a boundary. There is one implementation
    because two would drift, and then the health rule would judge the
    scheduler against an interval it never sleeps for.
    """
    if not CONFIG["quiet_hours"]["enabled"]:
        return False

    hour = (now or datetime.now()).hour
    start = CONFIG["quiet_hours"]["start"]
    end = CONFIG["quiet_hours"]["end"]
    
    if start < end:
        return start <= hour < end
    else:
        return hour >= start or hour < end


def get_sleep_interval(in_race_weekend: bool) -> int:
    """Get sleep interval in seconds."""
    if is_quiet_hours():
        return CONFIG["quiet_hours"]["interval_minutes"] * 60
    
    if in_race_weekend:
        return CONFIG["race_weekend_interval_minutes"] * 60
    else:
        return CONFIG["off_weekend_interval_minutes"] * 60

# =============================================================================
# MAIN SCHEDULER LOOP
# =============================================================================

def _ensure_state_files_are_namespaced():
    """
    Migrate a pre-hybrid /config, in case the entrypoint did not.

    ``entrypoint.sh`` does this before starting either process, which is the
    only point at which it is strictly a rename rather than a race. But the
    entrypoint is not the only way this program is started: a developer or an
    operator can run the WebUI or the scheduler directly, and then nothing
    would have moved ``grabber_state.json`` - so the app would start with an
    empty history and say nothing, which is exactly the silent-loss pattern
    this codebase keeps coming back to.

    Idempotent and cheap (six ``exists()`` calls), so running it twice is
    harmless. A failure is logged and does not stop startup; see
    ``utils.state_files.migrate_default_series_state``.
    """
    try:
        from utils.state_files import migrate_default_series_state

        for path in migrate_default_series_state(CONFIG_DIR):
            print(f"  moved legacy state into {path.name}")
    except Exception as exc:                      # pragma: no cover - startup only
        print(f"  ⚠️  Could not check state file names: {exc}")


def _series_pass(bound_series: str, args, state: "SchedulerState") -> int:
    """
    Evaluate one series once: read the calendar, decide, maybe search.

    Returns the number of seconds until this series should be looked at again.
    The caller runs a pass for every active series on **every** cycle - not one
    series until shutdown and then the next, which would leave the second
    championship unserved for as long as the first one's loop kept running (a
    weekend, in practice).

    The series is handed to child processes through the environment rather than
    as an argument because the grabber and the sorter read their binding at
    import, and they are separate processes.
    """
    from series import get_series
    from series.binding import ENV_VAR

    spec = get_series(bound_series)
    logger = get_logger("scheduler")

    season = get_current_season()

    logger.info("=" * 60)
    logger.info("  %s", spec.display_name.upper())
    logger.info("=" * 60)
    logger.info(f"  Season:              {season}")
    logger.info(f"  Library path:        {get_series_library_path(bound_series)}")
    logger.info(f"  Watch directory:     {get_series_downloads_path(bound_series)}")

    # Every child process this loop starts inherits the series.
    os.environ[ENV_VAR] = bound_series

    calendar = load_calendar(season, series=bound_series)
    if not calendar:
        logger.error(f"❌ Could not load {spec.calendar_prefix}_{season}.json")
        # A missing calendar is a configuration problem. Re-check at the
        # off-weekend interval rather than spinning: the file may be about to be
        # copied in by the entrypoint or edited through the WebUI.
        return get_sleep_interval(False)

    if not has_session_times(calendar):
        logger.warning(f"⚠️ Calendar doesn't have detailed session times")

    logger.info(f"📅 Loaded calendar: {len(calendar.get('rounds', []))} rounds")

    current_weekend = get_current_race_weekend(calendar)
    if current_weekend:
        logger.info(f"🏁 Currently in race weekend: Round {current_weekend['round']} {current_weekend['venue']}")
        state.reset_for_new_weekend(current_weekend)
    else:
        next_weekend = get_next_race_weekend(calendar)
        if next_weekend:
            until = datetime.combine(next_weekend['window_start'], datetime.min.time()) - datetime.now()
            logger.info(f"⏳ Next race weekend: Round {next_weekend['round']} {next_weekend['venue']} in {format_timedelta(until)}")

    # Progress record for the WebUI (Candidate A). "started" is written once
    # and then never changed, so a container that has just booted is visibly
    # different from one that has been silently stuck for a day.
    write_heartbeat(CONFIG_DIR, "scheduler", series=bound_series,
                    started=datetime.now().isoformat())

    now = datetime.now()
    state.last_check = now

    # The loop itself is the heartbeat, not the grabber: an off-weekend
    # loop legitimately skips every search, and the point of the record
    # is to prove the loop is still turning rather than that it is busy.
    write_heartbeat(CONFIG_DIR, "scheduler", series=bound_series,
                    last_loop=now.isoformat())

    # Settings and calendar can change while this process is running.
    reload_settings()
    previous_season = season
    season = get_current_season()
    loaded_calendar = load_calendar(season, series=bound_series)
    if loaded_calendar:
        calendar = loaded_calendar
    elif season != previous_season:
        logger.error("No calendar for configured season %s; refusing to use the previous season", season)
        calendar = {}
    from utils.submission_guard import intent_path, load_intents
    pending = load_intents(intent_path(CONFIG_DIR))
    if pending and (state.last_reconciliation is None or
                    now - state.last_reconciliation >= timedelta(hours=1)):
        state.last_reconciliation = now
        if not run_pending_reconciliation():
            # A manual job may hold the run lock; retry on the next
            # pass rather than waiting six hours between meetings.
            state.last_reconciliation = now - timedelta(minutes=55)
    current_weekend = get_current_race_weekend(calendar, now)
    in_race_weekend = current_weekend is not None
    sleep_seconds = get_sleep_interval(in_race_weekend)
    if pending and state.last_reconciliation:
        retry_at = state.last_reconciliation + timedelta(hours=1)
        sleep_seconds = min(sleep_seconds, max(1, int((retry_at - now).total_seconds())))
    plan = {"state": "off_weekend" if calendar else "calendar_missing", "next": None}

    if in_race_weekend:
        state.reset_for_new_weekend(current_weekend)
        if has_session_times(calendar):
            plan = plan_weekend(calendar, season, now, state.last_grabber_run, sleep_seconds)
            if plan["run"]:
                # Readiness is evaluated at the start of the child run.
                # A session that becomes ready while it is searching
                # must trigger another pass, not wait a full interval.
                state.last_grabber_run = datetime.now()
                result = run_grabber_scheduled()
                if result["success"]:
                    write_heartbeat(CONFIG_DIR, "scheduler", series=bound_series,
                                    last_check=datetime.now().isoformat())
                    if result["grabbed"] > 0:
                        state.total_grabbed_this_weekend += result["grabbed"]
                        state.consecutive_no_grabs = 0
                    else:
                        state.consecutive_no_grabs += 1
                    plan = plan_weekend(calendar, season, datetime.now(), state.last_grabber_run, sleep_seconds)
                elif result.get("busy"):
                    # The manual run might finish in seconds. Do not
                    # defer the scheduler for the full hourly interval.
                    state.last_grabber_run = None
                    plan = {"state": "busy", "run": False,
                            "next": datetime.now() + timedelta(minutes=5)}
                else:
                    plan = {"state": "error", "run": False,
                            "next": state.last_grabber_run + timedelta(seconds=sleep_seconds)}
            if plan["state"] == "complete":
                logger.info("✅ All wanted meeting sessions are in the library; scheduled searching paused")
                upcoming = get_next_race_weekend(calendar, now)
                if upcoming:
                    plan["next"] = datetime.combine(upcoming['window_start'], datetime.min.time())
                    plan["venue"] = upcoming.get("venue")
        else:
            plan = {"state": "calendar_missing_times", "next": None}
    else:
        next_weekend = get_next_race_weekend(calendar, now)
        if next_weekend:
            next_start = datetime.combine(next_weekend['window_start'], datetime.min.time())
            plan["next"] = next_start
            plan["venue"] = next_weekend.get("venue")
            logger.info("💤 Next: Round %s %s in %s", next_weekend['round'],
                        next_weekend['venue'], format_timedelta(next_start - now))

    # Publish the scheduler's decision separately from its heartbeat:
    # a deliberately old last-search timestamp is not a dead loop.
    from utils.json_store import write_json
    write_json(CONFIG_DIR / f"scheduler_plan.{bound_series}.json", {
        "state": plan["state"], "at": datetime.now().isoformat(),
        "next": plan.get("next").isoformat() if plan.get("next") else None,
        "venue": plan.get("venue"),
    })
    if plan.get("next") and not plan.get("run"):
        until = (plan["next"] - datetime.now()).total_seconds()
        sleep_seconds = min(sleep_seconds, max(1, int(until)))

    # Sleep in chunks, so a shutdown signal is acted on within 30 seconds.
    # Returning the wait lets the caller pace the cycle by the *shortest* one:
    # a series in a race weekend must not be delayed by another that is three
    # days from its next round.
    for _ in range(sleep_seconds // 30):
        if not state.running:
            return 0
        time.sleep(30)

    if state.running:
        time.sleep(sleep_seconds % 30)
    return max(30, sleep_seconds)

def main():
    """
    Main scheduler: one process, one loop per active series.

    "Active" is ``config_loader.active_series()`` - a series is active when it
    is configured or its library already exists, and the default series always
    is. An install that only follows MotoGP therefore runs exactly the loop it
    always did, with no F1 banner and no F1 searches.

    The loops run **in sequence**, each until shutdown, rather than as threads:
    a search is I/O-bound on Prowlarr, so two concurrent loops add contention
    without finishing anything sooner, and the per-series state (last run, the
    quiet-hours counter, the weekend tracker) was always modelled as one series'
    progress. The cost is that a sleeping MotoGP loop delays the start of the F1
    pass by up to one interval; the first loop sleeps in 30-second chunks and
    this is the same trade the app made when a single loop could only be in one
    place at a time.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Motorsportarr Scheduler")
    parser.add_argument("--no-watcher", action="store_true", help="Disable download watcher")
    parser.add_argument("--watcher-only", action="store_true", help="Only run watcher")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    # Setup logging
    setup_logging(debug_mode=args.debug)
    logger = get_logger("scheduler")

    _ensure_state_files_are_namespaced()

    state = SchedulerState()
    watcher = None

    # Handle graceful shutdown
    def signal_handler(signum, frame):
        logger.info("🛑 Shutdown signal received")
        state.running = False

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    from utils.config_loader import active_series

    active = [spec.key for spec in active_series()]
    logger.info("=" * 60)
    logger.info("  MOTORSPORTARR SCHEDULER")
    logger.info("=" * 60)
    logger.info(f"  Series:              {', '.join(active)}")

    # One watcher per active series. Each watches its own completed-downloads
    # folder and sorts into its own library; a single watcher on the default
    # series' folder would never see the other series' downloads, and pointing
    # it at both would sort them all with one series' paths.
    watchers = []
    if not args.no_watcher:
        logger.info("📂 Initializing download watchers...")
        for key in active:
            downloads_path = get_series_downloads_path(key)
            series_watcher = create_watcher(
                watch_dir=downloads_path,
                processor=functools.partial(process_download, series=key),
                config_dir=str(CONFIG_DIR),
                series=key,
            )
            series_watcher.start()
            watchers.append(series_watcher)
            logger.info(f"👁️ Watching ({key}): {downloads_path}")
    else:
        logger.info("👁️ Download watcher: Disabled")
    watcher = watchers[0] if watchers else None
    
    # Watcher-only mode
    if args.watcher_only:
        logger.info("Running in watcher-only mode")
        try:
            while state.running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            for series_watcher in watchers:
                series_watcher.stop()
        return 0

    # One *pass* per active series per cycle. Not one series until shutdown and
    # then the next: the first series' loop stays running for a whole weekend,
    # so the second championship would go unserved for exactly as long. The
    # passes are sequential because they are I/O-bound on the same Prowlarr and
    # share the same run lock; concurrency would add contention and racing
    # without finishing anything sooner.
    per_series_state = {key: SchedulerState() for key in active}
    per_series_state[active[0]] = state       # one object, so signals reach it

    while state.running:
        waits = []
        for key in active:
            if not state.running:
                break
            series_state = per_series_state[key]
            series_state.running = state.running
            try:
                waits.append(_series_pass(key, args, series_state))
            except KeyboardInterrupt:
                state.running = False
            except Exception:
                # One series' problem must not stop another's. A broken F1
                # calendar or an unreachable library for one championship is
                # not a reason to stop searching for the other.
                logger.exception("❌ Scheduler pass for %s failed", key)
                waits.append(60)
        if state.running:
            logger.debug("Next cycle in %ss", min(waits) if waits else 60)

    # Cleanup
    for series_watcher in watchers:
        series_watcher.stop()
    
    logger.info("🛑 Scheduler stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())