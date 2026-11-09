"""
Tests for the grab statistics (grab_stats.py).

Three things this module got wrong, all silently, and all of them wrong *after*
a second series existed:

1. **The class was guessed from the release title**, with MotoGP as the
   fall-through. A Formula 1 release matches no MotoGP token, so every F1 grab
   was counted as MotoGP - on a page that rendered perfectly.
2. **The classes were hardcoded** to MotoGP/Moto2/Moto3, so a one-class series
   showed three zeroes beside its own real numbers.
3. **The success rate was computed from `len(failed)`**, which the grabber
   *deletes* when a retry succeeds. A weekend where every session failed once
   and then succeeded reported a 100% success rate.

The theme is the one AGENTS.md keeps coming back to: a number that is wrong is
worse than a number that is missing, because nothing looks broken.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import grab_stats
from series import runtime

REPO_ROOT = Path(__file__).resolve().parent.parent


def _iso(days_ago: int = 0) -> str:
    return (datetime.now() - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point grab_stats at a temporary CONFIG_DIR and clear its cached path."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(grab_stats, "_STATE_FILE", None)
    return tmp_path


def write_state(tmp_path, series, *, grabbed=None, failed=None, failures=None,
                last_run=None):
    path = tmp_path / f"grabber_state.{series}.json"
    document = {
        "searched": {},
        "grabbed": grabbed or {},
        "failed": failed or {},
        "last_run": last_run,
    }
    if failures is not None:
        document["failures"] = failures
    path.write_text(json.dumps(document))
    return path


#: A MotoGP element number for the feature class' race. Element numbers are
#: allocated per class within a block (80/82/84 are the three classes' races),
#: which is why the number alone identifies both the session and its class.
MOTOGP_RACE_ELEMENT = None
for _session, _number in runtime("motogp").session_elements.items():
    if _session.name == "MOTOGP_RACE":
        MOTOGP_RACE_ELEMENT = _number

F1_RACE_ELEMENT = None
for _session, _number in runtime("f1").session_elements.items():
    if _session.name == "RACE":
        F1_RACE_ELEMENT = _number


def test_the_element_numbers_the_tests_use_are_real():
    """Guard the fixtures: the two constants above must resolve."""
    assert MOTOGP_RACE_ELEMENT is not None
    assert F1_RACE_ELEMENT is not None
    assert runtime("motogp").get_session_from_element(
        MOTOGP_RACE_ELEMENT, season=2026).name == "MOTOGP_RACE"
    assert runtime("f1").get_session_from_element(
        F1_RACE_ELEMENT, season=2026).name == "RACE"


# ===========================================================================
# Class attribution
# ===========================================================================

def test_a_motogp_race_is_counted_as_motogp(state_dir):
    write_state(state_dir, "motogp", grabbed={
        f"2026_R07_E{MOTOGP_RACE_ELEMENT:02d}": {
            "timestamp": _iso(1), "release": "MotoGP.2026.R07.France.Race"},
    })

    summary = grab_stats.get_stats_summary()

    assert summary["by_class"]["MotoGP"] == 1
    assert summary["by_class"]["Moto2"] == 0


def test_a_one_class_series_reports_only_its_own_class(state_dir):
    """
    The hardcoded chart reported an F1 grab as "MotoGP: 0, Moto2: 0, Moto3: 0"
    and counted it in none of them - the numbers were on screen and all wrong.
    """
    write_state(state_dir, "f1", grabbed={
        f"2026_R07_E{F1_RACE_ELEMENT:02d}": {
            "timestamp": _iso(1), "release": "Formula1.2026.R07.Silverstone.Race"},
    })

    summary = grab_stats.get_stats_summary("f1")

    assert list(summary["by_class"]) == ["F1"], (
        "the class list must come from the declaration, not a hardcoded chart"
    )
    assert summary["by_class"]["F1"] == 1


def test_a_release_title_from_another_series_is_never_counted_as_motogp(state_dir):
    """
    The original bug, in its own terms: ``get_class_from_release()`` fell through
    to "MotoGP" for anything it did not recognise, which is every F1 release.
    """
    write_state(state_dir, "f1", grabbed={
        f"2026_R07_E{F1_RACE_ELEMENT:02d}": {
            "timestamp": _iso(1), "release": "Formula1.2026.R07.Silverstone.Race"},
    })

    summary = grab_stats.get_stats_summary("motogp")

    assert summary["by_class"]["MotoGP"] == 0
    assert summary["total_grabbed"] == 0


def test_an_unresolvable_entry_counts_toward_no_class(state_dir):
    """
    An episode number from a previous numbering scheme resolves to nothing. It
    must not be attributed to the feature class - that is exactly how a foreign
    series' grabs became MotoGP's.
    """
    write_state(state_dir, "motogp", grabbed={
        "2026_R07_E99": {"timestamp": _iso(1), "release": "Something Unrelated"},
    })

    summary = grab_stats.get_stats_summary()

    assert sum(summary["by_class"].values()) == 0
    assert summary["total_grabbed"] == 1, "it still counts as a grab"


def test_the_session_is_named_from_the_registry(state_dir):
    write_state(state_dir, "motogp", grabbed={
        f"2026_R07_E{MOTOGP_RACE_ELEMENT:02d}": {
            "timestamp": _iso(1), "release": "MotoGP.2026.R07.France.Race"},
    })

    summary = grab_stats.get_stats_summary()

    assert "Race" in summary["by_session"]


def test_the_legacy_title_only_helpers_still_answer_but_never_guess(state_dir):
    """
    Retained for callers that only have a title. The important assertions are
    the last two: an unrecognised title is "Unknown", not the feature class.

    The first assertion is a limitation, not a feature: with only a title to go
    on, "MotoGP" is a substring of "MOTO3"/"MOTO2" in any normalisation that
    strips separators, so the *series*' own class tokens cannot be told apart
    reliably. That is exactly why the summary does not use this path - it reads
    the class from the session the element number resolves to.
    """
    assert grab_stats.get_class_from_release(
        "Formula1.2026.R07.Silverstone.Race", series="f1") == "F1"
    assert grab_stats.get_class_from_release("Some.Other.Sport.2026") == "Unknown"


def test_a_title_from_another_series_is_refused_rather_than_matched(state_dir):
    """
    The bug this guard exists for: "Formula1.2026.Silverstone.Race" contains the
    MotoGP session name "Race", so classifying it against MotoGP's vocabulary
    reported a Moto3 race. A title must name the series before any of that
    series' vocabulary is applied to it.
    """
    assert grab_stats.get_class_from_release(
        "Formula1.2026.R07.Silverstone.Race", series="motogp") == "Unknown"
    assert grab_stats.get_class_from_release(
        "MotoGP.2026.R07.France.Race", series="f1") == "Unknown"


# ===========================================================================
# The success rate
# ===========================================================================

def test_a_successful_retry_does_not_erase_the_failure_it_recovered_from(state_dir):
    """
    The bug in one test: the grabber deletes a failure when the retry succeeds,
    so a rate built from `len(failed)` reported 100%.
    """
    write_state(
        state_dir, "motogp",
        grabbed={f"2026_R07_E{MOTOGP_RACE_ELEMENT:02d}": {
            "timestamp": _iso(1), "release": "MotoGP.2026.R07.France.Race"}},
        # The retry succeeded, so `failed` is empty again...
        failed={},
        # ...but the attempt is remembered.
        failures={"2026_R07_E80@2026-11-01T10:00:00": {"reason": "no results"}},
    )

    summary = grab_stats.get_stats_summary()

    assert summary["total_failed"] == 0, "nothing is outstanding"
    assert summary["recorded_failures"] == 1
    assert summary["success_rate"] == 50, "one grab, one failed attempt"
    assert summary["failure_metric"] == "recorded"


def test_an_install_without_the_failure_history_falls_back_to_outstanding(state_dir):
    """
    A state file written before the history existed has no "failures" key. It
    must report what it did before - not zero failures, and not an invented
    history.
    """
    write_state(
        state_dir, "motogp",
        grabbed={f"2026_R07_E{MOTOGP_RACE_ELEMENT:02d}": {
            "timestamp": _iso(1), "release": "MotoGP.2026.R07.France.Race"}},
        failed={"2026_R07_E70": {"timestamp": _iso(2), "reason": "no results"}},
    )

    summary = grab_stats.get_stats_summary()

    assert summary["failure_metric"] == "outstanding"
    assert summary["success_rate"] == 50


def test_the_denominator_is_never_below_the_outstanding_count(state_dir):
    """
    A history that was truncated or edited must not make the rate look better
    than the failures the state file still lists.
    """
    write_state(
        state_dir, "motogp",
        grabbed={f"2026_R07_E{MOTOGP_RACE_ELEMENT:02d}": {
            "timestamp": _iso(1), "release": "MotoGP.2026.R07.France.Race"}},
        failed={"a": {"reason": "x"}, "b": {"reason": "y"}},
        failures={"only-one": {"reason": "x"}},
    )

    summary = grab_stats.get_stats_summary()

    assert summary["effective_failed"] == 2
    assert summary["success_rate"] == 33


def test_an_empty_state_file_is_not_an_error(state_dir):
    summary = grab_stats.get_stats_summary()

    assert summary["total_grabbed"] == 0
    assert summary["success_rate"] == 100
    assert len(summary["by_day"]) == 14
    assert len(summary["by_month"]) == 6


# ===========================================================================
# Chart maxima, per series
# ===========================================================================

def test_maxima_are_computed_so_the_template_does_not_have_to(state_dir):
    write_state(state_dir, "motogp", grabbed={
        f"2026_R07_E{MOTOGP_RACE_ELEMENT:02d}": {
            "timestamp": _iso(0), "release": "MotoGP.2026.R07.France.Race"},
        f"2026_R07_E{MOTOGP_RACE_ELEMENT + 2:02d}": {
            "timestamp": _iso(0), "release": "MotoGP.2026.R07.France.Race"},
    })

    summary = grab_stats.get_stats_summary()

    assert summary["max_daily"] == 2
    assert summary["max_monthly"] == 2


def test_the_day_series_includes_empty_days(state_dir):
    """
    A bar chart built from only the days that have grabs silently drops the gaps,
    so "nothing for a week" looks like "nothing to show".
    """
    summary = grab_stats.get_stats_summary()

    labels = [row["date"] for row in summary["by_day"]]
    assert len(labels) == 14
    assert labels == sorted(labels)


# ===========================================================================
# More than one series
# ===========================================================================

def test_by_series_reports_each_series_separately(state_dir):
    write_state(state_dir, "motogp", grabbed={
        f"2026_R07_E{MOTOGP_RACE_ELEMENT:02d}": {
            "timestamp": _iso(1), "release": "MotoGP.2026.R07.France.Race"},
    })
    write_state(state_dir, "f1", grabbed={
        f"2026_R07_E{F1_RACE_ELEMENT:02d}": {
            "timestamp": _iso(1), "release": "Formula1.2026.R07.Silverstone.Race"},
        f"2026_R07_E{F1_RACE_ELEMENT + 1:02d}": {
            "timestamp": _iso(1), "release": "Formula1.2026.R07.Race.Uncut"},
    })

    summary = grab_stats.get_stats_summary()

    assert summary["total_grabbed"] == 1, "the default series' own numbers"
    assert summary["by_series"]["f1"]["total_grabbed"] == 2
    assert set(summary["active_series"]) == {"motogp", "f1"}


def test_a_series_with_no_state_file_is_not_reported(state_dir):
    """
    An F1 panel on an install that has never grabbed an F1 release is noise, not
    information.
    """
    write_state(state_dir, "motogp", grabbed={})

    summary = grab_stats.get_stats_summary()

    assert summary["active_series"] == ["motogp"]


def test_each_series_reports_its_own_classes(state_dir):
    write_state(state_dir, "motogp", grabbed={})
    write_state(state_dir, "f1", grabbed={
        f"2026_R07_E{F1_RACE_ELEMENT:02d}": {
            "timestamp": _iso(1), "release": "Formula1.2026.R07.Silverstone.Race"},
    })

    summary = grab_stats.get_stats_summary()

    assert list(summary["by_series"]["motogp"]["by_class"]) == ["Moto3", "Moto2", "MotoGP"]
    assert list(summary["by_series"]["f1"]["by_class"]) == ["F1"]


# ===========================================================================
# The grabber records what the stats read
# ===========================================================================

def test_the_grabber_records_a_failed_attempt_that_a_retry_cannot_erase(tmp_path,
                                                                      monkeypatch):
    """
    The two modules have to agree on the shape, and the property that matters is
    that a failure survives the retry that fixes it.
    """
    import motogp_grabber

    monkeypatch.setattr(motogp_grabber, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(motogp_grabber, "STATE_FILE",
                        tmp_path / "grabber_state.motogp.json")

    state = motogp_grabber.GrabberState()
    state.mark_failed("2026_R07_E80", "no results")
    outstanding_after_failure = len(state.state["failed"])
    recorded_after_failure = len(state.state["failures"])

    # The retry succeeds.
    state.mark_grabbed("2026_R07_E80", "MotoGP.2026.R07.France.Race")
    state.clear_session("2026_R07_E80")

    assert outstanding_after_failure == 1
    assert recorded_after_failure == 1
    assert state.state["failed"] == {}, "outstanding work is cleared"
    assert len(state.state["failures"]) == 1, (
        "the attempt is history, not outstanding work, and must survive"
    )


def test_a_second_failure_for_the_same_session_is_a_second_record(tmp_path,
                                                                  monkeypatch):
    import motogp_grabber

    monkeypatch.setattr(motogp_grabber, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(motogp_grabber, "STATE_FILE",
                        tmp_path / "grabber_state.motogp.json")

    state = motogp_grabber.GrabberState()
    state.mark_failed("2026_R07_E80", "no results")
    state.state["failed"].clear()          # as if a retry had cleared it
    state.mark_failed("2026_R07_E80", "no results again")

    assert len(state.state["failures"]) == 2
    assert len(state.state["failed"]) == 1


# ===========================================================================
# The CLI
# ===========================================================================

def test_the_cli_prints_a_series_summary(state_dir, capsys, monkeypatch):
    write_state(state_dir, "motogp", grabbed={})
    monkeypatch.setattr(sys, "argv", ["grab_stats.py"])

    assert grab_stats.main() == 0
    out = capsys.readouterr().out
    assert "MotoGP" in out
    assert "Success rate" in out


def test_the_cli_rejects_a_missing_series_value(state_dir, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["grab_stats.py", "--series"])
    assert grab_stats.main() == 2
    assert "--series needs a value" in capsys.readouterr().out
