#!/usr/bin/env python3
"""
MotoGParr cleanup utility - DELETES media. Not a test.
================================================================

Randomly removes grabbed episodes from the Plex library and from
grabber_state.json so the download path can be exercised again.

This lives in scripts/, outside the test suite and outside the container image,
for three reasons:

  1. It destroys data. It was named test_cleanup_utility.py at the repository
     root, which meant `pytest` could collect it, and a test run could delete
     library folders. That is a foot-gun, not a test.
  2. It was shipped in the image, so anyone who reached the WebUI's host could
     have run it inside the container as root.
  3. It does not assert anything. Its imports were broken for months and no CI
     noticed, because nothing about it was a test.

It deletes only leaf session folders whose names match the expected
`<prefix> ...` pattern inside a `Round NN ...` folder, and it refuses to run
without --confirm.

Usage:
    python scripts/cleanup_utility.py --count 5 --dry-run
    python scripts/cleanup_utility.py --count 10 --confirm
    python scripts/cleanup_utility.py --count 5 --season 2025 --confirm
"""

import argparse
import json
import os
import random
import re
import shutil
import sys
from pathlib import Path
from datetime import datetime

# Configuration
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
# Mirrors config_loader.get_motogp_library_path(). The old default
# (/media/MotoGP) did not match the documented layout, so the utility found
# nothing and looked "safe" when it was really just pointed at the wrong tree.
PLEX_LIBRARY_BASE = Path(
    os.environ.get("MOTOGP_LIBRARY_PATH", "/media/Motorsport/MotoGP")
)
#: The grabber's state is namespaced per series since the hybrid change
#: (grabber_state.motogp.json). This utility REWRITES that history, so it must
#: resolve the same file the app reads: rewriting the old unprefixed name would
#: look like it worked and change nothing, and creating it would leave two
#: files claiming to be the truth.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.state_files import state_path  # noqa: E402

GRABBER_STATE_FILE = state_path(CONFIG_DIR, "grabber_state")

def load_grabber_state():
    """Load the grabber state JSON file."""
    if not GRABBER_STATE_FILE.exists():
        print(f"❌ Error: Grabber state file not found: {GRABBER_STATE_FILE}")
        sys.exit(1)
    
    with open(GRABBER_STATE_FILE, 'r') as f:
        return json.load(f)

def save_grabber_state(state, dry_run=False):
    """Save the grabber state JSON file."""
    if dry_run:
        print("\n[DRY-RUN] Would save updated grabber_state.json")
        return
    
    # Backup first
    backup_file = GRABBER_STATE_FILE.with_suffix('.json.backup')
    shutil.copy(GRABBER_STATE_FILE, backup_file)
    print(f"📦 Created backup: {backup_file}")
    
    with open(GRABBER_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
    print(f"💾 Saved updated grabber_state.json")

def parse_session_key(session_key):
    """
    Parse session key format: 2025_R01_E05
    Returns: (season, round_num, episode_num) or None if invalid
    """
    try:
        parts = session_key.split('_')
        if len(parts) != 3:
            return None
        
        season = parts[0]  # "2025"
        round_num = int(parts[1][1:])  # "R01" -> 1
        episode_num = int(parts[2][1:])  # "E05" -> 5
        
        return (season, round_num, episode_num)
    except (ValueError, IndexError):
        return None

def session_key_to_folder_prefix(session_key):
    """
    Convert session key to Plex folder prefix.
    2025_R01_E05 -> 25R01E05
    """
    parsed = parse_session_key(session_key)
    if not parsed:
        return None
    
    season, round_num, episode_num = parsed
    # Take last 2 digits of year
    year_short = season[-2:]
    return f"{year_short}R{round_num:02d}E{episode_num:02d}"

def find_plex_folder(session_key, season_filter=None):
    """
    Find the Plex folder for a given session key.
    Returns the full path to the episode folder, or None if not found.
    """
    parsed = parse_session_key(session_key)
    if not parsed:
        return None
    
    season, round_num, episode_num = parsed
    
    # Apply season filter if specified
    if season_filter and season != str(season_filter):
        return None
    
    folder_prefix = session_key_to_folder_prefix(session_key)
    if not folder_prefix:
        return None
    
    # Build path: /base/2025/Round X .../25RxxExx .../
    season_path = PLEX_LIBRARY_BASE / season
    
    if not season_path.exists():
        return None
    
    # Search for the round folder (format: "Round X Venue")
    for round_folder in season_path.iterdir():
        if not round_folder.is_dir():
            continue
        
        # Check if this is the right round (e.g., "Round 1 Thailand")
        if not round_folder.name.startswith(f"Round {round_num} "):
            continue
        
        # Search for episode folder within round
        for episode_folder in round_folder.iterdir():
            if episode_folder.is_dir() and episode_folder.name.startswith(folder_prefix):
                return episode_folder
    
    return None

def get_eligible_episodes(state, season_filter=None):
    """
    Get list of grabbed episodes that exist in Plex library.
    Returns list of (session_key, plex_folder_path) tuples.
    """
    eligible = []
    grabbed = state.get("grabbed", {})
    
    for session_key in grabbed.keys():
        # Apply season filter
        if season_filter:
            parsed = parse_session_key(session_key)
            if not parsed or parsed[0] != str(season_filter):
                continue
        
        plex_folder = find_plex_folder(session_key, season_filter)
        if plex_folder and plex_folder.exists():
            eligible.append((session_key, plex_folder))
    
    return eligible

def delete_episode(session_key, plex_folder, dry_run=False):
    """Delete an episode folder from Plex library."""
    if dry_run:
        print(f"  [DRY-RUN] Would delete: {plex_folder}")
        return True
    
    # Belt and braces: only ever remove a session folder, never a round or a
    # season. The caller matches the prefix, but a bad state file could name
    # anything, and rmtree does not ask questions.
    name = plex_folder.name
    if not re.match(r"^\d{2}R\d{2}E\d{2}", name):
        print(f"  ❌ Refusing to delete {plex_folder}: not a session folder name")
        return False

    try:
        shutil.rmtree(plex_folder)
        print(f"  🗑️  Deleted: {plex_folder}")
        return True
    except Exception as e:
        print(f"  ❌ Error deleting {plex_folder}: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(
        description="Randomly remove grabbed episodes for testing purposes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --count 5 --dry-run     Preview deletion of 5 random episodes
  %(prog)s --count 10              Delete 10 random episodes
  %(prog)s --count 5 --season 2025 Delete 5 episodes from 2025 season only
        """
    )
    parser.add_argument(
        '--count', '-c',
        type=int,
        required=True,
        help='Number of episodes to randomly select for deletion'
    )
    parser.add_argument(
        '--season', '-s',
        type=int,
        default=None,
        help='Filter to specific season (e.g., 2025)'
    )
    parser.add_argument(
        '--dry-run', '-d',
        action='store_true',
        help='Preview actions without making changes'
    )
    parser.add_argument(
        '--confirm',
        action='store_true',
        help='Required to actually delete anything. Without it the utility '
             'refuses to run, because it deletes media.'
    )

    args = parser.parse_args()

    if not args.dry_run and not args.confirm:
        print("Refusing to delete anything without --confirm.")
        print("This utility removes media from your library. Preview it first:")
        print(f"    python {Path(__file__).name} --count {args.count} --dry-run")
        print("Then re-run with --confirm if the list looks right.")
        sys.exit(2)
    
    print("=" * 60)
    print("  MotoGParr cleanup utility - DELETES MEDIA")
    print("=" * 60)
    print(f"  Library: {PLEX_LIBRARY_BASE}")

    if not PLEX_LIBRARY_BASE.is_dir():
        print(f"\n❌ Library path does not exist: {PLEX_LIBRARY_BASE}")
        print("   Set MOTOGP_LIBRARY_PATH, or check the volume is mounted.")
        print("   Nothing has been changed.")
        sys.exit(1)
    
    if args.dry_run:
        print("🔍 DRY-RUN MODE - No changes will be made\n")
    else:
        print("⚠️  LIVE MODE - Files will be deleted!\n")
    
    # Load current state
    print("📂 Loading grabber state...")
    state = load_grabber_state()
    grabbed_count = len(state.get("grabbed", {}))
    print(f"   Found {grabbed_count} grabbed episodes in state file")
    
    # Find eligible episodes (exist in both JSON and Plex)
    print(f"\n🔍 Scanning Plex library for matching episodes...")
    if args.season:
        print(f"   Filtering to season: {args.season}")
    
    eligible = get_eligible_episodes(state, args.season)
    print(f"   Found {len(eligible)} episodes in Plex library")
    
    if not eligible:
        print("\n❌ No eligible episodes found to delete.")
        print("   (Episodes must exist in both grabber_state.json and Plex library)")
        sys.exit(1)
    
    # Check if requested count exceeds available
    if args.count > len(eligible):
        print(f"\n⚠️  Requested {args.count} but only {len(eligible)} available.")
        print(f"   Will process all {len(eligible)} eligible episodes.")
        count = len(eligible)
    else:
        count = args.count
    
    # Randomly select episodes
    print(f"\n🎲 Randomly selecting {count} episodes...")
    selected = random.sample(eligible, count)
    
    # Display selection
    print(f"\n📋 Selected episodes for deletion:")
    print("-" * 60)
    for session_key, plex_folder in selected:
        folder_prefix = session_key_to_folder_prefix(session_key)
        print(f"   {session_key} -> {folder_prefix}")
        print(f"      {plex_folder}")
    print("-" * 60)
    
    # Confirmation (unless dry-run)
    if not args.dry_run:
        print(f"\n⚠️  This will DELETE {count} episode folders and update grabber_state.json")
        confirm = input("   Type 'yes' to confirm: ").strip().lower()
        if confirm != 'yes':
            print("\n❌ Cancelled by user.")
            sys.exit(0)
    
    # Perform deletions
    print(f"\n🗑️  Deleting episodes...")
    deleted_keys = []
    
    for session_key, plex_folder in selected:
        success = delete_episode(session_key, plex_folder, args.dry_run)
        if success:
            deleted_keys.append(session_key)
    
    # Update grabber_state.json
    print(f"\n📝 Updating grabber_state.json...")
    
    if args.dry_run:
        print(f"   [DRY-RUN] Would remove {len(deleted_keys)} entries from 'grabbed'")
        print(f"   [DRY-RUN] Would remove {len(deleted_keys)} entries from 'searched'")
    else:
        # Remove from grabbed
        for key in deleted_keys:
            if key in state.get("grabbed", {}):
                del state["grabbed"][key]
                print(f"   Removed from grabbed: {key}")
        
        # Also remove from searched so they can be re-searched
        for key in deleted_keys:
            if key in state.get("searched", {}):
                del state["searched"][key]
                print(f"   Removed from searched: {key}")
        
        save_grabber_state(state)
    
    # Summary
    print("\n" + "=" * 60)
    print("  Summary")
    print("=" * 60)
    print(f"   Episodes selected:  {count}")
    print(f"   Folders deleted:    {len(deleted_keys)}")
    print(f"   JSON entries removed: {len(deleted_keys)}")
    
    if args.dry_run:
        print(f"\n   [DRY-RUN] No actual changes were made.")
        print(f"   Run without --dry-run to apply changes.")
    else:
        print(f"\n   ✅ Cleanup complete!")
        print(f"   💡 You may want to run a Plex library scan to update metadata.")
    
    print("=" * 60)

if __name__ == "__main__":
    main()
