#!/usr/bin/env python3
"""
MotoGP Artwork Overlay Utility
===============================

Test and apply overlay changes to existing MotoGP media artwork.

Usage:
    # Test on a single folder (creates test_poster.jpg and test_fanart.jpg)
    motogp-overlay test "/media/Motorsport/MotoGP/2025/Round 1 Thailand/25R01E21 MotoGP Race"
    
    # Test with a specific scale factor
    motogp-overlay test "/path/to/folder" --scale 1.2
    
    # Apply to a single folder (overwrites existing artwork)
    motogp-overlay apply "/path/to/folder"
    
    # Apply to all sessions in a round
    motogp-overlay apply-round "/media/Motorsport/MotoGP/2025/Round 1 Thailand"
    
    # Apply to all sessions in a season
    motogp-overlay apply-season 2025
    
    # Apply to everything
    motogp-overlay apply-all
    
    # Dry run (show what would be changed)
    motogp-overlay apply-all --dry-run

Aliases (add to /boot/config/go):
    alias motogp-overlay='docker exec -it msportarr /opt/venv/bin/python3 /app/motogp_overlay_utility.py'
"""

import os
import sys
import argparse
import logging
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Tuple

from PIL import Image, ImageDraw, ImageFont
import textwrap

# Add script directory to path
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

# Bind this process to a series BEFORE importing anything that compiles one in.
# Every table in motogp_common is the bound series' vocabulary, so this has to
# happen first: --series (or MOTORSPORTARR_SERIES, which the WebUI sets for each
# child it starts) decides it, and the default series answers when neither is
# given. Without it the overlay would read F1's folders with MotoGP's element
# numbers and find "no session" for every one of them.
from series.binding import bind_from_argv  # noqa: E402
BOUND_SERIES = bind_from_argv()

from motogp_common import (
    parse_prefix,
    load_calendar,
    get_round_by_number,
    SessionType,
    RaceClass,
    CLASS_ASSET_DIRS,
)

from utils.config_loader import get_series_library_path

# =============================================================================
# CONFIGURATION
# =============================================================================

#: This series' library root. Resolved through the binding rather than the
#: default series' accessor, which is what ``MOTOGP_BASE`` used to mean.
MOTOGP_BASE = get_series_library_path(BOUND_SERIES)

# Default scale factor (can be overridden via command line or UI)
DEFAULT_SCALE_FACTOR = 1.0

# Target sizes for resize
POSTER_SIZE = (1000, 1500)
FANART_SIZE = (1920, 1080)

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# =============================================================================
# FONT HELPER
# =============================================================================

def get_font(size: int) -> ImageFont.FreeTypeFont:
    """Get font with fallback paths."""
    font_paths = [
        str(SCRIPT_DIR / "assets" / "DejaVuSans-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]
    
    for font_path in font_paths:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size)
            except OSError:
                continue
    
    logger.warning("No TrueType font found, using default")
    return ImageFont.load_default()

# =============================================================================
# OVERLAY FUNCTION (matches motogp_sort.py)
# =============================================================================

def overlay_text_on_image(
    image_path: str,
    session_text: str,
    round_info: str = None,
    venue: str = None,
    scale_factor: float = DEFAULT_SCALE_FACTOR
) -> Optional[Image.Image]:
    """
    Apply text overlay to an image.
    
    Args:
        image_path: Path to source image
        session_text: Session name (e.g., "MotoGP Race", "Moto2 Qualifying 1")
        round_info: Round string (e.g., "Round 1")
        venue: Venue name (e.g., "Thailand")
        scale_factor: Scale multiplier for text/background
    
    Returns:
        Modified PIL Image, or None on error
    """
    img = None
    overlay = None
    
    try:
        img = Image.open(image_path)
        img.load()
        
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
        
        is_portrait = img.height > img.width
        
        # Font size calculation
        if is_portrait:
            header_font_size = int(img.width * 0.07 * scale_factor)
            session_font_size = int(img.width * 0.06 * scale_factor)
            wrap_width = 12
        else:
            header_font_size = int(img.width * 0.045 * scale_factor)
            session_font_size = int(img.width * 0.05 * scale_factor)
            wrap_width = 20
        
        header_font_size = max(header_font_size, 24)
        session_font_size = max(session_font_size, 20)
        
        header_font = get_font(header_font_size)
        session_font = get_font(session_font_size)
        
        draw = ImageDraw.Draw(img)
        
        # Build text lines
        header_lines = []
        if round_info:
            header_lines.append(round_info)
        if venue:
            header_lines.append(venue)
        
        session_lines = textwrap.wrap(session_text, width=wrap_width)
        
        # Calculate dimensions
        header_line_height = header_font.getbbox("A")[3]
        session_line_height = session_font.getbbox("A")[3]
        
        header_spacing = int(header_line_height * 0.3)
        session_spacing = int(session_line_height * 0.4)
        section_gap = int(session_line_height * 1.5)
        
        header_total = (header_line_height + header_spacing) * len(header_lines) if header_lines else 0
        session_total = (session_line_height + session_spacing) * len(session_lines)
        
        total_height = header_total + (section_gap if header_lines else 0) + session_total
        y_start = int(img.height * 0.5) - (total_height // 2)
        
        # Calculate max line width
        max_line_width = 0
        for line in header_lines:
            bbox = draw.textbbox((0, 0), line, font=header_font)
            max_line_width = max(max_line_width, bbox[2] - bbox[0])
        for line in session_lines:
            bbox = draw.textbbox((0, 0), line, font=session_font)
            max_line_width = max(max_line_width, bbox[2] - bbox[0])
        
        # Background
        padding_x = int(img.width * 0.03 * scale_factor)
        padding_y = int(session_line_height * 0.4 * scale_factor)
        background_alpha = 179
        
        bg_x1 = max(0, (img.width - max_line_width) // 2 - padding_x)
        bg_y1 = max(0, y_start - padding_y)
        bg_x2 = min(img.width, (img.width + max_line_width) // 2 + padding_x)
        bg_y2 = min(img.height, y_start + total_height + padding_y)
        
        overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle([bg_x1, bg_y1, bg_x2, bg_y2], fill=(0, 0, 0, background_alpha))
        
        img = Image.alpha_composite(img, overlay)
        overlay.close()
        overlay = None
        
        draw = ImageDraw.Draw(img)
        stroke_width = max(1, int(2 * scale_factor))
        
        # Draw header
        current_y = y_start
        for line in header_lines:
            bbox = draw.textbbox((0, 0), line, font=header_font)
            x = (img.width - (bbox[2] - bbox[0])) // 2
            draw.text((x, current_y), line, font=header_font, fill="white",
                      stroke_width=stroke_width, stroke_fill="black")
            current_y += header_line_height + header_spacing
        
        if header_lines:
            current_y += section_gap - header_spacing
        
        # Draw session
        for line in session_lines:
            bbox = draw.textbbox((0, 0), line, font=session_font)
            x = (img.width - (bbox[2] - bbox[0])) // 2
            draw.text((x, current_y), line, font=session_font, fill="white",
                      stroke_width=stroke_width, stroke_fill="black")
            current_y += session_line_height + session_spacing
        
        result = img.convert('RGB')
        img.close()
        return result
        
    except Exception as e:
        logger.error(f"Error overlaying text: {e}")
        if img:
            img.close()
        if overlay:
            overlay.close()
        return None


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def parse_session_folder(folder_path: str) -> Optional[Tuple[int, int, SessionType, str, RaceClass]]:
    """
    Parse a session folder to extract metadata.
    
    Returns: (year, round_num, session_type, venue, race_class) or None
    """
    folder_name = os.path.basename(folder_path)
    parsed = parse_prefix(folder_name)
    
    if not parsed:
        logger.warning(f"Could not parse prefix from: {folder_name}")
        return None
    
    year, round_num, session_type = parsed
    race_class = session_type.race_class
    
    # Load calendar to get venue
    calendar = load_calendar(year)
    if not calendar:
        logger.warning(f"No calendar for {year}")
        return None
    
    round_info = get_round_by_number(calendar, round_num)
    if not round_info:
        logger.warning(f"No round info for Round {round_num}")
        return None
    
    venue = round_info.get("venue", "Unknown")
    
    return (year, round_num, session_type, venue, race_class)


def get_source_artwork(year: int, round_num: int, race_class: RaceClass) -> Tuple[Optional[Path], Optional[Path]]:
    """Get source fanart and poster paths for a round and class."""
    calendar = load_calendar(year)
    if not calendar:
        return None, None
    
    round_info = get_round_by_number(calendar, round_num)
    if not round_info:
        return None, None
    
    # Get class-specific assets
    class_key = race_class.folder_key if race_class else "motogp"
    assets = round_info.get("assets", {}).get(class_key, {})
    asset_dirs = CLASS_ASSET_DIRS.get(class_key, CLASS_ASSET_DIRS["motogp"])
    
    fanart_file = assets.get("fanart")
    poster_file = assets.get("poster")
    
    fanart_path = asset_dirs["fanart"] / fanart_file if fanart_file else None
    poster_path = asset_dirs["poster"] / poster_file if poster_file else None
    
    if fanart_path and not fanart_path.exists():
        logger.debug(f"Fanart not found: {fanart_path}")
        fanart_path = None
    if poster_path and not poster_path.exists():
        logger.debug(f"Poster not found: {poster_path}")
        poster_path = None
    
    return fanart_path, poster_path


def process_single_folder(
    folder_path: str,
    scale_factor: float = DEFAULT_SCALE_FACTOR,
    test_mode: bool = False,
    dry_run: bool = False
) -> bool:
    """
    Process a single session folder.
    
    Args:
        folder_path: Path to session folder
        scale_factor: Scale multiplier
        test_mode: If True, create test_*.jpg instead of overwriting
        dry_run: If True, just log what would happen
    
    Returns: True if successful
    """
    parsed = parse_session_folder(folder_path)
    if not parsed:
        return False
    
    year, round_num, session_type, venue, race_class = parsed
    session_name = session_type.full_display_name
    calendar = load_calendar(year) or {}
    round_info = get_round_by_number(calendar, round_num) or {}
    round_num = round_info.get("round", round_num)
    round_text = f"Round {round_num:02d}"
    
    logger.info(f"Processing: {year} {round_text} {venue} - {session_name}")
    logger.info(f"  Class: {race_class.display_name if race_class else 'Unknown'}")
    logger.info(f"  Scale factor: {scale_factor}")
    
    if dry_run:
        logger.info(f"  [DRY RUN] Would regenerate artwork")
        return True
    
    # Get source artwork for this class
    source_fanart, source_poster = get_source_artwork(year, round_num, race_class)
    
    if not source_fanart and not source_poster:
        logger.warning(f"  No source artwork found for {race_class.display_name if race_class else 'class'}")
        return False
    
    success = True
    
    # Process poster
    if source_poster:
        if test_mode:
            output_path = os.path.join(folder_path, "test_poster.jpg")
        else:
            output_path = os.path.join(folder_path, "poster.jpg")
        
        try:
            temp_path = os.path.join(folder_path, "_temp_poster.jpg")
            shutil.copy2(source_poster, temp_path)
            
            img = Image.open(temp_path)
            if img.mode in ('RGBA', 'P', 'LA'):
                img = img.convert('RGB')
            if img.size != POSTER_SIZE:
                img = img.resize(POSTER_SIZE, Image.LANCZOS)
            img.save(temp_path, "JPEG", quality=95)
            img.close()
            
            result = overlay_text_on_image(temp_path, session_name, round_text, venue, scale_factor)
            if result:
                result.save(output_path, "JPEG", quality=95)
                result.close()
                logger.info(f"  ✅ Poster saved: {output_path}")
            else:
                success = False
            
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception as e:
            logger.error(f"  ❌ Poster error: {e}")
            success = False
    
    # Process fanart
    if source_fanart:
        if test_mode:
            output_path = os.path.join(folder_path, "test_fanart.jpg")
        else:
            output_path = os.path.join(folder_path, "fanart.jpg")
        
        try:
            temp_path = os.path.join(folder_path, "_temp_fanart.jpg")
            shutil.copy2(source_fanart, temp_path)
            
            img = Image.open(temp_path)
            if img.mode in ('RGBA', 'P', 'LA'):
                img = img.convert('RGB')
            if img.size != FANART_SIZE:
                img = img.resize(FANART_SIZE, Image.LANCZOS)
            img.save(temp_path, "JPEG", quality=95)
            img.close()
            
            result = overlay_text_on_image(temp_path, session_name, round_text, venue, scale_factor)
            if result:
                result.save(output_path, "JPEG", quality=95)
                result.close()
                logger.info(f"  ✅ Fanart saved: {output_path}")
                
                # Also update thumb if not test mode
                if not test_mode:
                    thumb_path = os.path.join(folder_path, "thumb.jpg")
                    shutil.copy2(output_path, thumb_path)
                    logger.info(f"  ✅ Thumb updated")
            else:
                success = False
            
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception as e:
            logger.error(f"  ❌ Fanart error: {e}")
            success = False
    
    return success


def find_session_folders(base_path: str) -> List[str]:
    """Find all session folders under a path."""
    folders = []
    
    if not os.path.isdir(base_path):
        return folders
    
    for item in os.listdir(base_path):
        item_path = os.path.join(base_path, item)
        if os.path.isdir(item_path):
            # Check if it's a session folder (has prefix)
            if parse_prefix(item):
                folders.append(item_path)
            else:
                # Might be a round or season folder, recurse
                folders.extend(find_session_folders(item_path))
    
    return folders


def cleanup_test_files(folder_path: str):
    """Remove test_*.jpg files from a folder."""
    for f in ["test_poster.jpg", "test_fanart.jpg"]:
        path = os.path.join(folder_path, f)
        if os.path.exists(path):
            os.remove(path)
            logger.info(f"Removed: {path}")


# =============================================================================
# COMMAND HANDLERS
# =============================================================================

def cmd_test(args):
    """Test overlay on a single folder."""
    folder_path = args.folder
    
    if not os.path.isdir(folder_path):
        logger.error(f"Folder not found: {folder_path}")
        return 1
    
    cleanup_test_files(folder_path)
    
    success = process_single_folder(
        folder_path,
        scale_factor=args.scale,
        test_mode=True,
        dry_run=False
    )
    
    if success:
        logger.info("")
        logger.info("=" * 60)
        logger.info("TEST FILES CREATED:")
        logger.info(f"  {os.path.join(folder_path, 'test_poster.jpg')}")
        logger.info(f"  {os.path.join(folder_path, 'test_fanart.jpg')}")
        logger.info("")
        logger.info("View these files to check the overlay appearance.")
        logger.info("Adjust --scale and run again until satisfied.")
        logger.info("")
        logger.info("When ready, run:")
        logger.info(f"  motogp-overlay apply \"{folder_path}\" --scale {args.scale}")
        logger.info("=" * 60)
        return 0
    
    return 1


def cmd_apply(args):
    """Apply overlay to a single folder."""
    folder_path = args.folder
    
    if not os.path.isdir(folder_path):
        logger.error(f"Folder not found: {folder_path}")
        return 1
    
    cleanup_test_files(folder_path)
    
    success = process_single_folder(
        folder_path,
        scale_factor=args.scale,
        test_mode=False,
        dry_run=args.dry_run
    )
    
    return 0 if success else 1


def cmd_apply_round(args):
    """Apply overlay to all sessions in a round."""
    round_path = args.folder
    
    if not os.path.isdir(round_path):
        logger.error(f"Round folder not found: {round_path}")
        return 1
    
    folders = find_session_folders(round_path)
    logger.info(f"Found {len(folders)} session folders in {round_path}")
    
    success_count = 0
    for folder in sorted(folders):
        cleanup_test_files(folder)
        if process_single_folder(folder, args.scale, test_mode=False, dry_run=args.dry_run):
            success_count += 1
    
    logger.info(f"\nProcessed {success_count}/{len(folders)} folders successfully")
    return 0 if success_count == len(folders) else 1


def cmd_apply_season(args):
    """Apply overlay to all sessions in a season."""
    season_path = os.path.join(MOTOGP_BASE, str(args.year))
    
    if not os.path.isdir(season_path):
        logger.error(f"Season folder not found: {season_path}")
        return 1
    
    folders = find_session_folders(season_path)
    logger.info(f"Found {len(folders)} session folders in {args.year} season")
    
    success_count = 0
    for folder in sorted(folders):
        cleanup_test_files(folder)
        if process_single_folder(folder, args.scale, test_mode=False, dry_run=args.dry_run):
            success_count += 1
    
    logger.info(f"\nProcessed {success_count}/{len(folders)} folders successfully")
    return 0 if success_count == len(folders) else 1


def cmd_apply_all(args):
    """Apply overlay to all sessions in all seasons."""
    if not os.path.isdir(MOTOGP_BASE):
        logger.error(f"MotoGP base folder not found: {MOTOGP_BASE}")
        return 1
    
    folders = find_session_folders(MOTOGP_BASE)
    logger.info(f"Found {len(folders)} session folders total")
    
    if not args.dry_run:
        confirm = input(f"Apply overlay to {len(folders)} folders? [y/N]: ")
        if confirm.lower() != 'y':
            logger.info("Aborted")
            return 0
    
    success_count = 0
    for folder in sorted(folders):
        cleanup_test_files(folder)
        if process_single_folder(folder, args.scale, test_mode=False, dry_run=args.dry_run):
            success_count += 1
    
    logger.info(f"\nProcessed {success_count}/{len(folders)} folders successfully")
    return 0 if success_count == len(folders) else 1


def cmd_clean(args):
    """Remove test files from a folder or all folders."""
    if args.folder:
        cleanup_test_files(args.folder)
    else:
        folders = find_session_folders(MOTOGP_BASE)
        for folder in folders:
            cleanup_test_files(folder)
        logger.info(f"Cleaned {len(folders)} folders")
    return 0


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="MotoGP Artwork Overlay Utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test overlay with default scale
  motogp-overlay test "/media/Motorsport/MotoGP/2025/Round 1 Thailand/25R01E21 MotoGP Race"
  
  # Test with different scale
  motogp-overlay test "/path/to/folder" --scale 1.2
  
  # Apply to single folder
  motogp-overlay apply "/path/to/folder" --scale 1.0
  
  # Apply to entire round
  motogp-overlay apply-round "/media/Motorsport/MotoGP/2025/Round 1 Thailand"
  
  # Apply to entire season
  motogp-overlay apply-season 2025
  
  # Apply to everything (with confirmation)
  motogp-overlay apply-all
  
  # Dry run
  motogp-overlay apply-all --dry-run
  
  # Clean up test files
  motogp-overlay clean
        """
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command")
    
    # Test command
    test_parser = subparsers.add_parser("test", help="Test overlay on a single folder")
    test_parser.add_argument("folder", help="Path to session folder")
    test_parser.add_argument("--scale", type=float, default=DEFAULT_SCALE_FACTOR,
                             help=f"Scale factor (default: {DEFAULT_SCALE_FACTOR})")
    
    # Apply command
    apply_parser = subparsers.add_parser("apply", help="Apply overlay to a single folder")
    apply_parser.add_argument("folder", help="Path to session folder")
    apply_parser.add_argument("--scale", type=float, default=DEFAULT_SCALE_FACTOR,
                              help=f"Scale factor (default: {DEFAULT_SCALE_FACTOR})")
    apply_parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    
    # Apply-round command
    round_parser = subparsers.add_parser("apply-round", help="Apply overlay to all sessions in a round")
    round_parser.add_argument("folder", help="Path to round folder")
    round_parser.add_argument("--scale", type=float, default=DEFAULT_SCALE_FACTOR,
                              help=f"Scale factor (default: {DEFAULT_SCALE_FACTOR})")
    round_parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    
    # Apply-season command
    season_parser = subparsers.add_parser("apply-season", help="Apply overlay to all sessions in a season")
    season_parser.add_argument("year", type=int, help="Season year")
    season_parser.add_argument("--scale", type=float, default=DEFAULT_SCALE_FACTOR,
                               help=f"Scale factor (default: {DEFAULT_SCALE_FACTOR})")
    season_parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    
    # Apply-all command
    all_parser = subparsers.add_parser("apply-all", help="Apply overlay to all sessions")
    all_parser.add_argument("--scale", type=float, default=DEFAULT_SCALE_FACTOR,
                            help=f"Scale factor (default: {DEFAULT_SCALE_FACTOR})")
    all_parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    
    # Clean command
    clean_parser = subparsers.add_parser("clean", help="Remove test files")
    clean_parser.add_argument("folder", nargs="?", help="Folder to clean (default: all)")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    commands = {
        "test": cmd_test,
        "apply": cmd_apply,
        "apply-round": cmd_apply_round,
        "apply-season": cmd_apply_season,
        "apply-all": cmd_apply_all,
        "clean": cmd_clean,
    }
    
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())