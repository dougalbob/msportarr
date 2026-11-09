"""
Library scan cost (Candidate D of docs/IMPROVEMENT_PLAN.md).

The candidate is explicit that the scan has *not* been shown to be slow, so the
deliverable is a measurement, not an index. What is tested here:

* the instrumentation returns the scanned value untouched (a wrapper that
  changes the answer would be a bug, not a measurement);
* percentiles and slow-scan reporting behave on known inputs - including the
  outlier case a mean would hide;
* a representative temporary library is actually scanned and measured, so the
  numbers in `/diagnostics` come from the real walk;
* the watcher's cache refresh is measured too, and still caches.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp(prefix="motogp-scan-test-"))

from utils import scan_metrics  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    scan_metrics.reset()
    yield
    scan_metrics.reset()


# ===========================================================================
# The measurement itself
# ===========================================================================

def test_timed_returns_the_scanned_value_untouched():
    """Instrumentation that changes the result is a bug wearing a metric."""
    sentinel = {1: {"a": "path"}}
    assert scan_metrics.timed("x", lambda: sentinel) is sentinel


def test_timed_records_duration_and_scope_from_a_round_shaped_result():
    result = {1: {"a": 1, "b": 2}, 2: {"c": 3}}
    scan_metrics.timed("library-season", lambda: result)

    summary = scan_metrics.summarise("library-season")
    assert summary["samples"] == 1
    assert summary["slowest_scope"] == {"rounds": 2, "sessions": 3}


def test_a_raising_block_is_still_recorded():
    """
    A scan that fails after four seconds is exactly the interesting sample;
    dropping it would leave the fast successes looking like the whole story.
    Use the context manager for anything that can raise: ``timed()`` cannot
    record a call that never returned, and that limit is deliberate rather
    than papered over with a guess at the duration.
    """
    with pytest.raises(RuntimeError):
        with scan_metrics.timed_scan("library-season"):
            raise RuntimeError("permission denied")

    assert scan_metrics.summarise("library-season")["samples"] == 1


def test_an_outlier_is_reported_by_max_and_by_name_whatever_p95_says():
    """
    One bad scan in twenty is *correctly* the 95th percentile's neighbour, not
    above it, so p95 alone would not surface it. That is why the summary also
    carries max and names slow samples individually - a mean of ~0.26 s would
    have hidden the five-second scan entirely.
    """
    for seconds in [0.01] * 19 + [5.0]:
        scan_metrics.record("library-season", seconds)

    summary = scan_metrics.summarise("library-season")
    assert summary["median_ms"] == 10.0
    assert summary["max_ms"] == 5000.0
    assert [s["seconds"] for s in scan_metrics.slow_scans()] == [5.0]


def test_p95_rises_once_slow_scans_stop_being_isolated():
    """A regression, not a blip, does move the percentile."""
    for seconds in [0.01] * 10 + [3.0] * 10:
        scan_metrics.record("library-season", seconds)

    assert scan_metrics.summarise("library-season")["p95_ms"] == 3000.0


def test_slow_scans_names_the_samples_that_matter():
    scan_metrics.record("library-season", 0.05, {"rounds": 1})
    scan_metrics.record("library-season", 3.5, {"rounds": 22, "sessions": 300})

    slow = scan_metrics.slow_scans()
    assert len(slow) == 1
    assert slow[0]["seconds"] == 3.5
    assert slow[0]["scope"]["sessions"] == 300


def test_the_sample_ring_is_bounded():
    for _ in range(scan_metrics.MAX_SAMPLES + 20):
        scan_metrics.record("library-season", 0.01)

    assert scan_metrics.summarise("library-season")["samples"] == scan_metrics.MAX_SAMPLES


def test_an_unmeasured_process_says_so_rather_than_reporting_a_median():
    snapshot = scan_metrics.snapshot()
    assert snapshot["measured"] is False
    assert snapshot["by_label"] == {}


def test_timed_scan_records_even_when_the_block_raises():
    with pytest.raises(KeyError):
        with scan_metrics.timed_scan("watcher-library-cache") as scope:
            scope["rounds"] = 3
            raise KeyError("boom")

    assert scan_metrics.summarise("watcher-library-cache")["slowest_scope"] == {"rounds": 3}


# ===========================================================================
# A representative library, measured for real
# ===========================================================================

#: Real element numbers (SESSION_ELEMENTS allocates in blocks of ten, so 11 is
#: not a session at all). Using made-up consecutive numbers would build a
#: library the app cannot read and the scan would look unreasonably fast.
ELEMENT_NUMBERS = [10, 12, 14, 20, 22, 24, 30, 32]


def _build_library(root: Path, seasons=2, rounds=10, sessions=8) -> int:
    """
    A library shaped like a real one: season/Round NN Venue/YYRNNEMM Session/
    with one media file each. 8 sessions x 10 rounds x 2 seasons = 160 folders,
    which is a plausible current size and enough that the walk is not trivial.
    """
    assert sessions <= len(ELEMENT_NUMBERS)
    count = 0
    for season in range(2026, 2026 - seasons, -1):
        for round_num in range(1, rounds + 1):
            round_dir = root / str(season) / f"Round {round_num:02d} Testland"
            round_dir.mkdir(parents=True, exist_ok=True)
            for element in ELEMENT_NUMBERS[:sessions]:
                session_dir = round_dir / (
                    f"{str(season)[2:]}R{round_num:02d}E{element:02d} Session"
                )
                session_dir.mkdir(exist_ok=True)
                (session_dir / "session.mkv").write_bytes(b"x")
                count += 1
    return count


def test_the_real_webui_walk_is_measured_and_finds_every_session(tmp_path, monkeypatch):
    import motogp_webui

    created = _build_library(tmp_path)
    monkeypatch.setattr(motogp_webui, "library_root", lambda *a, **kw: str(tmp_path))

    result = motogp_webui.scan_library_for_season(2026)

    assert sum(len(sessions) for sessions in result.values()) == created // 2
    summary = scan_metrics.summarise("library-season")
    assert summary["samples"] == 1
    assert summary["slowest_scope"]["rounds"] == 10
    assert summary["slowest_scope"]["sessions"] == created // 2


def test_the_watcher_cache_walk_is_measured_and_still_caches(tmp_path):
    """
    The cache is the reason the candidate is only a measurement: a five-minute
    TTL means the walk happens once, so a slow walk costs one scan per five
    minutes, not one per folder.
    """
    from motogp_watcher import LibraryChecker

    _build_library(tmp_path, seasons=1, rounds=3, sessions=2)
    checker = LibraryChecker(str(tmp_path))

    assert checker.exists_in_library("26R01E10 Session")[0] is True
    first = scan_metrics.summarise("watcher-library-cache")
    assert first["samples"] == 1
    assert first["slowest_scope"]["rounds"] == 3
    assert first["slowest_scope"]["sessions"] == 6

    # A second lookup inside the TTL must not walk again.
    checker.exists_in_library("26R02E10 Session")
    assert scan_metrics.summarise("watcher-library-cache")["samples"] == 1


def test_the_cache_still_expires(tmp_path):
    from motogp_watcher import LibraryChecker

    _build_library(tmp_path, seasons=1, rounds=1, sessions=1)
    checker = LibraryChecker(str(tmp_path))
    checker.exists_in_library("26R01E10 Session")

    # Pretend the cache is stale rather than sleeping five minutes.
    checker._cache_time = datetime.now() - timedelta(minutes=6)
    checker.exists_in_library("26R01E10 Session")

    assert scan_metrics.summarise("watcher-library-cache")["samples"] == 2


# ===========================================================================
# Diagnostics surface
# ===========================================================================

def test_diagnostics_reports_scan_cost_without_credentials(tmp_path, monkeypatch):
    import motogp_webui

    _build_library(tmp_path, seasons=1, rounds=2, sessions=2)
    monkeypatch.setattr(motogp_webui, "library_root", lambda *a, **kw: str(tmp_path))
    monkeypatch.setattr(motogp_webui, "CONFIG_DIR", tmp_path)
    motogp_webui.app.config.update(TESTING=True)

    # One page-driven scan, then read the diagnostic.
    motogp_webui.scan_library_for_season(2026)

    with motogp_webui.app.test_client() as client:
        payload = client.get("/diagnostics").get_json()

    scan = payload["scan_cost"]
    assert scan["measured"] is True
    assert scan["by_label"]["library-season"]["samples"] == 1
    assert scan["threshold_seconds"] == scan_metrics.SLOW_SCAN_SECONDS
    assert "plex-token" not in json.dumps(payload)
