"""
Tests for container lifecycle robustness.

Three failure modes cost real debugging time, and each is covered here:

1. The WebUI was backgrounded with nothing watching it, so when it died the
   scheduler kept running and the container still reported "Up".
2. A missing downloads folder aborted the whole container, taking the WebUI with
   it - so the thing you would use to fix the problem was unavailable.
3. Nothing probed the UI, so Docker (and Unraid) had no way to report a container
   whose UI had gone as unhealthy.

The entrypoint tests run the real shell blocks against a throwaway /config rather
than trusting the script by inspection.
"""

import re
import socket
import threading
import http.server
import socketserver
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "entrypoint.sh"
DOCKERFILE = REPO_ROOT / "Dockerfile"

from utils import healthcheck


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------

@pytest.fixture
def live_server():
    """A local HTTP server that answers 200, standing in for the WebUI."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()


def test_healthcheck_passes_when_webui_answers(live_server):
    assert healthcheck.check(url=f"http://127.0.0.1:{live_server}/") is True


def test_healthcheck_fails_when_nothing_listens():
    # Grab a port, then close it: nothing is listening there.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    assert healthcheck.check(url=f"http://127.0.0.1:{port}/", timeout=1) is False


def test_healthcheck_is_dependency_free():
    """
    It runs inside python:3.11-slim, which has no curl. Only the standard
    library may be imported, or the probe itself becomes the outage.
    """
    source = (REPO_ROOT / "utils" / "healthcheck.py").read_text()
    imports = set(re.findall(r"^\s*(?:import|from)\s+([a-zA-Z_][\w.]*)", source, re.M))
    top_level = {name.split(".")[0] for name in imports}
    assert top_level <= {"os", "sys", "urllib"}, top_level


def test_dockerfile_declares_a_healthcheck_using_the_probe():
    text = DOCKERFILE.read_text()
    assert "HEALTHCHECK" in text
    assert "utils/healthcheck.py" in text
    # The probe must be copied into the image, or the healthcheck fails always.
    assert "COPY utils/ ./utils/" in text


# ---------------------------------------------------------------------------
# Entrypoint: path severity
# ---------------------------------------------------------------------------

def _run_path_block(config_dir: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """
    Execute the real path-severity block from entrypoint.sh.

    Extracted rather than reimplemented, so this test fails if the shipped
    script changes behaviour.
    """
    script = ENTRYPOINT.read_text()
    start = script.index("# Path severity") if "# Path severity" in script else script.index(
        "# Refuse to start when a configured path"
    )
    end = script.index('echo ""\necho "  Config Dir:', start)
    block = script[start:end]

    import os
    import sys

    env = {
        "PATH": os.environ["PATH"],
        "PYTHON": sys.executable,
        "CONFIG_DIR": str(config_dir),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    env.update(extra_env or {})

    return subprocess.run(
        ["bash", "-c", block], capture_output=True, text=True, env=env, timeout=30
    )


def _write_settings(tmp_path: Path, library: str, downloads: str) -> Path:
    import yaml

    (tmp_path / "settings.yml").write_text(
        yaml.safe_dump({"library": {"motogp_path": library, "downloads_path": downloads}})
    )
    return tmp_path


def _write_both_settings(tmp_path: Path, library: str, downloads: str,
                         f1_library: str, f1_downloads: str) -> Path:
    """A settings.yml with both series configured, which activates both."""
    import yaml

    (tmp_path / "settings.yml").write_text(
        yaml.safe_dump({"library": {
            "motogp_path": library,
            "downloads_path": downloads,
            "f1_path": f1_library,
            "f1_downloads_path": f1_downloads,
        }})
    )
    return tmp_path


def test_missing_library_path_refuses_to_start(tmp_path):
    lib = tmp_path / "exists"
    lib.mkdir()
    config = _write_settings(tmp_path, str(tmp_path / "gone"), str(lib))

    result = _run_path_block(config)

    assert result.returncode == 1
    assert "library path does not exist" in result.stdout
    assert str(tmp_path / "gone") in result.stdout


def test_missing_downloads_path_only_warns(tmp_path):
    """
    A downloads folder that does not exist yet is routine. Aborting the container
    for it removes the UI you would use to fix it - which is how a guard causes
    the outage it was meant to prevent.
    """
    lib = tmp_path / "lib"
    lib.mkdir()
    config = _write_settings(tmp_path, str(lib), str(tmp_path / "no-downloads"))

    result = _run_path_block(config)

    assert result.returncode == 0, result.stdout
    assert "downloads path does not exist" in result.stdout
    assert "starting normally" in result.stdout


def test_existing_paths_start_silently(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    dl = tmp_path / "dl"
    dl.mkdir()
    config = _write_settings(tmp_path, str(lib), str(dl))

    result = _run_path_block(config)

    assert result.returncode == 0
    assert "does not exist" not in result.stdout


def test_skip_path_check_override(tmp_path):
    config = _write_settings(tmp_path, str(tmp_path / "gone"), str(tmp_path / "gone2"))

    result = _run_path_block(config, {"MOTORSPORTARR_SKIP_PATH_CHECK": "1"})

    assert result.returncode == 0
    assert "continuing because MOTORSPORTARR_SKIP_PATH_CHECK=1" in result.stdout


# ---------------------------------------------------------------------------
# Entrypoint: supervision
# ---------------------------------------------------------------------------

def test_entrypoint_supervises_the_webui():
    text = ENTRYPOINT.read_text()
    # The WebUI must be tracked and checked, not merely launched with '&'.
    assert re.search(r"WEBUI_PID=\$!", text), "WebUI start must record its PID"
    assert "jobs -pr" in text, "supervision must use the job table, not kill -0"
    assert "Restarting it" in text, "a dead WebUI must be restarted"
    assert "MAX_WEBUI_RESTARTS" in text, "restarts must be bounded"


def test_entrypoint_stops_when_the_scheduler_dies():
    """
    A container whose scheduler is dead is doing no work. Staying "Up" hides
    that; exiting lets Docker's restart policy do something useful.
    """
    text = ENTRYPOINT.read_text()
    assert "Scheduler exited" in text
    assert re.search(r"exit \"\$\{?SCHEDULER_EXIT", text)


def test_entrypoint_handles_signals():
    """Without a trap, docker stop waits out the full timeout every time."""
    text = ENTRYPOINT.read_text()
    assert "trap shutdown TERM INT" in text
    assert "kill \"$WEBUI_PID\"" in text
    assert "kill \"$SCHEDULER_PID\"" in text


def test_webui_reports_a_port_clash_clearly():
    """
    The usual cause of a dead UI on Unraid is an older container still holding
    the port. Say so, instead of dumping a traceback.
    """
    source = (REPO_ROOT / "motogp_webui.py").read_text()
    assert "EADDRINUSE" in source
    assert "already in use" in source


def test_a_series_nobody_configured_does_not_block_startup(tmp_path):
    """
    The bug this exists for: making both series' paths mandatory broke every
    existing MotoGP-only install, because /media/F1 does not exist there and the
    library check refuses to start. A dormant series is not a broken one.
    """
    lib = tmp_path / "lib"
    lib.mkdir()
    dl = tmp_path / "dl"
    dl.mkdir()
    # Only MotoGP is configured. Formula 1's default path is not created.
    config = _write_settings(tmp_path, str(lib), str(dl))

    result = _run_path_block(config)

    assert result.returncode == 0, result.stdout
    assert "does not exist" not in result.stdout


def test_a_configured_second_series_is_checked_and_named(tmp_path):
    """
    Once F1 *is* configured, its missing library is a real failure - and the
    message has to say which series, or the user checks the MotoGP path and
    finds nothing wrong with it.
    """
    lib = tmp_path / "lib"
    lib.mkdir()
    dl = tmp_path / "dl"
    dl.mkdir()
    config = _write_both_settings(
        tmp_path, str(lib), str(dl), str(tmp_path / "no-f1-library"), str(dl)
    )

    result = _run_path_block(config)

    assert result.returncode == 1, result.stdout
    assert str(tmp_path / "no-f1-library") in result.stdout


def test_the_library_message_names_both_series_variables(tmp_path):
    """A refusal that names only MOTOGP_LIBRARY_PATH sends people to the wrong one."""
    lib = tmp_path / "lib"
    lib.mkdir()
    config = _write_settings(tmp_path, str(tmp_path / "gone"), str(lib))

    result = _run_path_block(config)

    assert "MOTOGP_LIBRARY_PATH" in result.stdout
    assert "F1_LIBRARY_PATH" in result.stdout
