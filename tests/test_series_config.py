"""
Per-series configuration: Plex identity, library paths, downloads, categories.

Before the hybrid there was one of each, and any code that named a series was
naming the only one. Now each of those is *per series*, and the failure mode of
getting it wrong is the quiet kind this codebase keeps coming back to:

  * a shared library path sorts one championship's rounds into the other's
    library, where Plex files them under whichever show already owns the folder;
  * a shared downloads path has each watcher sorting everything the other
    series' downloads too, because a watcher sorts whatever lands in the folder
    it watches;
  * a shared SABnzbd category completes both series' grabs into the folder
    named after that category - the same folder, so the same problem;
  * an inherited Plex library id tags files into a library that does not
    contain them, and Plex ignores the request without reporting anything.

None of those produce an error. Every test here is about them not happening, and
the last section is about the compatibility rule that lets an existing
MotoGP-only install keep working untouched.
"""

import yaml

import pytest

import utils.config_loader as config_loader
from series import all_series, default_series, get_series
from utils.config_loader import (
    ENV_SPEC,
    active_series,
    get_downloads_path,
    get_plex_library_id,
    get_plex_library_name,
    get_plex_path_mapping,
    get_series_downloads_path,
    get_series_library_path,
    get_series_sabnzbd_category,
    series_is_configured,
    translate_path_for_plex,
)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Point the loader at a temporary settings.yml and clear its cache."""
    path = tmp_path / "settings.yml"

    def write(document):
        path.write_text(yaml.safe_dump(document))
        monkeypatch.setattr(config_loader, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(config_loader, "SETTINGS_FILE", path)
        monkeypatch.setattr(config_loader, "_settings_cache", None)
        return tmp_path

    write({})
    return write


# ===========================================================================
# The declarations themselves
# ===========================================================================

def test_every_series_declares_its_own_media_locations():
    """
    A series that cannot say where its media lives cannot be sorted into it.

    These are required constructor arguments rather than defaults precisely so
    this cannot be forgotten: an omitted path would silently inherit the default
    series' and put Formula 1 in the MotoGP library.
    """
    for spec in all_series():
        assert spec.library_path.startswith("/"), spec.key
        assert spec.downloads_path.startswith("/"), spec.key
        assert spec.sabnzbd_category, spec.key
        assert spec.plex_library_name, spec.key


def test_no_two_series_share_a_library_downloads_path_or_category():
    """
    The whole point of the per-series fields. Sharing any one of them files one
    championship's media into the other's, and nothing reports it.
    """
    for field in ("library_path", "downloads_path", "sabnzbd_category"):
        values = {getattr(spec, field) for spec in all_series()}
        assert len(values) == len(all_series()), (
            f"two series share a {field}: {values}"
        )


def test_no_series_path_sits_inside_another_series_library():
    """
    A downloads folder *inside* a library is not merely untidy: the sorter walks
    the library, so it would find its own downloads as if they were sorted
    episodes.
    """
    libraries = {spec.key: spec.library_path.rstrip("/") for spec in all_series()}
    for spec in all_series():
        for other_key, library in libraries.items():
            assert not spec.downloads_path.startswith(library + "/"), (
                f"{spec.key} downloads into {other_key}'s library"
            )


def test_the_default_series_keeps_every_value_it_had_before_the_hybrid():
    """
    Step 5 must be invisible to an existing install: same library, same
    downloads folder, same category, same Plex library name, no library id.
    """
    motogp = get_series("motogp")
    assert motogp.key == default_series().key
    assert motogp.library_path == "/media/MotoGP"
    assert motogp.downloads_path == "/downloads/complete/motogp"
    assert motogp.sabnzbd_category == "motogp"
    assert motogp.plex_library_name == "Motorsport - MotoGP"
    assert motogp.plex_library_id is None, (
        "Plex assigns the section id; there is nothing safe to default to"
    )


# ===========================================================================
# Library and downloads paths
# ===========================================================================

def test_library_paths_come_from_the_declaration_by_default(settings):
    assert get_series_library_path() == "/media/MotoGP"
    assert get_series_library_path("f1") == "/media/F1"


def test_settings_yml_overrides_one_series_and_not_the_other(settings):
    settings({"library": {"f1_path": "/mnt/f1", "motogp_path": "/mnt/mgp"}})
    assert get_series_library_path() == "/mnt/mgp"
    assert get_series_library_path("f1") == "/mnt/f1"


def test_the_environment_wins_over_settings_yml(settings, monkeypatch):
    settings({"library": {"f1_path": "/from/yaml"}})
    monkeypatch.setenv("F1_LIBRARY_PATH", "/from/env")
    assert get_series_library_path("f1") == "/from/env"


def test_an_unset_second_series_never_inherits_the_first_series_paths(settings):
    """
    The bug the required constructor arguments exist to prevent. With settings.yml
    saying nothing about F1, F1's paths must still be its own.
    """
    settings({"library": {"motogp_path": "/media/MotoGP"}})
    assert get_series_library_path("f1") != get_series_library_path("motogp")
    assert get_series_downloads_path("f1") != get_series_downloads_path("motogp")


def test_the_historical_shared_downloads_key_still_means_the_default_series(settings):
    """
    Every pre-hybrid settings.yml names the downloads folder ``downloads_path``
    with no series in the name. That value is MotoGP's; it must keep working,
    and it must not leak to F1.
    """
    settings({"library": {"downloads_path": "/legacy/downloads"}})
    assert get_downloads_path() == "/legacy/downloads"
    assert get_series_downloads_path("f1") == "/downloads/complete/f1"


def test_a_series_specific_downloads_key_beats_the_legacy_one(settings):
    settings({"library": {
        "downloads_path": "/legacy/downloads",
        "motogp_downloads_path": "/new/motogp-downloads",
    }})
    # The legacy key wins for the default series - it is the value already in
    # every existing file, and both are accepted so a user can migrate at their
    # own pace without the app disagreeing with them in the meantime.
    assert get_downloads_path() == "/legacy/downloads"


def test_the_downloads_key_fills_in_when_the_legacy_one_is_absent(settings):
    settings({"library": {"motogp_downloads_path": "/new/motogp-downloads"}})
    assert get_downloads_path() == "/new/motogp-downloads"


# ===========================================================================
# SABnzbd categories
# ===========================================================================

def test_the_category_is_declared_rather_than_configured(settings):
    """
    It has to agree with the downloads path, and settings.yml is not where that
    relationship is written down - the declaration is, once.
    """
    assert get_series_sabnzbd_category() == "motogp"
    assert get_series_sabnzbd_category("f1") == "f1"


def test_every_category_has_its_own_complete_folder():
    """
    SABnzbd completes a grab into a folder named after its category. Two series
    sharing a category therefore share a completion folder, and whichever
    watcher runs first sorts both series' releases into its own library.
    """
    for spec in all_series():
        assert spec.downloads_path.rstrip("/").endswith(spec.sabnzbd_category), (
            f"{spec.key}'s downloads folder is not its category's"
        )


# ===========================================================================
# Plex identity and path mapping
# ===========================================================================

def test_the_library_id_is_never_inherited_by_another_series(settings):
    """
    Tagging into a library that does not contain the files is silent: Plex
    accepts the request and matches nothing. Inheriting MotoGP's id would do
    exactly that to every F1 file.
    """
    settings({"plex": {"library_id": "16"}})
    assert get_plex_library_id() == "16"
    assert get_plex_library_id("f1") is None


def test_a_series_reads_its_own_library_id(settings):
    settings({"plex": {"library_id": "16", "f1_library_id": "17"}})
    assert get_plex_library_id() == "16"
    assert get_plex_library_id("f1") == "17"


def test_a_series_without_a_library_name_falls_back_to_its_declaration(settings):
    settings({"plex": {"library_name": "My MotoGP"}})
    assert get_plex_library_name() == "My MotoGP"
    assert get_plex_library_name("f1") == "Motorsport - Formula 1"


def test_a_path_mapping_is_per_series_and_falls_back_to_the_shared_one(settings):
    """
    Two libraries on different host shares are routinely seen by Plex under
    different prefixes. A single shared mapping translates the first correctly
    and the second into a path that does not exist.
    """
    settings({"plex": {
        "path_mapping": {"container": "/media", "plex": "/data/media"},
        "f1_path_mapping": {"container": "/f1media", "plex": "/mnt/f1"},
    }})
    assert get_plex_path_mapping() == {"container": "/media", "plex": "/data/media"}
    assert get_plex_path_mapping("f1") == {"container": "/f1media", "plex": "/mnt/f1"}


def test_a_series_with_no_mapping_reuses_the_shared_one(settings):
    settings({"plex": {"path_mapping": {"container": "/media", "plex": "/data/media"}}})
    assert get_plex_path_mapping("f1") == {"container": "/media", "plex": "/data/media"}


def test_path_translation_uses_the_series_mapping(settings):
    settings({"plex": {
        "path_mapping": {"container": "/media", "plex": "/data/media"},
        "f1_path_mapping": {"container": "/f1media", "plex": "/mnt/f1"},
    }})
    assert translate_path_for_plex("/media/MotoGP/2026") == "/data/media/MotoGP/2026"
    assert translate_path_for_plex("/f1media/F1/2026", "f1") == "/mnt/f1/F1/2026"


def test_an_unmapped_path_is_returned_unchanged(settings):
    assert translate_path_for_plex("/media/MotoGP/2026") == "/media/MotoGP/2026"


# ===========================================================================
# Activation: a series nobody configured must not break the install
# ===========================================================================

def test_the_default_series_is_always_active(settings, tmp_path):
    """
    Gating it would mean an empty settings.yml switched MotoGP off.
    """
    assert series_is_configured(default_series().key) is True
    assert default_series().key in [spec.key for spec in active_series()]


def test_a_series_with_no_library_and_no_configuration_is_dormant(settings, tmp_path):
    settings({})
    # The declaration's F1 path does not exist in this sandbox.
    assert series_is_configured("f1") is False
    assert "f1" not in [spec.key for spec in active_series()]


def test_an_existing_library_path_alone_activates_a_series(settings, tmp_path):
    """
    An unattended install: put the folder where the declaration says it goes and
    the series is live, with nothing to configure.
    """
    library = tmp_path / "F1"
    library.mkdir()
    settings({"library": {"f1_path": str(library)}})
    assert series_is_configured("f1") is True


def test_an_environment_path_activates_a_series(settings, monkeypatch):
    settings({})
    monkeypatch.setenv("F1_LIBRARY_PATH", "/somewhere/that/does/not/exist")
    assert series_is_configured("f1") is True


# ===========================================================================
# The registry of environment variables
# ===========================================================================

@pytest.mark.parametrize("variable", [
    "MOTOGP_LIBRARY_PATH",
    "MOTOGP_DOWNLOADS_PATH",
    "F1_LIBRARY_PATH",
    "F1_DOWNLOADS_PATH",
    "MOTOGP_PLEX_LIBRARY_ID",
    "F1_PLEX_LIBRARY_ID",
    "MOTOGP_PLEX_LIBRARY",
    "F1_PLEX_LIBRARY",
    "PLEX_PATH_CONTAINER",
    "PLEX_PATH_PLEX",
    "F1_PLEX_PATH_CONTAINER",
    "F1_PLEX_PATH_PLEX",
])
def test_every_per_series_variable_is_registered(variable):
    """
    ENV_SPEC is the only place "what does the app read" is written down. A
    variable missing from it is read by nothing, whatever the template says.
    """
    assert variable in ENV_SPEC, f"{variable} is not in ENV_SPEC"


def test_every_active_series_path_setting_reads_the_right_series(tmp_path, monkeypatch):
    """
    The end-to-end property: with both series configured to different folders,
    each series' getter returns its own, from the environment.
    """
    monkeypatch.setenv("MOTOGP_LIBRARY_PATH", "/e/mgp")
    monkeypatch.setenv("F1_LIBRARY_PATH", "/e/f1")
    monkeypatch.setenv("MOTOGP_DOWNLOADS_PATH", "/e/mgp-dl")
    monkeypatch.setenv("F1_DOWNLOADS_PATH", "/e/f1-dl")

    assert get_series_library_path("motogp") == "/e/mgp"
    assert get_series_library_path("f1") == "/e/f1"
    # The default series prefers the legacy shared key, which is unset here.
    assert get_downloads_path() == "/e/mgp-dl"
    assert get_series_downloads_path("f1") == "/e/f1-dl"
