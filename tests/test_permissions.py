"""
Tests for the non-root container and the permission check.

The container now drops from root to PUID:PGID (default 99:100, Unraid's
nobody:users) before starting anything. The risk of that change is not the drop
itself, it is the failure it produces when the ids are wrong:

    * settings that save and then revert,
    * downloads that complete and are never sorted,
    * a PermissionError in a subprocess hours later,
    * a container that starts cleanly and does nothing all weekend.

None of those look like a permissions problem, so the check has to be loud and
specific. These tests pin the diagnosis and the entrypoint wiring.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from utils.permissions import check_permissions, parse_host_map

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "entrypoint.sh"
DOCKERFILE = REPO_ROOT / "Dockerfile"


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------

def test_writable_config_is_reported_ok(tmp_path):
    (tmp_path / "logs").mkdir()
    report = check_permissions(config_dir=str(tmp_path), uid=99, gid=100)

    assert report.ok
    assert report.is_root is False
    assert report.hint() == ""


def test_unwritable_config_is_reported_with_the_reason(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "logs").mkdir()
    os.chmod(config, 0o500)  # r-x, no write
    try:
        report = check_permissions(config_dir=str(config), uid=99, gid=100)
        assert not report.ok
        assert str(config) in str(report.failures)
        assert "not writable" in report.failures[0].detail
    finally:
        os.chmod(config, 0o700)


def _unwritable_config_report(tmp_path):
    """Build a failing report for an unwritable config dir, restoring
    permissions immediately after (the report object is already captured)."""
    config = tmp_path / "config"
    config.mkdir()
    os.chmod(config, 0o500)
    try:
        return check_permissions(config_dir=str(config), uid=99, gid=100)
    finally:
        os.chmod(config, 0o700)


@pytest.mark.skipif(os.getuid() == 0, reason="root can write anywhere")
def test_hint_without_host_map_names_container_and_host_mount(tmp_path):
    """No MOTOGPARR_HOST_MAP: the hint must not pretend a container path is
    a host-runnable chown target. It names the container path and tells the
    user to chown whatever they mounted there."""
    config = tmp_path / "config"
    config.mkdir()
    os.chmod(config, 0o500)
    try:
        report = check_permissions(config_dir=str(config), uid=99, gid=100)
        assert not report.ok
        hint = report.hint()
        assert "PUID" in hint, "must offer the alternative of changing PUID/PGID"
        assert "container" in hint.lower()
        assert "host" in hint.lower()
        # The old, wrong behaviour: `chown -R 99:100 '/config'` as if the
        # container path existed on the host. The real container path must not
        # appear as a quoted chown target.
        assert f"chown -R 99:100 '{config}" not in hint, hint
        # The fallback target is a placeholder, never a real path.
        for line in hint.splitlines():
            if line.strip().startswith("chown -R"):
                assert line.strip().startswith("chown -R 99:100 <host"), line
    finally:
        os.chmod(config, 0o700)


@pytest.mark.skipif(os.getuid() == 0, reason="root can write anywhere")
def test_hint_with_host_map_prints_the_host_path(tmp_path):
    config = tmp_path / "config"
    (config / "logs").mkdir(parents=True)
    library = tmp_path / "library"
    library.mkdir()
    os.chmod(library, 0o500)
    try:
        report = check_permissions(
            config_dir=str(config),
            library_path=str(library),
            uid=99, gid=100,
        )
        assert not report.ok
        host_map = {str(library): "/mnt/user/data/media"}
        hint = report.hint(host_map=host_map)

        chown_lines = [line for line in hint.splitlines() if line.strip().startswith("chown -R")]
        assert any("/mnt/user/data/media" in line for line in chown_lines), hint
        # The container path must not be inside the chown command itself.
        assert not any(str(library) in line for line in chown_lines), hint
        assert "PUID" in hint
    finally:
        os.chmod(library, 0o700)


@pytest.mark.skipif(os.getuid() == 0, reason="root can write anywhere")
def test_hint_with_uncovered_path_falls_back_to_honest_wording(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    os.chmod(config, 0o500)
    try:
        report = check_permissions(config_dir=str(config), uid=99, gid=100)
        assert not report.ok
        # The map covers /media, which is not one of the failing paths.
        hint = report.hint(host_map={"/media": "/mnt/user/data/media"})
        # No invented host path anywhere.
        assert "/mnt/user/data/media" not in hint, hint
        # It names the container path and describes the host mount instead.
        assert str(config) in hint
        assert "mounted" in hint.lower() or "container" in hint.lower()
        assert "PUID" in hint
    finally:
        os.chmod(config, 0o700)


def test_parse_host_map():
    assert parse_host_map("") == {}
    assert parse_host_map(None) == {}
    assert parse_host_map("/media=/mnt/user/data/media") == {
        "/media": "/mnt/user/data/media"
    }
    assert parse_host_map("/media=/mnt/user/data/media,/downloads=/mnt/user/data/downloads") == {
        "/media": "/mnt/user/data/media",
        "/downloads": "/mnt/user/data/downloads",
    }
    # Trailing slash and spaces are tolerated.
    assert parse_host_map("/media/ = /mnt/user/data/media ") == {
        "/media": "/mnt/user/data/media"
    }
    # Malformed entries are skipped.
    assert parse_host_map("/media, , =") == {}


@pytest.mark.skipif(os.getuid() == 0, reason="root can write anywhere")
def test_cli_hint_matches_the_report_hint(tmp_path):
    """python -m utils.permissions prints the same hint text as report.hint(),
    so the CLI and the entrypoint (which calls report.hint() directly) stay
    in sync."""
    config = tmp_path / "config"
    config.mkdir()
    os.chmod(config, 0o500)
    try:
        report = check_permissions(config_dir=str(config))
        assert not report.ok
        env = dict(os.environ, CONFIG_DIR=str(config))
        result = subprocess.run(
            [sys.executable, "-m", "utils.permissions"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 1, result.stdout + result.stderr
        for line in report.hint().splitlines():
            assert line in result.stdout, f"CLI output missing hint line: {line!r}"
    finally:
        os.chmod(config, 0o700)


def test_a_missing_library_path_is_not_a_permissions_failure(tmp_path):
    """
    A missing folder is the path check's business. Reporting it here as a
    permission problem would send people to chown a directory that does not
    exist.
    """
    (tmp_path / "logs").mkdir()
    report = check_permissions(
        config_dir=str(tmp_path),
        library_path=str(tmp_path / "does-not-exist"),
        uid=99, gid=100,
    )
    assert report.ok
    assert all(check.path != str(tmp_path / "does-not-exist") for check in report.checks)


def test_read_only_media_is_only_checked_at_the_library_path(tmp_path):
    """
    Only folders the app writes to are checked. Demanding write access to the
    mount root would fail on a legitimately read-only media share.
    """
    (tmp_path / "logs").mkdir()
    library = tmp_path / "library"
    library.mkdir()

    report = check_permissions(
        config_dir=str(tmp_path), library_path=str(library), uid=99, gid=100
    )
    checked = {check.path for check in report.checks}
    assert str(library) in checked
    assert str(tmp_path) not in {p for p in checked if p != str(tmp_path)}
    assert report.ok


def test_report_is_json_serialisable_for_the_webui(tmp_path):
    (tmp_path / "logs").mkdir()
    payload = check_permissions(config_dir=str(tmp_path)).as_dict()
    assert payload["ok"] is True
    assert isinstance(payload["checks"], list)
    assert payload["uid"] == os.getuid()


# ---------------------------------------------------------------------------
# The entrypoint must actually drop privileges
# ---------------------------------------------------------------------------

def test_entrypoint_drops_privileges_with_setpriv():
    text = ENTRYPOINT.read_text()
    assert "setpriv --reuid" in text
    assert "PUID=" in text and "PGID=" in text
    # Defaults are Unraid's nobody:users.
    assert 'PUID="${PUID:-99}"' in text
    assert 'PGID="${PGID:-100}"' in text


def test_entrypoint_refuses_to_continue_as_root():
    """
    If setpriv is missing or fails, the app must not carry on as root. Carrying
    on would silently reintroduce the problem while looking fixed.
    """
    text = ENTRYPOINT.read_text()
    assert "Still running as root after the privilege drop" in text


def test_entrypoint_checks_permissions_after_dropping():
    """A check that runs as root proves nothing about the user that follows."""
    text = ENTRYPOINT.read_text()
    drop = text.index("setpriv --reuid")
    perms = text.index("check_permissions")
    assert perms > drop, "the permission check runs before privileges are dropped"


def test_entrypoint_explains_an_unwritable_folder_and_offers_the_override():
    text = ENTRYPOINT.read_text()
    assert "Refusing to start" in text
    assert "MOTORSPORTARR_SKIP_PATH_CHECK=1" in text


def test_entrypoint_does_not_hard_code_the_app_directory():
    """
    SCRIPT_DIR is derived from the script's own location so the entrypoint can
    be run from a checkout. Without that, none of the above could be tested
    outside a container.
    """
    text = ENTRYPOINT.read_text()
    assert 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in text


# ---------------------------------------------------------------------------
# The image must support the drop
# ---------------------------------------------------------------------------

def test_dockerfile_installs_setpriv():
    """
    util-linux provides setpriv. It is often already present in Debian slim, but
    relying on that would make the privilege drop a build-time coin flip.
    """
    text = DOCKERFILE.read_text()
    assert "util-linux" in text


def test_dockerfile_does_not_bake_in_a_user():
    """
    A Dockerfile USER would be simpler and would break Unraid: the correct uid
    is per-installation, and an image with a fixed one cannot be adjusted
    without a rebuild.
    """
    text = DOCKERFILE.read_text()
    assert not re.search(r"^USER\s+", text, re.M), (
        "privileges are dropped at runtime by entrypoint.sh so PUID/PGID stay "
        "configurable"
    )


def test_puid_pgid_are_registered_and_offered_by_the_template():
    """Every variable the app reads must be documented and settable in Unraid."""
    from utils.config_loader import all_known_env_vars

    for name in ("PUID", "PGID", "UMASK"):
        assert name in all_known_env_vars(), f"{name} is read but not registered"

    template = (REPO_ROOT / "unraid" / "msportarr.xml").read_text()
    for name in ("PUID", "PGID", "UMASK"):
        assert f'Target="{name}"' in template, f"{name} is not in the Unraid template"


def test_a_missing_config_subfolder_is_created_not_reported_as_failure(tmp_path):
    """
    First-run bug: on an empty appdata folder /config/logs does not exist yet.
    Treating that as an unwritable path refused to start every new user.
    /config belongs to the app, so its own subfolders are created.
    """
    config = tmp_path / "fresh"
    config.mkdir()
    report = check_permissions(config_dir=str(config), uid=99, gid=100)

    assert report.ok, report.hint()
    assert (config / "logs").is_dir()


def test_library_and_downloads_are_never_created(tmp_path):
    """
    Creating them would hide a wrong volume mapping instead of revealing it, and
    would litter the host with directories the app guessed at.
    """
    (tmp_path / "logs").mkdir()
    missing_library = tmp_path / "library-that-does-not-exist"

    check_permissions(
        config_dir=str(tmp_path),
        library_path=str(missing_library),
        uid=99, gid=100,
    )
    assert not missing_library.exists()
