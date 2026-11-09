#!/usr/bin/env python3
"""
MotoGParr Configuration Loader
===============================

Loads settings with priority:
1. Environment variables
2. settings.yml (for easy configuration)
3. Defaults

Environment variables are the canonical way to configure a container, because
they are what a launcher (Unraid's template, docker run, compose) can actually
set. Every variable the app understands is declared once in ENV_SPEC below, and
that table is the single source of truth: the loader reads it, the Unraid
template is checked against it in the tests, and the docs are generated from it.

An environment variable that is set but EMPTY counts as "not set", so a template
can ship a blank field without silently overriding settings.yml.

Usage:
    from utils.config_loader import get_prowlarr_url
    
    prowlarr_url = get_prowlarr_url()
    # or
    from utils.config_loader import get
    prowlarr_url = get("prowlarr", "url")
"""

import os
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
SETTINGS_FILE = CONFIG_DIR / "settings.yml"

_settings_cache: Optional[Dict] = None


# =============================================================================
# ENVIRONMENT VARIABLE REGISTRY
# =============================================================================
#
# The single source of truth for "which variables does this app read?".
#
# Every entry maps an environment variable name to the settings.yml location it
# overrides. Nested keys are dotted ("path_mapping.container"). If a variable is
# missing from this table, the app does not read it - which is exactly the bug
# that made PLEX_PATH_CONTAINER / PLEX_PATH_PLEX documented but dead.
#
# Keys:
#   section     settings.yml section it overrides
#   key         settings.yml key (dotted for nested), or None for a top-level
#               setting that has no yaml equivalent (e.g. TZ)
#   cast        callable applied to the raw string
#   secret      never echo the value in logs / debug output
#   description one line, used by `python -m utils.config_loader --env-help`

ENV_SPEC: Dict[str, Dict[str, Any]] = {
    # --- services -----------------------------------------------------------
    "PROWLARR_URL": {
        "section": "prowlarr", "key": "url", "cast": str,
        "description": "Prowlarr base URL, e.g. http://192.168.1.100:9696",
    },
    "PROWLARR_API_KEY": {
        "section": "prowlarr", "key": "api_key", "cast": str, "secret": True,
        "description": "Prowlarr API key",
    },
    "SABNZBD_URL": {
        "section": "sabnzbd", "key": "url", "cast": str,
        "description": "SABnzbd base URL, e.g. http://192.168.1.100:8080",
    },
    "SABNZBD_API_KEY": {
        "section": "sabnzbd", "key": "api_key", "cast": str, "secret": True,
        "description": "SABnzbd API key",
    },
    "PLEX_URL": {
        "section": "plex", "key": "url", "cast": str,
        "description": "Plex base URL, e.g. http://192.168.1.100:32400",
    },
    "PLEX_TOKEN": {
        "section": "plex", "key": "token", "cast": str, "secret": True,
        "description": "Plex token",
    },
    "MOTOGP_PLEX_LIBRARY_ID": {
        "section": "plex", "key": "library_id", "cast": str,
        "description": "Plex library section id (the number in /library/sections/<id>/)",
    },
    "MOTOGP_PLEX_LIBRARY": {
        "section": "plex", "key": "library_name", "cast": str,
        "description": "MotoGP's Plex library display name",
    },
    "F1_PLEX_LIBRARY_ID": {
        "section": "plex", "key": "f1_library_id", "cast": str,
        "description": "Formula 1 Plex library section id",
    },
    "F1_PLEX_LIBRARY": {
        "section": "plex", "key": "f1_library_name", "cast": str,
        "description": "Formula 1's Plex library display name",
    },

    # --- paths --------------------------------------------------------------
    # One library path per series (library.<series>_path) and one downloads
    # path per series (library.<series>_downloads_path).
    #
    # Downloads used to be a single shared `library.downloads_path`. Two series
    # cannot share it: SABnzbd completes a grab into the folder named after its
    # category, and the watcher scans that folder for work to sort - so a shared
    # path means the MotoGP watcher sorting Formula 1 releases into the MotoGP
    # library, which nothing would report as an error.
    "MOTOGP_LIBRARY_PATH": {
        "section": "library", "key": "motogp_path", "cast": str,
        "description": "MotoGP library path INSIDE the container",
    },
    "MOTOGP_DOWNLOADS_PATH": {
        "section": "library", "key": "motogp_downloads_path", "cast": str,
        "description": "MotoGP completed-downloads path INSIDE the container",
    },
    "F1_LIBRARY_PATH": {
        "section": "library", "key": "f1_path", "cast": str,
        "description": "Formula 1 library path INSIDE the container",
    },
    "F1_DOWNLOADS_PATH": {
        "section": "library", "key": "f1_downloads_path", "cast": str,
        "description": "Formula 1 completed-downloads path INSIDE the container",
    },

    # --- Plex path translation ---------------------------------------------
    # Plex reports paths as its own container sees them. Without this mapping
    # the tagger compares two different strings and silently never matches,
    # which looks like "tagging stopped working" after any path change.
    "PLEX_PATH_CONTAINER": {
        "section": "plex", "key": "path_mapping.container", "cast": str,
        "description": "Path prefix as this container sees it, e.g. /media",
    },
    "PLEX_PATH_PLEX": {
        "section": "plex", "key": "path_mapping.plex", "cast": str,
        "description": "The same prefix as Plex sees it, e.g. /data/media",
    },
    # Per series, because an install with the two libraries on different host
    # shares commonly has Plex seeing them under different prefixes. The names
    # above stay as the default series' variables so no existing install has to
    # change a template that already works.
    "F1_PLEX_PATH_CONTAINER": {
        "section": "plex", "key": "f1_path_mapping.container", "cast": str,
        "description": "Formula 1 path prefix as this container sees it",
    },
    "F1_PLEX_PATH_PLEX": {
        "section": "plex", "key": "f1_path_mapping.plex", "cast": str,
        "description": "The same Formula 1 prefix as Plex sees it",
    },

    # --- misc ---------------------------------------------------------------
    "MOTOGPARR_HOST_MAP": {
        "section": "permissions",
        "key": None,
        "cast": str,
        "description": "Optional map of container path to host path, e.g. /media=/mnt/user/data/media,/downloads=/mnt/user/data/downloads, so the startup permission hint can print a host-runnable chown command",
    },
    "DISCORD_WEBHOOK_URL": {
        "section": "discord", "key": "webhook_url", "cast": str, "secret": True,
        "description": "Discord webhook URL for notifications (optional)",
    },
    "TZ": {
        "section": "timezone", "key": None, "cast": str,
        "description": "Timezone, e.g. Europe/London",
    },

    # --- authentication -----------------------------------------------------
    # Cloudflare Access verification. These live in ENV_SPEC because that table
    # is the single source of truth for "what does the app read", and the Unraid
    # template is checked against it. settings.yml is deliberately NOT an option:
    # an allowlist in a file the UI can rewrite is not a security control.
    "AUTH_ISSUER": {
        "section": "auth", "key": "issuer", "cast": str,
        "description": "Cloudflare Access issuer, e.g. https://<team>.cloudflareaccess.com",
    },
    "AUTH_AUDIENCE": {
        "section": "auth", "key": "audience", "cast": str,
        "description": "Access application AUD tag the JWT must be issued for",
    },
    "AUTH_ALLOWED_EMAILS": {
        "section": "auth", "key": "allowed_emails", "cast": str, "secret": True,
        "description": "Comma-separated emails allowed in (mirrors the CF policy)",
    },
    "AUTH_CERTS_URL": {
        "section": "auth", "key": "certs_url", "cast": str,
        "description": "Optional override for the JWKS URL (defaults to $AUTH_ISSUER/cdn-cgi/access/certs)",
    },
    "AUTH_MODE": {
        "section": "auth", "key": "mode", "cast": str,
        "description": "cloudflare (default when AUTH_ISSUER is set) or none",
    },
    "MOTOGPARR_COOKIE_SECURE": {
        "section": "auth", "key": "cookie_secure", "cast": str,
        "description": "1 = mark the session cookie Secure (only when served over https)",
    },
}


#: Variables read by entrypoint.sh rather than by the Python code. Kept separate
#: so "the app reads this" stays literally true for ENV_SPEC.
ENTRYPOINT_ENV: Dict[str, str] = {
    "MOTOGPARR_CALENDAR_UPDATE":
        "auto | warn | off - what to do when the /config calendars differ",
    "MOTOGPARR_SKIP_PATH_CHECK":
        "1 = start even when a configured path does not exist or is unwritable",
    "PUID":
        "uid to run as (default 99 = nobody; Unraid shares are 99:100)",
    "PGID":
        "gid to run as (default 100 = users)",
    "UMASK":
        "file creation mask for new media (default 0022)",
}

#: Informational only: set by the Dockerfile / build workflow, never a setting.
INFORMATIONAL_ENV = ("MOTOGPARR_VERSION",)

#: Read by ``series/binding.py`` rather than through this loader, and set by the
#: *app* for its own subprocesses (the scheduler hands each grabber/sorter run its
#: series). Not a user setting and not an entrypoint variable, so it lives in
#: none of the three tables above - importing series.binding here would be a
#: circular import for a string.
PROCESS_ENV = ("MOTOGPARR_SERIES",)


def all_known_env_vars() -> "set[str]":
    """Every variable this project understands, from any of the three sources."""
    return set(ENV_SPEC) | set(ENTRYPOINT_ENV) | set(INFORMATIONAL_ENV)


def _lookup_env_key(section: str, key: Optional[str]) -> Optional[str]:
    """Find the environment variable that overrides (section, key), if any."""
    for name, spec in ENV_SPEC.items():
        if spec["section"] == section and spec["key"] == key:
            return name
    return None


def _load_yaml() -> Dict:
    """Load settings from YAML file."""
    global _settings_cache
    
    if _settings_cache is not None:
        return _settings_cache
    
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, 'r') as f:
                _settings_cache = yaml.safe_load(f) or {}
                return _settings_cache
        except Exception as e:
            print(f"Warning: Could not load settings.yml: {e}")
    
    _settings_cache = {}
    return _settings_cache


def reload_settings():
    """Force reload of settings from disk."""
    global _settings_cache
    _settings_cache = None
    return _load_yaml()


def _yaml_lookup(settings: Dict, section: str, key: Optional[str]) -> Any:
    """Read a possibly-nested key out of the loaded settings.yml."""
    if key is None:
        return settings.get(section)

    node: Any = settings.get(section)
    for part in key.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def get_with_source(
    section: str, key: Optional[str], default: Any = None
) -> "tuple[Any, str]":
    """
    Resolve a setting and report where the value came from.

    Priority: environment variable > settings.yml > default.

    Returns:
        (value, source) where source is "env", "settings.yml" or "default".
        Knowing the source matters for paths: a container whose template
        overrides a path will not behave like settings.yml says it should.
    """
    env_name = _lookup_env_key(section, key)

    if env_name:
        env_value = os.environ.get(env_name)
        if env_value:  # empty string means "not set", so templates can ship blanks
            cast = ENV_SPEC[env_name].get("cast", str)
            return cast(env_value), "env"

    value = _yaml_lookup(_load_yaml(), section, key)
    if value is not None and value != "":
        return value, "settings.yml"

    return default, "default"


def get(section: str, key: Optional[str], default: Any = None) -> Any:
    """
    Get a setting value.

    Args:
        section: Section in YAML (e.g., 'prowlarr', 'plex')
        key: Key within section (e.g., 'url', 'api_key'). Dotted for nested
             keys (e.g. 'path_mapping.container'). Use None for a standalone
             setting such as the timezone.
        default: Default value if not found

    Returns:
        Setting value
    """
    return get_with_source(section, key, default)[0]


def has_setting(section: str, key: str) -> bool:
    """True when settings.yml explicitly contains (section, key).

    Distinct from :func:`get_with_source`: an explicit value that happens to
    equal the default is still "configured", so a stale copy in another store
    (webui_settings.json) must never overwrite it.
    """
    settings = _load_yaml() or {}
    node = settings.get(section)
    if not isinstance(node, dict):
        return False
    for part in key.split("."):
        if part not in node:
            return False
        node = node[part]
    return True


def get_int(section: str, key: str, default: int = 0) -> int:
    """Get setting as integer."""
    value = get(section, key)
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def get_bool(section: str, key: str, default: bool = False) -> bool:
    """Get setting as boolean."""
    value = get(section, key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")
    return default


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def get_prowlarr_url() -> Optional[str]:
    return get("prowlarr", "url")


def get_prowlarr_api_key() -> Optional[str]:
    return get("prowlarr", "api_key")


def get_sabnzbd_url() -> Optional[str]:
    return get("sabnzbd", "url")


def get_sabnzbd_api_key() -> Optional[str]:
    return get("sabnzbd", "api_key")


def get_plex_url() -> Optional[str]:
    return get("plex", "url")


def get_plex_token() -> Optional[str]:
    return get("plex", "token")


def _default_series_key() -> str:
    """
    The series a caller gets when it names none: this process' binding.

    Not "the default series" - that distinction is the whole point of
    ``series/binding.py``. A grabber started as ``--series f1`` must resolve
    every unqualified path, category and Plex id to Formula 1; if this returned
    MotoGP it would search F1's calendar and file the results in MotoGP's
    library.

    In the WebUI the binding is the default series, because that process serves
    both and passes a key explicitly wherever it means another one.
    """
    from series.binding import bound_series

    return bound_series()


def _non_default_series_keys() -> list:
    """Registered series other than the default, in registration order.

    The default series is reported under the un-prefixed keys above, which is
    what an existing settings.yml and the existing WebUI both use; adding it
    again under ``<series>_*`` would show the same value twice.
    """
    from series import all_series, default_series

    return [spec.key for spec in all_series() if spec.key != default_series().key]


def _library_key(series: str) -> str:
    """
    The settings.yml key holding one series' library path.

    ``library.<series>_path``, with no special case for any series: the default
    series' historical key already happens to be ``library.motogp_path``, which
    is that pattern. Nothing here needs to know which series is which.
    """
    return f"{series}_path"


def _downloads_key(series: str) -> str:
    """The settings.yml key holding one series' completed-downloads path."""
    return f"{series}_downloads_path"


def get_series_library_path(series: str = None) -> str:
    """
    The library root for one series, in-container.

    Resolution order: ``MOTOGP_LIBRARY_PATH`` / ``F1_LIBRARY_PATH`` (or the
    matching ``library.<series>_path`` in settings.yml), then the series'
    declaration, which supplies the path the app has always used for MotoGP and
    a separate one for Formula 1.

    The declaration is the last resort rather than the first because the two
    series must be able to sit on different host shares, but it is not optional:
    without it a second series would inherit ``/media/MotoGP`` and sort into the
    wrong library.
    """
    from series import get_series

    key = series or _default_series_key()
    value = get("library", _library_key(key), default=None)
    return str(value) if value else get_series(key).library_path


def get_series_downloads_path(series: str = None) -> str:
    """
    Where one series' completed downloads appear, in-container.

    **The default series also reads the historical shared key.** Before a second
    series existed, ``library.downloads_path`` was the only downloads path and
    had no series in its name. It still means the default series' path, and is
    preferred over ``library.motogp_downloads_path`` when both are present,
    because a settings.yml that has one and not the other is the normal case and
    the value already in the file is the one the user meant.

    There is deliberately no shared fallback for any *other* series: a series
    with no configured downloads path must not read another series' folder, or
    the watcher will sort one championship's releases into the other's library.
    """
    from series import get_series

    key = series or _default_series_key()
    if key == _default_series_key():
        legacy = get("library", "downloads_path", default=None)
        if legacy:
            return str(legacy)
    value = get("library", _downloads_key(key), default=None)
    return str(value) if value else get_series(key).downloads_path


def series_is_configured(series: str) -> bool:
    """
    Has this series ever been set up on this install?

    True when its library path is set explicitly - in the environment or in
    settings.yml - or when the path it resolves to already exists on disk. The
    second test is what an unattended install gets: put the folder where the
    declaration says it goes and the series is live, with nothing to configure.

    The **default series is always configured.** It is the series this app was
    built for, it has media on disk on every existing install, and gating it
    would mean an empty settings.yml switched MotoGP off.
    """
    from series import default_series, get_series

    if series == default_series().key:
        return True

    explicit_env = _lookup_env_key("library", _library_key(series))
    if explicit_env and os.environ.get(explicit_env):
        return True
    if has_setting("library", _library_key(series)):
        return True

    path = get_series_library_path(series)
    return bool(path) and os.path.isdir(str(path))


def active_series() -> list:
    """
    The series this install actually runs.

    Everything that does per-series work - the path and permission checks, the
    scheduler's round evaluation, the WebUI's queue view - filters on this, so
    an install that only ever wanted MotoGP is not asked to create folders for a
    championship it does not follow.

    This matters most on the check that refuses to start: once there are two
    libraries, "the library path does not exist" would stop an existing
    MotoGP-only install because *Formula 1's* default folder is missing. A
    dormant series is not a broken one.
    """
    from series import all_series

    return [spec for spec in all_series() if series_is_configured(spec.key)]


def get_series_sabnzbd_category(series: str = None) -> str:
    """
    The SABnzbd category this series' grabs are filed and completed under.

    From the declaration, not settings.yml: the category has to agree with the
    downloads path, and the declaration is the one place both are written down
    together, where they cannot drift.
    """
    from series import get_series

    return get_series(series or _default_series_key()).sabnzbd_category


def get_plex_library_id(series: str = None) -> Optional[str]:
    """
    One series' Plex library section id.

    A pre-hybrid settings.yml has exactly one ``plex.library_id``, and it means
    the default series' library. It is read as such, so an existing install
    keeps tagging into the library it already has; any other series reads
    ``plex.<series>_library_id`` and gets nothing when that is unset, because
    inheriting the default series' id would tag Formula 1 files into the MotoGP
    library - and Plex ignores an id that does not match the file's library
    without reporting anything.
    """
    key = series or _default_series_key()
    if key == _default_series_key():
        return get("plex", "library_id")
    return get("plex", f"{key}_library_id")


def get_plex_library_name(series: str = None) -> str:
    """One series' Plex library display name. Used in log lines and reports."""
    from series import get_series

    key = series or _default_series_key()
    spec = get_series(key)
    if key == _default_series_key():
        return get("plex", "library_name", spec.plex_library_name)
    return get("plex", f"{key}_library_name", spec.plex_library_name)


def get_motogp_library_path() -> str:
    """The default series' library path. Kept for the many existing callers."""
    return get_series_library_path(None)


def get_downloads_path(series: str = None) -> str:
    """
    Where one series' completed downloads appear.

    **Reads ``library.downloads_path`` for the default series**, which is where
    every existing settings.yml puts it, in preference to the newer
    ``library.motogp_downloads_path``. Both are accepted; the old key wins when
    both are set, because a file that has one and not the other is the normal
    case and the value in the file is what the user meant.
    """
    from series import default_series

    key = series or _default_series_key()
    if key == default_series().key:
        legacy = get("library", "downloads_path", default=None)
        if legacy:
            return str(legacy)
    return get_series_downloads_path(key)


def get_discord_webhook() -> Optional[str]:
    return get("discord", "webhook_url")


def get_timezone() -> str:
    """Timezone name. TZ wins (Docker sets it), then settings.yml, then UK."""
    return get("timezone", None, "Europe/London")


def get_plex_path_mapping(series: str = None) -> dict:
    """
    Get one series' Plex path mapping, used to translate container paths.

    Read per-key rather than as one dict, so each side can come from either
    PLEX_PATH_CONTAINER / PLEX_PATH_PLEX or settings.yml:

        plex:
          path_mapping:
            container: /media
            plex: /data/media

    **Per series**, because the mapping is a property of where a library is
    mounted, not of the install. Two libraries on different host shares are
    routinely seen by Plex under different prefixes, and a single shared mapping
    translates the first correctly and the second into a path that does not
    exist - which the Plex tagger reports as "nothing to tag", not as an error.

    A series with no mapping of its own falls back to the shared
    ``plex.path_mapping`` block, which is where every existing settings.yml has
    it and which is still correct for any install that mounts both libraries the
    same way.
    """
    key = series or _default_series_key()
    own_container = get("plex", f"{key}_path_mapping.container") \
        if key != _default_series_key() else None
    own_plex = get("plex", f"{key}_path_mapping.plex") \
        if key != _default_series_key() else None

    container = own_container or get("plex", "path_mapping.container")
    plex = own_plex or get("plex", "path_mapping.plex")
    return {
        "container": str(container) if container else None,
        "plex": str(plex) if plex else None,
    }


def plex_path_mapping_complete(series: str = None) -> bool:
    """True when both sides of the mapping are set, i.e. translation can work."""
    mapping = get_plex_path_mapping(series)
    return bool(mapping["container"] and mapping["plex"])


def translate_path_for_plex(container_path: str, series: str = None) -> str:
    """Translate a container path to what Plex expects, for one series."""
    mapping = get_plex_path_mapping(series)
    container_prefix = mapping.get("container")
    plex_prefix = mapping.get("plex")
    
    if container_prefix and plex_prefix and container_path.startswith(container_prefix):
        return container_path.replace(container_prefix, plex_prefix, 1)
    
    return container_path


# =============================================================================
# GRABBER-SPECIFIC SETTINGS
# =============================================================================

#: Release groups the grabber accepts when nothing is configured.
#:
#: This default is load-bearing, not cosmetic: filter_releases() rejects any
#: release whose group is not in the list, so an empty list means *nothing* is
#: ever grabbed - silently, because an empty candidate list looks the same as
#: "no releases published yet". Duplicated in motogp_common.filter_releases();
#: a test asserts the two agree.
DEFAULT_ALLOWED_GROUPS = ("VERUM", "MWR", "BILLIE")


def get_allowed_groups() -> List[str]:
    """
    Release groups the grabber will accept.

    Read from config but never written by the UI before v2.5.0, so it was
    effectively a hidden setting: present in settings.yml, invisible everywhere
    else. Accepts a list (settings.yml) or a comma-separated string (env), and
    falls back to DEFAULT_ALLOWED_GROUPS when unset.

    An explicitly configured empty list is honoured - that is a deliberate
    choice rather than an accident, so it is left to the caller to be surprised.
    """
    value = get("grabber", "allowed_groups")

    if value is None or value == "":
        return list(DEFAULT_ALLOWED_GROUPS)
    if isinstance(value, str):
        groups = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        groups = [str(part).strip() for part in value if str(part).strip()]
    else:
        return list(DEFAULT_ALLOWED_GROUPS)

    # A configured-but-empty value is almost always a YAML mistake such as
    # `allowed_groups:` with nothing after it. Falling back beats silently
    # grabbing nothing for a whole season.
    return groups or list(DEFAULT_ALLOWED_GROUPS)


def get_required_quality() -> str:
    """Get required video quality."""
    return get("grabber", "required_quality", "1080p")


def get_max_grabs_per_run() -> int:
    """Get maximum grabs per run."""
    return get_int("grabber", "max_grabs_per_run", 22)


def get_search_delay_hours() -> float:
    """Get configured search delay as hours while accepting older YAML files."""
    settings = _load_yaml() or {}
    grabber = settings.get("grabber", {})
    if isinstance(grabber, dict) and "search_delay_minutes" in grabber:
        return get_int("grabber", "search_delay_minutes", 60) / 60
    # Keep existing installs with the previous key/value working after upgrade.
    return get_int("grabber", "search_delay_hours", 1)



def get_current_season() -> int:
    """Get current season year."""
    return get_int("grabber", "current_season", 2026)


# =============================================================================
# VALIDATION
# =============================================================================

def validate() -> Dict[str, Any]:
    """
    Validate that required settings are configured.
    
    Returns:
        Dict with 'valid' bool, 'missing' list, and 'configured' list
    """
    required = [
        ("prowlarr", "url", "Prowlarr URL"),
        ("prowlarr", "api_key", "Prowlarr API Key"),
        ("sabnzbd", "url", "SABnzbd URL"),
        ("sabnzbd", "api_key", "SABnzbd API Key"),
        ("plex", "url", "Plex URL"),
        ("plex", "token", "Plex Token"),
    ]
    
    optional = [
        ("plex", "library_id", "Plex Library ID"),
        ("plex", "library_name", "Plex Library Name"),
        ("library", "motogp_path", "MotoGP Library Path"),
        ("library", "downloads_path", "Downloads Path"),
        ("library", "f1_path", "Formula 1 Library Path"),
        ("library", "f1_downloads_path", "Formula 1 Downloads Path"),
        ("discord", "webhook_url", "Discord Webhook"),
    ]
    
    missing = []
    configured = []
    
    for section, key, name in required:
        value = get(section, key)
        if value:
            configured.append(name)
        else:
            missing.append(name)
    
    for section, key, name in optional:
        value = get(section, key)
        if value:
            configured.append(name)
    
    # Schema problems are reported but do not make the config invalid: the app
    # has a default for everything, and refusing to start over a typo in an
    # optional key would be worse than telling the user about it.
    try:
        from utils.settings_schema import validate_file
        schema = validate_file(SETTINGS_FILE)
    except Exception as exc:  # noqa: BLE001 - never let the checker break startup
        schema = None
        print(f"Warning: could not validate settings.yml: {exc}")

    return {
        "valid": len(missing) == 0,
        "missing": missing,
        "configured": configured,
        "settings_file": str(SETTINGS_FILE),
        "settings_file_exists": SETTINGS_FILE.exists(),
        "schema": schema.as_dict() if schema else {"ok": True, "issues": [], "messages": []},
    }


def credential_sources() -> Dict[str, str]:
    """
    Where each credential came from: "env", "settings.yml" or "default".

    Exists because of a specific, repeated confusion: a key typed into the WebUI
    is saved to settings.yml and appears to take effect, while an environment
    variable in the container template quietly wins. The app then fails
    authentication against Prowlarr/Plex with a key the user has already
    replaced, and blames the wrong thing.

    Values are never returned - only the source.
    """
    return {
        name: get_with_source(section, key)[1]
        for name, (section, key) in {
            "prowlarr_api_key": ("prowlarr", "api_key"),
            "sabnzbd_api_key": ("sabnzbd", "api_key"),
            "plex_token": ("plex", "token"),
        }.items()
    }


def get_all_settings() -> Dict[str, Any]:
    """Get all current settings (for display/debugging)."""
    return {
        "prowlarr": {
            "url": get_prowlarr_url(),
            "api_key": "***" if get_prowlarr_api_key() else None,
        },
        "sabnzbd": {
            "url": get_sabnzbd_url(),
            "api_key": "***" if get_sabnzbd_api_key() else None,
        },
        "plex": {
            "url": get_plex_url(),
            "token": "***" if get_plex_token() else None,
            "library_id": get_plex_library_id(),
            "library_name": get_plex_library_name(),
            "path_mapping": get_plex_path_mapping(),
            **{
                f"{key}_library_id": get_plex_library_id(key)
                for key in _non_default_series_keys()
            },
            **{
                f"{key}_library_name": get_plex_library_name(key)
                for key in _non_default_series_keys()
            },
        },
        "library": {
            "motogp_path": get_motogp_library_path(),
            "downloads_path": get_downloads_path(),
            **{
                f"{key}_path": get_series_library_path(key)
                for key in _non_default_series_keys()
            },
            **{
                f"{key}_downloads_path": get_series_downloads_path(key)
                for key in _non_default_series_keys()
            },
        },
        "grabber": {
            "allowed_groups": get_allowed_groups(),
            "required_quality": get_required_quality(),
            "max_grabs_per_run": get_max_grabs_per_run(),
            "search_delay_hours": get_search_delay_hours(),
            "current_season": get_current_season(),
        },
        "discord": {
            "webhook_url": "***" if get_discord_webhook() else None,
        },
        "timezone": get_timezone(),
    }


# =============================================================================
# STARTUP DIAGNOSTICS
# =============================================================================

#: Settings whose value is a path inside the container. A wrong one of these
#: looks identical to "the share is not mounted": the app starts happily and
#: quietly does nothing.
PATH_SETTINGS = [
    ("library", "motogp_path", "Library path"),
    ("library", "downloads_path", "Downloads path"),
    ("library", "f1_path", "Formula 1 library path"),
    ("library", "f1_downloads_path", "Formula 1 downloads path"),
]


def describe_setting(section: str, key: Optional[str], label: str) -> Dict[str, Any]:
    """Resolve one setting and say where it came from and whether it exists."""
    value, source = get_with_source(section, key)
    info: Dict[str, Any] = {
        "label": label,
        "section": section,
        "key": key,
        "value": value,
        "source": source,
        "env_var": _lookup_env_key(section, key),
        "exists": None,
        "warning": None,
    }
    if value:
        info["exists"] = Path(str(value)).exists()
        if not info["exists"]:
            info["warning"] = (
                f"{label} does not exist inside the container: {value}"
            )
    else:
        info["warning"] = f"{label} is not configured"
    return info


def startup_report() -> Dict[str, Any]:
    """
    Everything the entrypoint should print before starting work.

    The point is that a misconfigured path is loud at startup instead of
    silently producing wrong results hours later.
    """
    paths = [
        describe_setting(section, key, label)
        for section, key, label in PATH_SETTINGS
    ]

    mapping = get_plex_path_mapping()
    mapping_complete = plex_path_mapping_complete()

    warnings = [p["warning"] for p in paths if p["warning"]]

    if not mapping_complete and paths[0]["value"]:
        warnings.append(
            "Plex path mapping is incomplete - Plex will not be told about new "
            "files by exact path. Set plex.path_mapping (container/plex) in "
            "settings.yml, or PLEX_PATH_CONTAINER / PLEX_PATH_PLEX."
        )

    return {
        "paths": paths,
        "plex_path_mapping": mapping,
        "plex_path_mapping_complete": mapping_complete,
        "settings_file": str(SETTINGS_FILE),
        "settings_file_exists": SETTINGS_FILE.exists(),
        "warnings": warnings,
    }


# =============================================================================
# SETTINGS FILE MANAGEMENT
# =============================================================================

def create_template() -> bool:
    """Create settings.yml template if it doesn't exist."""
    if SETTINGS_FILE.exists():
        return False
    
    template = """# MotoGParr Configuration
# ========================
# Edit these values to match your setup.
# Environment variables take priority over these settings.
#
# IMPORTANT: Path Configuration
# -----------------------------
# Paths in this file are CONTAINER paths, not host paths.
# They must match your Docker volume mounts.
#
# Example volume mounts (docker run):
#   -v /mnt/user/data/media:/media
#   -v /mnt/user/data/downloads:/downloads
#   -v /mnt/user/appdata/motogparr:/config
#
# To find your Plex library ID:
#   1. Open Plex web interface
#   2. Go to your MotoGP library
#   3. Look at the URL - it contains: /library/sections/X/
#   4. X is your library_id

prowlarr:
  url: ""          # e.g., http://192.168.1.100:9696
  api_key: ""

sabnzbd:
  url: ""          # e.g., http://192.168.1.100:8080
  api_key: ""

plex:
  url: ""          # e.g., http://192.168.1.100:32400
  token: ""        # See: https://support.plex.tv/articles/204059436
  library_id: ""   # MotoGP library section ID (see instructions above)
  library_name: "Motorsport - MotoGP"
  path_mapping:    # Both required if Plex sees the files under a different path
    container: ""  # Path prefix as THIS container sees it, e.g. /media
    plex: ""       # The same prefix as PLEX sees it, e.g. /data/media

  # Formula 1, which lives in its own Plex library. Fill these in only when
  # the F1 library path above is enabled. The mapping is separate because the
  # two libraries are often on different host shares, and Plex then sees them
  # under different prefixes; leave both sides blank to reuse plex.path_mapping.
  f1_library_id: ""   # /library/sections/<ID>/ in the URL of the F1 library
  f1_library_name: "Motorsport - Formula 1"
  f1_path_mapping:
    container: ""
    plex: ""

library:
  # One library and one downloads folder per series. They must not be shared:
  # SABnzbd completes a grab into the folder named after its category, and the
  # watcher sorts whatever lands in the folder it is watching - so a shared
  # downloads path files one series' releases into the other's library.
  motogp_path: "/media/MotoGP"
  downloads_path: "/downloads/complete/motogp"

  # Formula 1 is OFF until one of these lines is uncommented. A series that is
  # configured has its library path checked at startup and STOPS the container
  # when it is missing, so shipping these values live would refuse to start
  # every install that does not follow Formula 1. Uncommenting a line is the
  # whole opt-in; the defaults below are the paths shown in the template.
  # f1_path: "/media/F1"
  # f1_downloads_path: "/downloads/complete/f1"

grabber:
  allowed_groups: ["VERUM", "MWR"]
  required_quality: "1080p"
  max_grabs_per_run: 22
  search_delay_minutes: 240
  current_season: 2026

webui:
  quick_status_log_lines: 100

discord:
  webhook_url: ""  # Optional: for notifications

timezone: "Europe/London"
"""
    
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_FILE, 'w') as f:
            f.write(template)
        return True
    except Exception as e:
        print(f"Error creating settings template: {e}")
        return False


def update_settings(updates: Dict) -> bool:
    """
    Merge ``updates`` into settings.yml instead of replacing the file.

    This exists because replacing the file was silently destructive: the WebUI
    used to rebuild the whole document from the four sections it knew about, so
    every save from the UI deleted anything else in the file - the admin
    password, the entire grabber section, and any key added since. Nobody
    noticed because nothing failed; the settings just quietly reverted.

    Rules:
      * nested dicts are merged key by key, so a partial ``plex`` payload does
        not drop ``plex.library_name``;
      * a key explicitly set to ``None`` is deleted (that is how the caller says
        "remove this"), an omitted key is left alone;
      * an empty string is kept, because "clear this field" is a real intent and
        the env-var layer already treats empty as unset.
    """
    global _settings_cache

    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        current = dict(_load_yaml() or {})
        merged = _deep_merge(current, updates)

        # Write via a temporary file so a crash mid-write cannot leave a
        # truncated settings.yml behind - the file is small, the cost is nil.
        tmp_path = SETTINGS_FILE.with_suffix(".yml.tmp")
        with open(tmp_path, "w") as f:
            yaml.dump(merged, f, default_flow_style=False, sort_keys=False)
        tmp_path.replace(SETTINGS_FILE)

        _settings_cache = merged
        return True
    except Exception as e:
        print(f"Error saving settings: {e}")
        return False


def _deep_merge(base: Dict, updates: Dict) -> Dict:
    """Recursively merge updates into a copy of base. See update_settings."""
    result = dict(base)
    for key, value in updates.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def save_settings(settings: Dict) -> bool:
    """
    Replace settings.yml wholesale.

    Kept for callers that genuinely own the whole document (the template
    creator). Prefer :func:`update_settings`, which cannot lose keys it was not
    told about.
    """
    global _settings_cache

    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_FILE, 'w') as f:
            yaml.dump(settings, f, default_flow_style=False, sort_keys=False)
        _settings_cache = settings
        return True
    except Exception as e:
        print(f"Error saving settings: {e}")
        return False


# =============================================================================
# MODULE TEST
# =============================================================================

def env_help() -> str:
    """Render every environment variable the project understands."""
    width = max(len(name) for name in all_known_env_vars())

    lines = ["Read by the Python code (override settings.yml):", ""]
    for name, spec in sorted(ENV_SPEC.items()):
        section = spec["section"]
        key = spec["key"]
        target = section if key is None else f"{section}.{key}"
        flags = " (secret)" if spec.get("secret") else ""
        lines.append(
            f"  {name.ljust(width)}  {target.ljust(28)} {spec['description']}{flags}"
        )

    lines += ["", "Read by entrypoint.sh at container start:", ""]
    for name, description in sorted(ENTRYPOINT_ENV.items()):
        lines.append(f"  {name.ljust(width)}  {'(container start)'.ljust(28)} {description}")

    lines += ["", "Informational (set by the image build):", ""]
    for name in INFORMATIONAL_ENV:
        lines.append(f"  {name.ljust(width)}  {'(reported at startup)'.ljust(28)} Which release is running")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if "--env-help" in sys.argv:
        print("Environment variables understood by MotoGParr:")
        print()
        print(env_help())
        raise SystemExit(0)

    print("=== MotoGParr Configuration ===")
    print(f"Settings file: {SETTINGS_FILE}")
    print(f"Exists: {SETTINGS_FILE.exists()}")
    print()

    validation = validate()
    if validation["valid"]:
        print("✅ All required settings configured")
    else:
        print("❌ Missing required settings:")
        for item in validation["missing"]:
            print(f"   - {item}")

    print()
    print("Paths:")
    report = startup_report()
    for entry in report["paths"]:
        state = "ok" if entry["exists"] else "MISSING"
        print(f"  {entry['label']}: {entry['value']}  [{entry['source']}, {state}]")
    print(f"  Plex path mapping: {report['plex_path_mapping']}")

    for warning in report["warnings"]:
        print(f"  ! {warning}")

    print()
    print("Configured:")
    for item in validation["configured"]:
        print(f"   ✓ {item}")
