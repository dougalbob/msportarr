"""
Grab statistics from existing grabber_state.json
"""

import os
import json
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.resolve()))

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


def load_grabber_state():
    """Load grabber state file."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {"searched": {}, "grabbed": {}, "failed": {}}


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


def get_class_from_release(release: str):
    """Determine class from release name."""
    release_upper = release.upper()
    if 'MOTO3' in release_upper:
        return 'Moto3'
    elif 'MOTO2' in release_upper:
        return 'Moto2'
    else:
        return 'MotoGP'


def get_session_name_from_release(release: str):
    """Extract session name from release."""
    release_lower = release.lower()
    
    if 'race' in release_lower and 'practice' not in release_lower:
        return 'Race'
    elif 'sprint' in release_lower:
        return 'Sprint'
    elif 'qualifying' in release_lower or '.q1.' in release_lower or '.q2.' in release_lower:
        return 'Qualifying'
    elif 'practice' in release_lower or '.fp1.' in release_lower or '.fp2.' in release_lower:
        return 'Practice'
    elif 'warm' in release_lower:
        return 'Warm Up'
    elif 'press' in release_lower or 'conference' in release_lower:
        return 'Press Conference'
    elif 'gear.up' in release_lower:
        return 'Gear Up'
    elif 'flag' in release_lower or 'chequered' in release_lower:
        return 'Chequered Flag'
    else:
        return 'Session'


def get_stats_summary():
    """Get summary statistics from grabber state."""
    state = load_grabber_state()
    grabbed = state.get("grabbed", {})
    failed = state.get("failed", {})
    
    if not grabbed:
        return {
            "total_grabbed": 0,
            "total_failed": len(failed),
            "success_rate": 100,
            "grabs_today": 0,
            "grabs_this_week": 0,
            "grabs_this_month": 0,
            "by_class": {"MotoGP": 0, "Moto2": 0, "Moto3": 0},
            "by_day": [],
            "by_month": [],
            "by_year": {},
            "recent_grabs": [],
            "last_run": state.get("last_run"),
            "last_scheduled": state.get("last_scheduled_check")
        }
    
    now = datetime.now()
    today = now.date()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    
    grabs_today = 0
    grabs_this_week = 0
    grabs_this_month = 0
    
    by_class = defaultdict(int)
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
            race_class = get_class_from_release(release)
            session_name = get_session_name_from_release(release)
            
            # By class
            by_class[race_class] += 1
            
            # By year
            if parsed:
                by_year[parsed["year"]] += 1
            
            # By day (last 14 days)
            if grab_date >= today - timedelta(days=14):
                day_key = grab_date.strftime("%Y-%m-%d")
                by_day[day_key] += 1
            
            # By month (last 6 months)
            if grab_date >= today - timedelta(days=180):
                month_key = grab_time.strftime("%Y-%m")
                by_month[month_key] += 1
            
            # Time period counts
            if grab_date == today:
                grabs_today += 1
            if grab_date >= week_ago:
                grabs_this_week += 1
            if grab_date >= month_ago:
                grabs_this_month += 1
            
            # For recent list
            grabs_list.append({
                "key": key,
                "timestamp": timestamp,
                "grab_time": grab_time,
                "release": release,
                "race_class": race_class,
                "session_name": session_name,
                "round": parsed["round"] if parsed else 0,
                "year": parsed["year"] if parsed else 0
            })
            
        except (ValueError, KeyError, TypeError):
            continue
    
    # Sort and get recent
    grabs_list.sort(key=lambda x: x["grab_time"], reverse=True)
    recent_grabs = []
    for grab in grabs_list[:15]:
        recent_grabs.append({
            "time": grab["grab_time"].strftime("%d %b %H:%M"),
            "session": f"{grab['race_class']} {grab['session_name']}",
            "round": grab["round"],
            "year": grab["year"],
            "release": grab["release"][:50] + "..." if len(grab["release"]) > 50 else grab["release"],
            "success": True
        })
    
    # Format by_day for chart (last 14 days)
    day_data = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        day_key = d.strftime("%Y-%m-%d")
        day_data.append({
            "date": day_key,
            "label": d.strftime("%d %b"),
            "count": by_day.get(day_key, 0)
        })
    
    # Format by_month for chart (last 6 months)
    month_data = []
    for i in range(5, -1, -1):
        d = today.replace(day=1) - timedelta(days=i*30)
        month_key = d.strftime("%Y-%m")
        month_data.append({
            "month": month_key,
            "label": d.strftime("%b %Y"),
            "count": by_month.get(month_key, 0)
        })
    
    total = len(grabbed)
    total_failed = len(failed)
    
    return {
        "total_grabbed": total,
        "total_failed": total_failed,
        "success_rate": round(total / (total + total_failed) * 100) if (total + total_failed) > 0 else 100,
        "grabs_today": grabs_today,
        "grabs_this_week": grabs_this_week,
        "grabs_this_month": grabs_this_month,
        "by_class": dict(by_class),
        "by_day": day_data,
        "by_month": month_data,
        "by_year": dict(by_year),
        "recent_grabs": recent_grabs,
        "last_run": state.get("last_run"),
        "last_scheduled": state.get("last_scheduled_check")
    }
