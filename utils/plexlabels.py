#!/usr/bin/env python3
"""
Plex titles and sort titles for session items
=============================================

Two different things have to be true of a session in Plex at the same time:

1. **The visible name must say what the session is.** After the 2027 format
   change the Friday session is FP2, not the old one-hour Practice. A user who
   sees "Practice" is looking at a session that no longer exists, so the title
   is the short, canonical label ("MotoGP FP2", "Moto3 Q1") taken from
   `SessionType.short_name` - never from whatever the release happened to be
   called.

2. **It must still sort in running order.** An "Other Videos" library sorts by
   title, and the folder name carries `YYRREE` (season, round, element), which
   is already chronological because element numbers are allocated in spaced
   blocks. So the sort key is the same token as the folder name: it is stable,
   it is what is on disk, and it needs no second source of truth.

Doing (1) alone would make ordering *worse* - every "MotoGP FP2" in the season
would clump together - so `title` and `titleSort` are always written together.

Why the on-disk folder name is also short: Plex re-derives an item's title from
its filename whenever the item is re-scanned. If the folder said
"26R20E32 MotoGP Free Practice 2" and Plex said "MotoGP FP2", the next library
scan would quietly put the long name back. Keeping the two identical means a
rescan is a no-op.

Everything here is best-effort and read-only unless asked to write:
`apply_labels` returns the changes it made so the caller can log them, and a
Plex object type that does not support field edits is reported rather than
half-applied.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from motogp_common import SessionType, build_prefix

logger = logging.getLogger("msportarr.plexlabels")

#: Field name Plex uses for the sort title. `title` and `titleSort` are the pair
#: that matters; setting one without the other is what causes the clumping
#: described above.
SORT_FIELD = "titleSort"


def sort_title_for(session_type: SessionType, season: int, round_num: int) -> str:
    """
    The sort key for an item: the same YYRREE prefix as its folder.

    Matching the folder exactly means the value is derivable, visible on disk,
    and cannot drift from the numbering scheme - if the prefix scheme changes,
    this changes with it.
    """
    return build_prefix(season, round_num, session_type)


def labels_for(session_type: SessionType, season: int, round_num: int) -> Dict[str, str]:
    """The (title, titleSort) pair to write for one session item."""
    return {
        "title": session_type.full_short_name,
        SORT_FIELD: sort_title_for(session_type, season, round_num),
    }


def supports_field_edits(video: Any) -> bool:
    """
    Can this Plex object have its title edited?

    An "Other Videos" section can hand back a base `Video`, which carries the
    `title`/`titleSort` attributes but none of the edit mixins. Checking is
    cheap; guessing leads to an AttributeError halfway through a tagging run.
    """
    return hasattr(video, "editField") and hasattr(video, SORT_FIELD)


def apply_labels(video: Any, session_type: SessionType, season: int,
                 round_num: int, dry_run: bool = False) -> List[str]:
    """
    Set the visible title and sort title on one Plex item.

    Returns a list of human-readable changes. `dry_run` reports what it would do
    without touching Plex, so a full-library pass can be reviewed first.
    """
    wanted = labels_for(session_type, season, round_num)
    changes: List[str] = []

    if not supports_field_edits(video):
        logger.warning(
            "  ⚠️  This Plex item does not support title edits "
            f"(type={getattr(video, 'type', type(video).__name__)}). "
            "Titles and sort titles are left alone; see /diagnostics."
        )
        return changes

    current_title = getattr(video, "title", None)
    current_sort = getattr(video, SORT_FIELD, None)

    title_changed = current_title != wanted["title"]
    sort_changed = current_sort != wanted[SORT_FIELD]

    if not (title_changed or sort_changed):
        return changes

    if dry_run:
        if title_changed:
            changes.append(f"[DRY-RUN] Title: {current_title!r} -> {wanted['title']!r}")
        if sort_changed:
            changes.append(
                f"[DRY-RUN] Sort title: {current_sort!r} -> {wanted[SORT_FIELD]!r}"
            )
        return changes

    try:
        # Locked: a user pinning the sort order is not a thing here, and leaving
        # it unlocked lets a Plex metadata refresh overwrite the running order.
        if title_changed:
            video.editField("title", wanted["title"])
            changes.append(f"Title: {wanted['title']!r}")
        if sort_changed:
            video.editField(SORT_FIELD, wanted[SORT_FIELD])
            changes.append(f"Sort title: {wanted[SORT_FIELD]!r}")
    except Exception as exc:  # noqa: BLE001 - tagging must not abort the pipeline
        logger.warning(f"  ⚠️  Could not set Plex labels: {exc}")

    return changes


def collection_sort_title(season: int, round_num: int) -> str:
    """
    Sort key for a venue collection: "2026-04".

    The collection *name* ("2026 Round 04 Qatar") already sorts correctly, so
    this only matters if a name is ever changed or a client sorts differently.
    Setting it costs one lookup and removes a way for the order to drift.
    """
    return f"{season}-{round_num:02d}"


def ensure_collection_sort(plex: Any, library_name: str, collection_name: str,
                           season: int, round_num: int,
                           dry_run: bool = False) -> Optional[str]:
    """
    Set the sort title on a venue collection. Never creates, never raises.

    Called from the tagger after the collection has been applied to the item.
    Everything here is best-effort: the collection exists with a correct,
    zero-padded name, so a failure only risks the ordering hint, not the data.
    """
    wanted = collection_sort_title(season, round_num)
    try:
        section = plex.library.section(library_name)
        collection = section.collection(collection_name)
    except Exception as exc:  # noqa: BLE001 - not found, or Plex has no collections
        logger.debug(f"  Collection sort title skipped for {collection_name!r}: {exc}")
        return None

    if collection is None or getattr(collection, SORT_FIELD, None) == wanted:
        return None

    if dry_run:
        return f"[DRY-RUN] Collection sort title: {wanted}"

    try:
        collection.editSortTitle(wanted)
        return f"Collection sort title: {wanted}"
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"  Could not set collection sort title: {exc}")
        return None


def probe_section(plex: Any, library_name: str) -> Dict[str, Any]:
    """
    Report what an "Other Videos" section actually hands back.

    The title/sort behaviour depends on the object type Plex returns, which
    cannot be determined without a live server. This is read-only, invents
    nothing, and says "unknown" rather than guessing - the same rule the Plex
    library check follows.
    """
    result: Dict[str, Any] = {
        "library": library_name,
        "section_type": None,
        "item_type": None,
        "supports_title_edits": None,
        "sample": None,
        "note": None,
    }

    try:
        section = plex.library.section(library_name)
    except Exception as exc:  # noqa: BLE001 - unreachable Plex is a normal state
        result["note"] = f"Could not open library {library_name!r}: {exc}"
        return result

    result["section_type"] = getattr(section, "TYPE", None)

    try:
        items = section.all()
    except Exception as exc:  # noqa: BLE001
        result["note"] = f"Could not list library contents: {exc}"
        return result

    if not items:
        result["note"] = "Library is empty; add a session and re-run the check."
        return result

    item = items[0]
    result["item_type"] = getattr(item, "type", type(item).__name__)
    result["supports_title_edits"] = supports_field_edits(item)
    result["sample"] = {
        "title": getattr(item, "title", None),
        SORT_FIELD: getattr(item, SORT_FIELD, None),
    }

    if not result["supports_title_edits"]:
        result["note"] = (
            "This item type cannot be edited by this client, so titles and sort "
            "titles are skipped. Session ordering falls back to the filename."
        )

    return result
