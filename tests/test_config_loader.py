"""
Tests for the environment-variable registry in utils/config_loader.py.

The app is configured by environment variables (that is what a launcher such as
Unraid's template, docker run or compose can actually set), with settings.yml as
a fallback. The registry is the single source of truth for which variables exist,
so these tests pin both the registry's behaviour and its agreement with the
Unraid template.

The bug that motivated them: PLEX_PATH_CONTAINER / PLEX_PATH_PLEX were
documented in .env.example but absent from the loader's mapping table, so they
were silently ignored. The Plex tagger then compared two different strings
forever and gave up without saying why.
"""

import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "unraid" / "msportarr.xml"

from utils import config_loader
from utils.config_loader import (
    ENV_SPEC,
    ENTRYPOINT_ENV,
    INFORMATIONAL_ENV,
    all_known_env_vars,
    get,
    get_with_source,
    get_plex_path_mapping,
    plex_path_mapping_complete,
    translate_path_for_plex,
    get_timezone,
    get_motogp_library_path,
    startup_report,
    env_help,
)


SETTINGS = {
    "library": {
        "motogp_path": "/media/Motorsport/MotoGP",
        "downloads_path": "/downloads/complete/motogp",
    },
    "plex": {
        "path_mapping": {"container": "/media", "plex": "/data/media"},
        "library_name": "Motorsport - MotoGP",
    },
    "timezone": "Europe/London",
}


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Point the loader at a throwaway /config with a known settings.yml."""
    (tmp_path / "settings.yml").write_text(yaml.safe_dump(SETTINGS))
    monkeypatch.setattr(config_loader, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_loader, "SETTINGS_FILE", tmp_path / "settings.yml")
    monkeypatch.setattr(config_loader, "_settings_cache", None)

    # Clear every variable the app reads, so the host environment cannot leak in.
    for name in ENV_SPEC:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------

def test_every_env_spec_entry_is_well_formed():
    for name, spec in ENV_SPEC.items():
        assert set(spec) <= {"section", "key", "cast", "secret", "description"}, name
        assert spec["section"], name
        assert "key" in spec, name
        assert callable(spec["cast"]), name
        assert spec.get("description"), f"{name} has no description"


def test_env_var_names_are_upper_snake_case():
    for name in ENV_SPEC:
        assert name == name.upper(), name
        assert " " not in name and "-" not in name, name


def test_no_two_variables_override_the_same_setting():
    seen = {}
    for name, spec in ENV_SPEC.items():
        target = (spec["section"], spec["key"])
        assert target not in seen, f"{name} and {seen.get(target)} both set {target}"
        seen[target] = name


def test_secrets_are_flagged():
    for name in ("PLEX_TOKEN", "PROWLARR_API_KEY", "SABNZBD_API_KEY", "DISCORD_WEBHOOK_URL"):
        assert ENV_SPEC[name].get("secret") is True, f"{name} should be marked secret"


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------

def test_settings_yml_is_used_when_no_env_var(config_dir):
    value, source = get_with_source("library", "motogp_path")
    assert value == "/media/Motorsport/MotoGP"
    assert source == "settings.yml"


def test_env_var_beats_settings_yml(config_dir, monkeypatch):
    monkeypatch.setenv("MOTOGP_LIBRARY_PATH", "/media/Other/MotoGP")
    value, source = get_with_source("library", "motogp_path")
    assert value == "/media/Other/MotoGP"
    assert source == "env"


def test_blank_env_var_means_unset(config_dir, monkeypatch):
    """
    A container template ships blank fields for optional overrides. If an empty
    string counted as a value it would blank out settings.yml - which is worse
    than the baked-default problem it replaced.
    """
    monkeypatch.setenv("MOTOGP_LIBRARY_PATH", "")
    value, source = get_with_source("library", "motogp_path")
    assert value == "/media/Motorsport/MotoGP"
    assert source == "settings.yml"


def test_default_is_last_resort(config_dir):
    value, source = get_with_source("plex", "library_name", "fallback")
    assert value == "Motorsport - MotoGP"  # from settings.yml
    value, source = get_with_source("plex", "url", "http://default")
    assert value == "http://default"
    assert source == "default"


def test_nested_key_reading(config_dir):
    assert get("plex", "path_mapping.container") == "/media"
    assert get("plex", "path_mapping.missing") is None
    assert get("plex", "path_mapping.deeply.nested") is None


# ---------------------------------------------------------------------------
# The regression this work exists for
# ---------------------------------------------------------------------------

def test_plex_path_mapping_can_come_from_env(config_dir, monkeypatch):
    """PLEX_PATH_CONTAINER / PLEX_PATH_PLEX were documented but dead."""
    monkeypatch.setenv("PLEX_PATH_CONTAINER", "/media")
    monkeypatch.setenv("PLEX_PATH_PLEX", "/data/media")

    assert get_plex_path_mapping() == {"container": "/media", "plex": "/data/media"}
    assert plex_path_mapping_complete()


def test_plex_path_mapping_falls_back_to_settings_yml(config_dir):
    assert get_plex_path_mapping() == {"container": "/media", "plex": "/data/media"}
    assert plex_path_mapping_complete()


def test_plex_path_mapping_half_configured_is_incomplete(config_dir, monkeypatch):
    # Remove the pair from settings.yml, then supply only one side via env.
    (config_dir / "settings.yml").write_text(
        yaml.safe_dump({"library": SETTINGS["library"]})
    )
    config_loader._settings_cache = None
    monkeypatch.setenv("PLEX_PATH_CONTAINER", "/media")

    assert get_plex_path_mapping() == {"container": "/media", "plex": None}
    assert not plex_path_mapping_complete()


def test_translate_path_uses_mapping(config_dir):
    translated = translate_path_for_plex("/media/Motorsport/MotoGP/2026/x.mkv")
    assert translated == "/data/media/Motorsport/MotoGP/2026/x.mkv"


def test_translate_path_is_a_no_op_without_mapping(config_dir, monkeypatch):
    (config_dir / "settings.yml").write_text(yaml.safe_dump({"library": SETTINGS["library"]}))
    config_loader._settings_cache = None
    assert translate_path_for_plex("/media/x.mkv") == "/media/x.mkv"


# ---------------------------------------------------------------------------
# The crash that was one missing ENV away
# ---------------------------------------------------------------------------

def test_timezone_works_without_tz_set(config_dir, monkeypatch):
    """get_timezone() used to raise TypeError when TZ was unset."""
    monkeypatch.delenv("TZ", raising=False)
    assert get_timezone() == "Europe/London"


def test_timezone_prefers_tz(config_dir, monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    assert get_timezone() == "America/New_York"


# ---------------------------------------------------------------------------
# Startup report
# ---------------------------------------------------------------------------

def test_startup_report_reports_sources_and_missing_paths(config_dir, monkeypatch):
    monkeypatch.setenv("MOTOGP_LIBRARY_PATH", str(config_dir))  # exists
    report = startup_report()

    library = next(p for p in report["paths"] if p["key"] == "motogp_path")
    assert library["source"] == "env"
    assert library["env_var"] == "MOTOGP_LIBRARY_PATH"
    assert library["exists"] is True

    downloads = next(p for p in report["paths"] if p["key"] == "downloads_path")
    assert downloads["exists"] is False
    assert downloads["source"] == "settings.yml"
    assert any("Downloads path" in w for w in report["warnings"])


def test_startup_report_warns_about_missing_plex_mapping(config_dir):
    (config_dir / "settings.yml").write_text(
        yaml.safe_dump({"library": {"motogp_path": str(config_dir)}})
    )
    config_loader._settings_cache = None
    report = startup_report()
    assert report["plex_path_mapping_complete"] is False
    assert any("Plex path mapping" in w for w in report["warnings"])


def test_env_help_lists_every_variable():
    text = env_help()
    for name in all_known_env_vars():
        assert name in text


def test_entrypoint_variables_are_not_claimed_by_python():
    """
    MOTORSPORTARR_CALENDAR_UPDATE is read by entrypoint.sh, not by Python. Listing it
    in ENV_SPEC would make "the app reads this" untrue, which is the same class
    of lie as the dead PLEX_PATH_* variables.
    """
    assert "MOTORSPORTARR_CALENDAR_UPDATE" not in ENV_SPEC
    assert "MOTORSPORTARR_CALENDAR_UPDATE" in ENTRYPOINT_ENV

    entrypoint = (REPO_ROOT / "entrypoint.sh").read_text()
    for name in ENTRYPOINT_ENV:
        assert name in entrypoint, f"{name} declared but never used by entrypoint.sh"


def test_informational_variables_are_set_by_the_dockerfile():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    for name in INFORMATIONAL_ENV:
        assert name in dockerfile, f"{name} is not set by the Dockerfile"


# ---------------------------------------------------------------------------
# Agreement between the registry and the Unraid template
# ---------------------------------------------------------------------------

def _template_variable_targets():
    root = ET.parse(TEMPLATE).getroot()
    return {
        c.get("Target"): c
        for c in root.findall("Config")
        if c.get("Type") == "Variable"
    }


def test_template_sets_no_variable_the_project_does_not_read():
    unknown = set(_template_variable_targets()) - all_known_env_vars()
    assert not unknown, f"template sets variables nothing reads: {unknown}"


def test_every_user_facing_variable_is_offered_by_the_template():
    """
    Anything a user might reasonably want to set should be reachable from the
    template; otherwise the only way to set it is to hand-edit settings.yml.
    """
    template = set(_template_variable_targets())
    optional_in_template = {"TZ", "MOTOGPARR_CALENDAR_UPDATE"}
    missing = set(ENV_SPEC) - template - optional_in_template
    assert not missing, f"not offered by the template: {missing}"


def test_template_path_overrides_are_blank():
    """
    A non-blank default would silently beat settings.yml and recreate the
    original bug: edit the path in one place, watch the other win.
    """
    for name in ("MOTOGP_LIBRARY_PATH", "MOTOGP_DOWNLOADS_PATH",
                 "PLEX_PATH_CONTAINER", "PLEX_PATH_PLEX"):
        entry = _template_variable_targets()[name]
        assert not (entry.text or "").strip(), f"{name} must ship blank"
        assert not (entry.get("Default") or "").strip(), f"{name} Default must be blank"


# ---------------------------------------------------------------------------
# Grabber defaults: an empty allowlist silently grabs nothing
# ---------------------------------------------------------------------------

def test_allowed_groups_falls_back_to_the_built_in_default(config_dir):
    """
    filter_releases() rejects any release whose group is not in this list, so an
    empty list means nothing is ever grabbed - and an empty candidate list looks
    exactly like "no releases published yet". A duplicated function definition
    once made this return [] whenever allowed_groups was unset.
    """
    from utils.config_loader import DEFAULT_ALLOWED_GROUPS, get_allowed_groups

    assert get_allowed_groups() == list(DEFAULT_ALLOWED_GROUPS)
    assert get_allowed_groups(), "an empty allowlist means the grabber never matches"


def test_allowed_groups_default_agrees_with_the_grabber(config_dir):
    """The default is duplicated in motogp_common.filter_releases()."""
    from utils.config_loader import DEFAULT_ALLOWED_GROUPS, get_allowed_groups
    from motogp_common import filter_releases, SessionType

    release = {
        "title": "MotoGP.2026.Round01.Thailand.Race.1080p.WEB.h264-VERUM",
        "size": 1000, "link": "x",
    }
    # Calendars name classes by folder key ("motogp"), not display name; the
    # round gate compares against the session's own class, which is why this
    # fixture has to match the convention every real calendar uses.
    round_info = {"round": 1, "venue": "Thailand", "aliases": [], "classes": ["motogp"]}

    kept = filter_releases(
        [release], SessionType.MOTOGP_RACE, 2026, round_info,
        "1080p", get_allowed_groups(), None,
    )
    assert kept, (
        "a release from a default group did not survive filtering - the "
        "config_loader default and the grabber default have drifted apart"
    )


def test_allowed_groups_accepts_a_list_and_a_comma_string(config_dir):
    from utils.config_loader import get_allowed_groups

    settings = config_dir / "settings.yml"
    settings.write_text("grabber:\n  allowed_groups: [ALPHA, beta]\n")
    config_loader.reload_settings()
    assert get_allowed_groups() == ["ALPHA", "beta"]

    settings.write_text("grabber:\n  allowed_groups: ALPHA, beta ,\n")
    config_loader.reload_settings()
    assert get_allowed_groups() == ["ALPHA", "beta"]
    config_loader.reload_settings()


def test_configured_but_empty_allowed_groups_falls_back(config_dir):
    """
    `allowed_groups:` with nothing after it is a YAML mistake, not a request to
    grab nothing for a whole season.
    """
    from utils.config_loader import DEFAULT_ALLOWED_GROUPS, get_allowed_groups

    (config_dir / "settings.yml").write_text("grabber:\n  allowed_groups:\n")
    config_loader.reload_settings()
    assert get_allowed_groups() == list(DEFAULT_ALLOWED_GROUPS)
    config_loader.reload_settings()
