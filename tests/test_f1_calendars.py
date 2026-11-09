"""
The F1 calendars, and the schema generalisation that let them exist.

Two things are being pinned here, and they fail for different reasons:

  * **the port is faithful** - every value in ``config/f1_<year>.json`` matches
    what f1arr declared, and every reference it carries resolves. A bad port
    fails here.
  * **the schema is one schema** - MotoGP's and F1's calendars are validated by
    the same code, reached through the series registry, with no championship
    named in the validator. A regression to a MotoGP-shaped validator fails
    here, on the F1 files, rather than in a container months later.

The interesting assertions are the cross-checks, because a calendar that is
*wrong* is rarely malformed: it is a round whose declared sessions disagree with
its timetable, or an element of the artwork that does not exist.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import motogp_common as common
from series import get_series, runtime
from utils.validate_calendar import (
    check_round,
    check_round_format,
    discover_calendars,
    validate_one,
)

CONFIG = REPO_ROOT / "config"
F1 = get_series("f1")


def _load(season):
    path = CONFIG / f"f1_{season}.json"
    assert path.is_file(), f"missing {path.name}"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def f1_2026():
    return _load(2026)


@pytest.fixture(scope="module")
def f1_2025():
    return _load(2025)


@pytest.fixture
def round_2026(f1_2026):
    return copy.deepcopy(f1_2026["rounds"][0])


# ---------------------------------------------------------------------------
# The port is faithful
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("season", (2025, 2026))
def test_the_calendar_is_a_24_round_season_in_date_order(season):
    calendar = _load(season)
    assert calendar["season"] == season
    assert calendar["classes"] == ["f1"]
    rounds = calendar["rounds"]
    assert len(rounds) == 24
    assert [r["round"] for r in rounds] == list(range(1, 25))
    dates = [r["date"] for r in rounds]
    assert dates == sorted(dates)


@pytest.mark.parametrize("season", (2025, 2026))
def test_every_round_declares_the_format_its_timetable_actually_runs(season):
    calendar = _load(season)
    for entry in calendar["rounds"]:
        fmt = entry["weekend_format"]
        assert fmt in calendar["weekend_formats"], entry["venue"]
        declared = set(entry["f1_sessions"])
        from_format = {
            item["session"]
            for items in calendar["weekend_formats"][fmt].values()
            for item in items
        }
        assert declared == from_format, entry["venue"]


@pytest.mark.parametrize("season,expected", ((2025, [2, 6, 13, 19, 21, 23]),
                                             (2026, [2, 6, 7, 11, 14, 18])))
def test_the_sprint_rounds_are_the_ones_f1arr_declared(season, expected):
    """
    The numbers come from f1arr's calendar, not from its ``f1_sprints.json``.

    That sidecar claimed 2026 was ``[2, 6, 11, 13, 16, 19]`` while the calendar
    said ``[2, 6, 7, 11, 14, 18]`` - three rounds wrong in each direction. The
    calendar is what the code reads, so it is what was ported, and the sidecar
    was deliberately not brought across. This test is the record of that.
    """
    calendar = _load(season)
    sprints = [r["round"] for r in calendar["rounds"]
               if r["weekend_format"] == "f1_sprint"]
    assert sprints == expected


@pytest.mark.parametrize("season", (2025, 2026))
def test_every_round_names_a_folder_the_shared_sorter_can_build(season):
    """
    ``Round <nn> <venue>``, not f1arr's ``Round 1 Australia``.

    The sorter builds folder names from the round and venue, so a calendar
    carrying a different shape would be a second naming scheme - and two naming
    schemes is how Plex ends up with the same race twice.
    """
    for entry in _load(season)["rounds"]:
        assert entry["folder"] == f"Round {entry['round']:02d} {entry['venue']}" \
            or entry["folder"] == f"Round {entry['round']} {entry['venue']}"


@pytest.mark.parametrize("season", (2025, 2026))
def test_the_round_1987_rules_still_apply_to_a_ported_calendar(season):
    """The generic validator has no problems with either F1 season."""
    path = CONFIG / f"f1_{season}.json"
    assert validate_one(F1, season, path) == []


# ---------------------------------------------------------------------------
# The schema generalisation
# ---------------------------------------------------------------------------

def test_the_shared_spine_reads_the_series_prefixed_session_list(
        round_2026, f1_2026):
    """
    ``<series>_sessions``, resolved by series key - and the MotoGP default is
    unchanged, which is what lets the MotoGP calendars on disk keep working.
    """
    assert common.round_session_list(round_2026, f1_2026, series="f1") == \
        round_2026["f1_sessions"]

    # A round carrying only the other series' key must not be read as this one.
    other = dict(round_2026)
    other["motogp_sessions"] = ["race"]
    other.pop("f1_sessions")
    assert common.round_session_list(other, f1_2026, series="f1") == \
        [s["session"] for items in f1_2026["weekend_formats"][
            round_2026["weekend_format"]].values() for s in items]


def test_round_runs_session_gates_an_f1_round_by_its_own_format(f1_2026):
    """
    A standard weekend does not run a sprint, and a sprint weekend has no FP2.

    This is f1arr's ``get_excluded_sessions_for_round()`` expressed as data:
    the rule is now "is this session in the round's declared list", which is the
    same question MotoGP's 2027 cutover needed, so there is one implementation.
    """
    rnd = runtime("f1")
    SessionType = rnd.session_type

    standard = next(r for r in f1_2026["rounds"]
                    if r["weekend_format"] == "f1_standard")
    sprint = next(r for r in f1_2026["rounds"]
                  if r["weekend_format"] == "f1_sprint")

    assert common.round_runs_session(standard, SessionType.PRACTICE_2,
                                     f1_2026, series="f1")
    assert not common.round_runs_session(standard, SessionType.SPRINT,
                                         f1_2026, series="f1")
    assert common.round_runs_session(sprint, SessionType.SPRINT_QUALIFYING,
                                     f1_2026, series="f1")
    assert not common.round_runs_session(sprint, SessionType.PRACTICE_2,
                                         f1_2026, series="f1")


def test_a_support_show_is_not_gated_by_the_weekend_format(f1_2026):
    """
    ``f1_sessions`` describes the seven timed sessions only.

    Ted's Notebook airs whatever the weekend shape is, so it must not be
    filtered out by a list that never mentions it - the same rule that keeps
    Moto3's Practice alive through a 2027 MotoGP format.
    """
    rnd = runtime("f1")
    entry = f1_2026["rounds"][0]
    assert common.round_runs_session(entry, rnd.session_type.TEDS_NOTEBOOK,
                                     f1_2026, series="f1")


def test_the_validator_needs_no_championship_name(round_2026, f1_2026):
    """The five MotoGP hardpoints are gone; the same call validates an F1 round."""
    assert check_round(round_2026, 2026, True, f1_2026, F1) == []


def test_a_typo_in_an_f1_format_name_is_caught(round_2026, f1_2026):
    entry = copy.deepcopy(round_2026)
    entry["weekend_format"] = "f1_2027"
    problems = check_round(entry, 2026, True, f1_2026, F1)
    assert any("f1_2027" in p and "weekend_formats" in p for p in problems)


def test_an_f1_round_with_the_other_format_session_list_is_caught(
        round_2026, f1_2026):
    """
    The most likely hand edit: copy a sprint round and forget the flag.

    The round then declares a sprint it does not run, and the grabber hunts a
    session that never happens - silently, all weekend.
    """
    entry = copy.deepcopy(round_2026)          # round 1 is a standard weekend
    entry["f1_sessions"] = ["fp1", "sprint_qualifying", "sprint", "qualifying",
                            "race"]
    problems = check_round(entry, 2026, True, f1_2026, F1)
    assert any("f1_standard" in p and "'sprint'" in p for p in problems)
    assert any("'fp2'" in p and "does not list it" in p for p in problems)


def test_a_missing_f1_timetable_session_is_caught(round_2026, f1_2026):
    entry = copy.deepcopy(round_2026)
    entry["sessions"]["saturday"].pop("f1_fp3")
    problems = check_round(entry, 2026, True, f1_2026, F1)
    assert any("f1_fp3" in p for p in problems)


def test_a_bare_f1_key_is_rejected(round_2026, f1_2026):
    """
    The keys are class-prefixed. An unprefixed key is unresolvable, and an
    unresolvable key means a session the grabber can never target.
    """
    entry = copy.deepcopy(round_2026)
    entry["sessions"]["saturday"]["fp3"] = \
        entry["sessions"]["saturday"].pop("f1_fp3")
    problems = check_round(entry, 2026, True, f1_2026, F1)
    assert any("unknown session key 'fp3'" in p for p in problems)


def test_an_f1_release_alias_must_name_a_real_f1_session(f1_2026):
    calendar = copy.deepcopy(f1_2026)
    calendar["release_aliases"] = {"sq": "not_a_session"}
    problems = check_round(calendar["rounds"][0], 2026, True, calendar, F1)
    assert any("release_aliases" in p and "not_a_session" in p for p in problems)


def test_the_release_aliases_are_portable_between_the_two_seasons():
    """
    ``sq``/``spq`` mean Sprint Qualifying in both. f1arr kept them in a season
    file rather than the code, and so do we: a token that is ambiguous in one
    season is a per-season fact, even when today it is not.
    """
    for season in (2025, 2026):
        aliases = _load(season)["release_aliases"]
        assert aliases["sq"] == "sprint_qualifying"
        assert aliases["spq"] == "sprint_qualifying"


def test_a_season_without_a_timetable_is_not_reported_as_five_missing_sessions(
        f1_2025):
    """
    Backfill-by-date seasons carry no times, which is a different statement from
    a timetable missing a session. Reporting every format session as missing for
    every round would bury a genuine problem in 120 lines of noise.
    """
    for entry in f1_2025["rounds"]:
        assert "sessions" not in entry
        problems = check_round_format(entry, f1_2025, F1)
        assert not any("sessions block has no" in p for p in problems), \
            entry["venue"]


# ---------------------------------------------------------------------------
# Discovery is per series, and the two never collide
# ---------------------------------------------------------------------------

def test_discovery_finds_every_series_calendar():
    found = discover_calendars(CONFIG)
    prefixes = {spec.calendar_prefix for spec, _season, _path in found}
    seasons = {(spec.key, season) for spec, season, _path in found}
    assert prefixes >= {"motogp", "f1"}
    assert ("motogp", 2026) in seasons
    assert ("f1", 2026) in seasons
    assert ("f1", 2025) in seasons


def test_discovery_refuses_overlapping_prefixes(tmp_path, monkeypatch):
    """
    The prefixes are what make a refresh of one series unable to touch another.

    If one prefix were a prefix of another ("f" and "f1"), a single glob would
    match both series' files and the safety property would be gone - so it is
    refused rather than discovered and double-counted.

    The fake declaration is injected rather than registered: a test that
    mutates the global registry would leak into every later test in the
    process, and the registry deliberately has no unregister.
    """
    from series.base import ClassSpec, Series, build
    from utils import validate_calendar as validator

    fake = Series(
        key="f", display_name="F", calendar_prefix="f", log_name="f",
        state_prefix="f", search_prefix="F",
        classes=(ClassSpec("f", "F", 1),),
        session_members=(("RACE", 1),),
        session_elements={"RACE": 1},
        legacy_session_elements={},
    )
    build(fake)                    # the declaration itself must be sound
    monkeypatch.setattr(validator, "all_series", lambda: [F1, fake])

    with pytest.raises(ValueError, match="overlap"):
        discover_calendars(tmp_path)


def test_discovery_does_not_confuse_motogp_with_a_file_that_only_starts_like_it(
        tmp_path):
    """
    ``motogp_2026.json`` is MotoGP's and ``f1_2026.json`` is F1's, but a stray
    file must not be attributed to whichever prefix happens to match first.
    """
    (tmp_path / "f1_2026.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.json").write_text("{}", encoding="utf-8")
    (tmp_path / "f1_preview.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="does not name a season"):
        discover_calendars(tmp_path, ["f1"])

    (tmp_path / "f1_preview.json").unlink()
    found = discover_calendars(tmp_path, ["f1"])
    assert [(spec.key, season) for spec, season, _path in found] == [("f1", 2026)]
