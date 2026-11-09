#!/usr/bin/env python3
"""
Library scan cost - measure first (Candidate D)
===============================================

Candidate D says, correctly, that nobody has established the library walks are
slow enough to justify an index or a database. The watcher caches a scan for
five minutes and several WebUI routes walk the library per request, so the
suspicion is reasonable - but replacing the scan without a measurement would be
optimising whichever part *looks* expensive.

So this module only measures:

* :func:`timed_scan` wraps a scan callable, records duration and scope (rounds
  and sessions visited), and returns exactly what the callable returned. Callers
  are unchanged apart from the wrapper.
* :func:`snapshot` turns the recorded samples into a summary for
  ``/diagnostics``: count, median, 95th percentile, slowest, and the slowest
  scope. Percentiles rather than a mean, because one 40-second outlier on a
  cold page cache would hide behind a mean of 200 ms.
* :func:`slow_scans` names the samples above the threshold, so "unusually slow"
  is a visible fact rather than a feeling.

Deliberate limits, recorded so a later session does not "improve" them into the
thing the candidate argues against:

* No persistence. Samples live in this process and are lost on restart; a
  metrics file would be another `/config` store to lock and migrate for
  information that only matters for the current investigation.
* No caching change, no index, no database. That is the *outcome* of a
  measurement, not part of taking one.
* No timers in the sort/tagger path - the library scan is the question.
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

logger = logging.getLogger("msportarr.scan_metrics")

#: Samples kept per label. A ring of 50 is enough to see a trend on a page that
#: is reloaded a handful of times, and small enough to never grow unbounded in
#: a process that runs for months.
MAX_SAMPLES = 50

#: A scan slower than this is reported individually. Chosen well above what a
#: few hundred folder stats cost (tens of milliseconds) so it means "something
#: happened", not "the disk was a bit busy".
SLOW_SCAN_SECONDS = 2.0

_SAMPLES: Dict[str, List["Sample"]] = {}


@dataclass
class Sample:
    """One measured scan."""

    label: str
    seconds: float
    scope: Dict[str, int] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "seconds": round(self.seconds, 4),
            "scope": dict(self.scope),
            "age_seconds": round(time.time() - self.at, 1),
        }


def record(label: str, seconds: float, scope: Optional[Dict[str, int]] = None) -> Sample:
    """Store one sample, dropping the oldest when the ring is full."""
    sample = Sample(label=label, seconds=float(seconds), scope=dict(scope or {}))
    samples = _SAMPLES.setdefault(label, [])
    samples.append(sample)
    del samples[:-MAX_SAMPLES]
    return sample


def reset() -> None:
    """Forget every sample. For tests, and for a manual investigation restart."""
    _SAMPLES.clear()


@contextmanager
def timed_scan(label: str) -> Iterator[Dict[str, int]]:
    """
    Time a block and record it. The yielded dict is the scope to fill in.

    ``with timed_scan("library-season") as scope: scan(); scope["sessions"] = n``
    - the scope is recorded whether or not the block raises, because a scan that
    fails after four seconds is exactly the kind of thing worth seeing.
    """
    scope: Dict[str, int] = {}
    started = time.perf_counter()
    try:
        yield scope
    finally:
        record(label, time.perf_counter() - started, scope)


def timed(label: str, scan: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """
    Wrap one call. Returns the callable's own value untouched.

    A secondary scope counter is read from the result when it is a dict of
    rounds, which is the shape ``scan_library_for_season`` returns, so the
    diagnostic can say how big the thing that took four seconds was.
    """
    started = time.perf_counter()
    result = scan(*args, **kwargs)
    seconds = time.perf_counter() - started

    scope: Dict[str, int] = {}
    if isinstance(result, dict):
        scope["rounds"] = len(result)
        scope["sessions"] = sum(
            len(sessions) for sessions in result.values() if isinstance(sessions, dict)
        )

    record(label, seconds, scope)
    return result


def _percentile(values: List[float], fraction: float) -> float:
    """
    Nearest-rank percentile (``ceil(fraction * n)``), no dependency.

    Nearest-rank rather than linear interpolation because the question here is
    "how bad does it get", and interpolation smooths the single bad sample into
    its neighbours - exactly the outlier this is meant to expose.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(fraction * len(ordered))
    return ordered[max(0, min(len(ordered) - 1, rank - 1))]


def summarise(label: str, samples: Optional[List[Sample]] = None) -> Dict[str, Any]:
    """Count/median/p95/max for one label, or ``{"label", "samples": 0}``."""
    samples = _SAMPLES.get(label, []) if samples is None else samples
    if not samples:
        return {"label": label, "samples": 0, "median_ms": None, "p95_ms": None,
                "max_ms": None, "slowest_scope": {}}

    seconds = [sample.seconds for sample in samples]
    slowest = max(samples, key=lambda sample: sample.seconds)
    return {
        "label": label,
        "samples": len(samples),
        "median_ms": round(statistics.median(seconds) * 1000, 1),
        "p95_ms": round(_percentile(seconds, 0.95) * 1000, 1),
        "max_ms": round(slowest.seconds * 1000, 1),
        "slowest_scope": dict(slowest.scope),
    }


def slow_scans(label: Optional[str] = None,
               threshold: float = SLOW_SCAN_SECONDS) -> List[Dict[str, Any]]:
    """Samples above ``threshold``, newest last, with what they scanned."""
    labels = [label] if label else sorted(_SAMPLES)
    out = []
    for name in labels:
        for sample in _SAMPLES.get(name, []):
            if sample.seconds >= threshold:
                out.append(sample.to_dict())
    return out


def snapshot() -> Dict[str, Any]:
    """
    The whole picture, for ``/diagnostics``.

    ``measured: False`` is an explicit answer: a fresh process that has served
    one page has one sample, and saying "no data yet" is more useful than
    printing a confident median of one.
    """
    labels = sorted(_SAMPLES)
    summaries = {name: summarise(name) for name in labels}
    return {
        "threshold_seconds": SLOW_SCAN_SECONDS,
        "measured": any(summary["samples"] for summary in summaries.values()),
        "by_label": summaries,
        "slow": slow_scans(),
    }
