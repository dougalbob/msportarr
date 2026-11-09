"""
One scheduler, several championships.

Before the hybrid this process had one series and therefore one loop, one
watcher, one heartbeat and one plan file. All four are now per series, and every
one of them fails in a way that looks like health if it is shared:

* a single ``while`` loop per series that only ends at shutdown (the first
  implementation) leaves the second championship unserved for a whole weekend,
  while the container reports healthy because the loop it is watching is alive;
* one shared heartbeat lets the second series' writes mask the first stalling;
* one shared ``scheduler_plan.json`` shows whichever series wrote last;
* one watcher on the default series' download folder never sees the other
  series' releases, and one pointing at both sorts them with one set of paths.

The tests here are deliberately about *shape* - who runs, in what order, and
that one series' failure is not another series' outage - because the contents
of a pass are already covered by ``test_scheduler_planning``.
"""

import json
from types import SimpleNamespace

import pytest

import motogp_scheduler as scheduler


def _two_series(monkeypatch):
    """Make ``active_series()`` report MotoGP and F1."""
    specs = [SimpleNamespace(key="motogp"), SimpleNamespace(key="f1")]
    monkeypatch.setattr(
        "utils.config_loader.active_series", lambda: specs, raising=True
    )
    return specs


def _quiet_scheduler(monkeypatch, tmp_path):
    """The pass's I/O, silenced except for what a test is asserting on."""
    monkeypatch.setattr(scheduler, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(scheduler, "reload_settings", lambda: None)
    monkeypatch.setattr(scheduler, "get_current_season", lambda: 2026)
    monkeypatch.setattr(scheduler, "get_search_delay_hours", lambda: 1)
    monkeypatch.setattr(scheduler, "load_calendar", lambda season, **kw: {"rounds": []})
    monkeypatch.setattr(scheduler, "get_current_race_weekend", lambda cal, *a: None)
    monkeypatch.setattr(scheduler, "get_next_race_weekend", lambda cal, *a: None)
    monkeypatch.setattr(
        scheduler, "get_series_downloads_path", lambda key=None: f"/dl/{key}"
    )
    monkeypatch.setattr(
        scheduler, "get_series_library_path", lambda key=None: f"/lib/{key}"
    )
    monkeypatch.setattr(scheduler.signal, "signal", lambda *a: None)
    monkeypatch.setattr(scheduler, "_ensure_state_files_are_namespaced", lambda: None)
    monkeypatch.setattr("sys.argv", ["motogp_scheduler.py", "--no-watcher"])


def test_every_active_series_gets_one_pass_per_cycle(monkeypatch, tmp_path):
    """
    Interleaved, not one series to completion.

    The distinction is the whole bug: a per-series ``while`` loop keeps running
    until shutdown, so with two series the second one never starts. Order is
    asserted, not just the set, because "MotoGP then MotoGP" would satistfy a
    membership check while still starving F1.
    """
    _quiet_scheduler(monkeypatch, tmp_path)
    _two_series(monkeypatch)

    calls = []

    def fake_pass(key, args, state):
        calls.append(key)
        if len(calls) == 4:  # two full cycles
            raise KeyboardInterrupt  # main() treats this as a shutdown
        return 0

    monkeypatch.setattr(scheduler, "_series_pass", fake_pass)

    assert scheduler.main() == 0
    assert calls == ["motogp", "f1", "motogp", "f1"]


def test_one_series_failing_does_not_stop_the_other(monkeypatch, tmp_path):
    """
    A broken F1 calendar (or an unmounted F1 library) is not a reason to stop
    searching for MotoGP. The failing series is logged and retried next cycle.
    """
    _quiet_scheduler(monkeypatch, tmp_path)
    _two_series(monkeypatch)

    passes = []
    errors = []

    def fake_pass(key, args, state):
        passes.append(key)
        if key == "motogp":
            raise RuntimeError("calendar is unreadable")
        if passes.count("f1") == 2:
            raise KeyboardInterrupt
        return 0

    monkeypatch.setattr(scheduler, "_series_pass", fake_pass)
    monkeypatch.setattr(
        scheduler, "get_logger",
        lambda name: SimpleNamespace(
            info=lambda *a, **kw: None,
            debug=lambda *a, **kw: None,
            warning=lambda *a, **kw: None,
            error=lambda *a, **kw: None,
            exception=lambda *a, **kw: errors.append(a),
        ),
    )

    assert scheduler.main() == 0
    # MotoGP failed twice and F1 still ran twice after it.
    assert passes == ["motogp", "f1", "motogp", "f1"]
    assert len(errors) == 2
    assert all("motogp" in args for args in errors)


def test_each_series_records_its_own_heartbeat_and_plan(monkeypatch, tmp_path):
    """
    Per-series records, checked end to end through the real pass.

    A shared heartbeat is the dangerous case: F1 writing ``last_loop`` every
    six hours would keep MotoGP's dead loop looking alive in /diagnostics.
    """
    from utils import background_health

    _quiet_scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr(scheduler, "write_heartbeat", background_health.write_heartbeat)
    monkeypatch.setattr(scheduler.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(scheduler, "get_sleep_interval", lambda in_weekend: 60)

    for key in ("motogp", "f1"):
        assert scheduler._series_pass(key, None, scheduler.SchedulerState()) == 60

    names = {p.name for p in tmp_path.iterdir()}
    for key in ("motogp", "f1"):
        assert f"background_health.{key}.json" in names
        assert f"scheduler_plan.{key}.json" in names
        beat = background_health.read_heartbeat(tmp_path, series=key)
        assert "started" in beat["scheduler"]
        assert "last_loop" in beat["scheduler"]
        assert json.loads(
            (tmp_path / f"scheduler_plan.{key}.json").read_text()
        )["state"] == "off_weekend"

    # Named for the series on disk, so neither can overwrite the other.
    assert "background_health.json" not in names
    assert "scheduler_plan.json" not in names


def test_one_watcher_per_active_series(monkeypatch, tmp_path):
    """
    Each watcher watches its own download folder and names its own series.

    A single watcher could not be fixed by watching two folders: its state file
    records folders it has already processed, so whichever series scanned first
    would mark the other's download as done and the release would never sort.
    """
    _quiet_scheduler(monkeypatch, tmp_path)
    _two_series(monkeypatch)

    created = []

    class FakeWatcher:
        def __init__(self, watch_dir, processor, config_dir, series):
            self.watch_dir = watch_dir
            self.series = series
            self.processor = processor
            created.append(self)

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(
        scheduler, "create_watcher",
        lambda watch_dir, processor, config_dir, series: FakeWatcher(
            watch_dir, processor, config_dir, series
        ),
    )
    monkeypatch.setattr(
        scheduler.time, "sleep",
        lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr("sys.argv", ["motogp_scheduler.py", "--watcher-only"])

    assert scheduler.main() == 0
    assert [(w.series, w.watch_dir) for w in created] == [
        ("motogp", "/dl/motogp"),
        ("f1", "/dl/f1"),
    ]
    # The processor carries its series, so the sorter child bounds its own
    # library rather than sweeping every folder into the default series'.
    assert created[1].processor.keywords == {"series": "f1"}


def test_a_missing_calendar_does_not_raise_out_of_the_pass(monkeypatch, tmp_path):
    """
    The failure a real second series is most likely to hit: no calendar file
    for the season yet. It must cost one series an interval, not the process.
    """
    _quiet_scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr(scheduler, "load_calendar", lambda season, **kw: {})
    monkeypatch.setattr(scheduler, "write_heartbeat", lambda *a, **kw: True)
    monkeypatch.setattr(scheduler.time, "sleep", lambda seconds: None)

    seconds = scheduler._series_pass("f1", None, scheduler.SchedulerState())
    assert seconds > 0


def test_the_series_reaches_child_processes(monkeypatch, tmp_path):
    """
    The grabber and sorter bind their series at import, in their own process.
    The only channel the scheduler has is the environment.
    """
    from series.binding import ENV_VAR

    _quiet_scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr(scheduler, "write_heartbeat", lambda *a, **kw: True)
    monkeypatch.setattr(scheduler.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(scheduler, "get_sleep_interval", lambda in_weekend: 60)
    monkeypatch.delenv(ENV_VAR, raising=False)

    scheduler._series_pass("f1", None, scheduler.SchedulerState())

    import os

    assert os.environ[ENV_VAR] == "f1"


def test_the_plan_file_name_is_built_from_the_pass_argument():
    """
    Guard against the plan file reverting to a shared name.

    This is a source assertion because the failure is silent: one series' plan
    simply replaces the other's, and both look like a scheduler that is running.
    """
    import inspect

    source = inspect.getsource(scheduler._series_pass)
    assert "scheduler_plan.{bound_series}.json" in source


@pytest.mark.parametrize("key", ["motogp", "f1"])
def test_the_pass_never_hardcodes_a_series(key, monkeypatch, tmp_path):
    """
    A pass parameterised by series must not contain a series literal, or the
    second series silently receives the first one's behaviour.
    """
    import inspect

    _quiet_scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr(scheduler, "write_heartbeat", lambda *a, **kw: True)
    monkeypatch.setattr(scheduler.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(scheduler, "get_sleep_interval", lambda in_weekend: 60)

    seen = []
    monkeypatch.setattr(
        scheduler, "get_series_library_path", lambda k=None: seen.append(k) or "/lib"
    )

    scheduler._series_pass(key, None, scheduler.SchedulerState())
    assert key in seen
