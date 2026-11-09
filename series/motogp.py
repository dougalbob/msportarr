"""
The MotoGP series: MotoGP / Moto2 / Moto3.

Every table here was moved verbatim out of ``motogp_common.py``. Nothing in this
file computes anything - if a value needs deriving, that derivation belongs in
the spine, not in a declaration, because the whole point of the split is that a
series says *what* its vocabulary is and the shared code decides *how* to use it.

Two invariants are load-bearing and are enforced by
``tests/test_series_registry.py``:

1. **The element numbers are permanent.** They are baked into folder names, file
   names and Plex identity, so renumbering a live season renames files and Plex
   treats them as new items. They are allocated in blocks of ten so a new
   session takes a free number instead of being appended after the Race.
   ``SESSION_ELEMENTS_LEGACY`` is the frozen dense table that earlier versions
   wrote; it exists so those folders are still *read*, and is never written.
2. **The session enum values are not identity.** They are chronological order
   and nothing else. ``element_number()`` is the only thing that decides what
   goes in a folder name, and ``int(session_type)`` is a fallback inside it.
"""

from series.base import ClassSpec, SessionPatternSpec, Series, register

MOTOGP = register(
    Series(
        key="motogp",
        display_name="MotoGP",
        calendar_prefix="motogp",
        log_name="motogp",
        state_prefix="motogp",
        search_prefix="MotoGP",
        webui_port=5679,

        # ------------------------------------------------------------------
        # Where its media lives.
        #
        # These are the values this app has used since it was MotoGP-only, so
        # an existing install that never edited settings.yml gets exactly the
        # paths it had. The Plex library *id* is None because Plex assigns it -
        # there is nothing to default to, and a wrong guess would tag the wrong
        # library rather than fail.
        # ------------------------------------------------------------------
        library_path="/media/MotoGP",
        downloads_path="/downloads/complete/motogp",
        sabnzbd_category="motogp",
        plex_library_name="Motorsport - MotoGP",

        # ------------------------------------------------------------------
        # Classes. Order is the weekend running order: Moto3, then Moto2,
        # then the feature class.
        # ------------------------------------------------------------------
        # Declaration order is the precedence order for from_string(): the
        # tokens are substring tests, so "moto3" must be tried before "moto2"
        # and both before "motogp".
        classes=(
            ClassSpec("moto3", "Moto3", 1, name_tokens=("moto3",)),
            ClassSpec("moto2", "Moto2", 2, name_tokens=("moto2",)),
            ClassSpec("motogp", "MotoGP", 3, name_tokens=("motogp",)),
        ),

        # ------------------------------------------------------------------
        # Session vocabulary.
        #
        # The values are HISTORICAL ONLY - they are the pre-2027 numbering and
        # are kept because they are what already-existing folders on disk were
        # named with. Do not add members; the number written into a folder name
        # comes from session_elements below.
        # ------------------------------------------------------------------
        session_members=(
            ("MOTOGP_PRESS_CONFERENCE", 1),
            ("MOTO3_FP1", 2),
            ("MOTOGP_FP1", 3),
            ("MOTO2_FP1", 4),
            ("MOTO3_PRACTICE", 5),
            ("MOTOGP_PRACTICE", 6),
            ("MOTO2_PRACTICE", 7),
            ("MOTO3_FP2", 8),
            ("MOTOGP_FP2", 9),
            ("MOTO2_FP2", 10),
            ("MOTO3_Q1", 11),
            ("MOTO3_Q2", 12),
            ("MOTOGP_Q1", 13),
            ("MOTOGP_Q2", 14),
            ("MOTOGP_SPRINT", 15),
            ("MOTO2_Q1", 16),
            ("MOTO2_Q2", 17),
            ("MOTOGP_WARMUP", 18),
            ("MOTO3_RACE", 19),
            ("MOTO2_RACE", 20),
            ("MOTOGP_RACE", 21),
            ("MOTOGP_CHEQUERED_FLAG", 22),
            # Appended for the 2027 weekend format; never had a pre-2027 value.
            ("MOTOGP_FP3", 23),
            ("MOTOGP_SPRINT_QUALIFYING", 24),
            ("UNKNOWN", 99),
        ),

        # ------------------------------------------------------------------
        # Element numbers - the "E<NN>" in yyRNNEEE. Blocks of ten with gaps:
        #   10s FP1      20s Practice   30s FP2, Sprint Qualifying
        #   40s FP3      50s Q1         60s Q2
        #   70s Sprint   78 Warm-up     80s Races   90 Chequered
        #
        # Two rules this table satisfies:
        #   1. It is CHRONOLOGICAL for every weekend format the app
        #      understands. The old dense table could not be: it was full, so
        #      FP3 had to be appended after the Race and a 2027 round listed
        #      FP3 and Sprint Qualifying *after* the Grand Prix.
        #   2. It leaves room for a session that does not exist yet. 2027
        #      introduced two; the answer is a new number in a free slot, never
        #      a renumber.
        # ------------------------------------------------------------------
        session_elements={
            "MOTOGP_PRESS_CONFERENCE": 1,

            "MOTO3_FP1": 10,
            "MOTOGP_FP1": 12,
            "MOTO2_FP1": 14,

            "MOTO3_PRACTICE": 20,
            "MOTOGP_PRACTICE": 22,
            "MOTO2_PRACTICE": 24,

            "MOTO3_FP2": 30,
            "MOTOGP_FP2": 32,
            "MOTO2_FP2": 34,
            "MOTOGP_SPRINT_QUALIFYING": 36,

            "MOTOGP_FP3": 40,

            "MOTO3_Q1": 50,
            "MOTOGP_Q1": 52,
            "MOTO2_Q1": 54,

            "MOTO3_Q2": 60,
            "MOTOGP_Q2": 62,
            "MOTO2_Q2": 64,

            "MOTOGP_SPRINT": 70,
            "MOTOGP_WARMUP": 78,

            "MOTO3_RACE": 80,
            "MOTOGP_RACE": 82,
            "MOTO2_RACE": 84,

            "MOTOGP_CHEQUERED_FLAG": 90,
        },

        # Seasons at or before this were written with the dense table. The
        # switch is immediate rather than at the 2027 boundary: the 2025 and
        # 2026 library content is deleted and re-fetched under the new scheme,
        # so one numbering applies from 2026 onwards and no mixed library is
        # ever produced.
        #
        # Why a cutover season and not "always try both": the two schemes'
        # folder names have an identical shape ("26R04E06 MotoGP Practice"), so
        # a leftover E06 cannot be disambiguated from the name alone. Guessing
        # would read a stale Race folder as a practice session - silent
        # corruption. The season is the only information that distinguishes
        # them, so the season decides, and anything on the wrong side is
        # REPORTED rather than quietly skipped.
        legacy_element_season=2025,
        legacy_session_elements={
            "MOTOGP_PRESS_CONFERENCE": 1,
            "MOTO3_FP1": 2,
            "MOTOGP_FP1": 3,
            "MOTO2_FP1": 4,
            "MOTO3_PRACTICE": 5,
            "MOTOGP_PRACTICE": 6,
            "MOTO2_PRACTICE": 7,
            "MOTO3_FP2": 8,
            "MOTOGP_FP2": 9,
            "MOTO2_FP2": 10,
            "MOTO3_Q1": 11,
            "MOTO3_Q2": 12,
            "MOTOGP_Q1": 13,
            "MOTOGP_Q2": 14,
            "MOTOGP_SPRINT": 15,
            "MOTO2_Q1": 16,
            "MOTO2_Q2": 17,
            "MOTOGP_WARMUP": 18,
            "MOTO3_RACE": 19,
            "MOTO2_RACE": 20,
            "MOTOGP_RACE": 21,
            "MOTOGP_CHEQUERED_FLAG": 22,
            "MOTOGP_FP3": 23,
            "MOTOGP_SPRINT_QUALIFYING": 24,
        },

        session_display_names={
            "MOTOGP_PRESS_CONFERENCE": "Pre-event Press Conference",
            "MOTO3_FP1": "Free Practice 1",
            "MOTOGP_FP1": "Free Practice 1",
            "MOTO2_FP1": "Free Practice 1",
            "MOTO3_PRACTICE": "Practice",
            "MOTOGP_PRACTICE": "Practice",
            "MOTO2_PRACTICE": "Practice",
            "MOTO3_FP2": "Free Practice 2",
            "MOTOGP_FP2": "Free Practice 2",
            "MOTO2_FP2": "Free Practice 2",
            "MOTOGP_FP3": "Free Practice 3",
            "MOTOGP_SPRINT_QUALIFYING": "Sprint Qualifying",
            "MOTO3_Q1": "Qualifying 1",
            "MOTO3_Q2": "Qualifying 2",
            "MOTOGP_Q1": "Qualifying 1",
            "MOTOGP_Q2": "Qualifying 2",
            "MOTOGP_SPRINT": "Sprint",
            "MOTO2_Q1": "Qualifying 1",
            "MOTO2_Q2": "Qualifying 2",
            "MOTOGP_WARMUP": "Warm Up",
            "MOTO3_RACE": "Race",
            "MOTO2_RACE": "Race",
            "MOTOGP_RACE": "Race",
            "MOTOGP_CHEQUERED_FLAG": "Chequered Flag",
            "UNKNOWN": "Unknown",
        },

        # Short labels for what a user reads in Plex, e.g. "MotoGP FP2" rather
        # than "MotoGP Free Practice 2".
        #
        # Why not derive this from display_name: "Practice" is exactly the word
        # that causes confusion from 2027, when the Friday session is FP2 and no
        # longer called Practice. A short label keyed on the session type (not
        # on whatever the release happened to be titled) means the name always
        # says what the session actually is, in the vocabulary the rider-facing
        # timetable uses.
        session_short_names={
            "MOTOGP_PRESS_CONFERENCE": "Press Conference",
            "MOTO3_FP1": "FP1",
            "MOTOGP_FP1": "FP1",
            "MOTO2_FP1": "FP1",
            "MOTO3_PRACTICE": "Practice",
            "MOTOGP_PRACTICE": "Practice",
            "MOTO2_PRACTICE": "Practice",
            "MOTO3_FP2": "FP2",
            "MOTOGP_FP2": "FP2",
            "MOTO2_FP2": "FP2",
            "MOTOGP_FP3": "FP3",
            "MOTOGP_SPRINT_QUALIFYING": "Sprint Qualifying",
            "MOTO3_Q1": "Q1",
            "MOTO3_Q2": "Q2",
            "MOTOGP_Q1": "Q1",
            "MOTOGP_Q2": "Q2",
            "MOTOGP_SPRINT": "Sprint",
            "MOTO2_Q1": "Q1",
            "MOTO2_Q2": "Q2",
            "MOTOGP_WARMUP": "Warm Up",
            "MOTO3_RACE": "Race",
            "MOTO2_RACE": "Race",
            "MOTOGP_RACE": "Race",
            "MOTOGP_CHEQUERED_FLAG": "Chequered Flag",
            "UNKNOWN": "Unknown",
        },

        session_race_class={
            "MOTOGP_PRESS_CONFERENCE": "motogp",
            "MOTO3_FP1": "moto3",
            "MOTOGP_FP1": "motogp",
            "MOTO2_FP1": "moto2",
            "MOTO3_PRACTICE": "moto3",
            "MOTOGP_PRACTICE": "motogp",
            "MOTO2_PRACTICE": "moto2",
            "MOTO3_FP2": "moto3",
            "MOTOGP_FP2": "motogp",
            "MOTO2_FP2": "moto2",
            "MOTOGP_FP3": "motogp",
            "MOTOGP_SPRINT_QUALIFYING": "motogp",
            "MOTO3_Q1": "moto3",
            "MOTO3_Q2": "moto3",
            "MOTOGP_Q1": "motogp",
            "MOTOGP_Q2": "motogp",
            "MOTOGP_SPRINT": "motogp",
            "MOTO2_Q1": "moto2",
            "MOTO2_Q2": "moto2",
            "MOTOGP_WARMUP": "motogp",
            "MOTO3_RACE": "moto3",
            "MOTO2_RACE": "moto2",
            "MOTOGP_RACE": "motogp",
            "MOTOGP_CHEQUERED_FLAG": "motogp",
        },

        core_sessions=(
            "MOTO3_PRACTICE", "MOTOGP_PRACTICE", "MOTO2_PRACTICE",
            "MOTO3_Q1", "MOTO3_Q2", "MOTOGP_Q1", "MOTOGP_Q2",
            "MOTO2_Q1", "MOTO2_Q2", "MOTOGP_SPRINT",
            "MOTO3_RACE", "MOTO2_RACE", "MOTOGP_RACE",
        ),

        feature_race_sessions=("MOTO3_RACE", "MOTO2_RACE", "MOTOGP_RACE"),

        default_desired_sessions={
            "MOTOGP_PRESS_CONFERENCE": False,
            "MOTO3_FP1": False,
            "MOTOGP_FP1": True,
            "MOTO2_FP1": False,
            "MOTO3_PRACTICE": False,
            "MOTOGP_PRACTICE": True,
            "MOTO2_PRACTICE": True,
            "MOTO3_FP2": False,
            "MOTOGP_FP2": True,
            "MOTO2_FP2": False,
            "MOTOGP_FP3": True,
            "MOTOGP_SPRINT_QUALIFYING": True,
            "MOTO3_Q1": True,
            "MOTO3_Q2": True,
            "MOTOGP_Q1": True,
            "MOTOGP_Q2": True,
            "MOTOGP_SPRINT": True,
            "MOTO2_Q1": True,
            "MOTO2_Q2": True,
            "MOTOGP_WARMUP": False,
            "MOTO3_RACE": True,
            "MOTO2_RACE": True,
            "MOTOGP_RACE": True,
            "MOTOGP_CHEQUERED_FLAG": False,
        },

        # ------------------------------------------------------------------
        # Release-title patterns, most specific first.
        #
        # Sprint Qualifying must be tested before Sprint, and Sprint must
        # exclude "qualifying", otherwise "Sprint Qualifying" would be read as
        # the Sprint race. That comment survived three rewrites of this table;
        # keep it with the table.
        # ------------------------------------------------------------------
        session_patterns=(
            SessionPatternSpec(
                "MOTOGP_PRESS_CONFERENCE",
                ("pre event", "pre-event", "press conference", "pre.event", "gear up", "gear.up"),
                (), 100,
            ),
            SessionPatternSpec(
                "MOTOGP_CHEQUERED_FLAG",
                ("chequered flag", "checkered flag", "chequered.flag", "checkered.flag"),
                (), 100,
            ),
            SessionPatternSpec(
                "MOTOGP_SPRINT_QUALIFYING",
                ("sprint qualifying", "sprint.qualifying", "sprint quali",
                 "sprint shootout"),
                (), 97,
            ),
            SessionPatternSpec(
                "MOTOGP_SPRINT",
                ("sprint",),
                ("qualifying", "quali", "qualifing", "shootout"),
                95,
            ),
            SessionPatternSpec(
                "MOTOGP_WARMUP",
                ("warm up", "warm-up", "warmup", "warming up", "warm.up", "warming.up"),
                (), 95,
            ),
            SessionPatternSpec(
                "MOTOGP_FP1",
                ("fp1", "free practice 1", "free practice one",
                 "free.practice.1", "free.practice.one"),
                (), 90,
            ),
            SessionPatternSpec(
                "MOTOGP_FP3",
                ("fp3", "free practice 3", "free practice three",
                 "free.practice.3", "free.practice.three"),
                (), 91,
            ),
            SessionPatternSpec(
                "MOTOGP_FP2",
                ("fp2", "free practice 2", "free practice two",
                 "free.practice.2", "free.practice.two"),
                (), 90,
            ),
            SessionPatternSpec(
                "MOTOGP_Q1",
                ("q1", "qualifying 1", "qualifying one", "qualifying.1", "qualifying.one"),
                (), 88,
            ),
            SessionPatternSpec(
                "MOTOGP_Q2",
                ("q2", "qualifying 2", "qualifying two", "qualifying.2", "qualifying.two"),
                (), 88,
            ),
            SessionPatternSpec(
                "MOTOGP_Q2",
                ("qualifying", "qualifing"),
                ("q1", "q2", "qualifying 1", "qualifying 2",
                 "qualifying.1", "qualifying.2", "qualifying one", "qualifying two"),
                85,
            ),
            SessionPatternSpec(
                "MOTOGP_PRACTICE",
                ("practice",),
                ("free practice", "free.practice", "fp1", "fp2", "fp 1", "fp 2"),
                82,
            ),
            SessionPatternSpec(
                "MOTOGP_RACE",
                ("race",),
                ("sprint", "sprint race"),
                75,
            ),
        ),

        calendar_key_to_session={
            "moto3_fp1": "MOTO3_FP1",
            "motogp_fp1": "MOTOGP_FP1",
            "moto2_fp1": "MOTO2_FP1",
            "moto3_practice": "MOTO3_PRACTICE",
            "motogp_practice": "MOTOGP_PRACTICE",
            "moto2_practice": "MOTO2_PRACTICE",
            "moto3_fp2": "MOTO3_FP2",
            "motogp_fp2": "MOTOGP_FP2",
            "moto2_fp2": "MOTO2_FP2",
            "moto3_qualifying": "MOTO3_Q2",
            "motogp_qualifying": "MOTOGP_Q2",
            "moto2_qualifying": "MOTO2_Q2",
            "motogp_fp3": "MOTOGP_FP3",
            "motogp_sprint_qualifying": "MOTOGP_SPRINT_QUALIFYING",
            "motogp_sprint": "MOTOGP_SPRINT",
            "motogp_warmup": "MOTOGP_WARMUP",
            "moto3_race": "MOTO3_RACE",
            "moto2_race": "MOTO2_RACE",
            "motogp_race": "MOTOGP_RACE",
        },

        session_to_calendar_key={
            "MOTO3_FP1": "moto3_fp1",
            "MOTOGP_FP1": "motogp_fp1",
            "MOTO2_FP1": "moto2_fp1",
            "MOTO3_PRACTICE": "moto3_practice",
            "MOTOGP_PRACTICE": "motogp_practice",
            "MOTO2_PRACTICE": "moto2_practice",
            "MOTO3_FP2": "moto3_fp2",
            "MOTOGP_FP2": "motogp_fp2",
            "MOTO2_FP2": "moto2_fp2",
            "MOTO3_Q1": "moto3_qualifying",
            "MOTO3_Q2": "moto3_qualifying",
            "MOTOGP_Q1": "motogp_qualifying",
            "MOTOGP_Q2": "motogp_qualifying",
            "MOTOGP_FP3": "motogp_fp3",
            "MOTOGP_SPRINT_QUALIFYING": "motogp_sprint_qualifying",
            "MOTOGP_SPRINT": "motogp_sprint",
            "MOTO2_Q1": "moto2_qualifying",
            "MOTO2_Q2": "moto2_qualifying",
            "MOTOGP_WARMUP": "motogp_warmup",
            "MOTO3_RACE": "moto3_race",
            "MOTO2_RACE": "moto2_race",
            "MOTOGP_RACE": "motogp_race",
        },

        calendar_key_to_day={
            "moto3_fp1": "friday",
            "motogp_fp1": "friday",
            "moto2_fp1": "friday",
            "moto3_practice": "friday",
            "motogp_practice": "friday",
            "moto2_practice": "friday",
            "moto3_fp2": "saturday",
            "motogp_fp2": "saturday",
            "moto2_fp2": "saturday",
            "motogp_fp3": "saturday",
            "motogp_sprint_qualifying": "friday",
            "moto3_qualifying": "saturday",
            "motogp_qualifying": "saturday",
            "motogp_sprint": "saturday",
            "moto2_qualifying": "saturday",
            "motogp_warmup": "sunday",
            "moto3_race": "sunday",
            "moto2_race": "sunday",
            "motogp_race": "sunday",
        },

        # Map session types to weekend-format session names.
        #
        # Support class sessions are absent: only the MotoGP format varies by
        # season. Note that MOTOGP_FP2 maps to "fp2" and MOTOGP_PRACTICE maps
        # to "practice": in 2026 Friday runs FP1 + a one hour Practice, from
        # 2027 Friday runs FP1 + a 30 minute FP2 + Sprint Qualifying, so the
        # two names never collide within a single weekend.
        session_to_format_name={
            "MOTOGP_FP1": "fp1",
            "MOTOGP_PRACTICE": "practice",
            "MOTOGP_FP2": "fp2",
            "MOTOGP_FP3": "fp3",
            "MOTOGP_Q1": "qualifying",
            "MOTOGP_Q2": "qualifying",
            "MOTOGP_SPRINT_QUALIFYING": "sprint_qualifying",
            "MOTOGP_SPRINT": "sprint",
            "MOTOGP_WARMUP": "warmup",
            "MOTOGP_RACE": "race",
            "MOTOGP_CHEQUERED_FLAG": "chequered_flag",
        },

        # Reverse of the above, including the combined-name aliases a
        # calendar's release_aliases resolve through. "qualifying" is the
        # combined session and maps to Q2, which is the target the grabber uses
        # for a combined qualifying release; Q1 has no distinct weekend-format
        # name of its own.
        format_name_to_session={
            "fp1": "MOTOGP_FP1",
            "practice": "MOTOGP_PRACTICE",
            "fp2": "MOTOGP_FP2",
            "fp3": "MOTOGP_FP3",
            "qualifying": "MOTOGP_Q2",
            "sprint_qualifying": "MOTOGP_SPRINT_QUALIFYING",
            "sprint": "MOTOGP_SPRINT",
            "warmup": "MOTOGP_WARMUP",
            "race": "MOTOGP_RACE",
            "chequered_flag": "MOTOGP_CHEQUERED_FLAG",
        },

        asset_dirs={
            "motogp": {"fanart": "motogp_fanart", "poster": "motogp_posters"},
            "moto2": {"fanart": "moto2_fanart", "poster": "moto2_posters"},
            "moto3": {"fanart": "moto3_fanart", "poster": "moto3_posters"},
        },
    ),
    # MotoGP is the default because it is the series this app was built for and
    # the only one with media already on disk. Adding a series must never change
    # this - that would silently repoint every existing install.
    default=True,
)
