#!/usr/bin/env python3
"""
MotoGP Discord Notifications
==============================

Sends Discord notifications when sessions are added to the library.
"""

import os
import json
import logging
import requests
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add script directory to path
SCRIPT_DIR = Path(__file__).parent.resolve()

try:
    from utils.config_loader import get_discord_webhook, get_config_dir
except ImportError:
    def get_discord_webhook():
        return os.environ.get("DISCORD_WEBHOOK_URL")
    def get_config_dir():
        return SCRIPT_DIR / "config"

# Import centralized logging
try:
    from motogp_common import get_logger
    logger = get_logger("discord")
except ImportError:
    logger = logging.getLogger(__name__)

# =============================================================================
# RATE LIMITING
# =============================================================================

RATE_LIMIT_FILE = get_config_dir() / "discord_rate_limit.json"
RATE_LIMIT_SECONDS = 60  # Minimum seconds between notifications


def _load_rate_limit() -> dict:
    """Load rate limit state."""
    if RATE_LIMIT_FILE.exists():
        try:
            with open(RATE_LIMIT_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_notification": None}


def _save_rate_limit(state: dict):
    """Save rate limit state."""
    try:
        RATE_LIMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RATE_LIMIT_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        logger.warning(f"Could not save rate limit state: {e}")


def _is_rate_limited() -> bool:
    """Check if we're currently rate limited."""
    state = _load_rate_limit()
    last = state.get("last_notification")
    
    if not last:
        return False
    
    try:
        last_time = datetime.fromisoformat(last)
        elapsed = (datetime.now() - last_time).total_seconds()
        return elapsed < RATE_LIMIT_SECONDS
    except Exception:
        return False


def _update_rate_limit():
    """Update rate limit timestamp."""
    state = {"last_notification": datetime.now().isoformat()}
    _save_rate_limit(state)

# =============================================================================
# NOTIFICATION SETTINGS
# =============================================================================

# Sessions that trigger notifications (feature sessions)
NOTIFY_SESSIONS = {
    "MotoGP Race",
    "Moto2 Race", 
    "Moto3 Race",
    "MotoGP Sprint",
    "MotoGP Qualifying 1",
    "MotoGP Qualifying 2",
}


def should_notify(session_name: str) -> bool:
    """Check if this session type should trigger a notification."""
    return session_name in NOTIFY_SESSIONS

# =============================================================================
# DISCORD API
# =============================================================================

def send_discord_message(
    message: str,
    title: str = None,
    color: int = 0x00FF00,
    thumbnail_url: str = None,
    dry_run: bool = False
) -> bool:
    """
    Send a message to Discord via webhook.
    
    Args:
        message: Message content
        title: Embed title
        color: Embed color (hex)
        thumbnail_url: Optional thumbnail image
        dry_run: If True, just log without sending
    
    Returns:
        True if sent successfully
    """
    webhook_url = get_discord_webhook()
    
    if not webhook_url:
        logger.debug("Discord webhook not configured")
        return False
    
    if _is_rate_limited():
        logger.debug("Discord rate limited, skipping notification")
        return False
    
    if dry_run:
        logger.info(f"[DRY-RUN] Would send Discord: {title} - {message}")
        return True
    
    try:
        embed = {
            "description": message,
            "color": color,
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {"text": "Motorsportarr"}
        }
        
        if title:
            embed["title"] = title
        
        if thumbnail_url:
            embed["thumbnail"] = {"url": thumbnail_url}
        
        payload = {"embeds": [embed]}
        
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=10
        )
        
        if response.status_code in (200, 204):
            _update_rate_limit()
            logger.info(f"Discord notification sent: {title}")
            return True
        else:
            logger.warning(f"Discord webhook returned {response.status_code}")
            return False
    
    except Exception as e:
        logger.error(f"Discord notification failed: {e}")
        return False

# =============================================================================
# NOTIFICATION FUNCTIONS
# =============================================================================

def notify_session_added(
    session_name: str,
    venue: str,
    round_num: int,
    season: int,
    dry_run: bool = False
) -> bool:
    """
    Send notification that a session was added to the library.
    
    Args:
        session_name: Full session name (e.g., "MotoGP Race")
        venue: Race venue
        round_num: Round number
        season: Season year
        dry_run: If True, just log
    
    Returns:
        True if notification sent
    """
    title = f"🏍️ {session_name} Added"
    message = f"**Round {round_num} - {venue}** ({season})\n\nNow available in Plex!"
    
    # Green for races, blue for qualifying/practice
    if "Race" in session_name or "Sprint" in session_name:
        color = 0x00FF00  # Green
    else:
        color = 0x3498DB  # Blue
    
    return send_discord_message(
        message=message,
        title=title,
        color=color,
        dry_run=dry_run
    )


def notify_error(
    error_message: str,
    context: str = None,
    dry_run: bool = False
) -> bool:
    """Send error notification to Discord."""
    title = "❌ Motorsportarr Error"
    message = error_message
    
    if context:
        message = f"**{context}**\n\n{error_message}"
    
    return send_discord_message(
        message=message,
        title=title,
        color=0xFF0000,  # Red
        dry_run=dry_run
    )


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import argparse
    
    logging.basicConfig(level=logging.DEBUG)
    
    parser = argparse.ArgumentParser(description="MotoGP Discord Notifications")
    parser.add_argument("--test", action="store_true", help="Send test notification")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually send")
    args = parser.parse_args()
    
    if args.test:
        success = notify_session_added(
            session_name="MotoGP Race",
            venue="Test Circuit",
            round_num=1,
            season=2025,
            dry_run=args.dry_run
        )
        print(f"Test notification: {'sent' if success else 'failed'}")