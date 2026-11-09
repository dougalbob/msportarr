from datetime import datetime, timedelta

import motogp_common as common
import motogp_grabber as grabber
import motogp_scheduler as scheduler

NOW = datetime(2026, 11, 8, 18, 0)
ROUND = {'round': 20, 'venue': 'Qatar', 'date': '2026-11-08', 'weekend_start': '2026-11-06'}
SESSION = common.SessionType.MOTO2_RACE
REAL_STATE = grabber.GrabberState


def setup_plan(monkeypatch, tmp_path, *, ends, existing=False, grabbed=False):
    monkeypatch.setattr(grabber, 'SCHEDULABLE_SESSIONS', {SESSION})
    monkeypatch.setattr(common, 'get_desired_sessions', lambda *a, **kw: {SESSION: True})
    monkeypatch.setattr(common, 'round_runs_session', lambda *a, **kw: True)
    monkeypatch.setattr(grabber, 'get_session_end_time', lambda *a, **kw: ends)
    monkeypatch.setattr(scheduler, 'scan_available_sessions',
                        lambda year, series=None: {20: {SESSION}} if existing else {})
    monkeypatch.setattr(scheduler, 'get_search_delay_hours', lambda: 1)
    monkeypatch.setattr(scheduler, 'CONFIG_DIR', tmp_path)
    class State(REAL_STATE):
        def __init__(self, state_file=None, series=None):
            super().__init__(tmp_path / 'grabber_state.json', series=series)
    monkeypatch.setattr(grabber, 'GrabberState', State)
    if grabbed:
        state = State()
        state.mark_grabbed(state.make_session_key(2026, 20, SESSION), 'release', nzo_id='job')
        state.save()
    # *a, **kw: the real signature keeps growing (it now takes `series`), and a
    # fake pinned to today's parameters fails on the next keyword instead of on
    # the behaviour it is supposed to check.
    return lambda last=None, *a, **kw: scheduler.plan_weekend(
        {'rounds': [ROUND]}, 2026, NOW, last, 3600, *a, **kw)


def test_f1_scheduler_plan_uses_f1_preferences_and_race_timing(monkeypatch, tmp_path):
    """The default-bound scheduler must plan F1 with F1 enums and calendar keys."""
    import json
    from pathlib import Path

    from series import runtime

    calendar = json.loads(
        (Path(__file__).resolve().parent.parent / "config" / "f1_2026.json")
        .read_text(encoding="utf-8")
    )
    brazil = next(item for item in calendar["rounds"] if item["round"] == 21)
    f1 = runtime("f1")
    sessions = {
        session.name: session is f1.session_type.RACE
        for session in f1.session_type
        if session is not f1.session_type.UNKNOWN
    }
    (tmp_path / "webui_settings.f1.json").write_text(
        json.dumps({"sessions": sessions}), encoding="utf-8"
    )
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "get_series_library_path", lambda series=None: str(tmp_path / "library"))
    monkeypatch.setattr(scheduler, "get_search_delay_hours", lambda: 4)

    plan = scheduler.plan_weekend(
        {"rounds": [brazil]}, 2026, datetime(2026, 11, 8, 17, 30), None,
        3600, series="f1",
    )

    assert plan["state"] == "waiting"
    assert plan["run"] is False
    assert plan["next"] == datetime(2026, 11, 8, 23, 0)


def test_complete_only_when_wanted_sessions_are_in_library(monkeypatch, tmp_path):
    plan = setup_plan(monkeypatch, tmp_path, ends=NOW - timedelta(hours=3), existing=True)
    assert plan()['state'] == 'complete'
    assert not plan()['run']


def test_grabbed_but_not_sorted_still_reconciles(monkeypatch, tmp_path):
    plan = setup_plan(monkeypatch, tmp_path, ends=NOW - timedelta(hours=3), grabbed=True)
    assert plan()['run']
    assert plan(NOW)['state'] == 'waiting'
    assert plan(NOW)['next'] == NOW + timedelta(hours=1)


def test_missing_session_retries_but_future_deadline_wakes_it(monkeypatch, tmp_path):
    plan = setup_plan(monkeypatch, tmp_path, ends=NOW - timedelta(minutes=70))
    assert plan(NOW - timedelta(minutes=20))['run']  # became ready after last check
    assert not plan(NOW - timedelta(minutes=5))['run']
    plan = setup_plan(monkeypatch, tmp_path, ends=NOW + timedelta(minutes=5))
    assert plan()['next'] == NOW + timedelta(minutes=65)
    assert not plan()['run']


def test_unknown_timetable_is_not_completion(monkeypatch, tmp_path):
    plan = setup_plan(monkeypatch, tmp_path, ends=None, existing=True)
    assert plan()['run']
    assert plan(NOW)['state'] == 'waiting'


def test_available_requires_media_not_empty_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, 'get_series_library_path', lambda *a, **kw: str(tmp_path))
    folder = tmp_path / '2026' / 'Round 20 Qatar' / '26R20E84 Moto2 Race'
    folder.mkdir(parents=True)
    assert not scheduler.scan_available_sessions(2026)
    (folder / 'poster.jpg').write_bytes(b'image')
    assert not scheduler.scan_available_sessions(2026)
    (folder / 'race.mkv').write_bytes(b'video')
    assert SESSION in scheduler.scan_available_sessions(2026)[20]


def test_existing_future_media_is_not_reported_complete_early(monkeypatch, tmp_path):
    plan = setup_plan(monkeypatch, tmp_path, ends=NOW + timedelta(minutes=20), existing=True)
    assert plan()['state'] == 'waiting'
    assert plan()['next'] == NOW + timedelta(minutes=20)


def test_pending_replacement_is_reconciled_even_if_library_is_complete(monkeypatch, tmp_path):
    import json
    from utils.submission_guard import make_intent, intent_path
    plan = setup_plan(monkeypatch, tmp_path, ends=NOW - timedelta(hours=3), existing=True)
    key = grabber.GrabberState.make_session_key(2026, 20, SESSION)
    token, intent = make_intent([key], 'replacement', 'backfill')
    intent_path(tmp_path).write_text(json.dumps({token: intent}))
    assert plan()['run']
    assert plan(NOW)['state'] == 'waiting'


def test_scheduler_main_does_not_spawn_grabber_for_complete_meeting(tmp_path, monkeypatch):
    import sys
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None): return cls(2026, 11, 8, 18, 0)
    monkeypatch.setattr(scheduler, 'datetime', Frozen)
    monkeypatch.setattr(scheduler, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(scheduler, 'setup_logging', lambda **kw: None)
    monkeypatch.setattr(scheduler, 'reload_settings', lambda: None)
    monkeypatch.setattr(scheduler, 'get_current_season', lambda: 2026)
    monkeypatch.setattr(scheduler, 'get_search_delay_hours', lambda: 1)
    monkeypatch.setattr(scheduler, 'get_series_downloads_path', lambda *a, **kw: '/unused')
    monkeypatch.setattr(scheduler, 'get_series_library_path', lambda *a, **kw: '/unused')
    monkeypatch.setattr(scheduler, 'load_calendar', lambda season, **kw: {'rounds': [ROUND]})
    monkeypatch.setattr(scheduler, 'has_session_times', lambda calendar: True)
    monkeypatch.setattr(scheduler, 'write_heartbeat', lambda *a, **kw: True)
    monkeypatch.setattr(scheduler, 'plan_weekend', lambda *a, **kw: {'state': 'complete', 'run': False, 'next': None})
    monkeypatch.setattr(scheduler, 'run_grabber_scheduled', lambda **kw: (_ for _ in ()).throw(AssertionError('unexpected search')))
    monkeypatch.setattr(scheduler.time, 'sleep', lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(scheduler.signal, 'signal', lambda *a: None)
    monkeypatch.setattr(sys, 'argv', ['motogp_scheduler.py', '--no-watcher'])
    assert scheduler.main() == 0
    import json
    assert json.loads((tmp_path / 'scheduler_plan.motogp.json').read_text())['state'] == 'complete'


def test_scheduled_subprocess_reports_contention(monkeypatch):
    import subprocess
    monkeypatch.setattr(scheduler, 'get_search_delay_hours', lambda: 1)
    monkeypatch.setattr(scheduler.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a[0], 2, '', 'busy'))
    result = scheduler.run_grabber_scheduled()
    assert result['busy'] and not result['success']


def test_pending_reconciliation_runs_between_meetings_without_search(tmp_path, monkeypatch):
    import sys
    import json
    from utils.submission_guard import make_intent, intent_path
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None): return cls(2026, 11, 14, 12, 0)
    token, intent = make_intent(['2026_R20_E84'], 'race', 'scheduled')
    intent_path(tmp_path).write_text(json.dumps({token: intent}))
    calls = []
    monkeypatch.setattr(scheduler, 'datetime', Frozen)
    monkeypatch.setattr(scheduler, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(scheduler, 'setup_logging', lambda **kw: None)
    monkeypatch.setattr(scheduler, 'reload_settings', lambda: None)
    monkeypatch.setattr(scheduler, 'get_current_season', lambda: 2026)
    monkeypatch.setattr(scheduler, 'get_search_delay_hours', lambda: 1)
    monkeypatch.setattr(scheduler, 'get_series_downloads_path', lambda *a, **kw: '/unused')
    monkeypatch.setattr(scheduler, 'get_series_library_path', lambda *a, **kw: '/unused')
    monkeypatch.setattr(scheduler, 'load_calendar', lambda season, **kw: {'rounds': [ROUND]})
    monkeypatch.setattr(scheduler, 'has_session_times', lambda calendar: True)
    monkeypatch.setattr(scheduler, 'write_heartbeat', lambda *a, **kw: True)
    monkeypatch.setattr(scheduler, 'run_pending_reconciliation', lambda *a, **kw: calls.append('reconcile') or True)
    monkeypatch.setattr(scheduler, 'run_grabber_scheduled', lambda **kw: (_ for _ in ()).throw(AssertionError('unexpected search')))
    monkeypatch.setattr(scheduler.time, 'sleep', lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(scheduler.signal, 'signal', lambda *a: None)
    monkeypatch.setattr(sys, 'argv', ['motogp_scheduler.py', '--no-watcher'])
    assert scheduler.main() == 0
    assert calls == ['reconcile']
    assert json.loads((tmp_path / 'scheduler_plan.motogp.json').read_text())['state'] == 'off_weekend'
