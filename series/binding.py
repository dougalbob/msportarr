"""
Which series is this *process* bound to?

The app has a WebUI that serves every series in one process, and a pipeline
(grabber, sorter, watcher, tagger) that runs one series at a time in its own
subprocess. Those are different problems:

  * the **WebUI** must never rely on a process-wide binding, because two
    requests in the same process can be about different series. It passes a
    series key explicitly, or reads ``by_series`` from a per-series API.
  * the **pipeline** is genuinely one series per process: a grabber run is for
    Formula 1 or for MotoGP, never both. Its modules bind the series' enums and
    tables at import - ``motogp_common.SessionType`` *is* MotoGP's enum - and
    reading them per call would mean threading a series through every function
    in a 2,000-line module to no benefit.

So this module holds **one** binding per process, defaulting to the default
series. Two rules make it safe:

1. **It must be set before the app's modules are imported.** Every CLI does this
   with a four-line prologue ahead of its imports (see below), which is why the
   binding cannot simply be a function call inside ``main()``.
2. **Rebinding after import is refused**, not ignored. A late bind would leave
   ``motogp_common.SessionType`` pointing at one series while the grabber
   searches for another, and the symptom - folders numbered with the wrong
   series' element numbers - is silent and only visible on disk.

Setting it::

    MOTOGPARR_SERIES=f1 python3 motogp_grabber.py scheduled
    python3 motogp_grabber.py --series f1 scheduled      # argv wins

The scheduler sets the environment variable for each subprocess it starts, so
the pipeline inherits the binding without every script having to be re-plumbed.
"""

from __future__ import annotations

import os
from typing import Optional

#: Environment variable the scheduler uses to hand a series to a subprocess.
ENV_VAR = "MOTOGPARR_SERIES"

#: The bound series key, or None when nothing has been bound yet.
_BOUND: Optional[str] = None

#: True once any app module has imported the registry's bindings. Rebinding after
#: that point cannot work, and pretending otherwise produces the silent failure
#: this module exists to prevent.
_FROZEN = False

#: The series that was in effect when the tables were first built.
_FROZEN_AT: Optional[str] = None


def _validate(key: str) -> str:
    from series import series_keys

    if key not in series_keys():
        raise ValueError(
            f"unknown series {key!r}; known: {', '.join(series_keys())}"
        )
    return key


def bind(key: str) -> str:
    """
    Bind this process to one series.

    Call it before importing any module that binds a series' tables. Raises if
    the process is already frozen, because a second, different binding is a bug
    that cannot be honoured: the first one is already compiled into every
    imported table.
    """
    global _BOUND

    key = _validate(key)

    # Compare against the series that was in effect when the tables were built,
    # not just against an explicit binding: a process that never bound anything
    # was still using the default series, and building MotoGP's tables then
    # binding F1 is the same mistake.
    if _FROZEN and key != (_FROZEN_AT or _BOUND):
        raise RuntimeError(
            f"this process is already using {(_FROZEN_AT or _BOUND)!r}; it cannot "
            f"be rebound to {key!r} after the series tables have been imported. "
            f"One process handles one series - start another process for {key!r}."
        )

    _BOUND = key
    return key


def freeze() -> None:
    """
    Mark the binding as used, recording which series it was.

    Called by the registry when a series' tables are first built. Only the
    *first* freeze counts: building the second series' runtime is normal in the
    WebUI, which serves both, and that must not look like a rebind.
    """
    global _FROZEN, _FROZEN_AT

    if not _FROZEN:
        _FROZEN_AT = bound_series()
        _FROZEN = True


def bound_series() -> str:
    """
    The series this process is bound to.

    Falls back to ``MOTOGPARR_SERIES`` and then to the default series, so a
    process started without any binding behaves exactly as it did before the
    hybrid existed.
    """
    from series import default_series

    if _BOUND is not None:
        return _BOUND

    from_env = (os.environ.get(ENV_VAR) or "").strip()
    if from_env:
        return bind(from_env)

    return default_series().key


def reset() -> None:
    """
    Forget the binding. **Tests only.**

    Production code has no reason to unbind - a process does not change series -
    and the frozen flag makes that explicit. Tests need it because a single
    pytest process exercises several bindings, each in a subprocess or a fresh
    import.
    """
    global _BOUND, _FROZEN, _FROZEN_AT
    _BOUND = None
    _FROZEN = False
    _FROZEN_AT = None


def series_from_argv(argv=None) -> Optional[str]:
    """
    Read ``--series KEY`` (or ``--series=KEY``) out of an argument list.

    Used by the CLI prologues, which must know the series *before* argparse runs
    - argparse lives inside ``main()``, which is after the imports that need the
    answer. Returns None when the flag is absent, so the environment decides.
    """
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    for index, arg in enumerate(args):
        if arg == "--series":
            if index + 1 >= len(args):
                raise SystemExit("--series needs a value")
            return args[index + 1]
        if arg.startswith("--series="):
            return arg.split("=", 1)[1]
    return None


def bind_from_argv() -> str:
    """
    Bind from ``--series`` if given, else from the environment, else the default.

    The prologue every CLI runs, before its app imports::

        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from series.binding import bind_from_argv
        bind_from_argv()
    """
    requested = series_from_argv()
    if requested:
        return bind(requested)
    return bound_series()
