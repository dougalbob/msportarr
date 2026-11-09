#!/usr/bin/env python3
"""
Browser-write origin guard (Slice 5, light version)
====================================================

Cloudflare Access is the outer gate and ``utils.web_auth`` is the inner one,
but neither answers this question: *the request carries a valid identity -
did it actually come from a page this app served?* A malicious website open
in the same browser can make that browser send requests to this app with the
user's Cloudflare session attached. The app cannot distinguish those from
real UI actions by identity alone.

Browsers answer the question for free: every cross-origin request, and every
form/fetch POST, carries an ``Origin`` header that the sending page cannot
spoof. So for write methods this guard requires:

* ``Origin`` (or, as a fallback, ``Referer``) whose host matches the host
  the request was sent to, or
* neither header at all - which is what curl, scripts and other non-browser
  clients send, and those are already governed by auth, not by this guard.

Only the host is compared, never the scheme: Cloudflare terminates TLS and
forwards plain HTTP inside, so the app's own view of the scheme legitimately
differs from the browser's. The check is a ``before_request`` hook like the
auth guard, so a future route is covered the day it is added; safe methods
and the public endpoints (health probe, static assets) are exempt because
they change nothing.

This is defence-in-depth, not a replacement for auth: with
``AUTH_MODE=none`` on an untrusted network it stops cross-site triggering
but anyone who can reach the port can still just send the requests directly.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from flask import jsonify, request

from utils.web_auth import PUBLIC_ENDPOINTS

logger = logging.getLogger("msportarr.origin")

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _header_host(value: str) -> str:
    """Host[:port] of an Origin/Referer value, or '' if unparsable."""
    try:
        return urlparse(value).netloc.lower()
    except (ValueError, AttributeError):
        return ""


def origin_allowed() -> bool:
    """
    True when this request may perform a write.

    Decision order: Origin wins when present; Referer is the fallback for
    the rare client that sends only that; no header at all means a
    non-browser client, which auth - not this guard - is responsible for.
    """
    origin = request.headers.get("Origin")
    if origin:
        return _header_host(origin) == request.host.lower()

    referer = request.headers.get("Referer")
    if referer:
        return _header_host(referer) == request.host.lower()

    return True


def install_origin_guard(app):
    """Attach the write-origin check. Registered after auth, so 401 wins over 403."""

    @app.before_request
    def _check_write_origin():
        if request.method not in WRITE_METHODS:
            return None
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if origin_allowed():
            return None
        logger.warning(
            "Refused cross-origin write: %s %s (Origin: %s, Referer: %s)",
            request.method, request.path,
            request.headers.get("Origin", "-"),
            request.headers.get("Referer", "-"),
        )
        return jsonify({
            "error": "cross_origin_write_rejected",
            "detail": (
                "Write requests must come from this app's own pages. "
                "If you are using the UI and see this, the address in the "
                "browser does not match the address the app was reached at."
            ),
        }), 403

    return _check_write_origin
