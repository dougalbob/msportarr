"""Motorsportarr utilities package."""

from .config_loader import (
    get,
    get_int,
    get_bool,
    reload_settings,
    validate,
    get_prowlarr_url,
    get_prowlarr_api_key,
    get_sabnzbd_url,
    get_sabnzbd_api_key,
    get_plex_url,
    get_plex_token,
    get_plex_library_id,
    get_plex_library_name,
    get_motogp_library_path,
    get_downloads_path,
    get_discord_webhook,
    get_timezone,
)

# Backwards compatibility - expose old-style loader
from .env_loader import load_env, get_env

__all__ = [
    'get',
    'get_int', 
    'get_bool',
    'reload_settings',
    'validate',
    'get_prowlarr_url',
    'get_prowlarr_api_key',
    'get_sabnzbd_url',
    'get_sabnzbd_api_key',
    'get_plex_url',
    'get_plex_token',
    'get_plex_library_id',
    'get_plex_library_name',
    'get_motogp_library_path',
    'get_downloads_path',
    'get_discord_webhook',
    'get_timezone',
    'load_env',
    'get_env',
]