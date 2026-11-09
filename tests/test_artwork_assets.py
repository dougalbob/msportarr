"""Artwork is shared in the image, but Plex receives independent JPEGs.

Issue #25: every venue/class placeholder was an identical ~2 MB poster and
~1 MB fanart. Git deduplicated the blobs, but Docker copied all 138 files into
the image. The calendar filenames remain stable; only their source files are
now relative links to two masters. Custom artwork can replace a link with a
real JPEG without changing the calendar or the overlay code.
"""

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from motogp_common import CLASS_ASSET_DIRS, SessionType
from series import all_series, runtime

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
MASTERS = {
    "poster": ASSETS / "motogpPoster.jpg",
    "fanart": ASSETS / "motogpFanart.jpg",
}


def _calendars():
    """Every season calendar of every registered series."""
    return [
        path for spec in all_series()
        for path in sorted((ROOT / "config").glob(f"{spec.calendar_prefix}_*.json"))
    ]


def _asset_dir(class_key, kind):
    """The artwork directory for a class, whichever series declares it."""
    for spec in all_series():
        dirs = runtime(spec.key).asset_dirs.get(class_key)
        if dirs:
            return dirs[kind]
    raise AssertionError(f"no series declares artwork for class {class_key!r}")


def _digest(path):
    return hashlib.sha256(path.read_bytes()).digest()


def test_unused_2025_external_artwork_is_gone():
    """The old pre-rendered images had no runtime callers and dominated the image."""
    assert not (ASSETS / "external" / "2025").exists()


def test_every_calendar_asset_resolves_inside_the_image():
    """A dangling or escaping link would silently leave Plex without artwork."""
    referenced = set()
    for calendar_path in _calendars():
        calendar = json.loads(calendar_path.read_text(encoding="utf-8"))
        for round_info in calendar["rounds"]:
            for class_key, artwork in round_info.get("assets", {}).items():
                for kind, filename in artwork.items():
                    source = _asset_dir(class_key, kind) / filename
                    assert source.is_file(), f"{calendar_path.name}: missing {source}"
                    if source.is_symlink():
                        assert not source.readlink().is_absolute(), source
                        assert source.resolve().is_relative_to(ASSETS), source
                    referenced.add(source)
    assert referenced, "no calendar artwork was checked"


@pytest.mark.parametrize("class_key", ("motogp", "moto2", "moto3"))
@pytest.mark.parametrize("kind", ("poster", "fanart"))
def test_placeholders_are_links_not_duplicate_jpegs(class_key, kind):
    """Keep future venue art possible, but never reintroduce copied placeholders."""
    master = MASTERS[kind]
    directory = CLASS_ASSET_DIRS[class_key][kind]
    files = sorted(directory.glob("*.jpg"))
    assert files, f"no {kind} for {class_key}"
    for source in files:
        assert source.is_file(), f"broken link: {source}"
        if source.is_symlink():
            assert source.readlink() == Path("..") / master.name, source
            assert source.samefile(master), source
        else:
            # A real, venue-specific replacement is fine. An unchanged copy of
            # the shared placeholder would inflate the image again.
            assert _digest(source) != _digest(master), f"copied placeholder: {source}"


@pytest.mark.parametrize("session", (
    SessionType.MOTOGP_RACE, SessionType.MOTO2_RACE, SessionType.MOTO3_RACE,
))
def test_sorter_writes_real_artwork_without_modifying_shared_source(tmp_path, monkeypatch, session):
    """Plex has its own mount: it must receive files, not links into /app/assets."""
    import motogp_sort as sorter

    monkeypatch.setattr(sorter, "DRY_RUN", False)
    round_info = json.loads((ROOT / "config" / "motogp_2026.json").read_text())["rounds"][0]
    original = {kind: _digest(path) for kind, path in MASTERS.items()}

    sorter.apply_all_artwork(str(tmp_path), round_info, session)

    for name in ("poster.jpg", "fanart.jpg", "thumb.jpg"):
        output = tmp_path / name
        assert output.is_file() and not output.is_symlink(), name
        with Image.open(output) as image:
            image.verify()
    assert {kind: _digest(path) for kind, path in MASTERS.items()} == original


# ---------------------------------------------------------------------------
# F1: per-venue artwork, the opposite packaging decision to MotoGP's
# ---------------------------------------------------------------------------

def test_f1_ships_real_per_venue_artwork_for_every_calendar_reference():
    """
    F1's art is genuinely per venue, so it cannot be a link to a shared master.

    MotoGP's placeholders are links because every venue used the same image;
    F1's are real files because they are not. Asserting the *files* exist is
    what stops a future "let's shrink the repo" change from replacing 50 real
    JPEGs with 50 links to one and leaving Plex showing the same poster all
    season - which no test would otherwise notice.
    """
    referenced = 0
    for path in sorted((ROOT / "config").glob("f1_*.json")):
        calendar = json.loads(path.read_text(encoding="utf-8"))
        for round_info in calendar["rounds"]:
            artwork = round_info.get("assets", {}).get("f1", {})
            for kind, filename in artwork.items():
                source = _asset_dir("f1", kind) / filename
                assert source.is_file(), f"{path.name}: missing {source}"
                assert not source.is_symlink(), source
                referenced += 1
    assert referenced >= 40, "the ported F1 calendars reference no artwork"


def test_f1_artwork_is_not_a_copy_of_the_motogp_masters():
    """A placeholder copied into the F1 directories is the bloat issue #25 fixed."""
    for kind, master in MASTERS.items():
        for source in sorted(_asset_dir("f1", kind).glob("*.jpg")):
            assert _digest(source) != _digest(master), source


@pytest.mark.parametrize("directory", ("f1_fanart", "f1_posters"))
def test_f1_artwork_stays_small_enough_to_commit(directory):
    """
    The originals are 87 MB (2752x1536 fanart, ~1 MB posters).

    They are committed at a size that keeps a clone reasonable, and the sorter
    resizes to 1920x1080 / 1000x1500 before Plex ever sees them, so the source
    resolution above those targets buys nothing. The budget is per directory
    and generous - it exists to catch artwork being re-added at full size, not
    to pin today's byte count.
    """
    total = sum(p.stat().st_size for p in (ASSETS / directory).glob("*.jpg"))
    assert total < 9 * 1024 * 1024, f"{directory} is {total / 1e6:.1f} MB"


def test_the_f1_artwork_has_not_been_shrunk_below_usable_resolution():
    """
    A floor, not the sorter's target - and the difference is recorded here.

    22 of the 25 fanarts are 1376x768, which is smaller than the 1920x1080 the
    sorter resizes to, so Plex will receive an upscaled image for those
    venues. That is how f1arr ships them and this change is a port, not an
    art project. The floor exists so a future "shrink the repo" pass cannot
    quietly replace real artwork with thumbnails; 1280x720 is the point below
    which a 1080p upscale stops being presentable.
    """
    for source in sorted((ASSETS / "f1_fanart").glob("*.jpg")):
        with Image.open(source) as image:
            assert image.width >= 1280 and image.height >= 720, (
                source.name, image.size)


def test_the_f1_posters_keep_their_aspect_ratio_source_unstretched():
    """
    Four source posters are ~0.79 rather than 2:3 (Australia, China, Las Vegas,
    Abu Dhabi). They are stored as they are; ``motogp_sort.resize_image()``
    resizes to a fixed 1000x1500 target, so those four will be *stretched*
    when they reach Plex - a pre-existing defect in the resize path that has
    never been visible before, because MotoGP's placeholders are already
    1000x1500 and never needed squeezing. Fixing it means fit-within-target
    padding or cropping, which belongs with the series-aware sorter, not with
    the artwork port. This assertion pins that the sources are at least not
    *also* distorted on the way into the repository.
    """
    stretched = []
    for source in sorted((ASSETS / "f1_posters").glob("*.jpg")):
        with Image.open(source) as image:
            ratio = image.width / image.height
            assert image.width >= 600, (source.name, image.size)
            if abs(ratio - 2 / 3) > 0.02:
                stretched.append(source.name)
    assert len(stretched) == 4, stretched


def test_the_artwork_lookup_resolves_every_f1_reference_through_the_registry():
    """
    What ``motogp_sort.apply_round_artwork()`` will use once it is series-aware.

    The sorter still reads the MotoGP-bound ``CLASS_ASSET_DIRS``, which has no
    ``f1`` key - so wiring it up is the series-aware-sorter slice (step 6), not
    this one. This test pins the half that *is* done: given the F1 declaration,
    the same ``round_info["assets"][class_key][kind]`` lookup the sorter
    performs finds a real file for every round in both F1 calendars. A missing
    directory or a renamed JPEG fails here rather than in a container.
    """
    f1 = runtime("f1")
    sessions_by_class = {}
    for session in f1.session_type:
        if session.race_class is not None:
            sessions_by_class.setdefault(session.race_class.folder_key, []).append(session)
    assert set(sessions_by_class) == {"f1"}
    assert set(f1.asset_dirs) == {"f1"}

    for path in sorted((ROOT / "config").glob("f1_*.json")):
        calendar = json.loads(path.read_text(encoding="utf-8"))
        for round_info in calendar["rounds"]:
            class_key = "f1"
            for kind in ("fanart", "poster"):
                filename = round_info["assets"][class_key][kind]
                assert (f1.asset_dirs[class_key][kind] / filename).is_file(), (
                    path.name, round_info["round"], kind)
