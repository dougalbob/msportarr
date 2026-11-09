#!/usr/bin/env python3
"""
WebUI authentication guard
==========================

Enforces :mod:`utils.auth` on every request, and owns the one place in the app
that decides whether a request is allowed in.

Two things here are deliberate and worth not "simplifying":

1. **It is a ``before_request`` hook, not a decorator per route.** A route added
   later cannot be born unauthenticated by accident, which is how this app
   shipped 27 open routes in the first place. The cost is that anything which
   must stay public has to be named explicitly in :data:`PUBLIC_ENDPOINTS`, and
   there is a test that checks nothing secret sneaks into that set.

2. **Denial is 401 with an HTML page for browsers and JSON for API calls.** A
   bare 403 is indistinguishable from a broken app when you are standing in front
   of it; the page says which of the three things went wrong and what to do.

Modes, in order of precedence:

    AUTH_MODE=none        explicit opt-out, logs a loud warning
    AUTH_MODE=cloudflare  require a verified assertion (AUTH_* must be complete)
    (unset)               cloudflare if AUTH_ISSUER is set, otherwise none

``none`` is for local development and for a container that is genuinely only
reachable from the LAN. It is never silent: the warning is printed at the top of
the log and repeated in the UI banner.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from flask import jsonify, render_template, request, session

from utils import auth as auth_module
from utils.auth import AuthError, AuthConfig, Identity

logger = logging.getLogger("msportarr.auth")

MODE_NONE = "none"
MODE_CLOUDFLARE = "cloudflare"

VALID_MODES = (MODE_CLOUDFLARE, MODE_NONE)

#: Endpoints that must answer without an identity.
#:
#: Restricted to static assets and PWA plumbing - things that reveal nothing and
#: that a browser fetches before it has a page to show an error on. ``health`` is
#: here so the Docker HEALTHCHECK keeps working: a probe that needs credentials
#: makes every container report unhealthy, which is the failure mode the probe
#: was added to fix.
PUBLIC_ENDPOINTS = frozenset({
    "static",        # /static/<path>
    "static_files",  # /assets/<path> - icons and artwork
    "flag_files",    # /flags/<path>
    "manifest",      # /manifest.json - PWA
    "service_worker",  # /sw.js
    "health",        # /healthz - container probe
})

#: Session key holding the verified email, so the UI can show who is signed in.
SESSION_EMAIL_KEY = "identity_email"


class AuthGate:
    """Holds the resolved mode and verifier for the lifetime of the process."""

    def __init__(
        self,
        mode: str,
        verifier: Optional[auth_module.AccessVerifier] = None,
        config_error: Optional[str] = None,
    ) -> None:
        self.mode = mode
        self.verifier = verifier
        self.config_error = config_error

    @property
    def enabled(self) -> bool:
        return self.mode == MODE_CLOUDFLARE

    @classmethod
    def from_environment(cls, environ: Optional[dict] = None) -> "AuthGate":
        env = os.environ if environ is None else environ
        mode = (env.get("AUTH_MODE") or "").strip().lower()

        if mode and mode not in VALID_MODES:
            # Refuse to guess: a typo in AUTH_MODE must not silently mean "none".
            raise AuthError(
                f"AUTH_MODE={mode!r} is not valid. Use one of: "
                + ", ".join(VALID_MODES)
            )

        try:
            config = auth_module.load_config(env)
        except AuthError as exc:
            # A half-configured deployment fails closed: the container starts,
            # every request is refused, and the reason is in the log and on the
            # error page. Starting unauthenticated instead would be the worst
            # possible interpretation of an incomplete config.
            logger.error("Authentication misconfigured: %s", exc)
            return cls(mode=MODE_CLOUDFLARE, config_error=str(exc))

        if mode == MODE_NONE:
            logger.warning("=" * 66)
            logger.warning("  AUTH_MODE=none - the WebUI has NO authentication.")
            logger.warning("  Anyone who can reach this port can read your Plex")
            logger.warning("  token and API keys and trigger downloads.")
            logger.warning("  Only do this on a trusted network, or with AUTH_* set.")
            logger.warning("=" * 66)
            return cls(mode=MODE_NONE)

        if config is None:
            return cls(mode=MODE_NONE)

        return cls(mode=MODE_CLOUDFLARE, verifier=auth_module.AccessVerifier(config))

    # -- request-time decision ---------------------------------------------

    def _is_public(self) -> bool:
        return request.endpoint in PUBLIC_ENDPOINTS

    def check(self) -> Optional[Identity]:
        """Return the identity for this request, or raise AuthError."""
        if not self.enabled:
            return None

        if self.config_error:
            raise AuthError(self.config_error)

        token = auth_module.assertion_from_headers(request.headers.items())
        assert self.verifier is not None
        return self.verifier.verify(token)

    def deny_response(self, error: AuthError):
        """Explain the refusal. Browsers get a page, the API gets JSON."""
        logger.warning(
            "Refused %s %s: %s", request.method, request.path, error
        )

        wants_json = (
            request.path.startswith("/api/")
            or request.accept_mimetypes.best == "application/json"
            or request.is_json
        )
        if wants_json:
            return jsonify({"error": "unauthorized", "detail": str(error)}), 401

        return render_template(
            "auth_error.html",
            detail=str(error),
            login_url=self.verifier.config.error_page_url if self.verifier else None,
        ), 401

def install(app, gate: Optional[AuthGate] = None) -> AuthGate:
    """
    Attach the guard to a Flask app. Returns the gate so callers can introspect it.
    """
    gate = gate or AuthGate.from_environment()

    @app.before_request
    def _require_identity():  # pragma: no cover - exercised through the client
        if gate._is_public():
            return None

        try:
            identity = gate.check()
        except AuthError as exc:
            session.clear()
            return gate.deny_response(exc)

        if identity is not None:
            session[SESSION_EMAIL_KEY] = identity.email
        return None

    @app.route("/healthz")
    def health():
        """Unauthenticated liveness probe used by the Docker HEALTHCHECK."""
        return jsonify({"status": "ok"}), 200

    app.extensions["msportarr_auth"] = gate
    return gate
