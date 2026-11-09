"""
Marking a release as a mismatch, and making the replacement pick something else.

The failure this addresses is a loop, not a crash. The matcher selects a release
whose content is the wrong session; the user deletes it and asks for the session
again; the same rules see the same candidates and select the same wrong release.
Nothing errors, so nothing in the log looks wrong, and the only fix used to be a
code change.

These tests hold the three things that break that loop:

    1. the recorded title is genuinely excluded from later filters
    2. the exclusion is narrow enough that it cannot swallow a good release
    3. the existing library folder is moved, never deleted and never nested
       inside by the replacement sort

They also hold the diagnostic contract: the explanation of *why* a release was
accepted comes from the same code that accepted it, so the checklist in the UI
cannot drift from the rules.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

import motogp_common as common
from motogp_common import SessionType
from utils import release_mismatches as mismatches

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolate the mismatch store and return its paths."""
    state = tmp_path / "release_mismatches.motogp.json"
    monkeypatch.setattr(mismatches, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(mismatches, "MISMATCH_FILE", state)
    monkeypatch.setattr(mismatches, "LOCK_FILE",
                        tmp_path / "release_mismatches.motogp.lock")
    return state


@pytest.fixture
def round_info():
    return {
        "round": 7,
        "venue": "Italy",
        "aliases": ["italian", "mugello", "tuscany"],
        "folder": "Round 07 Italy",
        "date": "2026-05-31",
    }


GOOD = "MotoGP.2026.Round07.Italy.Race.1080p.WEB.h264-MWR"
WRONG_ROUND = "MotoGP.2026.Round08.Italy.Race.1080p.WEB.h264-MWR"
OTHER_GROUP = "MotoGP.2026.Round07.Italy.Race.1080p.WEB.h264-ALPHA"


def _filter(titles, round_info, excluded=None):
    return common.filter_releases(
        [{"title": t, "size": 1000} for t in titles],
        SessionType.MOTOGP_RACE,
        2026,
        round_info,
        "1080p",
        ["MWR", "ALPHA"],
        None,
        excluded_titles=excluded,
    )


# ---------------------------------------------------------------------------
# The loop: a marked release is not selected again
# ---------------------------------------------------------------------------

def test_marked_release_is_excluded_from_the_next_search(store, round_info):
    """
    The behaviour the feature exists for. Without the exclusion the replacement
    request is a no-op: the same release scores highest again.
    """
    assert len(_filter([GOOD, WRONG_ROUND], round_info)) == 2

    mismatches.add_mismatch(
        2026, 7, "MOTOGP_RACE", WRONG_ROUND, reason="Plex showed Round 8",
    )

    blocked = mismatches.blocked_titles(2026, 7, "MOTOGP_RACE")
    kept = _filter([GOOD, WRONG_ROUND], round_info, excluded=blocked)

    assert [r["title"] for r in kept] == [GOOD]


def test_exclusion_is_normalised_but_not_fuzzy(store, round_info):
    """
    Indexers re-punctuate titles. The same release with dots instead of spaces
    must still be excluded - but a *different* release must not be, because
    silently dropping a good candidate is undiagnosable from the UI.
    """
    mismatches.add_mismatch(2026, 7, "MOTOGP_RACE", WRONG_ROUND)

    blocked = mismatches.blocked_titles(2026, 7, "MOTOGP_RACE")
    respaced = WRONG_ROUND.replace(".", " ")

    assert _filter([respaced], round_info, excluded=blocked) == []

    # A different quality tag is a different release and stays available.
    other_quality = "MotoGP.2026.Round07.Italy.Race.1080i.WEB.h264-MWR"
    kept = _filter([other_quality], round_info, excluded=blocked)
    assert [r["title"] for r in kept] == [other_quality]


def test_session_scope_does_not_block_another_session(store, round_info):
    """
    A Race release wrongly filed as Practice is wrong *for Practice*. Blocking
    it for every session of the round would throw away the release that is
    actually wanted for the Race.
    """
    mismatches.add_mismatch(2026, 7, "MOTOGP_PRACTICE", WRONG_ROUND)

    assert mismatches.blocked_titles(2026, 7, "MOTOGP_PRACTICE")
    assert mismatches.blocked_titles(2026, 7, "MOTOGP_RACE") == set()


def test_global_scope_blocks_every_session(store):
    """An off-by-one round number is wrong everywhere, so global must work."""
    mismatches.add_mismatch(2026, 7, "MOTOGP_RACE", WRONG_ROUND, scope="global")

    for session in ("MOTOGP_RACE", "MOTOGP_PRACTICE", "MOTO2_RACE"):
        assert mismatches.blocked_titles(2026, 7, session), session
    # ... and for any round, not just the one it was marked from.
    assert mismatches.blocked_titles(2026, 12, "MOTO3_RACE")


def test_marking_the_same_release_twice_updates_rather_than_duplicates(store):
    """The list is a set of decisions, not a log of button presses."""
    first = mismatches.add_mismatch(2026, 7, "MOTOGP_RACE", WRONG_ROUND, reason="a")
    second = mismatches.add_mismatch(2026, 7, "MOTOGP_RACE", WRONG_ROUND, reason="b")

    entries = mismatches.load_entries()
    assert len(entries) == 1
    assert first["id"] == second["id"]
    assert entries[0]["reason"] == "b"
    assert entries[0]["recorded_at"] == first["recorded_at"], (
        "the original decision time must survive an update - it is the date the "
        "user needs when reviewing why a session is blocked"
    )


def test_unblocking_restores_the_release(store, round_info):
    entry = mismatches.add_mismatch(2026, 7, "MOTOGP_RACE", WRONG_ROUND)
    assert mismatches.remove_mismatch(entry["id"]) is True
    assert mismatches.remove_mismatch(entry["id"]) is False, "second removal is a no-op"
    assert mismatches.blocked_titles(2026, 7, "MOTOGP_RACE") == set()
    assert len(_filter([WRONG_ROUND], round_info)) == 1


def test_a_title_is_required(store):
    """A block with no title would block nothing while looking like it worked."""
    with pytest.raises(ValueError):
        mismatches.add_mismatch(2026, 7, "MOTOGP_RACE", "   ")


def test_an_unknown_scope_is_rejected(store):
    with pytest.raises(ValueError):
        mismatches.add_mismatch(2026, 7, "MOTOGP_RACE", WRONG_ROUND, scope="everything")


# ---------------------------------------------------------------------------
# Persistence: shared between the WebUI and the grabber
# ---------------------------------------------------------------------------

def test_entries_survive_a_fresh_read(store):
    mismatches.add_mismatch(2026, 7, "MOTOGP_RACE", WRONG_ROUND, reason="test")
    underlying = json.loads(store.read_text())
    assert underlying["version"] == mismatches.STATE_VERSION
    assert underlying["entries"][0]["title"] == WRONG_ROUND


def test_a_corrupt_file_is_reported_and_does_not_crash_a_search(store, caplog):
    store.write_text("{ this is not json")
    with caplog.at_level("ERROR", logger="motogparr.mismatches"):
        assert mismatches.load_entries() == []
    assert "NOT being excluded" in caplog.text, (
        "a silent fallback would let the next search pick the same wrong release"
    )


def test_a_missing_file_means_no_mismatches(store):
    assert mismatches.load_entries() == []


def test_concurrent_writers_do_not_lose_entries(store):
    """
    The WebUI and the grabber are separate processes. A read-then-write without
    a lock across the whole transaction loses one of two simultaneous marks,
    and the user is then told a release is blocked when it is not.
    """
    import threading

    titles = [f"MotoGP.2026.Round07.Italy.Race.1080p.WEB.h264-G{i}" for i in range(8)]

    def mark(title):
        mismatches.add_mismatch(2026, 7, "MOTOGP_RACE", title)

    threads = [threading.Thread(target=mark, args=(t,)) for t in titles]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stored = {e["title"] for e in mismatches.load_entries()}
    assert stored == set(titles)


# ---------------------------------------------------------------------------
# The library folder: moved, not deleted, not nested
# ---------------------------------------------------------------------------

def _make_library(tmp_path, season=2026, round_num=7, session=SessionType.MOTOGP_RACE):
    root = tmp_path / "library"
    prefix = common.build_prefix(season, round_num, session)
    folder = root / str(season) / f"Round {round_num:02d} Italy" / f"{prefix} MotoGP Race"
    folder.mkdir(parents=True)
    (folder / f"{prefix} MotoGP Race.mkv").write_bytes(b"x" * 64)
    return root, folder, prefix


def test_quarantine_moves_the_folder_out_of_plex_view(tmp_path):
    root, folder, prefix = _make_library(tmp_path)

    result = mismatches.quarantine_library_session(str(root), 2026, 7, prefix)

    assert result["ok"], result["reason"]
    assert not folder.exists()
    moved = Path(result["to"])
    assert moved.is_dir()
    assert (moved / f"{prefix} MotoGP Race.mkv").exists(), "the media must survive"
    assert result["video_files"] == 1
    assert mismatches.QUARANTINE_DIRNAME in moved.parts
    assert moved.relative_to(root).parts[0].startswith("."), (
        "Plex skips dot-directories; a visible folder would keep showing as a "
        "second item"
    )


def test_quarantine_refuses_to_touch_a_different_round(tmp_path):
    """Only the folder whose prefix matches is ever moved."""
    root, folder, prefix = _make_library(tmp_path)
    other = root / "2026" / "Round 08 Hungary" / "26R08E82 MotoGP Race"
    other.mkdir(parents=True)

    result = mismatches.quarantine_library_session(str(root), 2026, 7, prefix)

    assert result["ok"]
    assert other.is_dir(), "a neighbouring round must not be touched"


def test_quarantine_reports_a_missing_session_instead_of_failing_silently(tmp_path):
    root, _, prefix = _make_library(tmp_path)
    result = mismatches.quarantine_library_session(str(root), 2026, 9, prefix)
    assert result["ok"] is False
    assert "not in the library" in result["reason"]


def test_library_lookup_requires_the_prefix_boundary(tmp_path):
    """
    ``26R07E82`` is a prefix of ``26R07E820``. Matching on startswith() alone
    would find the wrong folder, and then move it.
    """
    root = tmp_path / "library"
    (root / "2026" / "Round 07 Italy").mkdir(parents=True)
    decoy = root / "2026" / "Round 07 Italy" / "26R07E820 MotoGP Something"
    decoy.mkdir()

    assert mismatches.library_session_path(str(root), 2026, 7, "26R07E82") is None


def test_sorter_moves_the_existing_folder_aside_instead_of_nesting(tmp_path, monkeypatch):
    """
    The replacement arrives for a session that is already sorted. Without this,
    ``shutil.move(src, dst)`` puts the new folder *inside* the old one, and Plex
    keeps showing the wrong file.
    """
    import motogp_sort

    library = tmp_path / "library"
    existing = library / "2026" / "Round 07 Italy" / "26R07E82 MotoGP Race"
    existing.mkdir(parents=True)
    (existing / "old.mkv").write_bytes(b"old")
    monkeypatch.setattr(motogp_sort, "MOTOGP_LIBRARY_PATH", str(library))

    ok = motogp_sort._clear_existing_session_folder(str(existing))

    assert ok
    assert not existing.exists()
    parked = list((library / ".mismatched").rglob("*.mkv"))
    assert parked, "the old media is parked, not deleted"
    assert parked[0].read_bytes() == b"old"


def test_sorter_leaves_a_clean_destination_alone(tmp_path, monkeypatch):
    import motogp_sort

    library = tmp_path / "library"
    destination = library / "2026" / "Round 07 Italy" / "26R07E82 MotoGP Race"
    monkeypatch.setattr(motogp_sort, "MOTOGP_LIBRARY_PATH", str(library))
    assert motogp_sort._clear_existing_session_folder(str(destination)) is True
    assert not destination.exists()


def test_sorter_refuses_a_destination_outside_the_library(tmp_path, monkeypatch):
    """
    The quarantine path is derived from the configured library root. A folder
    outside it must be refused rather than moved somewhere nothing else knows
    about - the sorter is a subprocess and has no user to ask.
    """
    import motogp_sort

    library = tmp_path / "library"
    library.mkdir()
    elsewhere = tmp_path / "somewhere-else" / "2026" / "Round 07 Italy" / "26R07E82 MotoGP Race"
    elsewhere.mkdir(parents=True)
    (elsewhere / "old.mkv").write_bytes(b"old")
    monkeypatch.setattr(motogp_sort, "MOTOGP_LIBRARY_PATH", str(library))

    assert motogp_sort._clear_existing_session_folder(str(elsewhere)) is False
    assert elsewhere.is_dir(), "the folder must be left exactly where it was"


# ---------------------------------------------------------------------------
# The explanation must come from the matcher itself
# ---------------------------------------------------------------------------

def test_explain_release_reports_an_acceptance_with_its_evidence(round_info):
    analysis = common.explain_release(
        GOOD, SessionType.MOTOGP_RACE, 2026, round_info, "1080p", ["MWR"]
    )
    assert analysis.accepted is True
    assert analysis.score is not None
    names = [c["name"] for c in analysis.checks]
    assert names[0] == "global_exclusion"
    assert names[-1] == "session"
    assert all(c["passed"] for c in analysis.checks)
    round_check = next(c for c in analysis.checks if c["name"] == "round")
    assert "italy" in round_check["detail"]


def test_explain_release_names_the_failing_check_and_stops(round_info):
    """Checks after the first failure were never run, so they are not listed."""
    analysis = common.explain_release(
        "MotoGP.2026.Round07.Italy.Race.720p.WEB.h264-MWR",
        SessionType.MOTOGP_RACE, 2026, round_info, "1080p", ["MWR"],
    )
    assert analysis.accepted is False
    assert "quality" in analysis.reason
    assert [c["name"] for c in analysis.checks][-1] == "quality"


def test_explain_release_shows_the_round_confusion_the_feature_exists_for(round_info):
    """
    A Round 08 title accepted for Round 07: the venue matched, so the round-number
    check was never reached. This is exactly the evidence a user needs, and it is
    produced by the code that accepted the release.
    """
    analysis = common.explain_release(
        WRONG_ROUND, SessionType.MOTOGP_RACE, 2026, round_info, "1080p", ["MWR"]
    )
    assert analysis.accepted is True
    round_check = next(c for c in analysis.checks if c["name"] == "round")
    assert "venue/alias" in round_check["detail"]
    assert analysis.as_dict()["parsed"]["round"] == 8


def test_explain_release_reports_a_refused_title(round_info):
    """
    The classifier refuses an unidentifiable title rather than filing it as the
    Race. The explanation has to distinguish "rejected" from "refused" - the
    second one means the vocabulary is missing, not that the release is wrong.
    """
    analysis = common.explain_release(
        "MotoGP.2026.Round07.Italy.SP 1080p WEB h264-MWR",
        SessionType.MOTOGP_RACE, 2026, round_info, "1080p", ["MWR"],
    )
    assert analysis.accepted is False
    assert analysis.refused is True
    assert analysis.classification, "a refusal must say why"


def test_explain_release_recognises_an_existing_block(round_info):
    analysis = common.explain_release(
        WRONG_ROUND, SessionType.MOTOGP_RACE, 2026, round_info, "1080p", ["MWR"],
        excluded_titles=[WRONG_ROUND],
    )
    assert analysis.accepted is False
    assert "marked as a mismatch" in analysis.reason


# ---------------------------------------------------------------------------
# The WebUI surface
# ---------------------------------------------------------------------------

os.environ.setdefault(
    "CONFIG_DIR", tempfile.mkdtemp(prefix="motogp-mismatch-test-")
)
os.environ.setdefault("AUTH_MODE", "none")

import motogp_webui  # noqa: E402
import utils.config_loader as config_loader  # noqa: E402


@pytest.fixture
def webui(tmp_path, monkeypatch):
    import shutil

    for calendar in (REPO_ROOT / "config").glob("motogp_*.json"):
        shutil.copy2(calendar, tmp_path / calendar.name)

    library = tmp_path / "library"
    library.mkdir()

    import yaml

    (tmp_path / "settings.yml").write_text(yaml.safe_dump({
        "library": {"motogp_path": str(library), "downloads_path": str(tmp_path / "dl")},
        "grabber": {"required_quality": "1080p", "allowed_groups": ["MWR", "VERUM"]},
    }))

    state = tmp_path / "release_mismatches.motogp.json"
    monkeypatch.setattr(mismatches, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(mismatches, "MISMATCH_FILE", state)
    monkeypatch.setattr(mismatches, "LOCK_FILE",
                        tmp_path / "release_mismatches.motogp.lock")

    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(motogp_webui, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(motogp_webui, "MOTOGP_LIBRARY_PATH", str(library))
    monkeypatch.setattr(config_loader, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_loader, "SETTINGS_FILE", tmp_path / "settings.yml")
    monkeypatch.setattr(config_loader, "_settings_cache", None)

    motogp_webui.app.config.update(TESTING=True)
    with motogp_webui.app.test_client() as client:
        yield client, tmp_path, library

    config_loader._settings_cache = None


def _write_grabber_state(tmp_path, release, key="2026_R07_E82", nzo="nzo_1"):
    (tmp_path / "grabber_state.motogp.json").write_text(json.dumps({
        "grabbed": {key: {"timestamp": "2026-05-31T20:00:00", "release": release,
                          "nzo_id": nzo}}
    }))


def test_evidence_endpoint_returns_the_release_and_why_it_matched(webui):
    client, tmp_path, _ = webui
    _write_grabber_state(tmp_path, WRONG_ROUND)

    data = client.get(
        "/api/release-mismatch?season=2026&round=7&session=MOTOGP_RACE"
    ).get_json()

    assert data["grabbed"]["release"] == WRONG_ROUND
    assert data["analysis"]["accepted"] is True
    assert data["prefix"] == common.build_prefix(2026, 7, SessionType.MOTOGP_RACE)
    assert data["can_mark"] is True
    assert data["session_name"] == "MotoGP Race"


def test_evidence_endpoint_refuses_a_session_the_round_does_not_run(webui):
    """A stale page must not be able to block a session that does not exist."""
    client, _, _ = webui
    response = client.get(
        "/api/release-mismatch?season=2026&round=1&session=MOTOGP_FP3"
    )
    assert response.status_code == 400
    assert "does not run" in response.get_json()["error"]


def test_evidence_endpoint_rejects_an_unknown_session_name(webui):
    client, _, _ = webui
    response = client.get(
        "/api/release-mismatch?season=2026&round=7&session=NOT_A_SESSION"
    )
    assert response.status_code == 400


def test_marking_records_the_entry_and_moves_the_library_folder(webui):
    client, tmp_path, library = webui
    _write_grabber_state(tmp_path, WRONG_ROUND)

    prefix = common.build_prefix(2026, 7, SessionType.MOTOGP_RACE)
    folder = library / "2026" / "Round 07 Italy" / f"{prefix} MotoGP Race"
    folder.mkdir(parents=True)
    (folder / "wrong.mkv").write_bytes(b"x" * 32)

    response = client.post("/api/release-mismatch", json={
        "season": 2026,
        "round": 7,
        "session": "MOTOGP_RACE",
        "reason": "Plex showed Round 8",
        "quarantine": True,
    })

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "recorded"
    assert payload["library_action"]["ok"] is True
    assert not folder.exists()

    stored = mismatches.load_entries()
    assert len(stored) == 1
    assert stored[0]["title"] == WRONG_ROUND
    assert stored[0]["reason"] == "Plex showed Round 8"
    assert stored[0]["grabbed_at"] == "2026-05-31T20:00:00"


def test_marking_without_a_recorded_release_is_refused(webui):
    """No title means a block that blocks nothing, which looks like success."""
    client, _, _ = webui
    response = client.post("/api/release-mismatch", json={
        "season": 2026, "round": 7, "session": "MOTOGP_RACE",
    })
    assert response.status_code == 400
    assert "No release title" in response.get_json()["error"]


def test_marked_entry_is_then_reported_as_blocked(webui):
    client, tmp_path, _ = webui
    _write_grabber_state(tmp_path, WRONG_ROUND)

    client.post("/api/release-mismatch", json={
        "season": 2026, "round": 7, "session": "MOTOGP_RACE",
    })

    data = client.get(
        "/api/release-mismatch?season=2026&round=7&session=MOTOGP_RACE"
    ).get_json()

    assert [b["title"] for b in data["blocked"]] == [WRONG_ROUND]
    assert data["analysis"]["accepted"] is False, (
        "the dialog must say the block is in effect, not repeat the old verdict"
    )


def test_unblocking_through_the_api_removes_the_entry(webui):
    client, tmp_path, _ = webui
    _write_grabber_state(tmp_path, WRONG_ROUND)
    entry = client.post("/api/release-mismatch", json={
        "season": 2026, "round": 7, "session": "MOTOGP_RACE",
    }).get_json()["entry"]

    assert client.delete(f"/api/release-mismatch/{entry['id']}").status_code == 200
    assert client.delete(f"/api/release-mismatch/{entry['id']}").status_code == 404
    assert mismatches.load_entries() == []


def test_replacement_command_uses_the_element_number_not_the_enum_value(webui):
    """
    The session filter is parsed back into an element number by the grabber.
    ``int(SessionType.MOTOGP_RACE)`` is 21 and the element number is 82; using
    the enum value would search a different session entirely.
    """
    client, tmp_path, _ = webui
    _write_grabber_state(tmp_path, WRONG_ROUND)

    payload = client.post("/api/release-mismatch", json={
        "season": 2026, "round": 7, "session": "MOTOGP_RACE",
    }).get_json()

    assert "--force" in payload["replacement_command"], (
        "without --force the grabber's 30-minute cooldown skips the search and "
        "the UI reports a download that never happened"
    )
    assert "--sessions E82" in payload["replacement_command"]
    assert "--round 7" in payload["replacement_command"]


def test_grabber_search_excludes_marked_titles(monkeypatch, round_info):
    """
    The exclusion must be applied by the grabber itself, not only by the WebUI.
    A scheduled run has no browser involved.
    """
    import motogp_grabber as grabber

    monkeypatch.setattr(grabber, "_blocked_titles", lambda *a: {mismatches.normalise_title(WRONG_ROUND)})
    monkeypatch.setattr(grabber, "get_required_quality", lambda: "1080p")
    monkeypatch.setattr(grabber, "get_allowed_groups", lambda: ["MWR"])
    monkeypatch.setattr(grabber, "build_search_queries", lambda *a: ["q"])
    monkeypatch.setattr(grabber, "get_indexers", lambda: [])
    # search_indexer reports (releases, error) so a failure and an empty result
    # are distinguishable; the fake answers with the candidates and no error.
    monkeypatch.setattr(grabber, "search_indexer", lambda *a: ([
        {"title": GOOD, "guid": "1", "size": 1},
        {"title": WRONG_ROUND, "guid": "2", "size": 1},
    ], None))

    results = grabber.search_for_session(
        round_info, SessionType.MOTOGP_RACE, 2026,
        [{"id": 1, "name": "idx"}], None,
    )

    assert [r["title"] for r in results] == [GOOD]


def test_download_tab_offers_the_mismatch_button(webui):
    """The entry point the user asked for: on the session, in the Download tab."""
    client, _, _ = webui
    html = client.get("/library").get_data(as_text=True)
    assert "openMismatchModal" in html
    assert "mismatch-modal" in html


def test_mismatch_log_page_lists_entries(webui):
    client, tmp_path, _ = webui
    _write_grabber_state(tmp_path, WRONG_ROUND)
    client.post("/api/release-mismatch", json={
        "season": 2026, "round": 7, "session": "MOTOGP_RACE",
        "reason": "Plex showed Round 8",
    })

    html = client.get("/mismatch").get_data(as_text=True)
    assert WRONG_ROUND in html
    assert "Plex showed Round 8" in html
    assert "Round 7 Italy" in html


def test_mismatch_log_page_is_empty_but_sensible_with_no_entries(webui):
    client, _, _ = webui
    html = client.get("/mismatch").get_data(as_text=True)
    assert "Nothing has been marked as a mismatch" in html


# ---------------------------------------------------------------------------
# The dialog itself, rendered from the real page script
# ---------------------------------------------------------------------------

MISMATCH_PAYLOAD = {
    "season": 2026,
    "round": 7,
    "venue": "Italy",
    "session": "MOTOGP_RACE",
    "session_name": "MotoGP Race",
    "prefix": "26R07E82",
    "can_mark": True,
    "grabbed": {
        "release": WRONG_ROUND,
        "nzo_id": "nzo_1",
        "timestamp": "2026-05-31T20:00:00",
    },
    "analysis": {
        "accepted": True,
        "reason": "passed every filter",
        "score": 175,
        "refused": False,
        "classification": None,
        "checks": [
            {"name": "global_exclusion", "passed": True, "detail": "none"},
            {"name": "class", "passed": True, "detail": "MotoGP"},
            {"name": "round", "passed": True, "detail": "venue/alias 'italy' in title"},
            {"name": "session", "passed": True, "detail": "MotoGP Race"},
        ],
        "parsed": {"round": 8, "session": "MOTOGP_RACE", "session_name": "MotoGP Race"},
    },
    "blocked": [{"id": "abc123", "title": "MotoGP.2026.Round08.Italy.Race.1080p.WEB.h264-OLD"}],
    "library": {"found": False, "path": None, "quarantine_dir": "/media/MotoGP/.mismatched"},
    "requirements": {"quality": "1080p", "groups": ["MWR"]},
}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_mismatch_dialog_renders_the_release_and_the_reasoning(webui, tmp_path):
    """
    The dialog has to show the release name *and* why the matcher took it.

    Run against the real page script rather than a copy, so a rename in the
    template breaks this test instead of silently producing an empty dialog on
    a user's phone - which is where a mismatch is usually noticed.
    """
    client, _, _ = webui
    html = client.get("/library").get_data(as_text=True)
    page_js = re.findall(r"<script>(.*?)</script>", html, flags=re.DOTALL)[-1]

    harness = r"""
const els = {};
function el(id) {
    if (!els[id]) {
        els[id] = {
            id, innerHTML: '', textContent: '', value: '', disabled: false,
            _classes: new Set(),
            classList: {
                add(c) { els[id]._classes.add(c); },
                remove(c) { els[id]._classes.delete(c); },
                contains(c) { return els[id]._classes.has(c); },
            },
        };
    }
    return els[id];
}
['library-season', 'library-round', 'library-content', 'mismatch-body',
 'mismatch-modal', 'mismatch-save-btn', 'mismatch-copy-btn',
 'warning-modal', 'failed-sessions-list', 'sticky-download-bar',
 'selected-count', 'toast-container'].forEach(el);
el('library-season').value = '2026';

global.document = {
    getElementById: el,
    querySelectorAll: () => [],
    addEventListener: () => {},
    createElement: () => ({ style: {}, classList: { add() {}, remove() {} },
                            appendChild() {}, remove() {}, select() {},
                            setAttribute() {} }),
    body: { appendChild() {}, removeChild() {} },
};
global.window = { addEventListener: () => {}, location: { href: '' } };
global.navigator = {};
global.confirm = () => false;

const payload = JSON.parse(process.env.MISMATCH_PAYLOAD);
global.fetch = (url) => Promise.resolve({
    json: () => Promise.resolve(String(url).startsWith('/api/release-mismatch?')
        ? payload : { entries: [] }),
});

""" + page_js + r"""

openMismatchModal(7, 'MOTOGP_RACE', 'MotoGP Race');
setImmediate(() => setImmediate(() => {
    const body = els['mismatch-body'].innerHTML;
    console.log(JSON.stringify({
        active: els['mismatch-modal'].classList.contains('active'),
        saveDisabled: els['mismatch-save-btn'].disabled,
        body: body,
    }));
}));
"""

    harness_file = tmp_path / "mismatch_harness.js"
    harness_file.write_text(harness, encoding="utf-8")

    env = os.environ.copy()
    env["MISMATCH_PAYLOAD"] = json.dumps(MISMATCH_PAYLOAD)
    proc = subprocess.run(
        ["node", str(harness_file)], env=env,
        capture_output=True, text=True, check=True,
    )
    out = json.loads(proc.stdout.strip())
    body = out["body"]

    assert out["active"], "the dialog never opened"
    assert out["saveDisabled"] is False
    assert WRONG_ROUND in body, "the release name is the thing the user needs"
    assert "Round 7 Italy" in body
    assert "26R07E82" in body
    assert body.count("mismatch-check ") >= 4, "the evidence checklist did not render"
    assert "venue/alias" in body, "the matched rule must be named"
    assert "would still be picked" in body.lower(), (
        "the user must be told the pattern is still live, or they will assume "
        "the block alone fixed it"
    )
    assert "unblockMismatch" in body, "a wrong verdict must be reversible"
    assert "mismatch-quarantine" in body, "the folder move must be opt-out-able"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_download_tab_marks_blocked_sessions_and_links_the_log(webui, tmp_path):
    """
    A blocked session must be visible on the session row, not only in the log.

    The count is per (season, round, session) because that is the granularity
    the block has; putting a badge on every session of the round would imply a
    scope the entry does not have.
    """
    client, _, _ = webui
    html = client.get("/library").get_data(as_text=True)
    page_js = re.findall(r"<script>(.*?)</script>", html, flags=re.DOTALL)[-1]

    harness = r"""
const els = {};
function el(id) {
    if (!els[id]) els[id] = { id, innerHTML: '', textContent: '', value: '', disabled: false,
        classList: { add() {}, remove() {}, contains() { return false; } } };
    return els[id];
}
['library-content', 'library-season', 'library-round'].forEach(el);
el('library-season').value = '2026';
global.document = { getElementById: el, querySelectorAll: () => [],
                    addEventListener: () => {},
                    createElement: () => ({ style: {}, classList: { add() {}, remove() {} },
                                            appendChild() {}, remove() {} }),
                    body: { appendChild() {}, removeChild() {} } };
global.window = { addEventListener: () => {}, location: { href: '' } };
global.fetch = (url) => Promise.resolve({ json: () => Promise.resolve(
    String(url).startsWith('/api/release-mismatches')
        ? { entries: [
            { season: 2026, round: 7, session: 'MOTOGP_RACE', scope: 'session' },
            { season: 2026, round: 7, session: 'MOTOGP_RACE', scope: 'session' },
          ] }
        : {}) });

""" + page_js + r"""

loadMismatchCounts().then(() => {
    renderLibraryStatus({
        total_desired: 2, total_grabbed: 2, total_in_library: 2,
        past_rounds_grabbed: 2, past_rounds_desired: 2,
        rounds: [{ number: 7, venue: 'Italy', date: '2026-05-31',
                   grabbed_count: 2, desired_count: 2,
                   sessions: [
                       { type: 'MOTOGP_RACE', name: 'MotoGP Race', exists: true },
                       { type: 'MOTOGP_Q1', name: 'MotoGP Qualifying 1', exists: false },
                   ] }],
    }, '2026');
    console.log(JSON.stringify({ body: els['library-content'].innerHTML }));
});
"""

    harness_file = tmp_path / "library_harness.js"
    harness_file.write_text(harness, encoding="utf-8")

    proc = subprocess.run(
        ["node", str(harness_file)], capture_output=True, text=True, check=True,
    )
    body = json.loads(proc.stdout.strip())["body"]

    assert "2 releases marked as a mismatch" in body
    assert '<a href="/mismatch">View log</a>' in body
    assert body.count("mismatch-badge") == 1, (
        "only the session with entries may be badged - the missing Q1 session "
        "has no mismatch and must not look blocked"
    )
    assert "🚫 2" in body
    assert body.count("mismatch-btn") == 2, (
        "both an existing and a missing session offer the check, because the "
        "grabber can hold a release record either way"
    )
    assert "openMismatchModal(7, 'MOTOGP_RACE')" in body


def test_a_failed_folder_move_still_records_the_block(webui, caplog):
    """
    The block is the part that changes future behaviour, so a read-only library
    must not lose it. But the user has to be told the old file is still in Plex:
    a replacement sorting into it is the stale-copy case, and silence there
    looks like the feature worked.
    """
    client, tmp_path, library = webui
    _write_grabber_state(tmp_path, WRONG_ROUND)

    prefix = common.build_prefix(2026, 7, SessionType.MOTOGP_RACE)
    folder = library / "2026" / "Round 07 Italy" / f"{prefix} MotoGP Race"
    folder.mkdir(parents=True)
    (folder / "wrong.mkv").write_bytes(b"x" * 16)

    if os.geteuid() == 0:  # pragma: no cover - root ignores the mode bits
        pytest.skip("permissions cannot be denied to root")

    os.chmod(library, 0o555)
    try:
        with caplog.at_level("ERROR", logger="motogp.webui"):
            response = client.post("/api/release-mismatch", json={
                "season": 2026, "round": 7, "session": "MOTOGP_RACE",
                "quarantine": True,
            })
    finally:
        os.chmod(library, 0o755)

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["status"] == "recorded"
    assert payload["library_action"]["ok"] is False
    assert payload["library_action"]["reason"]
    assert folder.exists(), "nothing may be deleted when the move fails"
    assert len(mismatches.load_entries()) == 1
    assert "still in Plex" in caplog.text
