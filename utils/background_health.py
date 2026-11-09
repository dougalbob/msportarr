#!/usr/bin/env python3
"""
Background-process health for the scheduler and the download watcher
====================================================================

Candidate A of ``docs/IMPROVEMENT_PLAN.md``. The container's Docker health
probe only proves that the *WebUI* answers ``/healthz``: ``entrypoint.sh``
supervises the scheduler process and stops the container when it exits, but a
scheduler that is alive and no longer looping - a stuck search, a dead watcher
thread inside it - leaves the container "healthy" while nothing downloads.
Process liveness is not progress, so progress has to be recorded.

This module owns the record and the rules. It is split so the interesting part
is provable without a container:

* :func:`write_heartbeat` / :func:`read_heartbeat` persist one small JSON
  document through :mod:`utils.json_store` (the single persistence contract
  for shared ``/config`` state). Losing a heartbeat must never break the loop
  that produces it, so a failed write is logged and swallowed here - the
  caller is a sleep loop, not a request.
* :func:`component_health` turns that document into the same shape the home
  page already uses for the scheduler (``scheduler_health``): ``never``,
  ``age_minutes``, ``age_label``, ``stale``. It takes an explicit ``now`` and
  does no I/O, so every threshold is testable without waiting.

What this deliberately does **not** do:

* It does not feed ``/healthz``. The probe stays a lightweight, deterministic
  "can the WebUI answer" check - no files, no services, no race-weekend
  awareness - so a slow but legitimate Prowlarr search cannot put the
  container into a restart loop. Staleness is reported to the user, not to
  Docker; see the note in ``docs/IMPROVEMENT_PLAN.md`` Candidate A and the
  decision recorded in ``docs/ARCHITECTURE.md``.
* It does not decide how often anything runs. The scheduler passes its own
  expected interval, so the stale threshold cannot drift away from the
  intervals it actually sleeps for.

The document shape is intentionally small and additive::

    {
      "scheduler": {"last_loop": "...ISO...", "last_check": "...", "started": "..."},
      "watcher":   {"last_scan": "...ISO...", "started": "..."}
    }

A key that is absent means "no evidence yet" and renders as ``unknown``, never
as healthy. An old document written before a key existed therefore degrades to
one unknown component, not to a false "everything is fine".
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from utils.state_files import state_filename
from utils.json_store import atomic_write_json, file_lock, read_json

logger = logging.getLogger("motogparr.background_health")

#: Components that record a heartbeat, in display order.
COMPONENTS = ("scheduler", "watcher")

#: Both components are always judged. The WebUI cannot see the scheduler's
#: argv, so it cannot tell whether ``--no-watcher`` was passed; that flag is
#: only ever used by a developer and the entrypoint never sets it. Choosing
#: to always judge is the honest default: in a deployment that really has no
#: watcher, "stale" is the accurate answer, and the alternative - assuming
#: the watcher is disabled - would silently hide a dead thread in every
#: normal deployment.

#: Per-component heartbeat fields. ``last_*`` is what proves progress;
#: ``started`` is only context (it stops "never" from meaning "just started").
FIELDS = {
    "scheduler": ("last_loop", "last_check", "started"),
    "watcher": ("last_scan", "started"),
}

#: The field that proves each component is still doing work.
PROGRESS_FIELD = {"scheduler": "last_loop", "watcher": "last_scan"}

#: Stale threshold = twice the expected interval plus this much grace. Two
#: intervals covers a slept interval that started just before the previous
#: heartbeat; the grace absorbs one slow scan without crying wolf.
GRACE_MINUTES = 60


def heartbeat_path(config_dir: Path) -> Path:
    """``<config>/background_health.<series>.json`` - one location, no duplicates."""
    return Path(config_dir) / state_filename("background_health")


def _empty() -> Dict[str, Any]:
    return {}


def _clean(value: Any) -> Optional[str]:
    """An ISO timestamp worth recording, or None.

    Non-strings and unparseable strings are dropped rather than stored: a
    heartbeat file full of junk would make the health rule parse-and-fail on
    every page load instead of reporting "never", which is the honest answer.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return value


def write_heartbeat(config_dir: Path, component: str, **fields: Any) -> bool:
    """
    Record progress for one component. Returns whether it was written.

    One component per call, merged under the store's lock, so the scheduler
    and the watcher (a thread writing on its own schedule) cannot overwrite
    each other's timestamps. A write failure is logged and reported, never
    raised: the caller is a monitoring loop, and a monitoring loop that dies
    because monitoring failed is a worse outage than a missing heartbeat.
    """
    if component not in FIELDS:
        raise ValueError(f"unknown heartbeat component: {component!r}")

    unknown = set(fields) - set(FIELDS[component])
    if unknown:
        raise ValueError(
            f"unknown heartbeat field(s) for {component}: {sorted(unknown)}"
        )

    cleaned = {name: _clean(value) for name, value in fields.items()}
    cleaned = {name: value for name, value in cleaned.items() if value}
    if not cleaned:
        return False

    path = heartbeat_path(config_dir)

    try:
        with file_lock(path):
            data = read_json(path, _empty(), log=logger)
            if not isinstance(data, dict):
                # A heartbeat document that is not an object carries nothing
                # worth preserving; the store has already logged the corruption.
                data = {}
            entry = data.get(component)
            if not isinstance(entry, dict):
                entry = {}
            entry.update(cleaned)
            data[component] = entry
            atomic_write_json(path, data)
        return True
    except Exception as exc:  # noqa: BLE001 - monitoring must not kill the loop
        logger.error("Could not write the %s heartbeat to %s: %s", component, path, exc)
        return False


def read_heartbeat(config_dir: Path) -> Dict[str, Any]:
    """The stored document, or ``{}``. Corruption is already logged by the store."""
    data = read_json(heartbeat_path(config_dir), _empty(), log=logger)
    return data if isinstance(data, dict) else {}


def format_age(minutes: int) -> str:
    """Human age, e.g. ``just now`` / ``12 min ago`` / ``1 d 03 h ago``.

    Duplicated from ``utils.home_summary`` deliberately: that module imports
    ``motogp_grabber`` (and therefore the whole search stack), and this one is
    imported by the scheduler loop and the WebUI. Importing the grabber to
    format a timestamp would couple every heartbeat write to the search code.
    """
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    days, remainder = divmod(minutes, 1440)
    hours, mins = divmod(remainder, 60)
    if days:
        return f"{days} d {hours:02d} h ago"
    return f"{hours} h {mins:02d} m ago"


def component_health(
    heartbeat: Optional[dict],
    component: str,
    now: datetime,
    expected_minutes: int,
) -> Dict[str, Any]:
    """
    Is ``component`` still making progress, judged against its own interval?

    States, and why each one exists:

    ``live``     the progress timestamp is within twice the expected interval
                 plus ``GRACE_MINUTES``. The interval is passed in by the
                 caller (the scheduler's own sleep interval), so this rule
                 cannot drift away from the loop it describes.
    ``stale``    it is older than that. Reported with the age *and* the
                 expected interval, because "stale" without the comparison
                 reads as a bug report rather than a measurement.
    ``never``    nothing has been recorded. Distinct from ``stale`` and never
                 reported as healthy: before the first heartbeat there is no
                 evidence either way, and a fresh container must not claim
                 everything is fine.

    ``note`` explains a ``never``, because "never" has two very different
    causes: a component that has not started yet, and a heartbeat file that
    is present but unreadable. The second needs attention, so it must not be
    indistinguishable from the first.
    """
    expected = max(int(expected_minutes), 1)
    result: Dict[str, Any] = {
        "component": component,
        "state": "never",
        "healthy": False,
        "never": True,
        "last": None,
        "age_minutes": None,
        "age_label": "never",
        "expected_minutes": expected,
        "threshold_minutes": expected * 2 + GRACE_MINUTES,
        "note": "",
    }

    entry = (heartbeat or {}).get(component)
    entry = entry if isinstance(entry, dict) else {}
    field = PROGRESS_FIELD[component]
    raw = entry.get(field)

    if not raw:
        result["note"] = f"no {field} recorded"
        return result

    try:
        last = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        result["note"] = f"{field} is not a timestamp: {raw!r}"
        return result

    age_minutes = int((now - last).total_seconds() // 60)
    stale = age_minutes > result["threshold_minutes"]
    result.update({
        "state": "stale" if stale else "live",
        "healthy": not stale,
        "never": False,
        "last": last,
        "age_minutes": age_minutes,
        "age_label": format_age(age_minutes),
    })
    return result


def quiet_hours_at(now: datetime) -> bool:
    """
    Whether the scheduler is in its quiet hours at ``now``.

    Delegates to the scheduler's own ``is_quiet_hours`` rather than restating
    the window: if quiet hours move, the stale threshold moves with them. A
    second copy here would eventually judge a healthy overnight scheduler
    against the daytime interval and report it stale every night at 03:00.
    """
    from motogp_scheduler import is_quiet_hours

    return is_quiet_hours(now)


def scheduler_interval_minutes(now: datetime, in_weekend: bool,
                               quiet: bool = False) -> int:
    """
    The scheduler's own sleep interval for ``now``, read from its CONFIG.

    Read from the scheduler rather than restated (see ``quiet_hours_at``).
    Quiet hours are the longest interval, so using them when they apply is
    what stops a healthy overnight scheduler from being reported stale.
    """
    from motogp_scheduler import CONFIG as SCHEDULER_CONFIG

    if quiet:
        return SCHEDULER_CONFIG["quiet_hours"]["interval_minutes"]
    if in_weekend:
        return SCHEDULER_CONFIG["race_weekend_interval_minutes"]
    return SCHEDULER_CONFIG["off_weekend_interval_minutes"]


def background_health(heartbeat: Optional[dict], now: datetime,
                      in_weekend: bool, quiet: bool = False) -> Dict[str, Any]:
    """
    Both components' health plus one overall verdict, for the WebUI.

    ``overall`` is ``stale`` if any component is stale, ``never`` if none has
    ever reported, otherwise ``live``. It is a verdict about the background
    work, not about the container: it is shown to the user and in
    ``/diagnostics``, and the Docker probe ignores it on purpose - a stale
    heartbeat is a warning, never a restart.

    The watcher's threshold uses the scheduler's interval: the watcher scans
    every ten seconds and the threshold is therefore hours wide, so this
    cannot produce false alarms. It is deliberately the same number rather
    than a second tunable that nobody would remember to change.
    """
    interval = scheduler_interval_minutes(now, in_weekend, quiet)
    scheduler = component_health(heartbeat, "scheduler", now, interval)
    watcher = component_health(heartbeat, "watcher", now, interval)

    states = [scheduler["state"], watcher["state"]]
    if "stale" in states:
        overall = "stale"
    elif all(state == "never" for state in states):
        overall = "never"
    else:
        overall = "live"

    return {
        "overall": overall,
        "in_weekend": in_weekend,
        "quiet": quiet,
        "components": {"scheduler": scheduler, "watcher": watcher},
    }
