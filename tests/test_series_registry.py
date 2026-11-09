"""
Tests for the series registry.

These exist to prove one thing: **binding MotoGP's vocabulary from
``series/motogp.py`` instead of declaring it inline in ``motogp_common.py`` did
not change a single value.** That is the whole risk of the refactor, and it is
invisible in normal use - a wrong element number only shows up as a folder Plex
cannot match months later.

So most of this file is an *equality* assertion between the registry's
materialised tables and what ``motogp_common`` exposes. If someone edits
``series/motogp.py`` and changes a number, these fail immediately rather than
silently renaming a library.

The rest pin the invariants that make a second series addable at all: a series
is data, the numbers are permanent, and nothing may be guessed.
"""

import pytest

import motogp_common as common
from series import (
    all_series,
    default_series,
    get_series,
    runtime,
    series_keys,
)


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------

def test_motogp_is_registered():
    assert "motogp" in series_keys()


def test_motogp_is_the_default_series():
    """
    Adding a series must never become the default by being added.

    MotoGP is the default because it is the series this app was built for and
    the only one with media on disk. A new series that silently became the
    default would repoint every existing install.
    """
    assert default_series().key == "motogp"


def test_an_unknown_series_names_the_registered_ones():
    with pytest.raises(KeyError, match="unknown series"):
        get_series("formula1")


def test_the_runtime_is_built_once():
    """
    Building twice would give two different SessionType classes for the same
    series, and equality between them would fail in ways that look like a
    matching bug.
    """
    assert runtime("motogp") is runtime("motogp")


def test_the_runtime_is_the_same_objects_the_spine_imports():
    mgp = runtime("motogp")
    assert common.SessionType is mgp.session_type
    assert common.RaceClass is mgp.race_class


# ---------------------------------------------------------------------------
# Every table the spine binds is exactly what the registry materialised
# ---------------------------------------------------------------------------

BOUND_TABLES = [
    "session_elements",
    "legacy_session_elements",
    "session_display_names",
    "session_short_names",
    "session_race_class",
    "core_sessions",
    "feature_race_sessions",
    "default_desired_sessions",
    "calendar_key_to_session",
    "session_to_calendar_key",
    "calendar_key_to_day",
    "session_to_format_name",
    "format_name_to_session",
]


@pytest.mark.parametrize("table", BOUND_TABLES)
def test_the_spine_table_matches_the_registry(table):
    mgp = runtime("motogp")
    assert getattr(common, {
        "session_elements": "SESSION_ELEMENTS",
        "legacy_session_elements": "SESSION_ELEMENTS_LEGACY",
        "session_display_names": "SESSION_DISPLAY_NAMES",
        "session_short_names": "SESSION_SHORT_NAMES",
        "session_race_class": "SESSION_RACE_CLASS",
        "core_sessions": "CORE_SESSIONS",
        "feature_race_sessions": "FEATURE_RACE_SESSIONS",
        "default_desired_sessions": "DEFAULT_DESIRED_SESSIONS",
        "calendar_key_to_session": "CALENDAR_KEY_TO_SESSION",
        "session_to_calendar_key": "SESSION_TO_CALENDAR_KEY",
        "calendar_key_to_day": "CALENDAR_KEY_TO_DAY",
        "session_to_format_name": "SESSION_TO_MOTOGP_SESSION_NAME",
        "format_name_to_session": "MOTOGP_SESSION_NAME_TO_TYPE",
    }[table]) == getattr(mgp, table)


def test_asset_dirs_are_bound_and_resolve_under_the_repo_assets():
    mgp = runtime("motogp")
    assert common.CLASS_ASSET_DIRS == mgp.asset_dirs
    for class_key, dirs in mgp.asset_dirs.items():
        assert set(dirs) == {"fanart", "poster"}, (class_key, sorted(dirs))
        for kind, path in dirs.items():
            assert path.parent == common.ASSETS_DIR, (class_key, kind)
            # One directory per class and per kind, so two classes cannot
            # silently share (and overwrite) the same source artwork.
            assert path.name.startswith(class_key), (class_key, kind, path)


def test_the_enum_properties_agree_with_the_runtime_tables():
    """
    The properties and the runtime's tables are built separately.

    A property's ``self`` is the enum member, not the SeriesRuntime, so it cannot
    read the runtime's attributes - it closes over its own copy. That is fine,
    but it means the two must agree, and a disagreement would show up as a Plex
    title or a folder name that quietly contradicts the lookup the grabber uses.
    This walks every member so they cannot drift apart unnoticed.
    """
    mgp = runtime("motogp")
    st = mgp.session_type
    for session in st:
        assert session.display_name == mgp.session_display_names.get(session, "Unknown")
        assert session.short_name == mgp.session_short_names.get(session, "Unknown")
        assert session.full_display_name == (
            f"{session.race_class.display_name} {session.display_name}"
            if session.race_class else session.display_name
        )
        assert session.full_short_name == (
            f"{session.race_class.display_name} {session.short_name}"
            if session.race_class else session.short_name
        )
        assert session.race_class == mgp.session_race_class.get(session)
        assert session.is_core_session == (session in mgp.core_sessions)
        assert session.is_feature_race == (session in mgp.feature_race_sessions)
        assert session.calendar_key == mgp.session_to_calendar_key.get(session)
        assert session.format_session_name == mgp.session_to_format_name.get(session)
        # The legacy alias must not fall behind the generic name.
        assert session.motogp_session_name == session.format_session_name


def test_the_pattern_table_is_bound_with_its_priorities():
    """A dropped priority would reorder the matcher, not break it."""
    mgp = runtime("motogp")
    assert len(common.SESSION_PATTERNS) == len(mgp.session_patterns)
    for bound, spec in zip(common.SESSION_PATTERNS, mgp.session_patterns):
        assert bound.session_type is spec.session_type
        assert bound.must_have == list(spec.must_have)
        assert bound.must_not_have == list(spec.must_not_have)
        assert bound.priority == spec.priority


# ---------------------------------------------------------------------------
# The numbering invariants (the ones that orphan files if broken)
# ---------------------------------------------------------------------------

def test_every_session_has_an_element_number():
    mgp = runtime("motogp")
    missing = [s for s in mgp.session_type
               if s != mgp.session_type.UNKNOWN
               and s not in mgp.session_elements]
    assert not missing, f"sessions with no element number: {missing}"


def test_element_numbers_are_unique():
    mgp = runtime("motogp")
    numbers = list(mgp.session_elements.values())
    assert len(numbers) == len(set(numbers)), "two sessions share one number"


def test_element_numbers_leave_room_for_a_new_session():
    """
    The gap between blocks is the whole point.

    The dense 1-24 table was full, so 2027's FP3 and Sprint Qualifying had to be
    appended *after* the Race and a 2027 round listed out of order in every
    folder, title and CLI listing. A table with no free slot reproduces that.
    """
    mgp = runtime("motogp")
    used = set(mgp.session_elements.values())
    # Every session sits in a block of ten, so each block must have a free slot.
    blocks = {}
    for number in used:
        blocks.setdefault(number // 10, []).append(number)
    for block, numbers in blocks.items():
        assert len(numbers) < 10, f"block {block * 10}s is full: {sorted(numbers)}"


def test_element_numbers_are_chronological_for_a_2027_weekend():
    """
    Ordering is a property of the identifier, not something patched in the UI.

    A 2027 round runs FP1, FP2, Sprint Qualifying, FP3, Qualifying, Sprint,
    Race. If the numbers do not ascend in that order the round lists out of
    order everywhere it is shown.
    """
    mgp = runtime("motogp")
    st = mgp.session_type
    order_2027 = [
        st.MOTOGP_FP1, st.MOTOGP_FP2, st.MOTOGP_SPRINT_QUALIFYING,
        st.MOTOGP_FP3, st.MOTOGP_Q2, st.MOTOGP_SPRINT, st.MOTOGP_RACE,
    ]
    numbers = [mgp.element_number(s, 2027) for s in order_2027]
    assert numbers == sorted(numbers), f"2027 weekend is out of order: {numbers}"
    assert len(set(numbers)) == len(numbers)


def test_element_numbers_are_chronological_for_a_2026_weekend():
    mgp = runtime("motogp")
    st = mgp.session_type
    order_2026 = [
        st.MOTOGP_FP1, st.MOTOGP_PRACTICE, st.MOTOGP_FP2,
        st.MOTOGP_Q2, st.MOTOGP_SPRINT, st.MOTOGP_RACE,
    ]
    numbers = [mgp.element_number(s, 2026) for s in order_2026]
    assert numbers == sorted(numbers), f"2026 weekend is out of order: {numbers}"


def test_the_legacy_table_is_frozen():
    """
    It exists so folders an older version wrote are still READ. Writing a new
    file with it would resurrect a scheme the app deliberately moved off.
    """
    mgp = runtime("motogp")
    assert mgp.legacy_session_elements, "the legacy table must not be emptied"
    # Every legacy entry must be reachable: an entry naming a member that does
    # not exist would silently stop being read.
    for member in mgp.legacy_session_elements:
        assert isinstance(member, mgp.session_type)


def test_the_cutover_season_decides_which_scheme_applies():
    mgp = runtime("motogp")
    st = mgp.session_type
    assert mgp.legacy_element_season == 2025
    # 2025 and earlier keep the dense value; nothing on disk changes.
    assert mgp.element_number(st.MOTOGP_RACE, 2025) == 21
    assert mgp.element_number(st.MOTOGP_RACE, 2024) == 21
    # 2026 onwards use the spaced scheme.
    assert mgp.element_number(st.MOTOGP_RACE, 2026) == 82


def test_an_ambiguous_number_is_refused_rather_than_guessed():
    """
    E06 is MotoGP Practice in the legacy scheme and nothing in the new one.

    Reading a 2026 Race folder as a practice session is silent corruption, not a
    wrong label, so with no season the honest answer is None.
    """
    mgp = runtime("motogp")
    st = mgp.session_type
    assert mgp.element_number(st.MOTOGP_PRACTICE, 2025) == 6
    assert mgp.get_session_from_element(6, season=2025) is st.MOTOGP_PRACTICE
    assert mgp.get_session_from_element(6, season=2026) is None
    assert mgp.get_session_from_element(6) is st.MOTOGP_PRACTICE
    assert mgp.element_is_ambiguous(6) is False


def test_a_number_used_by_both_schemes_for_different_sessions_is_ambiguous():
    """
    The reason the season is a required argument to find_legacy_folders().
    """
    mgp = runtime("motogp")
    # A number the legacy and current schemes give to different sessions in the legacy scheme. Find a number the new scheme
    # gives to a different session, so the two schemes genuinely disagree.
    legacy = {n: s for s, n in mgp.legacy_session_elements.items()}
    current = {n: s for s, n in mgp.session_elements.items()}
    disagreeing = [
        n for n, s in legacy.items() if n in current and current[n] is not s
    ]
    assert disagreeing, (
        "the two schemes must disagree somewhere, or the cutover was pointless"
    )
    for number in disagreeing:
        assert mgp.element_is_ambiguous(number) is True
        assert mgp.get_session_from_element(number) is None


def test_prefix_round_trips_for_every_session_and_season():
    mgp = runtime("motogp")
    for season in (2025, 2026, 2027):
        for session in mgp.session_type:
            if session == mgp.session_type.UNKNOWN:
                continue
            prefix = mgp.build_prefix(season, 7, session)
            parsed = mgp.parse_prefix(prefix)
            assert parsed == (season, 7, session), (season, session, prefix)


def test_a_prefix_with_no_known_element_is_not_parsed():
    mgp = runtime("motogp")
    # E99 is UNKNOWN and is deliberately not in the element table.
    assert mgp.parse_prefix("26R07E99") is None


def test_state_files_are_namespaced_per_series():
    """
    Both apps wrote grabber_state.json and webui_settings.json into the same
    /config. Merged, they would overwrite each other's entries within a day, so
    the filename carries the series.
    """
    mgp = runtime("motogp")
    assert mgp.state_file("grabber_state") == "grabber_state.motogp.json"
    assert mgp.state_file("webui_settings") == "webui_settings.motogp.json"
    # And two series must never collide.
    other = runtime("motogp")
    assert other.state_file("grabber_state") == mgp.state_file("grabber_state")


def test_race_class_from_string_matches_the_original_substring_rules():
    """
    `RaceClass.from_string` was accidentally dropped when the enums moved into
    the registry. It is called by motogp_webui._describe_calendar_session_key
    and had no test, so it failed silently - which is the whole reason this file
    exists.

    These cases are the ones where the rules are subtle: the tokens are
    substring tests on text with spaces and hyphens (but not dots) removed, and
    declaration order is the precedence order.
    """
    mgp = runtime("motogp")
    rc = mgp.race_class
    assert rc.from_string("moto3") is rc.MOTO3
    assert rc.from_string("Moto3") is rc.MOTO3
    assert rc.from_string("moto 3") is rc.MOTO3
    assert rc.from_string("moto-3") is rc.MOTO3
    assert rc.from_string("MOTO2") is rc.MOTO2
    assert rc.from_string("moto 2") is rc.MOTO2
    assert rc.from_string("motogp") is rc.MOTOGP
    assert rc.from_string("MotoGP") is rc.MOTOGP
    # "moto3" contains "moto", so the more specific class must be tried first.
    assert rc.from_string("moto2moto3") is rc.MOTO3
    assert rc.from_string("moto3moto2") is rc.MOTO3
    # Dots are not stripped, exactly as before.
    assert rc.from_string("moto.3") is None
    # No class token at all.
    assert rc.from_string("gp") is None
    assert rc.from_string("") is None


def test_the_series_metadata_is_what_the_spine_needs():
    mgp = get_series("motogp")
    assert mgp.key == "motogp"
    assert mgp.calendar_prefix == "motogp"
    assert mgp.state_prefix == "motogp"
    assert mgp.search_prefix == "MotoGP"
    assert [c.key for c in mgp.classes] == ["moto3", "moto2", "motogp"]
    # A series with no classes would need a special case in every folder name,
    # Plex title and artwork lookup.
    assert mgp.classes


def test_every_declared_member_exists_in_the_enum():
    """
    A typo in a declaration is a KeyError at import time, which is loud - but
    only if something reads it. This walks every declared name so an unused one
    still fails.
    """
    mgp = runtime("motogp")
    st = mgp.session_type
    names = set(st.__members__)
    for name in mgp.series.session_display_names:
        assert name in names, name
    for name in mgp.series.session_short_names:
        assert name in names, name
    for name in mgp.series.session_race_class:
        assert name in names, name
    for name in mgp.series.core_sessions:
        assert name in names, name
    for name in mgp.series.feature_race_sessions:
        assert name in names, name
    for name in mgp.series.default_desired_sessions:
        assert name in names, name
    for name in mgp.series.session_elements:
        assert name in names, name
    for name in mgp.series.legacy_session_elements:
        assert name in names, name
    for spec in mgp.series.session_patterns:
        assert spec.session in names, spec.session


def test_the_calendar_prefix_matches_the_calendars_on_disk():
    """
    The registry names the calendar files. If it names a prefix no calendar
    uses, the series loads nothing and every session looks missing.
    """
    from pathlib import Path

    mgp = get_series("motogp")
    config_dir = Path(common.get_config_dir())
    if not config_dir.exists():
        pytest.skip("no config directory in this environment")
    on_disk = sorted(
        p.name for p in config_dir.glob(f"{mgp.calendar_prefix}_*.json")
    )
    assert on_disk, f"no {mgp.calendar_prefix}_*.json calendars in {config_dir}"


def test_a_series_with_no_classes_is_refused():
    """
    Declaring zero classes is the one shortcut that would force a special case
    into shared code, which is how a hybrid turns back into two apps.
    """
    from series.base import Series, build

    empty = Series(
        key="empty", display_name="Empty", calendar_prefix="empty",
        log_name="empty", state_prefix="empty", search_prefix="Empty",
    )
    with pytest.raises(ValueError, match="declares no classes"):
        build(empty)


def test_registering_two_defaults_is_refused():
    from series.base import ClassSpec, Series, register

    other = Series(
        key="other", display_name="Other", calendar_prefix="other",
        log_name="other", state_prefix="other", search_prefix="Other",
        classes=(ClassSpec("other", "Other", 1),),
        session_members=(("RACE", 1), ("UNKNOWN", 99)),
    )
    with pytest.raises(ValueError, match="already is"):
        register(other, default=True)


def test_the_registry_lists_every_registered_series():
    assert set(series_keys()) == {s.key for s in all_series()}


# ---------------------------------------------------------------------------
# Series identity travels with the enum
# ---------------------------------------------------------------------------
#
# Added with the F1 calendars. Shared code receives a session from *either*
# series and has to ask questions about the series it came from; comparing the
# member's class against a hardcoded MotoGP enum was the wrong answer, and it
# failed silently.

def test_each_session_enum_knows_its_own_series():
    for spec in all_series():
        assert runtime(spec.key).session_type.series_key == spec.key, spec.key


def test_the_feature_class_is_the_last_declared_class():
    """
    Declared order is the weekend running order, so the feature class is the
    last one. MotoGP declares Moto3, Moto2, MotoGP; F1 declares only F1.
    """
    for spec in all_series():
        rnd = runtime(spec.key)
        expected = max(spec.classes, key=lambda c: c.order)
        assert rnd.feature_class.folder_key == expected.key, spec.key
        assert rnd.session_type.feature_class() == rnd.feature_class, spec.key


def test_a_foreign_session_is_never_equal_to_the_feature_class():
    """
    The bug this replaced: ``session_class != RaceClass.MOTOGP`` is True for
    every F1 session, because they are different enum classes - so F1's
    weekend-format gate was skipped entirely and every session looked runnable.
    """
    mgp = runtime("motogp")
    f1 = runtime("f1")
    assert f1.session_type.RACE.race_class != mgp.feature_class
    assert f1.session_type.RACE.race_class == f1.session_type.feature_class()
    assert mgp.session_type.MOTO3_RACE.race_class != mgp.session_type.feature_class()
    assert mgp.session_type.MOTOGP_RACE.race_class == mgp.session_type.feature_class()


def test_a_refused_registration_leaves_nothing_behind():
    """
    ``register()`` validates before it mutates.

    It used to insert the declaration first and refuse the default second, so a
    refused call left a half-registered series behind. Nothing failed at the
    call site - every later ``all_series()`` walk simply picked up a series
    that had never been materialised, and the error appeared somewhere else.
    """
    from series.base import ClassSpec, Series, register

    before = series_keys()
    candidate = Series(
        key="should_not_survive", display_name="Nope",
        calendar_prefix="should_not_survive", log_name="nope",
        state_prefix="nope", search_prefix="Nope",
        classes=(ClassSpec("nope", "Nope", 1),),
        session_members=(("RACE", 1),),
    )
    with pytest.raises(ValueError, match="already is"):
        register(candidate, default=True)
    assert series_keys() == before
    with pytest.raises(KeyError, match="should_not_survive|unknown series"):
        get_series("should_not_survive")
