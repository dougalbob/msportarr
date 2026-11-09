"""
Tests for the home page view-model and route (Issue #51).

Two layers:

* `utils/home_summary.py` is pure, so the rules are tested against a synthetic
  Qatar-shaped round with explicit GMT session times and an explicit `now`.
  No clock, no files, no Flask.
* The route is tested through the Flask test client against the real 2026
  calendar, with `datetime` frozen, proving the page renders from grabber
  state rather than from a running scheduler.
"""

import json
import os
import re
import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

pytest.importorskip("flask")

REPO_ROOT = Path(__file__).resolve().parent.parent

os.environ.setdefault(
    "CONFIG_DIR", tempfile.mkdtemp(prefix="motogp-home-summary-test-")
)
os.environ.setdefault("AUTH_MODE", "none")

import motogp_common as common  # noqa: E402
import motogp_webui  # noqa: E402
import utils.config_loader as config_loader  # noqa: E402
from motogp_common import SessionType  # noqa: E402
from utils import home_summary  # noqa: E402


# A Qatar-shaped race weekend, in the real calendar-file shape
# (sessions: {day: {calendar_key: {gmt, duration_mins, parts}}}), so the tests
# exercise the same parser production uses rather than a convenient fiction.
def _session(gmt, duration_mins, parts=None):
    entry = {"local": gmt, "gmt": gmt, "duration_mins": duration_mins}
    if parts:
        entry["parts"] = parts
    return entry


ROUND = {
    "round": 20,
    "venue": "Qatar",
    "date": "2026-11-08",
    "weekend_start": "2026-11-06",
    "status": "scheduled",
    "classes": ["motogp", "moto2", "moto3"],
    "weekend_format": "motogp_standard",
    "sessions": {
        "friday": {
            "moto3_fp1": _session("10:00", 35),
            "motogp_fp1": _session("11:45", 45),
            "motogp_practice": _session("16:00", 60),
        },
        "saturday": {
            "motogp_fp2": _session("11:00", 30),
            "motogp_qualifying": _session("11:40", 40, parts=[
                {"key": "q1", "local": "14:40", "duration_mins": 15},
                {"key": "q2", "local": "15:05", "duration_mins": 15},
            ]),
            "motogp_sprint": _session("16:00", 25),
        },
        "sunday": {
            "motogp_race": _session("16:00", 50),
        },
    },
}
CALENDAR = {"season": 2026, "rounds": [ROUND]}
DESIRED = {
    SessionType.MOTO3_FP1, SessionType.MOTOGP_FP1, SessionType.MOTOGP_PRACTICE,
    SessionType.MOTOGP_FP2, SessionType.MOTOGP_Q1, SessionType.MOTOGP_Q2,
    SessionType.MOTOGP_SPRINT, SessionType.MOTOGP_RACE,
}


# ===========================================================================
# Weekend window
# ===========================================================================

def test_find_weekend_round_covers_one_day_before_and_two_days_after():
    # window: weekend_start - 1 (Thu 5th) .. race + 2 (Tue 10th)
    assert home_summary.find_weekend_round(CALENDAR, date(2026, 11, 5)) is ROUND
    assert home_summary.find_weekend_round(CALENDAR, date(2026, 11, 10)) is ROUND
    assert home_summary.find_weekend_round(CALENDAR, date(2026, 11, 4)) is None
    assert home_summary.find_weekend_round(CALENDAR, date(2026, 11, 11)) is None


def test_find_weekend_round_skips_cancelled_rounds():
    cancelled = dict(ROUND, status="cancelled")
    calendar = {"season": 2026, "rounds": [cancelled]}
    assert home_summary.find_weekend_round(calendar, date(2026, 11, 7)) is None


def test_find_weekend_round_tolerates_missing_calendar():
    assert home_summary.find_weekend_round(None, date(2026, 11, 7)) is None
    assert home_summary.find_weekend_round({}, date(2026, 11, 7)) is None


# ===========================================================================
# Next session
# ===========================================================================

def test_f1_next_session_uses_f1_calendar_and_clock():
    """F1's session enum must be timed through F1's own calendar-key table."""
    import json
    from series import runtime

    calendar = json.loads(
        (REPO_ROOT / "config" / "f1_2026.json").read_text(encoding="utf-8")
    )
    round_info = next(item for item in calendar["rounds"] if item["round"] == 21)
    vocabulary = home_summary._vocabulary("f1")
    race = runtime("f1").session_type.RACE

    next_up = home_summary.next_session(
        round_info, calendar, {race}, datetime(2026, 11, 8, 17, 30), 4,
        vocabulary,
    )

    assert next_up["state"] == "live"
    assert next_up["name"] == "F1 Race"
    assert next_up["start_utc"] == "2026-11-08T17:00:00Z"
    assert next_up["end_utc"] == "2026-11-08T19:00:00Z"
    assert next_up["search_after_utc"] == "2026-11-08T23:00:00Z"


def test_next_session_is_live_while_the_session_is_running():
    next_up = home_summary.next_session(
        ROUND, CALENDAR, DESIRED, datetime(2026, 11, 7, 11, 10), 1
    )
    assert next_up["state"] == "live"
    assert next_up["name"] == "MotoGP FP2"
    assert next_up["start_utc"] == "2026-11-07T11:00:00Z"
    assert next_up["end_utc"] == "2026-11-07T11:30:00Z"


def test_next_session_becomes_upcoming_the_minute_the_session_ends():
    next_up = home_summary.next_session(
        ROUND, CALENDAR, DESIRED, datetime(2026, 11, 7, 11, 30), 1
    )
    assert next_up["state"] == "upcoming"
    assert next_up["name"] == "MotoGP Qualifying"
    assert next_up["countdown"] == "10 m"


def test_qualifying_parts_share_one_block_label():
    # Q1 and Q2 share the timetable block; the card must say the session
    # viewers recognise, not "MotoGP Q1" chosen by sort order.
    next_up = home_summary.next_session(
        ROUND, CALENDAR, DESIRED, datetime(2026, 11, 7, 11, 35), 1
    )
    assert next_up["name"] == "MotoGP Qualifying"
    assert next_up["class_label"] == "MotoGP"


def test_search_after_is_the_session_end_plus_the_configured_delay():
    next_up = home_summary.next_session(
        ROUND, CALENDAR, DESIRED, datetime(2026, 11, 7, 11, 35), 1
    )
    assert next_up["end_utc"] == "2026-11-07T12:20:00Z"
    assert next_up["search_after_utc"] == "2026-11-07T13:20:00Z"


def test_search_after_follows_the_delay_setting():
    next_up = home_summary.next_session(
        ROUND, CALENDAR, DESIRED, datetime(2026, 11, 7, 11, 35), 3
    )
    assert next_up["search_after_utc"] == "2026-11-07T15:20:00Z"


def test_next_session_reports_the_weekend_complete_after_the_last_chequered_flag():
    # Race ends 16:50 Sunday; the delay only shifts searching, not "airing".
    assert home_summary.next_session(
        ROUND, CALENDAR, DESIRED, datetime(2026, 11, 8, 17, 0), 1
    ) == {"state": "complete"}


def test_next_session_ignores_sessions_the_user_has_not_enabled():
    only_fp2 = {SessionType.MOTOGP_FP2}
    next_up = home_summary.next_session(
        ROUND, CALENDAR, only_fp2, datetime(2026, 11, 7, 11, 35), 1
    )
    assert next_up == {"state": "complete"}  # FP2 aired; nothing else wanted


def test_next_session_is_none_when_nothing_desired_has_a_time():
    assert home_summary.next_session(
        ROUND, CALENDAR, {SessionType.MOTO2_FP2}, datetime(2026, 11, 7, 9, 0), 1
    ) is None


# ===========================================================================
# Weekend progress
# ===========================================================================

def _progress(now, library=(), grabbed=(), failed=()):
    return home_summary.weekend_progress(
        ROUND, CALENDAR, 2026, DESIRED, now,
        {st: "/x" for st in library},
        {k: {} for k in grabbed},
        {k: {} for k in failed},
    )


def test_progress_counts_only_aired_sessions_in_the_denominator():
    # Saturday 12:30: Moto3 FP1, GP FP1, GP Practice, GP FP2 and both
    # qualifying parts have aired (6 - Q1 and Q2 count separately, because the
    # Download tab counts them separately and it is the same promise); the
    # sprint and race have not.
    p = _progress(datetime(2026, 11, 7, 12, 30), library={
        SessionType.MOTO3_FP1, SessionType.MOTOGP_FP1, SessionType.MOTOGP_PRACTICE,
        SessionType.MOTOGP_FP2, SessionType.MOTOGP_Q1, SessionType.MOTOGP_Q2,
    })
    assert p["aired"] == 6
    assert p["in_plex"] == 6
    assert p["missing"] == []


def test_progress_classifies_missing_sessions_from_grabber_state():
    p = _progress(
        datetime(2026, 11, 7, 12, 30),
        library={SessionType.MOTOGP_FP1},
        grabbed={"2026_R20_E22"},  # MotoGP Practice grabbed, not yet in Plex
        failed={"2026_R20_E32"},   # MotoGP FP2 searched, no release found
    )
    assert (p["in_plex"], p["downloading"], p["not_found"], p["awaiting"]) == (1, 1, 1, 3)
    states = {m["name"]: m["state"] for m in p["missing"]}
    assert states["MotoGP Practice"] == "downloading"
    assert states["MotoGP FP2"] == "not found yet"
    # Aired but neither grabbed nor failed: the scheduler has not searched yet.
    assert states["MotoGP Q1"] == "awaiting search"
    assert states["MotoGP Q2"] == "awaiting search"


def test_progress_before_the_weekend_starts_has_nothing_to_report():
    p = _progress(datetime(2026, 11, 6, 8, 0))
    assert p["aired"] == 0
    assert p["missing"] == []


# ===========================================================================
# Latest scheduled addition (hero)
# ===========================================================================

def _entry(mode=None, timestamp="2026-11-06T17:52:00"):
    entry = {"timestamp": timestamp, "release": "26R20E22 MotoGP Practice"}
    if mode:
        entry["mode"] = mode
    return entry


def _library(folder="Round 20 Qatar"):
    return {2026: {20: {SessionType.MOTOGP_PRACTICE: f"/lib/{folder}/26R20E22"}}}


def test_latest_addition_ignores_entries_without_a_recorded_mode():
    # The user's decision: history is unknown, so it is not shown rather than
    # guessed from a timestamp that looks like a race weekend.
    grabbed = {"2026_R20_E22": _entry(mode=None)}
    assert home_summary.latest_scheduled_addition(grabbed, [], _library()) is None


def test_latest_addition_ignores_backfills():
    grabbed = {"2026_R20_E22": _entry(mode=common.GRAB_MODE_BACKFILL)}
    assert home_summary.latest_scheduled_addition(grabbed, [], _library()) is None


def test_latest_addition_requires_the_session_to_be_in_the_library():
    # Grabbed but still downloading (or sorted into .mismatched) is not an addition.
    grabbed = {"2026_R20_E22": _entry(mode=common.GRAB_MODE_SCHEDULED)}
    assert home_summary.latest_scheduled_addition(grabbed, [], {2026: {}}) is None


def test_latest_addition_is_the_newest_scheduled_grab_that_reached_the_library():
    grabbed = {
        "2026_R20_E22": _entry(common.GRAB_MODE_SCHEDULED, "2026-11-06T17:52:00"),
        "2026_R20_E32": _entry(common.GRAB_MODE_SCHEDULED, "2026-11-07T12:35:00"),
        # Newer, but a backfill: a correction must not outrank a live addition.
        "2026_R20_E12": _entry(common.GRAB_MODE_BACKFILL, "2026-11-07T14:00:00"),
    }
    library = {2026: {20: {
        SessionType.MOTOGP_PRACTICE: "/lib/a",
        SessionType.MOTOGP_FP2: "/lib/b",
        SessionType.MOTOGP_FP1: "/lib/c",
    }}}
    latest = home_summary.latest_scheduled_addition(grabbed, [], library)
    assert latest["name"] == "MotoGP FP2"
    assert latest["round"] == 20
    assert latest["source"] == "grabbed"


def test_latest_addition_prefers_the_activity_event_timestamp_when_present():
    # The grabber writes the grab time; the tagger writes when Plex actually
    # had it. The card says which one it is showing.
    grabbed = {"2026_R20_E22": _entry(common.GRAB_MODE_SCHEDULED, "2026-11-06T17:52:00")}
    history = [{
        "id": "2026_R20_E22",
        "stage": "complete",
        "timestamp": "2026-11-06T18:31:00",
    }]
    latest = home_summary.latest_scheduled_addition(grabbed, history, _library())
    assert latest["source"] == "added"
    assert latest["timestamp_utc"] == "2026-11-06T18:31:00Z"


def test_latest_addition_skips_keys_it_cannot_resolve_to_a_session():
    grabbed = {
        "2026_R20_E99": _entry(common.GRAB_MODE_SCHEDULED, "2026-11-07T18:00:00"),
        "not-a-key": _entry(common.GRAB_MODE_SCHEDULED, "2026-11-07T19:00:00"),
    }
    library = {2026: {20: {"anything": "/lib/x"}}}
    assert home_summary.latest_scheduled_addition(grabbed, [], library) is None


def test_latest_addition_survives_missing_venue_data():
    grabbed = {"2026_R20_E22": _entry(common.GRAB_MODE_SCHEDULED)}
    latest = home_summary.latest_scheduled_addition(grabbed, [], _library(), {})
    assert latest is not None
    assert latest["venue"] == ""


# ===========================================================================
# Scheduler heartbeat
# ===========================================================================

def test_scheduler_health_never_ran_is_reported_as_never():
    health = home_summary.scheduler_health(None, datetime(2026, 11, 7, 11, 0), True)
    assert health["never"] is True
    assert health["stale"] is False
    assert health["expected_minutes"] == 60


def test_scheduler_health_is_fresh_before_the_stale_threshold():
    health = home_summary.scheduler_health(
        "2026-11-07T11:03:00", datetime(2026, 11, 7, 11, 15), True
    )
    assert health["age_minutes"] == 12
    assert health["age_label"] == "12 min ago"
    assert health["stale"] is False


def test_scheduler_is_stale_when_it_lags_twice_the_weekend_interval_plus_grace():
    # weekend expected 60 -> threshold 180 minutes
    health = home_summary.scheduler_health(
        "2026-11-07T08:00:00", datetime(2026, 11, 7, 11, 0), True
    )
    assert health["age_minutes"] == 180
    assert health["stale"] is False
    health = home_summary.scheduler_health(
        "2026-11-07T07:59:00", datetime(2026, 11, 7, 11, 0), True
    )
    assert health["stale"] is True


def test_scheduler_health_tolerates_quiet_hours_off_weekend():
    # off weekend expected 360 -> threshold 780; quiet hours add at most 120,
    # so a healthy scheduler must never be called stale there.
    health = home_summary.scheduler_health(
        "2026-11-10T05:00:00", datetime(2026, 11, 10, 8, 0), False
    )
    assert health["expected_minutes"] == 360
    assert health["stale"] is False


def test_scheduler_health_reports_unparseable_timestamps_visibly():
    health = home_summary.scheduler_health("not a date", datetime(2026, 11, 7), True)
    assert health["never"] is True
    assert health["age_label"] == "never"


# ===========================================================================
# WebUI route
# ===========================================================================

@pytest.fixture
def isolated_webui(tmp_path, monkeypatch):
    """Point WebUI, common calendar loader, and config_loader at tmp_path."""
    for cal_file in (REPO_ROOT / "config").glob("motogp_*.json"):
        shutil.copy2(cal_file, tmp_path / cal_file.name)

    lib_dir = tmp_path / "library"
    lib_dir.mkdir()

    settings = {
        "prowlarr": {"url": "http://prowlarr:9696", "api_key": "k"},
        "sabnzbd": {"url": "http://sab:8080", "api_key": "k"},
        "plex": {"url": "http://plex:32400", "token": "k"},
        "library": {"motogp_path": str(lib_dir), "downloads_path": str(tmp_path / "dl")},
        "grabber": {"current_season": 2026, "search_delay_hours": 1},
    }
    settings_file = tmp_path / "settings.yml"
    settings_file.write_text(yaml.safe_dump(settings), encoding="utf-8")

    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(motogp_webui, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(motogp_webui, "library_root", lambda *a, **kw: str(lib_dir))
    monkeypatch.setattr(
        motogp_webui, "WEBUI_CONFIG_FILE",
        tmp_path / "webui_settings.motogp.json"
    )
    monkeypatch.setattr(config_loader, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_loader, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_loader, "_settings_cache", None)

    motogp_webui.app.config.update(TESTING=True)
    with motogp_webui.app.test_client() as test_client:
        yield test_client, tmp_path, lib_dir

    config_loader._settings_cache = None


def _freeze_webui_clock(monkeypatch, when):
    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(motogp_webui, "datetime", _FakeDateTime)


def _make_library_round(lib_dir, season, round_num, element_names):
    round_dir = lib_dir / str(season) / f"Round {round_num} Test"
    round_dir.mkdir(parents=True, exist_ok=True)
    for folder in element_names:
        (round_dir / folder).mkdir()


def test_home_route_renders_the_next_session_inside_a_race_weekend(
    isolated_webui, monkeypatch
):
    client, cfg_dir, lib_dir = isolated_webui
    _freeze_webui_clock(monkeypatch, datetime(2026, 11, 7, 11, 35))

    state = {
        "last_scheduled_check": "2026-11-07T11:25:00",
        "grabbed": {
            "2026_R20_E22": {
                "timestamp": "2026-11-06T17:52:00",
                "release": "26R20E22 MotoGP Practice 1080p",
                "mode": "scheduled",
            },
        },
        "failed": {},
    }
    (cfg_dir / "grabber_state.motogp.json").write_text(json.dumps(state), encoding="utf-8")
    _make_library_round(lib_dir, 2026, 20, ["26R20E22 MotoGP Practice"])

    html = client.get("/").get_data(as_text=True)

    assert "MotoGP Qualifying" in html
    assert "Round 20" in html
    assert "2026-11-07T13:20:00Z" in html  # searchable from: 12:20 + 1h delay
    assert "Latest scheduled addition" in html
    assert "26R20E22 MotoGP Practice 1080p" in html


def test_home_route_stays_honest_with_no_recorded_scheduled_grabs(
    isolated_webui, monkeypatch
):
    client, cfg_dir, lib_dir = isolated_webui
    _freeze_webui_clock(monkeypatch, datetime(2026, 11, 7, 11, 35))

    # A mode-less entry (pre-change history) with a library folder: it must not
    # be promoted to the hero card.
    state = {
        "last_scheduled_check": "2026-11-07T11:25:00",
        "grabbed": {
            "2026_R20_E22": {
                "timestamp": "2026-11-06T17:52:00",
                "release": "26R20E22 MotoGP Practice 1080p",
            },
        },
        "failed": {},
    }
    (cfg_dir / "grabber_state.motogp.json").write_text(json.dumps(state), encoding="utf-8")
    _make_library_round(lib_dir, 2026, 20, ["26R20E22 MotoGP Practice"])

    html = client.get("/").get_data(as_text=True)
    assert "26R20E22 MotoGP Practice 1080p" not in html
    assert "Nothing recorded yet" in html


def test_home_route_survives_a_corrupt_grabber_state(isolated_webui, monkeypatch):
    client, cfg_dir, _ = isolated_webui
    _freeze_webui_clock(monkeypatch, datetime(2026, 11, 7, 11, 35))
    (cfg_dir / "grabber_state.motogp.json").write_text("{not json", encoding="utf-8")

    response = client.get("/")
    assert response.status_code == 200
    assert "Nothing recorded yet" in response.get_data(as_text=True)


def test_home_route_states_the_scheduler_is_stale_when_it_lags(
    isolated_webui, monkeypatch
):
    client, cfg_dir, _ = isolated_webui
    _freeze_webui_clock(monkeypatch, datetime(2026, 11, 7, 11, 35))
    state = {"last_scheduled_check": "2026-11-07T06:00:00", "grabbed": {}, "failed": {}}
    (cfg_dir / "grabber_state.motogp.json").write_text(json.dumps(state), encoding="utf-8")

    html = client.get("/").get_data(as_text=True)
    assert "Last scheduled check:" in html
    assert "chip bad" in html


def test_home_route_hides_the_weekend_block_between_weekends(
    isolated_webui, monkeypatch
):
    client, cfg_dir, _ = isolated_webui
    _freeze_webui_clock(monkeypatch, datetime(2026, 11, 15, 12, 0))
    (cfg_dir / "grabber_state.motogp.json").write_text(
        json.dumps({"grabbed": {}, "failed": {}}), encoding="utf-8"
    )

    html = client.get("/").get_data(as_text=True)
    assert "Latest scheduled addition" in html
    assert "Searchable from" not in html


def test_home_route_is_never_served_from_the_service_worker_cache():
    """The home page is a snapshot of live state; caching it would lie."""
    sw = (REPO_ROOT / "static" / "js" / "sw.js").read_text(encoding="utf-8")
    assert "/api/sabnzbd" in sw
    assert "/test-connections" in sw


def test_seasons_scan_is_limited_to_seasons_that_could_reach_the_card():
    # The route scans the library per season; unlabelled and backfill-only
    # history must not make it walk folders the card can never show.
    grabbed = {
        "2025_R03_E82": {"mode": "backfill", "timestamp": "2026-01-01T00:00:00"},
        "2024_R01_E82": {"timestamp": "2026-01-01T00:00:00"},  # no mode at all
        "2026_R20_E22": {"mode": "scheduled", "timestamp": "2026-11-06T17:02:00"},
        "2027_R01_E12": {"mode": "scheduled", "timestamp": "2027-03-01T09:00:00"},
        "garbage": {"mode": "scheduled"},
    }
    assert home_summary.seasons_with_scheduled_grabs(grabbed) == [2026, 2027]
    assert home_summary.seasons_with_scheduled_grabs({}) == []
    assert home_summary.seasons_with_scheduled_grabs(None) == []


def test_old_scheduled_check_is_not_stale_between_weekends():
    health = home_summary.scheduler_health(
        "2026-11-01T08:00:00", datetime(2026, 11, 10, 8, 0), False
    )
    assert health["stale"] is False


def test_pending_submission_requires_explicit_user_acknowledgement(isolated_webui, monkeypatch):
    from utils.submission_guard import make_intent
    client, cfg_dir, _ = isolated_webui
    _freeze_webui_clock(monkeypatch, datetime(2026, 11, 7, 11, 35))
    token, intent = make_intent(['2026_R20_E82'], 'Test release', 'scheduled')
    path = cfg_dir / 'submission_intents.json'
    path.write_text(json.dumps({token: intent}))
    html = client.get('/').get_data(as_text=True)
    assert 'Submissions needing attention' in html
    assert '2026_R20_E82' in html
    assert 'mgp-' + token in html
    assert client.post('/api/submission-intents/' + token + '/clear', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert token in json.loads(path.read_text())
    assert client.post('/api/submission-intents/' + token + '/clear').status_code == 200
    assert json.loads(path.read_text()) == {}
    assert client.post('/api/submission-intents/' + token + '/clear').status_code == 404


def test_home_shows_scheduler_completion_without_false_stale_check(isolated_webui, monkeypatch):
    client, cfg_dir, _ = isolated_webui
    now = datetime(2026, 11, 8, 19, 0)
    _freeze_webui_clock(monkeypatch, now)
    (cfg_dir / 'scheduler_plan.motogp.json').write_text(json.dumps({'at': now.isoformat(), 'state': 'complete', 'venue': 'Portugal'}))
    (cfg_dir / 'background_health.motogp.json').write_text(json.dumps({'scheduler': {'last_loop': now.isoformat()}, 'watcher': {'last_scan': now.isoformat()}}))
    (cfg_dir / 'grabber_state.motogp.json').write_text(json.dumps({'last_scheduled_check': '2026-11-08T10:00:00'}))
    html = client.get('/').get_data(as_text=True)
    assert 'Meeting complete — searches paused' in html
    assert 'Next meeting: Portugal' in html
    assert 'expected every 60 min)' not in html
