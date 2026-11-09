"""Shared JSON state must survive concurrent processes without lost updates.

Slices 1-2 of docs/IMPROVEMENT_PLAN.md: every /config JSON store now goes
through utils.json_store - a sidecar lock held across the whole
read-modify-write, a unique temp file, and atomic replacement. Before this,
the activity emitter read before locking and locked only a shared temp file
while serialising, and the grabber/watcher/WebUI stores were written with a
bare open("w"), so overlapping processes could overwrite each other's
updates or expose torn JSON to readers.
"""

import json
import logging
import multiprocessing

import pytest

from utils import json_store


def _update_worker(path, key, value, barrier):
    barrier.wait(timeout=30)
    def mutate(data):
        data[key] = value
    json_store.update_json(path, dict, mutate)


def _activity_worker(key, barrier):
    import activity_emitter

    barrier.wait(timeout=30)
    activity_emitter.emit_activity(
        episode_key=key,
        episode_name=f"Session {key}",
        stage=activity_emitter.ActivityStage.SEARCHING,
    )


def test_concurrent_updates_from_separate_processes_are_both_retained(tmp_path):
    """Two processes updating different keys must not drop each other's work."""
    path = tmp_path / "state.json"
    path.write_text("{}")
    barrier = multiprocessing.Barrier(2)
    procs = [
        multiprocessing.Process(target=_update_worker, args=(path, "a", 1, barrier)),
        multiprocessing.Process(target=_update_worker, args=(path, "b", 2, barrier)),
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=30)
        assert proc.exitcode == 0
    assert json.loads(path.read_text()) == {"a": 1, "b": 2}


def test_lock_is_released_when_the_mutator_raises(tmp_path):
    """A failed transaction must not wedge the store for the next writer."""
    path = tmp_path / "state.json"
    path.write_text('{"kept": true}')

    def boom(data):
        raise RuntimeError("mutator failed")

    with pytest.raises(RuntimeError):
        json_store.update_json(path, dict, boom)

    # Previous valid file is untouched, and a later update still proceeds.
    assert json.loads(path.read_text()) == {"kept": True}
    json_store.update_json(path, dict, lambda d: d.update({"after": 1}))
    assert json.loads(path.read_text()) == {"kept": True, "after": 1}


def test_failed_replacement_leaves_previous_file_readable(tmp_path, monkeypatch):
    """If the atomic replace fails, readers still see the last good file."""
    path = tmp_path / "state.json"
    path.write_text('{"version": 1}')

    def failing_replace(src, dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(json_store.os, "replace", failing_replace)
    with pytest.raises(OSError):
        json_store.update_json(path, dict, lambda d: d.update({"version": 2}))

    assert json.loads(path.read_text()) == {"version": 1}
    # No half-written temp file is left behind.
    assert list(tmp_path.glob("*.tmp")) == []


def test_corrupt_file_is_logged_at_error_and_defaulted(tmp_path, caplog):
    """Corrupt state must be loud: silently treating it as empty hides breakage."""
    path = tmp_path / "state.json"
    path.write_text("{not json")
    with caplog.at_level(logging.ERROR, logger="msportarr.json_store"):
        result = json_store.read_json(path, {"empty": True})
    assert result == {"empty": True}
    assert str(path) in caplog.text


def test_lock_file_is_a_stable_sidecar(tmp_path):
    path = tmp_path / "grabber_state.json"
    json_store.write_json(path, {"a": 1})
    assert (tmp_path / "grabber_state.lock").exists()
    assert json.loads(path.read_text()) == {"a": 1}


def test_three_way_merge_keeps_concurrent_additions_and_our_deletions():
    base = {"grabbed": {"A": 1, "B": 2}, "last_run": "t0"}
    ours = {"grabbed": {"A": 1}, "last_run": "t1"}          # we deleted B
    disk = {"grabbed": {"A": 1, "B": 2, "C": 3}, "last_run": "t0"}  # they added C
    merged = json_store.three_way_merge(base, ours, disk)
    assert merged == {"grabbed": {"A": 1, "C": 3}, "last_run": "t1"}


def test_three_way_merge_keeps_disk_change_to_a_key_we_deleted():
    base = {"grabbed": {"B": 2}}
    ours = {"grabbed": {}}
    disk = {"grabbed": {"B": 99}}  # changed after our snapshot: keep theirs
    assert json_store.three_way_merge(base, ours, disk) == {"grabbed": {"B": 99}}


# ---------------------------------------------------------------------------
# The activity store (Slice 1)
# ---------------------------------------------------------------------------

def test_concurrent_activity_emitters_retain_both_events(tmp_path, monkeypatch):
    import activity_emitter

    monkeypatch.setattr(activity_emitter, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(activity_emitter, "ACTIVITY_FILE",
                        tmp_path / "activity_events.motogp.json")

    barrier = multiprocessing.Barrier(2)
    procs = [
        multiprocessing.Process(target=_activity_worker, args=("2026_R01_E01", barrier)),
        multiprocessing.Process(target=_activity_worker, args=("2026_R01_E02", barrier)),
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=30)
        assert proc.exitcode == 0

    active = activity_emitter.get_active_events()
    assert set(active) == {"2026_R01_E01", "2026_R01_E02"}


def test_activity_failed_write_is_logged_not_silent(tmp_path, monkeypatch, caplog):
    import activity_emitter

    monkeypatch.setattr(activity_emitter, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(activity_emitter, "ACTIVITY_FILE",
                        tmp_path / "activity_events.motogp.json")

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(json_store.os, "replace", failing_replace)
    with caplog.at_level(logging.ERROR, logger="msportarr.activity"):
        activity_emitter.emit_activity(
            episode_key="2026_R01_E01",
            episode_name="MotoGP Race",
            stage=activity_emitter.ActivityStage.SEARCHING,
        )
    assert "not recorded" in caplog.text


# ---------------------------------------------------------------------------
# Grabber and watcher stores (Slice 2)
# ---------------------------------------------------------------------------

def test_overlapping_grabber_states_merge_on_save(tmp_path):
    """A scheduled and a manual grabber saving over each other lose nothing."""
    from motogp_grabber import GrabberState

    state_file = tmp_path / "grabber_state.json"
    scheduled = GrabberState(state_file)
    manual = GrabberState(state_file)

    scheduled.mark_grabbed("2026_R01_E01", "Release A", mode="scheduled")
    manual.mark_grabbed("2026_R01_E02", "Release B", mode="backfill")
    scheduled.save()
    manual.save()

    reloaded = GrabberState(state_file)
    assert set(reloaded.state["grabbed"]) == {"2026_R01_E01", "2026_R01_E02"}


def test_grabber_clear_session_stays_cleared_after_merge(tmp_path):
    from motogp_grabber import GrabberState

    state_file = tmp_path / "grabber_state.json"
    first = GrabberState(state_file)
    first.mark_grabbed("2026_R01_E01", "Release A")
    first.save()

    clearer = GrabberState(state_file)
    other = GrabberState(state_file)
    clearer.clear_session("2026_R01_E01")
    clearer.save()
    other.mark_searched("2026_R01_E09")
    other.save()

    reloaded = GrabberState(state_file)
    assert "2026_R01_E01" not in reloaded.state["grabbed"]
    assert "2026_R01_E09" in reloaded.state["searched"]


def test_overlapping_watcher_states_merge_on_save(tmp_path):
    from motogp_watcher import StateManager

    scheduled = StateManager(tmp_path)
    manual = StateManager(tmp_path)
    scheduled.mark_processed("folder-a")
    manual.mark_processed("folder-b")

    reloaded = StateManager(tmp_path)
    assert {"folder-a", "folder-b"} <= reloaded.state.processed


def test_watcher_save_never_leaves_partial_json(tmp_path):
    from motogp_watcher import StateManager

    manager = StateManager(tmp_path)
    manager.mark_failed("folder-x", "boom")
    data = json.loads((tmp_path / "watcher_state.motogp.json").read_text())
    assert data["failed"]["folder-x"]["attempt_count"] == 1
    assert (tmp_path / "watcher_state.motogp.lock").exists()


def test_webui_settings_write_is_atomic_and_locked(tmp_path, monkeypatch):
    import motogp_webui

    target = tmp_path / "webui_settings.motogp.json"
    monkeypatch.setattr(motogp_webui, "WEBUI_CONFIG_FILE", target)
    motogp_webui.save_webui_settings({"sessions": {"motogp_race": True}})
    assert json.loads(target.read_text()) == {"sessions": {"motogp_race": True}}
    assert (tmp_path / "webui_settings.motogp.lock").exists()
