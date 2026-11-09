"""
Namespaced ``/config`` filenames, one series at a time.

Every shared state file in this app is a JSON document in ``/config`` written by
several processes. Before there was a second series that was fine: MotoGP was
the only writer of ``grabber_state.json``, ``webui_settings.json`` and the rest.

It is not fine now. Two series writing ``grabber_state.json`` into one volume
overwrite each other's entries within a day, and the symptom is not an error -
it is history that quietly stops accumulating, or a desired-session preference
reverting because the other series saved its own defaults over it. So every
filename carries the series it belongs to::

    grabber_state.motogp.json
    grabber_state.f1.json      (when F1 starts writing state)

The naming rule itself lives on ``SeriesRuntime.state_file()``, because that is
where the declaration is. This module exists so call sites never have to reach
for a runtime object: a module can ask for the canonical MotoGP file by the
name the rest of the codebase uses, and the registry's rule is still the only
place the format is written down.

**The default series keeps a migration path.** MotoGP wrote
``grabber_state.json`` for years, so ``migrate_default_series_state()`` renames
it once at startup. That is deliberately a rename and deliberately
default-series-only: a series added later never had an un-namespaced file, and
inheriting MotoGP's would resurrect the exact collision this module prevents.
"""

import logging
from pathlib import Path
from typing import Optional

from series import default_series, runtime

logger = logging.getLogger("motogparr.state")

__all__ = [
    "state_filename",
    "state_path",
    "state_files_in_use",
    "migrate_default_series_state",
]


def _runtime(series: Optional[str] = None):
    """The runtime whose naming rule applies; the default series if unnamed."""
    return runtime(series or default_series().key)


def state_filename(name: str, series: Optional[str] = None) -> str:
    """
    The filename a series writes its state to.

    ``state_filename("grabber_state")`` -> ``grabber_state.motogp.json``. Name a
    series only when you mean one that is not the default; the default is what
    the existing call sites mean.
    """
    return _runtime(series).state_file(name)


def state_path(config_dir, name: str, series: Optional[str] = None) -> Path:
    """``<config_dir>/<name>.<series>.json``."""
    return Path(config_dir) / state_filename(name, series)


#: The shared /config documents whose names are namespaced to a series.
#:
#: calendars, settings.yml, logs and the Plex/SAB configuration are NOT here:
#: a calendar is already named for its series, settings.yml is one installation's
#: configuration rather than one series' state, and the log files are named by
#: series already. This list is the set of files whose *name* would have
#: collided, which is exactly the set that has to move.
STATE_FILES = (
    "grabber_state",
    "watcher_state",
    "webui_settings",
    "activity_events",
    "release_mismatches",
    "background_health",
)


def state_files_in_use() -> tuple:
    """Every namespaced state document, for the startup migration to check."""
    return STATE_FILES


def migrate_default_series_state(config_dir, names=None) -> list:
    """
    Rename the default series' pre-namespacing state files, once.

    Returns the list of new paths actually created. Idempotent, and it never
    overwrites: if the namespaced file exists it wins, and the legacy file is
    left exactly where it is. Silently merging two files with different
    contents would be a data decision disguised as a migration.

    A failure is logged and skipped rather than raised. This runs at startup,
    where refusing to start over an un-renamable file turns "your history is in
    the old place" into "the app will not run" - and the writers fall back to
    the namespaced name either way, so the worst case is a state file that
    starts empty and says so.
    """
    migrated = []
    rnd = _runtime()
    for name in (STATE_FILES if names is None else names):
        try:
            moved = rnd.migrate_state_file(config_dir, name)
        except OSError as exc:
            logger.warning(
                "Could not move legacy %s into %s: %s",
                name, rnd.state_file(name), exc,
            )
            continue
        if moved is not None:
            logger.info("Migrated %s -> %s", Path(config_dir) / f"{name}.json",
                        moved)
            migrated.append(moved)
    return migrated
