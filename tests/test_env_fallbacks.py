"""
Tests for Tier 2 environment-variable renames (MOTORSPORTARR_* with MOTOGPARR_* fallbacks).

Every application-level variable renamed from MOTOGPARR_* to MOTORSPORTARR_*
must obey the same precedence:

    new name (MOTORSPORTARR_*)  >  old name (MOTOGPARR_*)  >  default

and an empty string on the new name counts as unset so it falls through to the
legacy name rather than blanking it out.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "entrypoint.sh"


def _extract_entrypoint_header() -> str:
    text = ENTRYPOINT.read_text(encoding="utf-8")
    end = text.index("# Ensure config directory exists")
    return text[:end]


def test_app_dir_prefers_motorsportarr_then_falls_back_to_legacy():
    header = _extract_entrypoint_header() + '\necho "APP_DIR=${MOTORSPORTARR_APP_DIR}"\n'

    # 1. Legacy MOTOGPARR_APP_DIR still works when MOTORSPORTARR_APP_DIR is unset.
    res_legacy = subprocess.run(
        ["bash", "-c", header],
        env={"PATH": os.environ["PATH"], "MOTOGPARR_APP_DIR": "/legacy/app"},
        capture_output=True, text=True, check=True,
    )
    assert "APP_DIR=/legacy/app" in res_legacy.stdout

    # 2. New MOTORSPORTARR_APP_DIR wins when both are set.
    res_new = subprocess.run(
        ["bash", "-c", header],
        env={
            "PATH": os.environ["PATH"],
            "MOTORSPORTARR_APP_DIR": "/new/app",
            "MOTOGPARR_APP_DIR": "/legacy/app",
        },
        capture_output=True, text=True, check=True,
    )
    assert "APP_DIR=/new/app" in res_new.stdout

    # 3. Empty MOTORSPORTARR_APP_DIR falls through to MOTOGPARR_APP_DIR.
    res_empty = subprocess.run(
        ["bash", "-c", header],
        env={
            "PATH": os.environ["PATH"],
            "MOTORSPORTARR_APP_DIR": "",
            "MOTOGPARR_APP_DIR": "/legacy/app",
        },
        capture_output=True, text=True, check=True,
    )
    assert "APP_DIR=/legacy/app" in res_empty.stdout


def test_python_prefers_motorsportarr_then_falls_back_to_legacy():
    import sys
    import motogp_webui

    header = _extract_entrypoint_header() + '\necho "PY=${PYTHON}"\n'

    res_legacy = subprocess.run(
        ["bash", "-c", header],
        env={"PATH": os.environ["PATH"], "MOTOGPARR_PYTHON": "/legacy/python3"},
        capture_output=True, text=True, check=True,
    )
    assert "PY=/legacy/python3" in res_legacy.stdout

    res_new = subprocess.run(
        ["bash", "-c", header],
        env={
            "PATH": os.environ["PATH"],
            "MOTORSPORTARR_PYTHON": "/new/python3",
            "MOTOGPARR_PYTHON": "/legacy/python3",
        },
        capture_output=True, text=True, check=True,
    )
    assert "PY=/new/python3" in res_new.stdout

    assert motogp_webui._resolve_python_path({"MOTOGPARR_PYTHON": "/legacy/py"}) == "/legacy/py"
    assert motogp_webui._resolve_python_path({
        "MOTORSPORTARR_PYTHON": "/new/py",
        "MOTOGPARR_PYTHON": "/legacy/py",
    }) == "/new/py"
    assert motogp_webui._resolve_python_path({
        "MOTORSPORTARR_PYTHON": "",
        "MOTOGPARR_PYTHON": "/legacy/py",
    }) == "/legacy/py"
    assert motogp_webui._resolve_python_path({}) == sys.executable


def test_secret_key_prefers_motorsportarr_then_legacy_then_audience_then_ephemeral():
    import motogp_webui

    # 1. Legacy MOTOGPARR_SECRET_KEY works and beats AUTH_AUDIENCE.
    assert motogp_webui._resolve_secret_key({
        "MOTOGPARR_SECRET_KEY": "legacy-secret",
        "AUTH_AUDIENCE": "aud-tag",
    }) == "legacy-secret"

    # 2. New MOTORSPORTARR_SECRET_KEY beats MOTOGPARR_SECRET_KEY.
    assert motogp_webui._resolve_secret_key({
        "MOTORSPORTARR_SECRET_KEY": "new-secret",
        "MOTOGPARR_SECRET_KEY": "legacy-secret",
        "AUTH_AUDIENCE": "aud-tag",
    }) == "new-secret"

    # 3. Empty MOTORSPORTARR_SECRET_KEY falls through to MOTOGPARR_SECRET_KEY.
    assert motogp_webui._resolve_secret_key({
        "MOTORSPORTARR_SECRET_KEY": "",
        "MOTOGPARR_SECRET_KEY": "legacy-secret",
    }) == "legacy-secret"

    # 4. AUTH_AUDIENCE is used when neither secret key is set.
    assert motogp_webui._resolve_secret_key({"AUTH_AUDIENCE": "aud-tag"}) == "aud-tag"

    # 5. Ephemeral random hex key when nothing is set (does not repeat).
    k1 = motogp_webui._resolve_secret_key({})
    k2 = motogp_webui._resolve_secret_key({})
    assert len(k1) == 64 and k1 != k2


def test_healthcheck_timeout_prefers_motorsportarr_then_falls_back_to_legacy():
    from utils import healthcheck

    assert healthcheck._resolve_timeout({"MOTOGPARR_HEALTHCHECK_TIMEOUT": "7.5"}) == 7.5
    assert healthcheck._resolve_timeout({
        "MOTORSPORTARR_HEALTHCHECK_TIMEOUT": "12",
        "MOTOGPARR_HEALTHCHECK_TIMEOUT": "7.5",
    }) == 12.0
    assert healthcheck._resolve_timeout({
        "MOTORSPORTARR_HEALTHCHECK_TIMEOUT": "",
        "MOTOGPARR_HEALTHCHECK_TIMEOUT": "7.5",
    }) == 7.5
    assert healthcheck._resolve_timeout({}) == 3.0


def test_webui_port_prefers_motorsportarr_then_falls_back_to_legacy():
    from utils import healthcheck

    assert healthcheck._resolve_port({"MOTOGPARR_WEBUI_PORT": "8081"}) == 8081
    assert healthcheck._resolve_port({
        "MOTORSPORTARR_WEBUI_PORT": "9090",
        "MOTOGPARR_WEBUI_PORT": "8081",
    }) == 9090
    assert healthcheck._resolve_port({
        "MOTORSPORTARR_WEBUI_PORT": "",
        "MOTOGPARR_WEBUI_PORT": "8081",
    }) == 8081
    assert healthcheck._resolve_port({}) == 5679


def test_version_prefers_motorsportarr_then_falls_back_to_legacy():
    from utils.config_loader import INFORMATIONAL_ENV, DEPRECATED_ENV, all_known_env_vars

    assert "MOTORSPORTARR_VERSION" in INFORMATIONAL_ENV
    assert DEPRECATED_ENV.get("MOTOGPARR_VERSION") == "MOTORSPORTARR_VERSION"
    assert {"MOTORSPORTARR_VERSION", "MOTOGPARR_VERSION"} <= all_known_env_vars()

    header = _extract_entrypoint_header()

    res_legacy = subprocess.run(
        ["bash", "-c", header],
        env={"PATH": os.environ["PATH"], "MOTOGPARR_VERSION": "2.13.0"},
        capture_output=True, text=True, check=True,
    )
    assert "Version: 2.13.0" in res_legacy.stdout

    res_new = subprocess.run(
        ["bash", "-c", header],
        env={
            "PATH": os.environ["PATH"],
            "MOTORSPORTARR_VERSION": "1.0.0",
            "MOTOGPARR_VERSION": "2.13.0",
        },
        capture_output=True, text=True, check=True,
    )
    assert "Version: 1.0.0" in res_new.stdout
    assert "2.13.0" not in res_new.stdout

    res_empty_new = subprocess.run(
        ["bash", "-c", header],
        env={
            "PATH": os.environ["PATH"],
            "MOTORSPORTARR_VERSION": "",
            "MOTOGPARR_VERSION": "2.13.0",
        },
        capture_output=True, text=True, check=True,
    )
    assert "Version: 2.13.0" in res_empty_new.stdout


def test_calendar_update_prefers_motorsportarr_then_falls_back_to_legacy():
    from utils.config_loader import ENTRYPOINT_ENV, DEPRECATED_ENV, all_known_env_vars

    assert "MOTORSPORTARR_CALENDAR_UPDATE" in ENTRYPOINT_ENV
    assert DEPRECATED_ENV.get("MOTOGPARR_CALENDAR_UPDATE") == "MOTORSPORTARR_CALENDAR_UPDATE"
    assert {"MOTORSPORTARR_CALENDAR_UPDATE", "MOTOGPARR_CALENDAR_UPDATE"} <= all_known_env_vars()

    text = ENTRYPOINT.read_text(encoding="utf-8")
    cal_line = next(line for line in text.splitlines() if line.startswith("CALENDAR_UPDATE="))
    snippet = cal_line + '\necho "CAL=${CALENDAR_UPDATE}"\n'

    res_legacy = subprocess.run(
        ["bash", "-c", snippet],
        env={"PATH": os.environ["PATH"], "MOTOGPARR_CALENDAR_UPDATE": "warn"},
        capture_output=True, text=True, check=True,
    )
    assert "CAL=warn" in res_legacy.stdout

    res_new = subprocess.run(
        ["bash", "-c", snippet],
        env={
            "PATH": os.environ["PATH"],
            "MOTORSPORTARR_CALENDAR_UPDATE": "off",
            "MOTOGPARR_CALENDAR_UPDATE": "warn",
        },
        capture_output=True, text=True, check=True,
    )
    assert "CAL=off" in res_new.stdout

    res_empty = subprocess.run(
        ["bash", "-c", snippet],
        env={
            "PATH": os.environ["PATH"],
            "MOTORSPORTARR_CALENDAR_UPDATE": "",
            "MOTOGPARR_CALENDAR_UPDATE": "warn",
        },
        capture_output=True, text=True, check=True,
    )
    assert "CAL=warn" in res_empty.stdout

    res_default = subprocess.run(
        ["bash", "-c", snippet],
        env={"PATH": os.environ["PATH"]},
        capture_output=True, text=True, check=True,
    )
    assert "CAL=auto" in res_default.stdout


def test_skip_path_check_prefers_motorsportarr_then_falls_back_to_legacy():
    from utils.config_loader import ENTRYPOINT_ENV, DEPRECATED_ENV, all_known_env_vars

    assert "MOTORSPORTARR_SKIP_PATH_CHECK" in ENTRYPOINT_ENV
    assert DEPRECATED_ENV.get("MOTOGPARR_SKIP_PATH_CHECK") == "MOTORSPORTARR_SKIP_PATH_CHECK"
    assert {"MOTORSPORTARR_SKIP_PATH_CHECK", "MOTOGPARR_SKIP_PATH_CHECK"} <= all_known_env_vars()

    expr = 'echo "SKIP=${MOTORSPORTARR_SKIP_PATH_CHECK:-${MOTOGPARR_SKIP_PATH_CHECK:-0}}"'

    res_legacy = subprocess.run(
        ["bash", "-c", expr],
        env={"PATH": os.environ["PATH"], "MOTOGPARR_SKIP_PATH_CHECK": "1"},
        capture_output=True, text=True, check=True,
    )
    assert "SKIP=1" in res_legacy.stdout

    res_new_wins = subprocess.run(
        ["bash", "-c", expr],
        env={
            "PATH": os.environ["PATH"],
            "MOTORSPORTARR_SKIP_PATH_CHECK": "0",
            "MOTOGPARR_SKIP_PATH_CHECK": "1",
        },
        capture_output=True, text=True, check=True,
    )
    assert "SKIP=0" in res_new_wins.stdout

    res_empty_falls_back = subprocess.run(
        ["bash", "-c", expr],
        env={
            "PATH": os.environ["PATH"],
            "MOTORSPORTARR_SKIP_PATH_CHECK": "",
            "MOTOGPARR_SKIP_PATH_CHECK": "1",
        },
        capture_output=True, text=True, check=True,
    )
    assert "SKIP=1" in res_empty_falls_back.stdout


def test_host_map_prefers_motorsportarr_then_falls_back_to_legacy(monkeypatch):
    from utils.config_loader import ENV_SPEC, DEPRECATED_ENV, all_known_env_vars, get
    from utils.permissions import PathCheck, PermissionReport

    assert "MOTORSPORTARR_HOST_MAP" in ENV_SPEC
    assert DEPRECATED_ENV.get("MOTOGPARR_HOST_MAP") == "MOTORSPORTARR_HOST_MAP"
    assert {"MOTORSPORTARR_HOST_MAP", "MOTOGPARR_HOST_MAP"} <= all_known_env_vars()

    report = PermissionReport(
        uid=99,
        gid=100,
        is_root=False,
        checks=[PathCheck(path="/media", label="library", exists=True, writable=False)],
    )

    # 1. Legacy MOTOGPARR_HOST_MAP works.
    monkeypatch.delenv("MOTORSPORTARR_HOST_MAP", raising=False)
    monkeypatch.setenv("MOTOGPARR_HOST_MAP", "/media=/mnt/legacy/media")
    assert "'/mnt/legacy/media'" in report.hint()
    assert get("permissions", None) == "/media=/mnt/legacy/media"

    # 2. New MOTORSPORTARR_HOST_MAP wins when both are set.
    monkeypatch.setenv("MOTORSPORTARR_HOST_MAP", "/media=/mnt/new/media")
    assert "'/mnt/new/media'" in report.hint()
    assert get("permissions", None) == "/media=/mnt/new/media"

    # 3. Empty MOTORSPORTARR_HOST_MAP falls through to MOTOGPARR_HOST_MAP.
    monkeypatch.setenv("MOTORSPORTARR_HOST_MAP", "")
    assert "'/mnt/legacy/media'" in report.hint()
    assert get("permissions", None) == "/media=/mnt/legacy/media"


def test_cookie_secure_prefers_motorsportarr_then_falls_back_to_legacy(monkeypatch):
    import motogp_webui
    from utils.config_loader import ENV_SPEC, DEPRECATED_ENV, all_known_env_vars, get

    assert "MOTORSPORTARR_COOKIE_SECURE" in ENV_SPEC
    assert DEPRECATED_ENV.get("MOTOGPARR_COOKIE_SECURE") == "MOTORSPORTARR_COOKIE_SECURE"
    assert {"MOTORSPORTARR_COOKIE_SECURE", "MOTOGPARR_COOKIE_SECURE"} <= all_known_env_vars()

    assert motogp_webui._resolve_cookie_secure({"MOTOGPARR_COOKIE_SECURE": "1"}) is True
    assert motogp_webui._resolve_cookie_secure({
        "MOTORSPORTARR_COOKIE_SECURE": "0",
        "MOTOGPARR_COOKIE_SECURE": "1",
    }) is False
    assert motogp_webui._resolve_cookie_secure({
        "MOTORSPORTARR_COOKIE_SECURE": "",
        "MOTOGPARR_COOKIE_SECURE": "1",
    }) is True
    assert motogp_webui._resolve_cookie_secure({}) is False

    monkeypatch.delenv("MOTORSPORTARR_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("MOTOGPARR_COOKIE_SECURE", "1")
    assert get("auth", "cookie_secure") == "1"


def test_series_env_prefers_motorsportarr_then_falls_back_to_legacy():
    import sys
    from series import binding
    from utils.config_loader import PROCESS_ENV, ENTRYPOINT_ENV, DEPRECATED_ENV, all_known_env_vars

    assert "MOTORSPORTARR_SERIES" in PROCESS_ENV
    assert "MOTORSPORTARR_SERIES" not in ENTRYPOINT_ENV
    assert DEPRECATED_ENV.get("MOTOGPARR_SERIES") == "MOTORSPORTARR_SERIES"
    assert {"MOTORSPORTARR_SERIES", "MOTOGPARR_SERIES"} <= all_known_env_vars()
    assert binding.ENV_VAR == "MOTORSPORTARR_SERIES"

    probe = "from series.binding import bound_series; print(bound_series())"

    # 1. Legacy MOTOGPARR_SERIES binds the child process when MOTORSPORTARR_SERIES is unset.
    res_legacy = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env={"PATH": os.environ["PATH"], "MOTOGPARR_SERIES": "f1"},
        capture_output=True, text=True, check=True,
    )
    assert res_legacy.stdout.strip() == "f1"

    # 2. New MOTORSPORTARR_SERIES wins when both are set.
    res_new = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env={
            "PATH": os.environ["PATH"],
            "MOTORSPORTARR_SERIES": "motogp",
            "MOTOGPARR_SERIES": "f1",
        },
        capture_output=True, text=True, check=True,
    )
    assert res_new.stdout.strip() == "motogp"

    # 3. Empty MOTORSPORTARR_SERIES falls through to MOTOGPARR_SERIES.
    res_empty = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env={
            "PATH": os.environ["PATH"],
            "MOTORSPORTARR_SERIES": "",
            "MOTOGPARR_SERIES": "f1",
        },
        capture_output=True, text=True, check=True,
    )
    assert res_empty.stdout.strip() == "f1"

    # 4. Default series when neither is set.
    res_default = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env={"PATH": os.environ["PATH"]},
        capture_output=True, text=True, check=True,
    )
    assert res_default.stdout.strip() == "motogp"
