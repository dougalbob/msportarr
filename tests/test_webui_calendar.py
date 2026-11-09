"""
Tests for Calendar tab schedule rendering, browser-timezone conversion,
live-weekend status, default season selection, and 2027 format compatibility
(Issue #30).
"""

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
import yaml

pytest.importorskip("flask")

REPO_ROOT = Path(__file__).resolve().parent.parent

os.environ.setdefault(
    "CONFIG_DIR", tempfile.mkdtemp(prefix="motogp-webui-calendar-test-")
)
os.environ.setdefault("AUTH_MODE", "none")

import motogp_common as common  # noqa: E402
import motogp_webui  # noqa: E402
import utils.config_loader as config_loader  # noqa: E402


@pytest.fixture
def isolated_webui(tmp_path, monkeypatch):
    """Point WebUI, common calendar loader, and config_loader at tmp_path."""
    for cal_file in (REPO_ROOT / "config").glob("motogp_*.json"):
        shutil.copy2(cal_file, tmp_path / cal_file.name)

    lib_dir = tmp_path / "library"
    lib_dir.mkdir()

    settings = {
        "prowlarr": {"url": "http://prowlarr:9696", "api_key": "dummy-prowlarr-key"},
        "sabnzbd": {"url": "http://sab:8080", "api_key": "dummy-sab-key"},
        "plex": {"url": "http://plex:32400", "token": "dummy-plex-token"},
        "library": {"motogp_path": str(lib_dir), "downloads_path": str(tmp_path / "dl")},
        "timezone": "Europe/London",
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
        yield test_client, tmp_path, settings_file

    config_loader._settings_cache = None


# ---------------------------------------------------------------------------
# Default season selection
# ---------------------------------------------------------------------------

def test_calendar_route_defaults_to_current_season_not_2025(isolated_webui, monkeypatch):
    client, _, _ = isolated_webui

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 6, 12, 0, 0)

    monkeypatch.setattr(motogp_webui, "datetime", _FakeDateTime)

    html = client.get("/calendar").get_data(as_text=True)
    assert "2026 Season" in html
    assert "let currentYear = 2026;" in html

    data = client.get("/calendar-data").get_json()
    assert data["year"] == 2026


def test_default_calendar_year_honours_explicit_setting_and_new_season_drop(
    isolated_webui, monkeypatch
):
    _, cfg_dir, settings_file = isolated_webui

    # Explicit grabber.current_season in settings.yml wins when that calendar exists
    saved = yaml.safe_load(settings_file.read_text(encoding="utf-8"))
    saved["grabber"] = {"current_season": 2025}
    settings_file.write_text(yaml.safe_dump(saved), encoding="utf-8")
    config_loader._settings_cache = None
    assert motogp_webui.default_calendar_year() == 2025

    # Without an explicit setting, dropping motogp_2027.json makes 2027 default in 2027
    del saved["grabber"]
    settings_file.write_text(yaml.safe_dump(saved), encoding="utf-8")
    config_loader._settings_cache = None

    cal_2027 = json.loads((cfg_dir / "motogp_2026.json").read_text(encoding="utf-8"))
    cal_2027["season"] = 2027
    (cfg_dir / "motogp_2027.json").write_text(
        json.dumps(cal_2027), encoding="utf-8"
    )

    class _Fake2027(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2027, 4, 10, 9, 0, 0)

    monkeypatch.setattr(motogp_webui, "datetime", _Fake2027)
    assert motogp_webui.default_calendar_year() == 2027


# ---------------------------------------------------------------------------
# Schedule building & UTC timestamp accuracy
# ---------------------------------------------------------------------------

def test_qatar_2026_schedule_exposes_per_day_sessions_and_qualifying_parts(
    isolated_webui,
):
    rounds = {r["number"]: r for r in motogp_webui.get_rounds_for_season(2026)}
    qatar = rounds[20]

    assert qatar["weekend_start"] == "2026-11-06"
    assert qatar["weekend_end"] == "2026-11-08"

    schedule = qatar["schedule"]
    assert set(schedule) == {"friday", "saturday", "sunday"}
    assert schedule["friday"]["date"] == "2026-11-06"
    assert schedule["saturday"]["date"] == "2026-11-07"
    assert schedule["sunday"]["date"] == "2026-11-08"

    fri = schedule["friday"]["sessions"]
    assert [s["key"] for s in fri] == [
        "moto3_fp1",
        "moto2_fp1",
        "motogp_fp1",
        "moto3_practice",
        "moto2_practice",
        "motogp_practice",
    ]
    motogp_fp1 = [s for s in fri if s["key"] == "motogp_fp1"][0]
    assert motogp_fp1["start_utc"] == "2026-11-06T11:45:00Z"
    assert motogp_fp1["end_utc"] == "2026-11-06T12:30:00Z"
    assert motogp_fp1["duration_mins"] == 45
    assert motogp_fp1["full_name"] == "MotoGP FP1"

    # Qualifying must be labelled "Qualifying" (not "Qualifying 2") with Q1/Q2 parts
    sat_quali = [
        s for s in schedule["saturday"]["sessions"] if s["key"] == "motogp_qualifying"
    ][0]
    assert sat_quali["short_name"] == "Qualifying"
    assert sat_quali["full_name"] == "MotoGP Qualifying"
    assert [(p["label"], p["start_utc"], p["duration_mins"]) for p in sat_quali["parts"]] == [
        ("Q1", "2026-11-07T11:40:00Z", 15),
        ("Q2", "2026-11-07T12:05:00Z", 15),
    ]


def test_australia_morning_sessions_anchor_to_previous_utc_day(isolated_webui):
    """
    Phillip Island Friday 09:00 local (+11:00) on 2026-10-23 is 2026-10-22T22:00Z
    (Thursday night in Europe), NOT 2026-10-23T22:00Z.
    """
    rounds = {r["number"]: r for r in motogp_webui.get_rounds_for_season(2026)}
    australia = rounds[18]
    assert australia["venue"] == "Australia"

    fri_sessions = {
        s["key"]: s for s in australia["schedule"]["friday"]["sessions"]
    }
    assert fri_sessions["moto3_fp1"]["start_utc"] == "2026-10-22T22:00:00Z"
    assert fri_sessions["motogp_fp1"]["start_utc"] == "2026-10-22T22:45:00Z"
    assert fri_sessions["moto3_practice"]["start_utc"] == "2026-10-23T02:15:00Z"


def test_2027_format_schedule_puts_fp2_and_sprint_quali_on_friday_and_drops_warmup(
    isolated_webui,
):
    """
    2027 moves MotoGP FP2 to Friday, adds Sprint Qualifying (Friday) and FP3
    (Saturday), and drops Sunday Warm-up.
    """
    cal_2026 = common.load_calendar(2026)
    cal_2027 = copy.deepcopy(cal_2026)
    cal_2027["season"] = 2027

    entry = copy.deepcopy(cal_2027["rounds"][3])  # Spain (+02:00)
    entry["weekend_format"] = "motogp_2027"
    entry["motogp_sessions"] = [
        "fp1", "fp2", "sprint_qualifying", "fp3", "qualifying", "sprint", "race",
    ]
    entry["sessions"] = {
        "friday": {
            "moto3_fp1": {"local": "09:00", "gmt": "07:00", "duration_mins": 35},
            "motogp_fp1": {"local": "10:45", "gmt": "08:45", "duration_mins": 45},
            "motogp_fp2": {"local": "14:00", "gmt": "12:00", "duration_mins": 30},
            "motogp_sprint_qualifying": {
                "local": "15:00", "gmt": "13:00", "duration_mins": 15,
            },
        },
        "saturday": {
            "motogp_fp3": {"local": "10:10", "gmt": "08:10", "duration_mins": 20},
            "motogp_qualifying": {"local": "10:50", "gmt": "08:50", "duration_mins": 30},
            "motogp_sprint": {"local": "15:00", "gmt": "13:00", "duration_mins": 25},
        },
        "sunday": {
            # Even if warmup is left in a copied block, motogp_sessions omits it
            "motogp_warmup": {"local": "09:40", "gmt": "07:40", "duration_mins": 10},
            "moto3_race": {"local": "11:00", "gmt": "09:00", "duration_mins": 40},
            "moto2_race": {"local": "12:15", "gmt": "10:15", "duration_mins": 45},
            "motogp_race": {"local": "14:00", "gmt": "12:00", "duration_mins": 50},
        },
    }

    schedule = motogp_webui.build_round_schedule(entry, cal_2027)
    assert [s["full_name"] for s in schedule["friday"]["sessions"]] == [
        "Moto3 FP1",
        "MotoGP FP1",
        "MotoGP FP2",
        "MotoGP Sprint Qualifying",
    ]
    assert [s["full_name"] for s in schedule["saturday"]["sessions"]] == [
        "MotoGP FP3",
        "MotoGP Qualifying",
        "MotoGP Sprint",
    ]
    assert [s["full_name"] for s in schedule["sunday"]["sessions"]] == [
        "Moto3 Race",
        "Moto2 Race",
        "MotoGP Race",
    ]


def test_cancelled_and_legacy_rounds_have_empty_schedule(isolated_webui):
    rounds_2025 = motogp_webui.get_rounds_for_season(2025)
    assert rounds_2025 and rounds_2025[0]["schedule"] == {}

    cal_2026 = common.load_calendar(2026)
    cancelled = copy.deepcopy(cal_2026["rounds"][0])
    cancelled["status"] = "cancelled"
    assert motogp_webui.build_round_schedule(cancelled, cal_2026) == {}


def test_calendar_library_status_serialises_enum_names_for_download_selected(
    isolated_webui,
):
    """
    POST /download-selected looks up `SessionType[sel["session"]]`. `/calendar`
    used to pass the IntEnum value (21) and `/calendar-data` dropped `type`
    altogether, causing missing-session downloads from the Calendar modal to
    silently skip every selection.
    """
    client, _, _ = isolated_webui
    data = client.get("/calendar-data?year=2025").get_json()
    round_one_sessions = data["library_status"]["1"]["sessions"]
    race_entry = [s for s in round_one_sessions if s["name"] == "MotoGP Race"][0]

    assert race_entry["type"] == "MOTOGP_RACE"
    assert race_entry["episode"] == int(common.SessionType.MOTOGP_RACE)


# ---------------------------------------------------------------------------
# Client-side modal behaviour across clicked days, live weekends & timezones
# ---------------------------------------------------------------------------

def _extract_calendar_script(html: str) -> str:
    matches = re.findall(r"<script>(.*?)</script>", html, flags=re.DOTALL)
    assert len(matches) >= 2, "expected base script + calendar block script"
    return matches[-1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_all_calendar_marks_and_offers_multiple_same_day_meetings(
    isolated_webui, tmp_path, monkeypatch,
):
    client, config_dir, _ = isolated_webui
    # Qatar (MotoGP) and Brazil (F1) overlap on 6–8 November 2026.
    f1_calendar = REPO_ROOT / "config" / "f1_2026.json"
    shutil.copy2(f1_calendar, config_dir / "f1_2026.json")
    f1_lib = tmp_path / "f1-library"
    f1_lib.mkdir()
    monkeypatch.setenv("F1_LIBRARY_PATH", str(f1_lib))

    html = client.get("/calendar?year=2026&scope=all").get_data(as_text=True)
    calendar_js = _extract_calendar_script(html)
    harness = r"""
const elements = {
    'calendar-grid': { innerHTML: '', textContent: '' },
    'calendar-year': { innerHTML: '', textContent: '' },
    'modal-title': { innerHTML: '', textContent: '' },
    'modal-body': { innerHTML: '', textContent: '' },
    'modal-footer': { style: { display: 'none' } },
    'race-modal': { classList: { add() {}, remove() {}, contains() { return false; } } }
};
global.document = { getElementById: id => elements[id], addEventListener: () => {} };
global.window = { addEventListener: () => {}, location: { href: '' } };
global.fetch = () => Promise.resolve({ json: () => Promise.resolve({}) });
""" + calendar_js + r"""
renderCalendar();
showDateDetails('2026-11-08');
console.log(JSON.stringify({
    grid: elements['calendar-grid'].innerHTML,
    title: elements['modal-title'].textContent,
    body: elements['modal-body'].innerHTML
}));
"""
    harness_file = tmp_path / "all_calendar_harness.js"
    harness_file.write_text(harness, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness_file)], capture_output=True, text=True, check=True,
        env=os.environ.copy(),
    )
    rendered = json.loads(proc.stdout.strip())

    assert "series-dot motogp" in rendered["grid"]
    assert "series-dot f1" in rendered["grid"]
    assert "multi-series" in rendered["grid"]
    assert rendered["title"] == "Race weekends"
    assert "Formula 1" in rendered["body"]
    assert "MotoGP" in rendered["body"]
    assert "Qatar" in rendered["body"] and "Brazil" in rendered["body"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_modal_shows_clicked_day_in_browser_tz_and_live_or_past_states(
    isolated_webui, tmp_path
):
    client, _, _ = isolated_webui
    html = client.get("/calendar?year=2026").get_data(as_text=True)
    calendar_js = _extract_calendar_script(html)

    harness = r"""
const elements = {
    'calendar-grid': { innerHTML: '', textContent: '' },
    'calendar-year': { innerHTML: '', textContent: '' },
    'modal-title': { innerHTML: '', textContent: '' },
    'modal-body': { innerHTML: '', textContent: '' },
    'modal-footer': { style: { display: 'none' } },
    'calendar-selected-count': { textContent: '0' },
    'calendar-download-btn': { disabled: true },
    'race-modal': {
        _active: false,
        classList: {
            add(c) { if (c === 'active') elements['race-modal']._active = true; },
            remove(c) { if (c === 'active') elements['race-modal']._active = false; },
            contains(c) { return c === 'active' && elements['race-modal']._active; }
        }
    }
};
global.document = {
    getElementById: (id) => elements[id],
    addEventListener: () => {}
};
global.window = { addEventListener: () => {}, location: { href: '' } };
global.fetch = () => Promise.resolve({ json: () => Promise.resolve({}) });

const RealDate = Date;
let fakeNowIso = process.env.FAKE_NOW_ISO;
global.Date = class extends RealDate {
    constructor(...args) {
        if (args.length === 0) {
            super(fakeNowIso);
        } else {
            super(...args);
        }
    }
    static now() {
        return new RealDate(fakeNowIso).getTime();
    }
};
""" + calendar_js + r"""
renderCalendar();
const action = JSON.parse(process.env.MODAL_ACTION);
showRaceDetails(action.round, action.date);
if (action.switchDay) {
    selectModalDay(action.switchDay);
}
const out = {
    title: elements['modal-title'].textContent,
    body: elements['modal-body'].innerHTML,
    footerDisplay: elements['modal-footer'].style.display,
    gridHtml: elements['calendar-grid'].innerHTML
};
closeModal();
console.log(JSON.stringify(out));
"""
    harness_file = tmp_path / "calendar_harness.js"
    harness_file.write_text(harness, encoding="utf-8")

    def run_modal(fake_now_iso: str, tz: str, round_num: int, clicked_date: str, switch_day=None):
        env = os.environ.copy()
        env["TZ"] = tz
        env["FAKE_NOW_ISO"] = fake_now_iso
        env["MODAL_ACTION"] = json.dumps({
            "round": round_num,
            "date": clicked_date,
            "switchDay": switch_day,
        })
        proc = subprocess.run(
            ["node", str(harness_file)],
            env=env, capture_output=True, text=True, check=True,
        )
        return json.loads(proc.stdout.strip())

    # 1. Live Friday in New York (UTC-5): Qatar MotoGP FP1 (11:45Z) is underway at 12:00Z,
    #    shown as 06:45 local browser time; only Friday sessions appear (no Sprint / Warm Up),
    #    and "days to go" is replaced by NOW!
    fri_ny = run_modal("2026-11-06T12:00:00Z", "America/New_York", 20, "2026-11-06")
    assert "● NOW" in fri_ny["body"]
    assert "MotoGP FP1" in fri_ny["body"]
    assert "06:45" in fri_ny["body"]  # 11:45 UTC in EST (UTC-5)
    assert "45m" in fri_ny["body"]
    assert "days to go" not in fri_ny["body"]
    assert "Warm Up" not in fri_ny["body"]
    assert "Sprint" not in fri_ny["body"]

    # 2. Live weekend between sessions in London (GMT): shows Next session start time (14:15)
    #    and clicking Saturday shows Saturday's 7 sessions + Q1/Q2 split times
    sat_uk = run_modal("2026-11-06T13:00:00Z", "Europe/London", 20, "2026-11-07")
    assert "Next session" in sat_uk["body"]
    assert "Moto3 Practice" in sat_uk["body"]
    assert "14:15" in sat_uk["body"]
    assert "days to go" not in sat_uk["body"]
    assert "Saturday · 07-11-2026" in sat_uk["body"]
    assert "Q1 11:40 (15m) · Q2 12:05 (15m)" in sat_uk["body"]
    assert "Warm Up" not in sat_uk["body"]

    # 3. End of Friday (18:00Z on 2026-11-06): next session is Saturday morning -> "Sat 09:30"
    fri_evening = run_modal("2026-11-06T18:00:00Z", "Europe/London", 20, "2026-11-06")
    assert "Next session" in fri_evening["body"]
    assert "Sat 09:30" in fri_evening["body"]
    assert "Moto3 FP2" in fri_evening["body"]

    # 3b. Switching day tab inside the popup moves from Friday to Sunday's sessions
    switched_sun = run_modal(
        "2026-11-06T18:00:00Z", "Europe/London", 20, "2026-11-06", switch_day="sunday"
    )
    assert "Sunday · 08-11-2026" in switched_sun["body"]
    assert "Warm Up" in switched_sun["body"]
    assert "16:00" in switched_sun["body"]

    # 4. Sunday morning before Warm Up (09:00Z on 2026-11-08): must still be LIVE, not past!
    sun_morning = run_modal("2026-11-08T09:00:00Z", "Europe/London", 20, "2026-11-08")
    assert "Next session" in sun_morning["body"]
    assert "MotoGP Warm Up" in sun_morning["body"]
    assert "11:40" in sun_morning["body"]
    assert "Library Status:" not in sun_morning["body"]

    # 4b. Sunday evening after MotoGP Race ends (17:30Z on 2026-11-08): flips to past/library view
    sun_after = run_modal("2026-11-08T17:30:00Z", "Europe/London", 20, "2026-11-08")
    assert "Library Status:" in sun_after["body"]
    assert "day-timetable" not in sun_after["body"]

    # 5. Future round (Portugal Round 21 on 2026-11-20..22 viewed on 2026-11-06):
    #    shows "16" + "days to go" AND Friday's sessions when Friday 2026-11-20 is clicked
    future_pt = run_modal("2026-11-06T12:00:00Z", "Europe/London", 21, "2026-11-20")
    assert "16" in future_pt["body"]
    assert "days to go" in future_pt["body"]
    assert "Friday · 20-11-2026" in future_pt["body"]
    assert "Warm Up" not in future_pt["body"]

    # 6. Flyaway midnight shift: Australia Friday 09:00 AEDT (2026-10-22T22:00Z) viewed in
    #    London BST (UTC+1) on 2026-10-15 shows 23:00 with "Thu" day-shift tag
    aus_uk = run_modal("2026-10-15T12:00:00Z", "Europe/London", 18, "2026-10-23")
    assert "23:00" in aus_uk["body"]
    assert ">Thu<" in aus_uk["body"]

    # 7. Past round (Thailand Round 1 viewed on 2026-11-06): shows Library Status & checkboxes,
    #    NOT the session timetable
    past_th = run_modal("2026-11-06T12:00:00Z", "Europe/London", 1, "2026-03-01")
    assert "Library Status:" in past_th["body"]
    assert "day-timetable" not in past_th["body"]
    assert past_th["footerDisplay"] == "flex"


def test_the_real_2027_calendar_renders_in_running_order():
    """
    The 2027 calendar is data, not a fixture: the WebUI must render a real
    2027 round with Friday FP2 and Sprint Qualifying, Saturday FP3, and no
    Sunday warm-up. This is the end-to-end check that the format is wired.
    """
    calendar = json.loads(
        (REPO_ROOT / "config" / "motogp_2027.json").read_text(encoding="utf-8")
    )
    assert calendar["season"] == 2027

    entry = calendar["rounds"][0]  # Thailand, +07:00
    schedule = motogp_webui.build_round_schedule(entry, calendar)
    assert set(schedule) == {"friday", "saturday", "sunday"}

    friday = [s["key"] for s in schedule["friday"]["sessions"]]
    assert friday == [
        "moto3_fp1", "motogp_fp1", "moto2_fp1",
        "moto3_practice", "motogp_fp2", "motogp_sprint_qualifying",
        "moto2_practice",
    ]
    saturday = [s["key"] for s in schedule["saturday"]["sessions"]]
    assert saturday == [
        "moto3_fp2", "motogp_fp3", "moto2_fp2", "moto3_qualifying",
        "motogp_qualifying", "motogp_sprint", "moto2_qualifying",
    ]
    sunday = [s["key"] for s in schedule["sunday"]["sessions"]]
    assert sunday == ["moto3_race", "moto2_race", "motogp_race"]
    assert "motogp_warmup" not in friday + saturday + sunday

    fp2 = [s for s in schedule["friday"]["sessions"] if s["key"] == "motogp_fp2"][0]
    assert fp2["start_utc"] == "2027-03-05T07:00:00Z"
    assert fp2["end_utc"] == "2027-03-05T07:30:00Z"
    assert fp2["full_name"] == "MotoGP FP2"


def test_download_selected_accepts_a_string_season(isolated_webui, monkeypatch):
    """
    The Library page sends the season from a <select>, so it is a string.
    element_number() compares it to an int and raises TypeError; the route
    used to return 500 and the UI showed "Failed to start download" while
    every connection test was green.
    """
    client, _, _ = isolated_webui

    class _NoThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    # Don't actually spawn the grabber in the test.
    monkeypatch.setattr(motogp_webui.threading, "Thread", _NoThread)

    response = client.post("/download-selected", json={
        "season": "2026",
        "selections": [{"round": 19, "session": "MOTOGP_RACE"}],
    })
    assert response.status_code == 200, response.data
    assert response.get_json()["status"] == "started"
    assert response.get_json()["commands"] == [
        "backfill --season 2026 --round 19 --sessions E82 --force"
    ]


def test_download_selected_rejects_an_invalid_season(isolated_webui):
    client, _, _ = isolated_webui
    response = client.post("/download-selected", json={
        "season": "not-a-year",
        "selections": [{"round": 19, "session": "MOTOGP_RACE"}],
    })
    assert response.status_code == 400
