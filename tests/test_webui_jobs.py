"""WebUI jobs are typed, validated and single-at-a-time (Slice 3).

/run used to accept {"command": "..."} and split the string into argv after
a racy `running` check. It now accepts a named action with typed fields,
validates them before claiming the singleton job slot, claims that slot
synchronously in the request, and executes an explicit argv list with
bounded output, a recorded exit code and a timeout.
"""

import shutil
import threading

import pytest
import yaml

import motogp_common as common
import motogp_webui
from utils import config_loader
from utils.webui_jobs import JobManager

REPO_ROOT = motogp_webui.SCRIPT_DIR


@pytest.fixture
def client(tmp_path, monkeypatch):
    for cal_file in (REPO_ROOT / "config").glob("motogp_*.json"):
        shutil.copy2(cal_file, tmp_path / cal_file.name)
    library = tmp_path / "library"
    library.mkdir()
    settings_file = tmp_path / "settings.yml"
    settings_file.write_text(yaml.safe_dump({
        "library": {"motogp_path": str(library), "downloads_path": str(tmp_path / "dl")},
    }))
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(motogp_webui, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(motogp_webui, "library_root", lambda *a, **kw: str(library))
    monkeypatch.setattr(motogp_webui, "WEBUI_CONFIG_FILE",
                        tmp_path / "webui_settings.motogp.json")
    monkeypatch.setattr(config_loader, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_loader, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_loader, "_settings_cache", None)
    # Guarantee a free job slot whatever earlier tests left behind.
    motogp_webui.jobs.finish()
    motogp_webui.app.config.update(TESTING=True)
    with motogp_webui.app.test_client() as test_client:
        yield test_client
    motogp_webui.jobs.finish()
    config_loader._settings_cache = None


class FakeProcess:
    def __init__(self, lines=(), exit_code=0):
        self.stdout = iter(list(lines))
        self._exit_code = exit_code
        self.killed = False

    def wait(self):
        return self._exit_code

    def kill(self):
        self.killed = True
        self.stdout = iter(())


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------

def test_build_job_argv_for_every_action(client):
    # Every child process is told which series it is working for, before the
    # subcommand: the grabber binds its vocabulary at import, so an argv
    # without --series is a MotoGP job whatever tab it was started from.
    grabber = [motogp_webui.PYTHON_PATH, str(motogp_webui.GRABBER_SCRIPT),
               "--series", "motogp"]
    overlay = [motogp_webui.PYTHON_PATH, str(motogp_webui.OVERLAY_SCRIPT),
               "--series", "motogp"]

    argv, _ = motogp_webui.build_job({"action": "status"})
    assert argv == grabber + ["status"]

    argv, _ = motogp_webui.build_job({"action": "status", "season": 2026})
    assert argv == grabber + ["status", "--season", "2026"]

    argv, _ = motogp_webui.build_job(
        {"action": "scheduled", "season": 2026, "dry_run": True})
    assert argv == grabber + ["scheduled", "--season", "2026", "--dry-run"]

    argv, _ = motogp_webui.build_job({
        "action": "backfill", "season": 2026, "round": 7,
        "sessions": ["E82", "e71", "E82"], "force": True,
    })
    assert argv == grabber + [
        "backfill", "--season", "2026", "--round", "7",
        "--sessions", "E82,E71", "--force",
    ]

    argv, _ = motogp_webui.build_job(
        {"action": "overlay-season", "season": 2026, "scale": 1.5, "dry_run": True})
    assert argv == overlay + ["apply-season", "2026", "--scale", "1.5", "--dry-run"]

    argv, label = motogp_webui.build_job(
        {"action": "overlay-round", "season": 2026, "round": 7})
    # The path is resolved from the calendar server-side, never client-supplied.
    assert argv[:5] == overlay + ["apply-round"]
    assert "Round 07" in argv[5]
    assert argv[6:] == ["--scale", "1.0"]
    assert label.startswith("motogp-overlay apply-round")


def test_build_job_names_the_series_for_the_other_championship():
    """
    The same action, aimed at the other series: different calendar, different
    library, and the F1 overlay must resolve F1's round folder.

    Checked explicitly rather than trusted to the default, because the failure
    is silent - a job built for the wrong series succeeds and writes into the
    wrong library.
    """
    argv, label = motogp_webui.build_job({"action": "status"}, series="f1")
    assert argv[2:4] == ["--series", "f1"]
    assert label.startswith("f1-grab")

    argv, _ = motogp_webui.build_job(
        {"action": "overlay-round", "season": 2026, "round": 1}, series="f1")
    # F1's round 1 is Australia, named by F1's calendar.
    assert "Australia" in argv[5]
    assert "--series" in argv and "f1" in argv
    assert "/media/f1" in argv[5].lower()


@pytest.mark.parametrize("payload", [
    {"command": "backfill --season 2026"},          # the old string contract
    {"action": "rm-rf"},
    {"action": "status", "shell": True},
    {"action": "backfill"},                          # season required
    {"action": "backfill", "season": 1999},          # unknown season
    {"action": "backfill", "season": 2026, "round": 99},
    {"action": "backfill", "season": 2026, "sessions": ["--force"]},
    {"action": "backfill", "season": 2026, "sessions": "E82"},
    {"action": "backfill", "season": 2026, "force": "yes"},
    {"action": "overlay-season", "season": 2026, "scale": 9},
    {"action": "overlay-season", "season": 2026, "scale": "big"},
    {"action": "overlay-round", "season": 2026},     # round required
    {"action": "status", "round": 7},                # round not a status field
    "backfill --season 2026",
    None,
])
def test_run_rejects_malformed_payloads(client, payload):
    response = client.post("/run", json=payload)
    assert response.status_code == 400, response.data
    assert motogp_webui.jobs.running is False


# ---------------------------------------------------------------------------
# The singleton reservation
# ---------------------------------------------------------------------------

def test_run_starts_one_job_and_refuses_a_second(client, monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target, self._args = target, args

        def start(self):
            started.append(self._args)

    monkeypatch.setattr(motogp_webui.threading, "Thread", FakeThread)

    first = client.post("/run", json={"action": "status"})
    assert first.status_code == 200
    assert first.get_json()["command"] == "motogp-grab status"

    second = client.post("/run", json={"action": "status"})
    assert second.status_code == 409
    assert len(started) == 1, "exactly one worker may be launched"


def test_concurrent_try_start_has_exactly_one_winner():
    manager = JobManager(popen=lambda *a, **k: FakeProcess())
    barrier = threading.Barrier(8)
    results = []

    def claim():
        barrier.wait(timeout=10)
        results.append(manager.try_start("job"))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert results.count(True) == 1
    assert results.count(False) == 7


def test_download_selected_refuses_to_overlap_a_running_job(client):
    assert motogp_webui.jobs.try_start("already running")
    response = client.post("/download-selected", json={
        "season": 2026,
        "selections": [{"round": 19, "session": "MOTOGP_RACE"}],
    })
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# Execution: bounded output, exit status, timeout
# ---------------------------------------------------------------------------

def test_job_records_nonzero_exit_and_output():
    manager = JobManager(
        popen=lambda *a, **k: FakeProcess(["doing things\n"], exit_code=3))
    assert manager.try_start("job")
    manager.run_job(["prog"])
    assert manager.state["running"] is False
    assert manager.state["exit_code"] == 3
    assert "doing things" in manager.state["output"]
    assert "exited with status 3" in manager.state["output"]


def test_job_output_is_bounded():
    manager = JobManager(
        popen=lambda *a, **k: FakeProcess(["x" * 500 + "\n"]), max_output_chars=100)
    assert manager.try_start("job")
    manager.run_job(["prog"])
    assert len(manager.state["output"]) <= 100


def test_job_timeout_kills_and_reports():
    process = FakeProcess()

    def slow_lines():
        yield "started\n"
        while not process.killed:  # a hung process: no more output, ever
            threading.Event().wait(0.05)
        yield "never seen\n"

    process.stdout = slow_lines()
    manager = JobManager(popen=lambda *a, **k: process, timeout_seconds=1)
    assert manager.try_start("job")
    manager.run_job(["prog"])
    assert process.killed is True
    assert manager.state["timed_out"] is True
    assert manager.state["exit_code"] is None
    assert "killed" in manager.state["output"]
