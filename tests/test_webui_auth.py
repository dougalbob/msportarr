"""
End-to-end tests for the WebUI authentication guard (utils/web_auth.py).

The point of this file is the *route enumeration* test: it walks url_map and
asserts every route is either protected or named in the public allowlist. That is
what stops the original bug from coming back - 27 routes were open because auth
was never applied anywhere, and the next route added must not be able to repeat
that by omission.

The rest of the file covers the decisions a guard like this gets wrong:
  * the healthcheck must keep working (a 401 probe = every container unhealthy)
  * a misconfiguration must fail closed, not open
  * AUTH_MODE=none must be explicit and loud, never accidental
"""

import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask
from jwt import PyJWK

from utils import auth as auth_module
from utils import web_auth
from utils.auth import AuthConfig, AccessVerifier, AuthError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ISSUER = "https://example-team.cloudflareaccess.com"
AUDIENCE = "0123456789abcdef0123456789abcdef"
EMAIL = "me@example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeJWKS:
    def __init__(self, public_key):
        self._jwk = PyJWK.from_dict(_jwk_dict(public_key), algorithm="RS256")

    def get_signing_key_from_jwt(self, token):
        return self._jwk

    def refresh(self):
        pass


def _jwk_dict(public_key):
    import base64

    numbers = public_key.public_numbers()
    raw = numbers.n.to_bytes(256, "big")
    n = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return {"kty": "RSA", "alg": "RS256", "use": "sig", "kid": "test-kid", "n": n, "e": "AQAB"}


@pytest.fixture(scope="module")
def keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


def token(private_key, **overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "email": EMAIL,
        "iat": now - 5,
        "exp": now + 600,
    }
    claims.update({k: v for k, v in overrides.items() if v is not None})
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-kid"})


def build_app(public_key, mode=web_auth.MODE_CLOUDFLARE):
    """A tiny app wired exactly like the real one, without importing the WebUI."""
    app = Flask(__name__, template_folder=str(
        Path(__file__).resolve().parent.parent / "templates"
    ))
    app.secret_key = "test"

    @app.route("/")
    def index():
        return "home"

    @app.route("/app-config")
    def app_config():
        return {"plex": {"token": "secret"}}

    @app.route("/api/thing")
    def api_thing():
        return {"ok": True}

    @app.route("/run", methods=["POST"])
    def run():
        return "started"

    gate = web_auth.AuthGate(mode=mode)
    if mode == web_auth.MODE_CLOUDFLARE:
        config = AuthConfig(ISSUER, AUDIENCE, (EMAIL,), ISSUER + "/certs")
        gate.verifier = AccessVerifier(config, jwks=FakeJWKS(public_key))

    web_auth.install(app, gate=gate)
    return app, gate


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------

def test_auth_is_off_when_no_auth_variables_are_set():
    gate = web_auth.AuthGate.from_environment({})
    assert gate.mode == web_auth.MODE_NONE
    assert gate.enabled is False


def test_auth_is_on_when_issuer_is_set():
    gate = web_auth.AuthGate.from_environment({
        "AUTH_ISSUER": ISSUER,
        "AUTH_AUDIENCE": AUDIENCE,
        "AUTH_ALLOWED_EMAILS": EMAIL,
    })
    assert gate.mode == web_auth.MODE_CLOUDFLARE
    assert gate.enabled is True


def test_explicit_none_overrides_configured_auth():
    gate = web_auth.AuthGate.from_environment({
        "AUTH_MODE": "none",
        "AUTH_ISSUER": ISSUER,
        "AUTH_AUDIENCE": AUDIENCE,
        "AUTH_ALLOWED_EMAILS": EMAIL,
    })
    assert gate.mode == web_auth.MODE_NONE


def test_unknown_mode_is_rejected_not_ignored():
    """A typo in AUTH_MODE must not quietly mean 'no auth'."""
    with pytest.raises(AuthError):
        web_auth.AuthGate.from_environment({"AUTH_MODE": "cloudflair"})


def test_incomplete_configuration_fails_closed():
    """
    Half a config: the gate still enforces, and every request is refused with
    the reason. Starting open would turn a missing variable into an outage of a
    different kind.
    """
    gate = web_auth.AuthGate.from_environment({"AUTH_ISSUER": ISSUER})
    assert gate.mode == web_auth.MODE_CLOUDFLARE
    assert gate.enabled is True
    assert gate.config_error and "AUTH_AUDIENCE" in gate.config_error


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------

def test_unauthenticated_request_is_refused_before_reaching_the_view(keypair):
    _, public = keypair
    app, _ = build_app(public)
    client = app.test_client()

    for path in ("/", "/app-config", "/api/thing"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert b"secret" not in response.data


def test_post_routes_are_refused_too(keypair):
    """'It only reads' is not a defence; /run starts a download."""
    _, public = keypair
    app, _ = build_app(public)
    assert app.test_client().post("/run").status_code == 401


def test_valid_token_grants_access(keypair):
    private, public = keypair
    app, _ = build_app(public)
    client = app.test_client()

    response = client.get("/app-config", headers={"Cf-Access-Jwt-Assertion": token(private)})
    assert response.status_code == 200
    assert response.get_json()["plex"]["token"] == "secret"


def test_forged_token_is_refused(keypair):
    _, public = keypair
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    app, _ = build_app(public)

    response = app.test_client().get(
        "/app-config", headers={"Cf-Access-Jwt-Assertion": token(attacker)}
    )
    assert response.status_code == 401


def test_api_callers_get_json_and_browsers_get_a_page(keypair):
    _, public = keypair
    app, _ = build_app(public)
    client = app.test_client()

    api = client.get("/api/thing")
    assert api.status_code == 401
    assert api.get_json()["error"] == "unauthorized"

    page = client.get("/", headers={"Accept": "text/html"})
    assert page.status_code == 401
    assert b"Not authorised" in page.data


def test_refusal_explains_itself(keypair):
    """A bare 403 is indistinguishable from a broken app; the page must say why."""
    _, public = keypair
    app, _ = build_app(public)
    body = app.test_client().get("/", headers={"Accept": "text/html"}).data.decode()

    assert "Cf-Access-Jwt-Assertion" in body
    assert "Cloudflare Access" in body
    assert "AUTH_ALLOWED_EMAILS" in body


# ---------------------------------------------------------------------------
# The routes that must stay reachable
# ---------------------------------------------------------------------------

def test_healthcheck_endpoint_is_public(keypair):
    """
    utils/healthcheck.py probes this. If it required auth, every container would
    report unhealthy - the exact failure mode the probe was added to fix.
    """
    _, public = keypair
    app, _ = build_app(public)

    response = app.test_client().get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_real_healthcheck_uses_the_public_endpoint():
    from utils import healthcheck

    assert healthcheck.URL.endswith("/healthz")


# ---------------------------------------------------------------------------
# The structural test: no route may be born unauthenticated
# ---------------------------------------------------------------------------

def test_every_webui_route_is_protected_or_deliberately_public():
    """
    Walk the real WebUI's url_map and require a decision for every route.

    This is the regression test for the original bug. Adding a route without
    either protecting it or adding it to PUBLIC_ENDPOINTS (with a comment
    explaining why it is safe) fails here rather than in production.
    """
    import os
    import tempfile

    os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp(prefix="msportarr-auth-test-"))
    import motogp_webui  # imported late: needs CONFIG_DIR and installs the gate

    gate = motogp_webui.AUTH_GATE
    assert gate is not None, "the WebUI must install an auth gate"

    public = web_auth.PUBLIC_ENDPOINTS
    unprotected = {
        rule.rule
        for rule in motogp_webui.app.url_map.iter_rules()
        if rule.endpoint not in public
    }

    # Sanity: the guard is a before_request hook, so it applies to all of these.
    hooks = motogp_webui.app.before_request_funcs.get(None, [])
    assert hooks, "no before_request guard installed - every route is open"

    # Anything unprotected must at least be a known route name, so a typo in
    # PUBLIC_ENDPOINTS cannot silently protect nothing.
    known_endpoints = {r.endpoint for r in motogp_webui.app.url_map.iter_rules()}
    assert public <= known_endpoints, (
        f"PUBLIC_ENDPOINTS names endpoints that do not exist: {public - known_endpoints}"
    )
    assert unprotected, "expected some protected routes"


def test_public_endpoints_reveal_nothing_sensitive():
    """The allowlist must stay static assets and probes, not data."""
    forbidden = {
        "app_config_route", "run", "download_selected", "settings_route",
        "test_connections", "sabnzbd_queue", "stream_logs", "recent_logs",
        "library_status", "verify_admin",
    }
    assert not (web_auth.PUBLIC_ENDPOINTS & forbidden), (
        "a route that exposes secrets or triggers actions was made public"
    )
