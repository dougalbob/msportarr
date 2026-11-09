"""
Regression tests for the WebUI security issues found during the v2.4.1 review.

These started life as *characterisation* tests that asserted the then-current
insecure behaviour, so that fixing it would be a deliberate, visible act rather
than something that happened by accident. The auth work in v2.5.0 fixed them, so
they now assert the safe behaviour and serve as regression tests.

What each one guards:

    finding A  POST /app-config used to rebuild settings.yml from a six-section
               literal, silently deleting uiadminpassword, grabber.* and
               anything added later
    finding B  Access-Control-Allow-Origin: * on every response, including
               /app-config, so any page in any browser on the LAN could read the
               Plex token and API keys cross-origin
    issue 1    no authentication on any route at all

The route-enumeration test that used to live here has moved to
tests/test_webui_auth.py, where it asserts the guard exists for every route
rather than the absence of one.

References:
    AGENTS.md §6 findings A-D, docs/ARCHITECTURE.md §5-6
"""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

# motogp_webui computes module-level paths from CONFIG_DIR at import time, and
# importing it emits an activity event into CONFIG_DIR. Point that at a throwaway
# directory *before* the import so the test never touches a real /config and
# leaves nothing behind in the working tree.
_CONFIG_DIR = tempfile.mkdtemp(prefix="motogp-webui-test-")
os.environ["CONFIG_DIR"] = _CONFIG_DIR
# These tests are about the config/HTTP surface, not identity, so run them with
# the guard off; the guard itself is covered in tests/test_webui_auth.py.
os.environ["AUTH_MODE"] = "none"

import motogp_webui  # noqa: E402


SETTINGS = {
    "prowlarr": {"url": "http://prowlarr:9696", "api_key": "prowlarr-key"},
    "sabnzbd": {"url": "http://sab:8080", "api_key": "sab-key"},
    "plex": {"url": "http://plex:32400", "token": "plex-token", "library_id": "16"},
    "library": {"motogp_path": "/media/MotoGP", "downloads_path": "/downloads"},
    "grabber": {"max_grabs_per_run": 22, "allowed_groups": ["motogp", "MWR"]},
    "uiadminpassword": "hunter2",
    "timezone": "Europe/London",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client whose settings.yml lives in tmp_path."""
    import utils.config_loader as config_loader

    settings_file = tmp_path / "settings.yml"
    settings_file.write_text(yaml.safe_dump(SETTINGS), encoding="utf-8")

    monkeypatch.setattr(motogp_webui, "CONFIG_DIR", tmp_path)
    # /settings also writes webui_settings.json next to CONFIG_DIR; keep that in
    # tmp_path too, or a test run litters the checkout's config/ directory.
    monkeypatch.setattr(
        motogp_webui, "WEBUI_CONFIG_FILE",
        tmp_path / "webui_settings.motogp.json"
    )
    monkeypatch.setattr(config_loader, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_loader, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_loader, "_settings_cache", None)

    motogp_webui.app.config.update(TESTING=True)
    with motogp_webui.app.test_client() as test_client:
        yield test_client, settings_file

    # The loader caches settings globally; leaving tmp_path in the cache would
    # leak a deleted directory into the next test.
    config_loader._settings_cache = None


# ---------------------------------------------------------------------------
# Finding A: saving App Config must not destroy keys it was not told about
# ---------------------------------------------------------------------------

def test_app_config_post_preserves_unknown_keys(client):
    """
    The regression: saving the six sections the form knows about used to delete
    everything else. Nothing failed, so it went unnoticed - the settings simply
    reverted, and the admin password vanished.
    """
    test_client, settings_file = client

    posted = {
        "prowlarr": SETTINGS["prowlarr"],
        "sabnzbd": SETTINGS["sabnzbd"],
        "plex": SETTINGS["plex"],
        "library": SETTINGS["library"],
        "discord": {},
        "timezone": "Europe/London",
    }
    assert test_client.post("/app-config", json=posted).status_code == 200

    saved = yaml.safe_load(settings_file.read_text(encoding="utf-8"))

    assert saved["uiadminpassword"] == "hunter2", "saving config wiped a key it did not send"
    assert saved["grabber"]["allowed_groups"] == ["motogp", "MWR"]
    assert saved["grabber"]["max_grabs_per_run"] == 22


def test_app_config_post_updates_only_what_it_is_given(client):
    test_client, settings_file = client

    response = test_client.post(
        "/app-config", json={"plex": {"url": "http://new-plex:32400"}}
    )
    assert response.status_code == 200

    saved = yaml.safe_load(settings_file.read_text(encoding="utf-8"))

    assert saved["plex"]["url"] == "http://new-plex:32400"
    # A partial plex payload must not drop its siblings.
    assert saved["plex"]["token"] == "plex-token"
    assert saved["plex"]["library_id"] == "16"


def test_app_config_post_cannot_write_arbitrary_keys(client):
    """
    The allowlist matters in both directions: a browser form is not a trusted
    editor, so a payload key that is not a known setting is ignored rather than
    written into settings.yml.
    """
    test_client, settings_file = client

    test_client.post("/app-config", json={
        "prowlarr": {"url": "http://x", "evil_key": "value"},
        "not_a_section": {"nonsense": True},
    })

    saved = yaml.safe_load(settings_file.read_text(encoding="utf-8"))
    assert "evil_key" not in saved["prowlarr"]
    assert "not_a_section" not in saved


def test_app_config_post_rejects_a_non_object_body(client):
    test_client, _ = client
    response = test_client.post("/app-config", json=["not", "an", "object"])
    assert response.status_code == 400


def test_app_config_get_still_returns_the_settings_the_ui_edits(client):
    """The secret exposure is a transport problem, not a 'stop showing it' one:
    the UI is a configuration editor, so it must receive what it edits."""
    test_client, _ = client
    payload = test_client.get("/app-config").get_json()

    assert payload["plex"]["token"] == "plex-token"
    assert payload["prowlarr"]["api_key"] == "prowlarr-key"


def test_app_config_get_never_echoes_the_legacy_admin_password(client):
    """uiadminpassword was compared in plaintext on a public route and is gone.
    It must not be shipped to the browser either."""
    test_client, _ = client
    payload = test_client.get("/app-config").get_json()
    assert "uiadminpassword" not in payload


def test_settings_post_preserves_the_rest_of_settings_yml(client):
    """
    POST /settings writes one grabber key. It used to load the whole file, mutate
    it and write it all back - and when that write raised, the exception was
    caught and the route still answered "saved". Both halves are pinned here.
    """
    test_client, settings_file = client

    response = test_client.post("/settings", json={
        "max_grabs_per_run": 30,
        "sessions": {"MOTOGP_RACE": True},
    })
    assert response.status_code == 200
    assert response.get_json()["status"] == "saved"

    saved = yaml.safe_load(settings_file.read_text(encoding="utf-8"))
    assert saved["grabber"]["max_grabs_per_run"] == 30
    # Everything else survives, including the sibling grabber keys.
    assert saved["grabber"]["allowed_groups"] == ["motogp", "MWR"]
    assert saved["plex"]["token"] == "plex-token"
    assert saved["uiadminpassword"] == "hunter2"


def test_settings_post_reports_failure_instead_of_claiming_success(client, monkeypatch):
    """
    The app's recurring failure mode is claiming success while doing nothing.
    A read-only settings.yml must produce an error, not "saved".
    """
    import utils.config_loader as config_loader

    test_client, _ = client

    def explode(_updates):
        return False

    monkeypatch.setattr(motogp_webui, "update_settings", explode)
    response = test_client.post("/settings", json={"max_grabs_per_run": 5})

    assert response.status_code == 500
    assert response.get_json()["status"] == "error"


# ---------------------------------------------------------------------------
# Finding B: no CORS wildcard
# ---------------------------------------------------------------------------

def test_no_wildcard_cors_header(client):
    """
    The UI is same-origin, so the wildcard bought nothing and meant any page in
    any browser on the LAN could read /app-config cross-origin.
    """
    test_client, _ = client

    for path in ("/app-config", "/manifest.json", "/flags/gb.svg"):
        response = test_client.get(path)
        assert response.headers.get("Access-Control-Allow-Origin") is None, (
            f"{path} still advertises a cross-origin wildcard"
        )


def test_security_headers_are_present(client):
    test_client, _ = client
    headers = test_client.get("/manifest.json").headers

    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "SAMEORIGIN"


# ---------------------------------------------------------------------------
# The legacy admin password route is gone
# ---------------------------------------------------------------------------

def test_verify_admin_route_no_longer_exists(client):
    """
    It compared a plaintext password with ==, was unknown to config_loader, and
    gated only the client while /app-config stayed open. Identity is established
    by Cloudflare Access now; leaving a second, weaker door would be worse than
    having none.
    """
    test_client, _ = client
    assert test_client.post("/verify-admin", json={"password": "hunter2"}).status_code == 404


# ---------------------------------------------------------------------------
# Diagnostics: the configuration problems that are otherwise invisible
# ---------------------------------------------------------------------------

def test_diagnostics_route_reports_schema_credentials_and_plex(client):
    test_client, _ = client
    response = test_client.get("/diagnostics")

    assert response.status_code == 200
    payload = response.get_json()
    assert {"validation", "credentials", "plex_library"} <= set(payload)
    assert "schema" in payload["validation"]


def test_diagnostics_never_returns_credential_values(client):
    """
    It reports *where* each credential came from, because an env var in the
    container template silently beats a key typed into this page. It must never
    return the value itself.
    """
    test_client, _ = client
    body = test_client.get("/diagnostics").get_data(as_text=True)

    assert "plex-token" not in body
    assert "prowlarr-key" not in body
    assert "sab-key" not in body
    assert body.count("settings.yml") >= 1 or '"default"' in body
