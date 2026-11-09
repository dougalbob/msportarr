"""
The Formula 1 series.

Moved out of ``f1arr`` (https://github.com/dougalbob/f1arr) the same way
``motogp.py`` was moved out of ``motogp_common.py``: the tables are here, the
algorithms are not. Where a table had to be *changed* rather than copied, the
change is called out in a comment saying what it was and why, because a
silent edit to a vocabulary is how a port turns into a rewrite.

Three things about F1 are different from MotoGP and are load-bearing:

1. **One class, not three.** F1 declares a single class rather than none, so a
   folder name, a Plex title and an artwork lookup never need a "no class"
   special case. ``26R01E30 F1 FP1`` has the same shape as
   ``26R07E82 MotoGP Race``.

2. **A support-show family MotoGP has no concept of.** Ted's Notebook, The
   Paddock Show, Team Principals and the rest are real releases with real
   fans, and f1arr matched them with a priority-ordered pattern table. Sixteen
   of them, against MotoGP's one - which is why F1's element table does not
   have the same shape as MotoGP's, and why pretending otherwise produced a
   table that ran off the end of the prefix. See the comment on
   ``session_elements``.

3. **No legacy numbering, and no collisions with the superseded one.** Every F1
   folder f1arr wrote carries f1arr's dense 1-25 number in its name. Rather
   than freeze that table and cut over at a season boundary, F1 declares
   ``legacy_session_elements={}`` and ``legacy_element_season=None``, so no
   number can ever mean two things and ``parse_prefix`` never has to guess. The
   cost is paid up front instead: the new table avoids 4-29 entirely, so an old
   folder stops resolving and ``find_legacy_folders`` reports it. That is the
   re-fetch decision - and it is also the only safe one, because a number that
   is still *valid* under the new scheme but means something else would be
   misread silently, which is far worse than not being read at all.
"""

from series.base import ClassSpec, SessionPatternSpec, Series, register

F1 = register(
    Series(
        key="f1",
        display_name="Formula 1",
        calendar_prefix="f1",
        log_name="f1",
        state_prefix="f1",
        # The word releases actually carry. f1arr searched "Formula1 <season>
        # <venue>" first and "F1 <season> <venue>" second; the primary is
        # declared here and the alias list stays in the grabber.
        search_prefix="Formula1",
        # f1arr's own port. The combined app is one container on one port, so
        # this is the series' historical value for reference, not the port it
        # will be served on.
        webui_port=5678,

        # ------------------------------------------------------------------
        # Classes.
        #
        # One class. The tokens are substring tests against text with spaces
        # and hyphens removed, so "Formula 1 2026" and "Formula1 2026" both
        # normalise to something containing "formula1"; a bare "F1 2026"
        # contains "f1". Both are declared so neither spelling is missed.
        # ------------------------------------------------------------------
        classes=(
            ClassSpec("f1", "F1", 1, name_tokens=("formula1", "f1")),
        ),

        # ------------------------------------------------------------------
        # Session vocabulary.
        #
        # The values are f1arr's original dense numbering, kept because they
        # are chronological order and nothing else - never written to disk.
        # The number that IS written comes from session_elements below.
        # ------------------------------------------------------------------
        session_members=(
            ("WEEKEND_WARMUP", 1),
            ("TEAM_PRINCIPALS", 2),
            ("DRIVER_PRESS_CONFERENCE", 3),
            ("PADDOCK_UNCUT", 4),
            ("PADDOCK_SHOW", 5),
            ("F1_SHOW", 6),
            ("PRACTICE_1", 7),
            ("PRACTICE_2", 8),
            ("PRACTICE_3", 9),
            ("SPRINT_QUALIFYING", 10),
            ("SPRINT", 11),
            ("POST_SPRINT_SHOW", 12),
            ("TEDS_SPRINT_NOTEBOOK", 13),
            ("PRE_QUALIFYING_SHOW", 14),
            ("QUALIFYING", 15),
            ("POST_QUALIFYING_SHOW", 16),
            ("TEDS_QUALIFYING_NOTEBOOK", 17),
            ("PRE_RACE_SHOW", 18),
            ("RACE", 19),
            ("RACE_UNCUT", 20),
            ("POST_RACE_SHOW", 21),
            ("TEDS_RACE_NOTEBOOK", 22),
            ("TEDS_NOTEBOOK", 23),
            ("JOLYON_PALMERS_ANALYSIS", 24),
            ("FULL_EVENT", 25),
            ("UNKNOWN", 99),
        ),

        # ------------------------------------------------------------------
        # Element numbers - the "E<NN>" in yyRNNEEE.
        #
        # RENUMBERED, not migrated. f1arr used the raw enum value (dense
        # 1-25), which left no room for a new session: the next one would
        # have to be appended after the Race and would list out of order in
        # every folder, title and CLI listing. Blocks with gaps fix that
        # permanently.
        #
        #   1-3    the pre-event shows, before the weekend starts
        #   30s FP1       40s FP2       50s FP3
        #   60s the sprint-weekend sessions, then Qualifying
        #   70s Race, and the two broadcasts that ARE the race
        #   76+   the weekend's programming, in the order a viewer watches it
        #
        # TWO HARD CONSTRAINTS shape this table, and both were learned the
        # hard way while it was being written.
        #
        # 1. **No number may collide with the old dense scheme unless it
        #    means the same session.** ``session_members`` above *is* the old
        #    table - f1arr wrote the enum value straight into the folder name,
        #    so E10 was Sprint Qualifying. A new number in 4-25 that names a
        #    different session makes ``parse_prefix`` silently misread an old
        #    folder: an existing "26R02E10 Sprint Qualifying" would come back
        #    as Practice 1, and nothing would report it. The first draft of
        #    this table did exactly that in four places. So 4-29 is left
        #    deliberately unresolvable, and only 1, 2 and 3 are reused -
        #    because they are the three pre-event shows' original numbers, so
        #    an old folder still resolves to the session it always named.
        #    ``test_no_old_number_is_silently_misread`` proves this for every
        #    number, and it is the test to run first if this table changes.
        #
        # 2. **99 is the ceiling.** ``_PREFIX_RE`` is ``(\d{2})R(\d{2})E(\d{2})``,
        #    so an element is exactly two digits. The first draft ran to 110,
        #    and "26R07E100" parsed as E10 - Practice 1 - while "E110" did
        #    not parse at all. Both failures were invisible to a round-trip
        #    test, which only compares against what ``build_prefix`` made.
        #
        # The 76+ block uses gaps of ONE because thirteen programmes do not
        # fit in 76-99 at gaps of two. Eleven slots remain free. Widening the
        # prefix to three digits would give every series more room, but it
        # changes the on-disk format for all of them, so it is not a decision
        # to take inside one series' declaration.
        #
        # The pre-event trio is separated from the rest of the programming
        # because it airs on Thursday, before anything on track - the same
        # reason MotoGP's press conference is element 1. The 76+ block is
        # ordered as a viewer watches the weekend rather than by strict air
        # time, so a Friday magazine show sorts after the Sunday race; that
        # is a deliberate trade for keeping the feature sessions in clean
        # decades, and it is why the block is described as "programming" and
        # not "support shows in air order".
        # ------------------------------------------------------------------
        session_elements={
            # The pre-event shows, at the numbers f1arr already gave them.
            "WEEKEND_WARMUP": 1,
            "TEAM_PRINCIPALS": 2,
            "DRIVER_PRESS_CONFERENCE": 3,

            "PRACTICE_1": 30,
            "PRACTICE_2": 40,
            "PRACTICE_3": 50,

            "SPRINT_QUALIFYING": 60,
            "SPRINT": 62,
            "QUALIFYING": 64,

            "RACE": 70,
            "RACE_UNCUT": 72,
            "FULL_EVENT": 74,

            "PADDOCK_UNCUT": 76,
            "PADDOCK_SHOW": 77,
            "F1_SHOW": 78,
            "POST_SPRINT_SHOW": 79,
            "TEDS_SPRINT_NOTEBOOK": 80,
            "PRE_QUALIFYING_SHOW": 81,
            "POST_QUALIFYING_SHOW": 82,
            "TEDS_QUALIFYING_NOTEBOOK": 83,
            "PRE_RACE_SHOW": 84,
            "POST_RACE_SHOW": 85,
            "TEDS_RACE_NOTEBOOK": 86,
            "TEDS_NOTEBOOK": 87,
            "JOLYON_PALMERS_ANALYSIS": 88,
        },

        # Empty on purpose. See the module docstring: F1 re-fetches rather
        # than migrating, so there is no older scheme to keep reading and no
        # season at which one stops applying.
        legacy_element_season=None,
        legacy_session_elements={},

        session_display_names={
            # f1arr called this "Unknown Session". The registry's convention,
            # set by motogp.py, is "Unknown", and it wins: a combined UI with
            # an All / MotoGP / F1 filter would otherwise show two different
            # labels for the same state, which reads as a bug rather than a
            # difference of taste.
            "UNKNOWN": "Unknown",
            "WEEKEND_WARMUP": "Weekend Warm Up",
            "TEAM_PRINCIPALS": "Team Principals",
            "DRIVER_PRESS_CONFERENCE": "Driver Press Conference",
            "PADDOCK_UNCUT": "Paddock Uncut",
            "PADDOCK_SHOW": "The Paddock Show",
            "F1_SHOW": "The F1 Show",
            "PRACTICE_1": "Practice 1",
            "PRACTICE_2": "Practice 2",
            "PRACTICE_3": "Practice 3",
            "SPRINT_QUALIFYING": "Sprint Qualifying",
            "SPRINT": "Sprint",
            "POST_SPRINT_SHOW": "Post-Sprint Show",
            "TEDS_SPRINT_NOTEBOOK": "Ted's Sprint Notebook",
            "PRE_QUALIFYING_SHOW": "Pre-Qualifying Show",
            "QUALIFYING": "Qualifying",
            "POST_QUALIFYING_SHOW": "Post-Qualifying Show",
            "TEDS_QUALIFYING_NOTEBOOK": "Ted's Qualifying Notebook",
            "PRE_RACE_SHOW": "Pre-Race Show",
            "RACE": "Race",
            "RACE_UNCUT": "Race Uncut",
            "POST_RACE_SHOW": "Post-Race Show",
            "TEDS_RACE_NOTEBOOK": "Ted's Race Notebook",
            "TEDS_NOTEBOOK": "Ted's Notebook",
            "JOLYON_PALMERS_ANALYSIS": "Jolyon Palmer's Analysis",
            "FULL_EVENT": "Full Event",
        },

        # Short labels, which the class name is prepended to for a folder
        # name: "F1 FP1", "F1 Race", "F1 Ted's Notebook". The long display
        # names repeat the class ("Driver Press Conference", "The F1 Show")
        # and would read twice over.
        session_short_names={
            "WEEKEND_WARMUP": "Weekend Warm Up",
            "TEAM_PRINCIPALS": "Team Principals",
            "DRIVER_PRESS_CONFERENCE": "Press Conference",
            "PADDOCK_UNCUT": "Paddock Uncut",
            "PADDOCK_SHOW": "Paddock Show",
            "F1_SHOW": "F1 Show",
            "PRACTICE_1": "FP1",
            "PRACTICE_2": "FP2",
            "PRACTICE_3": "FP3",
            "SPRINT_QUALIFYING": "Sprint Qualifying",
            "SPRINT": "Sprint",
            "POST_SPRINT_SHOW": "Post-Sprint Show",
            "TEDS_SPRINT_NOTEBOOK": "Ted's Sprint Notebook",
            "PRE_QUALIFYING_SHOW": "Pre-Qualifying Show",
            "QUALIFYING": "Qualifying",
            "POST_QUALIFYING_SHOW": "Post-Qualifying Show",
            "TEDS_QUALIFYING_NOTEBOOK": "Ted's Qualifying Notebook",
            "PRE_RACE_SHOW": "Pre-Race Show",
            "RACE": "Race",
            "RACE_UNCUT": "Race Uncut",
            "POST_RACE_SHOW": "Post-Race Show",
            "TEDS_RACE_NOTEBOOK": "Ted's Race Notebook",
            "TEDS_NOTEBOOK": "Ted's Notebook",
            "JOLYON_PALMERS_ANALYSIS": "Palmer's Analysis",
            "FULL_EVENT": "Full Event",
            "UNKNOWN": "Unknown",
        },

        # Every session belongs to the one class.
        session_race_class={
            "WEEKEND_WARMUP": "f1",
            "TEAM_PRINCIPALS": "f1",
            "DRIVER_PRESS_CONFERENCE": "f1",
            "PADDOCK_UNCUT": "f1",
            "PADDOCK_SHOW": "f1",
            "F1_SHOW": "f1",
            "PRACTICE_1": "f1",
            "PRACTICE_2": "f1",
            "PRACTICE_3": "f1",
            "SPRINT_QUALIFYING": "f1",
            "SPRINT": "f1",
            "POST_SPRINT_SHOW": "f1",
            "TEDS_SPRINT_NOTEBOOK": "f1",
            "PRE_QUALIFYING_SHOW": "f1",
            "QUALIFYING": "f1",
            "POST_QUALIFYING_SHOW": "f1",
            "TEDS_QUALIFYING_NOTEBOOK": "f1",
            "PRE_RACE_SHOW": "f1",
            "RACE": "f1",
            "RACE_UNCUT": "f1",
            "POST_RACE_SHOW": "f1",
            "TEDS_RACE_NOTEBOOK": "f1",
            "TEDS_NOTEBOOK": "f1",
            "JOLYON_PALMERS_ANALYSIS": "f1",
            "FULL_EVENT": "f1",
        },

        # f1arr's CORE_SESSIONS, verbatim: the sessions that define a
        # weekend and are wanted by default.
        core_sessions=(
            "PRACTICE_1", "PRACTICE_2", "PRACTICE_3",
            "SPRINT_QUALIFYING", "SPRINT",
            "QUALIFYING", "RACE",
        ),

        # The feature race, and only it. RACE_UNCUT and FULL_EVENT are the
        # same session in a longer or shorter form, not a second race.
        feature_race_sessions=("RACE",),

        default_desired_sessions={
            "WEEKEND_WARMUP": False,
            "TEAM_PRINCIPALS": False,
            "DRIVER_PRESS_CONFERENCE": True,
            "PADDOCK_UNCUT": False,
            "PADDOCK_SHOW": True,
            "F1_SHOW": False,
            "PRACTICE_1": True,
            "PRACTICE_2": True,
            "PRACTICE_3": True,
            "SPRINT_QUALIFYING": True,
            "SPRINT": True,
            "POST_SPRINT_SHOW": False,
            "TEDS_SPRINT_NOTEBOOK": False,
            "PRE_QUALIFYING_SHOW": False,
            "QUALIFYING": True,
            "POST_QUALIFYING_SHOW": False,
            "TEDS_QUALIFYING_NOTEBOOK": False,
            "PRE_RACE_SHOW": True,
            "RACE": True,
            "RACE_UNCUT": False,
            "POST_RACE_SHOW": False,
            "TEDS_RACE_NOTEBOOK": False,
            "TEDS_NOTEBOOK": True,
            "JOLYON_PALMERS_ANALYSIS": True,
            "FULL_EVENT": False,
        },

        # ------------------------------------------------------------------
        # Release-title patterns, most specific first.
        #
        # The priorities are f1arr's, unchanged. Two rules they encode:
        #
        #   * The Ted's Notebook family outranks everything, because "Ted's
        #     Race Notebook" contains "race" and would otherwise be filed as
        #     the Grand Prix.
        #   * "Post-Race Show" must beat "Race", and does so on priority
        #     alone - it also carries "race" in must_not_have of the RACE
        #     pattern as a second line of defence.
        #
        # The must_have lists gain dotted variants ("ted's.notebook") that
        # f1arr did not need: f1arr's classify_session() rewrites [.\-_]+ to
        # spaces before matching, and MotoGP's does not, so a pattern with
        # only the spaced form is unreachable under one of the two
        # normalisations. The shared spine will pick one rule; until it
        # does, the data works under either.
        # ------------------------------------------------------------------
        session_patterns=(
            SessionPatternSpec(
                "FULL_EVENT",
                ("full event", "full.event"),
                (), 115,
            ),
            SessionPatternSpec(
                "TEDS_SPRINT_NOTEBOOK",
                ("teds sprint notebook", "teds sprint race notebook",
                 "ted's sprint notebook", "ted's sprint race notebook",
                 "teds.sprint.notebook", "teds.sprint.race.notebook",
                 "ted's.sprint.notebook", "ted's.sprint.race.notebook"),
                (), 110,
            ),
            SessionPatternSpec(
                "TEDS_QUALIFYING_NOTEBOOK",
                ("teds qualifying notebook", "ted's qualifying notebook",
                 "teds.qualifying.notebook", "ted's.qualifying.notebook"),
                ("sprint",), 109,
            ),
            SessionPatternSpec(
                "TEDS_RACE_NOTEBOOK",
                ("teds race notebook", "ted's race notebook",
                 "teds.race.notebook", "ted's.race.notebook"),
                ("sprint",), 108,
            ),
            SessionPatternSpec(
                "TEDS_NOTEBOOK",
                ("teds notebook", "ted's notebook",
                 "teds.notebook", "ted's.notebook"),
                ("qualifying", "race", "sprint"), 107,
            ),
            SessionPatternSpec(
                "JOLYON_PALMERS_ANALYSIS",
                ("jolyon palmer", "palmer's analysis", "palmers analysis",
                 "jolyon.palmer", "palmer's.analysis", "palmers.analysis"),
                (), 105,
            ),
            SessionPatternSpec(
                "POST_RACE_SHOW",
                ("post race show", "post-race show", "post.race.show"),
                (), 100,
            ),
            SessionPatternSpec(
                "PRE_RACE_SHOW",
                ("pre race show", "pre-race show", "pre.race.show"),
                (), 100,
            ),
            SessionPatternSpec(
                "POST_QUALIFYING_SHOW",
                ("post qualifying show", "post-qualifying show",
                 "post.qualifying.show"),
                (), 100,
            ),
            SessionPatternSpec(
                "PRE_QUALIFYING_SHOW",
                ("pre qualifying show", "pre-qualifying show",
                 "pre.qualifying.show"),
                (), 100,
            ),
            SessionPatternSpec(
                "POST_SPRINT_SHOW",
                ("post sprint show", "post-sprint show", "post.sprint.show"),
                (), 100,
            ),
            SessionPatternSpec(
                "PADDOCK_UNCUT",
                ("paddock uncut", "paddock.uncut"),
                (), 95,
            ),
            SessionPatternSpec(
                "PADDOCK_SHOW",
                ("paddock show", "paddock.show"),
                ("uncut",), 94,
            ),
            SessionPatternSpec(
                "F1_SHOW",
                ("f1 show", "the f1 show", "f1 show preview",
                 "f1.show", "the.f1.show"),
                ("paddock",), 93,
            ),
            SessionPatternSpec(
                "DRIVER_PRESS_CONFERENCE",
                ("drivers press conference", "driver press conference",
                 "drivers press", "press conference",
                 "drivers.press.conference", "driver.press.conference",
                 "drivers.press", "press.conference"),
                ("team principal",), 90,
            ),
            SessionPatternSpec(
                "TEAM_PRINCIPALS",
                ("team principals", "team principal",
                 "team.principals", "team.principal"),
                (), 90,
            ),
            SessionPatternSpec(
                "WEEKEND_WARMUP",
                ("weekend warm-up", "weekend warm up", "weekend warmup",
                 "weekend.warm-up", "weekend.warm.up", "weekend.warmup"),
                (), 85,
            ),
            SessionPatternSpec(
                "SPRINT_QUALIFYING",
                ("sprint qualifying", "sprint shootout",
                 "sprint.qualifying", "sprint.shootout"),
                ("post", "pre", "notebook"), 80,
            ),
            SessionPatternSpec(
                "SPRINT",
                ("sprint race", "sprint", "sprint.race"),
                ("qualifying", "shootout", "pre-sprint", "post-sprint",
                 "pre sprint", "post sprint", "notebook"), 70,
            ),
            SessionPatternSpec(
                "QUALIFYING",
                ("qualifying", "quali"),
                ("sprint", "pre-qualifying", "pre qualifying",
                 "post-qualifying", "post qualifying", "notebook"), 60,
            ),
            SessionPatternSpec(
                "PRACTICE_1",
                ("fp1", "practice 1", "practice one", "free practice 1",
                 "practice.1", "practice.one", "free.practice.1"),
                ("fp2", "fp3", "practice 2", "practice 3",
                 "practice two", "practice three"), 55,
            ),
            SessionPatternSpec(
                "PRACTICE_2",
                ("fp2", "practice 2", "practice two", "free practice 2",
                 "practice.2", "practice.two", "free.practice.2"),
                ("fp1", "fp3", "practice 1", "practice 3",
                 "practice one", "practice three"), 55,
            ),
            SessionPatternSpec(
                "PRACTICE_3",
                ("fp3", "practice 3", "practice three", "free practice 3",
                 "practice.3", "practice.three", "free.practice.3"),
                ("fp1", "fp2", "practice 1", "practice 2",
                 "practice one", "practice two"), 55,
            ),
            SessionPatternSpec(
                "RACE_UNCUT",
                ("uncut",),
                ("paddock",), 45,
            ),
            SessionPatternSpec(
                "RACE",
                ("race",),
                ("sprint", "pre-race", "post-race", "pre race", "post race",
                 "notebook", "uncut"), 40,
            ),
        ),

        # Calendar session keys. Unlike MotoGP's they carry no class prefix:
        # there is one class, so "fp1" is unambiguous, and the key is what
        # the calendar JSON already uses.
        # Calendar keys are CLASS-PREFIXED, exactly as MotoGP's are
        # ("motogp_fp1", "moto3_race"). f1arr's calendars used bare "fp1"
        # because there was only ever one class; a calendar key is resolved
        # through this table and its display label is derived from the part
        # after the first underscore, so prefixing costs nothing and buys two
        # things: one key namespace across both series in a combined UI, and
        # room for a support series at an F1 weekend without renaming every
        # key already written into a calendar.
        calendar_key_to_session={
            "f1_fp1": "PRACTICE_1",
            "f1_fp2": "PRACTICE_2",
            "f1_fp3": "PRACTICE_3",
            "f1_sprint_qualifying": "SPRINT_QUALIFYING",
            "f1_sprint": "SPRINT",
            "f1_qualifying": "QUALIFYING",
            "f1_race": "RACE",
        },

        session_to_calendar_key={
            "PRACTICE_1": "f1_fp1",
            "PRACTICE_2": "f1_fp2",
            "PRACTICE_3": "f1_fp3",
            "SPRINT_QUALIFYING": "f1_sprint_qualifying",
            "SPRINT": "f1_sprint",
            "QUALIFYING": "f1_qualifying",
            "RACE": "f1_race",
        },

        # A fallback only: a round's own sessions block is authoritative
        # about which day a key runs.
        calendar_key_to_day={
            "f1_fp1": "friday",
            "f1_fp2": "friday",
            "f1_fp3": "saturday",
            "f1_sprint_qualifying": "friday",
            "f1_sprint": "saturday",
            "f1_qualifying": "saturday",
            "f1_race": "sunday",
        },

        # Map session types to weekend-format session names. In F1 these are
        # the same seven keys the calendar uses, so this table is the
        # identity on the core sessions - it exists so the shared spine has
        # one name to ask for rather than two.
        session_to_format_name={
            "PRACTICE_1": "fp1",
            "PRACTICE_2": "fp2",
            "PRACTICE_3": "fp3",
            "SPRINT_QUALIFYING": "sprint_qualifying",
            "SPRINT": "sprint",
            "QUALIFYING": "qualifying",
            "RACE": "race",
        },

        format_name_to_session={
            "fp1": "PRACTICE_1",
            "fp2": "PRACTICE_2",
            "fp3": "PRACTICE_3",
            "sprint_qualifying": "SPRINT_QUALIFYING",
            "sprint": "SPRINT",
            "qualifying": "QUALIFYING",
            "race": "RACE",
        },

        # ------------------------------------------------------------------
        # Titles that belong to a different championship entirely.
        #
        # f1arr hardcoded this list inside classify_session() as
        # ``global_exclusions``. It is data, not an algorithm, so it lives
        # here - and it is per-series, which matters: "f2" is a junior
        # series to exclude for F1 and meaningless to MotoGP.
        #
        # Note the hazard this list carries. These are SUBSTRING tests, and
        # "f2" and "f3" are two characters. A normalised title containing
        # "f2" anywhere is rejected. That is correct for "Formula2 2026
        # Barcelona" and would also reject a title that happened to contain
        # those two characters adjacently for any other reason. f1arr has
        # run this list in production without a false positive, so it is
        # ported as-is rather than "improved" - but the next person to touch
        # it should know it is a sharp edge.
        #
        # The dotted variants are included because the two normalisations
        # disagree about dots, as above.
        # ------------------------------------------------------------------
        excluded_terms=(
            "academy",
            "f2", "formula 2", "formula.2",
            "f3", "formula 3", "formula.3",
            "porsche supercup", "porsche.supercup",
        ),

        # f1arr's own artwork directories, named as it named them. The JPEGs
        # themselves are ported in their own change - 86 MB of per-venue
        # art is a packaging decision, not a vocabulary one - so this
        # declares where they will live, exactly as calendar_prefix declares
        # where the calendars will live.
        asset_dirs={
            "f1": {"fanart": "f1_fanart", "poster": "f1_posters"},
        },
    ),
    # Not the default. MotoGP stays the default because it is the series this
    # app was built for and the only one with media already on disk; adding a
    # series must never silently repoint an existing install.
    default=False,
)
