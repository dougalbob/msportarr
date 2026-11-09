#!/usr/bin/env python3
"""
MotoGP Common Module
=====================

Single source of truth for MotoGP session definitions, pattern matching,
calendar functions, and shared utilities.

This module is imported by:
    - motogp_grabber.py
    - motogp_sort.py
    - motogp_plex_tagger.py
    - motogp_scheduler.py

Architecture follows the F1 common module pattern with adaptations for
MotoGP's three-class structure (MotoGP, Moto2, Moto3).
"""


import os
import sys
import re
import json
import argparse
import logging
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

# =============================================================================
# CENTRALIZED LOGGING
# =============================================================================

import logging
from logging.handlers import RotatingFileHandler
from typing import Optional
import threading

# Global lock for thread-safe logging setup
_logging_lock = threading.Lock()
_logging_initialized = False


class LogConfig:
    """Logging configuration singleton."""
    
    # Default paths - overridden by setup_logging()
    log_dir: Path = Path("/config/logs")
    
    # Log levels
    console_level: int = logging.INFO
    file_level: int = logging.DEBUG
    
    # File settings
    max_bytes: int = 10 * 1024 * 1024  # 10MB
    backup_count: int = 5
    
    # Debug mode
    debug_mode: bool = False


def setup_logging(
    name: str = "motogp",
    log_dir: Optional[Path] = None,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    debug_mode: bool = False,
    force: bool = False
) -> logging.Logger:
    """
    Centralized logging setup for all MotoGP scripts.
    
    Call this once at the start of each script. Subsequent calls
    return the existing logger unless force=True.
    
    Args:
        name: Logger name (usually "motogp")
        log_dir: Directory for log files (default: /config/logs)
        console_level: Console output level
        file_level: File output level  
        debug_mode: Enable verbose debug logging
        force: Force reconfiguration even if already set up
    
    Returns:
        Configured root logger for the motogp hierarchy
    
    Usage:
        from motogp_common import setup_logging
        
        logger = setup_logging(debug_mode=True)
        # or for module-specific logger:
        logger = logging.getLogger("motogp.watcher")
    """
    global _logging_initialized
    
    with _logging_lock:
        # Return existing logger if already configured
        if _logging_initialized and not force:
            return logging.getLogger("motogp")
        
        # Determine log directory
        if log_dir is None:
            # Try config dir first, fall back to script dir
            config_log_dir = CONFIG_DIR / "logs"
            if CONFIG_DIR.exists() or str(CONFIG_DIR).startswith("/config"):
                log_dir = config_log_dir
            else:
                log_dir = SCRIPT_DIR / "logs"
        
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # Update config
        LogConfig.log_dir = log_dir
        LogConfig.console_level = console_level
        LogConfig.file_level = file_level
        LogConfig.debug_mode = debug_mode
        
        # Get or create the motogp root logger
        logger = logging.getLogger("motogp")
        logger.setLevel(logging.DEBUG)
        
        # Clear existing handlers
        logger.handlers.clear()

        # Prevent propagation to root logger (avoids double logging)
        logger.propagate = False        

        # Formatter with module identification
        detailed_formatter = logging.Formatter(        
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        
        # Simple formatter for console
        console_formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S"
        )
        
        # Console handler
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG if debug_mode else console_level)
        console.setFormatter(console_formatter)
        logger.addHandler(console)
        
        # Main log file (rotating)
        main_log = log_dir / "motogp.log"
        try:
            file_handler = RotatingFileHandler(
                main_log,
                maxBytes=LogConfig.max_bytes,
                backupCount=LogConfig.backup_count,
                encoding="utf-8"
            )
            file_handler.setLevel(file_level)
            file_handler.setFormatter(detailed_formatter)
            logger.addHandler(file_handler)
        except Exception as e:
            logger.warning(f"Could not create log file {main_log}: {e}")
        
        # Debug log (separate, more verbose) - only in debug mode
        if debug_mode:
            debug_log = log_dir / "motogp_debug.log"
            try:
                debug_formatter = logging.Formatter(
                    "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s:%(funcName)s:%(lineno)d: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S"
                )
                debug_handler = RotatingFileHandler(
                    debug_log,
                    maxBytes=50 * 1024 * 1024,  # 50MB for debug
                    backupCount=3,
                    encoding="utf-8"
                )
                debug_handler.setLevel(logging.DEBUG)
                debug_handler.setFormatter(debug_formatter)
                logger.addHandler(debug_handler)
            except Exception as e:
                logger.warning(f"Could not create debug log file: {e}")
        
        _logging_initialized = True
        
        # Log startup
        logger.info(f"Logging initialized: {log_dir}")
        if debug_mode:
            logger.debug("Debug mode enabled")
        
        return logger


def get_logger(name: str = None) -> logging.Logger:
    """
    Get a logger in the motogp hierarchy.
    
    Args:
        name: Optional sub-logger name (e.g., "watcher", "sort")
              If None, returns the root motogp logger
    
    Returns:
        Logger instance
    
    Usage:
        logger = get_logger("watcher")  # Returns motogp.watcher logger
        logger = get_logger()           # Returns motogp logger
    """
    if name:
        return logging.getLogger(f"motogp.{name}")
    return logging.getLogger("motogp")


# =============================================================================
# LEGACY LOGGING COMPATIBILITY
# =============================================================================

def setup_legacy_logging(
    log_prefix: str,
    log_dir: Optional[str] = None,
    dry_run: bool = False
) -> str:
    """
    Legacy logging setup for backwards compatibility.
    
    Creates timestamped log files like before, but also logs to
    the centralized motogp.log.
    
    Args:
        log_prefix: Prefix for log file (e.g., "grabber", "sort")
        log_dir: Directory for logs (default: /config/logs)
        dry_run: If True, adds DRY-RUN notice
    
    Returns:
        Path to the timestamped log file
    """
    from datetime import datetime
    
    # Set up centralized logging first
    setup_logging()
    
    # Determine log directory
    if log_dir is None:
        log_dir = str(LogConfig.log_dir)
    
    os.makedirs(log_dir, exist_ok=True)
    
    # Create timestamped log file
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = os.path.join(log_dir, f"{log_prefix}_{timestamp}.log")
    
    # Add file handler for this specific run
    logger = logging.getLogger("motogp")
    
    run_handler = logging.FileHandler(log_file, encoding="utf-8")
    run_handler.setLevel(logging.DEBUG)
    run_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(run_handler)
    
    # Cleanup old logs (keep last 20)
    try:
        log_files = sorted([
            f for f in os.listdir(log_dir)
            if f.startswith(f"{log_prefix}_") and f.endswith(".log")
        ], reverse=True)
        for old_log in log_files[20:]:
            os.remove(os.path.join(log_dir, old_log))
    except Exception:
        pass
    
    logger.info(f"Log file: {log_file}")
    if dry_run:
        logger.info("DRY RUN MODE - no changes will be made")
    
    return log_file

# =============================================================================
# PATH CONFIGURATION
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()

# Config directory - use CONFIG_DIR env var in container, otherwise local
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", str(SCRIPT_DIR / "config")))

# Import config after defining SCRIPT_DIR
try:
    from utils.config_loader import get_motogp_library_path, get_downloads_path
    _CONFIG_AVAILABLE = True
except ImportError:
    _CONFIG_AVAILABLE = False


def get_library_path() -> str:
    """
    Get the MotoGP library path.

    Configuration wins. When running without the config loader (direct
    execution of a script) fall back to MOTOGP_LIBRARY_PATH, then a neutral
    default - never a guessed share spelling, because a plausible-looking wrong
    path is far worse than an obvious one.
    """
    if _CONFIG_AVAILABLE:
        return get_motogp_library_path()
    return os.environ.get("MOTOGP_LIBRARY_PATH", "/media/MotoGP")


def get_config_dir() -> Path:
    """Get config directory path."""
    return CONFIG_DIR


def state_path(config_dir, name: str) -> Path:
    """
    ``<config_dir>/<name>.<series>.json`` for the series this module is bound to.

    Re-exported from ``utils/state_files.py`` because every call site already
    imports from here and because this module is the one that knows which
    series it is bound to - a call site should never have to name a series just
    to find its own grabber state.
    """
    from utils.state_files import state_path as _state_path

    return _state_path(config_dir, name, series=SERIES_KEY)


# Legacy compatibility
MOTOGP_LIBRARY_PATH = get_library_path()
BASE_PATH = "/data" if os.path.exists("/data") else "/mnt/user/data"

# =============================================================================
# SERIES VOCABULARY
# =============================================================================
# The class and session enums, and every table keyed on them, come from the
# series registry (series/). They are bound here rather than declared here so
# MotoGP's vocabulary lives in exactly one place: adding a session means editing
# series/motogp.py, and adding a second series means adding series/<name>.py
# with no change to any algorithm in this module.
#
# The enums are deliberately not declared here any more. Declaring them twice -
# once as data for the registry and once as an enum - is how the two drift, and
# the symptom is a folder number no table recognises.
#
# This sits above the first table that needs it rather than beside the enums,
# because CLASS_ASSET_DIRS is declared earlier in the file than SessionType is.

from series import runtime as _series_runtime  # noqa: E402

_MGP = _series_runtime("motogp")

#: The series this module is bound to. Everything below reads MotoGP's
#: vocabulary; nothing below knows a second series exists yet.
SERIES_KEY = _MGP.key

#: The declaration behind it, for the fields that describe where its media
#: lives rather than what a session is called.
SERIES = _MGP.series

# Asset directories
ASSETS_DIR = SCRIPT_DIR / "assets"

# Where each class' source artwork lives. Bound from the registry so a second
# series brings its own directories with it; the sorter renders session text and
# writes ordinary JPEGs into the library, so Plex never needs these paths.
CLASS_ASSET_DIRS = _MGP.asset_dirs

RaceClass = _MGP.race_class

# Retained for callers that want the display names as a plain dict.
RACE_CLASS_DISPLAY_NAMES: Dict[RaceClass, str] = {
    member: member.display_name for member in RaceClass
}


# =============================================================================
# SESSION TYPE ENUM
# =============================================================================
# Bound from the registry. The enum's integer values are chronological order
# and nothing else - the number written into a folder name comes from
# element_number() below, never from int(session_type).
#
# Adding a member means adding it to series/motogp.py (the enum, its display and
# short names, its class, and its element number). Nothing here changes.

SessionType = _MGP.session_type


# =============================================================================
# SESSION ELEMENT NUMBERS  (the "E<NN>" in yyRNNEEE)
# =============================================================================
# Bound from the registry; the allocation rules and the reason for the gaps live
# in series/motogp.py beside the table itself, so they cannot drift apart.
#
# The number is baked into folder names, file names and Plex identity, so it is
# not merely a sort key - changing one orphans files. It is allocated in BLOCKS
# OF TEN with room to insert a session between two existing ones without
# renumbering anything.
#
# Two rules the table satisfies:
#
# 1. It must be CHRONOLOGICAL for every weekend format the app understands. The
#    old dense table could not be: it was full (1-24, no gaps), so FP3 had to be
#    appended after the Race and a 2027 round listed FP3 and Sprint Qualifying
#    *after* the Grand Prix in any folder/title listing.
# 2. It must leave room for a session that does not exist yet. 2027 introduced
#    two; the answer is now a new number in a free slot, never a renumber.

SESSION_ELEMENTS: Dict[SessionType, int] = _MGP.session_elements
LEGACY_ELEMENT_SEASON = _MGP.legacy_element_season
SESSION_ELEMENTS_LEGACY: Dict[SessionType, int] = _MGP.legacy_session_elements

# How a grab was found, recorded in the grabber_state.json "grabbed" entries.
#
# The home page reports the latest *scheduled* addition (issue #51), and the two
# paths must never be conflated: a backfill is a correction or a catch-up, not
# the machine keeping pace with the weekend. Entries written before this
# vocabulary existed carry no mode at all and are reported as unknown - never
# inferred from timestamps, for the same reason an ambiguous element number is
# refused rather than guessed.
GRAB_MODE_SCHEDULED = "scheduled"
GRAB_MODE_BACKFILL = "backfill"


def element_number(session_type: SessionType, season: Optional[int] = None) -> int:
    """The number to WRITE into a folder name for this session and season."""
    return _MGP.element_number(session_type, season)


def legacy_element_number(session_type: SessionType) -> int:
    """The number an earlier version would have written. Read-only."""
    return _MGP.legacy_element_number(session_type)


def find_legacy_folders(season: int, session_folders: Iterable[str]) -> List[str]:
    """
    Session folder names inside `season` that the current scheme cannot read.

    Season is a required argument, not a nicety. A stale "26R04E12 MotoGP Q2" is
    indistinguishable by name from a modern "Moto3 FP1" at E12, so guessing from
    the name alone would either miss stale folders or flag valid ones. The season
    folder the file actually sits in is the only reliable signal, and it is free.

    The caller is expected to say this out loud. A folder that silently never
    matches is the failure mode this whole module exists to avoid: the session
    looks absent, so the grabber fetches it again, and the library ends up with
    two items that mean the same thing.
    """
    return _MGP.find_legacy_folders(season, session_folders)


def _element_tables(season: Optional[int] = None):
    """
    Which (elements, is_legacy) pairs apply when READING a folder.

    With no season, both schemes are in play and the caller must be able to see
    that a number is ambiguous - that is the honest answer, and it is what stops
    an unknown-season scan from quietly picking one meaning.
    """
    return _MGP._element_tables(season)


# Session display names (session type only, without class). The short labels are
# the user-facing Plex titles; why they are keyed on the session type rather
# than derived from the release title is explained in series/motogp.py.
SESSION_DISPLAY_NAMES: Dict[SessionType, str] = _MGP.session_display_names
SESSION_SHORT_NAMES: Dict[SessionType, str] = _MGP.session_short_names

# Map session types to their race class
SESSION_RACE_CLASS: Dict[SessionType, RaceClass] = _MGP.session_race_class

# Core sessions (typically wanted for collection)
CORE_SESSIONS: Set[SessionType] = _MGP.core_sessions

# Feature race sessions (for special genre tagging)
FEATURE_RACE_SESSIONS: Set[SessionType] = _MGP.feature_race_sessions

# Default desired sessions configuration
DEFAULT_DESIRED_SESSIONS: Dict[SessionType, bool] = _MGP.default_desired_sessions


def get_desired_sessions() -> Dict[SessionType, bool]:
    """
    Get desired sessions from WebUI config, falling back to defaults.
    
    The WebUI saves session preferences to webui_settings.<series>.json.
    This function loads those preferences at runtime.
    
    Returns:
        Dict mapping SessionType to whether it's desired (True/False)
    """
    webui_config = state_path(CONFIG_DIR, "webui_settings")
    
    if webui_config.exists():
        try:
            with open(webui_config, 'r') as f:
                config = json.load(f)
            
            sessions_config = config.get("sessions", {})
            
            if sessions_config:
                result = {}
                for session_type in SessionType:
                    if session_type == SessionType.UNKNOWN:
                        continue
                    # WebUI uses session type name as key (e.g., "MOTOGP_RACE")
                    key = session_type.name
                    # Fall back to default if not specified
                    default = DEFAULT_DESIRED_SESSIONS.get(session_type, False)
                    result[session_type] = sessions_config.get(key, default)
                
                # Log what we loaded (debug level)
                enabled_count = sum(1 for v in result.values() if v)
                logging.getLogger("motogp.common").debug(
                    f"Loaded desired sessions from config: {enabled_count} enabled"
                )
                return result
        
        except Exception as e:
            logging.getLogger("motogp.common").warning(
                f"Could not load {webui_config.name}: {e}, using defaults"
            )
    
    # Fall back to defaults
    return DEFAULT_DESIRED_SESSIONS.copy()



# =============================================================================
# CALENDAR KEY MAPPINGS
# =============================================================================

# Map calendar session keys to SessionType
CALENDAR_KEY_TO_SESSION: Dict[str, SessionType] = _MGP.calendar_key_to_session

# Reverse mapping. Declared separately in the registry because the relationship
# is many-to-one: MotoGP Q1 and Q2 both read "motogp_qualifying", so reversing
# the forward map would silently drop one of them.
SESSION_TO_CALENDAR_KEY: Dict[SessionType, str] = _MGP.session_to_calendar_key

# Map MotoGP session types to weekend-format session names.
# Support class sessions are absent: only the MotoGP format varies by season.
SESSION_TO_MOTOGP_SESSION_NAME: Dict[SessionType, str] = _MGP.session_to_format_name

# Reverse mapping for resolving a calendar's release_aliases (e.g. "sq" ->
# "sprint_qualifying"). "qualifying" is the combined session and maps to Q2,
# which is the target the grabber uses for a combined qualifying release; Q1
# has no distinct weekend-format name of its own.
MOTOGP_SESSION_NAME_TO_TYPE: Dict[str, SessionType] = _MGP.format_name_to_session

# The days a calendar may use, in running order. Thursday is included because
# flyaway weekends (and the occasional Thursday running) put sessions there;
# the WebUI and the validator both read this one tuple so they cannot drift.
CALENDAR_DAYS: Tuple[str, ...] = ("thursday", "friday", "saturday", "sunday")

# Day index relative to the Friday of the weekend. Used to place a session on
# the calendar and to reconcile a session's local day with its UTC day.
CALENDAR_DAY_OFFSETS: Dict[str, int] = {
    "monday": 3,
    "tuesday": 4,
    "wednesday": -2,
    "thursday": -1,
    "friday": 0,
    "saturday": 1,
    "sunday": 2,
}

# Map calendar keys to their day in the weekend. This is the legacy fallback
# only: a round with a sessions block is authoritative about which day a key
# is on (see get_session_times()).
CALENDAR_KEY_TO_DAY: Dict[str, str] = _MGP.calendar_key_to_day


# =============================================================================
# SESSION PATTERN MATCHING
# =============================================================================

@dataclass
class SessionPattern:
    """Pattern for matching session types from release titles."""
    session_type: SessionType
    must_have: List[str]
    must_not_have: List[str]
    priority: int = 0

# Session patterns ordered by priority (most specific first).
#
# Bound from the registry. The table is data, not code, for the same reason the
# element numbers are: a release group can invent a token before a season starts,
# and the answer must be a line in series/motogp.py rather than a code release.
SESSION_PATTERNS: List[SessionPattern] = [
    SessionPattern(
        session_type=pattern.session_type,
        must_have=list(pattern.must_have),
        must_not_have=list(pattern.must_not_have),
        priority=pattern.priority,
    )
    for pattern in _MGP.session_patterns
]


def _remap_session_for_class(session_type: SessionType, race_class: RaceClass) -> SessionType:
    """Remap a generic session type to the class-specific version."""
    session_base_map = {
        # FP1
        (SessionType.MOTOGP_FP1, RaceClass.MOTO3): SessionType.MOTO3_FP1,
        (SessionType.MOTOGP_FP1, RaceClass.MOTO2): SessionType.MOTO2_FP1,
        (SessionType.MOTOGP_FP1, RaceClass.MOTOGP): SessionType.MOTOGP_FP1,
        # FP2
        (SessionType.MOTOGP_FP2, RaceClass.MOTO3): SessionType.MOTO3_FP2,
        (SessionType.MOTOGP_FP2, RaceClass.MOTO2): SessionType.MOTO2_FP2,
        (SessionType.MOTOGP_FP2, RaceClass.MOTOGP): SessionType.MOTOGP_FP2,
        # Practice
        (SessionType.MOTOGP_PRACTICE, RaceClass.MOTO3): SessionType.MOTO3_PRACTICE,
        (SessionType.MOTOGP_PRACTICE, RaceClass.MOTO2): SessionType.MOTO2_PRACTICE,
        (SessionType.MOTOGP_PRACTICE, RaceClass.MOTOGP): SessionType.MOTOGP_PRACTICE,
        # Q1
        (SessionType.MOTOGP_Q1, RaceClass.MOTO3): SessionType.MOTO3_Q1,
        (SessionType.MOTOGP_Q1, RaceClass.MOTO2): SessionType.MOTO2_Q1,
        (SessionType.MOTOGP_Q1, RaceClass.MOTOGP): SessionType.MOTOGP_Q1,
        # Q2
        (SessionType.MOTOGP_Q2, RaceClass.MOTO3): SessionType.MOTO3_Q2,
        (SessionType.MOTOGP_Q2, RaceClass.MOTO2): SessionType.MOTO2_Q2,
        (SessionType.MOTOGP_Q2, RaceClass.MOTOGP): SessionType.MOTOGP_Q2,
        # Race
        (SessionType.MOTOGP_RACE, RaceClass.MOTO3): SessionType.MOTO3_RACE,
        (SessionType.MOTOGP_RACE, RaceClass.MOTO2): SessionType.MOTO2_RACE,
        (SessionType.MOTOGP_RACE, RaceClass.MOTOGP): SessionType.MOTOGP_RACE,
    }
    
    return session_base_map.get((session_type, race_class), session_type)


def detect_race_class(text: str) -> RaceClass:
    """Detect race class from text."""
    text_lower = text.lower()
    text_normalized = text_lower.replace(".", " ").replace("-", " ")
    
    # Sprint is always MotoGP
    if "sprint" in text_normalized:
        return RaceClass.MOTOGP
    
    # Warm Up is always MotoGP
    if any(term in text_normalized for term in ["warm up", "warmup", "warming up"]):
        return RaceClass.MOTOGP
    
    # Chequered Flag is MotoGP class
    if "chequered" in text_normalized and "flag" in text_normalized:
        return RaceClass.MOTOGP
    
    # Pre-event press is MotoGP class
    if "pre" in text_normalized and "event" in text_normalized:
        return RaceClass.MOTOGP
    
    # Check explicit class mentions (order matters: moto3/moto2 before motogp)
    if re.search(r'moto\s*3', text_normalized):
        return RaceClass.MOTO3
    elif re.search(r'moto\s*2', text_normalized):
        return RaceClass.MOTO2
    elif re.search(r'moto\s*gp', text_normalized):
        return RaceClass.MOTOGP
    
    # Default to MotoGP
    return RaceClass.MOTOGP


def matches_qualifying_part(text: str, part: int) -> bool:
    """
    Does this text refer to Q1 or Q2 specifically?

    Uses word boundaries so "Qualifying 1080p" is not read as "Qualifying 1".
    """
    text_normalized = text.lower().replace(".", " ").replace("-", " ")
    if part == 1:
        pattern = r"\bq1\b|\bqualifying\s*(?:1|one)\b(?!\d)"
    else:
        pattern = r"\bq2\b|\bqualifying\s*(?:2|two)\b(?!\d)"
    return re.search(pattern, text_normalized) is not None


def classify_session(text: str, race_class: RaceClass = None,
                     round_info: dict = None,
                     calendar: dict = None) -> SessionType:
    """
    Classify session type from text with explicit handling for practice sessions.

    Refuses rather than guesses. An ambiguous release token (SQ, SPQ, a bare
    FP with no number) or a title with no session token at all returns
    UNKNOWN, except for the strictly matched BILLIE race convention; the old code fell through to MOTOGP_RACE, which filed an
    unrecognised title as the Grand Prix - a wrong folder name and a wrong Plex
    identity, permanently.

    round_info and calendar are optional context that make an ambiguous title
    resolvable from data instead of a hard-coded token table:

      * a 2027 round runs FP1 + FP2 + Sprint Qualifying and no Practice, so a
        "Practice"-titled release is the Friday session that replaced it (FP2);
        the same title in a 2026 round is MOTOGP_PRACTICE;
      * the calendar's release_aliases map teaches season-specific vocabulary
        (2027: "sq"/"spq" -> sprint_qualifying) without a code change.
    """
    import logging
    import re
    logger = logging.getLogger("motogp.classify")
    
    text_lower = text.lower()
    text_normalized = text_lower.replace(".", " ").replace("-", " ")

    # Quality tokens are removed before matching. Without this, "Qualifying
    # 1080p" matches the "qualifying 1" pattern (and 2160p matches "2"),
    # which made plain qualifying releases look like Q1/Q2 parts.
    text_normalized = re.sub(
        r"\b(2160p|1080p|1080i|720p|480p|4k|uhd)\b", " ", text_normalized
    )

    logger.debug(f"=" * 50)
    logger.debug(f"CLASSIFY: '{text}'")
    logger.debug(f"NORMALIZED: '{text_normalized}'")

    if race_class is None:
        race_class = detect_race_class(text)
    logger.debug(f"RACE CLASS: {race_class.display_name}")

    # === SEASON RELEASE ALIASES (calendar data) ===
    # A release group can invent a short token before the season starts. The
    # calendar carries the mapping so the first weekend is searchable without
    # waiting for a code release. The round still has to run the session: an
    # alias is a hint about vocabulary, not a licence to hunt a session that
    # is not on the timetable.
    aliases = (calendar or {}).get("release_aliases") or {}
    if isinstance(aliases, dict):
        for token, session_name in aliases.items():
            if not isinstance(token, str) or not isinstance(session_name, str):
                continue
            if re.search(rf"\b{re.escape(token.lower())}\b", text_normalized):
                session_type = MOTOGP_SESSION_NAME_TO_TYPE.get(session_name)
                if session_type is None:
                    logger.warning(
                        f"Calendar release alias {token!r} -> {session_name!r} "
                        f"is not a known MotoGP session; ignoring it"
                    )
                    continue
                if round_info is not None and not round_runs_session(
                        round_info, session_type, calendar):
                    logger.warning(
                        f"Refusing {text!r}: alias {token!r} resolves to "
                        f"{session_type.full_display_name}, which this round "
                        f"does not run"
                    )
                    return SessionType.UNKNOWN
                logger.debug(f">>> RELEASE ALIAS: {token} -> {session_type.name}")
                return session_type

    # === SPRINT QUALIFYING (2027+) ===
    # Must be checked before the practice handling below, because the 2027
    # session is literally called "Sprint Qualifying" and must not be
    # classified as the Sprint race or as grand prix qualifying.
    if re.search(r'\bsprint\b', text_normalized) and re.search(
            r'\bqualif', text_normalized):
        logger.debug(f">>> SPRINT QUALIFYING MATCH")
        return SessionType.MOTOGP_SPRINT_QUALIFYING

    # === BARE "FP" WITH NO NUMBER ===
    # "FP" alone could be FP1, FP2 or FP3. Resolve it only when the round runs
    # exactly one of them; otherwise the honest answer is "unknown".
    if re.search(r'\bfp\b(?!\s*\d)', text_normalized):
        candidates = []
        if round_info is not None:
            sessions = round_session_list(round_info, calendar)
            for name, session_type in (
                ("fp1", SessionType.MOTOGP_FP1),
                ("fp2", SessionType.MOTOGP_FP2),
                ("fp3", SessionType.MOTOGP_FP3),
            ):
                if sessions and name in sessions:
                    candidates.append(session_type)
        if len(candidates) == 1:
            logger.debug(f">>> BARE FP RESOLVED: {candidates[0].name}")
            return candidates[0]
        logger.warning(
            f"Refusing to classify {text!r}: 'FP' has no session number and "
            f"the round does not narrow it to one session"
        )
        return SessionType.UNKNOWN

    # === EXPLICIT PRACTICE SESSION HANDLING ===
    if "practice" in text_normalized:
        logger.debug(f">>> PRACTICE BLOCK ENTERED")

        # FP3 (2027+) - check before FP2/FP1 so "FP3" is not read as FP2.
        for pattern in [r'\bfp3\b', r'\bfp\s*3\b',
                        r'\bfree\s+practice\s+3\b',
                        r'\bfree\s+practice\s+three\b']:
            if re.search(pattern, text_normalized):
                logger.debug(f">>> FP3 MATCH: '{pattern}'")
                return SessionType.MOTOGP_FP3
        
        # Check for FP2 indicators using word boundaries
        fp2_patterns = [
            r'\bfp2\b',
            r'\bfp\s*2\b',
            r'\bfree\s+practice\s+2\b',
            r'\bfree\s+practice\s+two\b',
            r'\bpractice\s+two\b',
            r'\bpractice\s+2\b(?!\d)',  # practice 2 but not practice 2160p
        ]
        for pattern in fp2_patterns:
            if re.search(pattern, text_normalized):
                logger.debug(f">>> FP2 MATCH: '{pattern}'")
                if race_class == RaceClass.MOTO3:
                    return SessionType.MOTO3_FP2
                elif race_class == RaceClass.MOTO2:
                    return SessionType.MOTO2_FP2
                else:
                    return SessionType.MOTOGP_FP2

        # Check for FP1 indicators using word boundaries
        fp1_patterns = [
            r'\bfp1\b',
            r'\bfp\s*1\b',
            r'\bfree\s+practice\s+1\b',
            r'\bfree\s+practice\s+one\b',
            r'\bpractice\s+one\b',
            r'\bpractice\s+1\b(?!\d)',  # practice 1 but not practice 1080p
        ]
        for pattern in fp1_patterns:
            if re.search(pattern, text_normalized):
                logger.debug(f">>> FP1 MATCH: '{pattern}'")
                if race_class == RaceClass.MOTO3:
                    return SessionType.MOTO3_FP1
                elif race_class == RaceClass.MOTO2:
                    return SessionType.MOTO2_FP1
                else:
                    return SessionType.MOTOGP_FP1

        # Check for generic "Free Practice" (without number). This is the same
        # ambiguity as a bare "FP": it could be FP1, FP2 or FP3, so it is
        # refused rather than silently filed as FP1.
        if re.search(r'\bfree\s+practice\b', text_normalized):
            if not re.search(r'\bfree\s+practice\s+(1|2|3|one|two|three)\b', text_normalized):
                logger.warning(
                    f"Refusing to classify {text!r}: 'Free Practice' has no "
                    f"session number"
                )
                return SessionType.UNKNOWN

        # Standalone Practice - the actual Practice session.
        # Support classes still run a Practice session in 2027. MotoGP does
        # not: the 2027 Friday slot is FP2, so resolve against the round's own
        # session list rather than assuming the 2026 meaning.
        logger.debug(f">>> STANDALONE PRACTICE")
        if race_class == RaceClass.MOTO3:
            return SessionType.MOTO3_PRACTICE
        elif race_class == RaceClass.MOTO2:
            return SessionType.MOTO2_PRACTICE

        if round_info is not None:
            sessions = round_session_list(round_info, calendar)
            if sessions is not None:
                if "practice" in sessions:
                    return SessionType.MOTOGP_PRACTICE
                # The 2027 shape: Friday FP1 + FP2 + Sprint Qualifying, no
                # Practice. The un-numbered Practice title is the session that
                # replaced the old Friday Practice, i.e. FP2. Require the full
                # shape so a malformed or support-only round does not get a
                # guess.
                if {"fp1", "fp2", "sprint_qualifying"}.issubset(set(sessions)):
                    logger.debug(">>> 2027 PRACTICE -> FP2")
                    return SessionType.MOTOGP_FP2
                logger.warning(
                    f"Refusing to classify {text!r}: this round runs neither "
                    f"Practice nor the 2027 Friday shape"
                )
                return SessionType.UNKNOWN

        # No round context: keep the 2026 meaning, which is the only released
        # calendar that has a Practice session. A caller that knows the round
        # gets the 2027 resolution above.
        return SessionType.MOTOGP_PRACTICE

    # === REGULAR SESSION PATTERN MATCHING ===
    logger.debug(f">>> PATTERN MATCHING BLOCK")
    sorted_patterns = sorted(SESSION_PATTERNS, key=lambda p: p.priority, reverse=True)

    for pattern in sorted_patterns:
        has_required = any(term in text_normalized for term in pattern.must_have)
        if not has_required:
            continue

        has_excluded = any(term in text_normalized for term in pattern.must_not_have)
        if has_excluded:
            continue

        logger.debug(f">>> PATTERN MATCH: {pattern.session_type.name}")
        return _remap_session_for_class(pattern.session_type, race_class)

    # BILLIE publishes races without a session token. Only accept the exact
    # known release shape and an exact calendar venue/alias: arbitrary tokens
    # between venue and quality could be a new, unrecognised session instead.
    if round_info and race_class in (RaceClass.MOTOGP, RaceClass.MOTO2, RaceClass.MOTO3):
        locations = [round_info.get("venue", "")] + (round_info.get("aliases") or [])
        for location in locations:
            if not isinstance(location, str) or not location.strip():
                continue
            location_pattern = r"[. _-]+".join(
                re.escape(part) for part in re.split(r"[. _-]+", location.strip()) if part
            )
            if re.fullmatch(
                rf"(?:motogp|moto2|moto3)\.\d{{4}}\.{location_pattern}\."
                rf"(?:2160p|1080p|720p)\.WEB\.(?:h\.?264|h\.?265|x264|x265)-BILLIE",
                text, re.IGNORECASE,
            ):
                logger.debug(">>> BILLIE implicit race")
                return _remap_session_for_class(SessionType.MOTOGP_RACE, race_class)

    # === FALLBACK ===
    # No session token matched. The old code returned the Race here, so any
    # unrecognised title ("... SQ ...", "... SPQ ...", a bare "FP") was filed
    # as the Grand Prix. That is worse than refusing: it writes a wrong folder
    # name and a wrong Plex identity, and the library scan then believes the
    # Race is already present. Refuse, and let the caller log the full title.
    logger.warning(f"Refusing to classify {text!r}: no session token found")
    return SessionType.UNKNOWN


def _refusal_reason(text: str, round_info: dict = None,
                    calendar: dict = None) -> str:
    """Explain why classify_session() refused a title (for --report-unknown)."""
    text_normalized = text.lower().replace(".", " ").replace("-", " ")
    # classify_session() removes quality tokens before matching, so the reason
    # must too: "FP 1080p" is a bare FP, not an FP followed by a number.
    text_normalized = re.sub(
        r"\b(2160p|1080p|1080i|720p|480p|4k|uhd)\b", " ", text_normalized
    )
    aliases = (calendar or {}).get("release_aliases") or {}
    if isinstance(aliases, dict):
        for token, session_name in aliases.items():
            if not isinstance(token, str) or not isinstance(session_name, str):
                continue
            if re.search(rf"\b{re.escape(token.lower())}\b", text_normalized):
                session_type = MOTOGP_SESSION_NAME_TO_TYPE.get(session_name)
                if session_type is not None and round_info is not None and not round_runs_session(
                        round_info, session_type, calendar):
                    return (
                        f"alias {token!r} resolves to "
                        f"{session_type.full_display_name}, which this round does not run"
                    )
    if re.search(r'\b(spq|sq)\b', text_normalized):
        return (
            "unknown session token 'SQ' (add a release_aliases entry for this "
            "season, or use the full session name)"
        )
    if re.search(r'\bfp\b(?!\s*\d)', text_normalized):
        return "ambiguous 'FP' token with no session number"
    if "practice" in text_normalized:
        return "the round does not run a Practice session"
    return "no session token in the title"


@dataclass
class ReleaseClassification:
    """Result of classify_release(): the session, or why it was refused."""
    session_type: SessionType
    reason: Optional[str] = None
    race_class: Optional[RaceClass] = None
    round_num: Optional[int] = None
    venue: Optional[str] = None


def classify_release(title: str, calendar: dict = None,
                     season: int = None) -> ReleaseClassification:
    """
    Classify a release title for a dry-run report.

    Unlike classify_session() this also explains a refusal, so an operator can
    see why a candidate was not filed instead of only seeing that it vanished.
    """
    race_class = detect_race_class(title)
    round_info = identify_round(title, calendar) if calendar else None
    session_type = classify_session(
        title, race_class, round_info=round_info, calendar=calendar
    )
    return ReleaseClassification(
        session_type=session_type,
        reason=None if session_type != SessionType.UNKNOWN
        else _refusal_reason(title, round_info, calendar),
        race_class=race_class,
        round_num=round_info.get("round") if round_info else extract_round_number(title),
        venue=round_info.get("venue") if round_info else None,
    )
    
def get_session_from_element(element_num: int,
                             season: Optional[int] = None) -> Optional[SessionType]:
    """
    Get the SessionType for an element number.

    The meaning of a number depends on which numbering scheme wrote it, so pass
    the season whenever it is known (it is embedded in the prefix). With no
    season, only numbers that mean the SAME session in both schemes are
    returned: an ambiguous number must not be guessed, because reading a 2026
    Race folder as a practice session is silent corruption, not a wrong label.
    """
    return _MGP.get_session_from_element(element_num, season)


def element_is_ambiguous(element_num: int, season: Optional[int] = None) -> bool:
    """True when this number means different sessions in the two schemes."""
    return _MGP.element_is_ambiguous(element_num, season)


# =============================================================================
# PREFIX HANDLING
# =============================================================================

def build_prefix(season: int, round_num: int, session_type: SessionType) -> str:
    """
    Build the YYRREE prefix for a session.

    Format: YYRREE
        YY = 2-digit year
        RR = 2-digit round number
        EE = 2-digit session/element number

    Example: 26R06E82 = 2026, Round 6, MotoGP Race (element 82)

    The number comes from element_number(), so a season at or before
    LEGACY_ELEMENT_SEASON keeps the dense pre-2027 value and nothing that is
    already on disk changes. See SESSION_ELEMENTS for why the new scheme is
    spaced in tens.
    """
    return _MGP.build_prefix(season, round_num, session_type)


def parse_prefix(text: str) -> Optional[Tuple[int, int, SessionType]]:
    """
    Parse a YYRREE prefix from text.

    Returns (year, round, session_type), or an EpisodeKey with .ambiguous set
    for a number whose meaning differs between the two numbering schemes. The
    season is read from the prefix, so in practice it always disambiguates.
    """
    return _MGP.parse_prefix(text)


# =============================================================================
# CALENDAR FUNCTIONS
# =============================================================================

def load_calendar(season: int) -> Optional[dict]:
    """Load the MotoGP calendar for a given season."""
    calendar_file = CONFIG_DIR / f"{SERIES_KEY}_{season}.json"
    
    if not calendar_file.exists():
        return None
    
    try:
        with open(calendar_file, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def get_round_by_number(calendar: dict, round_num: int) -> Optional[dict]:
    """Get round info by round number."""
    if not calendar:
        return None
    
    for round_entry in calendar.get("rounds", []):
        if round_entry.get("round") == round_num:
            return round_entry
    
    return None


def extract_round_number(text: str) -> Optional[int]:
    """Extract round number from text."""
    text_lower = text.lower()
    
    number_words = {
        'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
        'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
        'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
        'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
        'nineteen': 19, 'twenty': 20, 'twentyone': 21, 'twentytwo': 22,
    }
    
    match = re.search(r'round\s*(\d+)', text_lower)
    if match:
        return int(match.group(1))
    
    match = re.search(r'\br(\d{1,2})\b', text_lower)
    if match:
        return int(match.group(1))
    
    match = re.search(r'round\s*([a-z]+)', text_lower)
    if match and match.group(1) in number_words:
        return number_words[match.group(1)]
    
    return None


def match_venue(text: str, calendar: dict) -> Optional[dict]:
    """Match text against venue names and aliases in calendar."""
    if not calendar:
        return None
    
    text_lower = text.lower()
    text_normalized = text_lower.replace(".", " ").replace("-", " ")
    tokens = set(text_normalized.split())
    
    # Phase 0: Extract "Grand Prix of X" pattern
    gp_match = re.search(r'grand\s*prix\s*of\s*(\w+)', text_normalized)
    if gp_match:
        gp_location = gp_match.group(1).lower()
        for round_entry in calendar.get("rounds", []):
            venue = round_entry.get("venue", "").lower()
            aliases = [a.lower() for a in round_entry.get("aliases", [])]
            
            if gp_location == venue or gp_location in aliases:
                return round_entry
            
            if gp_location in venue or venue in gp_location:
                return round_entry
            for alias in aliases:
                if gp_location in alias or alias in gp_location:
                    return round_entry
    
    # Phase 1: Multi-word alias matching
    for round_entry in calendar.get("rounds", []):
        venue = round_entry.get("venue", "").lower()
        aliases = [a.lower() for a in round_entry.get("aliases", [])]
        
        all_candidates = [venue] + aliases
        
        for candidate in all_candidates:
            if " " in candidate and candidate in text_normalized:
                return round_entry
    
    # Phase 2: Single-word matching
    for round_entry in calendar.get("rounds", []):
        venue = round_entry.get("venue", "").lower()
        aliases = [a.lower() for a in round_entry.get("aliases", [])]
        
        all_candidates = [venue] + aliases
        
        for candidate in all_candidates:
            if " " not in candidate and candidate in tokens:
                return round_entry
    
    return None


def identify_round(text: str, calendar: dict) -> Optional[dict]:
    """Identify round from text."""
    if not calendar:
        return None
    
    round_num = extract_round_number(text)
    if round_num:
        round_info = get_round_by_number(calendar, round_num)
        if round_info:
            return round_info
    
    return match_venue(text, calendar)


def has_session_times(calendar: dict) -> bool:
    """Check if calendar has detailed session times (2026+ format)."""
    if not calendar:
        return False
    
    rounds = calendar.get("rounds", [])
    if not rounds:
        return False
    
    return "sessions" in rounds[0]


# =============================================================================
# ROUND STATUS AND WEEKEND FORMAT
# =============================================================================
#
# These helpers keep calendar *data* authoritative so a cancelled, postponed or
# rescheduled round, a MotoGP-only event, or a weekend format change never
# requires editing code again.

# Statuses that mean "this round is not happening as published".
INACTIVE_ROUND_STATUSES = {"cancelled", "canceled", "postponed", "replaced"}


def is_round_active(round_info: dict) -> bool:
    """Is this round still on the calendar (not cancelled/postponed/replaced)?"""
    if round_info is None:
        return False
    status = str(round_info.get("status", "scheduled")).lower()
    return status not in INACTIVE_ROUND_STATUSES


def get_round_status(round_info: dict) -> str:
    """Normalised round status, defaulting to 'scheduled'."""
    if not round_info:
        return "scheduled"
    return str(round_info.get("status", "scheduled")).lower()


def parse_utc_offset(utc_offset: Optional[str]) -> Optional[int]:
    """
    Parse a '+03:00' style offset into minutes east of UTC.

    Returns None for a missing or malformed value so callers can fall back
    instead of silently treating the offset as zero.
    """
    if not utc_offset or not isinstance(utc_offset, str):
        return None
    match = re.match(r"^([+-])(\d{2}):(\d{2})$", utc_offset.strip())
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    return sign * (int(match.group(2)) * 60 + int(match.group(3)))


def get_round_classes(round_info: dict, calendar: dict = None) -> List[str]:
    """Classes racing at this round, falling back to the calendar default."""
    if round_info and round_info.get("classes"):
        return list(round_info["classes"])
    if calendar and calendar.get("classes"):
        return list(calendar["classes"])
    return ["motogp", "moto2", "moto3"]


def get_weekend_format(round_info: dict, calendar: dict = None) -> dict:
    """
    Get the weekend format definition for a round.

    Looks up round_info["weekend_format"] in calendar["weekend_formats"].
    Returns {} for old style calendars that have no formats block.
    """
    if not round_info or not calendar:
        return {}
    formats = calendar.get("weekend_formats") or {}
    return formats.get(round_info.get("weekend_format", ""), {})


def round_session_list(round_info: dict, calendar: dict = None,
                       series: str = None) -> Optional[List[str]]:
    """
    Which feature-class sessions this round actually runs.

    Returns a list of session names such as ["fp1", "practice", "fp2",
    "qualifying", "sprint", "warmup", "race"], or None when the calendar does
    not declare them (meaning: assume everything the search rules find).

    The key is ``<series>_sessions`` - ``motogp_sessions`` for MotoGP and
    ``f1_sessions`` for Formula 1. It is prefixed rather than a bare
    ``sessions`` for one reason: ``sessions`` is already the round's timetable
    (day -> session key -> times), and a second meaning for the same word would
    be a bug waiting for the next person who edits a calendar by hand.

    The default is the MotoGP key, because the MotoGP calendars are the ones
    already on disk: a caller that does not name a series keeps reading exactly
    what it always read.
    """
    if not round_info:
        return None
    if series is None:
        series = SERIES_KEY
    sessions = round_info.get(f"{series}_sessions")
    if sessions is not None:
        return list(sessions)

    fmt = get_weekend_format(round_info, calendar)
    if fmt:
        names = []
        for day_sessions in fmt.values():
            if isinstance(day_sessions, list):
                names.extend(s.get("session") for s in day_sessions if s.get("session"))
        return names

    return None


def get_round_motogp_sessions(round_info: dict,
                              calendar: dict = None) -> Optional[List[str]]:
    """
    The MotoGP round session list.

    Kept for callers written before the key was generalised to
    ``<series>_sessions``; new code should use :func:`round_session_list` and
    say which series it means.
    """
    return round_session_list(round_info, calendar, series=SERIES_KEY)


def round_runs_session(round_info: dict, session_type: SessionType,
                       calendar: dict = None, series: str = None) -> bool:
    """
    Does this round run the given session?

    Used to stop the grabber hunting for sessions that will never exist, e.g.
    a MotoGP warm-up from 2027 onwards.

    ``series`` selects whose round session-list to consult (see
    :func:`round_session_list`); it only matters once a second series' rounds
    are passed through this function.
    """
    if not is_round_active(round_info):
        return False

    session_class = session_type.race_class
    if session_class is not None:
        classes = get_round_classes(round_info, calendar)
        if session_class.folder_key not in classes:
            return False

    if session_type == SessionType.UNKNOWN:
        return False

    feature_sessions = round_session_list(round_info, calendar, series=series)
    if feature_sessions is None:
        return True

    # The weekend format describes ONE class: the feature class whose schedule
    # the calendar's weekend_formats block lays out. In MotoGP that is the
    # premier class, and Moto2/Moto3 keep their usual sessions; in F1 it is the
    # only class.
    #
    # Ask the session's OWN series which class that is. This read
    # `session_class != RaceClass.MOTOGP`, which looked right while the spine was
    # MotoGP-only and was wrong the moment an F1 session was passed in: a
    # different enum class is never equal to MotoGP's, so every F1 session took
    # the exemption below and the format gate never ran.
    if session_class is not None and \
            session_class != type(session_type).feature_class():
        return True

    # A session the format has no name for is not described by it at all.
    #
    # This is the difference between the two series' vocabularies, and it is
    # load-bearing for F1: ``f1_sessions`` lists the seven *timed* sessions, so
    # asking "is Ted's Notebook in that list?" answers no for a programme that
    # airs every weekend. F1 wants Ted's Notebook, Jolyon Palmer's Analysis and
    # the pre-race show by default; gating them on a timetable list would stop
    # all three from ever being searched, and the symptom would be "that show
    # never appears", not an error.
    #
    # MotoGP is unaffected: its feature-class members that the format cannot
    # express (the press conference, the chequered flag) are not desired by
    # default, and the members that ARE desired have a format name.
    format_name = session_type.format_session_name
    if format_name is None:
        return True

    return format_name in feature_sessions


def get_active_rounds(calendar: dict) -> List[dict]:
    """Rounds that are still scheduled, in championship order."""
    if not calendar:
        return []
    return [r for r in calendar.get("rounds", []) if is_round_active(r)]


def validate_calendar(calendar: dict) -> List[str]:
    """
    Sanity-check a calendar file. Returns a list of human readable problems.

    Catches the mistakes that are easy to make when hand editing a season after
    a cancellation or reschedule: duplicate or gappy round numbers, folders that
    disagree with the round number, bad dates, missing assets.
    """
    problems: List[str] = []
    if not calendar:
        return ["Calendar is empty or could not be parsed"]

    rounds = calendar.get("rounds", [])
    if not rounds:
        return ["Calendar has no rounds"]

    seen_numbers = set()
    seen_folders = set()
    previous_date = None

    for entry in rounds:
        round_num = entry.get("round")
        venue = entry.get("venue", "?")

        if round_num is None:
            problems.append(f"{venue}: missing 'round'")
            continue

        if round_num in seen_numbers:
            problems.append(f"Duplicate round number {round_num} ({venue})")
        seen_numbers.add(round_num)

        # Round numbers must be unique, but gaps are legal: a cancelled round
        # is normally removed from the calendar, and MotoGP has historically
        # renumbered, so we only warn about the pattern, never fail on it.

        folder = entry.get("folder", "")
        if folder in seen_folders:
            problems.append(f"{venue}: duplicate folder '{folder}'")
        seen_folders.add(folder)

        # The number in the folder must agree with the round. Padding is not
        # enforced here: legacy calendars use "Round 5 Spain" and renaming a
        # finished season's folders would churn the Plex library for nothing.
        if folder:
            match = re.match(r"Round\s*(\d+)\b", folder)
            if not match:
                problems.append(f"{venue}: folder '{folder}' is not 'Round <n> <venue>'")
            elif int(match.group(1)) != round_num:
                problems.append(
                    f"{venue}: folder '{folder}' says round {int(match.group(1))} "
                    f"but the entry is round {round_num}"
                )
            else:
                # Number agrees, so the venue in the folder should too. This is
                # what catches "Round 20 Malaysia" left behind after a round
                # moved: the folder belongs to a different race.
                rest = folder[match.end():].strip()
                if rest and rest.lower() != venue.lower():
                    problems.append(
                        f"{venue}: folder '{folder}' names '{rest}' instead of "
                        f"the venue '{venue}'"
                    )

        date = entry.get("date")
        if not date:
            problems.append(f"{venue}: missing 'date'")
        else:
            try:
                parsed = datetime.strptime(date, "%Y-%m-%d").date()
                if previous_date and parsed < previous_date:
                    problems.append(
                        f"{venue}: date {date} is earlier than the previous round"
                    )
                previous_date = parsed
            except (ValueError, TypeError):
                problems.append(f"{venue}: invalid 'date' {date!r}")

        if not entry.get("sessions") and has_session_times(calendar):
            problems.append(f"{venue}: no 'sessions' block (grabber cannot schedule)")

        if get_round_status(entry) in INACTIVE_ROUND_STATUSES:
            continue

    return problems


# =============================================================================
# BROADCASTER PREFERENCES
# =============================================================================

PREFERRED_BROADCASTERS = {
    "tnt": 40, #was 25
    "bt sport": 20,
    "bt.sport": 20,
    "btsport": 20,
}

# =============================================================================
# RELEASE PARSING AND FILTERING
# =============================================================================

@dataclass
class ReleaseInfo:
    """Parsed information from a release title."""
    title: str
    year: Optional[int] = None
    round_num: Optional[int] = None
    venue: Optional[str] = None
    session_type: SessionType = SessionType.UNKNOWN
    race_class: Optional[RaceClass] = None
    quality: Optional[str] = None
    group: Optional[str] = None
    broadcaster: Optional[str] = None


@dataclass
class ReleaseAnalysis:
    """
    One candidate's verdict from the matcher, with the evidence for it.

    Produced by ``explain_release()`` and, for every candidate, by the
    implementation behind ``filter_releases()``. It exists so that "why was this
    release chosen?" has an answer produced by the *same* code that chose it.
    A hand-written description of the rules would drift the first time a rule
    changed, and then confidently explain the wrong thing.

    ``checks`` is an ordered list of ``{"name", "passed", "detail"}``. Checks
    after the first failure are absent, because they were never reached - which
    is what makes the list readable as a narrative.
    """

    title: str
    accepted: bool
    reason: str = ""
    checks: List[dict] = field(default_factory=list)
    score: Optional[int] = None
    refused: bool = False
    classification: Optional[str] = None
    release: Optional[dict] = None

    def as_dict(self, calendar: dict = None) -> dict:
        """JSON-safe form for the WebUI. Pass the calendar for venue resolution."""
        parsed = parse_release_title(self.title, calendar) if self.title else None
        return {
            "title": self.title,
            "accepted": self.accepted,
            "reason": self.reason,
            "checks": list(self.checks),
            "score": self.score,
            "refused": self.refused,
            "classification": self.classification,
            "parsed": None if parsed is None else {
                "year": parsed.year,
                "round": parsed.round_num,
                "venue": parsed.venue,
                "session": parsed.session_type.name,
                "session_name": parsed.session_type.full_display_name,
                "class": parsed.race_class.name if parsed.race_class else None,
                "quality": parsed.quality,
                "group": parsed.group,
                "broadcaster": parsed.broadcaster,
            },
            "candidate": self.release or {},
        }


def parse_release_title(title: str, calendar: dict = None) -> ReleaseInfo:
    """Parse a release title to extract metadata."""
    info = ReleaseInfo(title=title)
    
    title_lower = title.lower()
    title_normalized = title_lower.replace(".", " ").replace("-", " ")
    
    # Extract year
    year_match = re.search(r'\b(20\d{2})\b', title)
    if year_match:
        info.year = int(year_match.group(1))
    
    # Extract quality
    quality_match = re.search(r'\b(2160p|1080p|1080i|720p|480p)\b', title_lower)
    if quality_match:
        info.quality = quality_match.group(1)
    
    # Extract release group
    group_patterns = [
        r'-(\w+)$',
        r'\[(\w+)\]',
    ]
    for pattern in group_patterns:
        group_match = re.search(pattern, title)
        if group_match:
            info.group = group_match.group(1)
            break
    
    # Extract broadcaster
    for broadcaster in PREFERRED_BROADCASTERS.keys():
        if broadcaster in title_normalized:
            info.broadcaster = broadcaster
            break
    
    # Detect race class
    info.race_class = detect_race_class(title)

    # Identify the round first: the round's own session list is what lets an
    # ambiguous title ("Practice", a bare "FP") be resolved from data.
    round_info = identify_round(title, calendar) if calendar else None
    if round_info:
        info.round_num = round_info.get("round")
        info.venue = round_info.get("venue")
    else:
        info.round_num = extract_round_number(title)

    # Classify session
    info.session_type = classify_session(
        title, info.race_class, round_info=round_info, calendar=calendar
    )

    return info


def _filter_releases_impl(
    releases: List[dict],
    target_session: SessionType,
    season: int,
    round_info: dict,
    required_quality: str = "1080p",
    allowed_groups: List[str] = None,
    calendar: dict = None,
    excluded_titles: Optional[Iterable[str]] = None,
) -> Tuple[List[dict], List["ReleaseAnalysis"]]:
    """
    The body of filter_releases(), with a per-candidate explanation.

    Split out so that ``explain_release()`` can answer "why was this release
    chosen?" from the same rules the matcher actually runs, instead of from a
    second description of them that will drift. Returns ``(accepted, analyses)``;
    ``filter_releases()`` discards the analyses, and the WebUI shows them.
    """
    if allowed_groups is None:
        allowed_groups = ["VERUM", "MWR", "BILLIE"]

    # Mismatches the user has marked. Compared on a normalised title (case,
    # dots and separators removed) but never fuzzier than that: a looser match
    # would block a good release because it resembles a bad one, and the
    # symptom - a session that simply never downloads - is undiagnosable.
    blocked_norm = {
        re.sub(r"\s+", " ", str(t).casefold().replace(".", " ").replace("-", " ")).strip()
        for t in (excluded_titles or ())
        if str(t).strip()
    }
    venue = round_info.get("venue", "").lower()
    aliases = [a.lower() for a in round_info.get("aliases", [])]
    all_locations = [venue] + aliases
    round_num = round_info.get("round", 0)

    target_class = target_session.race_class
    target_session_name = target_session.display_name

    scored = []
    analyses: List["ReleaseAnalysis"] = []

    # Terms to exclude globally
    excluded_terms = [
        "full event", "full.event", "fullevent",
        "weekend", "race weekend",
        "complete weekend", "complete.weekend",
        "all sessions", "all.sessions",
    ]

    # Get logger
    logger = logging.getLogger("motogp.filter")

    logger.debug(f"=" * 60)
    logger.debug(f"FILTER_RELEASES DEBUG")
    logger.debug(f"=" * 60)
    logger.debug(f"Target session: {target_session.full_display_name}")
    logger.debug(f"Target class: {target_class.display_name if target_class else 'None'}")
    logger.debug(f"Target session name: {target_session_name}")
    logger.debug(f"Season: {season}")
    logger.debug(f"Round: {round_num}")
    logger.debug(f"Venue: {venue}")
    logger.debug(f"All locations: {all_locations}")
    logger.debug(f"Required quality: {required_quality}")
    logger.debug(f"Allowed groups: {allowed_groups}")
    logger.debug(f"Total candidates: {len(releases)}")
    logger.debug(f"Blocked (marked mismatch): {len(blocked_norm)} title(s)")
    logger.debug(f"-" * 60)

    for idx, release in enumerate(releases):
        title = release.get('title', '')
        title_lower = title.lower()
        title_normalized = title_lower.replace('.', ' ').replace('-', ' ')
        norm_key = re.sub(r"\s+", " ", title_normalized).strip()

        checks: List[dict] = []
        score = 100

        logger.debug(f"")
        logger.debug(f"[{idx+1}/{len(releases)}] Evaluating: {title}")

        if norm_key in blocked_norm:
            logger.warning(
                f"  🚫 EXCLUDED (marked as a mismatch): {title}"
            )
            analyses.append(ReleaseAnalysis(
                title=title,
                accepted=False,
                reason="marked as a mismatch - it will not be selected again",
                checks=[{"name": "marked_mismatch", "passed": False,
                         "detail": "excluded by the user"}],
                score=None,
                release=release,
            ))
            continue

        # GLOBAL EXCLUSIONS
        excluded_match = None
        for term in excluded_terms:
            if term in title_normalized:
                excluded_match = term
                break

        if excluded_match:
            logger.debug(f"  ❌ REJECTED: Contains excluded term '{excluded_match}'")
            analyses.append(ReleaseAnalysis(
                title=title, accepted=False,
                reason=f"title contains the excluded term {excluded_match!r}",
                checks=[{"name": "global_exclusion", "passed": False,
                         "detail": excluded_match}],
                release=release,
            ))
            continue
        checks.append({"name": "global_exclusion", "passed": True,
                       "detail": "no full-weekend/combined marker"})

        # Must match class
        detected_class = detect_race_class(title)
        logger.debug(f"  Detected class: {detected_class.display_name}")

        if detected_class != target_class:
            logger.debug(f"  ❌ REJECTED: Class mismatch (want {target_class.display_name}, got {detected_class.display_name})")
            analyses.append(ReleaseAnalysis(
                title=title, accepted=False,
                reason=(
                    f"class mismatch: the title reads as "
                    f"{detected_class.display_name}, target is "
                    f"{target_class.display_name}"
                ),
                checks=checks + [{"name": "class", "passed": False,
                                  "detail": detected_class.display_name}],
                release=release,
            ))
            continue

        logger.debug(f"  ✓ Class matches: {detected_class.display_name}")
        checks.append({"name": "class", "passed": True,
                       "detail": detected_class.display_name})

        # Must match location or round number
        location_match = False
        matched_location = None
        for loc in all_locations:
            if loc in title_lower:
                location_match = True
                matched_location = loc
                break

        round_match = False
        matched_round_pattern = None
        if not location_match:
            round_patterns = [
                f"round{round_num:02d}",
                f"round{round_num}",
                f"round.{round_num:02d}",
                f"round.{round_num}",
                f"round {round_num}",
                f"r{round_num:02d}",
                f"r{round_num}",
            ]
            for p in round_patterns:
                if p in title_lower or p in title_normalized:
                    round_match = True
                    matched_round_pattern = p
                    logger.debug(f"  ✓ Round pattern matched: '{p}'")
                    break

        if location_match:
            logger.debug(f"  ✓ Location matched: '{matched_location}'")
            checks.append({"name": "round", "passed": True,
                           "detail": f"venue/alias {matched_location!r} in title"})
        elif round_match:
            logger.debug(f"  ✓ Round number matched")
            checks.append({"name": "round", "passed": True,
                           "detail": f"round pattern {matched_round_pattern!r} in title"})
        else:
            logger.debug(f"  ❌ REJECTED: No location/round match")
            logger.debug(f"     Title lower: {title_lower}")
            logger.debug(f"     Looking for locations: {all_locations}")
            logger.debug(f"     Looking for round patterns: round{round_num}, r{round_num}, etc.")
            analyses.append(ReleaseAnalysis(
                title=title, accepted=False,
                reason=(
                    f"no venue/alias ({', '.join(all_locations)}) or round "
                    f"pattern for Round {round_num} in the title"
                ),
                checks=checks + [{"name": "round", "passed": False,
                                  "detail": f"looked for {all_locations}"}],
                release=release,
            ))
            continue

        # Must have required quality
        quality_base = required_quality.lower().rstrip('pi')
        if quality_base not in title_lower:
            logger.debug(f"  ❌ REJECTED: Missing quality '{quality_base}'")
            analyses.append(ReleaseAnalysis(
                title=title, accepted=False,
                reason=f"missing required quality {quality_base!r}",
                checks=checks + [{"name": "quality", "passed": False,
                                  "detail": f"wanted {quality_base}"}],
                release=release,
            ))
            continue

        logger.debug(f"  ✓ Quality matched: {quality_base}")
        checks.append({"name": "quality", "passed": True, "detail": quality_base})

        # Must be from allowed release group
        group_match = False
        matched_group = None
        for g in allowed_groups:
            if g.lower() in title_lower:
                group_match = True
                matched_group = g
                break

        if not group_match:
            logger.debug(f"  ❌ REJECTED: No allowed group found (looking for {allowed_groups})")
            analyses.append(ReleaseAnalysis(
                title=title, accepted=False,
                reason=f"no allowed release group in the title (allowed: {', '.join(allowed_groups)})",
                checks=checks + [{"name": "group", "passed": False,
                                  "detail": ", ".join(allowed_groups)}],
                release=release,
            ))
            continue

        logger.debug(f"  ✓ Group matched: {matched_group}")
        checks.append({"name": "group", "passed": True, "detail": matched_group})

        # Must match year
        if str(season) not in title:
            logger.debug(f"  ❌ REJECTED: Year {season} not found in title")
            analyses.append(ReleaseAnalysis(
                title=title, accepted=False,
                reason=f"year {season} is not in the title",
                checks=checks + [{"name": "year", "passed": False,
                                  "detail": str(season)}],
                release=release,
            ))
            continue

        logger.debug(f"  ✓ Year matched: {season}")
        checks.append({"name": "year", "passed": True, "detail": str(season)})

        # SESSION MATCHING
        detected_session = classify_session(
            title, detected_class, round_info=round_info, calendar=calendar
        )
        logger.debug(f"  Detected session: {detected_session.display_name} (full: {detected_session.full_display_name})")

        # Refuse rather than guess. An unrecognised title used to fall through
        # to MOTOGP_RACE and was accepted as the Race target, writing a wrong
        # folder name and a wrong Plex identity. Say so loudly and skip it.
        if detected_session == SessionType.UNKNOWN:
            logger.warning(
                f"  ⚠️  REFUSED: could not identify a session in {title!r}; "
                f"not filing it as {target_session.full_display_name}"
            )
            analyses.append(ReleaseAnalysis(
                title=title, accepted=False, refused=True,
                reason=(
                    f"the session could not be identified (refused rather than "
                    f"filed as {target_session.full_display_name})"
                ),
                checks=checks + [{"name": "session", "passed": False,
                                  "detail": "unidentified - refused"}],
                release=release,
                classification=_refusal_reason(title, round_info, calendar),
            ))
            continue

        session_match = False
        is_combined_quali = False

        if detected_session == target_session:
            session_match = True
            logger.debug(f"  ✓ Session exact match")
        elif target_session_name in ["Qualifying 1", "Qualifying 2"]:
            target_is_q1 = target_session_name == "Qualifying 1"
            target_is_q2 = target_session_name == "Qualifying 2"

            has_q1 = matches_qualifying_part(title, 1)
            has_q2 = matches_qualifying_part(title, 2)

            if target_is_q1 and has_q1:
                session_match = True
                logger.debug(f"  ✓ Q1 pattern matched")
            elif target_is_q2 and has_q2:
                session_match = True
                logger.debug(f"  ✓ Q2 pattern matched")
            elif not has_q1 and not has_q2:
                if "qualifying" in title_normalized or "qualifing" in title_normalized:
                    session_match = True
                    is_combined_quali = True
                    release['_combined_quali'] = True
                    logger.debug(f"  ✓ Combined qualifying matched")
        else:
            # For non-qualifying sessions, check if detected matches target
            logger.debug(f"  Comparing: detected={detected_session.name} vs target={target_session.name}")

        if not session_match:
            logger.debug(f"  ❌ REJECTED: Session mismatch")
            logger.debug(f"     Want: {target_session.full_display_name} ({target_session.name})")
            logger.debug(f"     Got:  {detected_session.full_display_name} ({detected_session.name})")
            analyses.append(ReleaseAnalysis(
                title=title, accepted=False,
                reason=(
                    f"session mismatch: the title resolves to "
                    f"{detected_session.full_display_name}, target is "
                    f"{target_session.full_display_name}"
                ),
                checks=checks + [{
                    "name": "session", "passed": False,
                    "detail": f"classifier said {detected_session.full_display_name}",
                }],
                release=release,
            ))
            continue

        # PASSED ALL FILTERS!
        logger.debug(f"  ✅ ACCEPTED!")

        # Scoring
        if detected_session == target_session:
            score += 25
        elif is_combined_quali:
            score += 10

        broadcaster_score = 0
        for broadcaster, bonus in PREFERRED_BROADCASTERS.items():
            if broadcaster in title_normalized:
                broadcaster_score = bonus
                release['_broadcaster'] = broadcaster
                break
        score += broadcaster_score

        if "verum" in title_lower:
            score += 20
            release['_group'] = 'VERUM'
        elif "mwr" in title_lower:
            score += 40
            release['_group'] = 'MWR'
        elif "billie" in title_lower:
            score += 40
            release['_group'] = 'BILLIE'

        if "1080p" in title_lower:
            score += 10
        elif "2160p" in title_lower:
            score += 5

        release['_score'] = score
        release['_broadcaster_score'] = broadcaster_score
        scored.append(release)

        checks.append({"name": "session", "passed": True,
                       "detail": detected_session.full_display_name})
        if is_combined_quali:
            checks.append({"name": "combined_qualifying", "passed": True,
                           "detail": "one release covers Q1 and Q2"})
        analyses.append(ReleaseAnalysis(
            title=title, accepted=True, reason="passed every filter",
            checks=checks, score=score, release=release,
        ))

        logger.debug(f"  Score: {score}")

    logger.debug(f"")
    logger.debug(f"=" * 60)
    logger.debug(f"FILTER SUMMARY: {len(scored)}/{len(releases)} releases passed")
    logger.debug(f"=" * 60)

    scored.sort(key=lambda x: x.get('_score', 0), reverse=True)

    return scored, analyses


def filter_releases(
    releases: List[dict],
    target_session: SessionType,
    season: int,
    round_info: dict,
    required_quality: str = "1080p",
    allowed_groups: List[str] = None,
    calendar: dict = None,
    excluded_titles: Optional[Iterable[str]] = None,
) -> List[dict]:
    """
    Filter and score releases for a target session.

    ``excluded_titles`` names releases the user has marked as a mismatch (see
    utils/release_mismatches.py). They are dropped before scoring, so a
    replacement request for the same session cannot pick the same wrong
    release again - which is the whole point of tracking them. The exclusion is
    logged at WARNING rather than DEBUG: a user who marked a release needs to
    see that the mark took effect, and "the release I blocked was considered and
    skipped" is otherwise indistinguishable from "it was never indexed".
    """
    scored, _ = _filter_releases_impl(
        releases, target_session, season, round_info,
        required_quality, allowed_groups, calendar,
        excluded_titles=excluded_titles,
    )
    return scored


def explain_release(
    title: str,
    target_session: SessionType,
    season: int,
    round_info: dict,
    required_quality: str = "1080p",
    allowed_groups: List[str] = None,
    calendar: dict = None,
    excluded_titles: Optional[Iterable[str]] = None,
) -> "ReleaseAnalysis":
    """
    Explain how the matcher treats one release title for one target.

    This exists because a wrong download has to be understandable before it can
    be fixed. It runs the real filter over a single candidate and returns every
    check it performed, in order, with the evidence - venue alias matched, round
    pattern matched, the session the classifier resolved, and the score that
    decided the winner. The alternative, re-deriving the decision in the WebUI,
    would answer with a second implementation that drifts from the first.
    """
    candidate = {"title": title}
    _, analyses = _filter_releases_impl(
        [candidate], target_session, season, round_info,
        required_quality, allowed_groups, calendar,
        excluded_titles=excluded_titles,
    )
    for analysis in analyses:
        if analysis.title == title:
            return analysis
    # Every candidate produces exactly one analysis; this is unreachable
    # unless that changes, but a missing explanation must not be silent.
    return ReleaseAnalysis(
        title=title, accepted=False,
        reason="the matcher produced no decision for this title",
    )


# =============================================================================
# VENUE/COUNTRY MAPPING
# =============================================================================

VENUE_COUNTRY_MAP = {
    "Thailand": "Thailand",
    "Argentina": "Argentina",
    "Americas": "United States",
    "Qatar": "Qatar",
    "Spain": "Spain",
    "Jerez": "Spain",
    "France": "France",
    "Great Britain": "United Kingdom",
    "Aragon": "Spain",
    "Italy": "Italy",
    "Mugello": "Italy",
    "Misano": "Italy",
    "Netherlands": "Netherlands",
    "Assen": "Netherlands",
    "Germany": "Germany",
    "Sachsenring": "Germany",
    "Czech Republic": "Czech Republic",
    "Austria": "Austria",
    "Spielberg": "Austria",
    "Hungary": "Hungary",
    "Catalunya": "Spain",
    "Barcelona": "Spain",
    "San Marino": "San Marino",
    "Japan": "Japan",
    "Motegi": "Japan",
    "Indonesia": "Indonesia",
    "Mandalika": "Indonesia",
    "Australia": "Australia",
    "Phillip Island": "Australia",
    "Malaysia": "Malaysia",
    "Sepang": "Malaysia",
    "Portugal": "Portugal",
    "Portimao": "Portugal",
    "Valencia": "Spain",
    "Brazil": "Brazil",
}


def get_country_for_venue(venue: str) -> Optional[str]:
    """Get country name for a venue."""
    return VENUE_COUNTRY_MAP.get(venue)

# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="MotoGP Common Module - Test utilities",
    )
    
    parser.add_argument('--test-inline', metavar='TITLE', help='Test single title')
    parser.add_argument('--season', type=int, help='Season year for testing')
    parser.add_argument('--list-sessions', action='store_true', help='List all session types')
    parser.add_argument(
        '--report-unknown', action='store_true',
        help='Dry run: read release titles from stdin (one per line) and '
             'report the session each resolves to, or why it is refused'
    )

    args = parser.parse_args()

    if args.report_unknown:
        calendar = load_calendar(args.season) if args.season else None
        if args.season and not calendar:
            print(f"❌ No calendar for season {args.season}", file=sys.stderr)
            return 1
        print(f"Release classification report"
              f"{f' for {args.season}' if args.season else ''}")
        print("-" * 72)
        for line in sys.stdin:
            title = line.strip()
            if not title:
                continue
            result = classify_release(title, calendar, args.season)
            if result.session_type == SessionType.UNKNOWN:
                print(f"REFUSED  {title}")
                print(f"         reason: {result.reason}")
            else:
                round_label = (
                    f"Round {result.round_num:02d} {result.venue}"
                    if result.round_num else "round unknown"
                )
                print(f"{result.session_type.full_display_name:<24} {title}")
                print(f"         {round_label}")
        return 0

    if args.list_sessions:
        print("\nSESSION TYPES\n" + "=" * 50)
        current_class = None
        for session in SessionType:
            if session == SessionType.UNKNOWN:
                continue
            race_class = session.race_class
            if race_class != current_class:
                current_class = race_class
                if race_class:
                    print(f"\n{race_class.display_name}:")
            core = "⭐" if session.is_core_session else "  "
            feature = "🏁" if session.is_feature_race else "  "
            # The element number comes from SESSION_ELEMENTS, not the enum
            # value: the two diverged when the numbering moved to blocks of
            # ten, and printing int(session) here would repeat the mistake the
            # folder-naming code exists to avoid.
            print(f"  {core}{feature} E{element_number(session, 2026):02d} - {session.display_name}")
    
    elif args.test_inline:
        title = args.test_inline
        print(f"\nTitle: {title}")
        calendar = load_calendar(args.season) if args.season else None
        result = classify_release(title, calendar, args.season)
        race_class = result.race_class
        session = result.session_type
        print(f"  Class:   {race_class.display_name}")
        print(f"  Session: {session.display_name}")
        print(f"  Full:    {session.full_display_name}")
        if session == SessionType.UNKNOWN:
            print(f"  Refused: {result.reason}")
        else:
            print(f"  Element: E{element_number(session, args.season or 2026):02d}")
            if result.round_num:
                print(f"  Round:   {result.round_num} - {result.venue}")
                if args.season:
                    prefix = build_prefix(args.season, result.round_num, session)
                    print(f"  Prefix:  {prefix}")
    else:
        parser.print_help()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())