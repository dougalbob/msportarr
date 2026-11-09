"""
Background-process health (Candidate A of docs/IMPROVEMENT_PLAN.md).

The container health probe only answers "is the WebUI up". `entrypoint.sh`
stops the container if the scheduler *process* exits, but a scheduler that is
alive and no longer looping - or a watcher thread that died inside it - left no
trace anywhere: the container stayed healthy while nothing downloaded.

Three layers are tested here:

* the pure rule (`component_health` / `background_health`) against explicit
  timestamps, so every threshold is provable without waiting;
* the record (`write_heartbeat` / `read_heartbeat`) through the shared JSON
  store, including concurrent writers and a write that fails;
* the wiring - the scheduler loop and the watcher thread really do advance
  their own timestamp, and the WebUI renders the result without a network
  call.

Deliberately *not* tested: that a stale heartbeat affects Docker health. It
must not, and `utils/healthcheck.py` is asserted to stay a pure `/healthz`
probe.
"""

import json
import multiprocessing
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

from utils import background_health  # noqa: E402


# ===========================================================================
# The rule
# ===========================================================================

NOW = datetime(2026, 11, 7, 11, 0)


def _heartbeat(component, field, when):
    return {component: {field: when.isoformat()}}


def test_a_missing_record_is_never_and_not_healthy():
    """No evidence yet must not be reported as "everything is fine"."""
    health = background_health.component_health({}, "scheduler", NOW, 60)

    assert health["state"] == "never"
    assert health["healthy"] is False
    assert health["age_label"] == "never"
    # The cause is carried, so an unreadable file is distinguishable from a
    # component that has not started yet.
    assert "no last_loop recorded" in health["note"]


def test_a_recent_pass_is_live():
    beat = _heartbeat("scheduler", "last_loop", NOW - timedelta(minutes=12))
    health = background_health.component_health(beat, "scheduler", NOW, 60)

    assert health["state"] == "live"
    assert health["healthy"] is True
    assert health["age_minutes"] == 12
    assert health["age_label"] == "12 min ago"


def test_stale_is_twice_the_interval_plus_grace_and_the_boundary_is_inclusive():
    """
    Threshold is 2x the expected interval plus an hour of grace. Two intervals
    covers a sleep that began just after the previous pass; the grace absorbs
    one slow scan. Off-by-one matters: at exactly the threshold this is still
    live, one minute later it is stale.
    """
    threshold = 60 * 2 + background_health.GRACE_MINUTES  # 180

    on_time = _heartbeat(
        "scheduler", "last_loop", NOW - timedelta(minutes=threshold)
    )
    health = background_health.component_health(on_time, "scheduler", NOW, 60)
    assert health["threshold_minutes"] == threshold
    assert health["state"] == "live", "the boundary itself must not be stale"

    late = _heartbeat(
        "scheduler", "last_loop", NOW - timedelta(minutes=threshold + 1)
    )
    health = background_health.component_health(late, "scheduler", NOW, 60)
    assert health["state"] == "stale"
    assert health["healthy"] is False


def test_quiet_hours_widen_the_threshold_so_overnight_is_not_stale():
    """
    Quiet hours sleep for 120 minutes. A scheduler that is behaving correctly
    at 03:00 must not be called stale, which is why the interval is chosen by
    the caller (the scheduler's own rule) rather than hard-coded here.
    """
    beat = _heartbeat("scheduler", "last_loop", datetime(2026, 11, 10, 3, 0))

    daytime = background_health.component_health(
        beat, "scheduler", datetime(2026, 11, 10, 8, 0), 360
    )
    assert daytime["threshold_minutes"] == 780
    assert daytime["state"] == "live"

    quiet = background_health.component_health(
        beat, "scheduler", datetime(2026, 11, 10, 8, 0), 120
    )
    assert quiet["state"] == "live"


def test_an_unparseable_timestamp_is_never_and_says_why():
    health = background_health.component_health(
        {"scheduler": {"last_loop": "not a date"}}, "scheduler", NOW, 60
    )
    assert health["state"] == "never"
    assert "not a timestamp" in health["note"]


def test_a_non_object_component_entry_is_treated_as_absent():
    health = background_health.component_health(
        {"scheduler": "garbage"}, "scheduler", NOW, 60
    )
    assert health["state"] == "never"


def test_scheduler_progress_is_the_loop_not_the_last_search():
    """
    ``last_loop`` proves the loop is turning; ``last_check`` only moves when a
    search actually ran. Off-weekend an hour-old ``last_check`` is normal, so
    judging on it would report a healthy scheduler as stale for days.
    """
    heartbeat = {
        "scheduler": {
            "last_loop": (NOW - timedelta(minutes=3)).isoformat(),
            "last_check": (NOW - timedelta(days=2)).isoformat(),
        }
    }
    health = background_health.component_health(heartbeat, "scheduler", NOW, 360)
    assert health["state"] == "live"
    assert health["age_minutes"] == 3


def test_overall_verdict_uses_the_worst_component():
    fresh = (NOW - timedelta(minutes=1)).isoformat()
    stale = (NOW - timedelta(days=2)).isoformat()

    both_fresh = background_health.background_health(
        {"scheduler": {"last_loop": fresh}, "watcher": {"last_scan": fresh}},
        NOW, in_weekend=False,
    )
    assert both_fresh["overall"] == "live"

    dead_watcher = background_health.background_health(
        {"scheduler": {"last_loop": fresh}, "watcher": {"last_scan": stale}},
        NOW, in_weekend=False,
    )
    assert dead_watcher["overall"] == "stale"
    assert dead_watcher["components"]["scheduler"]["state"] == "live"
    assert dead_watcher["components"]["watcher"]["state"] == "stale"

    nothing = background_health.background_health({}, NOW, in_weekend=False)
    assert nothing["overall"] == "never"


def test_quiet_hours_and_intervals_come_from_the_scheduler_itself(monkeypatch):
    """
    The thresholds are the scheduler's own numbers. A second copy of the
    window here would drift, and then report a healthy scheduler as stale
    every night it behaved correctly.
    """
    import motogp_scheduler

    monkeypatch.setitem(
        motogp_scheduler.CONFIG["quiet_hours"], "enabled", True
    )
    # Default quiet hours are 01:00-06:00.
    assert background_health.quiet_hours_at(datetime(2026, 11, 10, 3, 0)) is True
    assert background_health.quiet_hours_at(datetime(2026, 11, 10, 12, 0)) is False

    quiet = background_health.scheduler_interval_minutes(
        datetime(2026, 11, 10, 3, 0), in_weekend=False, quiet=True
    )
    assert quiet == motogp_scheduler.CONFIG["quiet_hours"]["interval_minutes"]

    weekend = background_health.scheduler_interval_minutes(
        datetime(2026, 11, 7, 12, 0), in_weekend=True
    )
    assert weekend == motogp_scheduler.CONFIG["race_weekend_interval_minutes"]


# ===========================================================================
# The record
# ===========================================================================

def _beat_worker(config_dir, component, field, barrier):
    barrier.wait(timeout=30)
    background_health.write_heartbeat(
        Path(config_dir), component, **{field: datetime.now().isoformat()}
    )


def test_writes_merge_so_the_scheduler_and_watcher_do_not_clobber_each_other():
    """
    The scheduler and the watcher (a thread) write on their own schedules.
    One component's write must not erase the other's.
    """
    config_dir = Path(tempfile.mkdtemp(prefix="motogp-heartbeat-"))
    try:
        background_health.write_heartbeat(
            config_dir, "scheduler", last_loop=NOW.isoformat()
        )
        background_health.write_heartbeat(
            config_dir, "watcher", last_scan=NOW.isoformat()
        )

        stored = background_health.read_heartbeat(config_dir)
        assert stored["scheduler"]["last_loop"] == NOW.isoformat()
        assert stored["watcher"]["last_scan"] == NOW.isoformat()
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


def test_concurrent_writers_from_separate_processes_are_both_retained():
    """Both components are recorded even when they write at the same instant."""
    config_dir = Path(tempfile.mkdtemp(prefix="motogp-heartbeat-"))
    try:
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        procs = [
            ctx.Process(
                target=_beat_worker,
                args=(str(config_dir), "scheduler", "last_loop", barrier),
            ),
            ctx.Process(
                target=_beat_worker,
                args=(str(config_dir), "watcher", "last_scan", barrier),
            ),
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=60)

        stored = background_health.read_heartbeat(config_dir)
        assert "last_loop" in stored.get("scheduler", {})
        assert "last_scan" in stored.get("watcher", {})
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


def test_a_failed_write_is_logged_and_reported_not_raised(caplog):
    """
    The caller is a monitoring loop. Monitoring that dies because monitoring
    failed is a worse outage than a missing heartbeat, so the failure is
    returned and logged instead of raised.
    """
    config_dir = Path(tempfile.mkdtemp(prefix="motogp-heartbeat-"))
    try:
        # A directory where the heartbeat file should be makes every write fail.
        background_health.heartbeat_path(config_dir).mkdir()

        with caplog.at_level("ERROR"):
            ok = background_health.write_heartbeat(
                config_dir, "scheduler", last_loop=NOW.isoformat()
            )

        assert ok is False
        assert any(
            "Could not write the scheduler heartbeat" in record.message
            for record in caplog.records
        )
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


def test_corrupt_heartbeat_file_is_reported_as_never_not_healthy():
    config_dir = Path(tempfile.mkdtemp(prefix="motogp-heartbeat-"))
    try:
        background_health.heartbeat_path(config_dir).write_text("{not json")

        stored = background_health.read_heartbeat(config_dir)
        assert stored == {}
        health = background_health.background_health(stored, NOW, in_weekend=True)
        assert health["overall"] == "never"
        assert health["components"]["scheduler"]["healthy"] is False
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


def test_unknown_components_and_fields_are_rejected():
    """A typo must fail loudly here, not silently record nothing."""
    config_dir = Path(tempfile.mkdtemp(prefix="motogp-heartbeat-"))
    try:
        with pytest.raises(ValueError):
            background_health.write_heartbeat(config_dir, "schedular", last_loop="x")
        with pytest.raises(ValueError):
            background_health.write_heartbeat(config_dir, "watcher", last_loop="x")
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


def test_junk_timestamps_are_not_stored():
    config_dir = Path(tempfile.mkdtemp(prefix="motogp-heartbeat-"))
    try:
        assert background_health.write_heartbeat(
            config_dir, "watcher", last_scan="definitely not a time"
        ) is False
        assert not background_health.heartbeat_path(config_dir).exists()
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


def test_the_heartbeat_file_is_a_documented_config_store():
    """
    It lives in /config like every other shared store, and is disposable.

    Asserted on the *namespaced* name since the hybrid step-4 change: the
    heartbeat carries its series like the other five stores, so a doc that
    still described the unprefixed file would be describing the collision.
    """
    architecture = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text()
    assert "background_health.<series>.json" in architecture


# ===========================================================================
# The wiring
# ===========================================================================

def test_the_watcher_advances_its_own_timestamp_on_every_scan():
    """
    The point of the record: prove the thread scanned. An empty watch
    directory is the normal case and must still beat - "nothing to do" is not
    "not running".
    """
    from motogp_watcher import DownloadWatcher

    config_dir = Path(tempfile.mkdtemp(prefix="motogp-watcher-beat-"))
    downloads = Path(tempfile.mkdtemp(prefix="motogp-watcher-dl-"))
    try:
        watcher = DownloadWatcher(
            watch_dir=str(downloads),
            processor=lambda folder: True,
            config_dir=str(config_dir),
            library_path=str(config_dir / "library"),
            verify_library=False,
        )
        assert background_health.read_heartbeat(config_dir) == {}

        watcher._scan_directory()

        stored = background_health.read_heartbeat(config_dir)
        assert "last_scan" in stored["watcher"]
        datetime.fromisoformat(stored["watcher"]["last_scan"])
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)
        shutil.rmtree(downloads, ignore_errors=True)


def test_the_watcher_beats_even_when_the_downloads_path_is_missing():
    """
    A missing downloads path is reported as its own problem. It must not also
    look like a dead thread, or two faults get conflated into one.
    """
    from motogp_watcher import DownloadWatcher

    config_dir = Path(tempfile.mkdtemp(prefix="motogp-watcher-beat-"))
    try:
        watcher = DownloadWatcher(
            watch_dir=str(config_dir / "does-not-exist"),
            processor=lambda folder: True,
            config_dir=str(config_dir),
            library_path=str(config_dir / "library"),
            verify_library=False,
        )
        watcher._scan_directory()

        assert "last_scan" in background_health.read_heartbeat(config_dir)["watcher"]
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


def test_the_scheduler_records_start_and_loop_progress(monkeypatch, tmp_path):
    """
    Run the real loop for one iteration rather than trusting the source: the
    heartbeat must come from the loop, and must be written even off-weekend
    when no search runs at all.
    """
    import motogp_scheduler

    monkeypatch.setattr(motogp_scheduler, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(motogp_scheduler, "write_heartbeat",
                        background_health.write_heartbeat)
    monkeypatch.setattr(motogp_scheduler, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(
        motogp_scheduler, "load_calendar", lambda season, **kw: {"rounds": []}
    )
    monkeypatch.setattr(motogp_scheduler, "get_current_race_weekend", lambda cal, *a: None)
    monkeypatch.setattr(motogp_scheduler, "get_next_race_weekend", lambda cal, *a: None)
    monkeypatch.setattr(motogp_scheduler, "get_current_season", lambda: 2026)
    monkeypatch.setattr(motogp_scheduler, "get_search_delay_hours", lambda: 1)
    monkeypatch.setattr(motogp_scheduler, "get_series_downloads_path", lambda *a, **kw: str(tmp_path))
    monkeypatch.setattr(motogp_scheduler, "get_series_library_path", lambda *a, **kw: str(tmp_path))

    # Stop the loop after exactly one iteration, from inside the sleep.
    class _Stop(Exception):
        pass

    def _stop(_seconds):
        raise _Stop

    monkeypatch.setattr(motogp_scheduler.time, "sleep", _stop)

    # Driven as a single pass rather than through main(): main() deliberately
    # contains a failing series so it cannot stop the others, which would
    # swallow this sentinel. The heartbeat contract belongs to the pass.
    with pytest.raises(_Stop):
        motogp_scheduler._series_pass(
            "motogp", None, motogp_scheduler.SchedulerState()
        )

    # The heartbeat is per series: one shared record would let the second
    # series' loop mask the first one stalling.
    stored = background_health.read_heartbeat(tmp_path, series="motogp")
    assert "started" in stored["scheduler"]
    assert "last_loop" in stored["scheduler"]
    # Off-weekend: no search ran, so there is no check - and that must not
    # stop the loop from proving it is awake.
    assert "last_check" not in stored["scheduler"]


def test_the_docker_probe_never_reads_the_heartbeat():
    """
    Staleness is a warning, not a restart. If the probe read the heartbeat, a
    slow-but-legitimate search could put the container into a restart loop.
    """
    text = (REPO_ROOT / "utils" / "healthcheck.py").read_text()
    assert "background_health" not in text
    assert "background_health.json" not in text
    # It stays a single HTTP request to a public route.
    assert "/healthz" in text
    assert "urllib.request" in text


def test_diagnostics_reports_background_progress_without_credential_values(
    monkeypatch,
):
    import motogp_webui

    config_dir = Path(tempfile.mkdtemp(prefix="motogp-webui-bg-"))
    try:
        background_health.write_heartbeat(
            config_dir, "scheduler",
            last_loop=(datetime.now() - timedelta(minutes=2)).isoformat(),
            started=datetime.now().isoformat(),
        )
        background_health.write_heartbeat(
            config_dir, "watcher",
            last_scan=(datetime.now() - timedelta(minutes=2)).isoformat(),
        )
        monkeypatch.setattr(motogp_webui, "CONFIG_DIR", config_dir)
        monkeypatch.setattr(
            motogp_webui, "load_calendar_data",
            lambda season, *a, **kw: {"rounds": []}
        )
        motogp_webui.app.config.update(TESTING=True)

        with motogp_webui.app.test_client() as client:
            payload = client.get("/diagnostics").get_json()

        assert payload["background"]["overall"] == "live"
        assert payload["background"]["components"]["scheduler"]["state"] == "live"
        assert payload["background"]["components"]["watcher"]["state"] == "live"
        # A non-disclosure check, matching the route's existing contract.
        body = json.dumps(payload)
        assert "plex-token" not in body
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


def test_home_health_strip_renders_loop_and_watcher_progress(monkeypatch):
    """
    The template is the only place the rule becomes visible, so a typo there
    must fail here rather than render an empty chip in production.
    """
    import motogp_webui

    config_dir = Path(tempfile.mkdtemp(prefix="motogp-webui-home-"))
    library = Path(tempfile.mkdtemp(prefix="motogp-webui-lib-"))
    try:
        fresh = (datetime.now() - timedelta(minutes=4)).isoformat()
        cut_off = (datetime.now() - timedelta(days=3)).isoformat()
        background_health.write_heartbeat(config_dir, "scheduler", last_loop=fresh)
        background_health.write_heartbeat(config_dir, "watcher", last_scan=cut_off)

        monkeypatch.setattr(motogp_webui, "CONFIG_DIR", config_dir)
        monkeypatch.setattr(motogp_webui, "WEBUI_CONFIG_FILE", config_dir / "w.json")
        monkeypatch.setattr(motogp_webui, "library_root", lambda *a, **kw: str(library))
        monkeypatch.setattr(
            motogp_webui, "load_calendar_data",
            lambda season, *a, **kw: {"rounds": []}
        )
        monkeypatch.setattr(
            motogp_webui, "get_rounds_for_season", lambda season, *a, **kw: []
        )
        motogp_webui.app.config.update(TESTING=True)

        with motogp_webui.app.test_client() as client:
            response = client.get("/")

        body = response.get_data(as_text=True)
        assert response.status_code == 200
        # The scheduler loop is healthy...
        assert "Loop: last pass 4 min ago" in body
        # ...and the dead watcher is visible as its own fault, not hidden.
        assert "Watcher: last scan 3 d" in body
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)
        shutil.rmtree(library, ignore_errors=True)
