#!/usr/bin/env python3
"""
Release mismatches
==================

Sometimes the matcher is wrong. It picks a release that passes every rule -
right class, right round, right quality, a session token it recognises - and the
file inside is not the session that was wanted. Plex shows the wrong thing, and
the obvious response, "download it again", does not work: the same rules see the
same candidates and pick the same release again.

This module is the memory that breaks that loop. A release marked as a mismatch
for a session is refused by ``motogp_common.filter_releases()`` for that session
from then on, which is what makes a replacement request behave differently from
the first attempt. The entry keeps the evidence - the release title the matcher
saw, what the matcher concluded about it, and when - so the patterns can be
corrected afterwards instead of guessed at.

Design decisions that are load-bearing:

* **Session-scoped by default.** A release is normally wrong *relative to a
  target*: a Round 07 title that is really Round 08 is wrong for both rounds
  (use the global scope), but a Race release mis-filed as Practice is only wrong
  for the Practice target. Global blocking is explicit, never the default.
* **Exact title matching after normalisation, and nothing fuzzier.** Case,
  dots, underscores and hyphens are normalised away. Anything looser would block
  a good release because it resembles a bad one, and the symptom - a download
  that simply never happens - is impossible to diagnose from the UI.
* **The wrong file is moved, not deleted.** :func:`quarantine_library_session`
  renames the existing folder into ``<library>/.mismatched/<season>/``. Plex
  ignores dot-directories, every scanner in this app ignores the name, and the
  media is still on disk if it is wanted after all. Deleting media from a web
  request is not a thing this app does outside ``scripts/cleanup_utility.py``,
  which is deliberately absent from the image.
* **This is shared state.** The WebUI writes it and the grabber reads it, in
  separate processes. All mutations therefore take a sidecar lock and replace
  the file atomically, which is the persistence contract
  ``docs/IMPROVEMENT_PLAN.md`` asks for. The file is tiny, so getting it right
  costs nothing.
* **A corrupt file must not be silent.** Treating unreadable state as "no known
  mismatches" would let the next search pick the same bad release again, so the
  failure is logged at error level naming the path and the consequence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from utils.state_files import state_path
from utils.json_store import atomic_write_json as json_store_atomic_write
from utils.json_store import file_lock as json_store_file_lock

logger = logging.getLogger("motogparr.mismatches")

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
MISMATCH_FILE = state_path(CONFIG_DIR, "release_mismatches")
LOCK_FILE = Path(str(MISMATCH_FILE)[:-len(".json")] + ".lock")

STATE_VERSION = 1

#: Wrong for one session only (the default), or wrong wherever it appears.
SCOPE_SESSION = "session"
SCOPE_GLOBAL = "global"
VALID_SCOPES = (SCOPE_SESSION, SCOPE_GLOBAL)

#: Where a mismatched library folder is parked. The leading dot is the whole
#: point: Plex's scanner skips dot-directories, and so does every folder walk in
#: this app (they all match either ``Round NN`` or a ``YYRREE`` prefix).
QUARANTINE_DIRNAME = ".mismatched"

#: Titles are capped so a pathological payload cannot bloat the state file.
MAX_TITLE_LENGTH = 512


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

def normalise_title(title: str) -> str:
    """
    Canonical form used for comparing release titles.

    Indexers disagree about separators: ``MotoGP.2026.07.Italy.Race`` and
    ``MotoGP 2026 07 Italy Race`` are the same release. Case, dots,
    underscores, hyphens and runs of whitespace are all normalised away. Word
    order and every other token are kept, because two titles that differ in
    content must remain different.
    """
    text = str(title or "").casefold()
    text = re.sub(r"[._\-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_combined_suffix(title: str) -> str:
    """
    Remove the ``" (combined)"`` marker the grabber appends for Q1/Q2.

    When one release covers both qualifying sessions, the grabber records the
    second session key with that suffix. It is a local annotation, not part of
    the release name, so recording it in a blocklist would produce an entry that
    can never match a real candidate.
    """
    text = str(title or "").strip()
    suffix = " (combined)"
    if text.casefold().endswith(suffix):
        return text[: -len(suffix)].strip()
    return text


def entry_id(scope: str, season: int, round_num: int, session: str,
             title: str) -> str:
    """
    Stable identifier for a (scope, season, round, session, title) tuple.

    Deterministic on purpose: marking the same mismatch twice updates the entry
    instead of appending a duplicate, so the list cannot fill with copies of the
    same decision.
    """
    raw = "|".join([
        str(scope or SCOPE_SESSION).casefold(),
        str(season),
        str(round_num),
        str(session).upper(),
        normalise_title(title),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@contextmanager
def _locked(path: Path):
    """
    Exclusive lock on a sidecar file.

    Delegates to ``utils.json_store.file_lock`` (which this module's pattern
    was generalised into): the lock is a separate file, not the data file and
    not a temporary file, so it stays valid while the data file is replaced.
    Holding it across the whole read-modify-write is what prevents two writers
    from dropping each other's entries - locking only the write phase would
    keep the file syntactically valid while silently losing a mismatch, which
    is the failure mode that makes a replacement download useless.
    """
    with json_store_file_lock(Path(path)):
        yield


def load_entries(path: Optional[Path] = None) -> List[dict]:
    """
    Read every mismatch entry, tolerating a missing or unreadable file.

    A parse failure is logged at ERROR: returning an empty list is the only
    sensible answer for a search (a grabber must not die because of a state
    file), but it means blocking is OFF, and the next search may pick the same
    release again. Saying that out loud is the difference between a five minute
    fix and an evening of "why did it download that again".
    """
    path = Path(path or MISMATCH_FILE)

    if not path.exists():
        return []

    try:
        with open(path, "r") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(
            "Could not read release mismatches from %s: %s. "
            "Marked releases are NOT being excluded until this file is fixed "
            "or removed.",
            path,
            exc,
        )
        return []

    if isinstance(data, dict):
        raw = data.get("entries", [])
    elif isinstance(data, list):
        raw = data
    else:
        raw = []

    entries = []
    for item in raw:
        if isinstance(item, dict) and item.get("title"):
            entries.append(item)
    return entries


def _write(entries: Sequence[dict], path: Path) -> None:
    """Atomically replace the state file. Caller holds the lock."""
    payload = {"version": STATE_VERSION, "entries": list(entries)}
    json_store_atomic_write(Path(path), payload)


def add_mismatch(
    season: int,
    round_num: int,
    session: str,
    title: str,
    reason: str = "",
    scope: str = SCOPE_SESSION,
    venue: Optional[str] = None,
    sab_job_name: Optional[str] = None,
    grabbed_at: Optional[str] = None,
    release_info: Optional[dict] = None,
    analysis: Optional[dict] = None,
    library_action: Optional[dict] = None,
    source: str = "webui",
    path: Optional[Path] = None,
) -> dict:
    """
    Record (or refresh) one mismatch and return the stored entry.

    ``title`` is the release name the matcher saw. Everything else is evidence
    for the operator: what the matching logic concluded, what the grabber had
    recorded, and what happened to the file in the library.
    """
    title = str(title or "").strip()[:MAX_TITLE_LENGTH]
    if not title:
        raise ValueError("a release title is required to block a release")

    scope = str(scope or SCOPE_SESSION).strip().lower()
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope must be one of {', '.join(VALID_SCOPES)}")

    season = int(season)
    round_num = int(round_num)
    session = str(session).upper()
    store_path = Path(path or MISMATCH_FILE)

    ident = entry_id(scope, season, round_num, session, title)
    now = datetime.now().isoformat(timespec="seconds")

    with _locked(store_path):
        entries = load_entries(store_path)
        existing = next((e for e in entries if e.get("id") == ident), None)

        entry = {
            "id": ident,
            "scope": scope,
            "season": season,
            "round": round_num,
            "session": session,
            "venue": venue,
            "title": title,
            "normalised_title": normalise_title(title),
            "reason": str(reason or "").strip(),
            "source": source,
            "recorded_at": (existing or {}).get("recorded_at") or now,
            "updated_at": now,
            "sab_job_name": sab_job_name or (existing or {}).get("sab_job_name"),
            "grabbed_at": grabbed_at or (existing or {}).get("grabbed_at"),
            "release": release_info or (existing or {}).get("release"),
            "analysis": analysis if analysis is not None
            else (existing or {}).get("analysis"),
            "library_action": library_action if library_action is not None
            else (existing or {}).get("library_action"),
        }

        if existing is None:
            entries.append(entry)
        else:
            entries = [entry if e.get("id") == ident else e for e in entries]

        _write(entries, store_path)

    logger.warning(
        "🚫 Release marked as a mismatch: %s (Season %s R%s %s, %s scope). "
        "It will not be selected for this session again.",
        title,
        season,
        round_num,
        session,
        scope,
    )
    return entry


def remove_mismatch(ident: str, path: Optional[Path] = None) -> bool:
    """Forget one mismatch. Returns True if it existed."""
    store_path = Path(path or MISMATCH_FILE)

    with _locked(store_path):
        entries = load_entries(store_path)
        remaining = [e for e in entries if e.get("id") != ident]
        if len(remaining) == len(entries):
            return False
        _write(remaining, store_path)

    logger.info("Release unblocked (mismatch entry %s removed)", ident)
    return True


def find_entry(ident: str, path: Optional[Path] = None) -> Optional[dict]:
    return next((e for e in load_entries(path) if e.get("id") == ident), None)


def describe_session(session: str, series: Optional[str] = None) -> str:
    """
    Human label for a stored ``SessionType`` name ("MotoGP Race").

    Stored names have to be the enum name because that is what the matcher
    compares; the UI should not make a user read ``MOTOGP_SPRINT_QUALIFYING``.
    Falls back to the raw string so an entry written by a future version is
    still shown rather than hidden.

    ``series`` names whose vocabulary to read the label from. The WebUI shows
    both championships' logs from one process, and the two enums contain
    overlapping member names (``RACE``) that mean different sessions - so the
    series is required to be sure the label is the right one, and ``None``
    keeps the process binding for existing callers.
    """
    try:
        if series is None:
            from motogp_common import SessionType
        else:
            from series import runtime

            SessionType = runtime(series).session_type

        return SessionType[str(session).upper()].full_display_name
    except (KeyError, ImportError, AttributeError, ValueError):
        return str(session)


# ---------------------------------------------------------------------------
# Queries used by the matcher and the UI
# ---------------------------------------------------------------------------

def mismatch_applies(entry: dict, season: int, round_num: int,
                     session: str) -> bool:
    """Does this entry block this release for this target?"""
    if entry.get("scope") == SCOPE_GLOBAL:
        return True
    try:
        same_season = int(entry.get("season")) == int(season)
        same_round = int(entry.get("round")) == int(round_num)
    except (TypeError, ValueError):
        return False
    return (
        same_season
        and same_round
        and str(entry.get("session", "")).upper() == str(session).upper()
    )


def blocked_titles(
    season: int,
    round_num: int,
    session: str,
    entries: Optional[Iterable[dict]] = None,
) -> Set[str]:
    """
    Normalised titles that must not be selected for this target.

    Global entries apply to every target; session entries apply to exactly one
    (season, round, session). The normalisation is the same one the matcher
    uses, so a release cannot slip through because an indexer changed a dot to
    a space.
    """
    source = load_entries() if entries is None else entries
    blocked = set()
    for entry in source:
        if not isinstance(entry, dict) or not entry.get("title"):
            continue
        if mismatch_applies(entry, season, round_num, session):
            blocked.add(normalise_title(entry["title"]))
    return blocked


def mismatches_for(
    season: int,
    round_num: int,
    session: str,
    entries: Optional[Iterable[dict]] = None,
) -> List[dict]:
    """Entries that apply to one target, newest first."""
    source = load_entries() if entries is None else entries
    found = [e for e in source if mismatch_applies(e, season, round_num, session)]
    return sorted(found, key=lambda e: e.get("recorded_at") or "", reverse=True)


def counts_by_session(entries: Optional[Iterable[dict]] = None) -> Dict[tuple, int]:
    """
    ``{(season, round, session): count}`` for the Download tab badges.

    Global entries are counted against every target they could affect, which is
    the honest answer: the user needs to know a release is blocked there.
    """
    source = load_entries() if entries is None else entries
    counts: Dict[tuple, int] = {}
    for entry in source:
        if not isinstance(entry, dict):
            continue
        if entry.get("scope") == SCOPE_GLOBAL:
            # No target to attribute it to; expose it separately.
            counts[("global",)] = counts.get(("global",), 0) + 1
            continue
        try:
            key = (
                int(entry.get("season")),
                int(entry.get("round")),
                str(entry.get("session", "")).upper(),
            )
        except (TypeError, ValueError):
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# The library folder
# ---------------------------------------------------------------------------

def library_session_path(
    library_root: str,
    season: int,
    round_num: int,
    prefix: str,
) -> Optional[Path]:
    """
    Locate the library folder for one session, or None.

    The prefix (``YYRREE``) is the identity: round folders are matched by their
    leading ``Round NN`` and the session folder by the exact prefix followed by
    a space. Requiring the space is not pedantry - ``26R07E82`` is a prefix of
    ``26R07E820``, and matching that would find the wrong session.
    """
    season_dir = Path(library_root) / str(season)
    if not season_dir.is_dir():
        return None

    for round_dir in sorted(season_dir.iterdir()):
        if not round_dir.is_dir():
            continue
        match = re.match(r"Round\s*(\d+)", round_dir.name)
        if not match or int(match.group(1)) != int(round_num):
            continue

        for child in sorted(round_dir.iterdir()):
            if not child.is_dir():
                continue
            if child.name == prefix or child.name.startswith(prefix + " "):
                return child
    return None


def _count_video_files(folder: Path) -> int:
    extensions = {".mkv", ".mp4", ".avi", ".m4v", ".wmv", ".ts", ".m2ts"}
    total = 0
    try:
        for item in folder.rglob("*"):
            if item.is_file() and item.suffix.lower() in extensions:
                total += 1
    except OSError:
        pass
    return total


def quarantine_library_session(
    library_root: str,
    season: int,
    round_num: int,
    prefix: str,
) -> Dict[str, Any]:
    """
    Move the existing session folder out of the way, without deleting it.

    A replacement download must not be sorted into the folder that is already
    there: ``shutil.move(source_dir, existing_dir)`` nests the new folder inside
    the old one, Plex then shows the wrong file (or two items), and nothing has
    visibly failed. Moving the old one aside first makes the sort a clean
    one-to-one replacement.

    Returns ``{"ok": bool, "reason": str, "from": str, "to": str, "video_files":
    int}``. Every guard here exists to make "wrong folder removed" impossible:
    the folder must be under the configured library root after resolving
    symlinks, and it must be a directory this module found by prefix, not a path
    the caller assembled.
    """
    result: Dict[str, Any] = {
        "ok": False,
        "reason": "",
        "from": None,
        "to": None,
        "video_files": 0,
    }

    root = Path(library_root)
    if not root.is_dir():
        result["reason"] = f"library path does not exist: {root}"
        return result

    target = library_session_path(library_root, season, round_num, prefix)
    if target is None:
        result["reason"] = "the session is not in the library"
        return result

    if target.is_symlink():
        result["reason"] = f"refusing to move a symlinked folder: {target}"
        logger.error(result["reason"])
        return result

    try:
        resolved_root = root.resolve()
        target.resolve().relative_to(resolved_root)
    except ValueError:
        result["reason"] = f"refusing to move a folder outside the library: {target}"
        logger.error(result["reason"])
        return result
    except OSError as exc:
        result["reason"] = f"could not resolve {target}: {exc}"
        return result

    quarantine_root = resolved_root / QUARANTINE_DIRNAME / str(season)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = quarantine_root / f"{target.name} [{stamp}]"

    video_files = _count_video_files(target)

    try:
        quarantine_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(destination))
    except OSError as exc:
        result["reason"] = f"could not move {target}: {exc}"
        logger.error(result["reason"])
        return result

    logger.warning(
        "📦 Mismatched library folder moved aside: %s -> %s (%d video file(s)). "
        "It is hidden from Plex by the leading dot in '%s'; delete it when the "
        "replacement looks right.",
        target,
        destination,
        video_files,
        QUARANTINE_DIRNAME,
    )

    result.update({
        "ok": True,
        "from": str(target),
        "to": str(destination),
        "video_files": video_files,
    })
    return result
