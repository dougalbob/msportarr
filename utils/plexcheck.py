#!/usr/bin/env python3
"""
Plex library check
==================

Answers one question: **is Plex actually scanning the folder this app writes to?**

That question sounds obvious and is almost impossible to answer by looking at the
app, which is why it cost real time. The failure mode is completely silent:

  * the download succeeds,
  * the sort succeeds,
  * the file lands in the right place on disk,
  * and Plex never sees it, because the library's own path list does not include
    that folder - or includes it under a different prefix than the app assumes.

Nothing in the log looks wrong, because nothing in this app *is* wrong. The
session simply never appears, and the search starts for a bug that is not there.

The check is deliberately read-only and defensive: it never refreshes a library,
never triggers a scan, and reports "unknown" rather than guessing when Plex is
unreachable. A diagnostic that invents an answer is worse than no diagnostic.

Path translation is the subtle part. Plex reports paths as *Plex* sees them, so
the comparison is made in Plex's namespace:

    app path                   /media/Motorsport/MotoGP/2026/...
    plex.path_mapping          container /media  ->  plex /data/media
    translated                 /data/media/Motorsport/MotoGP/2026/...
    plex section locations     [/data/media/Motorsport/MotoGP]

Without a mapping the two namespaces are incomparable, and the report says so
rather than reporting a false mismatch.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("msportarr.plexcheck")

OK = "ok"
NOT_SCANNED = "not_scanned"
MAPPING_MISSING = "mapping_missing"
TMDB_UNMATCHED = "configuration"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"


@dataclass
class LibraryCheck:
    """Result of comparing the configured library path with Plex's view."""

    status: str
    summary: str
    detail: List[str] = field(default_factory=list)
    app_path: Optional[str] = None
    plex_path: Optional[str] = None
    section_locations: List[str] = field(default_factory=list)
    mapping_used: bool = False

    @property
    def ok(self) -> bool:
        return self.status == OK

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "summary": self.summary,
            "detail": self.detail,
            "app_path": self.app_path,
            "plex_path": self.plex_path,
            "section_locations": self.section_locations,
            "mapping_used": self.mapping_used,
        }


def _normalise(path: str) -> str:
    """Compare paths without caring about trailing slashes or duplicate ones."""
    if not path:
        return ""
    cleaned = os.path.normpath(str(path).replace("\\", "/"))
    return "/" + cleaned.strip("/") if not cleaned.startswith("/") else cleaned


def is_inside(child: str, parent: str) -> bool:
    """
    True when ``child`` is ``parent`` or beneath it.

    Uses a real path-prefix comparison rather than ``str.startswith`` so that
    ``/media/MotoGP2`` is not considered inside ``/media/MotoGP``. Getting that
    wrong here would produce exactly the false confidence this check exists to
    remove.
    """
    child_n, parent_n = _normalise(child), _normalise(parent)
    if not child_n or not parent_n:
        return False
    if child_n == parent_n:
        return True
    return child_n.startswith(parent_n.rstrip("/") + "/")


def check_library(
    plex: Any,
    app_library_path: Optional[str],
    mapping: Optional[Dict[str, Optional[str]]] = None,
    library_name: Optional[str] = None,
    library_id: Optional[str] = None,
) -> LibraryCheck:
    """
    Compare the app's library path with what the Plex library actually scans.

    ``plex`` is anything with ``.library.section(name)`` and a ``.library.sections()``
    method - i.e. a ``plexapi.server.PlexServer``, or a stub in tests. It is
    injected rather than constructed here so this is testable without a Plex
    server and reusable by the WebUI, the CLI and the startup report.
    """
    if not app_library_path:
        return LibraryCheck(
            status=UNKNOWN,
            summary="No library path is configured, so there is nothing to check.",
        )

    mapping = mapping or {}
    container_prefix = mapping.get("container")
    plex_prefix = mapping.get("plex")
    mapping_complete = bool(container_prefix and plex_prefix)

    # Translate into Plex's namespace, because that is what its location list is
    # in. Without a complete mapping the two namespaces are not comparable.
    if mapping_complete and is_inside(app_library_path, container_prefix):
        relative = _normalise(app_library_path)[len(_normalise(container_prefix)):]
        plex_path = _normalise(plex_prefix) + relative
        mapping_used = True
    else:
        plex_path = _normalise(app_library_path)
        mapping_used = False

    # Find the section.
    try:
        section = _find_section(plex, library_name, library_id)
    except Exception as exc:  # noqa: BLE001 - unreachable is a valid answer
        return LibraryCheck(
            status=UNREACHABLE,
            summary=f"Could not query Plex: {exc}",
            app_path=app_library_path,
            plex_path=plex_path,
            mapping_used=mapping_used,
        )

    if section is None:
        return LibraryCheck(
            status=TMDB_UNMATCHED,
            summary=(
                "The Plex library named in settings.yml was not found. "
                "Nothing can be tagged until library_name matches a real section."
            ),
            app_path=app_library_path,
            plex_path=plex_path,
            mapping_used=mapping_used,
        )

    try:
        locations = [str(loc) for loc in (getattr(section, "locations", None) or [])]
    except Exception as exc:  # noqa: BLE001
        return LibraryCheck(
            status=UNREACHABLE,
            summary=f"Could not read the library's folders from Plex: {exc}",
            app_path=app_library_path,
            plex_path=plex_path,
            mapping_used=mapping_used,
        )

    if not locations:
        return LibraryCheck(
            status=UNKNOWN,
            summary=(
                "Plex did not report any folders for this library, so the check "
                "cannot be made. This usually means the section is a 'Other' or "
                "legacy agent section."
            ),
            app_path=app_library_path,
            plex_path=plex_path,
            section_locations=locations,
            mapping_used=mapping_used,
        )

    covered = [loc for loc in locations if is_inside(plex_path, loc)]

    if covered:
        return LibraryCheck(
            status=OK,
            summary=f"Plex is scanning this folder (via {covered[0]}).",
            detail=[
                f"App path:        {app_library_path}",
                f"As Plex sees it: {plex_path}",
                f"Plex folders:    {', '.join(locations)}",
            ],
            app_path=app_library_path,
            plex_path=plex_path,
            section_locations=locations,
            mapping_used=mapping_used,
        )

    if not mapping_complete:
        return LibraryCheck(
            status=MAPPING_MISSING,
            summary=(
                "Plex is not scanning this folder, and no path mapping is set - "
                "so the comparison may be unfair. Set plex.path_mapping."
            ),
            detail=_explain_mismatch(app_library_path, plex_path, locations),
            app_path=app_library_path,
            plex_path=plex_path,
            section_locations=locations,
            mapping_used=mapping_used,
        )

    return LibraryCheck(
        status=NOT_SCANNED,
        summary=(
            "Plex is NOT scanning this folder. Sessions will download, sort "
            "correctly, and then never appear in Plex."
        ),
        detail=_explain_mismatch(app_library_path, plex_path, locations),
        app_path=app_library_path,
        plex_path=plex_path,
        section_locations=locations,
        mapping_used=mapping_used,
    )


def _explain_mismatch(app_path: str, plex_path: str, locations: List[str]) -> List[str]:
    """Say which of the two paths is wrong, as far as can be told."""
    detail = [
        f"App path:        {app_path}",
        f"As Plex sees it: {plex_path}",
        f"Plex folders:    {', '.join(locations)}",
        "",
        f"'{plex_path}' is not inside any of those folders.",
        "Either add the folder to the library in Plex "
        "(Manage Library -> Edit -> Add Folders), or fix "
        "library.motogp_path / plex.path_mapping so they agree.",
    ]
    # A very common cause: the mapping is inverted (container and plex swapped).
    for loc in locations:
        if is_inside(app_path, loc):
            detail.append(
                f"Note: '{app_path}' IS inside '{loc}'. The path mapping may be "
                "inverted - container and plex look swapped."
            )
            break
    return detail


def _find_section(plex: Any, library_name: Optional[str], library_id: Optional[str]):
    """Locate the library section by name, falling back to its id."""
    library = getattr(plex, "library", None)
    if library is None:
        raise AttributeError("not a PlexServer-like object")

    if library_name:
        try:
            return library.section(library_name)
        except Exception:  # noqa: BLE001 - fall through to the id
            pass

    sections = library.sections()
    if library_id:
        for section in sections:
            if str(getattr(section, "key", "")) == str(library_id):
                return section

    return None


# ---------------------------------------------------------------------------
# Convenience: build a real connection from configuration
# ---------------------------------------------------------------------------

def check_from_settings(series: str = None) -> LibraryCheck:
    """
    Run the check using the app's own configuration and a real Plex connection.

    ``series`` selects whose library is checked: each series has its own Plex
    section, its own path on disk and possibly its own path mapping, so
    "the Plex library is fine" is only meaningful per series.

    Imported lazily so this module stays importable (and unit-testable) without
    plexapi installed.
    """
    from utils.config_loader import (
        get_plex_library_id,
        get_plex_library_name,
        get_plex_path_mapping,
        get_plex_token,
        get_plex_url,
        get_series_library_path,
    )

    url, token = get_plex_url(), get_plex_token()
    if not url or not token:
        return LibraryCheck(
            status=UNKNOWN,
            summary="Plex URL/token are not configured, so the check cannot run.",
        )

    try:
        from plexapi.server import PlexServer

        plex = PlexServer(url, token, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return LibraryCheck(
            status=UNREACHABLE,
            summary=f"Could not connect to Plex at {url}: {exc}",
        )

    return check_library(
        plex,
        get_series_library_path(series),
        get_plex_path_mapping(series),
        get_plex_library_name(series),
        get_plex_library_id(series),
    )


def main() -> int:
    """python -m utils.plexcheck - prints the verdict and exits non-zero on failure."""
    result = check_from_settings()
    print(f"{result.status}: {result.summary}")
    for line in result.detail:
        print(f"  {line}")
    return 0 if result.status in (OK, UNKNOWN) else 1


if __name__ == "__main__":
    raise SystemExit(main())
