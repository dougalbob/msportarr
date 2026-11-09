#!/usr/bin/env python3
"""
Cloudflare Access identity verification
========================================

External access to Motorsportarr is through Cloudflare Zero Trust. Cloudflare's email
policy is the outer gate; this module is the *inner* gate. It exists because the
outer gate is only as good as the assumption that every request actually passes
through it, and that assumption is false in two ordinary situations:

  * another container on the same Docker network can reach 0.0.0.0:5679
    directly, with no Cloudflare in the path at all, and
  * anything on the LAN can do the same.

An app that trusts ``Cf-Access-Jwt-Assertion`` without verifying it is worse than
one with no auth at all, because the header is trivially forgeable by anyone who
can reach the port. So every assertion is checked properly: signature against
Cloudflare's published keys, issuer, audience and expiry.

There is no user database and no sign-up. One house, one user: a verified email
must also be listed in AUTH_ALLOWED_EMAILS, which mirrors the Cloudflare policy
in-app. An email Cloudflare is happy with but that is not on this list is denied.

Configuration (all via environment; see ENV_SPEC in utils/config_loader.py):

    AUTH_ISSUER          https://<team>.cloudflareaccess.com
    AUTH_AUDIENCE        the Access application's AUD tag
    AUTH_ALLOWED_EMAILS  comma-separated, e.g. "me@example.com"
    AUTH_CERTS_URL       optional, defaults to $AUTH_ISSUER/cdn-cgi/access/certs

Design notes:

* ``verify()`` returns an :class:`Identity` or raises :class:`AuthError`. It never
  logs the email at INFO by default (it is personal data) but does return it so
  the caller can attribute actions.
* Keys are cached and refetched when an unknown ``kid`` arrives, which is how
  Cloudflare key rotation is handled without a restart.
* Nothing here imports Flask. That keeps it testable without an app and keeps
  the dependency direction one way (WebUI -> auth, never auth -> WebUI).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import jwt  # PyJWT
from jwt import PyJWKClient

logger = logging.getLogger("msportarr.auth")


# How long to trust a fetched key set before refetching. Cloudflare rotates
# signing keys rarely; an hour keeps rotation recovery prompt without hammering
# the certs endpoint on every request.
JWKS_CACHE_SECONDS = 3600

DEFAULT_LEEWAY_SECONDS = 60


class AuthError(Exception):
    """Verification failed. The message is safe to log; it is never shown raw."""


@dataclass(frozen=True)
class AuthConfig:
    """Resolved authentication settings."""

    issuer: str
    audience: str
    allowed_emails: Tuple[str, ...]
    certs_url: str

    @property
    def error_page_url(self) -> str:
        """Where a browser should go to (re)authenticate via Cloudflare."""
        return self.issuer.rstrip("/")

    def is_allowed(self, email: str) -> bool:
        """Exact, case-insensitive match against the allowlist."""
        if not email:
            return False
        return email.strip().lower() in {e.lower() for e in self.allowed_emails}


def _split_emails(raw: Optional[str]) -> Tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def load_config(environ: Optional[Dict[str, str]] = None) -> Optional[AuthConfig]:
    """
    Build an AuthConfig from the environment, or None when auth is not configured.

    Reading AUTH_ISSUER and AUTH_AUDIENCE is not enough to enable auth: without
    an allowlist the check would either admit everyone or deny everyone. A
    half-filled configuration is therefore reported as an error at startup rather
    than silently doing the wrong thing.
    """
    env = os.environ if environ is None else environ

    issuer = (env.get("AUTH_ISSUER") or "").strip()
    audience = (env.get("AUTH_AUDIENCE") or "").strip()
    emails = _split_emails(env.get("AUTH_ALLOWED_EMAILS"))

    if not issuer and not audience and not emails:
        return None

    missing = [
        name for name, value in (
            ("AUTH_ISSUER", issuer),
            ("AUTH_AUDIENCE", audience),
            ("AUTH_ALLOWED_EMAILS", emails),
        ) if not value
    ]
    if missing:
        raise AuthError(
            "Incomplete authentication configuration; missing "
            + ", ".join(missing)
            + ". Set all of AUTH_ISSUER, AUTH_AUDIENCE and AUTH_ALLOWED_EMAILS, "
              "or none of them (which leaves the WebUI unauthenticated)."
        )

    if not issuer.startswith("https://"):
        raise AuthError(
            f"AUTH_ISSUER must be an https:// URL, got {issuer!r}. The signing "
            "keys are fetched from it, and over http they could be replaced in "
            "transit."
        )

    certs_url = (env.get("AUTH_CERTS_URL") or "").strip() or (
        issuer.rstrip("/") + "/cdn-cgi/access/certs"
    )

    return AuthConfig(
        issuer=issuer.rstrip("/"),
        audience=audience,
        allowed_emails=emails,
        certs_url=certs_url,
    )


class AccessVerifier:
    """
    Verifies Cloudflare Access JWTs.

    The key client is injectable so tests can supply their own JWKS instead of
    reaching the network - which matters because the sandbox (and CI in some
    cases) cannot resolve <team>.cloudflareaccess.com.
    """

    def __init__(
        self,
        config: AuthConfig,
        jwks: Optional[Any] = None,
        leeway: int = DEFAULT_LEEWAY_SECONDS,
        now: Optional[Any] = None,
        cache_seconds: int = JWKS_CACHE_SECONDS,
    ) -> None:
        self.config = config
        self.leeway = leeway
        self._now = now or (lambda: int(time.time()))
        self._cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._key_client = jwks or self._build_key_client()
        self._keys_loaded_at: float = 0.0

    def _build_key_client(self) -> PyJWKClient:
        logger.info("Verifying Cloudflare Access JWTs against %s", self.config.certs_url)
        return PyJWKClient(self.config.certs_url, cache_keys=True, lifespan=self._cache_seconds)

    # -- key handling -------------------------------------------------------

    def _signing_key(self, token: str) -> Optional[Any]:
        """
        Return the key for this token, refetching the key set if it is stale.

        PyJWKClient raises on an unknown ``kid``; that is exactly the signal that
        Cloudflare has rotated its keys, so force one refetch before giving up.
        """
        with self._lock:
            stale = (time.time() - self._keys_loaded_at) > self._cache_seconds
            if stale:
                try:
                    self._key_client.refresh()  # not all key clients implement this
                except Exception:  # noqa: BLE001 - refresh is best effort
                    pass
                self._keys_loaded_at = time.time()

            try:
                return self._key_client.get_signing_key_from_jwt(token).key
            except Exception as exc:  # noqa: BLE001 - any failure means "no key"
                self._keys_loaded_at = 0.0
                raise AuthError(f"No signing key for the presented token: {exc}") from exc

    # -- verification -------------------------------------------------------

    def verify(self, token: Optional[str]) -> "Identity":
        """Validate a raw assertion string and return the identity it proves."""
        if not token:
            raise AuthError(
                "No Cf-Access-Jwt-Assertion header. Either this request did not "
                "come through Cloudflare Access, or the Access policy does not "
                "cover this hostname."
            )

        try:
            key = self._signing_key(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.config.audience,
                issuer=self.config.issuer,
                leeway=self.leeway,
                options={
                    # Only what we actually rely on is required, so a change in
                    # Cloudflare's token shape cannot lock the user out.
                    "require": ["exp", "iss", "aud"],
                    "verify_exp": True,
                    "verify_iss": True,
                    "verify_aud": True,
                    "verify_signature": True,
                },
            )
        except AuthError:
            raise
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("Token has expired.") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError(
                "Token audience does not match AUTH_AUDIENCE. This usually means "
                "the token belongs to a different Access application."
            ) from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthError("Token issuer does not match AUTH_ISSUER.") from exc
        except jwt.PyJWTError as exc:
            raise AuthError(f"Token failed verification: {exc}") from exc

        email = (claims.get("email") or "").strip()
        if not email:
            raise AuthError(
                "Token is valid but carries no email claim, so the allowlist "
                "cannot be applied. Check that the Access policy uses an email "
                "rule rather than a service token."
            )

        if not self.config.is_allowed(email):
            # Deliberately does not echo the attempted address at INFO: it is
            # personal data and would end up in the log file and the UI.
            raise AuthError(
                "This account is not assigned to this installation. "
                "Add the address to AUTH_ALLOWED_EMAILS to allow it."
            )

        return Identity(email=email, claims=claims)


@dataclass(frozen=True)
class Identity:
    """A verified caller."""

    email: str
    claims: Dict[str, Any]

    @property
    def display_name(self) -> str:
        return self.email.split("@")[0]

    def as_dict(self) -> Dict[str, str]:
        return {"email": self.email}


# ---------------------------------------------------------------------------
# Request helpers (kept Flask-free so they can be unit tested)
# ---------------------------------------------------------------------------

ASSERTION_HEADER = "Cf-Access-Jwt-Assertion"


def assertion_from_headers(headers: Iterable[Tuple[str, str]]) -> Optional[str]:
    """
    Pull the assertion out of a header collection.

    Werkzeug's Headers is case-insensitive; a plain dict is not, and proxies can
    change case, so compare case-insensitively either way.
    """
    for name, value in headers:
        if name.lower() == ASSERTION_HEADER.lower():
            return (value or "").strip() or None
    return None
