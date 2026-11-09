#!/usr/bin/env python3
"""
MotoGP Grabber Web UI
======================
Web interface for MotoGP grabber - refactored with templates.
"""

import os
import sys
import threading
import json
import re
import logging
from functools import lru_cache
from urllib.parse import quote
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta, timezone
from flask import (
    Flask, render_template, request, jsonify, 
    send_from_directory, Response
)

logger = logging.getLogger("motogp.webui")

# Add paths for imports
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from motogp_common import (
    SessionType,
    RaceClass,
    CALENDAR_KEY_TO_SESSION,
    CALENDAR_DAYS,
    parse_prefix,
    build_prefix,
    load_calendar,
    DEFAULT_DESIRED_SESSIONS,
    get_config_dir,
    get_round_by_number,
    get_round_status,
    is_round_active,
    get_round_classes,
    get_weekend_format,
    round_runs_session,
    element_number,
    # The mismatch dialog has to explain a real decision, so it asks the code
    # that makes the decision. Re-deriving "why" in the WebUI would produce a
    # second implementation that explains the wrong thing after the first rule
    # change.
    explain_release,
    # Namespaced /config filenames: every shared state file carries its
    # series, because two series writing grabber_state.json into one volume
    # overwrite each other within a day.
    state_path,
    SERIES_KEY,
)

from series import all_series, runtime as series_runtime

from utils.config_loader import (
    SETTINGS_FILE,
    get_series_downloads_path,
    get_series_library_path,
    validate,
    reload_settings,
    update_settings,
    has_setting,
    get_max_grabs_per_run,
    get_current_season,
    _load_yaml,
    get_prowlarr_url,
    get_prowlarr_api_key,
    get_sabnzbd_url,
    get_sabnzbd_api_key,
    get_plex_url,
    get_plex_token,
    get_plex_library_name,
    get_required_quality,
    get_allowed_groups,
    get_search_delay_hours,
    get_int,
)
from utils import background_health, home_summary, scan_metrics
from utils.json_store import atomic_write_json, file_lock, read_json, write_json
from utils.webui_jobs import JobManager

# =============================================================================
# CONFIGURATION
# =============================================================================

# The interpreter that spawns the grabber/overlay children.
#
# sys.executable rather than a hard-coded /opt/venv path: the WebUI is already
# running under the right interpreter, so asking it is always correct, and it
# keeps the app runnable outside the image (tests, local runs) where /opt/venv
# does not exist. MOTORSPORTARR_PYTHON (or legacy MOTOGPARR_PYTHON) overrides
# both, matching entrypoint.sh.
def _resolve_python_path(env=None) -> str:
    env = os.environ if env is None else env
    return (
        env.get("MOTORSPORTARR_PYTHON")
        or env.get("MOTOGPARR_PYTHON")
        or sys.executable
    )


PYTHON_PATH = _resolve_python_path()
GRABBER_SCRIPT = SCRIPT_DIR / "motogp_grabber.py"
OVERLAY_SCRIPT = SCRIPT_DIR / "motogp_overlay_utility.py"
CONFIG_DIR = get_config_dir()
WEBUI_CONFIG_FILE = state_path(CONFIG_DIR, "webui_settings")
LOG_FILE = CONFIG_DIR / "logs" / "motogp.log"
# There is deliberately no module-level library path any more. It used to be
# ``MOTOGP_LIBRARY_PATH = get_motogp_library_path()``, and that single value was
# wrong twice over: a request for F1 read MotoGP's library, and a value captured
# at import froze whatever settings.yml said when the process started - while
# every other setting is re-read per page load. Callers use ``library_root()``.


def discover_seasons(series: Optional[str] = None) -> list:
    """
    Seasons that have a calendar file for a series, newest last.

    Derived from ``config/<prefix>_<year>.json`` rather than hard coded, so
    dropping in next season's calendar is enough to make it appear in the UI.
    Keyed on the series' declared calendar prefix: a glob of ``motogp_*``
    silently returns an empty list for Formula 1, which reads in the UI as
    "this championship has no seasons".
    """
    from series import get_series

    key = series or DEFAULT_SERIES
    prefix = get_series(key).calendar_prefix
    years = set()
    try:
        for path in CONFIG_DIR.glob(f"{prefix}_*.json"):
            suffix = path.stem.split("_")[-1]
            if suffix.isdigit():
                years.add(int(suffix))
    except OSError:
        pass

    if not years:
        years.add(datetime.now().year)

    return sorted(years)


#: Which series this process serves requests for when nothing says otherwise.
#:
#: The default series, deliberately: the WebUI must answer exactly as it did
#: before the hybrid existed when no series is named, so an existing install
#: that has never heard of F1 gets its own dashboard and not an empty one.
#: ``motogp_common`` is bound to the same series at import, which is what makes
#: the module-level ``SessionType`` below the right vocabulary for it.
DEFAULT_SERIES = SERIES_KEY

SEASONS = discover_seasons()

SESSION_GROUPS = {
    "MotoGP": [
        "MOTOGP_PRESS_CONFERENCE", "MOTOGP_FP1", "MOTOGP_PRACTICE",
        "MOTOGP_FP2", "MOTOGP_FP3", "MOTOGP_Q1", "MOTOGP_Q2",
        "MOTOGP_SPRINT_QUALIFYING", "MOTOGP_SPRINT",
        "MOTOGP_WARMUP", "MOTOGP_RACE", "MOTOGP_CHEQUERED_FLAG",
    ],
    "Moto2": [
        "MOTO2_FP1", "MOTO2_PRACTICE", "MOTO2_FP2",
        "MOTO2_Q1", "MOTO2_Q2", "MOTO2_RACE",
    ],
    "Moto3": [
        "MOTO3_FP1", "MOTO3_PRACTICE", "MOTO3_FP2",
        "MOTO3_Q1", "MOTO3_Q2", "MOTO3_RACE",
    ],
}

# Build SESSION_TYPES from SessionType enum
SESSION_TYPES = {}
for session_type in SessionType:
    if session_type == SessionType.UNKNOWN:
        continue
    SESSION_TYPES[session_type.name] = {
        "name": session_type.full_display_name,
        "default": DEFAULT_DESIRED_SESSIONS.get(session_type, False)
    }

from grab_stats import get_stats_summary
from activity_emitter import get_all_activity, clear_all_activity

app = Flask(__name__)

# Session cookie hygiene. The cookie only carries the verified identity so the UI
# can show who is signed in; it is not the authentication mechanism (the JWT on
# every request is), but there is no reason to let JavaScript read it.
def _resolve_secret_key(env=None) -> str:
    """
    Resolve the Flask session secret key.

    Precedence: MOTORSPORTARR_SECRET_KEY -> MOTOGPARR_SECRET_KEY (legacy)
    -> AUTH_AUDIENCE -> ephemeral random hex key.
    """
    env = os.environ if env is None else env
    secret = (
        env.get("MOTORSPORTARR_SECRET_KEY")
        or env.get("MOTOGPARR_SECRET_KEY")
        or env.get("AUTH_AUDIENCE")
        or ""
    )
    if not secret:
        # Ephemeral: a restart invalidates sessions. That is acceptable because the
        # session is only a display concern, and it avoids shipping a fixed default
        # secret that would be the same in every deployment.
        import secrets as _secrets
        secret = _secrets.token_hex(32)
    return secret


def _resolve_cookie_secure(env=None) -> bool:
    env = os.environ if env is None else env
    raw = (
        env.get("MOTORSPORTARR_COOKIE_SECURE")
        or env.get("MOTOGPARR_COOKIE_SECURE")
        or "0"
    )
    return raw == "1"


_secret = _resolve_secret_key()
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Not Secure-by-default: the app is served over http inside the LAN and
    # Cloudflare terminates TLS outside it, so a Secure cookie would simply not
    # be sent back by the LAN browser. Set MOTORSPORTARR_COOKIE_SECURE=1 if this
    # container is ONLY ever reached through Cloudflare.
    SESSION_COOKIE_SECURE=_resolve_cookie_secure(),
)

# The UI is same-origin. A wildcard here meant any page in any browser that could
# reach this host could read /app-config (Plex token, API keys) cross-origin, so
# there is deliberately no Access-Control-Allow-Origin header at all.
@app.after_request
def remember_series(response):
    """
    Make an explicit ``?series=`` outlive the link that carried it.

    Every page link already names its series, but a browser fetch or a form
    POST to a route that takes no query string would otherwise fall back to the
    default series - so a settings save or a diagnostics call from the F1 tab
    would quietly describe the wrong championship. The cookie is the only
    channel those requests have. It is validated against ``active_series()``
    on every read, so a stale or hand-edited value cannot select a series the
    install does not follow.
    """
    requested = (request.args.get("series") or "").strip().lower()
    if requested and requested in _active_series_keys():
        response.set_cookie(
            SERIES_COOKIE, requested, max_age=60 * 60 * 24 * 365,
            samesite="Lax", httponly=False,
        )
    return response


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    # Report-only on purpose (Slice 5): the templates still use inline
    # scripts and styles, so an enforcing CSP would break the UI. This makes
    # the browser log what *would* be blocked, which is the evidence needed
    # to tighten it later - it must not be "fixed" to enforcing without
    # removing the inline handlers first, and it must not quietly gain
    # 'unsafe-inline' for scripts either, or it stops reporting the thing it
    # exists to measure. Static/media allowances match what the UI loads.
    response.headers.setdefault(
        "Content-Security-Policy-Report-Only",
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self'",
    )
    return response


def _install_auth(flask_app):
    """Attach the Cloudflare Access guard. Fail loudly, never silently open."""
    from utils.web_auth import install as install_auth_guard

    try:
        return install_auth_guard(flask_app)
    except Exception as exc:  # noqa: BLE001 - a broken guard must not mean no guard
        print("=" * 66)
        print("  ❌ Authentication could not be initialised:")
        print(f"     {exc}")
        print("  The WebUI will refuse every request until this is fixed.")
        print("=" * 66)
        return install_auth_guard(
            flask_app,
            gate=__import__("utils.web_auth", fromlist=["AuthGate"]).AuthGate(
                mode="cloudflare", config_error=str(exc)
            ),
        )


AUTH_GATE = _install_auth(app)

# Browser-write origin check (utils/origin_guard.py). Installed after auth
# so an unauthenticated request is refused as 401 before the origin question
# is asked. No Cloudflare configuration is involved: Origin/Referer pass
# through the tunnel untouched, and this is decided entirely in this process.
from utils.origin_guard import install_origin_guard

install_origin_guard(app)


@app.context_processor
def inject_auth_context():
    """Expose who is signed in (and whether auth is off) to every template."""
    from flask import session as _session
    from utils.web_auth import SESSION_EMAIL_KEY

    return {
        "identity_email": _session.get(SESSION_EMAIL_KEY),
        "auth_disabled": getattr(AUTH_GATE, "mode", "none") == "none",
    }


def _series_nav():
    """
    What ``base.html`` needs to draw the series switcher.

    Every page renders it, so this is one function rather than an argument
    threaded through a dozen ``render_template`` calls: a page that forgot to
    pass it would show the *other* championship's name in the header while
    displaying this one's data, which is the precise confusion the switcher
    exists to prevent.
    """
    context = series_context()
    return {
        "series_key": context["key"],
        "series_spec": context["spec"],
        "series_options": [
            {
                "key": spec.key,
                "name": spec.display_name,
                "url": _series_url(spec.key),
                "current": spec.key == context["key"],
            }
            for spec in context["active"]
        ],
        "series_cookie": SERIES_COOKIE,
        # Only shown when there is a choice to make. An install that follows one
        # championship keeps the single-series UI it has always had.
        "series_switcher": len(context["active"]) > 1,
    }


def _series_url(key: str) -> str:
    """This page, for another series: same path, same query, new series."""
    args = request.args.to_dict(flat=True)
    args["series"] = key
    query = "&".join(
        f"{quote(str(name))}={quote(str(value))}"
        for name, value in sorted(args.items())
        if value != ""
    )
    return f"{request.path}?{query}" if query else request.path


@app.context_processor
def inject_series_context():
    return _series_nav()
# The one background job (grabber/overlay) and its status. The manager owns
# the singleton reservation, the bounded output buffer and the exit/timeout
# bookkeeping; command_output stays as the plain dict templates and /status
# render. See utils/webui_jobs.py for why this is process-local on purpose.
jobs = JobManager()
command_output = jobs.state


# =============================================================================
# THE SERIES CONTEXT
# =============================================================================
#
# One process serves every active series. That is the whole shape of this app
# and it is the reason the WebUI **cannot** use ``motogp_common``'s bound
# vocabulary for a request: the binding is the default series (see
# series/binding.py), so every F1 page would be rendered with MotoGP's session
# names, MotoGP's element numbers and MotoGP's library.
#
# So a request names a series - ``?series=f1``, or the ``msportarr_series``
# cookie the switcher sets - and everything below resolves through
# ``runtime(key)``: the enums, the tables, the calendar prefix, the paths and
# the state filenames. Nothing in a handler may import a name from
# ``motogp_common`` that is *vocabulary*; those module-level names exist for the
# pipeline (where one process really is one series) and for backwards
# compatibility, not for rendering.
#
# The default series is what "no series named" means, which keeps an existing
# MotoGP-only install byte-identical to what it was before the hybrid.

#: Cookie the series switcher sets, so a navigation between tabs stays on the
#: championship the user chose. Not a security boundary - it only selects which
#: of the *configured* series a page renders - and it is re-validated on every
#: request against ``active_series()``.
SERIES_COOKIE = "msportarr_series"


def _active_series_specs() -> list:
    """Every series this install actually follows, in declaration order."""
    from utils.config_loader import active_series

    return list(active_series())


def _active_series_keys() -> list:
    return [spec.key for spec in _active_series_specs()]


def series_context() -> dict:
    """
    The series this request is for, plus what the switcher needs to render.

    Read order: an explicit ``?series=`` (a link, a bookmark, a fetch), then the
    cookie the switcher set, then the default series. An unknown or inactive key
    is *not* obeyed - it falls back to the default rather than rendering a
    championship nobody configured, which would otherwise show an empty
    dashboard and look like data loss.
    """
    active = _active_series_keys()
    requested = (request.args.get("series") or "").strip().lower()
    if not requested:
        requested = (request.cookies.get(SERIES_COOKIE) or "").strip().lower()

    key = requested if requested in active else DEFAULT_SERIES
    return {
        "key": key,
        "spec": _spec(key),
        "active": [_spec(k) for k in active],
        # True when the user explicitly asked for this series, which is when
        # the answer should be remembered in the cookie rather than re-derived.
        "explicit": bool(requested) and requested == key,
    }


def _request_series() -> str:
    """The series key this request is for. The one helper routes call."""
    return series_context()["key"]


def _spec(series: Optional[str] = None):
    """A series declaration by key, defaulting to the request/default series."""
    from series import get_series

    return get_series(series or DEFAULT_SERIES)


@lru_cache(maxsize=None)
def _runtime_for(series: str):
    """
    The cached runtime for one series.

    Cached because building it materialises an IntEnum per series, and a page
    render asks for it repeatedly (session names, class labels, element
    numbers). Keyed on the series so two series' enums never share a cache
    slot - sharing one would make F1 sessions answer to MotoGP's names.
    """
    return series_runtime(series)


def series_vocabulary(series: Optional[str] = None) -> dict:
    """
    The session vocabulary a series' UI renders from.

    Shape:
        ``types``    member name -> {"name", "default", "class", "short"}
        ``groups``   class display name -> [member name, ...] in running order
        ``session_type``  the series' own enum, for names -> members
    """
    key = series or DEFAULT_SERIES
    rt = _runtime_for(key)

    types = {}
    groups = {}
    for class_spec in rt.series.classes:
        groups[class_spec.display_name] = []

    for member in rt.session_type:
        if member is rt.session_type.UNKNOWN:
            continue
        race_class = rt.session_race_class.get(member)
        types[member.name] = {
            "name": rt.session_display_names.get(member, member.name),
            "short": rt.session_short_names.get(member, member.name),
            "default": bool(rt.default_desired_sessions.get(member, False)),
            "class": race_class.folder_key if race_class is not None else "",
        }
        if race_class is not None:
            label = race_class.display_name
        else:
            label = rt.series.display_name
        groups.setdefault(label, []).append(member.name)

    return {"types": types, "groups": groups, "session_type": rt.session_type}


def library_root(series: Optional[str] = None) -> str:
    """The library root of one series. The only correct way to reach it."""
    return get_series_library_path(series or DEFAULT_SERIES)


def downloads_root(series: Optional[str] = None) -> str:
    """Where one series' completed downloads land."""
    return get_series_downloads_path(series or DEFAULT_SERIES)


def state_file(name: str, series: Optional[str] = None) -> Path:
    """
    A namespaced ``/config`` file for one series.

    The binding-based ``state_path()`` above is right for the pipeline and wrong
    here for the same reason as everything else in this section: one process
    reads two series' state.
    """
    from utils.state_files import state_path as _state_path

    return _state_path(CONFIG_DIR, name, series=series or DEFAULT_SERIES)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

#: Timetable display labels keyed by the suffix of a calendar session key
#: (e.g. "motogp_qualifying" -> "qualifying"). Kept separate from
#: SessionType.display_name because CALENDAR_KEY_TO_SESSION maps "*_qualifying"
#: to the Q2 enum member ("Qualifying 2"), whereas in the timetable it
#: represents the whole Q1+Q2 qualifying slot.
CALENDAR_SESSION_LABELS = {
    "fp1": ("FP1", "Free Practice 1"),
    "practice": ("Practice", "Practice"),
    "fp2": ("FP2", "Free Practice 2"),
    "fp3": ("FP3", "Free Practice 3"),
    "qualifying": ("Qualifying", "Qualifying"),
    "q1": ("Q1", "Qualifying 1"),
    "q2": ("Q2", "Qualifying 2"),
    "sprint_qualifying": ("Sprint Qualifying", "Sprint Qualifying"),
    "sprint": ("Sprint", "Sprint"),
    "warmup": ("Warm Up", "Warm Up"),
    "race": ("Race", "Race"),
    "press_conference": ("Press Conference", "Pre-event Press Conference"),
    "chequered_flag": ("Chequered Flag", "Chequered Flag"),
}

_DAY_NAMES_IN_ORDER = CALENDAR_DAYS


def _parse_hhmm(value: str):
    """Parse 'HH:MM' into (hour, minute), or None on malformed input."""
    if not value or not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _parse_utc_offset_minutes(utc_offset: str):
    """Parse '+03:00' / '-05:00' into signed integer minutes, or None."""
    if not utc_offset or not isinstance(utc_offset, str):
        return None
    match = re.match(r"^([+-])(\d{2}):(\d{2})$", utc_offset.strip())
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    return sign * (int(match.group(2)) * 60 + int(match.group(3)))


def _session_utc_bounds(day_date, day_name: str, info: dict, offset_mins):
    """
    Resolve (start_utc, end_utc) for a single calendar session entry.

    Calendars group sessions under track-local days ("friday", "saturday",
    "sunday"), so a morning session in Australia (09:00 +11:00 on Friday) has
    `"gmt": "22:00"` with no `"gmt_day"` field - combining that `"gmt"` with
    Friday's date puts the session 24 hours late on Friday night UTC instead of
    Thursday night UTC. Anchoring on `day_date + local + utc_offset` keeps the
    exact instant across midnight boundaries, falling back to `gmt` only when
    `local`/`utc_offset` are absent or disagree (synthetic test fixtures).
    """
    try:
        duration = int(info.get("duration_mins") or 45)
    except (TypeError, ValueError):
        duration = 45

    local_hm = _parse_hhmm(info.get("local"))
    gmt_hm = _parse_hhmm(info.get("gmt"))

    if local_hm is not None and offset_mins is not None:
        expected_gmt_mins = (local_hm[0] * 60 + local_hm[1] - offset_mins) % 1440
        gmt_matches = (
            gmt_hm is None or (gmt_hm[0] * 60 + gmt_hm[1]) == expected_gmt_mins
        )
        if gmt_matches:
            local_tz = timezone(timedelta(minutes=offset_mins))
            local_dt = datetime(
                day_date.year, day_date.month, day_date.day,
                local_hm[0], local_hm[1], tzinfo=local_tz,
            )
            start_utc = local_dt.astimezone(timezone.utc)
            return start_utc, start_utc + timedelta(minutes=duration)

    if gmt_hm is not None:
        target_date = day_date
        gmt_day = str(info.get("gmt_day") or "").lower()
        if gmt_day in _DAY_NAMES_IN_ORDER and day_name in _DAY_NAMES_IN_ORDER:
            delta = _DAY_NAMES_IN_ORDER.index(gmt_day) - _DAY_NAMES_IN_ORDER.index(day_name)
            target_date = day_date + timedelta(days=delta)
        start_utc = datetime(
            target_date.year, target_date.month, target_date.day,
            gmt_hm[0], gmt_hm[1], tzinfo=timezone.utc,
        )
        return start_utc, start_utc + timedelta(minutes=duration)

    return None, None


def _describe_calendar_session_key(key: str, session_type,
                                   series: Optional[str] = None):
    """Return (class_key, class_label, short_name, full_label) for a calendar key."""
    series_key = series or DEFAULT_SERIES
    rt = _runtime_for(series_key)
    spec = _spec(series_key)

    if session_type is not None and session_type.race_class is not None:
        race_class = session_type.race_class
        class_key = race_class.folder_key
        class_label = race_class.display_name
    else:
        # Resolved through the *series'* enum, never MotoGP's: parsing an F1
        # prefix with MotoGP's RaceClass returns None and the fallback would
        # then label the session "f1" in the wrong case and wording.
        prefix, _, _ = key.partition("_")
        try:
            parsed_class = rt.race_class(prefix.upper())
        except ValueError:
            parsed_class = None
        if parsed_class is not None:
            class_key = rt.class_of(prefix.lower()).folder_key
            class_label = rt.class_of(prefix.lower()).display_name
        else:
            class_key = prefix or series_key
            class_label = prefix.upper() if prefix else spec.display_name

    slug = key.split("_", 1)[1] if "_" in key else key
    if slug in CALENDAR_SESSION_LABELS:
        short_name, name = CALENDAR_SESSION_LABELS[slug]
    elif session_type is not None:
        short_name = name = rt.session_display_names.get(
            session_type, session_type.display_name
        )
    else:
        short_name = name = slug.replace("_", " ").title()

    return class_key, class_label, short_name, name


def build_round_schedule(round_info: dict, calendar: dict = None,
                        series: Optional[str] = None) -> dict:
    """
    Build per-day timed session lists with exact UTC instants for the Calendar UI.

    Reads directly from `round_info["sessions"][day]` rather than a
    `CALENDAR_KEY_TO_DAY` table: from 2027 `motogp_fp2` moves from Saturday to
    Friday and `motogp_warmup` is dropped, so the day structure in the JSON must
    win without code assumptions about which day a key belongs to.

    ``series`` selects the vocabulary the session keys are read with, which is
    not cosmetic: an F1 round's keys (``f1_fp1``, ``f1_race``) resolve to
    nothing in MotoGP's calendar-key table, so the schedule would come back
    empty and the Calendar tab would show a meeting with no sessions.
    """
    series_key = series or DEFAULT_SERIES
    rt = _runtime_for(series_key)

    if not round_info or not is_round_active(round_info):
        return {}

    raw_sessions = round_info.get("sessions")
    if not isinstance(raw_sessions, dict) or not raw_sessions:
        return {}

    race_date_str = round_info.get("date")
    weekend_start_str = round_info.get("weekend_start")
    if not race_date_str:
        return {}

    try:
        race_date = datetime.strptime(race_date_str, "%Y-%m-%d").date()
        weekend_start = (
            datetime.strptime(weekend_start_str, "%Y-%m-%d").date()
            if weekend_start_str
            else race_date - timedelta(days=2)
        )
    except ValueError:
        return {}

    day_dates = {
        "thursday": weekend_start - timedelta(days=1),
        "friday": weekend_start,
        "saturday": weekend_start + timedelta(days=1),
        "sunday": race_date,
    }
    offset_mins = _parse_utc_offset_minutes(round_info.get("utc_offset"))
    allowed_classes = set(get_round_classes(round_info, calendar))

    schedule = {}
    ordered_days = [d for d in _DAY_NAMES_IN_ORDER if d in raw_sessions]
    ordered_days.extend(d for d in raw_sessions if d not in ordered_days)

    for day_name in ordered_days:
        day_entries = raw_sessions.get(day_name)
        day_date = day_dates.get(day_name)
        if not isinstance(day_entries, dict) or day_date is None:
            continue

        built_sessions = []
        for key, info in day_entries.items():
            if not isinstance(info, dict):
                continue

            session_type = rt.calendar_key_to_session.get(key)
            if session_type is not None:
                if not round_runs_session(round_info, session_type, calendar,
                                          series=series_key):
                    continue
            else:
                # A key the series' table does not know: keep it only when its
                # class prefix is racing at this round. Hardcoding MotoGP's
                # three class names here meant an unknown F1 key was always
                # dropped, and the series' own class list is right there.
                class_prefix, _, _ = key.partition("_")
                if class_prefix in _runtime_for(series_key)._race_class_by_key \
                        and class_prefix not in allowed_classes:
                    continue

            start_utc, end_utc = _session_utc_bounds(day_date, day_name, info, offset_mins)
            if start_utc is None or end_utc is None:
                continue

            class_key, class_label, short_name, full_label = _describe_calendar_session_key(
                key, session_type, series=series_key
            )
            duration_mins = int((end_utc - start_utc).total_seconds() // 60)

            parts_out = []
            for part in info.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                part_start, part_end = _session_utc_bounds(
                    day_date, day_name, part, offset_mins
                )
                if part_start is None or part_end is None:
                    continue
                part_key = str(part.get("key") or "").lower()
                part_short = CALENDAR_SESSION_LABELS.get(
                    part_key, (part_key.upper() or "Part", "")
                )[0]
                parts_out.append({
                    "key": part_key,
                    "label": part_short,
                    "start_utc": part_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end_utc": part_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "duration_mins": int((part_end - part_start).total_seconds() // 60),
                })

            entry_out = {
                "key": key,
                "class": class_key,
                "class_label": class_label,
                "short_name": short_name,
                "name": full_label,
                "full_name": f"{class_label} {short_name}",
                "start_utc": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_utc": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "local": info.get("local"),
                "gmt": info.get("gmt"),
                "duration_mins": duration_mins,
            }
            if parts_out:
                entry_out["parts"] = parts_out

            built_sessions.append(entry_out)

        if built_sessions:
            built_sessions.sort(key=lambda item: item["start_utc"])
            schedule[day_name] = {
                "date": day_date.strftime("%Y-%m-%d"),
                "sessions": built_sessions,
            }

    return schedule


def default_calendar_year(seasons: list = None, series: Optional[str] = None) -> int:
    """
    Pick the season `/calendar` opens on when no `?year=` query parameter is given.

    Prefers an explicit `grabber.current_season` in settings.yml/env if that
    calendar exists, then the current calendar year, then `get_current_season()`,
    then the newest discovered season - so dropping in next year's calendar file
    never leaves the UI opening on an old season.
    """
    series_key = series or DEFAULT_SERIES
    available = list(seasons) if seasons is not None else discover_seasons(series_key)
    if not available:
        return get_current_season()

    if has_setting("grabber", "current_season"):
        configured = get_current_season()
        if configured in available:
            return configured

    now_year = datetime.now().year
    if now_year in available:
        return now_year

    fallback = get_current_season()
    if fallback in available:
        return fallback

    return available[-1]


def load_calendar_data(year: int, series: Optional[str] = None) -> dict:
    """
    Load one series' calendar for a season.

    Named explicitly everywhere in this module: ``motogp_common.load_calendar``
    falls back to the process binding, which is the default series, and F1
    would therefore be rendered from MotoGP's calendar - the same rounds, the
    same venues and no F1 session keys at all.
    """
    return load_calendar(year, series=series or DEFAULT_SERIES) or {"rounds": []}


def get_rounds_for_season(year: int, series: Optional[str] = None) -> list:
    """Get list of rounds with venue names and timed schedules for a season."""
    series_key = series or DEFAULT_SERIES
    calendar = load_calendar_data(year, series_key)
    rounds = []
    for r in calendar.get("rounds", []):
        race_date_str = r.get("date")
        weekend_start_str = r.get("weekend_start")
        weekend_end_str = r.get("weekend_end") or race_date_str
        if not weekend_start_str and race_date_str:
            try:
                weekend_start_str = (
                    datetime.strptime(race_date_str, "%Y-%m-%d").date()
                    - timedelta(days=2)
                ).strftime("%Y-%m-%d")
            except ValueError:
                weekend_start_str = None

        rounds.append({
            "number": r.get("round"),
            "venue": r.get("venue"),
            "date": race_date_str,
            "weekend_start": weekend_start_str,
            "weekend_end": weekend_end_str,
            "status": get_round_status(r),
            "status_note": r.get("status_note"),
            "active": is_round_active(r),
            "classes": get_round_classes(r, calendar),
            "format": get_weekend_format(r, calendar),
            "schedule": build_round_schedule(r, calendar, series=series_key),
        })
    return rounds


def get_next_race_info(series: Optional[str] = None) -> dict:
    """
    Get information about the next race weekend for one series.

    Both series have a next race, and they are almost never the same meeting, so
    this is asked per series rather than once per page render.
    """
    today = datetime.now().date()
    
    for year in [today.year, today.year + 1]:
        calendar = load_calendar_data(year, series)
        
        for r in calendar.get("rounds", []):
            race_date_str = r.get("date")
            weekend_start_str = r.get("weekend_start")
            
            if not race_date_str:
                continue
            
            try:
                race_date = datetime.strptime(race_date_str, "%Y-%m-%d").date()
                weekend_start = (
                    datetime.strptime(weekend_start_str, "%Y-%m-%d").date()
                    if weekend_start_str
                    else race_date - timedelta(days=2)
                )
                weekend_end = race_date + timedelta(days=1)
                
                # Format date as DD-MM-YYYY
                formatted_date = race_date.strftime("%d-%m-%Y")
                
                if weekend_start <= today <= weekend_end:
                    return {
                        "round": r.get("round"),
                        "venue": r.get("venue"),
                        "date": formatted_date,
                        "flag": r.get("flag", ""),
                        "status": "now",
                        "countdown": "Race Weekend Now!",
                        "days": 0
                    }
                
                if weekend_start > today:
                    days_until = (weekend_start - today).days
                    
                    if days_until == 1:
                        countdown = "Tomorrow!"
                    elif days_until < 7:
                        countdown = f"{days_until} days"
                    elif days_until < 14:
                        countdown = f"{days_until} days (next week)"
                    else:
                        weeks = days_until // 7
                        countdown = f"{days_until} days (~{weeks} weeks)"
                    
                    return {
                        "round": r.get("round"),
                        "venue": r.get("venue"),
                        "date": formatted_date,
                        "flag": r.get("flag", ""),
                        "status": "upcoming",
                        "countdown": countdown,
                        "days": days_until
                    }
            except ValueError:
                continue
    
    return {
        "round": None,
        "venue": "No upcoming races",
        "date": None,
        "flag": "",
        "status": "none",
        "countdown": "Season ended",
        "days": None
    }



def migrate_max_grabs_per_run() -> None:
    """
    One-time migration of max_grabs_per_run out of webui_settings.json.

    settings.yml owns grabber.* (the grabber reads config_loader, not this
    JSON file), so a value saved only in the JSON was never actually used by
    the grabber while the UI displayed it.

    Idempotent and never loses a value:

      * settings.yml lacks grabber.max_grabs_per_run  -> write the JSON's
        value there once, then drop the key from the JSON;
      * settings.yml already has it                  -> the JSON copy is
        stale; drop it without overwriting (settings.yml wins).
    """
    if not WEBUI_CONFIG_FILE.exists():
        return
    try:
        with open(WEBUI_CONFIG_FILE, "r") as f:
            saved = json.load(f)
    except Exception:
        # Corrupt JSON: nothing to migrate; load_webui_settings warns instead.
        return
    if not isinstance(saved, dict) or "max_grabs_per_run" not in saved:
        return

    value = saved["max_grabs_per_run"]

    if not has_setting("grabber", "max_grabs_per_run"):
        ok = update_settings({"grabber": {"max_grabs_per_run": value}})
        if not ok:
            logger.warning(
                "Could not migrate max_grabs_per_run to %s; the value stays "
                "in %s until /config is writable",
                SETTINGS_FILE, WEBUI_CONFIG_FILE,
            )
            return

    del saved["max_grabs_per_run"]
    try:
        # Write under the store lock without re-taking it via
        # save_webui_settings (flock on a second fd would deadlock against
        # our own lock), so a concurrent /settings save cannot resurrect
        # the migrated key or be overwritten by this stale snapshot.
        with file_lock(WEBUI_CONFIG_FILE):
            # Re-read inside the lock: the value may have changed since the
            # unlocked read above; only the key removal is applied.
            current = read_json(WEBUI_CONFIG_FILE, {}, log=logger)
            if isinstance(current, dict):
                current.pop("max_grabs_per_run", None)
                atomic_write_json(WEBUI_CONFIG_FILE, current)
    except OSError as exc:  # noqa: BLE001 - report, never silently keep a duplicate
        logger.warning(
            "Could not drop max_grabs_per_run from %s: %s",
            WEBUI_CONFIG_FILE, exc,
        )


def load_webui_settings() -> dict:
    """Load WebUI settings from config file.

    webui_settings.json owns only the desired sessions. grabber settings
    (max_grabs_per_run and the rest of grabber.*) live in settings.yml and are
    read by the grabber via config_loader.get_max_grabs_per_run(); they are
    merged into the UI presentation at read time, never stored in this file.

    settings.yml may be edited outside this process, so it is re-read on
    every load - there is no stale JSON shadowing it any more.

    A corrupt or missing webui_settings.json logs a warning naming the path
    and falls back to default sessions; sessions still work.
    """
    reload_settings()  # settings.yml can change under this process
    migrate_max_grabs_per_run()

    defaults = {
        "sessions": {k: v["default"] for k, v in SESSION_TYPES.items()}
    }

    if WEBUI_CONFIG_FILE.exists():
        try:
            with open(WEBUI_CONFIG_FILE, "r") as f:
                saved = json.load(f)
        except Exception as exc:  # noqa: BLE001 - never silently default
            logger.warning(
                "Could not read %s: %s; falling back to default sessions",
                WEBUI_CONFIG_FILE, exc,
            )
        else:
            if isinstance(saved, dict):
                defaults.update(saved)
    else:
        logger.warning(
            "%s not found; falling back to default sessions",
            WEBUI_CONFIG_FILE,
        )

    return defaults


def save_webui_settings(settings):
    """Save WebUI settings to config file.

    Callers must not pass max_grabs_per_run: it is a grabber setting and
    belongs in settings.yml, not in this file.

    The write takes the store's sidecar lock and replaces the file
    atomically (``utils.json_store``): concurrent settings requests can no
    longer interleave on the same file, and the grabber reading this file
    (``motogp_common``) sees the old settings or the new ones, never a
    truncated mixture. Callers submit the complete settings document, so a
    locked whole-file write is the right transaction here; failures raise
    OSError, which the /settings route already turns into a 500.
    """
    write_json(WEBUI_CONFIG_FILE, settings)


# =============================================================================
# LIBRARY SCANNING
# =============================================================================

def scan_library_for_season(year: int, series: Optional[str] = None) -> dict:
    """
    Scan the library to find existing sessions for a season.

    Instrumented, not optimised (Candidate D): the home page and several routes
    walk this per request, and the review did not establish whether that is
    slow. The wrapper records duration and scope so ``/diagnostics`` can answer
    the question with numbers before anyone builds an index "for performance".
    """
    return scan_metrics.timed(
        "library-season", _scan_library_for_season, year, series
    )


def _scan_library_for_season(year: int, series: Optional[str] = None) -> dict:
    """
    The actual walk; called through the instrumented wrapper above.

    The prefix parser is the series' own. Parsing an F1 folder with MotoGP's
    numbers does not fail loudly - it resolves an element number to a
    *different real session* - so the scan would report F1's library as holding
    MotoGP's races and every coverage count would be wrong.
    """
    series_key = series or DEFAULT_SERIES
    rt = _runtime_for(series_key)
    season_path = os.path.join(library_root(series_key), str(year))
    
    if not os.path.exists(season_path):
        return {}
    
    existing = {}
    
    for round_folder in os.listdir(season_path):
        round_path = os.path.join(season_path, round_folder)
        
        if not os.path.isdir(round_path):
            continue
        
        round_match = re.match(r'Round\s*(\d+)', round_folder)
        if not round_match:
            continue
        
        round_num = int(round_match.group(1))
        existing[round_num] = {}
        
        for session_folder in os.listdir(round_path):
            session_path = os.path.join(round_path, session_folder)
            
            if not os.path.isdir(session_path):
                continue
            
            parsed = rt.parse_prefix(session_folder)
            if parsed:
                folder_year, folder_round, session_type = parsed
                if folder_year == year and folder_round == round_num:
                    existing[round_num][session_type] = session_path
    
    return existing


def _desired_session_set(settings: dict, series: Optional[str] = None) -> set:
    """
    The session types the user wants, from the saved WebUI settings.

    Falls back to the defaults when nothing is enabled. Shared by the Download
    tab and the home page so "desired" means one thing: if the home page used
    its own answer, the two tabs would disagree about which sessions count.

    ``series`` picks the enum the names are looked up in. A member name that
    belongs to another series is skipped rather than resolved: the two enums
    are unrelated classes, and ``SessionType["RACE"]`` in a process bound to
    MotoGP would raise while F1's ``RACE`` is perfectly valid.
    """
    series_key = series or DEFAULT_SERIES
    rt = _runtime_for(series_key)

    desired_set = set()
    for key, enabled in settings.get("sessions", {}).items():
        if enabled:
            try:
                desired_set.add(rt.session_type[key])
            except KeyError:
                pass

    if not desired_set:
        desired_set = {
            member for member, enabled in rt.default_desired_sessions.items()
            if enabled
        }
    return desired_set


def _session_settings(settings: dict) -> dict:
    """
    The desired flags for the series this request is for.

    ``webui_settings.<series>.json`` is one document per series, exactly like
    every other shared store, so a page needs to know which series it is
    reading before it can say which sessions are wanted. The default series
    alone falls back to a shared ``sessions`` block, so an install that has
    never switched series reads exactly the file it always did.
    """
    key = _request_series()

    if key != DEFAULT_SERIES and f"{key}_sessions" in settings:
        return settings[f"{key}_sessions"]
    return settings.get("sessions", {})


def _load_grabber_state(series: Optional[str] = None) -> dict:
    """
    Read one series' grabber state, tolerating absence and half-written files.

    The grabber writes this file without a lock while the WebUI may read it,
    so a torn read is expected rather than exceptional. A failed read leaves
    the home page empty (and logs a warning) instead of 500-ing the whole tab
    because a background job was mid-save.
    """
    grabber_state = state_file("grabber_state", series)
    if not grabber_state.exists():
        return {}
    try:
        with open(grabber_state, "r") as handle:
            state = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Could not read {grabber_state}: {exc}")
        return {}
    return state if isinstance(state, dict) else {}


def get_home_summary(settings: dict, series: Optional[str] = None) -> dict:
    """
    Everything the home tab renders, assembled once per page load.

    Static by design: the user chose page-load rendering over polling, so
    this is called from index() and nowhere else. The only browser-side
    fetches left are the health chips, which hit routes that already exist.
    """
    series_key = series or DEFAULT_SERIES
    rt = _runtime_for(series_key)
    vocabulary = {"runtime": rt, "series": series_key,
                  "session_to_calendar_key": rt.session_to_calendar_key}

    now = datetime.now()  # process clock is UTC (see entrypoint.sh)
    season = get_current_season()
    calendar = load_calendar_data(season, series_key)
    state = _load_grabber_state(series_key)
    grabbed = state.get("grabbed") or {}
    failed = state.get("failed") or {}
    desired = _desired_session_set(settings, series_key)
    delay = get_search_delay_hours()

    weekend_round = home_summary.find_weekend_round(calendar, now.date())
    weekend = None
    library_round = {}
    if weekend_round:
        round_num = weekend_round.get("round")
        library_round = scan_library_for_season(season, series_key).get(round_num, {})
        weekend = {
            "round": round_num,
            "venue": weekend_round.get("venue", ""),
            "flag": weekend_round.get("flag", ""),
            "next": home_summary.next_session(
                weekend_round, calendar, desired, now, delay, vocabulary
            ),
            "progress": home_summary.weekend_progress(
                weekend_round, calendar, season, desired, now,
                library_round, grabbed, failed, vocabulary
            ),
        }

    # Only seasons that could feed the hero card: scanning every season ever
    # mentioned in state would make the home tab slower every year, for
    # history the card will never show.
    seasons = home_summary.seasons_with_scheduled_grabs(grabbed)
    library_by_season = {s: scan_library_for_season(s, series_key) for s in seasons}
    calendars_by_season = {s: load_calendar_data(s, series_key) for s in seasons}

    from utils.submission_guard import intent_path, load_intents
    try:
        pending_submissions = load_intents(intent_path(CONFIG_DIR))
        pending_error = None
    except (OSError, ValueError) as exc:
        logger.error("Cannot read submission reservations: %s", exc)
        pending_submissions = {}
        pending_error = "Submission reservations are unreadable; downloads are blocked until the file is repaired."

    background = _background_health(now, bool(weekend_round), series_key)
    plan = read_json(CONFIG_DIR / f"scheduler_plan.{series_key}.json", {},
                     log=logger)
    if not isinstance(plan, dict):
        plan = {}
    try:
        planned_at = datetime.fromisoformat(plan.get("at", ""))
        expected_minutes = background["components"]["scheduler"]["expected_minutes"]
        if (background["components"]["scheduler"]["state"] != "live"
                or (now - planned_at).total_seconds() > (expected_minutes * 2 + 60) * 60):
            plan = {}
    except (ValueError, TypeError):
        plan = {}

    return {
        "scheduler_plan": plan,
        "pending_submissions": pending_submissions,
        "pending_submissions_error": pending_error,
        "latest": home_summary.latest_scheduled_addition(
            grabbed,
            get_all_activity(series=series_key).get("history", []),
            library_by_season,
            calendars_by_season,
            vocabulary,
        ),
        "weekend": weekend,
        "health": {
            **home_summary.scheduler_health(
                state.get("last_scheduled_check"), now, bool(weekend_round)
            ),
            "library_ok": os.path.isdir(library_root(series_key)),
        },
        "background": background,
        "season": season,
        "series": series_key,
    }


def _background_health(now: datetime, in_weekend: bool,
                       series: Optional[str] = None) -> dict:
    """
    Scheduler-loop and watcher-thread progress, from the on-disk heartbeat.

    Separate from ``health.scheduler`` above, which is the grabber's own
    ``last_scheduled_check``: that one moves when a scheduled check finishes,
    so an off-weekend week reads as "never run" even though the scheduler is
    sleeping normally. This one answers the different question "is the loop
    still turning", and both are shown because they fail differently.

    No I/O beyond one small JSON read and no network calls - the home tab is
    rendered on load and must not wait on a service to say whether the
    machine is alive.
    """
    try:
        heartbeat = background_health.read_heartbeat(
            CONFIG_DIR, series=series or DEFAULT_SERIES
        )
    except Exception as exc:  # noqa: BLE001 - a broken record must not 500 the page
        logger.warning(f"Could not read the background heartbeat: {exc}")
        heartbeat = {}

    return background_health.background_health(
        heartbeat,
        now,
        in_weekend=in_weekend,
        quiet=background_health.quiet_hours_at(now),
    )


def get_library_status(year: int, round_filter: int = None,
                      series: Optional[str] = None) -> dict:
    """
    Get library status for display.

    ``year`` defaults to the season this series is currently on when the caller
    passes None - the two championships are not necessarily on the same round
    numbering, and F1's calendar may not even have the year the default series
    is on.
    """
    series_key = series or DEFAULT_SERIES
    calendar = load_calendar_data(year, series_key)
    existing = scan_library_for_season(year, series_key)
    settings = load_webui_settings()
    
    desired_set = _desired_session_set(settings, series_key)
    
    result = {
        "rounds": [], 
        "total_grabbed": 0, 
        "total_desired": 0,
        "past_rounds_grabbed": 0,
        "past_rounds_desired": 0,
        "total_in_library": 0
    }
    today = datetime.now().date()
    
    # Count total files in library (all sessions, not just desired)
    for round_num, sessions in existing.items():
        result["total_in_library"] += len(sessions)
    
    for r in calendar.get("rounds", []):
        round_num = r.get("round")
        
        if round_filter and round_num != round_filter:
            continue
        
        # Check if future round (don't skip, just flag it)
        race_date_str = r.get("date", "")
        is_future = False
        if race_date_str:
            try:
                is_future = datetime.strptime(race_date_str, "%Y-%m-%d").date() > today
            except ValueError:
                pass
        
        round_existing = existing.get(round_num, {})
        sessions = []
        grabbed_count = 0
        desired_count = 0
        
        for session_type in sorted(desired_set, key=lambda x: int(x)):
            # Only count sessions this weekend actually runs, otherwise a 2026
            # round would show Sprint Qualifying / FP3 as missing.
            if not round_runs_session(r, session_type, calendar,
                                      series=series_key):
                continue

            exists = session_type in round_existing
            sessions.append({
                "type": session_type,
                "name": session_type.full_display_name,
                "exists": exists,
            })
            desired_count += 1
            if exists:
                grabbed_count += 1
        
        result["rounds"].append({
            "number": round_num,
            "venue": r.get("venue", "Unknown"),
            "date": r.get("date", ""),
            "status": get_round_status(r),
            "active": is_round_active(r),
            "is_future": is_future,
            "sessions": sessions,
            "grabbed_count": grabbed_count,
            "desired_count": desired_count,
        })
        
        # Cancelled/postponed rounds do not count towards the season totals.
        if not is_round_active(r):
            continue

        result["total_grabbed"] += grabbed_count
        result["total_desired"] += desired_count
        
        # Only count past rounds for the "preferred" stats
        if not is_future:
            result["past_rounds_grabbed"] += grabbed_count
            result["past_rounds_desired"] += desired_count
    
    return result


# =============================================================================
# COMMAND RUNNERS
# =============================================================================

def run_job(argv):
    """Worker body for one job whose slot has already been claimed."""
    jobs.run_job(list(argv))


def _job_int(payload, field, required=False):
    value = payload.get(field)
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{field} must be a number")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number")


def _job_bool(payload, field):
    value = payload.get(field, False)
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be true or false")
    return value


def _job_season(payload, series=None, required=False):
    season = _job_int(payload, "season", required=required)
    if season is not None and season not in discover_seasons(series):
        raise ValueError(f"unknown season: {season}")
    return season


def _job_round(payload, season, series=None, required=False):
    round_num = _job_int(payload, "round", required=required)
    if round_num is None:
        return None
    if season is None:
        raise ValueError("round requires a season")
    calendar = load_calendar_data(season, series)
    known = {r.get("round") for r in calendar.get("rounds", [])}
    if known and round_num not in known:
        raise ValueError(f"unknown round {round_num} for season {season}")
    return round_num


def _job_sessions(payload):
    raw = payload.get("sessions")
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise ValueError("sessions must be a non-empty list")
    sessions = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("sessions must be element tokens like 'E82'")
        token = item.strip().upper()
        if not token.startswith("E") or not token[1:].isdigit():
            raise ValueError("sessions must be element tokens like 'E82'")
        number = int(token[1:])
        if not 1 <= number <= 99:
            raise ValueError("session element numbers must be between 1 and 99")
        token = f"E{number:02d}"
        if token not in sessions:
            sessions.append(token)
    return sessions


def _job_scale(payload):
    value = payload.get("scale", 1.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("scale must be a number")
    scale = float(value)
    if not 0.5 <= scale <= 3.0:
        raise ValueError("scale must be between 0.5 and 3.0")
    return scale


#: Fields each /run action may carry. Anything else is rejected rather than
#: ignored: a misspelled field that silently does nothing is how the UI ends
#: up reporting a job that never matched what the user asked for.
_JOB_FIELDS = {
    "status": {"season"},
    "scheduled": {"season", "dry_run", "force"},
    "backfill": {"season", "round", "sessions", "dry_run", "force"},
    "overlay-season": {"season", "scale", "dry_run"},
    "overlay-round": {"season", "round", "scale", "dry_run"},
}


def build_job(payload, series: Optional[str] = None):
    """
    Validate a /run payload and return ``(argv, label)``.

    This replaces the old contract where the browser sent a command string
    and the server split it into argv: a client could pass any grabber flag
    that exists (or several that don't), and a typo produced a subprocess
    usage error instead of a 400. Now the client names an action and typed
    fields; every argv element below is built here, from validated values,
    and nothing the client sent is ever split, joined or shell-interpreted.
    Raises ValueError with a user-facing message for anything unsupported.

    ``series`` is carried into every argv as ``--series`` because the grabber
    and the overlay bind their vocabulary at import, in their own process. It
    is passed explicitly rather than left to the environment so a job started
    from a browser tab cannot inherit whichever series happened to be in the
    process environment - the two queues, calendars and libraries differ.
    """
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    action = payload.get("action")
    if action not in _JOB_FIELDS:
        raise ValueError(
            f"unsupported action: {action!r} "
            f"(expected one of: {', '.join(sorted(_JOB_FIELDS))})"
        )
    unexpected = set(payload) - _JOB_FIELDS[action] - {"action"}
    if unexpected:
        raise ValueError(
            f"unsupported field(s) for {action}: {', '.join(sorted(unexpected))}"
        )

    series_key = series or DEFAULT_SERIES
    grabber = [PYTHON_PATH, str(GRABBER_SCRIPT), "--series", series_key]
    overlay = [PYTHON_PATH, str(OVERLAY_SCRIPT), "--series", series_key]
    label_prefix = f"{series_key}-"

    def display(argv, kind):
        """The command without the interpreter, the script or --series."""
        return f"{label_prefix}{kind} " + " ".join(argv[4:])

    if action == "status":
        argv = grabber + ["status"]
        season = _job_season(payload, series_key)
        if season is not None:
            argv += ["--season", str(season)]
        return argv, display(argv, "grab")

    if action == "scheduled":
        argv = grabber + ["scheduled"]
        season = _job_season(payload, series_key)
        if season is not None:
            argv += ["--season", str(season)]
        if _job_bool(payload, "dry_run"):
            argv.append("--dry-run")
        if _job_bool(payload, "force"):
            argv.append("--force")
        return argv, display(argv, "grab")

    if action == "backfill":
        season = _job_season(payload, series_key, required=True)
        argv = grabber + ["backfill", "--season", str(season)]
        round_num = _job_round(payload, season, series_key)
        if round_num is not None:
            argv += ["--round", str(round_num)]
        sessions = _job_sessions(payload)
        if sessions:
            argv += ["--sessions", ",".join(sessions)]
        if _job_bool(payload, "dry_run"):
            argv.append("--dry-run")
        if _job_bool(payload, "force"):
            argv.append("--force")
        return argv, display(argv, "grab")

    # Overlay actions share scale/dry-run handling.
    season = _job_season(payload, series_key, required=True)
    scale = _job_scale(payload)
    dry_run = _job_bool(payload, "dry_run")

    if action == "overlay-season":
        argv = overlay + ["apply-season", str(season), "--scale", str(scale)]
        if dry_run:
            argv.append("--dry-run")
        return argv, display(argv, "overlay")

    # overlay-round: the folder is resolved from the calendar server-side,
    # so a renumbered round (e.g. Qatar moving to Round 20) still resolves,
    # and the client never supplies a filesystem path. The calendar is the
    # series' own: F1's round 5 is a different meeting from MotoGP's.
    round_num = _job_round(payload, season, series_key, required=True)
    calendar = load_calendar_data(season, series_key)
    round_info = next(
        (r for r in calendar.get("rounds", []) if r.get("round") == round_num),
        None,
    )
    if round_info is None:
        raise ValueError(f"Round {round_num} not found in calendar")
    folder = round_info.get("folder") or (
        f"Round {round_num:02d} {round_info.get('venue', 'Unknown')}"
    )
    round_path = os.path.join(library_root(series_key), str(season), folder)
    argv = overlay + ["apply-round", round_path, "--scale", str(scale)]
    if dry_run:
        argv.append("--dry-run")
    return argv, display(argv, "overlay")


def _start_job(argv, label):
    """Claim the singleton slot and launch; returns False when busy."""
    if not jobs.try_start(label):
        return False
    thread = threading.Thread(target=run_job, args=(list(argv),), daemon=True)
    thread.start()
    return True


# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route("/")
def index():
    series_key = _request_series()
    vocabulary = series_vocabulary(series_key)
    seasons = discover_seasons(series_key)
    settings = load_webui_settings()
    session_defaults = {k: v["default"] for k, v in vocabulary["types"].items()}
    next_race = get_next_race_info(series_key)
    
    calendar_data = {str(year): get_rounds_for_season(year, series_key)
                     for year in seasons}
    
    return render_template(
        "dashboard.html",
        status=command_output,
        seasons=seasons,
        settings=settings,
        session_types=vocabulary["types"],
        session_groups=vocabulary["groups"],
        session_defaults=session_defaults,
        next_race=next_race,
        calendar_data=calendar_data,
        home=get_home_summary(settings, series_key),
        quick_status_log_lines=get_int("webui", "quick_status_log_lines", 100),
        active_tab="home"
    )


@app.route("/library")
def library():
    series_key = _request_series()
    seasons = discover_seasons(series_key)
    settings = load_webui_settings()
    next_race = get_next_race_info(series_key)
    calendar_data = {str(year): get_rounds_for_season(year, series_key)
                     for year in seasons}
    
    # Build flag data per season
    flag_data = {}
    for year in seasons:
        flag_data[str(year)] = {}
        cal = load_calendar_data(year, series_key)
        for r in cal.get("rounds", []):
            flag_data[str(year)][r.get("round")] = r.get("flag", "")
    
    return render_template(
        "library.html",
        status=command_output,
        seasons=seasons,
        settings=settings,
        next_race=next_race,
        calendar_data=calendar_data,
        flag_data=flag_data,
        current_year=default_calendar_year(seasons, series_key),
        active_tab="download"
    )


@app.route("/settings-page")
def settings_page():
    series_key = _request_series()
    vocabulary = series_vocabulary(series_key)
    seasons = discover_seasons(series_key)
    settings = load_webui_settings()
    settings["max_grabs_per_run"] = get_max_grabs_per_run()
    settings["search_delay_minutes"] = get_search_delay_hours() * 60
    settings["quick_status_log_lines"] = get_int("webui", "quick_status_log_lines", 100)
    session_defaults = {k: v["default"] for k, v in vocabulary["types"].items()}
    next_race = get_next_race_info(series_key)
    calendar_data = {str(year): get_rounds_for_season(year, series_key)
                     for year in seasons}
    
    return render_template(
        "settings.html",
        status=command_output,
        seasons=seasons,
        settings=settings,
        session_types=vocabulary["types"],
        session_groups=vocabulary["groups"],
        session_defaults=session_defaults,
        next_race=next_race,
        calendar_data=calendar_data,
        active_tab="settings"
    )


@app.route("/logs")
def logs_page():
    next_race = get_next_race_info(_request_series())
    
    return render_template(
        "logs.html",
        status=command_output,
        next_race=next_race,
        active_tab="logs"
    )


@app.route("/run", methods=["POST"])
def run():
    """
    Start one typed background job.

    The payload names an action plus typed fields (see build_job); the old
    ``{"command": "..."}`` string contract is gone, so nothing a client sends
    is split into argv. Validation happens before the job slot is claimed, and
    the slot is claimed synchronously here - not inside the worker thread -
    so two simultaneous requests cannot both start a job.
    """
    payload = request.get_json(silent=True)
    try:
        argv, label = build_job(payload, _request_series())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not _start_job(argv, label):
        return jsonify({"error": "Command already running"}), 409

    return jsonify({"status": "started", "command": label})


@app.route("/settings", methods=["GET", "POST"])
def settings_route():
    if request.method == "POST":
        new_settings = request.get_json(silent=True)
        if not isinstance(new_settings, dict):
            return jsonify({"status": "error", "detail": "Expected a JSON object"}), 400

        # max_grabs_per_run is a grabber setting: it lives in settings.yml,
        # which is what the grabber reads. webui_settings.json keeps sessions
        # only, so pop it before writing the JSON store.
        max_grabs = new_settings.pop("max_grabs_per_run", None)

        # The desired-session flags belong to the series the form was rendered
        # for. Writing them into the shared document is how switching series in
        # the form would silently rewrite the other championship's wanted list.
        series_key = _request_series()
        if series_key != DEFAULT_SERIES:
            new_settings = {f"{series_key}_sessions": new_settings.get("sessions", {})}

        # Both writes must succeed before this says "saved". Previously a failed
        # settings.yml write was swallowed and the UI reported success anyway.
        try:
            save_webui_settings(new_settings)
        except OSError as exc:
            return jsonify({
                "status": "error",
                "detail": f"Could not write {WEBUI_CONFIG_FILE.name}: {exc}",
            }), 500

        if max_grabs is not None and not update_settings(
            {"grabber": {"max_grabs_per_run": max_grabs}}
        ):
            return jsonify({
                "status": "error",
                "detail": "Could not write settings.yml; check that /config is writable.",
            }), 500

        return jsonify({"status": "saved"})
    series_key = _request_series()
    settings = load_webui_settings()
    settings["sessions"] = _session_settings(settings)
    settings["series"] = series_key
    settings["max_grabs_per_run"] = get_max_grabs_per_run()
    settings["search_delay_minutes"] = get_search_delay_hours() * 60
    settings["quick_status_log_lines"] = get_int("webui", "quick_status_log_lines", 100)
    return jsonify(settings)


@app.route("/status")
def status():
    return jsonify(command_output)

@app.route("/api/submission-intents/<token>/clear", methods=["POST"])
def clear_submission_intent(token):
    """User has checked SABnzbd and explicitly accepts the duplicate risk."""
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        return jsonify({"error": "Invalid submission identifier"}), 400
    from utils.submission_guard import clear_intent, intent_path
    try:
        cleared = clear_intent(intent_path(CONFIG_DIR), token)
    except (OSError, ValueError):
        logger.exception("Could not clear submission reservation")
        return jsonify({"error": "Could not update submission reservations"}), 500
    if not cleared:
        return jsonify({"error": "Submission is no longer pending"}), 404
    return jsonify({"status": "cleared"})


@app.route("/api/activity-events")
def api_activity_events():
    """Get all activity events for the activity feed."""
    try:
        activity = get_all_activity(series=_request_series())
        return jsonify(activity)
    except Exception as e:
        return jsonify({"error": str(e), "active": [], "history": []}), 500


@app.route("/api/activity-events/clear", methods=["POST"])
def api_clear_activity():
    """Clear all activity events."""
    try:
        clear_all_activity(series=_request_series())
        return jsonify({"status": "cleared"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@app.route("/library-status")
def library_status():
    series_key = _request_series()
    season = request.args.get("season", type=int)
    if season is None:
        season = get_current_season()
    round_num = request.args.get("round", "", type=str)
    round_filter = int(round_num) if round_num else None
    
    data = get_library_status(season, round_filter, series_key)
    
    for round_data in data["rounds"]:
        for session in round_data["sessions"]:
            # Include episode number for session key building
            session["episode"] = int(session["type"])
            session["type"] = session["type"].name
    
    return jsonify(data)


# =============================================================================
# RELEASE MISMATCHES
# =============================================================================
#
# The matcher is not perfect, and "download it again" does not fix a mismatch:
# the same rules see the same candidates and choose the same release. These
# routes exist so the user can say "not that one" once, have the replacement
# request actually behave differently, and see the evidence needed to correct
# the patterns afterwards.
#
# The download itself is deliberately left to the existing grabber. This app has
# no delete action and no re-download machinery of its own: it records the
# block, moves the wrong folder aside, and then runs the same
# ``backfill --sessions ... --force`` command the Download tab already uses.
# A second download path would be a second set of bugs.


def _mismatch_target(season: int, round_num: int, session_name: str,
                     series: Optional[str] = None):
    """
    Resolve a (season, round, session) triple against the calendar.

    Returns ``(round_info, session_type)`` or raises ValueError with a message
    meant to be shown to a user. Validating here rather than in the browser
    keeps the API honest when called by anything else, and it means a stale page
    cannot mark a session that does not exist for that round.
    """
    series_key = series or _request_series()
    rt = _runtime_for(series_key)
    try:
        session_type = rt.session_type[str(session_name).strip().upper()]
    except KeyError:
        raise ValueError(f"Unknown session type: {session_name!r}")

    calendar = load_calendar_data(season, series_key)
    round_info = get_round_by_number(calendar, round_num)
    if not round_info:
        raise ValueError(f"Season {season} has no round {round_num}")

    if not round_runs_session(round_info, session_type, calendar,
                              series=series_key):
        raise ValueError(
            f"Round {round_num} {round_info.get('venue', '')} does not run "
            f"{session_type.full_display_name}"
        )

    return round_info, session_type


def _grabbed_entry(season: int, round_num: int, session_type: SessionType,
                   series: Optional[str] = None):
    """The grabber's record for this session, if it has one."""
    series_key = series or _request_series()
    grabber_state = state_file("grabber_state", series_key)
    if not grabber_state.exists():
        return None
    try:
        with open(grabber_state, "r") as handle:
            state = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Could not read {grabber_state}: {exc}")
        return None

    number = _runtime_for(series_key).element_number(session_type, season)
    key = f"{season}_R{round_num:02d}_E{number:02d}"
    entry = (state.get("grabbed") or {}).get(key)
    return entry if isinstance(entry, dict) else None


def _library_session_for(season: int, round_num: int,
                         session_type: SessionType,
                         series: Optional[str] = None):
    """Locate the library folder for this session, or None."""
    from utils.release_mismatches import library_session_path

    series_key = series or _request_series()
    prefix = _runtime_for(series_key).build_prefix(season, round_num, session_type)
    return library_session_path(library_root(series_key), season, round_num, prefix)


@app.route("/mismatch")
def mismatch_page():
    """
    The full mismatch log.

    A single list rather than another tab: the decision a user needs to make
    here ("is this the same problem I hit last month?") depends on seeing the
    history, not one session's view of it.
    """
    from utils.release_mismatches import describe_session, load_entries

    series_key = _request_series()
    entries = sorted(
        load_entries(path=state_file("release_mismatches", series_key)),
        key=lambda e: e.get("recorded_at") or "",
        reverse=True,
    )
    for entry in entries:
        entry["session_label"] = describe_session(
            entry.get("session", ""), series=series_key
        )

    return render_template(
        "mismatches.html",
        status=command_output,
        next_race=get_next_race_info(series_key),
        current_year=default_calendar_year(None, series_key),
        active_tab="download",
        entries=entries,
    )


@app.route("/api/release-mismatch")
def api_release_mismatch():
    """
    Evidence for one session: what the matcher decided, and what is blocked.

    ``accepted`` is the interesting field. It says what the matcher would do
    with the stored release title *now* - so if the answer is "this would pass
    every filter", the stored title is still a live risk, and re-running the
    search without fixing the rules will pick it again.
    """
    series_key = _request_series()
    season = request.args.get("season", type=int)
    round_num = request.args.get("round", type=int)
    session_name = request.args.get("session", "")

    if not season or not round_num or not session_name:
        return jsonify({
            "error": "season, round and session are required"
        }), 400

    try:
        round_info, session_type = _mismatch_target(
            season, round_num, session_name, series_key
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    from utils.release_mismatches import (
        QUARANTINE_DIRNAME,
        load_entries,
        mismatches_for,
    )

    grabbed = _grabbed_entry(season, round_num, session_type, series_key) or {}
    entries = load_entries(path=state_file("release_mismatches", series_key))
    blocked_titles = [e["title"] for e in mismatches_for(
        season, round_num, session_type.name, entries
    )]

    logged_title = grabbed.get("release")
    library_path = _library_session_for(
        season, round_num, session_type, series_key
    )
    calendar = load_calendar_data(season, series_key)

    analysis = None
    if logged_title:
        analysis = explain_release(
            logged_title,
            session_type,
            season,
            round_info,
            get_required_quality(),
            get_allowed_groups(),
            calendar,
            excluded_titles=blocked_titles,
            series=series_key,
        ).as_dict(calendar)

    # A title that came back truncated or annotated by the grabber cannot be
    # blocked usefully, so say so rather than offering a button that would
    # store something no candidate will ever match.
    can_mark = bool(logged_title) and "Unknown" not in str(logged_title)

    return jsonify({
        "season": season,
        "round": round_num,
        "venue": round_info.get("venue", ""),
        "session": session_type.name,
        "session_name": session_type.full_display_name,
        "prefix": build_prefix(season, round_num, session_type),
        "grabbed": {
            "release": logged_title,
            "nzo_id": grabbed.get("nzo_id"),
            "timestamp": grabbed.get("timestamp"),
        },
        "analysis": analysis,
        "can_mark": can_mark,
        "blocked": mismatches_for(season, round_num, session_type.name, entries),
        "library": {
            "found": library_path is not None,
            "path": str(library_path) if library_path else None,
            "quarantine_dir": str(
                Path(library_root(series_key)) / QUARANTINE_DIRNAME
            ),
        },
        "requirements": {
            "quality": get_required_quality(),
            "groups": get_allowed_groups(),
        },
    })


@app.route("/api/release-mismatch", methods=["POST"])
def api_mark_release_mismatch():
    """
    Record a mismatch, optionally hiding the existing library folder.

    Write route: it changes what the grabber will select and can move a folder
    in the library. It is authenticated by the same ``before_request`` guard as
    everything else, and the folder move is guarded by
    ``quarantine_library_session``, which only ever touches a folder it found
    by prefix inside the configured library root.
    """
    from utils.release_mismatches import (
        SCOPE_GLOBAL,
        add_mismatch,
        quarantine_library_session,
    )

    series_key = _request_series()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON object"}), 400

    try:
        season = int(payload.get("season"))
        round_num = int(payload.get("round"))
    except (TypeError, ValueError):
        return jsonify({"error": "season and round must be numbers"}), 400

    session_name = str(payload.get("session") or "").strip()

    try:
        round_info, session_type = _mismatch_target(
            season, round_num, session_name, series_key
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    grabbed = _grabbed_entry(season, round_num, session_type, series_key) or {}
    title = str(payload.get("title") or grabbed.get("release") or "").strip()
    if not title:
        return jsonify({
            "error": "No release title: the grabber has no record for this "
                     "session and none was supplied."
        }), 400

    scope = (payload.get("scope") or "session").strip().lower()
    if scope not in ("session", "global"):
        return jsonify({"error": "scope must be 'session' or 'global'"}), 400

    # Evidence, captured at the moment of the decision. This is what makes the
    # record useful later: it says what the matcher thought, not what someone
    # remembered it thinking.
    calendar = load_calendar_data(season, series_key)
    analysis = explain_release(
        title,
        session_type,
        season,
        round_info,
        get_required_quality(),
        get_allowed_groups(),
        calendar,
        series=series_key,
    ).as_dict(calendar)

    library_action = None
    if payload.get("quarantine"):
        library_action = quarantine_library_session(
            library_root(series_key), season, round_num,
            _runtime_for(series_key).build_prefix(season, round_num, session_type),
        )

    try:
        entry = add_mismatch(
            season=season,
            round_num=round_num,
            session=session_type.name,
            title=title,
            reason=str(payload.get("reason") or ""),
            scope=scope,
            venue=round_info.get("venue"),
            sab_job_name=payload.get("sab_job_name"),
            grabbed_at=grabbed.get("timestamp"),
            release_info={
                "nzo_id": grabbed.get("nzo_id"),
                "score": analysis.get("score"),
                "parsed": analysis.get("parsed"),
            },
            analysis=analysis,
            library_action=library_action,
            source="webui",
            path=state_file("release_mismatches", series_key),
        )
    except (ValueError, OSError) as exc:
        return jsonify({"error": f"Could not save the mismatch: {exc}"}), 500

    if library_action is not None and not library_action.get("ok"):
        # The block is saved, which is the part that changes future behaviour.
        # Say clearly that the file is still in the library, because leaving it
        # there while a replacement downloads is precisely the stale-copy
        # situation that makes Plex show the wrong thing again. ERROR rather
        # than WARNING on purpose: the request succeeded but did not do what the
        # user asked, and the container's uid often cannot write next to the
        # media even when it can read it.
        logger.error(
            "Mismatch recorded for %s but the library folder was not moved: %s. "
            "The old file is still in Plex and a replacement will sort into it.",
            title, library_action.get("reason"),
        )

    return jsonify({
        "status": "recorded",
        "entry": entry,
        "library_action": library_action,
        "replacement_command": _replacement_command(
            season, round_num, session_type, series_key
        ),
    })


@app.route("/api/release-mismatch/<ident>", methods=["DELETE"])
def api_remove_release_mismatch(ident):
    """Unblock a release. Undo has to exist: a wrong verdict must be reversible."""
    from utils.release_mismatches import remove_mismatch

    if not remove_mismatch(
        ident, path=state_file("release_mismatches", _request_series())
    ):
        return jsonify({"error": "No such mismatch entry"}), 404
    return jsonify({"status": "removed", "id": ident})


@app.route("/api/release-mismatches")
def api_release_mismatches():
    """Every recorded mismatch, newest first, aflat enough to render anywhere."""
    from utils.release_mismatches import load_entries

    entries = load_entries(
        path=state_file("release_mismatches", _request_series())
    )
    entries.sort(key=lambda e: e.get("recorded_at") or "", reverse=True)
    return jsonify({
        "count": len(entries),
        "entries": entries,
        "global_count": sum(1 for e in entries if e.get("scope") == "global"),
    })


@app.route("/api/release-mismatch/redownload", methods=["POST"])
def api_release_mismatch_redownload():
    """
    Start the replacement download for a session, using the existing grabber.

    ``backfill --force`` is required, not cosmetic: without it the grabber's
    30-minute search cooldown skips the session and the user is told a download
    started when nothing was searched for. The blocked title is still excluded,
    so the force rerun selects the *next* candidate - which is exactly the
    behaviour the feature promises.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON object"}), 400

    try:
        season = int(payload.get("season"))
        round_num = int(payload.get("round"))
    except (TypeError, ValueError):
        return jsonify({"error": "season and round must be numbers"}), 400

    session_name = str(payload.get("session") or "").strip()

    series_key = _request_series()
    try:
        _, session_type = _mismatch_target(
            season, round_num, session_name, series_key
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    argv = _replacement_argv(season, round_num, session_type, series_key)
    if not _start_job(argv, f"{series_key}-grab " + _replacement_command(
            season, round_num, session_type, series_key)):
        return jsonify({
            "error": "A grabber job is already running. Wait for it to finish."
        }), 409

    return jsonify({
        "status": "started",
        "command": f"motogp-grab {_replacement_command(season, round_num, session_type)}",
    })


def _replacement_argv(season: int, round_num: int,
                      session_type: SessionType,
                      series: Optional[str] = None) -> list:
    """
    The grabber argv that re-searches one session.

    Built server-side from a resolved SessionType, never from client text: the
    element number must come from the series' own numbering, and a session
    number typed by a browser is exactly how the wrong session gets searched
    (the enum value and the element number diverged in 2027 - see
    find_legacy_folders()).

    ``--series`` is not optional in effect: the child binds its vocabulary at
    import, so without it an F1 backfill would enumerate F1's calendar and then
    search with MotoGP's table.
    """
    series_key = series or DEFAULT_SERIES
    number = _runtime_for(series_key).element_number(session_type, season)
    episode = f"E{number:02d}"
    return [
        PYTHON_PATH, str(GRABBER_SCRIPT), "--series", series_key,
        "backfill", "--season", str(season), "--round", str(round_num),
        "--sessions", episode, "--force",
    ]


def _replacement_command(season: int, round_num: int,
                         session_type: SessionType,
                         series: Optional[str] = None) -> str:
    """Display form of _replacement_argv, for the mismatch dialog."""
    argv = _replacement_argv(season, round_num, session_type, series)
    start = argv.index("backfill")
    return " ".join(argv[start:])


#: Sections and keys the WebUI is allowed to write through /app-config.
#:
#: This is an allowlist rather than "whatever the client sent" because the client
#: is a browser form, not a trusted editor. Anything not listed here stays in
#: settings.yml untouched - which is the whole point: the previous version
#: rebuilt the file from a fixed dict and silently deleted uiadminpassword,
#: grabber.* and anything added later.
APP_CONFIG_SECTIONS = {
    "prowlarr": ("url", "api_key"),
    "sabnzbd": ("url", "api_key"),
    "plex": ("url", "token", "library_id", "library_name",
             "f1_library_id", "f1_library_name"),
    "library": ("motogp_path", "downloads_path",
                "f1_path", "f1_downloads_path"),
    "discord": ("webhook_url",),
}
APP_CONFIG_NESTED = {
    "plex": {
        "path_mapping": ("container", "plex"),
        "f1_path_mapping": ("container", "plex"),
    },
}
APP_CONFIG_SCALARS = ("timezone",)
APP_CONFIG_GRABBER = ("max_grabs_per_run", "allowed_groups", "required_quality", "search_delay_minutes")
APP_CONFIG_WEBUI = ("quick_status_log_lines",)


def _build_app_config_updates(payload: dict) -> dict:
    """Translate a WebUI payload into a partial settings.yml update."""
    updates = {}

    for section, keys in APP_CONFIG_SECTIONS.items():
        incoming = payload.get(section)
        if not isinstance(incoming, dict):
            continue
        section_updates = {
            key: incoming[key] for key in keys if key in incoming
        }
        for nested_name, nested_keys in APP_CONFIG_NESTED.get(section, {}).items():
            nested_incoming = incoming.get(nested_name)
            if isinstance(nested_incoming, dict):
                section_updates[nested_name] = {
                    key: nested_incoming[key]
                    for key in nested_keys
                    if key in nested_incoming
                }
        if section_updates:
            updates[section] = section_updates

    for key in APP_CONFIG_SCALARS:
        if key in payload:
            updates[key] = payload[key]

    grabber = payload.get("grabber")
    grabber_updates = {}
    if isinstance(grabber, dict):
        grabber_updates = {k: grabber[k] for k in APP_CONFIG_GRABBER if k in grabber}
    elif "max_grabs_per_run" in payload:
        # The settings form historically posts this at the top level.
        grabber_updates["max_grabs_per_run"] = payload["max_grabs_per_run"]
    if grabber_updates:
        updates["grabber"] = grabber_updates
    webui = payload.get("webui")
    if isinstance(webui, dict):
        webui_updates = {k: webui[k] for k in APP_CONFIG_WEBUI if k in webui}
        if webui_updates:
            updates["webui"] = webui_updates

    return updates


@app.route("/app-config", methods=["GET", "POST"])
def app_config_route():
    """Get or save app configuration (settings.yml)."""
    if request.method == "POST":
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"status": "error", "detail": "Expected a JSON object"}), 400

        updates = _build_app_config_updates(payload)
        success = update_settings(updates)
        reload_settings()
        validation = validate()

        return jsonify({
            "status": "saved" if success else "error",
            "applied": sorted(updates),
            "validation": validation,
        })

    reload_settings()
    settings = _load_yaml()
    validation = validate()
    # uiadminpassword is excluded on purpose: it is not needed by the UI any more
    # and one less copy of a credential in a JSON response is one less leak.
    settings = {k: v for k, v in settings.items() if k != "uiadminpassword"}
    return jsonify({**settings, "validation": validation})


@app.route("/diagnostics")
def diagnostics():
    """
    Configuration problems that are invisible in normal use.

    Two things live here because neither shows up as an error anywhere else:

      * unknown or malformed settings.yml keys - the loader silently falls back
        to defaults, so the app behaves as if the line were never written;
      * whether Plex actually scans the folder this app writes to - if it does
        not, sessions download and sort correctly and then simply never appear.

    Also reports where the credentials came from (never their values), because
    an environment variable in the container template silently beats a key typed
    into this page, which makes "I already changed that" fail in a confusing way.
    """
    from utils.config_loader import credential_sources
    from utils.plexcheck import check_from_settings
    from utils.plexlabels import probe_section

    series_key = _request_series()
    now = datetime.now()
    in_weekend = home_summary.find_weekend_round(
        load_calendar_data(get_current_season(), series_key), now.date()
    ) is not None

    return jsonify({
        "series": series_key,
        "validation": validate(),
        "credentials": credential_sources(),
        "plex_library": check_from_settings(series=series_key).as_dict(),
        "plex_labels": _probe_plex_labels(probe_section, series_key),
        # Measurement, not an opinion: how long this process's library walks
        # actually take. No index or cache change is justified until these
        # numbers say the walk is the problem (Candidate D).
        "scan_cost": scan_metrics.snapshot(),
        # Progress, not just liveness: the scheduler and the watcher can both
        # stop doing work while the WebUI keeps answering /healthz, which is
        # the one failure the Docker probe structurally cannot see.
        "background": _background_health(now, in_weekend, series_key),
    })


def _probe_plex_labels(probe, series: Optional[str] = None):
    """
    What does this Plex library hand back for a video item?

    Session titles and sort titles are only writable if the section returns an
    object type that supports field edits - and an "Other Videos" section is the
    case where it may not. This is reported rather than assumed: the answer
    cannot be known without a live server, and a diagnostic that guesses is worse
    than one that says "unknown".
    """
    series_key = series or DEFAULT_SERIES
    try:
        from motogp_plex_tagger import get_plex_connection

        # The Plex *server* URL and token are install-wide, not per series -
        # only the library section differs, and that is passed to the probe
        # below.
        plex = get_plex_connection()
    except Exception as exc:  # noqa: BLE001 - unreachable Plex is a normal state
        return {"note": f"Plex is not reachable: {exc}"}

    try:
        return probe(plex, get_plex_library_name(series_key))
    except Exception as exc:  # noqa: BLE001
        return {"note": f"Could not inspect the library: {exc}"}


@app.route("/test-connections")
def test_connections():
    """Test connections to Prowlarr, SABnzbd, and Plex."""
    import requests as req
    
    results = {"prowlarr": False, "sabnzbd": False, "plex": False, "errors": []}
    
    # Prowlarr
    try:
        url, key = get_prowlarr_url(), get_prowlarr_api_key()
        if url and key:
            r = req.get(f"{url}/api/v1/health", headers={"X-Api-Key": key}, timeout=5)
            results["prowlarr"] = r.status_code == 200
    except Exception as e:
        results["errors"].append(f"Prowlarr: {str(e)}")
    
    # SABnzbd
    try:
        url, key = get_sabnzbd_url(), get_sabnzbd_api_key()
        if url and key:
            r = req.get(f"{url}/api?mode=version&apikey={key}&output=json", timeout=5)
            results["sabnzbd"] = r.status_code == 200
    except Exception as e:
        results["errors"].append(f"SABnzbd: {str(e)}")
    
    # Plex
    try:
        url, token = get_plex_url(), get_plex_token()
        if url and token:
            r = req.get(f"{url}/?X-Plex-Token={token}", timeout=5)
            results["plex"] = r.status_code == 200
    except Exception as e:
        results["errors"].append(f"Plex: {str(e)}")
    
    return jsonify(results)


@app.route("/logs/stream")
def stream_logs():
    """Stream log file using Server-Sent Events."""
    def generate():
        try:
            if not LOG_FILE.exists():
                yield f"data: Log file not found: {LOG_FILE}\n\n"
                return
            
            # Send last 100 lines initially
            with open(LOG_FILE, 'r') as f:
                lines = f.readlines()
                for line in lines[-max(1, min(get_int("webui", "quick_status_log_lines", 100), 2000)):]:
                    yield f"data: {line.rstrip()}\n\n"
            
            # Then tail the file
            with open(LOG_FILE, 'r') as f:
                f.seek(0, 2)  # Go to end
                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {line.rstrip()}\n\n"
                    else:
                        import time
                        time.sleep(0.5)
        except GeneratorExit:
            pass
        except Exception as e:
            yield f"data: Error reading log: {e}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')


@app.route("/logs/recent")
def recent_logs():
    """Get recent log lines."""
    lines = request.args.get("lines", 100, type=int)
    
    if not LOG_FILE.exists():
        return jsonify({"error": "Log file not found", "lines": []})
    
    try:
        with open(LOG_FILE, 'r') as f:
            all_lines = f.readlines()
            return jsonify({"lines": [l.rstrip() for l in all_lines[-lines:]]})
    except Exception as e:
        return jsonify({"error": str(e), "lines": []})




@app.route("/stats")
def stats():
    """Statistics page."""
    series_key = _request_series()
    next_race = get_next_race_info(series_key)
    stats_data = get_stats_summary(series=series_key)
    
    # Calculate max values for chart scaling
    max_daily = max([d["count"] for d in stats_data.get("by_day", [])], default=1) or 1
    max_monthly = max([m["count"] for m in stats_data.get("by_month", [])], default=1) or 1
    
    return render_template(
        "stats.html",
        status=command_output,
        next_race=next_race,
        stats=stats_data,
        max_daily=max_daily,
        max_monthly=max_monthly,
        active_tab="stats"
    )



@app.route("/failed-sessions")
def failed_sessions():
    """
    Recently failed session searches, with the stage that failed.

    Each entry is normalised by ``utils.failure_reasons.describe`` so the
    Download tab can say *which* actor failed ("Search service: Prowlarr is
    unreachable") and whether waiting can help, instead of showing the bare
    "No results" every cause used to collapse into. Entries written before the
    stage existed keep their reason and carry no stage: they are reported as
    unknown rather than labelled with a guess.

    Read through the shared store so a reader cannot see a torn file - the
    grabber writes this while the WebUI may be rendering.
    """
    from utils import failure_reasons

    state = _load_grabber_state(_request_series())
    failed = {
        key: failure_reasons.describe(entry)
        for key, entry in (state.get("failed") or {}).items()
    }
    return jsonify({"failed": failed})


@app.route("/download-selected", methods=["POST"])
def download_selected():
    """Download selected missing sessions."""
    data = request.json
    season = data.get("season")
    selections = data.get("selections", [])

    if not selections:
        return jsonify({"error": "No sessions selected"}), 400

    # The Library page sends the season from a <select>, so it arrives as a
    # string; the Calendar page sends an int. element_number() compares the
    # season to an int, so a string raises TypeError and the whole request
    # 500s - the UI then shows "Failed to start download" even though every
    # connection test passes. Coerce once, here, so both callers work.
    try:
        season = int(season)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid season"}), 400
    
    series_key = _request_series()
    rt = _runtime_for(series_key)
    label_prefix = f"{series_key}-"

    # Group by round for efficiency
    by_round = {}
    for sel in selections:
        round_num = sel.get("round")
        if round_num not in by_round:
            by_round[round_num] = []
        
        # Convert session type name to episode number. The enum is the series'
        # own: the two vocabularies overlap in member names, and MotoGP's
        # "RACE" is a different episode number from F1's.
        session_name = sel.get("session", "")
        try:
            session_type = rt.session_type[session_name]
            number = rt.element_number(session_type, season)
            by_round[round_num].append(f"E{number:02d}")
        except (KeyError, ValueError, TypeError) as e:
            # If conversion fails, skip this session
            print(f"Could not convert session: {session_name} - {e}")
            continue
    
    # Build one argv list per round. These are executed as lists, never
    # joined and re-split; the "commands" strings in the response are a
    # display form for the UI, built from the same argv.
    job_list = []
    commands = []
    for round_num, episodes in by_round.items():
        if episodes:  # Only if we have valid episodes
            sessions_str = ",".join(episodes)
            argv = [
                PYTHON_PATH, str(GRABBER_SCRIPT), "--series", series_key,
                "backfill", "--season", str(season), "--round", str(round_num),
                "--sessions", sessions_str, "--force",
            ]
            job_list.append((
                argv,
                f"{label_prefix}searching: Season {season} R{round_num} {sessions_str}",
            ))
            # The display form is the command without the interpreter, the
            # script and the series, which are the same for every job in the
            # batch; the label above already names the series.
            commands.append(" ".join(argv[4:]))

    if job_list:
        # Claim the singleton slot synchronously, like /run: this route used
        # to start a batch even while another job was already running.
        if not jobs.try_start(
            f"{label_prefix}searching: {len(job_list)} round(s), Season {season}",
            output=f"Starting batch download of {len(job_list)} job(s)...\n",
        ):
            return jsonify({"error": "Command already running"}), 409
        thread = threading.Thread(
            target=jobs.run_sequence,
            args=(job_list, f"Starting batch download of {len(job_list)} job(s)...\n"),
            daemon=True,
        )
        thread.start()

    return jsonify({
        "status": "started",
        "rounds": list(by_round.keys()),
        "total_sessions": len(selections),
        "commands": commands
    })



@app.route("/activity")
def activity():
    """Activity feed page."""
    next_race = get_next_race_info(_request_series())
    
    return render_template(
        "activity.html",
        status=command_output,
        next_race=next_race,
        active_tab="activity"
    )


@app.route("/activity/stream")
def activity_stream():
    """Stream activity events parsed from log file."""
    import re
    
    def parse_log_line(line):
        """Parse a log line into an activity event."""
        # Pattern: 2026-01-31 19:24:30 [INFO] motogp.grabber: message
        match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] ([\w.]+): (.+)', line)
        if not match:
            return None
        
        timestamp, level, source, message = match.groups()
        event = {'timestamp': timestamp, 'level': level, 'source': source, 'raw': message}
        
        # Parse specific event types
        if '🔍 Searching:' in message:
            # 🔍 Searching: Round 3 Americas - MotoGP Free Practice 2
            m = re.search(r'Round (\d+) (\w+) - (.+)', message)
            if m:
                event['stage'] = 'searching'
                event['round'] = m.group(1)
                event['venue'] = m.group(2)
                event['name'] = f"{m.group(2)} {m.group(3)}"  # "Americas MotoGP Free Practice 2"
                event['session_key'] = f"R{m.group(1)}_{m.group(3).replace(' ', '_')}"
        
        elif '📋 Found' in message:
            # 📋 Found 6 candidates for Round 4 Qatar - MotoGP Free Practice 2
            m = re.search(r'Found (\d+) candidates for Round (\d+) (\w+) - (.+)', message)
            if m:
                event['stage'] = 'found'
                event['candidates'] = int(m.group(1))
                event['round'] = m.group(2)
                event['venue'] = m.group(3)
                event['name'] = f"{m.group(3)} {m.group(4)}"  # "Qatar MotoGP Free Practice 2"
                event['session_key'] = f"R{m.group(2)}_{m.group(4).replace(' ', '_')}"
            else:
                # Fallback for old format without Round/Venue
                m = re.search(r'Found (\d+) candidates for (.+)', message)
                if m:
                    event['stage'] = 'found'
                    event['candidates'] = int(m.group(1))
                    event['name'] = m.group(2)
                    event['session_key'] = f"search_{m.group(2).replace(' ', '_')}"
        
        elif '⬇️ Sending to SABnzbd:' in message:
            # ⬇️ Sending to SABnzbd: MotoGP.2025.The.Americas...
            m = re.search(r'Sending to SABnzbd: (.+)', message)
            if m:
                event['stage'] = 'sending'
                event['release'] = m.group(1)
        
        elif '✅ Grabbed:' in message:
            # ✅ Grabbed: 25R03E09 Americas MotoGP Free Practice 2
            m = re.search(r'Grabbed: (\S+) (.+)', message)
            if m:
                event['stage'] = 'grabbed'
                event['session_key'] = m.group(1)
                event['name'] = m.group(2)
        
        elif '📦 Release:' in message:
            # 📦 Release: MotoGP.2025.The.Americas...
            m = re.search(r'Release: (.+)', message)
            if m:
                event['release'] = m.group(1)
        
        elif '📊 Score:' in message:
            # 📊 Score: 155
            m = re.search(r'Score: (\d+)', message)
            if m:
                event['score'] = int(m.group(1))
        
        elif '📂 New download ready:' in message:
            # 📂 New download ready: 25R03E09 Americas MotoGP Free Practice 2
            m = re.search(r'New download ready: (\S+) (.+)', message)
            if m:
                event['stage'] = 'ready'
                event['session_key'] = m.group(1)
                event['name'] = m.group(2)
        
        elif 'Processing:' in message and 'motogp.sort' in source:
            # Processing: 25R03E09 Americas MotoGP Free Practice 2
            m = re.search(r'Processing: (\S+) (.+)', message)
            if m:
                event['stage'] = 'sorting'
                event['session_key'] = m.group(1)
                event['name'] = m.group(2)
        
        elif 'Applying Plex tags' in message:
            event['stage'] = 'tagging'
        
        elif 'Triggering Plex refresh' in message:
            event['stage'] = 'plex'
        
        elif '✅ Successfully processed:' in message:
            # ✅ Successfully processed: 25R03E09 Americas MotoGP Free Practice 2
            m = re.search(r'Successfully processed: (\S+) (.+)', message)
            if m:
                event['stage'] = 'complete'
                event['session_key'] = m.group(1)
                event['name'] = m.group(2)
        
        elif '✅ Sort completed:' in message:
            # ✅ Sort completed: 25R08E07 Aragon Moto2 Practice
            m = re.search(r'Sort completed: (\S+) (.+)', message)
            if m:
                event['stage'] = 'complete'
                event['session_key'] = m.group(1)
                event['name'] = m.group(2)
                
        elif '✅ Plex refresh triggered' in message:
            # This one doesn't have session info in the message
            # Skip it - let "Successfully processed" be the complete trigger
            return None

        elif '❌ No candidates:' in message:
            m = re.search(r'No candidates: Round (\d+) (.+?) - (.+)', message)
            if m:
                event['stage'] = 'error'
                event['round'] = m.group(1)
                event['venue'] = m.group(2)
                event['name'] = f"{m.group(2)} {m.group(3)}"
                event['session_key'] = f"R{m.group(1)}_{m.group(3).replace(' ', '_')}"
                event['detail'] = 'No matching releases found'
                event['candidates'] = 0
                event['error_message'] = 'Episode unavailable - no matching releases after filtering'

           
        elif '❌' in message or 'Error' in message or 'error' in message.lower():
            event['stage'] = 'error'
            event['detail'] = message
        
        return event if event.get('stage') or event.get('release') or event.get('score') else None
        
    def generate():
        try:
            log_file = CONFIG_DIR / "logs" / "motogp.log"
            
            if not log_file.exists():
                yield "data: " + json.dumps({"error": "Log file not found"}) + "\n\n"
                return
            
            # Send last 50 lines initially
            with open(log_file, 'r') as f:
                lines = f.readlines()[-50:]
                for line in lines:
                    event = parse_log_line(line.strip())
                    if event:
                        yield "data: " + json.dumps(event) + "\n\n"
            
            # Then tail the file
            with open(log_file, 'r') as f:
                f.seek(0, 2)  # Go to end
                while True:
                    line = f.readline()
                    if line:
                        event = parse_log_line(line.strip())
                        if event:
                            yield "data: " + json.dumps(event) + "\n\n"
                    else:
                        import time
                        time.sleep(0.5)
        except GeneratorExit:
            pass
        except Exception as e:
            yield "data: " + json.dumps({"error": str(e)}) + "\n\n"
    
    return Response(generate(), mimetype='text/event-stream')


def _serialize_calendar_library_status(year: int,
                                       series: Optional[str] = None) -> dict:
    """
    Build per-round library status payload for the Calendar modal.

    `get_library_status()` stores `type` as a `SessionType(IntEnum)`. Converting
    it to `.name` and recording `episode = int(...)` keeps `/calendar` and
    `/calendar-data` aligned with what `POST /download-selected` expects
    (`SessionType[session_name]`).
    """
    library_status = {}
    lib_data = get_library_status(year, series=series)
    for r in lib_data.get("rounds", []):
        serialized_sessions = []
        for s in r["sessions"]:
            raw_type = s["type"]
            type_name = raw_type.name if hasattr(raw_type, "name") else str(raw_type)
            episode = s.get("episode") or (int(raw_type) if isinstance(raw_type, int) else 0)
            serialized_sessions.append({
                "name": s["name"],
                "exists": s["exists"],
                "type": type_name,
                "episode": episode,
            })
        library_status[r["number"]] = {
            "grabbed": r["grabbed_count"],
            "total": r["desired_count"],
            "sessions": serialized_sessions,
        }
    return library_status


@app.route("/calendar")
def calendar():
    """Calendar view of race schedule."""
    series_key = _request_series()
    seasons = discover_seasons(series_key) or SEASONS
    year = request.args.get("year", type=int)
    if year is None:
        year = default_calendar_year(seasons, series_key)
    next_race = get_next_race_info(series_key)

    # Get calendar data for all seasons
    calendar_data = {}
    for y in seasons:
        calendar_data[y] = get_rounds_for_season(y, series_key)
    if year not in calendar_data:
        calendar_data[year] = get_rounds_for_season(year, series_key)

    library_status = _serialize_calendar_library_status(year, series_key)

    # Build flag data per season
    flag_data = {}
    for y in calendar_data:
        flag_data[y] = {}
        cal = load_calendar_data(y, series_key)
        for r in cal.get("rounds", []):
            flag_data[y][r.get("round")] = r.get("flag", "")

    return render_template(
        "calendar.html",
        status=command_output,
        next_race=next_race,
        year=year,
        calendar_data=calendar_data,
        library_status=library_status,
        flag_data=flag_data,
        active_tab="calendar"
    )


@app.route("/calendar-data")
def calendar_data_api():
    """Get calendar and library data for a specific year."""
    series_key = _request_series()
    seasons = discover_seasons(series_key) or SEASONS
    year = request.args.get("year", type=int)
    if year is None:
        year = default_calendar_year(seasons, series_key)

    return jsonify({
        "year": year,
        "series": series_key,
        "library_status": _serialize_calendar_library_status(year, series_key),
    })

@app.route("/api/sabnzbd/queue")
def sabnzbd_queue():
    """
    SABnzbd queue status, split by series.

    One SABnzbd serves every series, so the queue holds both series' grabs and
    the only way to tell them apart is the category they were submitted under -
    which each series declares. Filtering on a hardcoded "motogp" would show
    MotoGP's downloads and silently omit F1's, which looks like "F1 never
    grabbed anything" rather than a display bug.
    """
    import requests
    
    try:
        # Load SABnzbd settings
        settings_file = CONFIG_DIR / "settings.yml"
        if not settings_file.exists():
            return jsonify({"error": "Settings file not found"}), 404
        
        with open(settings_file, 'r') as f:
            import yaml
            settings = yaml.safe_load(f)
        
        sab_config = settings.get('sabnzbd', {})
        sab_url = sab_config.get('url', '').rstrip('/')
        sab_api_key = sab_config.get('api_key', '')
        
        if not sab_url or not sab_api_key:
            return jsonify({"error": "SABnzbd not configured"}), 400
        
        # Call SABnzbd API
        api_url = f"{sab_url}/api"
        params = {
            'apikey': sab_api_key,
            'mode': 'queue',
            'output': 'json'
        }
        
        response = requests.get(api_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        queue = data.get('queue', {})
        slots = queue.get('slots', [])
        
        # Split the queue by the category each series submits under. The keys
        # keep their historical "motogp_*" names for the default series because
        # the dashboard and activity templates read them; the generic
        # "by_series" map is what new front-end code should use.
        categories = {
            spec.key: spec.sabnzbd_category.lower() for spec in all_series()
        }
        by_series = {key: [] for key in categories}
        for slot in slots:
            category = str(slot.get('cat', '')).lower()
            for series_key, wanted in categories.items():
                if category == wanted:
                    by_series[series_key].append({
                        'filename': slot.get('filename', 'Unknown'),
                        'size': slot.get('size', '0 MB'),
                        'sizeleft': slot.get('sizeleft', '0 MB'),
                        'percentage': slot.get('percentage', '0'),
                        'timeleft': slot.get('timeleft', 'Unknown'),
                        'status': slot.get('status', 'Unknown')
                    })

        motogp_downloads = by_series.get(SERIES_KEY, [])

        # Get total queue info
        result = {
            'by_series': by_series,
            'categories': categories,
            'motogp_downloads': motogp_downloads,
            'motogp_count': len(motogp_downloads),
            'total_size': queue.get('size', '0 MB'),
            'total_sizeleft': queue.get('sizeleft', '0 MB'),
            'speed': queue.get('speed', '0 KB/s'),
            'timeleft': queue.get('timeleft', 'Unknown'),
            'paused': queue.get('paused', False),
            'total_slots': len(slots)
        }
        
        return jsonify(result)
    
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Failed to connect to SABnzbd: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/flags/<path:filename>")
def flag_files(filename):
    """Serve flag SVG files."""
    return send_from_directory(str(SCRIPT_DIR / "assets" / "flag_assets_svg"), filename)


@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "Motorsportarr",
        "short_name": "Motorsportarr",
        "description": "Motorsport Automated Recording & Retrieval",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#000000",
        "theme_color": "#52aad8 ",
        "orientation": "any",
        "icons": [
            {
                "src": "/assets/icon-192x192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable"
            },
            {
                "src": "/assets/icon-512x512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable"
            }
        ]
    })

@app.route("/sw.js")
def service_worker():
    return send_from_directory(str(SCRIPT_DIR / "static" / "js"), "sw.js", mimetype='application/javascript')


@app.route("/assets/<path:filename>")
def static_files(filename):
    return send_from_directory(str(SCRIPT_DIR / "assets"), filename)


def _ensure_state_files_are_namespaced():
    """
    Migrate a pre-hybrid /config, in case the entrypoint did not.

    ``entrypoint.sh`` does this before starting either process, which is the
    only point at which it is strictly a rename rather than a race. But the
    entrypoint is not the only way this program is started: a developer or an
    operator can run the WebUI or the scheduler directly, and then nothing
    would have moved ``grabber_state.json`` - so the app would start with an
    empty history and say nothing, which is exactly the silent-loss pattern
    this codebase keeps coming back to.

    Idempotent and cheap (six ``exists()`` calls), so running it twice is
    harmless. A failure is logged and does not stop startup; see
    ``utils.state_files.migrate_default_series_state``.
    """
    try:
        from utils.state_files import migrate_default_series_state

        for path in migrate_default_series_state(CONFIG_DIR):
            print(f"  moved legacy state into {path.name}")
    except Exception as exc:                      # pragma: no cover - startup only
        print(f"  ⚠️  Could not check state file names: {exc}")


if __name__ == "__main__":
    _ensure_state_files_are_namespaced()
    print("=" * 50)
    print("  Motorsportarr Web UI")
    print("  Open http://your-server-ip:5679")
    print("=" * 50)

    try:
        app.run(host="0.0.0.0", port=5679, debug=False)
    except OSError as exc:
        # Flask/Werkzeug would otherwise dump a traceback whose most useful
        # line is the errno. Say the actual thing that is wrong: something else
        # already owns the port (on Unraid, almost always an older container
        # that was not fully removed).
        import errno as _errno

        if exc.errno == _errno.EADDRINUSE:
            print("")
            print("  ❌ Port 5679 is already in use - the WebUI cannot start.")
            print("     Another container is almost certainly still running and")
            print("     holding the port. Check with:")
            print("       docker ps --format '{{.Names}} {{.Ports}}'")
            print("     Remove or stop it, or give this container a different")
            print("     host port.")
            print("=" * 50)
            raise SystemExit(1)

        print(f"  ❌ WebUI could not start: {exc}")
        raise SystemExit(1)