"""
Series registry: the per-championship vocabulary of the combined app.

MotoGParr began life as a MotoGP-only program and grew three classes
(MotoGP / Moto2 / Moto3) inside it. Adding Formula 1 is the same move one level
up: a *series* is a championship with its own session vocabulary, its own
calendar files, its own Plex library and its own artwork, and everything the app
does with a session is driven by tables that belong to that series.

This module holds those tables as **data** and materialises them into the enums
and dictionaries the rest of the code imports. It deliberately contains no
algorithms: classifying a release title, filtering candidates and building a
folder prefix are shared behaviour that reads a series' vocabulary, and they live
in the spine (`motogp_common.py` today, a series-agnostic module once the F1
series lands).

Why data and not code: the 2027 MotoGP weekend format taught the lesson. FP3 and
Sprint Qualifying arrived, the dense 1-24 element table had no room for them, and
the only way to add them was to append numbers *after* the Race - so a 2027 round
listed out of order in every folder, title and CLI listing. The fix was to make
the numbering a table with gaps. The same reasoning applies one level up: a new
series must be a declaration, not a fork.

Adding a series means writing one module in this package and nothing else. If
that stops being true, the abstraction is wrong - fix the abstraction rather than
special-casing a series inside shared code, because the second special case is
how a hybrid turns back into two apps.
"""

from dataclasses import dataclass, field
import re
from enum import IntEnum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: The shape every session folder name starts with. Kept here so the
#: registry and the spine cannot disagree about what a prefix looks like.
_PREFIX_RE = re.compile(r'(\d{2})R(\d{2})E(\d{2})')

__all__ = [
    "ClassSpec",
    "SessionPatternSpec",
    "Series",
    "SeriesRuntime",
    "build",
    "register",
    "get_series",
    "all_series",
    "default_series",
    "series_keys",
]


# =============================================================================
# DECLARATION
# =============================================================================

@dataclass(frozen=True)
class ClassSpec:
    """
    One class inside a series.

    MotoGP has three; Formula 1 has one. A series with a single class still
    declares it, because the class is what the folder name, the Plex title and
    the artwork directory are keyed on - an absent class would mean every one of
    those had to grow a special case for "no class".
    """
    key: str
    display_name: str
    #: Sort order within a weekend; lower runs earlier.
    order: int = 0
    #: Substrings that identify this class in a bare class token, matched
    #: against text with spaces and hyphens removed. Checked in *declaration*
    #: order, which is why "moto3" must be declared before "moto2": the check is
    #: a substring test, and "moto3" contains "moto".
    name_tokens: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionPatternSpec:
    """
    One release-title pattern, in a series' own vocabulary.

    ``session`` names a member of the series' SessionType enum. Keeping it a
    string rather than an enum member is what lets a declaration be written
    before the enum exists.
    """
    session: str
    must_have: Tuple[str, ...]
    must_not_have: Tuple[str, ...] = ()
    #: Higher wins. Specific beats general, which is why "Sprint Qualifying"
    #: outranks both "Sprint" and "Qualifying".
    priority: int = 0


@dataclass(frozen=True)
class Series:
    """
    Everything about one championship that is not an algorithm.

    The tables are keyed by *member name* (``"MOTOGP_RACE"``) rather than by
    enum value, so a declaration reads the same way regardless of which integer
    a member happens to carry and cannot be broken by renumbering.
    """

    # --- identity -----------------------------------------------------------
    key: str
    display_name: str
    #: Prefix of the calendar files: ``config/<calendar_prefix>_<year>.json``.
    calendar_prefix: str
    #: Root logger name, so ``logs/<log_name>.log`` is per series.
    log_name: str
    #: Prefix that namespaces this series' state in /config. Two series writing
    #: ``grabber_state.json`` into one volume would overwrite each other's
    #: entries, which is the collision this field exists to prevent.
    state_prefix: str
    #: Word releases actually carry: "MotoGP 2026 Buriram", "Formula1 2026 Silverstone".
    search_prefix: str
    #: Port the WebUI listens on. One container, one port - see ``webui_port``
    #: on the combined app rather than per series.
    webui_port: int = 5679

    # --- classes ------------------------------------------------------------
    classes: Tuple[ClassSpec, ...] = ()

    # --- session vocabulary -------------------------------------------------
    #: (member name, enum value). The value is chronological order and nothing
    #: more; it is never written to disk.
    session_members: Tuple[Tuple[str, int], ...] = ()
    #: (member name, element number) - the number that IS written to disk.
    session_elements: Tuple[Tuple[str, int], ...] = ()
    #: The dense pre-cutover table, kept so folders an older version wrote are
    #: still read. Empty for a series that has never been renumbered.
    legacy_session_elements: Tuple[Tuple[str, int], ...] = ()
    #: Seasons at or before this were written with the legacy table. ``None``
    #: means the series has no legacy scheme, so every season uses the current
    #: one and no number can ever be ambiguous.
    legacy_element_season: Optional[int] = None

    session_display_names: Dict[str, str] = field(default_factory=dict)
    session_short_names: Dict[str, str] = field(default_factory=dict)
    #: (member name, class key)
    session_race_class: Dict[str, str] = field(default_factory=dict)
    core_sessions: Tuple[str, ...] = ()
    feature_race_sessions: Tuple[str, ...] = ()
    default_desired_sessions: Dict[str, bool] = field(default_factory=dict)

    session_patterns: Tuple[SessionPatternSpec, ...] = ()

    #: Calendar session key -> member name. A combined session (MotoGP
    #: qualifying is one release covering Q1 and Q2) maps both ways to the
    #: member the grabber targets.
    calendar_key_to_session: Dict[str, str] = field(default_factory=dict)
    #: Member name -> calendar session key. Declared separately from the map
    #: above because the relationship is many-to-one: MotoGP Q1 and Q2 both
    #: read ``motogp_qualifying``, so reversing the forward map would silently
    #: drop one of them.
    session_to_calendar_key: Dict[str, str] = field(default_factory=dict)
    #: Calendar session key -> day name. A fallback only: a round's own
    #: ``sessions`` block is authoritative about which day a key runs.
    calendar_key_to_day: Dict[str, str] = field(default_factory=dict)
    #: Member name -> the weekend-format session name in the calendar's
    #: ``weekend_formats`` block (``"fp1"``, ``"sprint_qualifying"``).
    session_to_format_name: Dict[str, str] = field(default_factory=dict)
    #: Reverse of the above, including the combined-name aliases a calendar's
    #: ``release_aliases`` resolve through.
    format_name_to_session: Dict[str, str] = field(default_factory=dict)

    #: Substrings that make a title belong to a different championship
    #: entirely - F1 Academy, F2, F3. Matched case-insensitively against the
    #: normalised title before anything else.
    excluded_terms: Tuple[str, ...] = ()

    #: (class key, {"fanart": <dir>, "poster": <dir>}), relative to assets/.
    asset_dirs: Dict[str, Dict[str, str]] = field(default_factory=dict)


# =============================================================================
# MATERIALISATION
# =============================================================================

class SeriesRuntime:
    """
    A declaration turned into the objects the spine imports.

    One instance per series, built once at import. It owns the enums, so
    ``motogp_common.SessionType`` and a future ``f1_common.SessionType`` are
    different classes with the same shape - which is what lets shared code take
    a session from either series without either series' numbers leaking into
    the other's folder names.
    """

    def __init__(self, series: Series, assets_dir: Optional[Path] = None):
        self.series = series
        self.key = series.key
        self.assets_dir = Path(assets_dir) if assets_dir else _default_assets_dir()

        self._race_class_by_key: Dict[str, object] = {}
        self.race_class = self._build_race_class()
        self.session_type = self._build_session_type()

        st = self.session_type
        self.session_elements: Dict[object, int] = {
            st[name]: number for name, number in series.session_elements.items()
        }
        self.legacy_session_elements: Dict[object, int] = {
            st[name]: number for name, number in series.legacy_session_elements.items()
        }
        self.legacy_element_season = series.legacy_element_season

        self.session_display_names: Dict[object, str] = {
            st[name]: value for name, value in series.session_display_names.items()
        }
        self.session_short_names: Dict[object, str] = {
            st[name]: value for name, value in series.session_short_names.items()
        }
        self.session_race_class: Dict[object, object] = {
            st[name]: self.class_of(key)
            for name, key in series.session_race_class.items()
        }
        self.core_sessions = {st[n] for n in series.core_sessions}
        self.feature_race_sessions = {st[n] for n in series.feature_race_sessions}
        self.default_desired_sessions: Dict[object, bool] = {
            st[name]: wanted
            for name, wanted in series.default_desired_sessions.items()
        }

        self.session_patterns = [
            _Pattern(st[p.session], p.must_have, p.must_not_have, p.priority)
            for p in series.session_patterns
        ]

        self.calendar_key_to_session: Dict[str, object] = {
            key: st[name] for key, name in series.calendar_key_to_session.items()
        }
        self.session_to_calendar_key: Dict[object, str] = {
            st[name]: key for name, key in series.session_to_calendar_key.items()
        }
        self.calendar_key_to_day: Dict[str, str] = dict(series.calendar_key_to_day)
        self.session_to_format_name: Dict[object, str] = {
            st[name]: value for name, value in series.session_to_format_name.items()
        }
        self.format_name_to_session: Dict[str, object] = {
            name: st[member]
            for name, member in series.format_name_to_session.items()
        }

        self.asset_dirs: Dict[str, Dict[str, Path]] = {
            class_key: {
                kind: self.assets_dir / relative
                for kind, relative in dirs.items()
            }
            for class_key, dirs in series.asset_dirs.items()
        }

    # -- classes ------------------------------------------------------------

    @property
    def feature_class(self):
        """
        The class whose weekend format a calendar's ``weekend_formats`` describes.

        Declared order is the weekend running order, so the feature class is
        the last one declared. MotoGP: Moto3, Moto2, MotoGP. F1: F1. Deriving it
        from the declaration is what lets shared code ask "is this the class the
        format governs?" without naming a class - and naming one was the bug
        that skipped F1's gate entirely.
        """
        return self.class_of(
            max(self.series.classes, key=lambda declared: declared.order).key
        )

    def class_of(self, key: str):
        """The RaceClass member for a class key such as ``"motogp"``."""
        try:
            return self._race_class_by_key[key]
        except KeyError:
            raise KeyError(
                f"series {self.key!r} has no class {key!r}; "
                f"declared: {sorted(self._race_class_by_key)}"
            ) from None

    def _build_race_class(self):
        members = [(c.key.upper(), c.order) for c in self.series.classes]
        if not members:
            raise ValueError(
                f"series {self.key!r} declares no classes; a series with no "
                f"class would need a special case in every folder name, Plex "
                f"title and artwork lookup"
            )
        classes = self.series.classes
        cls = IntEnum(f"{self._stem()}RaceClass", members)
        self._race_class_by_key = {c.key: cls[c.key.upper()] for c in classes}
        # Closed over rather than looked up on the runtime: a property's self is
        # the enum member, not the SeriesRuntime, so it has no way back here.
        display_by_key = {c.key: c.display_name for c in self.series.classes}

        def display_name(self) -> str:
            return display_by_key.get(self.name.lower(), self.name)

        def folder_key(self) -> str:
            return self.name.lower()

        def from_string(cls_, text: str):
            """
            Parse a class from a bare token, or None.

            Declaration order is the precedence order, and it has to be: the
            tokens are substring tests, so "moto3" contains "moto" and a class
            declared as "moto" would swallow every "moto3".
            """
            text_lower = text.lower().replace(" ", "").replace("-", "")
            for spec in classes:
                for token in spec.name_tokens:
                    if token in text_lower:
                        return cls_[spec.key.upper()]
            return None

        cls.display_name = property(display_name)  # type: ignore[assignment]
        cls.folder_key = property(folder_key)  # type: ignore[assignment]
        cls.from_string = classmethod(from_string)  # type: ignore[assignment]
        return cls

    # -- session vocabulary -------------------------------------------------

    def _build_session_type(self):
        st = IntEnum(
            f"{self._stem()}SessionType", list(self.series.session_members)
        )
        # The lookup tables are attached after construction because they are
        # keyed on the members, which do not exist until the enum does.
        #
        # They are LOCALS, closed over by the properties below. __init__ owns
        # the public attributes and builds them separately: a property cannot
        # reach the runtime (its self is the enum member), so the two would
        # have to agree by convention, and a table built in two places is a
        # table that drifts.
        display_names = {
            st[name]: value for name, value in self.series.session_display_names.items()
        }
        short_names = {
            st[name]: value for name, value in self.series.session_short_names.items()
        }
        race_class_of = {
            st[name]: self.class_of(key)
            for name, key in self.series.session_race_class.items()
        }
        core = {st[n] for n in self.series.core_sessions}
        feature = {st[n] for n in self.series.feature_race_sessions}
        calendar_key_of = {
            st[name]: key for name, key in self.series.session_to_calendar_key.items()
        }
        format_name_of = {
            st[name]: value for name, value in self.series.session_to_format_name.items()
        }

        def s_display_name(self) -> str:
            return display_names.get(self, "Unknown")

        def s_short_name(self) -> str:
            return short_names.get(self, "Unknown")

        def s_full_display_name(self) -> str:
            race_class = race_class_of.get(self)
            if race_class:
                return f"{race_class.display_name} {display_names.get(self, 'Unknown')}"
            return display_names.get(self, "Unknown")

        def s_full_short_name(self) -> str:
            race_class = race_class_of.get(self)
            if race_class:
                return f"{race_class.display_name} {short_names.get(self, 'Unknown')}"
            return short_names.get(self, "Unknown")

        def s_race_class(self):
            return race_class_of.get(self)

        def s_is_core(self) -> bool:
            return self in core

        def s_is_feature(self) -> bool:
            return self in feature

        def s_calendar_key(self):
            return calendar_key_of.get(self)

        def s_format_name(self):
            return format_name_of.get(self)

        # The MotoGP-era name. Kept as an alias rather than renamed at every
        # call site because "format" is the generic concept and the old name
        # named the only series that had one.
        def s_motogp_session_name(self):
            return format_name_of.get(self)

        #: Which series these members belong to, and which class its weekend
        #: format describes, on the *class* rather than on a member.
        #:
        #: Shared code receives a session from either series and has to ask
        #: questions about the series it came from. Comparing the member's
        #: class against a hardcoded MotoGP enum silently exempts every
        #: foreign session - which is exactly how F1's weekend-format gate was
        #: skipped - so the answer has to travel with the enum. There is one
        #: place it can come from, and this is it.
        st.series_key = self.key  # type: ignore[attr-defined]
        st.feature_class = classmethod(  # type: ignore[attr-defined]
            lambda cls: runtime(cls.series_key).feature_class
        )

        st.display_name = property(s_display_name)  # type: ignore[assignment]
        st.short_name = property(s_short_name)  # type: ignore[assignment]
        st.full_display_name = property(s_full_display_name)  # type: ignore[assignment]
        st.full_short_name = property(s_full_short_name)  # type: ignore[assignment]
        st.race_class = property(s_race_class)  # type: ignore[assignment]
        st.is_core_session = property(s_is_core)  # type: ignore[assignment]
        st.is_feature_race = property(s_is_feature)  # type: ignore[assignment]
        st.calendar_key = property(s_calendar_key)  # type: ignore[assignment]
        st.format_session_name = property(s_format_name)  # type: ignore[assignment]
        st.motogp_session_name = property(s_motogp_session_name)  # type: ignore[assignment]
        return st

    # -- numbering -----------------------------------------------------------

    def element_number(self, session, season: Optional[int] = None) -> int:
        """The number to WRITE into a folder name for this session and season."""
        if season is not None and self.legacy_element_season is not None \
                and season <= self.legacy_element_season:
            return self.legacy_session_elements.get(session, int(session))
        return self.session_elements.get(session, int(session))

    def legacy_element_number(self, session) -> int:
        """The number an earlier version would have written. Read-only."""
        return self.legacy_session_elements.get(session, int(session))

    def _element_tables(self, season: Optional[int] = None):
        """
        Which (elements, is_legacy) pairs apply when READING a folder.

        With no season, both schemes are in play and the caller must be able to
        see that a number is ambiguous - that is the honest answer, and it is
        what stops an unknown-season scan from quietly picking one meaning.

        A series with no legacy table has exactly one scheme, so nothing can be
        ambiguous and no season argument is needed.
        """
        if self.legacy_session_elements:
            if season is None:
                return [(self.session_elements, False),
                        (self.legacy_session_elements, True)]
            if self.legacy_element_season is not None \
                    and season <= self.legacy_element_season:
                return [(self.legacy_session_elements, True)]
        return [(self.session_elements, False)]

    def get_session_from_element(self, element_num: int,
                                 season: Optional[int] = None):
        """
        The session an element number names, or None when it names none.

        The meaning of a number depends on which numbering scheme wrote it, so
        pass the season whenever it is known (it is embedded in the prefix).
        With no season, only numbers that mean the SAME session in both schemes
        are returned: an ambiguous number must not be guessed, because reading a
        Race folder as a practice session is silent corruption.
        """
        candidates = set()
        for table, _is_legacy in self._element_tables(season):
            for session, number in table.items():
                if number == element_num:
                    candidates.add(session)
        if len(candidates) == 1:
            return candidates.pop()
        return None

    def element_is_ambiguous(self, element_num: int,
                             season: Optional[int] = None) -> bool:
        """True when this number means different sessions in the two schemes."""
        if season is not None:
            return False
        meanings = set()
        for table, _is_legacy in self._element_tables(None):
            for session, number in table.items():
                if number == element_num:
                    meanings.add(session)
        return len(meanings) > 1

    def build_prefix(self, season: int, round_num: int, session) -> str:
        """
        The YYRREE prefix: season, round, session-as-episode.

        The number comes from element_number(), so a season at or before the
        cutover keeps its dense value and nothing already on disk changes.
        """
        return f"{season % 100:02d}R{round_num:02d}" \
               f"E{self.element_number(session, season):02d}"

    def parse_prefix(self, text: str):
        """
        Parse a YYRREE prefix, or None when it does not resolve.

        The season is read from the prefix, so in practice it always
        disambiguates the numbering scheme. Returns None rather than a guess
        for a number that means nothing in the applicable scheme.
        """
        match = _PREFIX_RE.match(text)
        if not match:
            return None
        year_short, round_num, element_num = map(int, match.groups())
        session = self.get_session_from_element(
            element_num, season=2000 + year_short
        )
        if session is None:
            return None
        return (2000 + year_short, round_num, session)

    def find_legacy_folders(self, season: int, session_folders) -> List[str]:
        """
        Session folder names inside `season` the current scheme cannot read.

        Season is a required argument, not a nicety: a stale folder is
        indistinguishable by name from a modern one, so guessing from the name
        alone would either miss stale folders or flag valid ones.
        """
        found = []
        for name in session_folders:
            if not _PREFIX_RE.match(name):
                continue
            if self.parse_prefix(name) is None:
                found.append(name)
        return found

    # -- convenience ---------------------------------------------------------

    def state_file(self, name: str) -> str:
        """
        A /config filename namespaced to this series.

        ``grabber_state`` -> ``grabber_state.motogp.json``. Never derive this
        by hand at a call site: two series sharing one filename is the bug.
        """
        return f"{name}.{self.series.state_prefix}.json"

    def legacy_state_file(self, name: str) -> str:
        """
        The un-namespaced filename this series wrote before it had a sibling.

        MotoGP wrote ``grabber_state.json`` for years into a ``/config`` whose
        only writer was MotoGP. The namespaced name is where new writes go; the
        old one is read once and migrated. It is a property of the *default*
        series only - a series added later never had an unprefixed file, and
        inheriting one from MotoGP would resurrect the collision.
        """
        if self.series.key != default_series().key:
            raise ValueError(
                f"{self.key!r} has no legacy state file: only "
                f"{default_series().key!r} ever wrote an un-namespaced one"
            )
        return f"{name}.json"

    def migrate_state_file(self, config_dir: Path, name: str) -> Optional[Path]:
        """
        Move this series' pre-namespacing file to its namespaced name.

        Returns the new path if a file was moved, else None. Idempotent and
        safe to call on every start:

          * namespaced file already exists -> nothing to do. It wins, always;
            the legacy file is left alone rather than merged or deleted,
            because two writers disagreeing is a data question for the user,
            not something a rename should settle.
          * legacy file exists -> rename it. A rename, not a copy, so there is
            exactly one file afterwards and no way for the app to read one
            while writing the other.
          * neither -> nothing to do.

        Not run through ``utils/json_store``: nothing else may be running yet
        (this is startup, before the WebUI and scheduler exist), and a rename
        cannot half-happen. A failed rename is logged and swallowed by the
        caller's own writer - losing the history is bad, refusing to start is
        worse.
        """
        config_dir = Path(config_dir)
        target = config_dir / self.state_file(name)
        if target.exists():
            return None
        try:
            source = config_dir / self.legacy_state_file(name)
        except ValueError:
            return None
        if not source.exists():
            return None
        source.rename(target)
        return target

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SeriesRuntime {self.key} " \
               f"{len(self.series.session_members)} sessions>"


    def _stem(self) -> str:
        return "".join(part.capitalize() for part in self.key.split("_"))

@dataclass
class _Pattern:
    """A materialised SessionPattern, mirroring the spine's dataclass shape."""

    session_type: object
    must_have: Tuple[str, ...]
    must_not_have: Tuple[str, ...]
    priority: int = 0


def _default_assets_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "assets"


def build(series: Series, assets_dir: Optional[Path] = None) -> SeriesRuntime:
    """Materialise a declaration into the objects the spine imports."""
    return SeriesRuntime(series, assets_dir=assets_dir)


# =============================================================================
# REGISTRY
# =============================================================================

_REGISTRY: Dict[str, Series] = {}
_RUNTIMES: Dict[str, SeriesRuntime] = {}
_DEFAULT: Optional[str] = None


def register(series: Series, default: bool = False) -> Series:
    """

    Add a series to the registry.

    Called at import time by each module in this package, so the set of series
    is exactly the set of modules present. ``default=True`` marks the series
    used when a caller does not name one; there can be only one, and a second
    call is a programming error rather than a silent switch.
    """
    global _DEFAULT

    # Validate BEFORE mutating. A refusal that has already added the series
    # leaves it half-registered: every later all_series() walk then picks up a
    # declaration that never materialised, and the failure surfaces somewhere
    # else entirely - as it did when a test asserting a refused second default
    # left a two-member fake series in the registry for the rest of the run.
    if series.key in _REGISTRY and _REGISTRY[series.key] is not series:
        raise ValueError(f"series {series.key!r} is already registered")
    if default and _DEFAULT is not None and _DEFAULT != series.key:
        raise ValueError(
            f"{series.key!r} cannot be the default series; "
            f"{_DEFAULT!r} already is"
        )

    _REGISTRY[series.key] = series
    if default:
        _DEFAULT = series.key
    return series


def get_series(key: str) -> Series:
    """Look up a declaration by key."""
    try:
        return _REGISTRY[key]
    except KeyError:
        raise KeyError(
            f"unknown series {key!r}; registered: {sorted(_REGISTRY)}"
        ) from None


def all_series() -> List[Series]:
    """Every registered series, in registration order."""
    return list(_REGISTRY.values())


def series_keys() -> List[str]:
    return list(_REGISTRY)


def default_series() -> Series:
    """
    The series a caller gets when it names none.

    MotoGP is the default because it is the series this app was built for and
    the only one with media already on disk. A new series must never become the
    default by being added - that would silently repoint every existing install.
    """
    if _DEFAULT is None:
        raise RuntimeError("no default series has been registered")
    return _REGISTRY[_DEFAULT]


def runtime(key: Optional[str] = None) -> SeriesRuntime:
    """
    The materialised runtime for a series, built once and cached.

    Building twice would produce two different ``SessionType`` classes for the
    same series, and ``isinstance``/``==`` between them would fail in ways that
    look like a matching bug.
    """
    key = key or default_series().key
    if key not in _RUNTIMES:
        _RUNTIMES[key] = build(get_series(key))
    return _RUNTIMES[key]
