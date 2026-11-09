"""
Every series, held to the same standard (hybrid step 9).

Slices 1-8 were each tested against the series they were written for. The risk
this file exists for is the one those tests structurally cannot catch: a suite
built from one example passes a second example that is wrong in the same places
the first one was never asked about.

So this is not "more F1 tests". It is the shared spine's contract, asserted for
every registered series by iterating ``all_series()`` - which means a third
series added tomorrow is covered by these checks the moment it registers, and a
*new* series cannot be added by copying MotoGP's shape without the differences
being real declarations.

Three of these would have caught actual defects if they had existed earlier:

* ``test_prefix_round_trips_for_every_session`` - the numbering is the thing
  written to disk, and the two series' tables are different namespaces sharing
  one format;
* ``test_a_round_never_runs_a_session_its_format_cannot_hold`` - a programme
  that only exists on a sprint weekend was searched on every weekend;
* ``test_every_season_calendar_validates`` - F1's calendars are the only ones
  whose session keys are not MotoGP's.
"""

import json
from pathlib import Path

import pytest

import motogp_common as common
from series import all_series, runtime

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Every series, by key, for parametrisation. ``all_series()`` rather than a
#: literal list: a hardcoded list is a test that silently stops covering a new
#: series, and "the tests pass" is then true and meaningless.
SERIES_KEYS = [spec.key for spec in all_series()]

#: Seasons the repository actually ships calendars for.
SEASONS = (2025, 2026)


def spec_for(key):
    from series import get_series

    return get_series(key)


def calendar_for(key, season):
    path = REPO_ROOT / "config" / f"{spec_for(key).calendar_prefix}_{season}.json"
    if not path.exists():
        pytest.skip(f"no {key} calendar for {season}")
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# The vocabulary is complete
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", SERIES_KEYS)
def test_every_session_has_a_label_a_class_and_a_number(key):
    """
    A member with no display name renders as ``MOTOGP_SPRINT_QUALIFYING`` in the
    UI, and one with no class cannot be placed in a folder or given artwork.
    """
    rt = runtime(key)
    members = [m for m in rt.session_type if m is not rt.session_type.UNKNOWN]

    assert members, f"{key} declares no sessions"
    for member in members:
        assert member.name in {m.name for m in rt.session_display_names}, member.name
        assert member.race_class is not None, member.name
        assert rt.element_number(member, 2026) > 0, member.name


@pytest.mark.parametrize("key", SERIES_KEYS)
def test_element_numbers_are_unique_and_fit_the_two_digit_prefix(key):
    rt = runtime(key)
    numbers = {
        member: rt.element_number(member, 2026)
        for member in rt.session_type
        if member is not rt.session_type.UNKNOWN
    }

    assert len(set(numbers.values())) == len(numbers), (
        f"{key} allocates one number to two sessions: "
        f"{sorted(numbers.items(), key=lambda kv: kv[1])}"
    )
    for member, number in numbers.items():
        assert 1 <= number <= 99, f"{member.name} -> {number} does not fit E%02d"
        assert rt.build_prefix(2026, 1, member).endswith(f"E{number:02d}")


@pytest.mark.parametrize("key", SERIES_KEYS)
def test_prefix_round_trips_for_every_session(key):
    """
    The prefix is what is written to disk, so a number that does not parse back
    to the session that wrote it is a folder the app cannot read - which looks
    like "the session never downloads".
    """
    rt = runtime(key)

    for member in rt.session_type:
        if member is rt.session_type.UNKNOWN:
            continue
        for season in SEASONS:
            if rt.is_ambiguous(member, season) if hasattr(rt, "is_ambiguous") else False:
                continue
            prefix = rt.build_prefix(season, 7, member)
            assert rt.parse_prefix(prefix) == (season, 7, member), prefix


@pytest.mark.parametrize("key", SERIES_KEYS)
def test_a_prefix_from_one_series_is_never_read_as_the_same_session_by_another(key):
    """
    The two numbering schemes share a format and not a meaning.

    Reading one series' folder name with the other's table must fail or resolve
    to a *different* session - it must never resolve to the same one, because
    that is the case where the confusion is indistinguishable from correctness.
    Element 82 is a real session in both namespaces: MotoGP's race and F1's
    Post-Qualifying Show.
    """
    rt = runtime(key)
    season = 2026

    for member in rt.session_type:
        if member is rt.session_type.UNKNOWN:
            continue
        prefix = rt.build_prefix(season, 3, member)
        number = rt.element_number(member, season)

        for other in SERIES_KEYS:
            if other == key:
                continue
            other_rt = runtime(other)
            # The same *number* in the other namespace is a different session,
            # or no session at all - never this one under a different label.
            other_member = other_rt.get_session_from_element(number, season)
            if other_member is None:
                continue
            assert other_member.name != member.name or \
                other_rt.element_number(other_member, season) != number or \
                other == key, (
                    f"{prefix} means {member.name} in {key} and "
                    f"{other_member.name} in {other} at the same number {number}"
                )


# ---------------------------------------------------------------------------
# The calendars are real data, and the spine reads them
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", SERIES_KEYS)
@pytest.mark.parametrize("season", SEASONS)
def test_every_season_calendar_validates(key, season):
    """
    The shared validator, run against both series' real calendars.

    Written as one test so a calendar edit to either championship is checked by
    the same rules - the F1 calendars are the only ones whose session keys are
    not MotoGP's, which is precisely the case the rules were extended for.
    """
    from utils.validate_calendar import check_round

    spec = spec_for(key)
    calendar = calendar_for(key, season)
    problems = []
    for entry in calendar["rounds"]:
        problems.extend(check_round(
            entry, season,
            require_sessions=bool(entry.get("sessions")),
            calendar=calendar,
            spec=spec,
        ))

    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("key", SERIES_KEYS)
@pytest.mark.parametrize("season", SEASONS)
def test_every_calendar_session_key_belongs_to_its_series(key, season):
    """
    A calendar key the series' table does not know is a session nothing can
    search: the round's timetable shows it and the grabber never looks.
    """
    rt = runtime(key)
    calendar = calendar_for(key, season)

    unknown = []
    for entry in calendar["rounds"]:
        for day, sessions in (entry.get("sessions") or {}).items():
            for session_key in sessions:
                if session_key not in rt.calendar_key_to_session:
                    unknown.append(f"Round {entry['round']} {day} {session_key}")
    assert unknown == []


@pytest.mark.parametrize("key", SERIES_KEYS)
@pytest.mark.parametrize("season", SEASONS)
def test_a_round_never_runs_a_session_its_format_cannot_hold(key, season):
    """
    The gate, both ways round, for every round of every calendar.

    This is the check that caught F1's Post-Sprint Show and Ted's Sprint
    Notebook being searched on standard weekends: a programme with no format
    name is treated as "every weekend runs it", which is right for the shows
    that do and wrong for the ones that follow a Sprint.
    """
    rt = runtime(key)
    calendar = calendar_for(key, season)

    wrong = []
    for entry in calendar["rounds"]:
        if not common.is_round_active(entry):
            continue
        fmt = common.get_weekend_format(entry, calendar)
        if not fmt:
            continue
        format_sessions = {
            item.get("session")
            for day in fmt.values()
            for item in (day or [])
            if isinstance(item, dict)
        }

        for member in rt.session_type:
            if member is rt.session_type.UNKNOWN:
                continue
            runs = common.round_runs_session(entry, member, calendar, series=key)

            gated = (member.race_class == type(member).feature_class())
            if not gated:
                continue

            if member.format_session_name is not None:
                expected = member.format_session_name in format_sessions
            elif member.absent_format_name is not None:
                expected = member.absent_format_name in format_sessions
            else:
                expected = True  # a programme no format describes at all

            if runs != expected:
                wrong.append(
                    f"Round {entry['round']} {entry['venue']} "
                    f"({entry.get('weekend_format')}): {member.name} "
                    f"runs={runs} expected={expected}"
                )

    assert wrong == [], "\n".join(wrong)


@pytest.mark.parametrize("key", SERIES_KEYS)
def test_sprint_sessions_are_only_run_on_sprint_weekends(key):
    """
    The concrete case behind the test above, stated in the words a user would
    use: a Sprint is not run at a round whose format has no Sprint.
    """
    rt = runtime(key)
    sprint = [
        member for member in rt.session_type
        if getattr(member, "format_session_name", None) == "sprint"
    ]
    if not sprint:
        pytest.skip(f"{key} has no sprint session")

    sprint_rounds = 0
    for season in SEASONS:
        calendar = calendar_for(key, season)
        for entry in calendar["rounds"]:
            fmt = common.get_weekend_format(entry, calendar)
            if not fmt:
                # No format block at all (pre-2026 calendars): there is nothing
                # to gate on, so every session runs and that is correct. The
                # rule only has something to say where a calendar declares one.
                continue
            has_sprint = any(
                item.get("session") == "sprint"
                for day in fmt.values() for item in (day or [])
                if isinstance(item, dict)
            )
            if has_sprint:
                sprint_rounds += 1
            assert common.round_runs_session(
                entry, sprint[0], calendar, series=key
            ) == has_sprint, f"Round {entry['round']} {entry.get('venue')}"

    assert sprint_rounds > 0, f"{key}'s calendars declare no sprint weekend at all"


# ---------------------------------------------------------------------------
# The numbering sorts in the order a weekend happens
# ---------------------------------------------------------------------------

#: Each series' weekend, in the order it actually happens, per format it runs.
#:
#: This is the *specification*, not an observation of the tables: the numbers
#: are written into folder names, so a weekend that sorts out of order is a
#: library that lists the race before qualifying and cannot be fixed without
#: renaming files. MotoGP's own formats are asserted in test_session_numbering;
#: this is the same requirement stated for every series.
WEEKEND_ORDERS = {
    "motogp": {
        "standard": ["MOTOGP_FP1", "MOTOGP_PRACTICE", "MOTOGP_FP2",
                     "MOTOGP_Q1", "MOTOGP_Q2", "MOTOGP_WARMUP", "MOTOGP_RACE"],
        "sprint": ["MOTOGP_FP1", "MOTOGP_PRACTICE", "MOTOGP_FP2",
                   "MOTOGP_Q1", "MOTOGP_Q2", "MOTOGP_SPRINT",
                   "MOTOGP_WARMUP", "MOTOGP_RACE"],
    },
    "f1": {
        "standard": ["PRACTICE_1", "PRACTICE_2", "PRACTICE_3",
                     "QUALIFYING", "RACE"],
        "sprint": ["PRACTICE_1", "SPRINT_QUALIFYING", "SPRINT",
                   "QUALIFYING", "RACE"],
    },
}


@pytest.mark.parametrize("key", SERIES_KEYS)
def test_the_numbers_sort_in_the_order_the_weekend_happens(key):
    rt = runtime(key)
    season = 2026

    orders = WEEKEND_ORDERS.get(key)
    assert orders, f"no weekend order declared for {key}; add it, do not skip it"

    for fmt_name, members in orders.items():
        numbers = [rt.element_number(rt.session_type[name], season) for name in members]
        assert numbers == sorted(numbers), (
            f"{key} {fmt_name}: {list(zip(members, numbers))} does not sort "
            f"chronologically"
        )
        assert len(set(numbers)) == len(numbers)


@pytest.mark.parametrize("key", SERIES_KEYS)
def test_the_numbers_a_series_claims_are_the_ones_its_calendars_describe(key):
    """
    The element numbers and the calendars have to agree about which sessions a
    weekend has: a format naming a session the series cannot number is a
    session that can never be searched.
    """
    rt = runtime(key)
    for season in SEASONS:
        calendar = calendar_for(key, season)
        for fmt_name, days in (calendar.get("weekend_formats") or {}).items():
            for day, items in days.items():
                for item in items or []:
                    name = item.get("session")
                    if name is None:
                        continue
                    assert name in rt.format_name_to_session, (
                        f"{key} {season} {fmt_name}/{day}: format names "
                        f"{name!r}, which the series does not declare"
                    )


# ---------------------------------------------------------------------------
# The paths and files are per series, for every series
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", SERIES_KEYS)
def test_no_two_series_share_a_library_a_downloads_folder_or_a_category(key):
    """
    Sharing any of the three is not an error anywhere - it files one
    championship's media into the other's library, where Plex accepts it and
    matches it against nothing.
    """
    spec = spec_for(key)
    others = [s for s in all_series() if s.key != key]

    for other in others:
        assert spec.library_path != other.library_path, other.key
        assert spec.downloads_path != other.downloads_path, other.key
        assert spec.sabnzbd_category != other.sabnzbd_category, other.key
        assert spec.calendar_prefix != other.calendar_prefix, other.key
        assert spec.state_prefix != other.state_prefix, other.key


@pytest.mark.parametrize("key", SERIES_KEYS)
def test_every_state_file_is_namespaced_for_every_series(key):
    from utils.state_files import STATE_FILES, state_filename

    for name in STATE_FILES:
        filename = state_filename(name, key)
        assert filename.startswith(f"{name}.")
        assert filename != f"{name}.json", key


# ---------------------------------------------------------------------------
# The derived views follow the series, not a default
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", SERIES_KEYS)
def test_the_stats_page_reports_only_its_own_classes(key, tmp_path, monkeypatch):
    """
    The chart is built from the declaration (step 7). The failure this pins is
    the one that survived that fix: asking for one series and being handed the
    *default* series' class list, which renders three zeroes beside F1's real
    numbers and looks like "nothing has ever downloaded".
    """
    import grab_stats

    # grab_stats reads CONFIG_DIR lazily from the environment, so this is how a
    # caller points it at another /config (and how the app itself does it).
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    # A series only appears in `active_series` once it has state on disk - an
    # empty panel for a championship that has never grabbed anything is
    # deliberately not shown.
    (tmp_path / f"grabber_state.{key}.json").write_text(json.dumps({"grabbed": {}}))

    summary = grab_stats.get_stats_summary(series=key)
    declared = {c.display_name for c in spec_for(key).classes}

    assert set(summary["by_class"]) == declared
    # `active_series` is the list of series this install has data for;
    # `series` is the one that was asked for.
    assert key in summary["active_series"]
    assert summary["series"] == key


@pytest.mark.parametrize("key", SERIES_KEYS)
def test_the_series_owns_the_folder_and_search_wording_its_releases_carry(key):
    """
    ``search_prefix`` is what a release title actually contains ("MotoGP 2026
    Buriram", "Formula1 2026 Silverstone"). Two series sharing one would make
    every search match the other championship's releases.
    """
    spec = spec_for(key)
    assert spec.search_prefix
    for other in all_series():
        if other.key == key:
            continue
        assert spec.search_prefix != other.search_prefix, other.key
        assert spec.display_name != other.display_name, other.key
        assert spec.log_name != other.log_name, other.key


# ---------------------------------------------------------------------------
# The element-number decision, per series
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", SERIES_KEYS)
def test_a_number_means_one_session_in_every_season_it_is_used(key):
    """
    Either a series has two numbering schemes and the cutover season decides
    between them, or it has one and no season can ever be ambiguous.

    F1 is the second case by decision (renumber into blocks of ten and re-fetch,
    no legacy table). MotoGP is the first. Both must answer the same number
    consistently within a season, and the number that *is* ambiguous where the
    schemes disagree must be refused rather than answered by either.
    """
    rt = runtime(key)
    seasons = (2025, 2026, 2027)

    for season in seasons:
        for number in range(1, 100):
            resolved = rt.get_session_from_element(number, season=season)
            if resolved is None:
                continue
            # Whatever it resolved to must name that number back, unless the two
            # schemes disagree for this season - in which case it must not have
            # resolved at all.
            if rt.element_is_ambiguous(number, season=season):
                pytest.fail(
                    f"{key} {season}: element {number} is ambiguous but resolved "
                    f"to {resolved.name}"
                )
            assert rt.element_number(resolved, season) == number, (
                f"{key} {season}: element {number} resolved to {resolved.name}, "
                f"which numbers as {rt.element_number(resolved, season)}"
            )

    if rt.legacy_session_elements:
        assert rt.legacy_element_season is not None, (
            f"{key} declares a legacy table without a cutover season, so every "
            f"season would read two schemes at once"
        )
    else:
        assert rt.legacy_element_season is None


@pytest.mark.parametrize("key", SERIES_KEYS)
def test_the_default_desired_sessions_are_all_real_sessions(key):
    """
    A typo in ``default_desired_sessions`` is invisible: the key silently never
    matches, and the session is simply never wanted by a fresh install.
    """
    spec = spec_for(key)
    rt = runtime(key)
    known = {member.name for member in rt.session_type}

    unknown = set(spec.default_desired_sessions) - known
    assert unknown == set(), f"{key} declares desired sessions that do not exist: {unknown}"
    assert any(spec.default_desired_sessions.values()), f"{key} wants nothing by default"


# ---------------------------------------------------------------------------
# The release matcher reads titles with the series' own vocabulary
# ---------------------------------------------------------------------------

def _release(title):
    return {"title": title}


@pytest.mark.parametrize("key", SERIES_KEYS)
def test_a_title_from_the_series_is_read_as_that_series(key):
    """
    Class detection is the first gate every candidate passes. Reading an F1
    title with MotoGP's rules classified it as MotoGP, so the matcher rejected
    it for every F1 target: the championship could never have downloaded
    anything, and the search would have looked like "no releases published".
    """
    spec = spec_for(key)
    rt = runtime(key)
    title = f"{spec.search_prefix}.2026.Round.01.Race.1080p.VERUM"

    assert common.detect_race_class(title, series=key) == rt.feature_class


@pytest.mark.parametrize("key", SERIES_KEYS)
def test_a_title_from_another_series_is_never_read_as_this_one(key):
    """
    A MotoGP title offered to an F1 target must be recognised as MotoGP and
    rejected, not adopted as F1 because nothing matched.
    """
    rt = runtime(key)
    for other in all_series():
        if other.key == key:
            continue
        title = f"{other.search_prefix}.2026.Round.01.Race.1080p.VERUM"
        assert common.detect_race_class(title, series=key) != rt.feature_class


@pytest.mark.parametrize("key", SERIES_KEYS)
def test_the_matcher_accepts_the_session_it_was_asked_for(key):
    """
    End to end through ``filter_releases`` against the series' own calendar:
    the right session is accepted, and the neighbouring sessions are not.

    This is the test that would have failed for F1 before the fix, at the first
    check, for every release.
    """
    from series import all_series as _all

    rt = runtime(key)
    calendar = calendar_for(key, 2026)
    round_info = calendar["rounds"][0]
    venue = round_info["venue"]

    for name in ("RACE", "QUALIFYING") if key == "f1" else ("MOTOGP_RACE", "MOTOGP_Q2"):
        target = rt.session_type[name]
        wanted = f"{spec_for(key).search_prefix}.2026.{venue}.{name.replace('_', '.')}.1080p.VERUM"
        other = f"{spec_for(key).search_prefix}.2026.{venue}.Somethingelse.1080p.VERUM"

        accepted = common.filter_releases(
            [_release(wanted), _release(other)], target, 2026, round_info,
            "1080p", ["VERUM"], calendar, series=key,
        )
        titles = [r["title"] for r in accepted]
        assert wanted in titles, (
            f"{key}: {name} release {wanted!r} was not accepted; got {titles}"
        )
        assert other not in titles, (
            f"{key}: an unidentifiable release was accepted as {name}"
        )


@pytest.mark.parametrize("key", SERIES_KEYS)
def test_a_release_is_matched_to_the_round_its_own_venue_names(key):
    """
    Every search is scoped to one round by the round passed in, but the release
    says which round it is. A matcher that ignores the title's venue accepts the
    first round's release for round 11 - which is how a library ends up with one
    race filed eleven times.
    """
    rt = runtime(key)
    calendar = calendar_for(key, 2026)
    rounds = calendar["rounds"]
    later = rounds[10]
    first = rounds[0]
    title = (f"{spec_for(key).search_prefix}.2026.{later.get('round'):02d}."
             f"{later['venue'].replace(' ', '.')}.RACE.1080p.VERUM"
             if key == "f1" else
             f"{spec_for(key).search_prefix}.2026.{later['venue'].replace(' ', '.')}."
             f"MOTOGP.RACE.1080p.VERUM")
    target = rt.session_type["RACE"] if key == "f1" else rt.session_type["MOTOGP_RACE"]

    args = ([_release(title)], target, 2026, "1080p", ["VERUM"], calendar)
    for round_info, should_match in ((later, True), (first, False)):
        accepted = common.filter_releases(
            *args[:3], round_info, *args[3:], series=key
        )
        assert bool(accepted) is should_match, (
            f"{key}: round {round_info.get('round')} "
            f"{'refused' if should_match else 'accepted'} {title!r}"
        )


def test_a_programme_that_only_airs_on_sprint_weekends_is_not_searched_otherwise():
    """
    F1's Post-Sprint Show and Ted's Sprint Notebook follow the Sprint. Declaring
    them without a format name (as every other programme is) made them run on
    all 24 rounds instead of the 6 sprint ones - six times the searches, on
    releases that do not exist.

    Asserted through the matcher, not just through ``round_runs_session``: the
    declaration is only worth anything if the code path that rejects a
    candidate reads it.
    """
    rt = runtime("f1")
    calendar = calendar_for("f1", 2026)
    sprint = next(r for r in calendar["rounds"]
                  if "sprint" in (r.get("f1_sessions") or []))
    standard = next(r for r in calendar["rounds"]
                    if "sprint" not in (r.get("f1_sessions") or []))

    programme = rt.session_type["POST_SPRINT_SHOW"]

    for round_info, should_match in ((sprint, True), (standard, False)):
        # The title has to name the round it belongs to: the matcher will not
        # accept a release for a round the title does not identify, which is
        # what stops round 1's race being filed for round 11.
        title = (f"Formula1.2026.{round_info['venue'].replace(' ', '.')}."
                 f"Post.Sprint.Show.1080p.VERUM")
        accepted = common.filter_releases(
            [_release(title)], programme, 2026, round_info,
            "1080p", ["VERUM"], calendar, series="f1",
        )
        assert bool(accepted) is should_match, (
            f"round {round_info['round']} ({round_info['venue']}) "
            f"{'refused' if should_match else 'accepted'} {title!r}"
        )
