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

def test_both_championships_are_offered(two_series):
    client, _ = two_series

    body = client.get("/").get_data(as_text=True)

    assert "series-switcher" in body
    assert ">MotoGP<" in body
    assert ">Formula 1<" in body
    # Each option is a link to this page for the other series, not a bare "/".
    assert "series=f1" in body and "series=motogp" in body


def test_every_tab_link_keeps_the_selected_series(two_series):
    """
    A tab that dropped the series would show MotoGP's data under the F1 header -
    the exact confusion the switcher exists to remove.
    """
    client, _ = two_series

    body = client.get("/calendar?series=f1").get_data(as_text=True)

    for path in ("/", "/library", "/settings-page", "/stats", "/calendar",
                 "/activity"):
        assert f'href="{path}?series=f1"' in body, path


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


def test_an_unknown_series_falls_back_rather_than_rendering_nothing(two_series):
    client, _ = two_series

    payload = client.get("/calendar-data?series=indycar").get_json()

    assert payload["series"] == "motogp"


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

    assert "<h2>Formula 1</h2>" in f1
    assert "<h2>MotoGP</h2>" in motogp
    # ...and the app's own name is not silently rewritten by a series switch.
    assert "MotoGParr" in f1 and "MotoGParr" in motogp


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
        json={"sessions": {"RACE": True, "SPRINT": False}},
    )
    assert response.status_code == 200

    stored = json.loads((config_dir / "webui_settings.motogp.json").read_text())
    assert set(stored) == {"f1_sessions"}
    assert stored["f1_sessions"] == {"RACE": True, "SPRINT": False}


def test_every_started_job_names_its_series(two_series, monkeypatch):
    """
    Every child process binds a vocabulary at import. An argv without
    ``--series`` is a MotoGP job whatever tab started it.
    """
    client, _ = two_series
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
