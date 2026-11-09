#!/usr/bin/env python3
"""
MotoGP Watcher - Monitor download folder for completed MotoGP downloads
=========================================================================

Watches /downloads/complete/motogp/ for new folders and triggers sorting.
Runs as part of the scheduler, eliminating SABnzbd post-processing dependency.

Features:
- Detects new folders appearing in watch directory
- Waits for folder to be "stable" (no changes for X seconds)
- Verifies items exist in library before skipping (smart reprocessing)
- Tracks processed, failed, and pending states separately
- Retries failed items with exponential backoff
- Comprehensive logging for debugging

This replaces the SABnzbd post-script approach with a clean folder watcher.
"""

import os
import sys
import time
import json
import logging
import threading
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Set, Dict, Optional, Callable, List, Tuple
from dataclasses import dataclass, field, asdict

# Add script directory to path
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

# Import centralized logging and common functions
from motogp_common import (
    setup_logging, get_logger,
    parse_prefix, load_calendar, get_round_by_number,
    get_config_dir, SessionType, element_number,
    # Namespaced /config filenames: every shared state file carries its
    # series, because two series writing grabber_state.json into one volume
    # overwrite each other within a day.
    state_path,
)

from activity_emitter import emit_activity, ActivityStage, mark_complete, mark_error
from utils import failure_reasons, scan_metrics
from utils.background_health import write_heartbeat

def _folder_to_episode_key(folder_name: str) -> tuple:
    """
    Convert folder name to episode key and metadata.
    Returns (episode_key, episode_name, round_num, session_type) or (None, None, None, None) if parsing fails.
    """
    parsed = parse_prefix(folder_name)
    if not parsed:
        return (None, None, None, None)
    
    year, round_num, session_type = parsed
    episode_key = f"{year}_R{round_num:02d}_E{element_number(session_type, year):02d}"
    episode_name = session_type.full_display_name
    return (episode_key, episode_name, round_num, session_type)




from utils.config_loader import (
    get_motogp_library_path,
    get_plex_token,
    get_sabnzbd_api_key,
)
from utils.json_store import atomic_write_json, file_lock, read_json, three_way_merge


def _configured_secrets() -> List[str]:
    """
    Credentials that must never appear in a stored processing error.

    A sorter/tagger failure message can carry a Plex URL, and the Plex token
    travels in its query string. The reason is persisted in
    ``watcher_state.json`` and rendered in the UI, so it has to be clean before
    it is written, not at display time.
    """
    return [
        value
        for value in (get_plex_token(), get_sabnzbd_api_key())
        if value
    ]

# Get module logger
logger = get_logger("watcher")


# =============================================================================
# LIBRARY VERIFICATION
# =============================================================================

class LibraryChecker:
    """
    Verifies whether a session exists in the Plex library.
    
    This allows the watcher to reprocess items that were previously
    processed but later deleted from the library.
    """
    
    def __init__(self, library_path: str):
        self.library_path = Path(library_path)
        self._cache: Dict[str, bool] = {}
        self._cache_time: Optional[datetime] = None
        self._cache_ttl = timedelta(minutes=5)
    
    def _refresh_cache_if_needed(self):
        """
        Refresh the library cache if expired.

        Instrumented (Candidate D) because this walk covers every season the
        library holds, and it is the most plausible source of "the watcher
        feels slow" - but the suspicion is not a measurement, so the duration
        and scope are recorded for /diagnostics rather than acted on.
        """
        now = datetime.now()
        if self._cache_time and (now - self._cache_time) < self._cache_ttl:
            return

        with scan_metrics.timed_scan("watcher-library-cache") as scope:
            self._refresh_cache(scope)

    def _refresh_cache(self, scope: dict):
        """The actual walk. ``scope`` records how much of the tree was visited."""
        now = datetime.now()
        self._cache.clear()

        if not self.library_path.exists():
            logger.warning(f"Library path does not exist: {self.library_path}")
            self._cache_time = now
            return
        
        # Scan library for existing session folders
        try:
            for year_dir in self.library_path.iterdir():
                if not year_dir.is_dir() or not year_dir.name.isdigit():
                    continue
                
                for round_dir in year_dir.iterdir():
                    if not round_dir.is_dir():
                        continue
                    scope["rounds"] = scope.get("rounds", 0) + 1
                    
                    for session_dir in round_dir.iterdir():
                        if not session_dir.is_dir():
                            continue
                        
                        # Check if folder has video files
                        has_video = any(
                            f.suffix.lower() in {'.mkv', '.mp4', '.avi', '.m4v', '.ts'}
                            for f in session_dir.iterdir()
                            if f.is_file()
                        )
                        
                        if has_video:
                            scope["sessions"] = scope.get("sessions", 0) + 1
                            # Extract prefix from folder name
                            folder_name = session_dir.name
                            match = re.match(r'^(\d{2}R\d{2}E\d{2})', folder_name)
                            if match:
                                prefix = match.group(1)
                                self._cache[prefix] = True
            
            logger.debug(f"Library cache refreshed: {len(self._cache)} sessions found")
            self._cache_time = now
            
        except Exception as e:
            logger.error(f"Error scanning library: {e}")
            self._cache_time = now
    
    def exists_in_library(self, folder_name: str) -> Tuple[bool, Optional[str]]:
        """
        Check if a session exists in the library.
        
        Args:
            folder_name: Download folder name (e.g., "25R17E11 Japan Moto3 Qualifying 1")
        
        Returns:
            Tuple of (exists: bool, library_path: Optional[str])
        """
        self._refresh_cache_if_needed()
        
        # Extract prefix from folder name
        match = re.match(r'^(\d{2}R\d{2}E\d{2})', folder_name)
        if not match:
            logger.debug(f"No prefix found in folder name: {folder_name}")
            return (False, None)
        
        prefix = match.group(1)
        exists = prefix in self._cache
        
        if exists:
            # Find the actual path
            parsed = parse_prefix(folder_name)
            if parsed:
                year, round_num, session_type = parsed
                calendar = load_calendar(year)
                if calendar:
                    round_info = get_round_by_number(calendar, round_num)
                    if round_info:
                        expected_path = (
                            self.library_path / 
                            str(year) / 
                            round_info.get("folder", f"Round {round_num}")
                        )
                        # Find matching session folder
                        if expected_path.exists():
                            for session_dir in expected_path.iterdir():
                                if session_dir.name.startswith(prefix):
                                    return (True, str(session_dir))
        
        return (exists, None)
    
    def invalidate_cache(self):
        """Force cache refresh on next check."""
        self._cache_time = None


# =============================================================================
# STATE MANAGEMENT
# =============================================================================

@dataclass
class FailedItem:
    """Track a failed processing attempt."""
    folder_name: str
    first_failed: str  # ISO format datetime
    last_attempt: str  # ISO format datetime
    attempt_count: int = 1
    last_error: str = ""
    #: Which actor failed and what to do about it, from
    #: ``utils.failure_reasons``. A sorter returning False and an exception in
    #: the processor are different faults; both used to be one string.
    stage: str = ""
    hint: str = ""
    actionable: bool = False

    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'FailedItem':
        # Tolerate entries written before the structured fields existed; a
        # missing key must not make the whole retry state unreadable.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass 
class WatcherState:
    """Persistent state for the watcher."""
    processed: Set[str] = field(default_factory=set)
    failed: Dict[str, FailedItem] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "processed": list(self.processed),
            "failed": {k: v.to_dict() for k, v in self.failed.items()}
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'WatcherState':
        state = cls()
        state.processed = set(data.get("processed", []))
        state.failed = {
            k: FailedItem.from_dict(v) 
            for k, v in data.get("failed", {}).items()
        }
        return state


class StateManager:
    """Manages persistent watcher state."""
    
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_file = state_path(self.state_dir, "watcher_state")
        self.legacy_file = self.state_dir / "watcher_processed.txt"
        self.state = WatcherState()
        # Snapshot of what was loaded, so _save() can merge this process's
        # changes into whatever is on disk then (see utils.json_store).
        self._snapshot = self.state.to_dict()
        self._load()

    def _load(self):
        """Load state from disk."""
        # Try new JSON format first
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                self.state = WatcherState.from_dict(data)
                self._snapshot = self.state.to_dict()
                logger.debug(f"Loaded state: {len(self.state.processed)} processed, {len(self.state.failed)} failed")
                return
            except Exception as e:
                logger.warning(f"Could not load state file: {e}")
        
        # Fall back to legacy format
        if self.legacy_file.exists():
            try:
                with open(self.legacy_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self.state.processed.add(line)
                logger.info(f"Migrated {len(self.state.processed)} entries from legacy state file")
                self._save()  # Save in new format
                # Rename legacy file
                try:
                    self.legacy_file.rename(self.legacy_file.with_suffix('.txt.migrated'))
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"Could not load legacy state: {e}")
    
    def _save(self):
        """
        Save state to disk as a locked merge, not an overwrite.

        The watcher is long-lived, so its in-memory state can be hours older
        than the file (the WebUI reads this file, and a second watcher or a
        CLI clear can write it). Reloading under the sidecar lock and
        three-way merging against the load-time snapshot keeps entries
        committed since then, while this process's own additions, retries
        and clears are applied. The write is atomic, so the WebUI never
        reads a half-written file. ``processed`` is a set, so it is merged
        as a list in its JSON form and converted back afterwards.
        """
        try:
            with file_lock(self.state_file):
                disk = read_json(self.state_file, {}, log=logger)
                if not isinstance(disk, dict):
                    disk = {}
                disk.setdefault("processed", [])
                disk.setdefault("failed", {})
                ours = self.state.to_dict()
                # three_way_merge works on dicts; merge the processed list
                # as a set union/difference against the snapshot instead.
                merged_failed = three_way_merge(
                    self._snapshot.get("failed", {}),
                    ours.get("failed", {}),
                    disk.get("failed", {}),
                )
                base_processed = set(self._snapshot.get("processed", []))
                our_processed = set(ours.get("processed", []))
                disk_processed = set(disk.get("processed", []))
                merged_processed = (
                    (disk_processed | (our_processed - base_processed))
                    - (base_processed - our_processed)
                )
                merged = {
                    "processed": sorted(merged_processed),
                    "failed": merged_failed,
                }
                atomic_write_json(self.state_file, merged)
            self.state = WatcherState.from_dict(merged)
            self._snapshot = merged
        except Exception as e:
            logger.error(f"Could not save state: {e}")
    
    def is_processed(self, folder_name: str) -> bool:
        """Check if folder has been successfully processed."""
        return folder_name in self.state.processed
    
    def is_failed(self, folder_name: str) -> bool:
        """Check if folder has failed processing."""
        return folder_name in self.state.failed
    
    def get_failed_item(self, folder_name: str) -> Optional[FailedItem]:
        """Get failed item details."""
        return self.state.failed.get(folder_name)
    
    def mark_processed(self, folder_name: str):
        """Mark folder as successfully processed."""
        self.state.processed.add(folder_name)
        # Remove from failed if it was there
        self.state.failed.pop(folder_name, None)
        self._save()
        logger.debug(f"Marked as processed: {folder_name}")
    
    def mark_failed(self, folder_name: str, error: str = "",
                    stage: str = failure_reasons.STAGE_PROCESS,
                    hint: str = ""):
        """
        Mark folder as failed, with the stage that failed.

        ``error`` is sanitised here rather than by the caller: it is a sorter or
        tagger message that can contain a library path and, historically, a
        Plex URL with its token in the query string. ``watcher_state.json`` is
        read by the WebUI and survives restarts, so the stored text has to be
        safe on its own.
        """
        now = datetime.now().isoformat()
        error = failure_reasons.sanitise(error, _configured_secrets())
        hint = hint or failure_reasons.HINTS.get(stage, "")

        if folder_name in self.state.failed:
            item = self.state.failed[folder_name]
            item.last_attempt = now
            item.attempt_count += 1
            item.last_error = error
            item.stage = stage
            item.hint = hint
            item.actionable = stage in failure_reasons.ACTIONABLE
        else:
            self.state.failed[folder_name] = FailedItem(
                folder_name=folder_name,
                first_failed=now,
                last_attempt=now,
                attempt_count=1,
                last_error=error,
                stage=stage,
                hint=hint,
                actionable=stage in failure_reasons.ACTIONABLE,
            )
        
        self._save()
        logger.debug(f"Marked as failed: {folder_name} (attempt {self.state.failed[folder_name].attempt_count})")
    
    def should_retry(self, folder_name: str, max_attempts: int = 5, backoff_minutes: int = 30) -> bool:
        """Check if a failed item should be retried."""
        if folder_name not in self.state.failed:
            return True
        
        item = self.state.failed[folder_name]
        
        # Max attempts exceeded
        if item.attempt_count >= max_attempts:
            return False
        
        # Exponential backoff: 30min, 1h, 2h, 4h, 8h
        backoff = timedelta(minutes=backoff_minutes * (2 ** (item.attempt_count - 1)))
        last_attempt = datetime.fromisoformat(item.last_attempt)
        next_retry = last_attempt + backoff
        
        return datetime.now() >= next_retry
    
    def remove_from_processed(self, folder_name: str):
        """Remove a specific item from processed list."""
        if folder_name in self.state.processed:
            self.state.processed.discard(folder_name)
            self._save()
            logger.info(f"Removed from processed: {folder_name}")
    
    def clear_failed(self, folder_name: str = None):
        """Clear failed state for retry."""
        if folder_name:
            self.state.failed.pop(folder_name, None)
        else:
            self.state.failed.clear()
        self._save()
    
    def clear_processed(self, folder_name: str = None):
        """Clear processed state for reprocessing."""
        if folder_name:
            self.state.processed.discard(folder_name)
        else:
            self.state.processed.clear()
        self._save()
    
    def get_stats(self) -> dict:
        """Get state statistics."""
        return {
            "processed_count": len(self.state.processed),
            "failed_count": len(self.state.failed),
            "failed_items": [
                {
                    "name": k,
                    "attempts": v.attempt_count,
                    "last_error": v.last_error[:100] if v.last_error else ""
                }
                for k, v in self.state.failed.items()
            ]
        }


# =============================================================================
# DOWNLOAD WATCHER
# =============================================================================

class DownloadWatcher:
    """
    Watches a directory for new completed downloads.
    
    Features:
    - Detects new folders appearing in watch directory
    - Waits for folder to be "stable" (no changes for X seconds)
    - Verifies items exist in library before skipping
    - Calls processor function when ready
    - Tracks processed/failed folders separately
    - Retries failed items with exponential backoff
    - Thread-safe for running alongside scheduler
    """
    
    def __init__(
        self,
        watch_dir: str,
        processor: Callable[[str], bool],
        stable_seconds: int = 15,
        check_interval: int = 10,
        config_dir: Optional[str] = None,
        library_path: Optional[str] = None,
        max_retry_attempts: int = 5,
        retry_backoff_minutes: int = 30,
        verify_library: bool = True
    ):
        """
        Initialize the watcher.
        
        Args:
            watch_dir: Directory to monitor for new folders
            processor: Function to call with folder path when ready
            stable_seconds: Seconds folder must be unchanged before processing
            check_interval: Seconds between directory scans
            config_dir: Directory for state files
            library_path: Path to Plex library for verification
            max_retry_attempts: Maximum retry attempts for failed items
            retry_backoff_minutes: Base backoff time for retries
            verify_library: Whether to verify items exist in library before skipping
        """
        self.watch_dir = Path(watch_dir)
        self.processor = processor
        self.stable_seconds = stable_seconds
        self.check_interval = check_interval
        self.max_retry_attempts = max_retry_attempts
        self.retry_backoff_minutes = retry_backoff_minutes
        self.verify_library = verify_library
        
        # State management
        config_path = Path(config_dir) if config_dir else Path("/config")
        self.state_manager = StateManager(config_path)
        # Heartbeat destination (Candidate A). A watcher thread that dies
        # inside the scheduler's process leaves no trace anywhere else: the
        # scheduler keeps looping and the container stays healthy while
        # completed downloads are never picked up.
        self._heartbeat_dir = config_path
        
        # Library verification
        if library_path is None:
            library_path = get_motogp_library_path()
        self.library_checker = LibraryChecker(library_path)
        
        # Runtime tracking
        self.pending: Dict[str, datetime] = {}  # folder -> first_seen_time
        self.folder_sizes: Dict[str, int] = {}  # folder -> last_known_size
        
        # Thread control
        self._running = False
        self._thread: Optional[threading.Thread] = None
        
        # Media extensions
        self.media_extensions = {'.mkv', '.mp4', '.avi', '.m4v', '.wmv', '.ts', '.m2ts'}
        
        logger.info(f"Watcher initialized for: {self.watch_dir}")
        logger.info(f"Library verification: {'enabled' if verify_library else 'disabled'}")
    
    def _get_folder_size(self, folder_path: Path) -> int:
        """Get total size of all files in folder."""
        total = 0
        try:
            for item in folder_path.rglob('*'):
                if item.is_file():
                    try:
                        total += item.stat().st_size
                    except (OSError, IOError):
                        pass
        except Exception as e:
            logger.debug(f"Error getting folder size for {folder_path.name}: {e}")
        return total
    
    def _is_folder_stable(self, folder_path: Path) -> bool:
        """Check if folder has stopped changing (download complete)."""
        folder_str = str(folder_path)
        current_size = self._get_folder_size(folder_path)
        
        # First time seeing this folder
        if folder_str not in self.folder_sizes:
            self.folder_sizes[folder_str] = current_size
            self.pending[folder_str] = datetime.now()
            logger.debug(f"New folder detected: {folder_path.name} (size: {current_size:,} bytes)")
            return False
        
        # Size changed - reset timer
        if current_size != self.folder_sizes[folder_str]:
            old_size = self.folder_sizes[folder_str]
            self.folder_sizes[folder_str] = current_size
            self.pending[folder_str] = datetime.now()
            logger.debug(f"Folder size changed: {folder_path.name} ({old_size:,} -> {current_size:,} bytes)")
            return False
        
        # Size unchanged - check if stable long enough
        first_seen = self.pending.get(folder_str, datetime.now())
        stable_time = (datetime.now() - first_seen).total_seconds()
        
        if stable_time >= self.stable_seconds:
            logger.debug(f"Folder stable: {folder_path.name} (stable for {stable_time:.0f}s)")
            return True
        
        return False
    
    def _has_media_files(self, folder_path: Path) -> Tuple[bool, List[str], List[str]]:
        """
        Check if folder contains media files.
        
        Returns:
            Tuple of (has_media: bool, media_files: List[str], other_extensions: List[str])
        """
        media_files = []
        other_files = []
        
        try:
            for item in folder_path.rglob('*'):
                if item.is_file():
                    if item.suffix.lower() in self.media_extensions:
                        media_files.append(item.name)
                    else:
                        other_files.append(item.suffix.lower())
        except Exception as e:
            logger.debug(f"Error scanning {folder_path.name}: {e}")
        
        return (len(media_files) > 0, media_files, other_files)
    
    def _should_skip_processed(self, folder_name: str) -> Tuple[bool, str]:
        """
        Determine if a processed folder should be skipped.
        
        Returns:
            Tuple of (should_skip: bool, reason: str)
        """
        if not self.state_manager.is_processed(folder_name):
            return (False, "")
        
        # Verify library if enabled
        if self.verify_library:
            exists, library_path = self.library_checker.exists_in_library(folder_name)
            
            if exists:
                return (True, f"exists in library: {library_path}")
            else:
                # Item was processed but no longer in library - allow reprocessing
                logger.info(f"🔄 Reprocessing: {folder_name} (not found in library)")
                self.state_manager.remove_from_processed(folder_name)
                return (False, "")
        
        return (True, "in processed list")
    
    def _process_folder(self, folder_path: Path) -> bool:
        """Process a single folder."""
        folder_name = folder_path.name
        folder_str = str(folder_path)
        
        # Generate correlation ID for tracking through pipeline
        correlation_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        logger.info(f"📂 [{correlation_id}] Processing: {folder_name}")
        
        # Emit PROCESSING activity
        episode_key, episode_name, round_num, _ = _folder_to_episode_key(folder_name)
        if episode_key:
            emit_activity(
                episode_key=episode_key,
                episode_name=episode_name,
                stage=ActivityStage.PROCESSING,
                details="Sorting and renaming",
                round_info={"round": round_num, "venue": None},
                release_name=folder_name
            )
        
        try:
            success = self.processor(folder_str)
            
            if success:
                logger.info(f"✅ [{correlation_id}] Successfully processed: {folder_name}")
                self.state_manager.mark_processed(folder_name)
                # Invalidate library cache so new item is recognized
                self.library_checker.invalidate_cache()
                # Note: COMPLETE event is emitted by plex_tagger after successful tagging
                return True
            else:
                error_msg = "the sorter did not confirm the move"
                logger.warning(f"⚠️ [{correlation_id}] Processing failed: {folder_name} - {error_msg}")
                # The sorter returning False and the processor raising are
                # different faults with different next steps, so both record
                # the stage rather than the old generic "Processing failed".
                self.state_manager.mark_failed(
                    folder_name, error_msg, stage=failure_reasons.STAGE_PROCESS
                )

                # Emit ERROR activity
                if episode_key:
                    mark_error(
                        episode_key=episode_key,
                        error_message=failure_reasons.summarise(
                            failure_reasons.STAGE_PROCESS, error_msg
                        ),
                        episode_name=episode_name
                    )
                
                return False
        
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            logger.error(f"❌ [{correlation_id}] Error processing {folder_name}: {error_msg}", exc_info=True)
            self.state_manager.mark_failed(
                folder_name, error_msg, stage=failure_reasons.STAGE_PROCESS
            )
            if episode_key:
                mark_error(
                    episode_key=episode_key,
                    error_message=failure_reasons.summarise(
                        failure_reasons.STAGE_PROCESS, error_msg
                    ),
                    episode_name=episode_name,
                )
            return False
        
        finally:
            # Clean up pending state
            self.pending.pop(folder_str, None)
            self.folder_sizes.pop(folder_str, None)
    
    def _scan_directory(self):
        """
        Scan watch directory for new folders to process.

        Every completed scan is a heartbeat, including one that found nothing
        and one whose watch directory is missing: both prove the thread is
        alive, and a missing downloads path is already reported as its own
        problem rather than disguised as a dead watcher.
        """
        logger.debug(f"Scanning: {self.watch_dir}")

        if not self.watch_dir.exists():
            logger.warning(f"Watch directory does not exist: {self.watch_dir}")
            self._beat()
            return
        
        try:
            items = list(self.watch_dir.iterdir())
            folders = [item for item in items if item.is_dir()]
            
            logger.debug(f"Found {len(folders)} folders in watch directory")
            
            for folder_path in folders:
                folder_name = folder_path.name
                
                # Skip hidden/temp folders
                if folder_name.startswith('.') or folder_name.startswith('_'):
                    logger.debug(f"Skipping hidden/temp folder: {folder_name}")
                    continue
                
                # FIRST: Check if file exists in library (smart reprocessing)
                # This takes priority over both processed and failed states
                if self.verify_library:
                    exists, library_path = self.library_checker.exists_in_library(folder_name)
                    
                    if exists:
                        # File is in library - safe to skip
                        logger.debug(f"Skipping: {folder_name} (exists in library)")
                        continue
                    else:
                        # File NOT in library - clear any stale state and allow processing
                        if self.state_manager.is_processed(folder_name):
                            logger.info(f"🔄 Reprocessing: {folder_name} (not found in library, was marked processed)")
                            self.state_manager.remove_from_processed(folder_name)
                        
                        if self.state_manager.is_failed(folder_name):
                            logger.info(f"🔄 Clearing failed state: {folder_name} (not found in library)")
                            self.state_manager.clear_failed(folder_name)
                
                # If library verification is disabled, use state-based checks
                else:
                    # Check if should skip (processed)
                    if self.state_manager.is_processed(folder_name):
                        logger.debug(f"Skipping: {folder_name} (in processed list)")
                        continue
                    
                    # Check if this is a failed item that should be retried
                    if self.state_manager.is_failed(folder_name):
                        if not self.state_manager.should_retry(
                            folder_name, 
                            self.max_retry_attempts,
                            self.retry_backoff_minutes
                        ):
                            failed_item = self.state_manager.get_failed_item(folder_name)
                            logger.debug(
                                f"Skipping failed item (attempt {failed_item.attempt_count}/{self.max_retry_attempts}): "
                                f"{folder_name}"
                            )
                            continue
                        else:
                            failed_item = self.state_manager.get_failed_item(folder_name)
                            logger.info(f"🔄 Retrying failed item (attempt {failed_item.attempt_count + 1}): {folder_name}")
                
                # Check if has media files
                has_media, media_files, other_extensions = self._has_media_files(folder_path)
                
                if not has_media:
                    if other_extensions:
                        unique_ext = set(other_extensions)
                        logger.debug(f"No media files in {folder_name}, found: {unique_ext}")
                    else:
                        logger.debug(f"Empty or no files in {folder_name}")
                    continue
                
                logger.debug(f"Found {len(media_files)} media file(s) in {folder_name}")
                
                # Check if stable (download complete)
                if not self._is_folder_stable(folder_path):
                    folder_str = str(folder_path)
                    if folder_str in self.pending:
                        age = (datetime.now() - self.pending[folder_str]).total_seconds()
                        size = self.folder_sizes.get(folder_str, 0)
                        logger.debug(f"Waiting for stability: {folder_name} ({age:.0f}s, {size:,} bytes)")
                    continue
                
                # Ready to process
                logger.info(f"📂 New download ready: {folder_name}")
                
                # Emit DOWNLOAD_COMPLETE activity
                episode_key, episode_name, round_num, _ = _folder_to_episode_key(folder_name)
                if episode_key:
                    emit_activity(
                        episode_key=episode_key,
                        episode_name=episode_name,
                        stage=ActivityStage.DOWNLOAD_COMPLETE,
                        details="Download complete, ready to process",
                        round_info={"round": round_num, "venue": None},
                        release_name=folder_name
                    )
                
                self._process_folder(folder_path)

        except Exception as e:
            logger.error(f"Error scanning directory: {e}", exc_info=True)
        finally:
            # Also after a failed scan: the loop survived it, which is the
            # fact the record is for. A thread that raises and stops scanning
            # stops beating, and that is what must show up as stale.
            self._beat()

    def _beat(self):
        """Record that this thread completed a scan cycle."""
        write_heartbeat(
            self._heartbeat_dir, "watcher",
            last_scan=datetime.now().isoformat(),
        )
    
    def _run_loop(self):
        """Main watcher loop."""
        logger.info(f"👁️ Watcher started")
        logger.info(f"   Watch directory: {self.watch_dir}")
        logger.info(f"   Library path: {self.library_checker.library_path}")
        logger.info(f"   Stable time: {self.stable_seconds}s")
        logger.info(f"   Check interval: {self.check_interval}s")
        logger.info(f"   Max retry attempts: {self.max_retry_attempts}")
        logger.info(f"   Library verification: {self.verify_library}")
        
        stats = self.state_manager.get_stats()
        logger.info(f"   State: {stats['processed_count']} processed, {stats['failed_count']} failed")
        
        while self._running:
            self._scan_directory()
            
            # Sleep in small chunks for responsive shutdown
            for _ in range(self.check_interval):
                if not self._running:
                    break
                time.sleep(1)
        
        logger.info("👁️ Watcher stopped")
    
    def start(self):
        """Start the watcher in a background thread."""
        if self._running:
            logger.warning("Watcher already running")
            return
        
        self._running = True
        write_heartbeat(
            self._heartbeat_dir, "watcher",
            started=datetime.now().isoformat(),
        )
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="DownloadWatcher")
        self._thread.start()
    
    def stop(self):
        """Stop the watcher."""
        logger.info("Stopping watcher...")
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
    
    def run_once(self):
        """Run a single scan (for testing or manual trigger)."""
        logger.info("Running single scan...")
        self._scan_directory()
        logger.info("Scan complete")
    
    def get_status(self) -> dict:
        """Get current watcher status."""
        stats = self.state_manager.get_stats()
        return {
            "running": self._running,
            "watch_dir": str(self.watch_dir),
            "watch_dir_exists": self.watch_dir.exists(),
            "library_path": str(self.library_checker.library_path),
            "verify_library": self.verify_library,
            "pending_count": len(self.pending),
            "pending_folders": [Path(p).name for p in self.pending.keys()],
            **stats
        }
    
    def clear_failed(self, folder_name: str = None):
        """Clear failed state for retry."""
        self.state_manager.clear_failed(folder_name)
        if folder_name:
            logger.info(f"Cleared failed state for: {folder_name}")
        else:
            logger.info("Cleared all failed states")
    
    def clear_processed(self, folder_name: str = None):
        """Clear processed state for reprocessing."""
        self.state_manager.clear_processed(folder_name)
        if folder_name:
            logger.info(f"Cleared processed state for: {folder_name}")
        else:
            logger.info("Cleared all processed states")
    
    def refresh_library_cache(self):
        """Force refresh of library cache."""
        self.library_checker.invalidate_cache()
        logger.info("Library cache invalidated")
    
    def debug_state(self):
        """Log current state for debugging."""
        logger.info("=== WATCHER STATE DEBUG ===")
        logger.info(f"Watch directory: {self.watch_dir}")
        logger.info(f"Watch dir exists: {self.watch_dir.exists()}")
        logger.info(f"Library path: {self.library_checker.library_path}")
        logger.info(f"Library exists: {self.library_checker.library_path.exists()}")
        logger.info(f"Running: {self._running}")
        logger.info(f"Verify library: {self.verify_library}")
        
        stats = self.state_manager.get_stats()
        logger.info(f"Processed count: {stats['processed_count']}")
        logger.info(f"Failed count: {stats['failed_count']}")
        
        if stats['failed_items']:
            logger.info("Failed items:")
            for item in stats['failed_items']:
                logger.info(f"  - {item['name']}: {item['attempts']} attempts, error: {item['last_error']}")
        
        if self.pending:
            logger.info("Pending folders:")
            for folder, first_seen in self.pending.items():
                age = (datetime.now() - first_seen).total_seconds()
                size = self.folder_sizes.get(folder, 0)
                logger.info(f"  - {Path(folder).name}: {age:.0f}s old, {size:,} bytes")
        
        # Show some processed items
        processed_list = list(self.state_manager.state.processed)
        if processed_list:
            logger.info(f"Processed items (showing last 10 of {len(processed_list)}):")
            for item in sorted(processed_list)[-10:]:
                exists, path = self.library_checker.exists_in_library(item)
                status = "✓ in library" if exists else "✗ NOT in library"
                logger.info(f"  - {item}: {status}")
        
        logger.info("=== END STATE DEBUG ===")


# =============================================================================
# FACTORY FUNCTION
# =============================================================================

def create_watcher(
    watch_dir: str,
    processor: Callable[[str], bool],
    config_dir: str = None,
    library_path: str = None,
    verify_library: bool = True
) -> DownloadWatcher:
    """
    Factory function to create a configured watcher.
    
    Args:
        watch_dir: Directory to watch
        processor: Function to process folders
        config_dir: Directory for state file (optional)
        library_path: Path to Plex library (optional, uses config if not provided)
        verify_library: Whether to verify items exist in library before skipping
    
    Returns:
        Configured DownloadWatcher instance
    """
    return DownloadWatcher(
        watch_dir=watch_dir,
        processor=processor,
        stable_seconds=15,
        check_interval=10,
        config_dir=config_dir,
        library_path=library_path,
        max_retry_attempts=5,
        retry_backoff_minutes=30,
        verify_library=verify_library
    )


# =============================================================================
# STANDALONE TESTING
# =============================================================================

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="MotoGP Download Watcher")
    parser.add_argument("--watch-dir", "-w", default="/downloads/complete/motogp",
                        help="Directory to watch")
    parser.add_argument("--config-dir", "-c", default="/config",
                        help="Config directory for state files")
    parser.add_argument("--library-path", "-l", default=None,
                        help="Path to Plex library")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Don't actually process, just log")
    parser.add_argument("--debug", "-d", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--status", "-s", action="store_true",
                        help="Show current status and exit")
    parser.add_argument("--clear-failed", metavar="NAME", nargs="?", const="__all__",
                        help="Clear failed state (use without argument for all)")
    parser.add_argument("--clear-processed", metavar="NAME", nargs="?", const="__all__",
                        help="Clear processed state (use without argument for all)")
    parser.add_argument("--no-verify-library", action="store_true",
                        help="Disable library verification")
    parser.add_argument("--refresh-cache", action="store_true",
                        help="Refresh library cache and exit")
    
    args = parser.parse_args()
    
    # Setup logging
    setup_logging(debug_mode=args.debug)
    
    def test_processor(folder_path: str) -> bool:
        logger.info(f"[TEST] Would process: {folder_path}")
        return True
    
    watcher = create_watcher(
        watch_dir=args.watch_dir,
        processor=test_processor if args.dry_run else test_processor,
        config_dir=args.config_dir,
        library_path=args.library_path,
        verify_library=not args.no_verify_library
    )
    
    if args.status:
        watcher.debug_state()
        sys.exit(0)
    
    if args.refresh_cache:
        watcher.refresh_library_cache()
        logger.info("Library cache refreshed")
        sys.exit(0)
    
    if args.clear_failed is not None:
        name = None if args.clear_failed == "__all__" else args.clear_failed
        watcher.clear_failed(name)
        sys.exit(0)
    
    if args.clear_processed is not None:
        name = None if args.clear_processed == "__all__" else args.clear_processed
        watcher.clear_processed(name)
        sys.exit(0)
    
    logger.info("Running single scan...")
    watcher.run_once()
    logger.info("Done.")