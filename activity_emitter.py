#!/usr/bin/env python3
"""
Activity Emitter Module for Motorsportarr
=========================================

Provides a simple API for all scripts to emit activity events.
Events are stored in a shared JSON file and read by the WebUI.

Usage:
    from activity_emitter import emit_activity, ActivityStage
    
    emit_activity(
        episode_key="2025_R01_E05",
        episode_name="Moto3 Free Practice 1",
        stage=ActivityStage.SEARCHING,
        details="Round 1 Thailand",
        round_info={"round": 1, "venue": "Thailand"}
    )
"""

import logging
import os
from pathlib import Path
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List, Any

from utils.json_store import read_json, update_json, write_json
from utils.state_files import state_path

logger = logging.getLogger("msportarr.activity")

# Configuration
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
ACTIVITY_FILE = state_path(CONFIG_DIR, "activity_events")
MAX_COMPLETED_HISTORY = 10


def activity_file(series: Optional[str] = None) -> Path:
    """
    The activity document for one series.

    ``None`` means this process' binding, and returns ``ACTIVITY_FILE`` itself
    rather than recomputing the same path: that attribute is what the writers
    use, and a second computation would let a caller (or a test, or a future
    refactor that redirects the store) end up reading a different file from the
    one being written.

    A named series resolves through the shared naming rule, because the WebUI
    serves both championships in one process. Formula 1's activities in
    MotoGP's feed would be presented with MotoGP's stage wording and, worse,
    its own feed would look permanently empty.
    """
    if series is None:
        return ACTIVITY_FILE
    return state_path(CONFIG_DIR, "activity_events", series=series)


class ActivityStage(Enum):
    """Stages in the download lifecycle."""
    SEARCHING = "searching"
    CANDIDATES_FOUND = "candidates_found"
    DOWNLOADING = "downloading"
    DOWNLOAD_COMPLETE = "download_complete"
    PROCESSING = "processing"
    METADATA = "metadata"
    COMPLETE = "complete"
    ERROR = "error"


# Stage display configuration
STAGE_CONFIG = {
    ActivityStage.SEARCHING: {"icon": "🔍", "color": "#52aad8", "label": "Searching"},
    ActivityStage.CANDIDATES_FOUND: {"icon": "📋", "color": "#52aad8", "label": "Found"},
    ActivityStage.DOWNLOADING: {"icon": "⬇️", "color": "#f39c12", "label": "Downloading"},
    ActivityStage.DOWNLOAD_COMPLETE: {"icon": "📦", "color": "#f1c40f", "label": "Downloaded"},
    ActivityStage.PROCESSING: {"icon": "⚙️", "color": "#f1c40f", "label": "Processing"},
    ActivityStage.METADATA: {"icon": "🎬", "color": "#f1c40f", "label": "Metadata"},
    ActivityStage.COMPLETE: {"icon": "✅", "color": "#27ae60", "label": "Complete"},
    ActivityStage.ERROR: {"icon": "❌", "color": "#e74c3c", "label": "Error"},
}


def _empty_data() -> Dict[str, Any]:
    return {"events": {}, "completed_history": []}


def _normalise(data: Any) -> Dict[str, Any]:
    """Ensure the required keys exist, whatever shape was loaded."""
    if not isinstance(data, dict):
        return _empty_data()
    data.setdefault("events", {})
    data.setdefault("completed_history", [])
    return data


def _load_activity_data(series: Optional[str] = None) -> Dict[str, Any]:
    """Load activity data from JSON file (read-only; writers are atomic)."""
    return _normalise(read_json(activity_file(series), _empty_data(), log=logger))


def _save_activity_data(data: Dict[str, Any]) -> bool:
    """
    Save the whole activity document under the sidecar lock.

    Prefer :func:`_update_activity_data` for changes: replacing a snapshot
    loaded earlier can discard another process's event. This whole-document
    write remains for ``clear_all_activity`` and compatibility, and is still
    locked and atomic so a reader never sees a partial file. Failures are
    logged at ERROR rather than printed or swallowed - an event that could
    not be written is an event the Activity tab will never show.
    """
    try:
        write_json(ACTIVITY_FILE, _normalise(data))
        return True
    except OSError as exc:
        logger.error("Could not save activity data to %s: %s", ACTIVITY_FILE, exc)
        return False


def _update_activity_data(mutator) -> Any:
    """
    Apply ``mutator`` to the activity document as one locked transaction.

    The document is reloaded after the sidecar lock is taken, so concurrent
    emitters in the grabber, watcher and tagger each build on the previous
    writer's committed state. Previously the file was read first and the lock
    held only around serialising to a shared temp file, so two processes
    could read the same snapshot and overwrite each other's events. A failed
    write is logged at ERROR (the event is lost, but never silently).
    """
    def _apply(data):
        return mutator(_normalise(data))

    try:
        return update_json(ACTIVITY_FILE, _empty_data, _apply, log=logger)
    except OSError as exc:
        logger.error(
            "Could not update activity events in %s: %s; the event was not recorded",
            ACTIVITY_FILE, exc,
        )
        return None


def emit_activity(
    episode_key: str,
    episode_name: str,
    stage: ActivityStage,
    details: Optional[str] = None,
    round_info: Optional[Dict] = None,
    progress: Optional[int] = None,
    error_message: Optional[str] = None,
    release_name: Optional[str] = None,
    nzo_id: Optional[str] = None
) -> None:
    """
    Emit an activity event for the feed.
    
    Args:
        episode_key: Unique identifier (e.g., "2025_R01_E05")
        episode_name: Human-readable name (e.g., "Moto3 Free Practice 1")
        stage: Current stage in the lifecycle
        details: Optional status details
        round_info: Optional dict with round/venue info
        progress: Optional download progress (0-100)
        error_message: Optional error message if stage is ERROR
        release_name: Optional release/filename being processed
        nzo_id: Optional SABnzbd job ID for tracking downloads
    """
    stage_config = STAGE_CONFIG.get(stage, {})

    def _mutate(data):
        # The watcher can reconstruct the round from a folder prefix, but not the
        # venue. Retain identity supplied by an earlier stage rather than making an
        # active download lose its venue as it moves through the pipeline. The
        # previous event is read inside the locked transaction, so it is the
        # latest committed one, not a snapshot from before another process wrote.
        previous_event = data["events"].get(episode_key, {})
        round_num = round_info.get("round") if round_info else None
        venue = round_info.get("venue") if round_info else None
        if round_num is None:
            round_num = previous_event.get("round")
        if venue is None:
            venue = previous_event.get("venue")

        event = {
            "id": episode_key,
            "name": episode_name,
            "stage": stage.value,
            "stage_icon": stage_config.get("icon", "❓"),
            "stage_color": stage_config.get("color", "#666"),
            "stage_label": stage_config.get("label", stage.value),
            "details": details,
            "timestamp": datetime.now().isoformat(),
            "round": round_num,
            "venue": venue,
            "progress": progress,
            "error_message": error_message,
            "release_name": release_name,
            "nzo_id": nzo_id,
        }

        # Handle completion - move to history
        if stage in (ActivityStage.COMPLETE, ActivityStage.ERROR):
            data["events"].pop(episode_key, None)
            data["completed_history"].insert(0, event)
            # Trim history to max size
            data["completed_history"] = data["completed_history"][:MAX_COMPLETED_HISTORY]
        else:
            # Update or create in active events
            data["events"][episode_key] = event

    _update_activity_data(_mutate)


def update_download_progress(
    episode_key: str,
    progress: int,
    details: Optional[str] = None
) -> None:
    """
    Update download progress for an existing event.
    
    Args:
        episode_key: The episode to update
        progress: Download progress (0-100)
        details: Optional updated details string
    """
    def _mutate(data):
        if episode_key in data["events"]:
            data["events"][episode_key]["progress"] = progress
            data["events"][episode_key]["timestamp"] = datetime.now().isoformat()
            if details:
                data["events"][episode_key]["details"] = details

    _update_activity_data(_mutate)


def mark_complete(
    episode_key: str,
    details: Optional[str] = "Ready in Plex"
) -> None:
    """
    Mark an episode as complete and move to history.
    
    Args:
        episode_key: The episode to mark complete
        details: Completion message
    """
    data = _load_activity_data()
    
    if episode_key in data["events"]:
        event = data["events"][episode_key]
        # Update to complete stage
        emit_activity(
            episode_key=episode_key,
            episode_name=event.get("name", "Unknown"),
            stage=ActivityStage.COMPLETE,
            details=details,
            round_info={"round": event.get("round"), "venue": event.get("venue")}
        )


def mark_error(
    episode_key: str,
    error_message: str,
    episode_name: Optional[str] = None
) -> None:
    """
    Mark an episode as failed and move to history.
    
    Args:
        episode_key: The episode that failed
        error_message: Description of the error
        episode_name: Episode name (optional, will try to get from existing event)
    """
    data = _load_activity_data()
    
    # Try to get existing event info
    existing = data["events"].get(episode_key, {})
    name = episode_name or existing.get("name", "Unknown Episode")
    round_num = existing.get("round")
    venue = existing.get("venue")
    
    emit_activity(
        episode_key=episode_key,
        episode_name=name,
        stage=ActivityStage.ERROR,
        details="Failed",
        error_message=error_message,
        round_info={"round": round_num, "venue": venue} if round_num else None
    )


def clear_episode(episode_key: str) -> None:
    """
    Remove an episode from active events without adding to history.
    
    Args:
        episode_key: The episode to remove
    """
    def _mutate(data):
        data["events"].pop(episode_key, None)

    _update_activity_data(_mutate)


def get_active_events() -> Dict[str, Any]:
    """
    Get all currently active events.
    
    Returns:
        Dict of active events keyed by episode_key
    """
    data = _load_activity_data()
    return data.get("events", {})


def get_completed_history(count: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Get recent completed/error events.
    
    Args:
        count: Max number of events to return (default: all up to MAX_COMPLETED_HISTORY)
    
    Returns:
        List of completed events, most recent first
    """
    data = _load_activity_data()
    history = data.get("completed_history", [])
    
    if count:
        return history[:count]
    return history


def get_all_activity(series: Optional[str] = None) -> Dict[str, Any]:
    """
    Get all activity data for WebUI display.
    
    Returns:
        Dict with 'active' (list) and 'history' (list) keys
    """
    data = _load_activity_data(series)
    
    # Convert events dict to sorted list (most recent first)
    active_list = sorted(
        data.get("events", {}).values(),
        key=lambda x: x.get("timestamp", ""),
        reverse=True
    )
    
    return {
        "active": active_list,
        "history": data.get("completed_history", [])
    }


def clear_all_activity(series: Optional[str] = None) -> bool:
    """
    Clear all activity data. Use with caution.

    ``series`` scopes the clear to one championship's feed: the WebUI serves
    both, and a "clear" pressed on the F1 tab must not erase MotoGP's history.
    ``_save_activity_data`` still writes another module's global, so a series
    other than the binding writes its own store directly.
    """
    empty = {"events": {}, "completed_history": []}
    if series is None:
        return _save_activity_data(empty)

    try:
        write_json(activity_file(series), _normalise(empty))
        return True
    except OSError as exc:
        logger.error("Could not clear %s: %s", activity_file(series), exc)
        return False


# For testing
if __name__ == "__main__":
    print("Testing activity_emitter...")
    
    # Test emit
    emit_activity(
        episode_key="2025_R01_E05",
        episode_name="Moto3 Free Practice 1",
        stage=ActivityStage.SEARCHING,
        details="Round 1 Thailand",
        round_info={"round": 1, "venue": "Thailand"}
    )
    print("✅ Emitted SEARCHING event")
    
    # Test get
    activity = get_all_activity()
    print(f"✅ Active events: {len(activity['active'])}")
    print(f"✅ History events: {len(activity['history'])}")
    
    # Show event
    if activity['active']:
        print(f"   Latest: {activity['active'][0]}")
    
    print("\n✅ activity_emitter module is working!")
