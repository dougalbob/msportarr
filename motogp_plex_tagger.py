#!/usr/bin/env python3
"""
MotoGP Plex Tagger Module
==========================

Handles MotoGP content tagging in Plex.
"""

import os
import sys
import re
import argparse
import time
from pathlib import Path
from plexapi.server import PlexServer

# Add script directory to path
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

# Bind this process to a series BEFORE importing anything that compiles one in.
# Every table in motogp_common is the bound series' vocabulary, so this has to
# happen first: --series (or MOTOGPARR_SERIES, which the scheduler sets for each
# subprocess it starts) decides it, and the default series answers when neither
# is given. See series/binding.py.
from series.binding import bind_from_argv  # noqa: E402
BOUND_SERIES = bind_from_argv()

# Import from motogp_common
from utils.plexlabels import apply_labels, ensure_collection_sort

from motogp_common import (
    setup_logging,
    get_logger,
    SessionType,
    RaceClass,
    load_calendar,
    get_round_by_number,
    parse_prefix,
    classify_session,
    detect_race_class,
    MOTOGP_LIBRARY_PATH,
    VENUE_COUNTRY_MAP,
    element_number,
)
#: This series' Plex section. The tagger runs inside the sorter's process, which
#: is already bound, so these resolve to the same series as the files being
#: tagged; the default-series accessors would file F1 media into MotoGP's
#: library, which Plex accepts and then matches against nothing.

from utils.config_loader import (
    get_plex_url,
    get_plex_token,
    get_plex_library_name,
    get_plex_library_id,
    translate_path_for_plex,
    plex_path_mapping_complete,
    get_plex_path_mapping,
    get as config_get,
)

from activity_emitter import emit_activity, ActivityStage, mark_complete

# Get module logger
logger = get_logger("plex_tagger")

# =============================================================================
# CONFIGURATION
# =============================================================================

# Plex configuration (from settings.yml or environment)
PLEX_URL = get_plex_url()
PLEX_TOKEN = get_plex_token()
PLEX_LIBRARY = get_plex_library_name(BOUND_SERIES)
PLEX_LIBRARY_ID = get_plex_library_id(BOUND_SERIES)

# Validate required settings
if not PLEX_URL or not PLEX_TOKEN:
    print("WARNING: Plex URL or Token not configured - tagging will fail")

# Library path
MOTOGP_BASE_PATH = MOTOGP_LIBRARY_PATH

# Config directory
CONFIG_DIR = SCRIPT_DIR / "config"

MAX_RETRIES = 6    
RETRY_DELAY = 20

_plex_connection = None

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

# Module logger - uses centralized logging from motogp_common
logger = get_logger("plex_tagger")


def host_to_plex_path(host_path):
    return host_path.replace('/mnt/user/data', '/data')


def get_plex_connection():
    global _plex_connection
    if _plex_connection is None:
        logger = get_logger()
        logger.info(f"Connecting to Plex at {PLEX_URL}...")
        try:
            _plex_connection = PlexServer(PLEX_URL, PLEX_TOKEN)
            logger.info(f"  ✅ Connected to: {_plex_connection.friendlyName}")
        except Exception as e:
            logger.error(f"❌ Failed to connect to Plex: {e}")
            raise
    return _plex_connection


def load_season_config(year):
    """Load season config using shared calendar loader."""
    return load_calendar(year)


def get_round_info(season_config, round_num):
    """Get round info using shared function."""
    return get_round_by_number(season_config, round_num)


def get_country_from_venue(venue):
    """Get country from venue name."""
    return VENUE_COUNTRY_MAP.get(venue, None)


def trigger_library_scan(plex, library_name):
    logger.info(f"Triggering Plex library scan for '{library_name}'...")
    try:
        plex.library.section(library_name).update()
        logger.info("  Scan triggered")
        return True
    except Exception as e:
        logger.error(f"  Failed to trigger scan: {e}")
        return False


def find_plex_item_by_path(plex, library_name, file_path, retries=MAX_RETRIES):
    library = plex.library.section(library_name)
    
    for attempt in range(retries):
        for video in library.all():
            for media in video.media:
                for part in media.parts:
                    if part.file == file_path:
                        return video
        
        if attempt < retries - 1:
            logger.info(f"  File not in Plex yet, waiting {RETRY_DELAY}s... ({attempt + 1}/{retries})")
            time.sleep(RETRY_DELAY)
            if attempt == 0:
                trigger_library_scan(plex, library_name)
    
    return None


def apply_tags_and_collection(video, year, round_num, session_type, round_info, dry_run=False):
    """Apply tags, collection, genre, and country to a MotoGP video."""
    
    session_name = session_type.display_name
    venue = round_info.get('venue', f'Round {round_num}')
    collection_name = f"{year} Round {round_num:02d} {venue}"
    
    # Build session tag
    session_tag = session_name
    
    # Determine genres
    genres_to_add = []
    
    # Season genre: "MotoGP 2025"
    genres_to_add.append(f"MotoGP {year}")
    
    # Feature race genre (for main race sessions)
    if session_type.is_feature_race:
        genres_to_add.append("MotoGP - Feature Race")
    
    # Country from venue
    country = get_country_from_venue(venue)
    
    # Get current metadata
    current_labels = [label.tag for label in video.labels]
    current_collections = [col.tag for col in video.collections]
    
    try:
        current_genres = [genre.tag for genre in video.genres]
    except:
        current_genres = []
    
    try:
        current_countries = [c.tag for c in video.countries]
    except:
        current_countries = []
    
    changes_made = []
    
    # Add session tag
    if session_tag not in current_labels:
        if not dry_run:
            video.addLabel(session_tag)
        changes_made.append(f"Tag: {session_tag}")
    
    # Add collection
    if collection_name not in current_collections:
        if not dry_run:
            video.addCollection(collection_name)
        changes_made.append(f"Collection: {collection_name}")

    # The collection name already sorts correctly ("2026 Round 04" < "2026 Round
    # 05") because the round is zero padded, so this is belt and braces: it keeps
    # the order intact if a collection is ever renamed or grouped differently.
    # The item's own server handle is reused rather than reconnecting per session.
    server = getattr(video, "_server", None)
    if server is not None:
        collection_sort = ensure_collection_sort(
            server, PLEX_LIBRARY, collection_name, year, round_num, dry_run
        )
        if collection_sort:
            changes_made.append(collection_sort)
    
    # Add genres
    for genre in genres_to_add:
        if genre not in current_genres:
            if not dry_run:
                try:
                    video.addGenre(genre)
                    changes_made.append(f"Genre: {genre}")
                except Exception as e:
                    logger.warning(f"  Could not add genre '{genre}': {e}")
    
    # Add country
    if country and country not in current_countries:
        if not dry_run:
            try:
                video.addCountry(country)
                changes_made.append(f"Country: {country}")
            except Exception as e:
                logger.warning(f"  Could not add country '{country}': {e}")
    
    if changes_made:
        logger.info(f"  {'[DRY-RUN] ' if dry_run else ''}Applied: {', '.join(changes_made)}")
    else:
        logger.info(f"  ℹ️  All metadata already present")
    
    return changes_made


# =============================================================================
# PUBLIC API
# =============================================================================


def _build_episode_key_from_parsed(year: int, round_num: int, session_type) -> str:
    """Build episode key for activity tracking."""
    return f"{year}_R{round_num:02d}_E{element_number(session_type, year):02d}"

def tag_single_file(file_path, dry_run=False, wait_for_plex=True):
    """Tag a single MotoGP file in Plex."""
    logger.info(f"=== MotoGP Plex Tagger: Processing ===")
    logger.info(f"  Path: {file_path}")
    
    filename = os.path.basename(file_path)
    folder_name = os.path.basename(os.path.dirname(file_path))
    
    # Parse prefix using shared function (try folder first, then filename)
    parsed = parse_prefix(folder_name)
    if not parsed:
        parsed = parse_prefix(filename)
    
    if not parsed:
        logger.warning(f"  ⚠️  No valid prefix in: {folder_name}")
        # Try to classify without prefix
        race_class = detect_race_class(folder_name, series=BOUND_SERIES)
        session_type = classify_session(folder_name, race_class,
                                        series=BOUND_SERIES)
        if session_type == SessionType.UNKNOWN:
            logger.error(f"  ❌ Could not determine session type")
            return False
        # Try to extract year from path
        year_match = re.search(r'/(\d{4})/', file_path)
        if year_match:
            year = int(year_match.group(1))
        else:
            year = 2025  # Default
        round_num = 1  # Default if can't determine
        logger.info(f"  Classified as: {session_type.display_name}")
    else:
        year, round_num, session_type = parsed
        logger.info(f"  Parsed: {year} R{round_num:02d} E{element_number(session_type, year):02d}")
        logger.info(f"  Session: {session_type.display_name}")
    
    # Load season config using shared function
    season_config = load_season_config(year)
    if not season_config:
        logger.warning(f"  ⚠️  No season config for {year}, using defaults")
        round_info = {'venue': f'Round {round_num}', 'round': round_num}
    else:
        # Get round info using shared function
        round_info = get_round_info(season_config, round_num)
        if not round_info:
            logger.warning(f"  ⚠️  No round info for Round {round_num}")
            round_info = {'venue': f'Round {round_num}', 'round': round_num}
    
    venue = round_info.get('venue', '')
    country = get_country_from_venue(venue)
    
    logger.info(f"  Venue: {venue}")
    if country:
        logger.info(f"  Country: {country}")
    
    try:
        plex = get_plex_connection()
    except Exception as e:
        logger.error(f"  ❌ Plex connection failed: {e}")
        return False
    
    trigger_library_scan(plex, PLEX_LIBRARY)
    
    # Emit METADATA activity
    episode_key = _build_episode_key_from_parsed(year, round_num, session_type)
    emit_activity(
        episode_key=episode_key,
        episode_name=(
            session_type.full_display_name
            if hasattr(session_type, "full_display_name")
            else str(session_type)
        ),
        stage=ActivityStage.METADATA,
        details="Scanning Plex library",
        round_info={"round": round_num, "venue": venue}
    )
    
    if wait_for_plex:
        logger.info(f"  Waiting for Plex to index...")
        time.sleep(5)
    
    retries = MAX_RETRIES if wait_for_plex else 1

    plex_path = translate_path_for_plex(file_path)

    # Matching is an exact string comparison against what Plex reports. When the
    # two containers see the same file under different prefixes and no mapping
    # is configured, this can never match - so say so before burning minutes on
    # retries that are guaranteed to fail.
    mapping = get_plex_path_mapping()
    if not plex_path_mapping_complete():
        logger.warning(
            "  Plex path mapping is not configured - matching by exact path is "
            "unlikely to work unless this container and Plex use identical paths."
        )
        logger.warning(
            "  Fix: set plex.path_mapping.container / .plex in settings.yml, or "
            "PLEX_PATH_CONTAINER / PLEX_PATH_PLEX on the container."
        )
    elif not str(file_path).startswith(mapping["container"]):
        logger.warning(
            f"  {file_path} is not under the configured container prefix "
            f"({mapping['container']}) - Plex paths will not match."
        )

    logger.info(f"  Plex path: {plex_path}")
    video = find_plex_item_by_path(plex, PLEX_LIBRARY, plex_path, retries=retries)

    if not video:
        logger.error(f"  ❌ File not found in Plex")
        if not plex_path_mapping_complete():
            logger.error(
                "  Most likely cause: no Plex path mapping, so Plex's path for "
                "this file is not the path searched above."
            )
        else:
            logger.error(
                "  Check the file is inside a folder the Plex library actually "
                "scans, and that the library name matches exactly."
            )
        return False
    
    logger.info(f"  ✅ Found in Plex: {video.title}")
    
    # Title and sort title are written together, before the tags: the visible
    # name has to say what the session is (FP2, not the old "Practice"), and the
    # sort key has to keep it in running order. See utils/plexlabels.py for why
    # doing only one of the two makes the library worse.
    label_changes = apply_labels(video, session_type, year, round_num, dry_run)
    for change in label_changes:
        logger.info(f"  {change}")

    changes = apply_tags_and_collection(
        video, year, round_num, session_type, round_info, dry_run
    )
    
    logger.info(f"=== MotoGP Plex Tagger: Complete ===")
    
    # Emit COMPLETE activity - episode is ready in Plex
    mark_complete(
        episode_key=episode_key,
        details="Ready in Plex"
    )
    
    return True


def tag_from_host_path(host_path, dry_run=False, wait_for_plex=True):
    """Tag a file using host path (converts to container path)."""
    container_path = host_to_plex_path(host_path)
    return tag_single_file(container_path, dry_run, wait_for_plex)


# =============================================================================
# FULL LIBRARY SCAN
# =============================================================================

def build_plex_index(plex, library_name):
    """Build an index of all files in the Plex library."""
    logger.info(f"Building Plex library index for '{library_name}'...")
    index = {}
    
    library = plex.library.section(library_name)
    for video in library.all():
        for media in video.media:
            for part in media.parts:
                if part.file:
                    index[part.file] = video
    
    logger.info(f"  Indexed {len(index)} files")
    return index


def scan_full_library(dry_run=False):
    """Scan and tag all MotoGP content in the library."""
    
    plex = get_plex_connection()
    plex_index = build_plex_index(plex, PLEX_LIBRARY)
    
    # Translate base path for comparison
    plex_base_path = translate_path_for_plex(MOTOGP_BASE_PATH)
    
    stats = {
        'scanned': 0, 'matched': 0, 'updated': 0,
        'skipped': 0, 'errors': 0, 'no_prefix': 0
    }
    
    season_configs = {}
    
    logger.info(f"\nScanning for MotoGP content...")
    logger.info(f"Base path: {plex_base_path}")
    logger.info(f"Dry run: {dry_run}")
    logger.info("=" * 70)
    
    for file_path, video in plex_index.items():
        # Only process MotoGP files
        if not file_path.startswith(plex_base_path):
            continue
        
        stats['scanned'] += 1
        filename = os.path.basename(file_path)
        folder_name = os.path.basename(os.path.dirname(file_path))
        
        # Parse prefix using shared function
        parsed = parse_prefix(folder_name)
        if not parsed:
            parsed = parse_prefix(filename)
        
        if not parsed:
            stats['no_prefix'] += 1
            continue
        
        year, round_num, session_type = parsed
        stats['matched'] += 1
        
        # Cache season configs
        if year not in season_configs:
            season_configs[year] = load_season_config(year)
        
        round_info = None
        if season_configs[year]:
            round_info = get_round_info(season_configs[year], round_num)
        
        if not round_info:
            round_info = {'venue': f'Round {round_num}', 'round': round_num}
        
        try:
            changes = apply_tags_and_collection(
                video, year, round_num, session_type, round_info, dry_run
            )
            
            if changes:
                stats['updated'] += 1
                venue = round_info.get('venue', '')
                logger.info(f"{'[DRY-RUN] ' if dry_run else ''}✅ {filename}")
                logger.info(f"    → MotoGP {year} R{round_num:02d} {venue} | {session_type.display_name}")
            else:
                stats['skipped'] += 1
                
        except Exception as e:
            logger.error(f"  ❌ Error processing {filename}: {e}")
            stats['errors'] += 1
    
    logger.info("\n" + "=" * 70)
    logger.info("SUMMARY")
    logger.info("=" * 70)
    logger.info(f"  Scanned:    {stats['scanned']}")
    logger.info(f"  Matched:    {stats['matched']}")
    logger.info(f"  Updated:    {stats['updated']}")
    logger.info(f"  Skipped:    {stats['skipped']}")
    logger.info(f"  No prefix:  {stats['no_prefix']}")
    logger.info(f"  Errors:     {stats['errors']}")
    
    if dry_run:
        logger.info("\n⚠️  DRY RUN - No changes made")
    
    return stats


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='MotoGP Plex Tagger')
    parser.add_argument('file_path', nargs='?', help='Single file (container path)')
    parser.add_argument('--host-path', help='Single file (host path)')
    parser.add_argument('--scan', '-s', action='store_true', help='Scan full library')
    parser.add_argument('--dry-run', '-n', action='store_true', help='Preview only')
    parser.add_argument('--no-wait', action='store_true', help="Don't wait for Plex")
    parser.add_argument('--list-sessions', '-l', action='store_true', 
                        help='List all session types')
    args = parser.parse_args()
    
    setup_logging(debug_mode=args.verbose if hasattr(args, 'verbose') else False)
    logger = get_logger("plex_tagger")
    logger.info("=" * 70)
    logger.info("MOTOGP PLEX TAGGER")
    logger.info("=" * 70)
    
    # List sessions option
    if args.list_sessions:
        print("\nMotoGP Session Types:")
        print("=" * 50)
        for session in SessionType:
            if session != SessionType.UNKNOWN:
                feature = "🏁" if session.is_feature_race else "  "
                print(f"  {feature} E{element_number(session, 2027):02d}. {session.display_name}")
        print(f"     E{int(SessionType.UNKNOWN):02d}. {SessionType.UNKNOWN.display_name}")
        return
    
    if args.scan:
        scan_full_library(args.dry_run)
        logger.info("\n✅ Done")
        return
    
    if args.host_path:
        success = tag_from_host_path(args.host_path, args.dry_run, not args.no_wait)
        sys.exit(0 if success else 1)
    elif args.file_path:
        success = tag_single_file(args.file_path, args.dry_run, not args.no_wait)
        sys.exit(0 if success else 1)
    else:
        parser.print_help()
        return
    
    logger.info("\n✅ Done")


if __name__ == "__main__":
    main()
