"""
Tests for the Unraid container template.

Unraid drives a container from an XML template rather than a compose file, so a
broken or half-filled template is the difference between "install and configure
in the WebUI" and "the app starts but every path is wrong". These tests pin the
contract that matters: the template parses, the ports and volumes the app
actually needs are present, and every variable it sets is one the app reads.
"""

import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "unraid" / "msportarr.xml"


@pytest.fixture(scope="module")
def template():
    return ET.parse(TEMPLATE).getroot()


def configs(root, kind=None):
    entries = root.findall("Config")
    if kind:
        entries = [c for c in entries if c.get("Type") == kind]
    return entries


def test_template_parses_and_is_unraid_v2(template):
    assert template.tag == "Container"
    assert template.get("version") == "2"


def test_repository_and_icon_are_pinned_to_this_project(template):
    assert template.findtext("Repository") == "ghcr.io/dougalbob/msportarr:latest"
    assert "flat/v2" not in (template.findtext("Icon") or "")
    icon = template.findtext("Icon")
    assert icon and icon.startswith("https://raw.githubusercontent.com/dougalbob/msportarr/")


def test_webui_port_matches_the_port_the_app_binds(template):
    """motogp_webui.py binds 5679; a mismatched template port hides the UI."""
    webui = template.findtext("WebUI")
    assert "[PORT:5679]" in webui

    ports = configs(template, "Port")
    assert [p.get("Target") for p in ports] == ["5679"]


def test_required_volumes_present(template):
    targets = {c.get("Target") for c in configs(template, "Path")}
    assert {"/config", "/media", "/downloads"} <= targets


def test_config_folder_is_persistent(template):
    config_dir = next(c for c in configs(template, "Path") if c.get("Target") == "/config")
    assert config_dir.get("Required") == "true"
    assert config_dir.text.startswith("/mnt/user/")


def test_variables_the_app_actually_reads(template):
    """
    Every Variable must be one the app reads - a typo here fails silently.
    The authoritative list is ENV_SPEC, not a copy of it, so this cannot rot.
    """
    from utils.config_loader import all_known_env_vars

    targets = {c.get("Target") for c in configs(template, "Variable")}
    unknown = targets - all_known_env_vars()
    assert not unknown, f"template sets variables nothing reads: {unknown}"


def test_secrets_are_masked(template):
    for name in ("PLEX_TOKEN", "PROWLARR_API_KEY", "SABNZBD_API_KEY", "DISCORD_WEBHOOK_URL"):
        entry = next(c for c in configs(template, "Variable") if c.get("Target") == name)
        assert entry.get("Mask") == "true", f"{name} is displayed in plain text"


def test_api_settings_are_optional(template):
    """
    Prowlarr/SABnzbd/Plex credentials are editable in the WebUI, so the template
    must not make them required - otherwise a fresh install cannot be saved.
    """
    for name in ("PROWLARR_URL", "SABNZBD_URL", "PLEX_URL", "PLEX_TOKEN"):
        entry = next(c for c in configs(template, "Variable") if c.get("Target") == name)
        assert entry.get("Required") == "false"


def test_calendar_update_variable_is_documented(template):
    """The whole point of the option is discoverable behaviour, so the default
    must match entrypoint.sh and the description must explain the risk."""
    entry = next(
        c for c in configs(template, "Variable")
        if c.get("Target") == "MOTORSPORTARR_CALENDAR_UPDATE"
    )
    assert entry.text == "auto"
    assert "auto" in entry.get("Description")
    assert "warn" in entry.get("Description")

    entrypoint = (REPO_ROOT / "entrypoint.sh").read_text()
    assert 'MOTORSPORTARR_CALENDAR_UPDATE:-${MOTOGPARR_CALENDAR_UPDATE:-auto}' in entrypoint


def newest_tag() -> Optional[str]:
    """Newest X.Y.Z version tag in the repo, or None before the first release tag.

    Tags are fetched from the remote (a shallow checkout may not carry them
    locally), which is why this shells out to git rather than reading a file.
    msportarr uses bare semver tags (1.0.0) without a 'v' prefix so they do not
    collide with legacy v2.x motogparr tags.
    """
    refs = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", "origin"],
        capture_output=True, text=True, check=True,
    ).stdout
    version_re = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
    tags = []
    for line in refs.splitlines():
        if "refs/tags/" not in line or line.endswith("^{}"):
            continue
        tag = line.split("refs/tags/", 1)[1]
        if version_re.match(tag):
            tags.append(tag)
    if not tags:
        return None
    return sorted(
        tags,
        key=lambda t: tuple(int(p) for p in version_re.match(t).groups()),
    )[-1]


def _is_next_version(candidate: str, base: str) -> bool:
    """True if candidate is a plausible *next* version after base.

    The bump PR is created before the tag exists, so the pinned ref will be
    one step ahead of the newest remote tag. Accept the next patch, minor, or
    major as valid so that PR can pass CI; a stale pin (behind) is still a
    failure, and a wild jump (e.g. 9.9.9) is treated as not-next and fails.
    """
    version_re = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
    m_cand = version_re.match(candidate)
    m_base = version_re.match(base)
    if not m_cand or not m_base:
        return False
    cand = tuple(int(p) for p in m_cand.groups())
    base_t = tuple(int(p) for p in m_base.groups())
    # Candidate must be newer than base, but not absurdly far ahead.
    if cand <= base_t:
        return False
    # Allow any newer patch within the same minor (e.g. 1.0.2 -> 1.0.3)
    # or exactly one minor/major bump.
    if cand[0] == base_t[0] and cand[1] == base_t[1]:
        return cand[2] > base_t[2]
    if cand[0] == base_t[0] and cand[1] == base_t[1] + 1 and cand[2] == 0:
        return True
    if cand[0] == base_t[0] + 1 and cand[1] == 0 and cand[2] == 0:
        return True
    return False


def test_template_url_is_pinned_to_the_latest_release_tag(template):
    """TemplateURL must pin a release tag, never float on 'main'.

    Pinning (decision b in the handoff) means pressing "Apply" in the Unraid
    GUI cannot silently replace a user's locally edited template with whatever
    main happens to be. The trade-off: old installs do not receive template
    updates until the user re-points it, so the release checklist must remind
    the releaser to bump TemplateURL on every release.
    """
    url = template.findtext("TemplateURL")
    assert "raw.githubusercontent.com/dougalbob/msportarr/" in url, url
    # The segment after the repo name is the pinned ref (a tag, never main).
    ref = url.split("raw.githubusercontent.com/dougalbob/msportarr/", 1)[1].split(
        "/", 1
    )[0]
    assert ref != "main", f"TemplateURL must not float on main: {url}"
    assert re.match(r"^\d+\.\d+\.\d+$", ref), (
        f"TemplateURL must pin a bare X.Y.Z version tag (no 'v' prefix): {url}"
    )
    # During a release bump the template may pin the *next* tag before it
    # exists remotely. Allow a pinned ref that is newer than the current
    # newest tag so the bump PR can pass CI and the tag can be created from
    # the merge commit (which is the intended flow — see PR #19 at 2ccff1d).
    # Before the initial 1.0.0 release tag is pushed to origin, the template
    # must pin 1.0.0.
    newest = newest_tag()
    if newest is None:
        assert ref == "1.0.0", (
            f"No X.Y.Z tags exist on origin yet; initial TemplateURL must pin 1.0.0, got {ref}"
        )
    else:
        assert ref == newest or _is_next_version(ref, newest), (
            f"TemplateURL pins {ref} but the newest tag is {newest}; "
            "bump TemplateURL on every release"
        )
    # The file must exist at the path the URL advertises, or Unraid 404s.
    assert (REPO_ROOT / "unraid" / "msportarr.xml").is_file()


def test_every_config_has_unique_target_and_required_attributes(template):
    """Each <Config> must carry a unique Target plus Type/Display/Default so
    Unraid can render the Add Container form unambiguously."""
    entries = configs(template)
    targets = [c.get("Target") for c in entries]
    assert all(targets), "every Config needs a Target"
    assert len(targets) == len(set(targets)), f"duplicate Target values: {targets}"
    for c in entries:
        assert c.get("Type"), f"Config {c.get('Target')!r} missing Type"
        assert c.get("Display") is not None, (
            f"Config {c.get('Target')!r} missing Display"
        )
        assert c.get("Default") is not None, (
            f"Config {c.get('Target')!r} missing Default"
        )


def test_puid_pgid_umask_and_auth_targets_present(template):
    """PUID/PGID/UMASK and every AUTH_* variable must be offered by the
    template (they are runtime configuration the user may need to set)."""
    targets = {c.get("Target") for c in configs(template)}
    for name in ("PUID", "PGID", "UMASK"):
        assert name in targets, f"{name} is not in the Unraid template"
    # Every AUTH_* the app reads must have a template entry.
    from utils.config_loader import ENV_SPEC

    auth_vars = [name for name in ENV_SPEC if name.startswith("AUTH_")]
    assert auth_vars, "ENV_SPEC should define AUTH_* variables"
    for name in auth_vars:
        assert name in targets, f"{name} is not in the Unraid template"


def test_new_variables_are_not_required(template):
    """Newly added optional variables must not be Required="true", or they
    would block the Add Container form for existing users."""
    newly_optional = ("MOTORSPORTARR_HOST_MAP",)
    for name in newly_optional:
        entry = next(c for c in configs(template) if c.get("Target") == name)
        assert entry.get("Required") != "true", f"{name} must not be required"


def test_icon_file_referenced_by_url_exists(template):
    url = template.findtext("Icon")
    name = url.rsplit("/", 1)[-1]
    assert (REPO_ROOT / "assets" / name).is_file(), f"missing asset for Icon URL: {name}"
