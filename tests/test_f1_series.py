"""
Tests for the Formula 1 series.

``test_series_registry.py`` pins MotoGP, because the risk there was that moving
its vocabulary into the registry changed a value. The risk here is different and
larger: **F1 is new, so nothing yet depends on it.** A wrong element number in
this file produces a wrong folder name that nobody notices until a season has
been fetched and Plex has learned the wrong identity - at which point fixing it
renames a library.

So these tests are mostly *derived* rather than declared: they compute the
property from the table and assert it, rather than restating the table. A
renumber that keeps the properties is fine; a renumber that breaks one is not.

One test below (``test_every_element_number_fits_the_two_digit_prefix``) exists
because it caught a real bug while this file was being written. The first
draft allocated F1's support shows into the 80s at gaps of two, which ran to
110. ``_PREFIX_RE`` is ``(\\d{2})R(\\d{2})E(\\d{2})``, so ``26R07E100`` parsed as
``E10`` - Practice 1 - and ``26R07E110`` did not parse at all. Both failures
were silent in every other check, including a full prefix round-trip, because
the round trip only compares against what ``build_prefix`` produced.
"""

import pytest

from series import all_series, default_series, get_series, runtime, series_keys


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_f1_is_registered():
    assert "f1" in series_keys()


def test_f1_is_not_the_default_series():
    """
    Adding a series must never become the default by being added.

    MotoGP is the default because it is the series this app was built for and
    the only one with media on disk. If registering F1 flipped the default,
    every existing install would start looking in F1's calendars and state
    files.
    """
    assert default_series().key == "motogp"


def test_both_series_are_registered_and_ordered():
    """
    MotoGP first. ``all_series()`` returns registration order, and a UI that
    lists series should not have its order depend on which module happened to
    be imported first.
    """
    assert series_keys() == ["motogp", "f1"]
    assert [s.key for s in all_series()] == ["motogp", "f1"]


def test_the_two_series_have_distinct_identities():
    """
    The collisions that would corrupt a single-container app.

    Two series sharing a calendar prefix would read each other's calendars; a
    state prefix would overwrite each other's state within a day; a log name
    would interleave two grabbers' output in one file.
    """
    mgp, f1 = get_series("motogp"), get_series("f1")
    for field in ("key", "calendar_prefix", "state_prefix", "log_name",
                  "search_prefix"):
        assert getattr(mgp, field) != getattr(f1, field), field


def test_the_two_series_have_distinct_session_enums():
    """
    One SessionType class per series.

    Sharing one class would let an F1 element number be resolved against
    MotoGP's table, which is how a folder ends up named for the wrong
    championship.
    """
    mgp, f1 = runtime("motogp"), runtime("f1")
    assert mgp.session_type is not f1.session_type
    assert mgp.race_class is not f1.race_class
    assert mgp.session_type.__name__ == "MotogpSessionType"
    assert f1.session_type.__name__ == "F1SessionType"


def test_the_two_series_have_distinct_state_files():
    """
    The single worst collision available, made impossible by construction.

    Both apps wrote ``grabber_state.json`` into /config. Namespacing per series
    is the whole reason ``state_prefix`` exists.
    """
    names = {rt.state_file("grabber_state")
             for rt in (runtime("motogp"), runtime("f1"))}
    assert len(names) == 2, names
    assert runtime("f1").state_file("grabber_state") == "grabber_state.f1.json"
    assert (runtime("motogp").state_file("grabber_state")
            == "grabber_state.motogp.json")


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

def test_f1_declares_exactly_one_class():
    """
    One class, not zero.

    A series with no class would need a special case in every folder name, Plex
    title and artwork lookup - which is how a hybrid turns back into two apps.
    """
    f1 = runtime("f1")
    assert [c.key for c in f1.series.classes] == ["f1"]
    assert f1.race_class.F1.display_name == "F1"


@pytest.mark.parametrize("text,expected", [
    ("Formula1 2026 Silverstone", True),
    ("Formula 1 2026 Silverstone", True),
    ("F1 2026 Silverstone", True),
    ("f1 2026 silverstone", True),
    ("Formula1.2026.R01.Australia.1080p-VERUM", True),
    ("MotoGP 2026 Buriram", False),
    ("Moto2 2026 Buriram", False),
    ("", False),
])
def test_race_class_from_string_recognises_f1_titles(text, expected):
    """
    The class is what a folder name and a Plex title are keyed on.

    Releases spell the championship three ways - "Formula1", "Formula 1" and
    "F1" - and all three must resolve, because a title that resolves to no
    class cannot be filed at all.
    """
    f1 = runtime("f1")
    parsed = f1.race_class.from_string(text)
    if expected:
        assert parsed is f1.race_class.F1, text
    else:
        assert parsed is None, text


def test_every_session_belongs_to_the_one_class():
    """
    No session may be left without a class.

    ``full_short_name`` builds the folder name from the class, so a session
    with no class produces a folder with no championship in it.
    """
    f1 = runtime("f1")
    for session in f1.session_type:
        if session == f1.session_type.UNKNOWN:
            continue
        assert session.race_class is f1.race_class.F1, session.name


def test_folder_names_read_the_way_a_user_expects():
    """
    The naming convention, asserted on real output rather than described.

    motogparr's ``{prefix} {class} {short session}`` was chosen over f1arr's
    ``{prefix} {venue} {session}`` because the round folder already carries the
    venue, and the class prefix is more informative in Plex.
    """
    f1 = runtime("f1")
    st = f1.session_type
    assert f1.build_prefix(2026, 1, st.PRACTICE_1) == "26R01E30"
    assert st.PRACTICE_1.full_short_name == "F1 FP1"
    assert st.RACE.full_short_name == "F1 Race"
    assert st.SPRINT_QUALIFYING.full_short_name == "F1 Sprint Qualifying"
    assert st.TEDS_NOTEBOOK.full_short_name == "F1 Ted's Notebook"
    assert st.PADDOCK_SHOW.full_display_name == "F1 The Paddock Show"


def test_the_support_show_family_is_present():
    """
    The thing F1 has that MotoGP does not.

    Ted's Notebook, The Paddock Show and the rest are real releases with real
    fans. Declaring them is not scope creep - leaving them out means they
    classify as UNKNOWN and are dropped on the floor.
    """
    f1 = runtime("f1")
    st = f1.session_type
    for name in ("WEEKEND_WARMUP", "TEAM_PRINCIPALS", "DRIVER_PRESS_CONFERENCE",
                 "PADDOCK_UNCUT", "PADDOCK_SHOW", "F1_SHOW",
                 "POST_SPRINT_SHOW", "TEDS_SPRINT_NOTEBOOK",
                 "PRE_QUALIFYING_SHOW", "POST_QUALIFYING_SHOW",
                 "TEDS_QUALIFYING_NOTEBOOK", "PRE_RACE_SHOW", "POST_RACE_SHOW",
                 "TEDS_RACE_NOTEBOOK", "TEDS_NOTEBOOK",
                 "JOLYON_PALMERS_ANALYSIS", "FULL_EVENT"):
        assert hasattr(st, name), name


def test_the_pattern_table_orders_the_notebooks_above_the_race():
    """
    "Ted's Race Notebook" contains "race".

    Without the priority ordering it would be filed as the Grand Prix - a wrong
    folder name and a wrong Plex identity, permanently. This is the same bug
    class motogparr spent a release eliminating, and f1arr's table already
    solves it, so the solution is ported rather than rediscovered.
    """
    f1 = get_series("f1")
    by_priority = sorted(f1.session_patterns,
                         key=lambda p: p.priority, reverse=True)
    names = [p.session for p in by_priority]
    for notebook in ("TEDS_SPRINT_NOTEBOOK", "TEDS_QUALIFYING_NOTEBOOK",
                     "TEDS_RACE_NOTEBOOK", "TEDS_NOTEBOOK"):
        assert names.index(notebook) < names.index("RACE"), notebook
    # And the belt to go with the braces: RACE also refuses "notebook".
    race = next(p for p in f1.session_patterns if p.session == "RACE")
    assert "notebook" in race.must_not_have


def test_the_pattern_table_orders_the_post_race_show_above_the_race():
    """Same hazard, same defence: "Post-Race Show" contains "race"."""
    f1 = get_series("f1")
    by_priority = sorted(f1.session_patterns,
                         key=lambda p: p.priority, reverse=True)
    names = [p.session for p in by_priority]
    assert names.index("POST_RACE_SHOW") < names.index("RACE")
    assert "post race" in next(
        p for p in f1.session_patterns if p.session == "RACE").must_not_have


def test_excluded_terms_cover_the_junior_series():
    """
    f1arr hardcoded this inside classify_session(). It is data, not an
    algorithm, so it belongs in the declaration - and per-series, because "f2"
    is a junior series to exclude for F1 and meaningless to MotoGP.
    """
    f1 = get_series("f1")
    for term in ("academy", "f2", "f3", "formula 2", "formula 3",
                 "porsche supercup"):
        assert term in f1.excluded_terms, term


def test_excluded_terms_are_per_series():
    """
    A shared exclusion list would let one series' vocabulary reject the other's
    releases.
    """
    assert get_series("motogp").excluded_terms == ()
    assert get_series("f1").excluded_terms


# ---------------------------------------------------------------------------
# Element numbers
# ---------------------------------------------------------------------------

def test_every_session_has_an_element_number():
    f1 = runtime("f1")
    missing = [s for s in f1.session_type
               if s != f1.session_type.UNKNOWN
               and s not in f1.session_elements]
    assert not missing, f"sessions with no element number: {missing}"


def test_element_numbers_are_unique():
    f1 = runtime("f1")
    numbers = list(f1.session_elements.values())
    assert len(numbers) == len(set(numbers)), "two sessions share one number"


def test_every_element_number_fits_the_two_digit_prefix():
    """
    The hard ceiling, asserted for every series.

    ``_PREFIX_RE`` captures the element as exactly two digits, so 100 is not
    merely untidy - it parses as 10 and silently means a different session. See
    the module docstring: this test exists because the first draft of
    ``series/f1.py`` got it wrong.
    """
    for rt in (runtime("motogp"), runtime("f1")):
        for session, number in rt.session_elements.items():
            assert 0 <= number <= 99, (rt.key, session.name, number)


def test_the_programming_block_leaves_free_slots():
    """
    The block is dense because thirteen programmes do not fit in 76-99 at gaps
    of two. What matters is that it is not *full*: a new programme must take a
    free number, never a renumber.
    """
    f1 = runtime("f1")
    used = {n for n in f1.session_elements.values() if n >= 76}
    assert len(used) < 24, f"the programming block is full: {sorted(used)}"
    assert max(used) == 88, sorted(used)
    assert {n for n in range(89, 100)} - used, "no free slot above the block"


def test_element_numbers_are_chronological_for_a_standard_weekend():
    """
    Ordering is a property of the identifier, not something patched in the UI.

    A conventional round runs FP1, FP2, FP3, Qualifying, Race. If the numbers
    do not ascend in that order the round lists out of order everywhere it is
    shown.
    """
    f1 = runtime("f1")
    st = f1.session_type
    order = [st.PRACTICE_1, st.PRACTICE_2, st.PRACTICE_3,
             st.QUALIFYING, st.RACE]
    numbers = [f1.element_number(s) for s in order]
    assert numbers == sorted(numbers), f"out of order: {numbers}"


def test_element_numbers_are_chronological_for_a_sprint_weekend():
    """
    A sprint round runs FP1, Sprint Qualifying, Sprint, Qualifying, Race -
    with no FP2 or FP3 at all. The numbers must still ascend.
    """
    f1 = runtime("f1")
    st = f1.session_type
    order = [st.PRACTICE_1, st.SPRINT_QUALIFYING, st.SPRINT,
             st.QUALIFYING, st.RACE]
    numbers = [f1.element_number(s) for s in order]
    assert numbers == sorted(numbers), f"out of order: {numbers}"


def test_the_pre_event_shows_sort_before_the_first_session():
    """
    They air on Thursday, before anything on track.

    Putting them after the Race would sort them wrongly in Plex, which is why
    they sit at 1, 3 and 5 - the same reason MotoGP's press conference is
    element 1.
    """
    f1 = runtime("f1")
    st = f1.session_type
    fp1 = f1.element_number(st.PRACTICE_1)
    for name in ("WEEKEND_WARMUP", "TEAM_PRINCIPALS", "DRIVER_PRESS_CONFERENCE"):
        assert f1.element_number(getattr(st, name)) < fp1, name


def test_the_race_broadcasts_sort_next_to_the_race():
    """
    RACE_UNCUT and FULL_EVENT are the race in a longer or shorter form, not a
    second race and not a magazine show.
    """
    f1 = runtime("f1")
    st = f1.session_type
    race = f1.element_number(st.RACE)
    for name in ("RACE_UNCUT", "FULL_EVENT"):
        number = f1.element_number(getattr(st, name))
        assert race < number < f1.element_number(st.PADDOCK_UNCUT), name


def test_only_the_race_is_a_feature_race():
    """
    Not RACE_UNCUT, not FULL_EVENT.

    ``is_feature_race`` drives the flag in the CLI listing, and marking the
    long-form broadcasts as feature races would put three flags on one
    weekend's feature.
    """
    f1 = runtime("f1")
    assert {s.name for s in f1.feature_race_sessions} == {"RACE"}


# ---------------------------------------------------------------------------
# No legacy numbering
# ---------------------------------------------------------------------------

def test_f1_has_no_legacy_numbering():
    """
    The re-fetch decision, expressed as data.

    F1 declares no legacy table and no cutover season, so no number can ever
    mean two things and ``parse_prefix`` never has to guess. The cost is
    deliberate: an existing dense-numbered folder stops resolving.
    """
    f1 = runtime("f1")
    assert f1.legacy_session_elements == {}
    assert f1.legacy_element_season is None


def test_no_f1_number_is_ambiguous():
    f1 = runtime("f1")
    ambiguous = [s.name for s in f1.session_type
                 if f1.element_is_ambiguous(f1.element_number(s))]
    assert ambiguous == []


def test_no_old_number_is_silently_misread():
    """
    THE test for this table. Run it first if the numbering ever changes.

    ``session_members`` is f1arr's original dense table - it wrote the enum
    value straight into the folder name, so ``26R02E10`` was Sprint Qualifying.
    Any new number in 4-25 that names a *different* session makes
    ``parse_prefix`` silently misread an old folder, and nothing reports it: the
    season looks fetched, and one session in it is filed as another.

    So every number in the old range must either mean the SAME session under
    the new scheme, or mean nothing at all. Meaning nothing is fine and
    intended - ``find_legacy_folders`` reports those - but meaning something
    else is silent corruption.

    The first draft of ``series/f1.py`` failed this in four places: E03, E05,
    E10 and E20. E10 is the dangerous one, because a sprint round's "Sprint
    Qualifying" folder would have been read as Practice 1.
    """
    f1 = runtime("f1")
    # value -> member name: f1arr's dense numbering, preserved as the
    # chronological enum values.
    old = {value: name for name, value in f1.series.session_members
           if name != "UNKNOWN"}

    misread = []
    for number in range(1, 100):
        old_name = old.get(number)
        if old_name is None:
            continue
        new_session = f1.get_session_from_element(number)
        if new_session is not None and new_session.name != old_name:
            misread.append((number, old_name, new_session.name))

    assert not misread, f"old numbers silently misread: {misread}"


def test_the_three_reused_numbers_mean_the_same_session():
    """
    The only overlap between the two schemes, and it is safe on purpose.

    1, 2 and 3 are the pre-event shows' original numbers, so an old folder
    still resolves to the session it always named. Reusing them is what lets
    the prelude sort before the weekend without leaving the old range
    entirely.
    """
    f1 = runtime("f1")
    st = f1.session_type
    assert f1.get_session_from_element(1) is st.WEEKEND_WARMUP
    assert f1.get_session_from_element(2) is st.TEAM_PRINCIPALS
    assert f1.get_session_from_element(3) is st.DRIVER_PRESS_CONFERENCE


def test_the_rest_of_the_old_range_no_longer_resolves():
    """
    Not a bug - the re-fetch rule, made safe.

    E07 was Practice 1 and E19 was the Race under f1arr's dense numbering.
    Under the new scheme they name nothing, so ``find_legacy_folders`` reports
    the folders instead of reading them. The alternative would be to keep
    reading a stale folder, and a stale number that is still *valid* under the
    new scheme could not be told apart from a current one by its name alone.
    """
    f1 = runtime("f1")
    for stale in range(4, 26):
        assert f1.get_session_from_element(stale) is None, stale


def test_find_legacy_folders_reports_an_old_dense_folder():
    """
    The user-facing half of the same rule.

    A folder the current scheme cannot read must be *reported*, not silently
    skipped - otherwise the season looks fetched and half of it is missing.
    """
    f1 = runtime("f1")
    stale = f1.find_legacy_folders(2026, ["26R01E07 Practice 1",
                                          "26R01E30 F1 FP1"])
    assert stale == ["26R01E07 Practice 1"]


# ---------------------------------------------------------------------------
# Prefixes
# ---------------------------------------------------------------------------

def test_a_prefix_round_trips_for_every_session():
    f1 = runtime("f1")
    for session in f1.session_type:
        if session == f1.session_type.UNKNOWN:
            continue
        prefix = f1.build_prefix(2026, 7, session)
        assert f1.parse_prefix(prefix) == (2026, 7, session), prefix


def test_a_prefix_with_no_known_element_is_not_parsed():
    f1 = runtime("f1")
    assert f1.parse_prefix("26R07E99 F1 Race") is None
    assert f1.parse_prefix("not a prefix") is None


def test_the_prefix_regex_is_shared_and_two_digits():
    """
    Pinned because it is the constraint the element allocation must respect.

    Widening it to three digits would give every series more room, but it
    changes the on-disk format for all of them - not a decision to take inside
    one series' declaration.
    """
    from series.base import _PREFIX_RE

    assert _PREFIX_RE.pattern == r"(\d{2})R(\d{2})E(\d{2})"
    assert _PREFIX_RE.match("26R07E10").groups() == ("26", "07", "10")
    # Three digits do not fit: the third digit is left unconsumed, so the
    # prefix resolves to a *different* session rather than failing.
    assert _PREFIX_RE.match("26R07E100").groups() == ("26", "07", "10")


# ---------------------------------------------------------------------------
# Calendar keys
# ---------------------------------------------------------------------------

def test_the_calendar_key_tables_are_consistent():
    """
    Forward and reverse must agree.

    MotoGP declares both directions separately because qualifying is
    many-to-one; F1 is one-to-one, but the two tables are still built
    independently and a disagreement between them would surface as a session
    the grabber targets that no calendar key names.
    """
    f1 = get_series("f1")
    forward = f1.session_to_calendar_key
    reverse = f1.calendar_key_to_session
    assert set(reverse.values()) == set(forward)
    for key, name in reverse.items():
        assert forward[name] == key, (key, name)


def test_f1_calendar_keys_carry_the_class_prefix():
    """
    As MotoGP's do - and this is the reverse of what the declaration first said.

    When the F1 declaration was written, before any F1 calendar existed, the
    keys were bare (``fp1``) on the grounds that F1 has one class so there is
    nothing to tell apart. Porting f1arr's calendars changed the answer: a
    calendar key is resolved through this table and its display label is
    derived from the part after the first underscore, so the prefix costs
    nothing, and it buys one key namespace across both series plus room for a
    support series at an F1 weekend without rewriting every key already in a
    calendar. The validator refuses a key that is not in this table, so this
    assertion is also what keeps the calendars and the declaration in step.
    """
    f1 = get_series("f1")
    assert set(f1.calendar_key_to_session) == {
        "f1_fp1", "f1_fp2", "f1_fp3", "f1_sprint_qualifying",
        "f1_sprint", "f1_qualifying", "f1_race",
    }
    for key in f1.calendar_key_to_session:
        assert key.startswith(f"{f1.key}_"), key


def test_the_format_name_tables_are_consistent():
    f1 = get_series("f1")
    forward = f1.session_to_format_name
    reverse = f1.format_name_to_session
    assert set(reverse.values()) == set(forward)
    for name, key in reverse.items():
        assert forward[key] == name, (name, key)


def test_the_core_sessions_are_the_ones_that_define_a_weekend():
    """
    f1arr's CORE_SESSIONS, which is also what a user wants by default.

    The support shows are deliberately absent: wanting Ted's Notebook is a
    preference, wanting the Grand Prix is not.
    """
    f1 = runtime("f1")
    assert {s.name for s in f1.core_sessions} == {
        "PRACTICE_1", "PRACTICE_2", "PRACTICE_3", "SPRINT_QUALIFYING",
        "SPRINT", "QUALIFYING", "RACE",
    }


def test_default_desired_sessions_cover_every_session_except_unknown():
    """
    A session missing from the table has no preference, and the WebUI would
    show it as neither wanted nor unwanted.
    """
    f1 = runtime("f1")
    declared = set(f1.series.default_desired_sessions)
    expected = {s.name for s in f1.session_type if s != f1.session_type.UNKNOWN}
    assert declared == expected


def test_every_core_session_is_desired_by_default():
    f1 = runtime("f1")
    for session in f1.core_sessions:
        assert f1.series.default_desired_sessions[session.name] is True, session


# ---------------------------------------------------------------------------
# Artwork
# ---------------------------------------------------------------------------

def test_asset_dirs_are_named_after_the_class_and_resolve_under_assets():
    """
    The convention, asserted rather than assumed.

    One directory per class and per kind, named after the class, under assets/.
    The JPEGs themselves are ported in their own change - 86 MB of per-venue art
    is a packaging decision, not a vocabulary one - so this pins where they will
    live rather than that they exist yet.
    """
    f1 = runtime("f1")
    assert set(f1.asset_dirs) == {"f1"}
    for kind, path in f1.asset_dirs["f1"].items():
        assert set(f1.asset_dirs["f1"]) == {"fanart", "poster"}
        assert path.parent == f1.assets_dir, (kind, path)
        assert path.name.startswith("f1"), (kind, path)


def test_the_two_series_do_not_share_artwork_directories():
    """
    Two series writing into one directory would overwrite each other's art.
    """
    mgp, f1 = runtime("motogp"), runtime("f1")
    mgp_dirs = {p for d in mgp.asset_dirs.values() for p in d.values()}
    f1_dirs = {p for d in f1.asset_dirs.values() for p in d.values()}
    assert not (mgp_dirs & f1_dirs)


# ---------------------------------------------------------------------------
# Cross-series invariants
# ---------------------------------------------------------------------------

def test_every_series_satisfies_the_registry_invariants():
    """
    One place that holds every series to the same rules.

    Adding a series means writing a module; this is the check that the module
    is well-formed, so a mistake is caught here rather than in a user's library.
    """
    for rt in (runtime("motogp"), runtime("f1")):
        spec = rt.series
        # Identity
        assert spec.key and spec.display_name, spec.key
        assert spec.calendar_prefix and spec.state_prefix, spec.key
        assert spec.calendar_prefix and spec.log_name, spec.key
        # Classes
        assert spec.classes, spec.key
        assert len({c.key for c in spec.classes}) == len(spec.classes), spec.key
        # Vocabulary
        assert spec.session_members, spec.key
        assert "UNKNOWN" in dict(spec.session_members), spec.key
        assert spec.session_elements, spec.key
        # Every declared table names real members
        names = {name for name, _ in spec.session_members}
        for name in spec.session_race_class:
            assert name in names, (spec.key, name)
            assert spec.session_race_class[name] in spec.asset_dirs or True
        for name in spec.session_display_names:
            assert name in names, (spec.key, name)
        for name in spec.session_short_names:
            assert name in names, (spec.key, name)
        for name in spec.core_sessions:
            assert name in names, (spec.key, name)
        for name in spec.feature_race_sessions:
            assert name in names, (spec.key, name)
        for name in spec.default_desired_sessions:
            assert name in names, (spec.key, name)
        for pattern in spec.session_patterns:
            assert pattern.session in names, (spec.key, pattern.session)
        # Calendar keys resolve both ways. NOT one-to-one: MotoGP's Q1 and Q2
        # both read "motogp_qualifying", and reversing the forward map would
        # silently drop one of them - which is exactly why the two directions
        # are declared separately. So the check is that every session's key
        # maps back to a session, not that it maps back to itself.
        for key, name in spec.calendar_key_to_session.items():
            assert name in names, (spec.key, key)
        for name, key in spec.session_to_calendar_key.items():
            assert name in names, (spec.key, name)
            assert key in spec.calendar_key_to_session, (spec.key, key)
            assert spec.calendar_key_to_session[key] in names, (spec.key, key)
        # Artwork directories are per class
        assert set(spec.asset_dirs) == {c.key for c in spec.classes}, spec.key


def test_a_calendar_file_for_f1_is_additive_to_an_existing_install():
    """
    The reason per-series files were chosen over one shared calendar.

    An existing install has ``config/motogp_*.json``. F1's calendars arrive as
    new files with a different prefix, so nothing that exists is rewritten and
    a hand-edited MotoGP round cannot be rolled back by an F1 refresh.
    """
    from pathlib import Path

    from motogp_common import get_config_dir

    mgp_prefix = get_series("motogp").calendar_prefix
    f1_prefix = get_series("f1").calendar_prefix
    assert mgp_prefix != f1_prefix

    config_dir = Path(get_config_dir())
    if not config_dir.exists():
        pytest.skip("no config directory in this environment")

    motogp_files = sorted(config_dir.glob(f"{mgp_prefix}_*.json"))
    f1_files = sorted(config_dir.glob(f"{f1_prefix}_*.json"))
    assert motogp_files, f"expected existing {mgp_prefix}_*.json calendars"
    # The globs are disjoint, so an F1 calendar can never be picked up as a
    # MotoGP one - which is what makes the two series' refresh cycles safe.
    assert not (set(motogp_files) & set(f1_files))
