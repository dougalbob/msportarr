"""
The WebUI series switcher (hybrid step 8).

One process serves both championships, and ``motogp_common`` is bound to the
default series at import. So every page has a choice to make, and the two ways
to get it wrong both look like working software:

* render F1's page from MotoGP's vocabulary - the rounds and venues come from
  the wrong calendar, element numbers are decoded with the wrong table, and the
  library walk reports F1's folders as MotoGP's sessions;
* send a job to a child process without saying which series it is for - the
  grabber binds at import, so an F1 "search this round" silently searches
  MotoGP's round.

The tests here check what a user can see (the switcher, the right calendar, the
right library) and what a child process receives (``--series`` on every argv),
rather than the internals that produce them.
"""

import json

import pytest

pytest.importorskip("flask")

import motogp_webui
from series import get_series


@pytest.fixture
def two_series(tmp_path, monkeypatch):
    """
    An install that follows both championships, with both calendars present.

    The F1 library path is set so ``active_series()`` reports F1 configured -
    which is the same rule the container uses, so the switcher's visibility is
    tested against the real activation logic rather than a fake.
    """
    import shutil

    from pathlib import Path

    repo_config = Path(__file__).resolve().parent.parent / "config"
    for name in ("motogp_2026.json", "f1_2026.json"):
        shutil.copy2(repo_config / name, tmp_path / name)

    (tmp_path / "motogp_lib").mkdir()
    (tmp_path / "f1_lib").mkdir()

    monkeypatch.setenv("MOTOGP_LIBRARY_PATH", str(tmp_path / "motogp_lib"))
    monkeypatch.setenv("F1_LIBRARY_PATH", str(tmp_path / "f1_lib"))
    monkeypatch.setattr(motogp_webui, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        motogp_webui, "WEBUI_CONFIG_FILE", tmp_path / "webui_settings.motogp.json"
    )
    motogp_webui.app.config.update(TESTING=True)

    with motogp_webui.app.test_client() as client:
        yield client, tmp_path


# ---------------------------------------------------------------------------
# The switcher itself
# ---------------------------------------------------------------------------

def test_read_views_default_to_all_and_offer_each_series_filter(two_series):
    client, _ = two_series

    body = client.get("/").get_data(as_text=True)

    assert "series-switcher" in body
    assert 'data-series="all"' in body
    assert 'data-series="motogp"' in body
    assert 'data-series="f1"' in body
    assert "scope=all" in body
    assert "scope=motogp" in body and "scope=f1" in body
    # Home's All view presents both championship summaries side by side.
    assert 'data-series="motogp"' in body and 'data-series="f1"' in body
    assert "All active championships" in body


def test_every_tab_link_keeps_the_selected_series(two_series):
    """
    A tab that dropped the series would show MotoGP's data under the F1 header -
    the exact confusion the switcher exists to remove.
    """
    client, _ = two_series

    body = client.get("/calendar?series=f1").get_data(as_text=True)

    for path in ("/", "/stats", "/calendar", "/activity"):
        assert f'href="{path}?scope=f1"' in body, path
    for path in ("/library", "/settings-page"):
        assert f'href="{path}?series=f1"' in body, path


def test_download_and_settings_pages_remain_single_series_targets(two_series):
    client, _ = two_series

    download = client.get("/library?series=f1")
    settings = client.get("/settings-page?series=f1")

    assert download.status_code == settings.status_code == 200
    download_body = download.get_data(as_text=True)
    settings_body = settings.get_data(as_text=True)

    for body in (download_body, settings_body):
        assert "Select Championship" in body
        assert "Formula 1" in body
        assert 'data-series="all"' not in body
        assert 'class="series-switcher-wrap"' not in body
    assert 'href="/library?series=f1" class="tab active">Download</a>' in download_body
    assert 'href="/settings-page?series=f1" class="tab active">Settings</a>' in settings_body
    assert 'href="/library?series=motogp"' in download_body
    assert 'href="/settings-page?series=motogp"' in settings_body
    assert 'const SETTINGS_SERIES = "f1"' in settings_body
    assert "Selected sessions will only be searched for this championship." in download_body
    assert "Wanted-session preferences are saved only for this championship." in settings_body


def test_an_explicit_series_is_remembered_for_bare_requests(two_series):
    """
    A fetch or form POST has no query string to carry the series, so it would
    fall back to the default - a settings save from the F1 tab would rewrite
    MotoGP's wanted list. The cookie is the only channel those requests have.
    """
    client, _ = two_series

    client.get("/calendar?series=f1")

    assert client.get("/calendar-data").get_json()["series"] == "f1"
    assert client.get("/calendar-data").get_json()["year"] == 2026


def test_all_calendar_api_merges_both_series_with_identity(two_series):
    client, _ = two_series

    payload = client.get("/calendar-data?year=2026&scope=all").get_json()

    assert payload["series"] == "all"
    assert set(payload["series_keys"]) == {"motogp", "f1"}
    rounds = payload["calendar_data"]
    assert {round_info["series"] for round_info in rounds} == {"motogp", "f1"}
    assert {round_info["series_name"] for round_info in rounds} == {"MotoGP", "Formula 1"}
    assert len(rounds) == 22 + 24
    assert set(payload["library_status_by_series"]) == {"motogp", "f1"}


def test_an_unknown_series_falls_back_rather_than_rendering_nothing(two_series):
    client, _ = two_series

    payload = client.get("/calendar-data?series=indycar").get_json()

    assert payload["series"] == "motogp"


def test_all_activity_feed_merges_and_labels_active_and_recent_events(
    two_series, monkeypatch,
):
    client, _ = two_series
    by_series = {
        "motogp": {
            "active": [{"id": "2026_R01_E82", "timestamp": "2026-11-08T10:00:00", "stage": "downloading"}],
            "history": [{"id": "2026_R01_E82", "timestamp": "2026-11-07T10:00:00", "stage": "complete"}],
        },
        "f1": {
            "active": [{"id": "2026_R01_E70", "timestamp": "2026-11-08T11:00:00", "stage": "downloading"}],
            "history": [{"id": "2026_R01_E70", "timestamp": "2026-11-07T11:00:00", "stage": "complete"}],
        },
    }
    monkeypatch.setattr(
        motogp_webui, "get_all_activity", lambda series=None: by_series[series]
    )

    payload = client.get("/api/activity-events?scope=all").get_json()

    assert payload["scope"] == "all"
    assert [event["series"] for event in payload["active"]] == ["f1", "motogp"]
    assert [event["series_name"] for event in payload["history"]] == ["Formula 1", "MotoGP"]
    assert len(payload["active"]) == len(payload["history"]) == 2

    f1_only = client.get("/api/activity-events?scope=f1").get_json()
    assert f1_only["series_keys"] == ["f1"]
    assert all(event["series"] == "f1" for event in f1_only["active"] + f1_only["history"])


def test_clearing_activity_only_clears_the_visible_scope(two_series, monkeypatch):
    client, _ = two_series
    cleared = []
    monkeypatch.setattr(
        motogp_webui, "clear_all_activity", lambda series=None: cleared.append(series)
    )

    one = client.post("/api/activity-events/clear?scope=f1")
    assert one.status_code == 200
    assert cleared == ["f1"]

    cleared.clear()
    all_series = client.post("/api/activity-events/clear?scope=all")
    assert all_series.status_code == 200
    assert set(cleared) == {"motogp", "f1"}

    monkeypatch.setattr(motogp_webui, "clear_all_activity", lambda series=None: False)
    failed = client.post("/api/activity-events/clear?scope=f1")
    assert failed.status_code == 500
    assert "Could not clear f1 activity" in failed.get_json()["error"]


def test_a_series_the_install_does_not_follow_is_not_selectable(two_series,
                                                               monkeypatch):
    """
    F1 unconfigured means F1 is dormant: it must not be offered, and asking for
    it must not render an empty dashboard that looks like lost data.
    """
    monkeypatch.setenv("F1_LIBRARY_PATH", "")

    payload = two_series[0].get("/calendar-data?series=f1").get_json()
    assert payload["series"] == "motogp"

    body = two_series[0].get("/").get_data(as_text=True)
    assert "series-switcher" not in body


# ---------------------------------------------------------------------------
# The page content follows the series
# ---------------------------------------------------------------------------

def test_stats_route_aggregates_all_and_keeps_a_per_series_breakdown(two_series):
    client, _ = two_series

    all_view = client.get("/stats?scope=all")
    assert all_view.status_code == 200
    body = all_view.get_data(as_text=True)
    assert "All-Series Statistics" in body
    assert "By Series" in body
    assert "MotoGP" in body and "Formula 1" in body

    f1_view = client.get("/stats?scope=f1")
    assert f1_view.status_code == 200
    assert "Formula 1 Statistics" in f1_view.get_data(as_text=True)


def test_the_calendar_renders_the_requested_championships_rounds(two_series):
    """
    The assertion that would have caught a shared vocabulary: the two calendars
    do not even share their first venue in 2026 (Thailand vs Australia), so a
    page rendering the wrong one is unmistakable.
    """
    client, _ = two_series

    motogp = client.get("/calendar?series=motogp").get_data(as_text=True)
    formula1 = client.get("/calendar?series=f1").get_data(as_text=True)

    assert "Thailand" in motogp and "Australia" in motogp
    assert "Australia" in formula1 and "Thailand" not in formula1
    assert len(motogp_webui.get_rounds_for_season(2026, "motogp")) == 22
    assert len(motogp_webui.get_rounds_for_season(2026, "f1")) == 24


def test_f1_sessions_are_described_by_f1_vocabulary(two_series):
    """
    ``f1_race`` must resolve through F1's calendar-key table. Read with MotoGP's
    it returns nothing and the meeting shows an empty timetable - a page that
    looks built but says the race has no sessions.
    """
    first = motogp_webui.get_rounds_for_season(2026, "f1")[0]

    sessions = [
        entry
        for day in first["schedule"].values()
        for entry in day["sessions"]
    ]
    assert [s["key"] for s in sessions] == [
        "f1_fp1", "f1_fp2", "f1_fp3", "f1_qualifying", "f1_race",
    ]
    race = sessions[-1]
    assert race["full_name"] == "F1 Race"
    assert race["start_utc"] == "2026-03-08T04:00:00Z"


def test_the_home_tab_names_the_selected_championship(two_series):
    client, _ = two_series

    f1 = client.get("/?series=f1").get_data(as_text=True)
    motogp = client.get("/?series=motogp").get_data(as_text=True)

    assert 'data-series="f1"' in f1 and ">Formula 1<" in f1
    assert 'data-series="motogp"' in motogp and ">MotoGP<" in motogp
    # ...and the app's own name is not silently rewritten by a series switch.
    assert "Motorsportarr" in f1 and "Motorsportarr" in motogp


def test_seasons_are_discovered_per_series(two_series):
    """
    A glob of ``motogp_*.json`` returns nothing for F1, which reads in the UI as
    "this championship has no seasons at all".
    """
    _, config_dir = two_series
    (config_dir / "f1_2027.json").write_text(json.dumps({"rounds": []}))

    assert motogp_webui.discover_seasons("f1") == [2026, 2027]
    assert motogp_webui.discover_seasons("motogp") == [2026]


# ---------------------------------------------------------------------------
# State, libraries and jobs are per series
# ---------------------------------------------------------------------------

def test_the_library_walk_uses_the_requested_series_root(two_series, monkeypatch):
    client, config_dir = two_series
    # A session is a *folder* named by its prefix, with the media inside it -
    # that is what the parser and the sort produce. Round folders for both
    # series, each in its own library, each claiming to be round 1.
    for root, prefix in ((config_dir / "motogp_lib", "26R01E82"),
                         (config_dir / "f1_lib", "26R01E70")):
        session_dir = root / "2026" / "Round 01 Test" / f"{prefix} Session"
        session_dir.mkdir(parents=True)
        (session_dir / "race.mkv").write_bytes(b"x")

    monkeypatch.setattr(
        motogp_webui, "library_root",
        lambda key=None: str(config_dir / f"{'f1' if key == 'f1' else 'motogp'}_lib"),
    )

    assert list(motogp_webui.scan_library_for_season(2026, "f1")) == [1]
    assert list(motogp_webui.scan_library_for_season(2026, "motogp")) == [1]
    # And each resolved a *different* session from the same folder name in the
    # same round - E70 is F1's race and E82 is MotoGP's. The session that comes
    # back is what proves the parser was the series' own.
    f1 = motogp_webui.scan_library_for_season(2026, "f1")[1]
    motogp = motogp_webui.scan_library_for_season(2026, "motogp")[1]
    assert [s.name for s in f1] == ["RACE"]
    assert [s.name for s in motogp] == ["MOTOGP_RACE"]


def test_grabber_state_is_read_per_series(two_series):
    """
    Both files can hold a round 1 element 82 key, because 82 is that series'
    race. Reading the shared one would report the other championship's download.
    """
    client, config_dir = two_series
    (config_dir / "grabber_state.f1.json").write_text(
        json.dumps({"grabbed": {"2026_R01_E70": {"release": "F1.Australia"}}})
    )
    (config_dir / "grabber_state.motogp.json").write_text(
        json.dumps({"grabbed": {"2026_R01_E82": {"release": "MotoGP.Thailand"}}})
    )

    assert "2026_R01_E70" in motogp_webui._load_grabber_state("f1")["grabbed"]
    assert "2026_R01_E82" in motogp_webui._load_grabber_state("motogp")["grabbed"]


def test_a_wanted_sessions_save_only_touches_its_own_series(two_series):
    """
    The settings form is the same for both series, so a save from the F1 tab
    must not rewrite MotoGP's wanted list. That is the collision that makes
    "some sessions stopped downloading" after configuring the other series.
    """
    client, config_dir = two_series

    response = client.post(
        "/settings?series=f1",
        json={"series": "f1", "sessions": {"RACE": True, "SPRINT": False}},
    )
    assert response.status_code == 200

    stored = json.loads((config_dir / "webui_settings.f1.json").read_text())
    assert stored == {"sessions": {"RACE": True, "SPRINT": False}}
    # The MotoGP target remains untouched by an F1 settings submission.
    assert not (config_dir / "webui_settings.motogp.json").exists()


def test_settings_reject_an_inactive_explicit_target(two_series):
    response = two_series[0].post(
        "/settings",
        json={"series": "indycar", "sessions": {"RACE": True}},
    )

    assert response.status_code == 400
    assert not (two_series[1] / "webui_settings.f1.json").exists()


def test_calendar_download_uses_explicit_f1_target_and_f1_episode_number(
    two_series, monkeypatch,
):
    from utils.webui_jobs import JobManager

    client, _ = two_series
    started = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            self.args = args
            started.append(self.args)

        def start(self):
            pass

    monkeypatch.setattr(motogp_webui, "jobs", JobManager())
    monkeypatch.setattr(motogp_webui.threading, "Thread", FakeThread)

    response = client.post("/download-selected", json={
        "season": 2026,
        "series": "f1",
        "selections": [{"round": 1, "session": "RACE"}],
    })

    assert response.status_code == 200
    assert started
    argv = started[0][0][0][0]
    assert argv[2:4] == ["--series", "f1"]
    assert argv[argv.index("--sessions") + 1] == "E70"


def test_calendar_download_rejects_an_inactive_explicit_target(two_series):
    response = two_series[0].post("/download-selected", json={
        "season": 2026,
        "series": "indycar",
        "selections": [{"round": 1, "session": "RACE"}],
    })

    assert response.status_code == 400


def test_every_started_job_names_its_series(two_series, monkeypatch):
    """
    Every child process binds a vocabulary at import. An argv without
    ``--series`` is a MotoGP job whatever tab started it.
    """
    from utils.webui_jobs import JobManager

    client, _ = two_series
    monkeypatch.setattr(motogp_webui, "jobs", JobManager())
    started = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            pass

        def start(self):
            started.append(True)

    monkeypatch.setattr(motogp_webui.threading, "Thread", FakeThread)

    for series in ("f1", "motogp"):
        started.clear()
        motogp_webui.jobs.state["running"] = False
        response = client.post(f"/run?series={series}",
                               json={"action": "status"})
        assert response.status_code == 200
        assert response.get_json()["command"].startswith(f"{series}-grab")


def test_the_series_switcher_is_css_backed():
    """
    The switcher is markup this project added; a class with no rule renders as
    an unstyled list of links and reads as a navigation bug.
    """
    from pathlib import Path

    css = (Path(__file__).resolve().parent.parent
           / "static" / "css" / "style.css").read_text()
    assert ".series-switcher" in css
    assert ".series-switch.active" in css
    assert ".championship-picker-button" in css
    assert ".championship-picker-menu" in css
