#!/usr/bin/env python3
"""
Container permission check
==========================

The container drops privileges to PUID:PGID (default 99:100, which is what Unraid
calls nobody:users) before the app starts. Dropping privileges is the easy part;
the failure it produces is the problem.

A wrong owner does not look like a permissions problem. It looks like:

    * the WebUI saving settings and the values silently reverting,
    * downloads that "complete" but never get sorted,
    * `PermissionError` in a log line at 3am, in a subprocess, after a grab,
    * a container that starts perfectly and does nothing all weekend.

None of those say "your appdata folder belongs to root". So this module checks
the folders the app must write to, *before* it starts doing work, and says
exactly which one is wrong and which command fixes it.

Two design points:

  * It only checks paths the app actually writes. /media is usually writable
    only where the library lives; demanding write access to the mount root would
    produce false failures on read-only media shares.
  * It reports, it does not fix. Changing ownership of a user's media tree
    uninvited is not a startup script's business.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

#: Directories that must be writable, relative to CONFIG_DIR.
#: Everything the app persists - settings, state, calendars, logs - lives here.
#:
#: These are created if missing: /config belongs to the app. Library and
#: downloads paths deliberately are NOT created - those are the user's data
#: locations, and inventing them would hide a wrong volume mapping rather than
#: reveal it.
CONFIG_WRITABLE_ITEMS = ("", "logs")


@dataclass
class PathCheck:
    path: str
    label: str
    exists: bool
    writable: bool
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.exists and self.writable

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "label": self.label,
            "exists": self.exists,
            "writable": self.writable,
            "ok": self.ok,
            "detail": self.detail,
        }


#: Optional environment variable mapping container paths to the host paths they
#: are mounted from, e.g. "/media=/mnt/user/data/media,/downloads=/mnt/user/data/downloads".
#: When set, the permission hint can print the exact host-runnable chown command;
#: when unset, the hint honestly describes the container path instead of
#: inventing a host path.
HOST_MAP_ENV = "MOTORSPORTARR_HOST_MAP"
LEGACY_HOST_MAP_ENV = "MOTOGPARR_HOST_MAP"


def parse_host_map(raw: Optional[str]) -> Dict[str, str]:
    """Parse ``MOTORSPORTARR_HOST_MAP`` into {container path: host path}.

    Blank entries and entries without an `=` are ignored. Container paths are
    compared without a trailing slash so "/media/" and "/media" match.
    """
    mapping: Dict[str, str] = {}
    if not raw:
        return mapping
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        container, _, host = pair.partition("=")
        container = container.strip().rstrip("/")
        host = host.strip()
        if container and host:
            mapping[container] = host
    return mapping


@dataclass
class PermissionReport:
    uid: int
    gid: int
    is_root: bool
    checks: List[PathCheck]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> List[PathCheck]:
        return [check for check in self.checks if not check.ok]

    def hint(self, host_map: Optional[Dict[str, str]] = None) -> str:
        """How to fix the usual cause, or '' when nothing is wrong.

        The paths in the failures are *inside the container*. The hint never
        prints one as a host-runnable command: with a host map it prints the
        chown against the mapped host path; without one it tells the user to
        chown whatever they mounted at the container path.

        ``host_map`` defaults to parsing ``MOTORSPORTARR_HOST_MAP`` (falling
        back to legacy ``MOTOGPARR_HOST_MAP``) from the environment, so the CLI
        and the entrypoint print identical text.
        """
        if self.ok:
            return ""

        if host_map is None:
            raw = os.environ.get(HOST_MAP_ENV) or os.environ.get(LEGACY_HOST_MAP_ENV)
            host_map = parse_host_map(raw)

        lines = [
            f"This is almost always ownership. The container runs as "
            f"{self.uid}:{self.gid}, so those folders must be owned (or "
            f"writable) by it.",
            f"The paths below are inside the container. On the host, chown the "
            f"folder you mounted at each container path - never the container "
            f"path itself - or set PUID/PGID on the container to the owner of "
            f"those folders (Unraid shares are normally 99:100):",
        ]
        for check in self.failures:
            host = host_map.get(check.path)
            if host:
                lines.append(f"    {check.path} (container) -> '{host}' (host)")
                lines.append(f"        chown -R {self.uid}:{self.gid} '{host}'")
            else:
                lines.append(
                    f"    {check.path} (container) -> no host mapping set "
                    f"({HOST_MAP_ENV}); chown whatever you mounted there"
                )
                lines.append(
                    f"        chown -R {self.uid}:{self.gid} "
                    f"<host path mounted at '{check.path}'>"
                )
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "uid": self.uid,
            "gid": self.gid,
            "is_root": self.is_root,
            "ok": self.ok,
            "checks": [check.as_dict() for check in self.checks],
            "failures": [check.path for check in self.failures],
            "hint": self.hint(),
        }


def _writable(path: str) -> bool:
    """
    Real write test: create and remove a file.

    os.access(W_OK) answers for the *real* uid in some situations and can be
    wrong where ACLs or read-only mounts are involved. Creating a file is the
    question the app is actually asking.
    """
    probe = os.path.join(path, ".msportarr-write-test")
    try:
        with open(probe, "w") as handle:
            handle.write("")
        os.unlink(probe)
        return True
    except Exception:  # noqa: BLE001 - any failure means "not writable"
        return False


def check_permissions(
    config_dir: Optional[str] = None,
    library_path: Optional[str] = None,
    downloads_path: Optional[str] = None,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
    series_paths: Optional[Dict[str, Tuple[str, str]]] = None,
) -> PermissionReport:
    """
    Check that the app can write where it must.

    library_path and downloads_path are only checked when they exist - a missing
    downloads folder is routine on a fresh install and is already reported
    separately. What matters here is *existing but unwritable*, which is the
    ownership case.

    ``series_paths`` checks one (library, downloads) pair per series, labelled
    with the series so a failure names the library that is wrong. It exists
    because the two series are usually different folders owned by different
    things - and on Unraid, a second share that nobody chowned looks exactly
    like "sorting stopped working" for that series alone.
    """
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid

    if config_dir is None:
        config_dir = os.environ.get("CONFIG_DIR", "/config")

    checks: List[PathCheck] = []

    for item in CONFIG_WRITABLE_ITEMS:
        path = os.path.join(config_dir, item) if item else config_dir
        label = f"config ({item or 'root'})"

        # /config belongs to the app, so its own subfolders are created here
        # rather than reported missing. On a fresh install logs/ does not exist
        # yet, and treating that as a failure would refuse to start every new
        # user - a first-run bug that only shows up on an empty appdata folder.
        if item and not os.path.isdir(path):
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                pass  # reported below as not writable, with the reason

        exists = os.path.isdir(path)
        writable = _writable(path) if exists else False
        detail = ""
        if not exists:
            detail = "could not be created"
        elif not writable:
            detail = f"exists but is not writable by {uid}:{gid}"
        checks.append(PathCheck(path, label, exists, writable, detail))

    pairs = [(library_path, "library"), (downloads_path, "downloads")]
    for series, pair in (series_paths or {}).items():
        library, downloads = pair
        pairs.append((library, f"{series} library"))
        pairs.append((downloads, f"{series} downloads"))

    for path, label in pairs:
        if not path:
            continue
        exists = os.path.isdir(path)
        if not exists:
            # Missing paths are the path check's business, not this one's.
            continue
        writable = _writable(path)
        checks.append(PathCheck(
            path, label, True, writable,
            "" if writable else f"exists but is not writable by {uid}:{gid}",
        ))

    return PermissionReport(uid=uid, gid=gid, is_root=(uid == 0), checks=checks)


def main() -> int:
    """python -m utils.permissions - exit 1 when a required folder is unwritable."""
    from series import default_series
    from utils.config_loader import (
        active_series,
        get_series_downloads_path,
        get_series_library_path,
    )

    default_key = default_series().key
    report = check_permissions(
        library_path=get_series_library_path(default_key),
        downloads_path=get_series_downloads_path(default_key),
        series_paths={
            spec.key: (get_series_library_path(spec.key),
                       get_series_downloads_path(spec.key))
            for spec in active_series() if spec.key != default_key
        },
    )

    print(f"Running as uid={report.uid} gid={report.gid}" + (" (root)" if report.is_root else ""))
    for check in report.checks:
        state = "ok" if check.ok else ("MISSING" if not check.exists else "NOT WRITABLE")
        print(f"  {check.label:<14} {check.path}  [{state}]")

    if not report.ok:
        print()
        print(report.hint())
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
