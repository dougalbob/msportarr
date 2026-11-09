"""
The UI and the grabber must agree on max_grabs_per_run.

settings.yml owns grabber.* (that is what config_loader.get_max_grabs_per_run()
reads, and the grabber calls it). webui_settings.json owns only the desired
sessions. The UI reads max_grabs_per_run from config_loader at presentation
time, so there is exactly one source of truth and no stale JSON shadowing it.

These tests exercise the real /settings HTTP endpoints through the Flask test
client, with CONFIG_DIR pointed at a throwaway directory.
"""

import json
import logging
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fresh config dir bound to every store module."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))

    import motogp_common
    import motogp_webui
    import utils.config_loader as config_loader

    # Point every store at the throwaway directory. monkeypatch undoes this
    # after the test, so later test files (which import the same modules)
    # see the real defaults again.
    monkeypatch.setattr(motogp_common, "CONFIG_DIR", Path(tmp_path))
    monkeypatch.setattr(config_loader, "CONFIG_DIR", Path(tmp_path))
    monkeypatch.setattr(
        config_loader, "SETTINGS_FILE", Path(tmp_path) / "settings.yml"
    )
    monkeypatch.setattr(
        motogp_webui, "WEBUI_CONFIG_FILE",
        Path(tmp_path) / "webui_settings.motogp.json"
    )
    config_loader.reload_settings()

    yield tmp_path, config_loader, motogp_webui


# ---------------------------------------------------------------------------
# Round trip: both HTTP endpoints, one source of truth
# ---------------------------------------------------------------------------

def test_settings_round_trip_leaves_one_source_of_truth(env):
    tmp_path, config_loader, motogp_webui = env
    client = motogp_webui.app.test_client()

    sessions = {"MOTOGP_RACE": True, "MOTOGP_FP1": False}

    response = client.post(
        "/settings",
        json={"max_grabs_per_run": 25, "sessions": sessions},
    )
    assert response.get_json() == {"status": "saved"}

    # Reading back agrees with the getter the grabber uses, same process,
    # no restart.
    body = client.get("/settings").get_json()
    assert body["max_grabs_per_run"] == 25
    assert body["sessions"] == sessions
    assert config_loader.get_max_grabs_per_run() == 25

    # Exactly one source of truth on disk: sessions only in the JSON store,
    # max_grabs_per_run only in settings.yml.
    webui = json.loads((config_loader.CONFIG_DIR / "webui_settings.motogp.json").read_text())
    assert webui == {"sessions": sessions}, webui
    assert "max_grabs_per_run" not in webui

    settings_yml = config_loader._load_yaml()
    assert settings_yml["grabber"]["max_grabs_per_run"] == 25


# ---------------------------------------------------------------------------
# Disagreement: settings.yml wins
# ---------------------------------------------------------------------------

def test_disagreement_settings_yml_wins(env, caplog):
    tmp_path, config_loader, motogp_webui = env
    client = motogp_webui.app.test_client()
    caplog.set_level(logging.WARNING)

    # Stale JSON copy from before the migration.
    (config_loader.CONFIG_DIR / "webui_settings.motogp.json").write_text(
        json.dumps({"max_grabs_per_run": 30})
    )
    Path(config_loader.SETTINGS_FILE).write_text(
        "grabber:\n  max_grabs_per_run: 12\n"
    )
    config_loader.reload_settings()

    body = client.get("/settings").get_json()
    assert body["max_grabs_per_run"] == 12
    assert config_loader.get_max_grabs_per_run() == 12

    # Migration drops the stale JSON key without overwriting settings.yml.
    assert "max_grabs_per_run" not in json.loads(
        (config_loader.CONFIG_DIR / "webui_settings.motogp.json").read_text()
    )
    assert config_loader._load_yaml()["grabber"]["max_grabs_per_run"] == 12


# ---------------------------------------------------------------------------
# Migration: legacy JSON with max_grabs only
# ---------------------------------------------------------------------------

def test_migration_writes_value_once_then_noops(env):
    tmp_path, config_loader, motogp_webui = env
    client = motogp_webui.app.test_client()

    (config_loader.CONFIG_DIR / "webui_settings.motogp.json").write_text(
        json.dumps({"max_grabs_per_run": 30})
    )

    # First load: value lands in settings.yml, key dropped from the JSON.
    body = client.get("/settings").get_json()
    assert body["max_grabs_per_run"] == 30
    assert config_loader.get_max_grabs_per_run() == 30
    assert config_loader._load_yaml()["grabber"]["max_grabs_per_run"] == 30
    assert "max_grabs_per_run" not in json.loads(
        (config_loader.CONFIG_DIR / "webui_settings.motogp.json").read_text()
    )

    settings_yml = config_loader.SETTINGS_FILE
    first_mtime = settings_yml.stat().st_mtime_ns
    first_bytes = settings_yml.read_bytes()

    # Second load: idempotent no-op, nothing is rewritten, nothing lost.
    client.get("/settings")
    assert settings_yml.stat().st_mtime_ns == first_mtime
    assert settings_yml.read_bytes() == first_bytes
    assert config_loader.get_max_grabs_per_run() == 30


# ---------------------------------------------------------------------------
# Corrupt / missing JSON: loud warning, deterministic fallback
# ---------------------------------------------------------------------------

def test_corrupt_json_logs_warning_and_falls_back(env, caplog):
    tmp_path, config_loader, motogp_webui = env
    client = motogp_webui.app.test_client()
    caplog.set_level(logging.WARNING, logger="motogp.webui")

    (config_loader.CONFIG_DIR / "webui_settings.motogp.json").write_text("{not valid json")

    body = client.get("/settings").get_json()
    defaults = {
        k: v["default"] for k, v in motogp_webui.SESSION_TYPES.items()
    }
    assert body["sessions"] == defaults
    assert body["max_grabs_per_run"] == config_loader.get_max_grabs_per_run()

    assert any(
        "webui_settings.motogp.json" in record.getMessage() for record in caplog.records
    ), caplog.text

    # Sessions still work: a save/load round trip after the corrupt file.
    client.post("/settings", json={"sessions": {"MOTOGP_RACE": True}})
    assert client.get("/settings").get_json()["sessions"] == {
        "MOTOGP_RACE": True,
    }


def test_missing_json_logs_warning_and_defaults(env, caplog):
    tmp_path, config_loader, motogp_webui = env
    client = motogp_webui.app.test_client()
    caplog.set_level(logging.WARNING, logger="motogp.webui")

    assert not (config_loader.CONFIG_DIR / "webui_settings.motogp.json").exists()

    body = client.get("/settings").get_json()
    assert body["sessions"] == {
        k: v["default"] for k, v in motogp_webui.SESSION_TYPES.items()
    }
    assert body["max_grabs_per_run"] == config_loader.get_max_grabs_per_run()
    assert any(
        "webui_settings.motogp.json" in record.getMessage() for record in caplog.records
    ), caplog.text


# ---------------------------------------------------------------------------
# Guard: the default of 22 is declared in exactly one module (config_loader)
# ---------------------------------------------------------------------------

def test_no_duplicated_max_grabs_default_in_webui():
    source = (REPO_ROOT / "motogp_webui.py").read_text(encoding="utf-8")
    assert source.count('"max_grabs_per_run": 22') == 0, (
        "max_grabs_per_run's default must live in config_loader only"
    )
