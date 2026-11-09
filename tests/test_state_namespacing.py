"""
Step 4: every shared /config state file is namespaced to its series.

This is the one change in the hybrid plan that is *only* a rename, and it is
also the one that has to be right before F1 writes anything at all. Both apps
historically wrote ``grabber_state.json`` into one ``/config``; merged, they
would overwrite each other's entries within a day, and nothing would report it -
history would simply stop accumulating.

Three things are pinned:

  * the naming rule has exactly one implementation (the registry's), and the
    helpers reach it rather than re-spelling it;
  * the old files are migrated once, by rename, and only for the series that
    ever wrote them;
  * a migration never destroys data - a namespaced file that already exists
    wins, and the legacy one is left alone for the user to look at.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import activity_emitter
import grab_stats
import motogp_common as common
import motogp_grabber
import motogp_webui
import motogp_watcher
from series import default_series, get_series, runtime
from utils import background_health, release_mismatches
from utils.state_files import (
    STATE_FILES,
    migrate_default_series_state,
    state_filename,
    state_files_in_use,
    state_path,
)


# ---------------------------------------------------------------------------
# One naming rule
# ---------------------------------------------------------------------------

def test_the_filename_comes_from_the_registry():
    """The format is written down once, in SeriesRuntime.state_file()."""
    for spec in (get_series("motogp"), get_series("f1")):
        rnd = runtime(spec.key)
        assert state_filename("grabber_state", spec.key) == \
            rnd.state_file("grabber_state")
        assert state_filename("grabber_state", spec.key) == \
            f"grabber_state.{spec.state_prefix}.json"


def test_the_two_series_never_share_a_state_filename():
    """
    The whole point. This is the assertion that would have caught the collision
    before the F1 series existed.
    """
    motogp = runtime("motogp")
    f1 = runtime("f1")
    for name in STATE_FILES:
        assert motogp.state_file(name) != f1.state_file(name), name


def test_the_default_series_keeps_its_historical_name_for_reading():
    """
    ``grabber_state.json`` is what a pre-hybrid install has on disk.

    It is a *default series* fact, not a MotoGP fact: the series that has media
    already is the only one with state to migrate, and a series added later must
    not inherit a filename it never wrote.
    """
    assert runtime(default_series().key).legacy_state_file("grabber_state") == \
        "grabber_state.json"
    with pytest.raises(ValueError, match="no legacy state file"):
        runtime("f1").legacy_state_file("grabber_state")


# ---------------------------------------------------------------------------
# The call sites actually use it
# ---------------------------------------------------------------------------

def test_every_shared_state_path_is_namespaced():
    """
    One assertion per module, against the real module-level binding.

    A rename that missed a call site is invisible until two series run: the
    second one silently reads the first one's file. Naming each binding here
    makes a missed site a test failure instead.
    """
    names = {
        "grabber": (motogp_grabber.STATE_FILE, "grabber_state"),
        "webui": (motogp_webui.WEBUI_CONFIG_FILE, "webui_settings"),
        "activity": (activity_emitter.ACTIVITY_FILE, "activity_events"),
        "grab_stats": (grab_stats.STATE_FILE, "grabber_state"),
        "mismatches": (release_mismatches.MISMATCH_FILE, "release_mismatches"),
    }
    for label, (path, stem) in names.items():
        assert path.name == state_filename(stem), label

    heartbeat = background_health.heartbeat_path("/config")
    assert heartbeat.name == state_filename("background_health")

    # The mismatch store's sidecar lock follows the data file, or two writers
    # lock different files while believing they are serialised.
    assert release_mismatches.LOCK_FILE.name == \
        state_filename("release_mismatches").replace(".json", ".lock")


def test_the_watcher_namespaces_its_state_file(tmp_path):
    manager = motogp_watcher.StateManager(tmp_path)
    manager.mark_failed("folder-x", "boom")
    assert (tmp_path / "watcher_state.motogp.json").is_file()
    assert not (tmp_path / "watcher_state.json").exists()
    # ...and the sidecar lock is namespaced with it.
    assert (tmp_path / "watcher_state.motogp.lock").exists()


def test_each_watcher_keeps_its_own_state(tmp_path):
    """
    The second series' watcher must not write the default series' state.

    Both watchers run inside one scheduler process, so the binding cannot
    answer this: the state file has to come from the watcher's own series or
    the two folders' histories overwrite each other, and a folder F1 processed
    looks already-done to MotoGP.
    """
    motogp = motogp_watcher.StateManager(tmp_path)
    f1 = motogp_watcher.StateManager(tmp_path, series="f1")
    assert motogp.state_file.name == "watcher_state.motogp.json"
    assert f1.state_file.name == "watcher_state.f1.json"

    f1.mark_failed("folder-f1", "boom")

    assert (tmp_path / "watcher_state.f1.json").is_file()
    # MotoGP's record is untouched: F1's failure never appears in it.
    assert not (tmp_path / "watcher_state.motogp.json").exists()


def test_the_desired_sessions_reader_uses_the_namespaced_file(tmp_path,
                                                              monkeypatch):
    """
    Two series each keep their own desired-session preferences.

    Sharing this file would be the nastiest collision of the set: F1 saving its
    preferences would rewrite MotoGP's, and the symptom is "some sessions
    stopped downloading" after the other championship was configured.
    """
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(common, "DEFAULT_DESIRED_SESSIONS",
                        {common.SessionType.MOTOGP_RACE: False})

    (tmp_path / "webui_settings.json").write_text(
        json.dumps({"sessions": {"MOTOGP_RACE": True}}), encoding="utf-8")
    assert common.get_desired_sessions()[common.SessionType.MOTOGP_RACE] is False, \
        "the un-namespaced file must not be read"

    (tmp_path / "webui_settings.motogp.json").write_text(
        json.dumps({"sessions": {"MOTOGP_RACE": True}}), encoding="utf-8")
    assert common.get_desired_sessions()[common.SessionType.MOTOGP_RACE] is True


def test_f1_desired_sessions_use_the_f1_document_and_enum(tmp_path, monkeypatch):
    """The scheduler's explicit F1 lookup must not read MotoGP preferences."""
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    f1 = runtime("f1")
    sessions = {
        session.name: session is f1.session_type.RACE
        for session in f1.session_type
        if session is not f1.session_type.UNKNOWN
    }
    (tmp_path / "webui_settings.f1.json").write_text(
        json.dumps({"sessions": sessions}), encoding="utf-8"
    )

    desired = common.get_desired_sessions(series="f1")

    assert desired[f1.session_type.RACE] is True
    assert all(
        desired[session] is False
        for session in f1.session_type
        if session is not f1.session_type.UNKNOWN and session is not f1.session_type.RACE
    )


def test_f1_desired_sessions_read_legacy_preferences_from_the_default_series_file(
    tmp_path, monkeypatch,
):
    """Older F1 preferences stored as ``f1_sessions`` remain readable."""
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    f1 = runtime("f1")
    default_key = default_series().key
    (tmp_path / f"webui_settings.{default_key}.json").write_text(
        json.dumps({"f1_sessions": {"RACE": True}}), encoding="utf-8"
    )

    desired = common.get_desired_sessions(series="f1")

    assert desired[f1.session_type.RACE] is True


# ---------------------------------------------------------------------------
# The one-time migration
# ---------------------------------------------------------------------------

def test_the_migration_renames_every_legacy_file(tmp_path):
    for name in state_files_in_use():
        (tmp_path / f"{name}.json").write_text(
            json.dumps({"from": "before the rename"}), encoding="utf-8")

    migrated = migrate_default_series_state(tmp_path)

    assert {path.name for path in migrated} == {
        state_filename(name) for name in state_files_in_use()
    }
    for path in migrated:
        assert json.loads(path.read_text()) == {"from": "before the rename"}
        assert not (tmp_path / path.name.replace(".motogp", "")).exists(), \
            "the rename must MOVE the file, not copy it"


def test_the_migration_is_idempotent(tmp_path):
    (tmp_path / "grabber_state.json").write_text("{}", encoding="utf-8")
    assert len(migrate_default_series_state(tmp_path)) == 1
    assert migrate_default_series_state(tmp_path) == []


def test_a_namespaced_file_always_wins_over_a_legacy_one(tmp_path):
    """
    Two files, two contents - the rename must not pick one and delete the other.

    That is a data question for the user, not something startup should settle.
    The namespaced file is what the app is already reading, so it wins, and the
    legacy file stays where it is.
    """
    (tmp_path / "grabber_state.json").write_text(
        json.dumps({"entry": "legacy"}), encoding="utf-8")
    (tmp_path / "grabber_state.motogp.json").write_text(
        json.dumps({"entry": "current"}), encoding="utf-8")

    assert migrate_default_series_state(tmp_path, ["grabber_state"]) == []
    assert json.loads((tmp_path / "grabber_state.motogp.json").read_text()) == \
        {"entry": "current"}
    assert json.loads((tmp_path / "grabber_state.json").read_text()) == \
        {"entry": "legacy"}


def test_a_missing_config_directory_is_not_an_error(tmp_path):
    """A fresh install has no state files at all; that is the normal case."""
    empty = tmp_path / "nothing-here"
    assert migrate_default_series_state(empty) == []


def test_an_unnamespaced_file_belonging_to_another_series_is_left_alone(tmp_path):
    """
    Only the default series reads ``<name>.json``.

    If a deployment somehow has an F1 series first, its state must not be
    adopted as MotoGP's - the migration is keyed on the default series, so the
    file is simply not a legacy file for anyone yet.
    """
    f1 = runtime("f1")
    (tmp_path / f"grabber_state.{f1.series.state_prefix}.json").write_text(
        json.dumps({"entry": "f1"}), encoding="utf-8")
    assert migrate_default_series_state(tmp_path, ["grabber_state"]) == []
    assert json.loads(
        (tmp_path / "grabber_state.f1.json").read_text()) == {"entry": "f1"}


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

def test_the_new_state_names_are_git_ignored():
    """
    A checkout that runs the app writes these into the repo's config/.

    One of them (the run lock) was committed during this change simply because
    nothing ignored it, and a tracked runtime file survives .dockerignore and
    can be copied into an image.
    """
    root = REPO_ROOT
    ignored = (root / ".gitignore").read_text(encoding="utf-8")
    assert "config/*.lock" in ignored
    assert "config/background_health.json" in ignored
    assert "config/submission_intents.json" in ignored


def test_both_process_entry_points_run_the_migration(tmp_path, monkeypatch,
                                                     capsys):
    """
    The entrypoint is not the only way this program is started.

    ``entrypoint.sh`` migrates before either process exists, which is the only
    point at which it is purely a rename. But the WebUI and the scheduler can
    be run directly - by a developer, by an operator, or by a script - and then
    nothing would have moved the old files, so the app would start with an
    empty history and say nothing. Both entry points therefore run the same
    idempotent migration, and both say so when it moves something.
    """
    import motogp_scheduler

    (tmp_path / "grabber_state.json").write_text(
        json.dumps({"grabbed": {"2026_R07_E82": {}}}), encoding="utf-8")

    for module in (motogp_webui, motogp_scheduler):
        monkeypatch.setattr(module, "CONFIG_DIR", tmp_path)
        module._ensure_state_files_are_namespaced()
        assert (tmp_path / "grabber_state.motogp.json").is_file(), module.__name__

    out = capsys.readouterr().out
    assert "moved legacy state" in out

    # Idempotent: a second run (the other module) must not re-print or fail.
    assert motogp_scheduler._ensure_state_files_are_namespaced() is None


def test_the_migration_helper_never_raises_on_a_broken_directory(tmp_path,
                                                                 monkeypatch):
    """
    Startup must not be refused over a filename.

    The worst case is state that starts empty and says so; an exception here
    would turn "your history is in the old file" into "the container will not
    start".
    """
    import motogp_webui

    monkeypatch.setattr(motogp_webui, "CONFIG_DIR", tmp_path)

    def explode(config_dir, names=None):
        raise OSError("read-only /config")

    monkeypatch.setattr("utils.state_files.migrate_default_series_state", explode)
    assert motogp_webui._ensure_state_files_are_namespaced() is None
