"""
Explaining ordinary search and processing failures (Candidate C).

Before this, every way a session could fail was stored as one of three strings:
"No results", "Grab failed", "Processor returned False". Those are different
problems wearing the same label, and the difference is the diagnosis:

* Prowlarr unreachable or its key wrong -> a human has to fix it now;
* nothing published yet -> waiting is correct and nothing is wrong.

The tests below pin that distinction at the layer that decides it
(`utils/failure_reasons.py`), at the layer that produces the evidence
(`motogp_grabber.search_for_session` / `search_indexer` / `grab_release`), and
at the layer the user reads (`/failed-sessions` and the Download tab dialog).

Redaction is tested as a security property, not a nicety: these strings go into
`grabber_state.json`, the UI and any "copy report" the user makes, and this
repository has already published a live Prowlarr key once (AGENTS.md §7a).
"""

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

pytest.importorskip("flask")

REPO_ROOT = Path(__file__).resolve().parent.parent

os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp(prefix="motogp-failure-test-"))
os.environ.setdefault("AUTH_MODE", "none")

import motogp_grabber as grabber  # noqa: E402
import motogp_webui  # noqa: E402
from motogp_common import SessionType  # noqa: E402
from utils import failure_reasons as fr  # noqa: E402


# ===========================================================================
# The vocabulary
# ===========================================================================

def test_every_stage_has_a_label_and_a_hint():
    """A stage without a hint would show the user a fault and no next step."""
    for stage in fr.STAGES:
        assert fr.LABELS[stage]
        assert fr.HINTS[stage]


def test_the_stages_that_need_a_human_are_the_ones_that_are_not_waiting():
    """
    The single most useful bit of the record: whether retrying can possibly
    help. "Not published yet" and "rejected by the rules" are self-resolving or
    deliberate; a dead service never fixes itself.
    """
    assert fr.STAGE_NO_CANDIDATES not in fr.ACTIONABLE
    assert fr.STAGE_REJECTED not in fr.ACTIONABLE
    assert fr.STAGE_BLOCKED not in fr.ACTIONABLE
    for stage in (
        fr.STAGE_SEARCH_SETUP,
        fr.STAGE_SEARCH_INDEXER,
        fr.STAGE_SAB_SUBMIT,
        fr.STAGE_DOWNLOAD,
        fr.STAGE_PROCESS,
    ):
        assert stage in fr.ACTIONABLE, stage


def test_an_unknown_stage_is_refused_rather_than_stored():
    with pytest.raises(ValueError):
        fr.make("something_else", "detail")


def test_the_reason_line_always_names_the_stage():
    """
    "Grab failed" alone told the user nothing. The summary keeps the plain
    sentence shape every existing consumer reads, but prefixes the actor.
    """
    record = fr.make(fr.STAGE_SAB_SUBMIT, "SABnzbd is unreachable from this container")
    assert record["reason"] == (
        "Downloader: SABnzbd is unreachable from this container"
    )
    assert record["stage"] == fr.STAGE_SAB_SUBMIT
    assert record["actionable"] is True
    assert record["hint"] == fr.HINTS[fr.STAGE_SAB_SUBMIT]


def test_a_record_without_detail_still_reads_as_a_sentence():
    record = fr.make(fr.STAGE_NO_CANDIDATES)
    assert record["reason"] == "Not published yet"


# ===========================================================================
# Redaction (security property)
# ===========================================================================

def test_urls_keep_their_host_and_lose_everything_else():
    """
    Prowlarr's search URL carries the API key in its query string, and a
    requests exception renders the whole URL. Which host failed is useful;
    the path and query are not.
    """
    message = (
        "HTTPConnectionPool(host='prowlarr', port=9696): Max retries exceeded "
        "with url: /1/api?t=search&q=MotoGP&apikey=abc123def456ghi789&limit=100"
    )
    cleaned = fr.sanitise(message)
    assert "abc123def456ghi789" not in cleaned
    assert "apikey=***" in cleaned


def test_configured_secrets_are_removed_even_without_a_recognisable_parameter():
    key = "s3cr3tK3yThatAppearsBare"
    cleaned = fr.sanitise(f"failed with {key} in the header", secrets=[key])
    assert key not in cleaned
    assert "***" in cleaned


def test_a_bare_long_key_is_redacted():
    """The shape of a Prowlarr/SABnzbd key, even with no parameter name."""
    key = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    assert key not in fr.sanitise(f"rejected: {key}")


def test_sanitising_is_idempotent_and_safe_on_empty_input():
    once = fr.sanitise("http://sab:8080/api?apikey=xyz123")
    assert fr.sanitise(once) == once
    assert fr.sanitise(None) == ""


def test_a_url_keeps_its_port_so_the_reader_knows_which_service():
    cleaned = fr.sanitise("http://192.168.1.50:9696/1/api?apikey=abc")
    assert cleaned == "http://192.168.1.50:9696"


# ===========================================================================
# From search counters to a cause
# ===========================================================================

def test_a_setup_error_outranks_an_empty_result():
    """
    Both produce zero candidates. One is a broken machine, the other is a
    quiet weekend, and reporting "no results" for the first is the lie this
    removes.
    """
    cause = fr.explain_search({"setup_error": "no enabled Prowlarr indexers",
                               "candidates": 0})
    assert cause["stage"] == fr.STAGE_SEARCH_SETUP
    assert cause["actionable"] is True


def test_every_indexer_failing_is_not_reported_as_nothing_published():
    cause = fr.explain_search({
        "indexers": 2, "indexers_failed": 2, "candidates": 0,
        "errors": ["Prowlarr is unreachable from this container"],
    })
    assert cause["stage"] == fr.STAGE_SEARCH_INDEXER
    assert "unreachable" in cause["detail"]
    assert cause["actionable"] is True


def test_some_indexers_failing_is_reported_but_not_called_a_failure():
    """
    Partial results are incomplete, not broken. Calling it a failure would send
    the user to check a service that is half working, every weekend.
    """
    cause = fr.explain_search({
        "indexers": 3, "indexers_failed": 1, "candidates": 0, "errors": ["idx: HTTP 500"],
    })
    assert cause["stage"] == fr.STAGE_NO_CANDIDATES
    assert "1 of 3" in cause["detail"]
    assert cause["actionable"] is False


def test_nothing_published_yet_is_not_actionable():
    cause = fr.explain_search({"indexers": 1, "indexers_failed": 0, "candidates": 0})
    assert cause["stage"] == fr.STAGE_NO_CANDIDATES
    assert cause["actionable"] is False
    assert "retries automatically" in cause["hint"]


def test_candidates_that_all_failed_the_filter_are_reported_as_rejected():
    cause = fr.explain_search({
        "indexers": 1, "candidates": 4, "filtered": 0, "all_blocked": False,
    })
    assert cause["stage"] == fr.STAGE_REJECTED
    assert "4 candidate(s)" in cause["detail"]
    # Rejection is what the mismatch dialog exists for, and the hint says so.
    assert "mismatch" in cause["hint"]


def test_candidates_the_user_blocked_are_distinguished_from_rule_rejection():
    """
    "The rules rejected everything" and "I blocked everything" both leave
    nothing to grab. Only the second is the user's own doing, and telling them
    the rules are wrong would send them to edit patterns that work.
    """
    cause = fr.explain_search({
        "indexers": 1, "candidates": 2, "filtered": 0, "all_blocked": True,
    })
    assert cause["stage"] == fr.STAGE_BLOCKED
    assert cause["actionable"] is False


def test_no_diagnostics_at_all_falls_back_to_the_harmless_answer():
    """
    Without counters nothing can be claimed, so the answer is the one that
    cannot send the user chasing a service that is fine: "nothing found yet",
    explicitly not actionable. Inventing "Prowlarr is down" from missing data
    would be worse than saying nothing.
    """
    for empty in (None, {}):
        cause = fr.explain_search(empty)
        assert cause["stage"] == fr.STAGE_NO_CANDIDATES
        assert cause["actionable"] is False


def test_a_successful_search_has_no_failure_stage():
    cause = fr.explain_search({
        "indexers": 1, "candidates": 3, "filtered": 2, "all_blocked": False,
    })
    assert cause["stage"] == ""


# ===========================================================================
# Reading stored entries
# ===========================================================================

def test_a_legacy_entry_is_shown_without_inventing_a_stage():
    """
    Every existing installation has {"timestamp", "reason"} entries. Labelling
    them would mean guessing, and guessing "No results" means "nothing
    published" would relabel historic outages as quiet weekends.
    """
    described = fr.describe({"timestamp": "2026-11-01T10:00:00", "reason": "No results"})
    assert described["stage"] == ""
    assert described["stage_label"] == ""
    assert described["reason"] == "No results"
    assert described["actionable"] is False


def test_a_structured_entry_survives_a_round_trip_through_the_store():
    record = fr.make(fr.STAGE_SEARCH_SETUP, "no enabled Prowlarr indexers")
    described = fr.describe(json.loads(json.dumps(record)))
    assert described["stage"] == fr.STAGE_SEARCH_SETUP
    assert described["actionable"] is True
    assert described["reason"] == record["reason"]


def test_a_junk_entry_is_described_rather_than_crashing_the_route():
    assert fr.describe("garbage")["reason"] == "Unknown failure"
    assert fr.describe(None)["reason"] == "Unknown failure"


# ===========================================================================
# The grabber produces the evidence
# ===========================================================================

ROUND = {"round": 19, "venue": "Malaysia", "date": "2026-11-08",
         "weekend_start": "2026-11-06", "status": "scheduled", "classes": ["motogp"]}


def _search(monkeypatch, indexer_results, indexers=None):
    """Run search_for_session with a scripted indexer and return diagnostics."""
    monkeypatch.setattr(grabber, "build_search_queries", lambda *a: ["q"])
    monkeypatch.setattr(grabber, "search_indexer", indexer_results)
    monkeypatch.setattr(grabber, "get_required_quality", lambda: "1080p")
    monkeypatch.setattr(grabber, "get_allowed_groups", lambda: [])
    monkeypatch.setattr(grabber, "_blocked_titles", lambda *a: set())

    diagnostics = {}
    results = grabber.search_for_session(
        ROUND, SessionType.MOTO3_PRACTICE, 2026,
        indexers if indexers is not None else [{"id": 1, "name": "idx"}],
        None,
        diagnostics=diagnostics,
    )
    return results, diagnostics


def test_an_unreachable_prowlarr_is_recorded_as_an_indexer_failure(monkeypatch):
    results, diag = _search(
        monkeypatch,
        lambda *a: ([], "Prowlarr is unreachable from this container"),
    )
    assert results == []
    cause = fr.explain_search(diag)
    assert cause["stage"] == fr.STAGE_SEARCH_INDEXER
    assert "unreachable" in cause["detail"]


def test_no_enabled_indexers_is_a_setup_error_not_an_empty_search(monkeypatch):
    results, diag = _search(monkeypatch, lambda *a: ([], None), indexers=[])
    assert results == []
    assert diag["setup_error"] == "no enabled Prowlarr indexers"
    assert fr.explain_search(diag)["stage"] == fr.STAGE_SEARCH_SETUP


def test_an_answering_indexer_with_no_releases_is_not_a_failure(monkeypatch):
    results, diag = _search(monkeypatch, lambda *a: ([], None))
    assert results == []
    cause = fr.explain_search(diag)
    assert cause["stage"] == fr.STAGE_NO_CANDIDATES
    assert cause["actionable"] is False


def test_a_partial_indexer_oute_is_counted_but_the_search_is_not_a_failure(monkeypatch):
    calls = {"n": 0}

    def flaky(*a):
        calls["n"] += 1
        if calls["n"] == 1:
            return [], "idx-one timed out"
        return [], None

    results, diag = _search(
        monkeypatch, flaky,
        indexers=[{"id": 1, "name": "one"}, {"id": 2, "name": "two"}],
    )
    assert diag["indexers"] == 2
    assert diag["indexers_failed"] == 1
    cause = fr.explain_search(diag)
    assert cause["stage"] == fr.STAGE_NO_CANDIDATES
    assert "1 of 2" in cause["detail"]


def test_search_indexer_never_puts_the_api_key_in_its_error(monkeypatch):
    """
    The error text is a first line of defence; sanitise() is the second. It
    must not carry the URL at all, because the URL has `apikey=` in it.
    """
    import requests

    class _Response:
        status_code = 401

        def json(self):
            return {}

    monkeypatch.setattr(grabber, "get_prowlarr_url", lambda: "http://prowlarr:9696")
    monkeypatch.setattr(grabber, "get_prowlarr_api_key", lambda: "supersecretkey123456")
    monkeypatch.setattr(grabber.requests, "get", lambda *a, **k: _Response())

    releases, error = grabber.search_indexer("q", 1, "idx")
    assert releases == []
    assert "supersecretkey123456" not in error
    assert "401" in error


def test_search_indexer_reports_a_timeout_as_such(monkeypatch):
    import requests

    monkeypatch.setattr(grabber, "get_prowlarr_url", lambda: "http://prowlarr:9696")
    monkeypatch.setattr(grabber, "get_prowlarr_api_key", lambda: "k" * 20)

    def boom(*a, **k):
        raise requests.exceptions.Timeout()

    monkeypatch.setattr(grabber.requests, "get", boom)
    _, error = grabber.search_indexer("q", 1, "idx")
    assert "timed out" in error


def test_grab_release_reports_why_sabnzbd_refused(monkeypatch):
    """
    An unreachable SABnzbd and a refused release both used to become "Grab
    failed". One needs the service fixed, the other needs a different release.
    """
    import requests

    monkeypatch.setattr(grabber, "get_sabnzbd_url", lambda: "http://sab:8080")
    monkeypatch.setattr(grabber, "get_sabnzbd_api_key", lambda: "k" * 20)

    def unreachable(*a, **k):
        raise requests.exceptions.ConnectionError()

    monkeypatch.setattr(grabber.requests, "get", unreachable)
    nzo_id, error = grabber.grab_release(
        {"downloadUrl": "http://x/1.nzb", "title": "t"},
        {"round": 1, "round_info": {"venue": "V"}, "session_type": SessionType.MOTOGP_RACE},
        2026,
    )
    assert nzo_id is None
    assert "unreachable" in error


def test_grab_release_reports_a_release_with_no_download_link(monkeypatch):
    monkeypatch.setattr(grabber, "get_sabnzbd_url", lambda: "http://sab:8080")
    monkeypatch.setattr(grabber, "get_sabnzbd_api_key", lambda: "k" * 20)
    nzo_id, error = grabber.grab_release(
        {"title": "no link"},
        {"round": 1, "round_info": {"venue": "V"}, "session_type": SessionType.MOTOGP_RACE},
        2026,
    )
    assert nzo_id is None
    assert "no download link" in error


def test_grabber_state_still_accepts_a_plain_string_reason(tmp_path):
    """Every existing caller and file on disk uses the string form."""
    state = grabber.GrabberState.__new__(grabber.GrabberState)
    state.state = {"searched": {}, "grabbed": {}, "failed": {}}

    state.mark_failed("2026_R01_E10", "No results")
    entry = state.state["failed"]["2026_R01_E10"]
    assert entry["reason"] == "No results"
    assert "stage" not in entry


def test_grabber_state_stores_a_structured_record(tmp_path):
    state = grabber.GrabberState.__new__(grabber.GrabberState)
    state.state = {"searched": {}, "grabbed": {}, "failed": {}}

    record = fr.make(fr.STAGE_SEARCH_SETUP, "no enabled Prowlarr indexers")
    state.mark_failed("2026_R01_E10", record)

    entry = state.state["failed"]["2026_R01_E10"]
    assert entry["stage"] == fr.STAGE_SEARCH_SETUP
    assert entry["actionable"] is True
    assert entry["timestamp"]


# ===========================================================================
# The user-facing route and dialog
# ===========================================================================

@pytest.fixture
def webui(tmp_path, monkeypatch):
    lib_dir = tmp_path / "library"
    lib_dir.mkdir()

    monkeypatch.setattr(motogp_webui, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(motogp_webui, "library_root", lambda *a, **kw: str(lib_dir))
    monkeypatch.setattr(
        motogp_webui, "WEBUI_CONFIG_FILE",
        tmp_path / "webui_settings.motogp.json"
    )
    motogp_webui.app.config.update(TESTING=True)
    with motogp_webui.app.test_client() as client:
        yield client, tmp_path


def test_failed_sessions_reports_the_stage_because_it_changes_what_to_do(webui):
    client, cfg = webui
    (cfg / "grabber_state.motogp.json").write_text(json.dumps({
        "failed": {
            "2026_R19_E40": fr.make(fr.STAGE_SEARCH_SETUP, "no enabled Prowlarr indexers"),
            "2026_R19_E50": {"timestamp": "2026-11-01T10:00:00", "reason": "No results"},
        }
    }))

    payload = client.get("/failed-sessions").get_json()["failed"]

    broken = payload["2026_R19_E40"]
    assert broken["stage"] == fr.STAGE_SEARCH_SETUP
    assert broken["actionable"] is True
    assert "Prowlarr URL" in broken["hint"]

    legacy = payload["2026_R19_E50"]
    assert legacy["stage"] == ""
    assert legacy["reason"] == "No results"
    assert legacy["actionable"] is False


def test_failed_sessions_survives_a_missing_or_corrupt_state_file(webui):
    client, cfg = webui
    assert client.get("/failed-sessions").get_json() == {"failed": {}}

    (cfg / "grabber_state.motogp.json").write_text("{not json")
    assert client.get("/failed-sessions").get_json() == {"failed": {}}
    cfg.joinpath("grabber_state.motogp.json").unlink()


def test_the_dialog_escapes_the_reason_and_shows_the_hint(webui):
    """
    The reason contains text from Prowlarr/SABnzbd errors, so it is data, not
    markup - the mismatch dialog made the same call for release titles.
    """
    client, _ = webui
    html = client.get("/library").get_data(as_text=True)

    assert "esc(f.reason)" in html
    assert "esc(f.hint)" in html
    assert "${f.reason}" not in html, "the reason must not be interpolated raw"
    assert "actionable" in html


def test_the_dialog_tells_the_user_what_to_do(webui):
    client, _ = webui
    html = client.get("/library").get_data(as_text=True)
    assert "which is the difference between" in html


# ===========================================================================
# The watcher's half of the pipeline
# ===========================================================================

def _watcher(tmp_path):
    from motogp_watcher import DownloadWatcher

    downloads = tmp_path / "downloads"
    downloads.mkdir()
    return DownloadWatcher(
        watch_dir=str(downloads),
        processor=lambda folder: True,
        config_dir=str(tmp_path),
        library_path=str(tmp_path / "library"),
        verify_library=False,
    )


def test_processing_failures_record_the_stage_and_a_hint(tmp_path):
    from motogp_watcher import StateManager

    manager = StateManager(tmp_path)
    manager.mark_failed("2026_R19_E40 Moto3 Practice", "the sorter did not confirm the move")

    item = manager.get_failed_item("2026_R19_E40 Moto3 Practice")
    assert item.stage == fr.STAGE_PROCESS
    assert item.actionable is True
    assert item.hint == fr.HINTS[fr.STAGE_PROCESS]
    # The retry bookkeeping that already existed is untouched.
    assert item.attempt_count == 1


def test_a_sorter_exception_is_stored_as_an_actionable_processing_failure(
    tmp_path, monkeypatch
):
    """
    The processor raising used to be stored as a bare string with no activity
    event at all, so a permanently broken sorter looked like a quiet week.
    """
    watcher = _watcher(tmp_path)
    events = []
    monkeypatch.setattr(
        "motogp_watcher.mark_error",
        lambda **kwargs: events.append(kwargs),
    )

    def explode(folder):
        raise RuntimeError("disk full")

    watcher.processor = explode
    # A prefix the watcher can parse, so the activity event path is exercised
    # too; an unparseable folder name would skip it and pass for the wrong reason.
    folder = tmp_path / "downloads" / "26R19E40 Moto3 Practice"
    folder.mkdir()

    assert watcher._process_folder(folder) is False

    item = watcher.state_manager.get_failed_item(folder.name)
    assert item.stage == fr.STAGE_PROCESS
    assert "RuntimeError" in item.last_error
    assert item.actionable is True
    assert events and events[0]["error_message"].startswith("Processing:")


def test_watcher_errors_never_store_the_plex_token(tmp_path, monkeypatch):
    """
    A tagger error can carry a Plex URL, and that URL carries the token. The
    stored text is rendered in the UI and survives restarts, so it is cleaned
    before it is written.
    """
    from motogp_watcher import StateManager

    monkeypatch.setattr(
        "motogp_watcher._configured_secrets", lambda: ["plex-token-value-123"]
    )

    manager = StateManager(tmp_path)
    manager.mark_failed(
        "folder", "tagging failed for http://plex:32400/library?X-Plex-Token=plex-token-value-123"
    )

    stored = manager.get_failed_item("folder").last_error
    assert "plex-token-value-123" not in stored
    assert "http://plex:32400" in stored, "the host is the useful part"


def test_old_watcher_state_entries_still_load(tmp_path):
    """
    Adding fields to FailedItem must not make an existing watcher_state.json
    unreadable - losing retry state silently reprocesses every folder.
    """
    from motogp_watcher import StateManager

    (tmp_path / "watcher_state.motogp.json").write_text(json.dumps({
        "processed": [],
        "failed": {
            "old-folder": {
                "folder_name": "old-folder",
                "first_failed": "2026-11-01T10:00:00",
                "last_attempt": "2026-11-01T10:00:00",
                "attempt_count": 2,
                "last_error": "Processor returned False",
            }
        },
    }))

    item = StateManager(tmp_path).get_failed_item("old-folder")
    assert item.attempt_count == 2
    assert item.stage == ""
    assert item.actionable is False
