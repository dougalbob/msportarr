#!/usr/bin/env python3
"""
MotoGP Sort - Standalone File Organization and Artwork
========================================================

Sorts MotoGP downloads into the Plex library structure and applies artwork.
Supports MotoGP, Moto2, and Moto3 classes.

Usage:
    python3 motogp_sort.py --source /path/to/folder
    python3 motogp_sort.py --source /path/to/folder --dry-run
"""

import os
import sys
import shutil
import re
import time
import urllib.request
import argparse
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple
from PIL import Image, ImageDraw, ImageFont

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

# Import centralized logging FIRST
from motogp_common import (
    setup_logging, get_logger,
    SessionType, RaceClass,
    build_prefix, parse_prefix,
    load_calendar, get_round_by_number, identify_round,
    classify_session, detect_race_class,
    CLASS_ASSET_DIRS, get_library_path, get_config_dir,
)

# Import config
from utils.config_loader import (
    get_plex_url, get_plex_token,
    get_plex_library_id, get_plex_library_name,
    get_motogp_library_path,
)

# Try to import Discord notifications
try:
    from motogp_discord import notify_session_added, should_notify
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False

# =============================================================================
# LOGGING SETUP
# =============================================================================

# Get module logger - will be configured by setup_logging() call
logger = get_logger("sort")

# =============================================================================
# CONFIGURATION
# =============================================================================

# Plex configuration
PLEX_URL = get_plex_url()
PLEX_TOKEN = get_plex_token()
PLEX_LIBRARY_NAME = get_plex_library_name()
PLEX_LIBRARY_ID = get_plex_library_id()

# Library path
MOTOGP_LIBRARY_PATH = get_motogp_library_path()

#: Where a replaced session folder is parked: ``<library>/.mismatched/<season>/``.
#: Shared with utils/release_mismatches.py so the app and the sorter agree on
#: which folders are "out of the library but still on disk".
QUARANTINE_DIRNAME = ".mismatched"

# Supported video extensions
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.m4v', '.ts', '.m2ts'}

# Tag pattern for cleanup
TAG_PATTERN = re.compile(
    r"(?:1080p|720p|2160p|WEB[-]?DL|WEB|webrip|bluray|h264|x264|x265|H265|hevc|"
    r"hdr|dv|dd5[._]1|aac|dts|atmos|remux|nf|amzn|verum|Repack|Proper|PlayTV|RBB|"
    r"Multi[-]?MWR|English[-]?scmcgill1969|English[-]?MWR|TNT|Sky|"
    r"AHDTV|AHDTVDARKSPORT|DARKSPORT|HDTV|JFF|RiP|Round|BILLIE|"
    r"Upscaled|Dorna|BT[-]?Sport|ServusTV)",
    re.IGNORECASE
)

# Global state
DRY_RUN = False
changes = set()


def init_logging(dry_run: bool = False, debug: bool = False):
    """Initialize logging for sort script."""
    global DRY_RUN
    DRY_RUN = dry_run
    
    # Setup centralized logging
    setup_logging(debug_mode=debug)
    
    if dry_run:
        logger.info("DRY RUN MODE - no changes will be made")


# =============================================================================
# PLEX TAGGER INTEGRATION
# =============================================================================

PLEX_TAGGER_AVAILABLE = False
tag_single_file = None

def init_plex_tagger():
    """Initialize the Plex tagger module."""
    global PLEX_TAGGER_AVAILABLE, tag_single_file
    try:
        from motogp_plex_tagger import tag_single_file as tagger_func
        tag_single_file = tagger_func
        PLEX_TAGGER_AVAILABLE = True
        logger.debug("MotoGP Plex tagger loaded")
    except ImportError as e:
        logger.warning(f"MotoGP Plex tagger not available: {e}")


def apply_plex_tags(final_path: str) -> bool:
    """Apply Plex tags to media file in folder."""
    if not PLEX_TAGGER_AVAILABLE:
        return False
    
    # Find media file
    media_file = None
    for f in os.listdir(final_path):
        if any(f.lower().endswith(ext) for ext in VIDEO_EXTENSIONS):
            media_file = os.path.join(final_path, f)
            break
    
    if not media_file:
        logger.warning(f"No media file found in {final_path}")
        return False
    
    logger.info(f"Applying Plex tags to: {os.path.basename(media_file)}")
    
    try:
        return tag_single_file(media_file, dry_run=DRY_RUN, wait_for_plex=True)
    except Exception as e:
        logger.error(f"Plex tagging failed: {e}")
        return False


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def is_video_file(filename: str) -> bool:
    """Check if file is a supported video format."""
    _, ext = os.path.splitext(filename)
    return ext.lower() in VIDEO_EXTENSIONS


def normalize_name(name: str) -> str:
    """Normalize name by removing dots and extra whitespace."""
    clean = name.lstrip(".")
    clean = clean.replace(".", " ")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def extract_year(folder_name: str) -> Optional[int]:
    """Extract 4-digit year from folder name."""
    match = re.search(r"\b(20\d{2})\b", folder_name)
    return int(match.group(1)) if match else None


def get_video_files(folder_path: str) -> List[str]:
    """Get list of video files in folder (recursively)."""
    videos = []
    for root, _, files in os.walk(folder_path):
        for f in files:
            if is_video_file(f):
                videos.append(os.path.join(root, f))
    return sorted(videos)


def count_video_files(folder_path: str) -> int:
    """Count video files in folder."""
    return len(get_video_files(folder_path))


# =============================================================================
# CLEANUP FUNCTIONS
# =============================================================================

def delete_unwanted(base_dir: str, name: str):
    """Delete files/folders starting with name."""
    for root, dirs, files in os.walk(base_dir, topdown=False):
        for f in files:
            if f.lower().startswith(name.lower()):
                path = os.path.join(root, f)
                if not DRY_RUN:
                    os.remove(path)
                logger.debug(f"Deleted file: {path}")
                changes.add(root)
        for d in dirs:
            if d.lower().startswith(name.lower()):
                path = os.path.join(root, d)
                if not DRY_RUN:
                    shutil.rmtree(path)
                logger.debug(f"Deleted folder: {path}")
                changes.add(root)


def clean_filenames(base_dir: str):
    """Remove release tags from video filenames."""
    for root, _, files in os.walk(base_dir):
        for f in files:
            if is_video_file(f):
                name, ext = os.path.splitext(f)
                cleaned = TAG_PATTERN.sub("", name)
                cleaned = re.sub(r"[-.]+$", "", cleaned).strip()
                newname = f"{cleaned}{ext}"
                if f != newname:
                    oldpath = os.path.join(root, f)
                    newpath = os.path.join(root, newname)
                    if not os.path.exists(newpath) and not DRY_RUN:
                        shutil.move(oldpath, newpath)
                        logger.debug(f"Renamed: {f} → {newname}")
                        changes.add(root)


def clean_foldernames(base_dir: str):
    """Remove release tags from folder names."""
    for d in os.listdir(base_dir):
        fullpath = os.path.join(base_dir, d)
        if not os.path.isdir(fullpath):
            continue
        cleaned = TAG_PATTERN.sub("", d)
        cleaned = re.sub(r"[-.]+$", "", cleaned).strip()
        if d != cleaned:
            newpath = os.path.join(base_dir, cleaned)
            if not os.path.exists(newpath) and not DRY_RUN:
                shutil.move(fullpath, newpath)
                logger.debug(f"Renamed folder: {d} → {cleaned}")
                changes.add(base_dir)


def cleanup_folder(job_path: str) -> str:
    """Clean up download folder."""
    logger.debug("Cleanup started")
    parent_dir = os.path.dirname(job_path)
    folder_name = os.path.basename(job_path)
    
    safe_name = normalize_name(folder_name)
    safe_name = TAG_PATTERN.sub("", safe_name)
    safe_name = re.sub(r"[-.]+$", "", safe_name).strip()
    
    new_path = os.path.join(parent_dir, safe_name)
    if job_path != new_path and not DRY_RUN:
        os.rename(job_path, new_path)
        job_path = new_path
    
    delete_unwanted(job_path, "sample")
    delete_unwanted(job_path, "screens")
    clean_foldernames(job_path)
    clean_filenames(job_path)
    
    logger.debug("Cleanup finished")
    return job_path


def flatten_if_single_subdir(path: str):
    """Flatten folder if it contains only a single subfolder."""
    subdirs = [d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d))]
    files = [f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))]
    
    if len(subdirs) == 1 and not files:
        subdir_path = os.path.join(path, subdirs[0])
        for name in os.listdir(subdir_path):
            src = os.path.join(subdir_path, name)
            dst = os.path.join(path, name)
            if not DRY_RUN:
                shutil.move(src, dst)
        if not DRY_RUN:
            os.rmdir(subdir_path)
        logger.debug(f"Flattened: {subdir_path}")


# =============================================================================
# ARTWORK FUNCTIONS
# =============================================================================

def get_font(size: int) -> ImageFont.FreeTypeFont:
    """Get font with fallback paths."""
    font_paths = [
        str(SCRIPT_DIR / "assets" / "DejaVuSans-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ]
    
    for font_path in font_paths:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size)
            except OSError:
                continue
    
    return ImageFont.load_default()


def overlay_text_on_image(
    image_path: str, 
    session_text: str, 
    round_info: str = None, 
    venue: str = None
) -> Optional[Image.Image]:
    """Create image with text overlay."""
    try:
        img = Image.open(image_path)
        img.load()
        
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
        
        is_portrait = img.height > img.width
        
        if is_portrait:
            header_font_size = max(50, min(80, int(img.width * 0.07)))
            session_font_size = max(36, min(60, int(img.width * 0.06)))
            wrap_width = 12
            max_width_ratio = 0.85
        else:
            header_font_size = max(60, min(90, int(img.width * 0.045)))
            session_font_size = max(50, min(80, int(img.width * 0.05)))
            wrap_width = 20
            max_width_ratio = 0.70
        
        header_font = get_font(header_font_size)
        session_font = get_font(session_font_size)
        
        draw = ImageDraw.Draw(img)
        
        # Build lines
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
        
        # Draw background
        padding_x = int(img.width * 0.05)
        padding_y = int(session_line_height * 0.5)
        
        bg_x1 = max(0, (img.width - max_line_width) // 2 - padding_x)
        bg_y1 = max(0, y_start - padding_y)
        bg_x2 = min(img.width, (img.width + max_line_width) // 2 + padding_x)
        bg_y2 = min(img.height, y_start + total_height + padding_y)
        
        overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle([bg_x1, bg_y1, bg_x2, bg_y2], fill=(0, 0, 0, 179))
        
        img = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)
        
        # Draw text
        current_y = y_start
        
        for line in header_lines:
            bbox = draw.textbbox((0, 0), line, font=header_font)
            x = (img.width - (bbox[2] - bbox[0])) // 2
            draw.text((x, current_y), line, font=header_font, 
                     fill="white", stroke_width=2, stroke_fill="black")
            current_y += header_line_height + header_spacing
        
        if header_lines:
            current_y += section_gap - header_spacing
        
        for line in session_lines:
            bbox = draw.textbbox((0, 0), line, font=session_font)
            x = (img.width - (bbox[2] - bbox[0])) // 2
            draw.text((x, current_y), line, font=session_font,
                     fill="white", stroke_width=2, stroke_fill="black")
            current_y += session_line_height + session_spacing
        
        return img.convert('RGB')
        
    except Exception as e:
        logger.error(f"Error overlaying text: {e}")
        return None


def apply_round_artwork(
    final_path: str, 
    round_info: dict, 
    session_type: SessionType
):
    """Generate poster and fanart with overlays."""
    race_class = session_type.race_class
    if not race_class:
        return
    
    class_key = race_class.folder_key
    assets = round_info.get("assets", {}).get(class_key, {})
    asset_dirs = CLASS_ASSET_DIRS.get(class_key, CLASS_ASSET_DIRS["motogp"])
    
    round_num = round_info.get("round", 0)
    venue = round_info.get("venue", "")
    round_text = f"Round {round_num}" if round_num else None
    session_text = session_type.full_display_name
    
    # Fanart
    fanart_file = assets.get("fanart")
    if fanart_file:
        src = asset_dirs["fanart"] / fanart_file
        if src.exists():
            result = overlay_text_on_image(str(src), session_text, round_text, venue)
            if result and not DRY_RUN:
                out_path = os.path.join(final_path, "fanart.jpg")
                result.save(out_path, "JPEG", quality=95)
                logger.debug(f"Created fanart: {out_path}")
    
    # Poster
    poster_file = assets.get("poster")
    if poster_file:
        src = asset_dirs["poster"] / poster_file
        if src.exists():
            result = overlay_text_on_image(str(src), session_text, round_text, venue)
            if result and not DRY_RUN:
                out_path = os.path.join(final_path, "poster.jpg")
                result.save(out_path, "JPEG", quality=95)
                logger.debug(f"Created poster: {out_path}")


def resize_image(file_path: str, target_size: tuple) -> bool:
    """
    Resize an image into ``target_size``, preserving its aspect ratio.

    The first version resized to the exact dimensions, which is right only when
    the source already has that shape. It was invisible for years because every
    MotoGP artwork file *is* 1000x1500 or 1920x1080 (the venue posters are
    symlinks to one such pair). F1's are not: its 25 fanarts are 1376x768 and
    four of its posters are 0.79 rather than 2:3, so the exact resize would
    stretch them - and a stretched poster is not an error anywhere, it is just
    wrong on screen.

    So the image is scaled to *cover* the target (full width and height, no
    empty bars), keeping its own proportions, and the overflow is cropped
    centrally. Geometry that already matches is a single pass with no crop.
    """
    if not os.path.exists(file_path):
        return False
    
    if DRY_RUN:
        logger.debug(f"[DRY-RUN] Would resize: {file_path}")
        return True
    
    try:
        img = Image.open(file_path)
        if img.size == target_size:
            img.close()
            return True

        target_w, target_h = target_size
        source_w, source_h = img.size

        # Scale so the smaller relative dimension fills the target, then crop
        # the overhang. `max` is what makes it "cover" rather than "fit" - with
        # `min` the image would fit inside and need letterboxing.
        scale = max(target_w / source_w, target_h / source_h)
        scaled = (max(target_w, round(source_w * scale)),
                  max(target_h, round(source_h * scale)))
        resample = Image.BILINEAR if img.width > 4000 else Image.LANCZOS
        resized = img.resize(scaled, resample)
        img.close()

        left = (scaled[0] - target_w) // 2
        top = (scaled[1] - target_h) // 2
        cropped = resized.crop((left, top, left + target_w, top + target_h))
        resized.close()

        cropped.save(file_path, "JPEG", quality=95)
        cropped.close()
        
        logger.debug(
            f"Resized: {os.path.basename(file_path)} from {source_w}x{source_h} "
            f"to {target_size}"
        )
        return True
    except Exception as e:
        logger.error(f"Error resizing {file_path}: {e}")
        return False


def generate_thumb(folder_path: str) -> bool:
    """Generate thumb.jpg from fanart.jpg."""
    fanart = os.path.join(folder_path, "fanart.jpg")
    thumb = os.path.join(folder_path, "thumb.jpg")
    
    if not os.path.exists(fanart):
        return False
    
    if DRY_RUN:
        logger.debug(f"[DRY-RUN] Would create thumb")
        return True
    
    try:
        shutil.copy2(fanart, thumb)
        logger.debug(f"Created thumb.jpg")
        return True
    except Exception as e:
        logger.error(f"Error creating thumb: {e}")
        return False


def apply_all_artwork(final_path: str, round_info: dict, session_type: SessionType):
    """Apply all artwork to session folder."""
    if DRY_RUN:
        logger.debug("[DRY-RUN] Would apply artwork")
        return
    
    apply_round_artwork(final_path, round_info, session_type)
    resize_image(os.path.join(final_path, "poster.jpg"), (1000, 1500))
    resize_image(os.path.join(final_path, "fanart.jpg"), (1920, 1080))
    generate_thumb(final_path)


# =============================================================================
# FILE/FOLDER MANAGEMENT
# =============================================================================

def rename_files_to_match_folder(folder_path: str):
    """Rename video files to match folder name."""
    folder_name = os.path.basename(folder_path)
    special_files = {'poster.jpg', 'fanart.jpg', 'thumb.jpg'}
    
    files = [f for f in os.listdir(folder_path) 
             if is_video_file(f) and f.lower() not in special_files]
    
    if not files:
        return
    
    files.sort()
    
    # Use temp names to avoid conflicts
    temp_mappings = []
    for idx, fname in enumerate(files):
        old_path = os.path.join(folder_path, fname)
        _, ext = os.path.splitext(fname)
        temp_name = f"_temp_rename_{idx}{ext}"
        temp_path = os.path.join(folder_path, temp_name)
        
        if not DRY_RUN:
            os.rename(old_path, temp_path)
        temp_mappings.append((temp_path, ext, idx))
    
    for temp_path, ext, idx in temp_mappings:
        if len(temp_mappings) == 1:
            new_name = f"{folder_name}{ext}"
        else:
            new_name = f"{folder_name} - Part {idx + 1}{ext}"
        
        new_path = os.path.join(folder_path, new_name)
        
        if not DRY_RUN:
            if not os.path.exists(new_path):
                os.rename(temp_path, new_path)
                logger.debug(f"Renamed → {new_name}")
            else:
                fallback = f"{folder_name}_{idx}{ext}"
                os.rename(temp_path, os.path.join(folder_path, fallback))
                logger.debug(f"Renamed → {fallback} (fallback)")


def ensure_round_folders(year: int, calendar: dict) -> str:
    """Create round folders for all calendar entries."""
    root = os.path.join(MOTOGP_LIBRARY_PATH, str(year))
    if not DRY_RUN:
        os.makedirs(root, exist_ok=True)
    
    for round_entry in calendar.get("rounds", []):
        dest_dir = os.path.join(root, round_entry["folder"])
        if not os.path.exists(dest_dir) and not DRY_RUN:
            os.makedirs(dest_dir)
            logger.debug(f"Created round folder: {round_entry['folder']}")
    
    return root


# =============================================================================
# COMBINED QUALIFYING DETECTION
# =============================================================================

def _get_q1_session_for_class(race_class: RaceClass) -> SessionType:
    """Get Q1 SessionType for a race class."""
    mapping = {
        RaceClass.MOTO3: SessionType.MOTO3_Q1,
        RaceClass.MOTO2: SessionType.MOTO2_Q1,
        RaceClass.MOTOGP: SessionType.MOTOGP_Q1,
    }
    return mapping.get(race_class, SessionType.MOTOGP_Q1)


def _get_q2_session_for_class(race_class: RaceClass) -> SessionType:
    """Get Q2 SessionType for a race class."""
    mapping = {
        RaceClass.MOTO3: SessionType.MOTO3_Q2,
        RaceClass.MOTO2: SessionType.MOTO2_Q2,
        RaceClass.MOTOGP: SessionType.MOTOGP_Q2,
    }
    return mapping.get(race_class, SessionType.MOTOGP_Q2)


def detect_qualifying_parts(video_files: List[str], session_type: SessionType) -> List[Tuple[str, SessionType]]:
    """Detect if video files are Q1/Q2 parts of a combined qualifying release."""
    race_class = session_type.race_class
    
    if len(video_files) != 2:
        return [(f, session_type) for f in video_files]
    
    sorted_files = sorted(video_files)
    results = []
    
    for file_path in sorted_files:
        filename = os.path.basename(file_path).lower()
        filename_normalized = filename.replace(".", " ").replace("-", " ")
        
        if any(x in filename_normalized for x in ["q1", "qualifying 1", "qualifying1", "qualifying one"]):
            results.append((file_path, _get_q1_session_for_class(race_class)))
        elif any(x in filename_normalized for x in ["q2", "qualifying 2", "qualifying2", "qualifying two"]):
            results.append((file_path, _get_q2_session_for_class(race_class)))
        else:
            results.append((file_path, None))
    
    # If we couldn't detect from filenames, assume order is Q1 then Q2
    if all(r[1] is None for r in results):
        logger.info("  Could not detect Q1/Q2 from filenames, assuming order: Q1, Q2")
        results = [
            (sorted_files[0], _get_q1_session_for_class(race_class)),
            (sorted_files[1], _get_q2_session_for_class(race_class)),
        ]
    elif any(r[1] is None for r in results):
        detected = [r[1] for r in results if r[1] is not None][0]
        if detected.display_name == "Qualifying 1":
            other_session = _get_q2_session_for_class(race_class)
        else:
            other_session = _get_q1_session_for_class(race_class)
        
        results = [(f, s if s else other_session) for f, s in results]
    
    return results


def is_combined_qualifying_release(folder_name: str, video_count: int, session_type: SessionType) -> bool:
    """Check if this is a combined Q1+Q2 qualifying release."""
    if video_count != 2:
        return False
    
    session_name = session_type.display_name
    if session_name not in ["Qualifying 1", "Qualifying 2"]:
        return False
    
    folder_lower = folder_name.lower().replace(".", " ").replace("-", " ")
    
    has_specific_q = (
        re.search(r'\bq1\b', folder_lower) or
        re.search(r'\bq2\b', folder_lower) or
        re.search(r'\bqualifying\s*1\b', folder_lower) or
        re.search(r'\bqualifying\s*2\b', folder_lower) or
        re.search(r'\bqualifying\s*one\b', folder_lower) or
        re.search(r'\bqualifying\s*two\b', folder_lower)
    )
    
    return not has_specific_q


# =============================================================================
# PLEX REFRESH
# =============================================================================

def refresh_plex_and_wait(wait_seconds: int = 10):
    """Trigger Plex refresh and wait for scan to start."""
    if DRY_RUN:
        logger.debug("[DRY-RUN] Would trigger Plex refresh")
        return
    
    if not PLEX_URL or not PLEX_TOKEN or not PLEX_LIBRARY_ID:
        logger.warning("Plex not configured, skipping refresh")
        return
    
    logger.debug(f"Triggering Plex refresh...")
    
    url = f"{PLEX_URL}/library/sections/{PLEX_LIBRARY_ID}/refresh?X-Plex-Token={PLEX_TOKEN}"
    
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            if response.status == 200:
                logger.debug(f"Plex refresh triggered, waiting {wait_seconds}s for scan...")
                time.sleep(wait_seconds)
                return
    except Exception as e:
        logger.warning(f"Plex refresh failed: {e}")


def trigger_plex_refresh():
    """Trigger Plex library refresh."""
    if not changes:
        logger.debug("No changes - Plex refresh skipped")
        return
    
    if not PLEX_URL or not PLEX_TOKEN or not PLEX_LIBRARY_ID:
        logger.warning("Plex not configured, skipping refresh")
        return
    
    logger.info(f"Triggering Plex refresh for '{PLEX_LIBRARY_NAME}'...")
    time.sleep(10)
    
    url = f"{PLEX_URL}/library/sections/{PLEX_LIBRARY_ID}/refresh?X-Plex-Token={PLEX_TOKEN}"
    
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status == 200:
                    logger.info("✅ Plex refresh triggered")
                    return
        except Exception as e:
            logger.warning(f"Plex refresh attempt {attempt + 1} failed: {e}")
            time.sleep(5)
    
    logger.error("❌ Plex refresh failed")


# =============================================================================
# DISCORD NOTIFICATIONS
# =============================================================================

def send_discord_notification(session_type: SessionType, round_info: dict, season: int):
    """Send Discord notification if configured."""
    if not DISCORD_AVAILABLE:
        return
    
    session_name = session_type.full_display_name
    venue = round_info.get("venue", "Unknown")
    round_num = round_info.get("round", 0)
    
    if should_notify(session_name):
        notify_session_added(
            session_name=session_name,
            venue=venue,
            round_num=round_num,
            season=season,
            dry_run=DRY_RUN
        )


# =============================================================================
# MULTI-SESSION HANDLING
# =============================================================================

def is_multi_session_download(folder_name: str, video_count: int, session_type: SessionType = None) -> tuple:
    """Determine if download contains multiple sessions."""
    name = folder_name.lower()
    name_normalized = name.replace(".", " ").replace("-", " ")
    
    # Count class mentions
    class_count = 0
    if re.search(r'moto\s*3', name):
        class_count += 1
    if re.search(r'moto\s*2', name):
        class_count += 1
    if re.search(r'moto\s*gp', name):
        class_count += 1
    
    if class_count > 1:
        return (True, 'multi_class')
    
    # Check for combined qualifying
    if video_count == 2 and session_type is not None:
        session_name = session_type.display_name
        
        if session_name in ["Qualifying 1", "Qualifying 2"]:
            has_specific_q = (
                re.search(r'\bq1\b', name_normalized) or
                re.search(r'\bq2\b', name_normalized) or
                re.search(r'\bqualifying\s*1\b', name_normalized) or
                re.search(r'\bqualifying\s*2\b', name_normalized)
            )
            
            if not has_specific_q:
                return (True, 'combined_quali')
    
    if video_count > 1:
        return (True, 'unknown')
    
    return (False, 'single')


def process_multi_session_download(
    complete_dir: str,
    season_root: str,
    round_info: dict,
    year: int
) -> int:
    """Handle download with multiple video files."""
    video_files = get_video_files(complete_dir)
    
    logger.info(f"Processing multi-session download: {len(video_files)} video files")
    
    processed = 0
    
    for file_path in video_files:
        filename = os.path.basename(file_path)
        race_class = detect_race_class(filename)
        session_type = classify_session(filename, race_class)
        
        if not session_type or session_type == SessionType.UNKNOWN:
            logger.warning(f"Could not classify: {filename}")
            continue
        
        logger.info(f"  → {filename} as {session_type.full_display_name}")
        
        round_num = round_info.get("round", 0)
        prefix = build_prefix(year, round_num, session_type)
        session_display = session_type.full_short_name
        final_folder_name = f"{prefix} {session_display}"
        
        dest_parent = os.path.join(season_root, round_info["folder"])
        final_path = os.path.join(dest_parent, final_folder_name)
        
        if DRY_RUN:
            logger.debug(f"[DRY-RUN] Would create: {final_path}")
            processed += 1
            continue
        
        os.makedirs(final_path, exist_ok=True)
        
        _, ext = os.path.splitext(filename)
        new_filename = f"{final_folder_name}{ext}"
        shutil.move(file_path, os.path.join(final_path, new_filename))
        
        apply_all_artwork(final_path, round_info, session_type)
        refresh_plex_and_wait(10)
        apply_plex_tags(final_path)
        send_discord_notification(session_type, round_info, year)
        
        processed += 1
        changes.add(final_path)
    
    # Cleanup empty source folder
    if not DRY_RUN:
        remaining = get_video_files(complete_dir)
        if not remaining:
            try:
                shutil.rmtree(complete_dir)
                logger.debug(f"Removed empty source folder")
            except Exception:
                pass
    
    logger.info(f"Multi-session complete: {processed} sessions processed")
    return processed


def process_combined_qualifying(
    complete_dir: str,
    season_root: str,
    round_info: dict,
    year: int,
    race_class: RaceClass
) -> int:
    """Handle combined Q1+Q2 qualifying release."""
    video_files = get_video_files(complete_dir)
    
    if len(video_files) != 2:
        logger.warning(f"Expected 2 files for combined quali, found {len(video_files)}")
        return 0
    
    logger.info(f"Processing combined qualifying: {race_class.display_name}")
    
    q1_session = _get_q1_session_for_class(race_class)
    file_sessions = detect_qualifying_parts(video_files, q1_session)
    
    round_num = round_info.get("round", 0)
    processed = 0
    
    for file_path, session_type in file_sessions:
        filename = os.path.basename(file_path)
        
        prefix = build_prefix(year, round_num, session_type)
        session_display = session_type.full_short_name
        final_folder_name = f"{prefix} {session_display}"
        
        dest_parent = os.path.join(season_root, round_info["folder"])
        final_path = os.path.join(dest_parent, final_folder_name)
        
        logger.info(f"  → {filename} → {session_display}")
        
        if DRY_RUN:
            logger.debug(f"  [DRY-RUN] Would create: {final_path}")
            processed += 1
            continue
        
        os.makedirs(final_path, exist_ok=True)
        
        _, ext = os.path.splitext(filename)
        new_filename = f"{final_folder_name}{ext}"
        shutil.move(file_path, os.path.join(final_path, new_filename))
        
        apply_all_artwork(final_path, round_info, session_type)
        refresh_plex_and_wait(10)
        apply_plex_tags(final_path)
        send_discord_notification(session_type, round_info, year)
        
        processed += 1
        changes.add(final_path)
    
    # Cleanup empty source folder
    if not DRY_RUN:
        remaining = get_video_files(complete_dir)
        if not remaining:
            try:
                shutil.rmtree(complete_dir)
                logger.debug(f"Removed empty source folder")
            except Exception as e:
                logger.warning(f"Could not remove source folder: {e}")
    
    logger.info(f"Combined qualifying complete: {processed} sessions")
    return processed


# =============================================================================
# DUAL-PATH PROCESSING
# =============================================================================

def _clear_existing_session_folder(final_path: str) -> bool:
    """
    Make room for a replacement download, without deleting the old media.

    When a replacement for an already-sorted session is downloaded, the
    destination folder exists. ``shutil.move(src, dst)`` then moves the new
    folder *inside* the old one, so Plex keeps showing the old (wrong) file, a
    second item appears, or both, and nothing has visibly failed.

    The old folder is renamed aside to ``<library>/.mismatched/<season>/``: a
    leading dot keeps it out of Plex's scanner and out of every folder walk in
    this app, and the media is still there if the mismatch verdict was wrong.
    This is deliberately a move, not a delete - the only thing in this
    repository that deletes library media is ``scripts/cleanup_utility.py``,
    which is not shipped in the image.
    """
    if not os.path.exists(final_path):
        return True

    parent = os.path.dirname(final_path)
    session_name = os.path.basename(final_path)
    round_dir = os.path.basename(parent)

    # Anchor on the configured library root, not on the shape of final_path:
    # dirname() walking upwards would put the quarantine wherever the path
    # happens to sit if the layout ever changes. If the destination is not
    # under the configured root, refuse - the alternative is moving a folder to
    # a location nothing else knows about.
    library_root = os.path.abspath(MOTOGP_LIBRARY_PATH)
    season_dir = os.path.dirname(os.path.abspath(parent))
    try:
        if os.path.commonpath([library_root, season_dir]) != library_root:
            logger.error(
                f"  ❌ Refusing to move {final_path}: it is not inside the "
                f"configured library {library_root}"
            )
            return False
    except ValueError:  # different drives on Windows-style paths
        logger.error(f"  ❌ Refusing to move {final_path}: not under {library_root}")
        return False

    quarantine_root = os.path.join(
        library_root, QUARANTINE_DIRNAME, os.path.basename(season_dir)
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = os.path.join(
        quarantine_root, f"{session_name} [{round_dir}] [{stamp}]"
    )

    logger.warning(
        "  ⚠️  A session folder already exists for this download. Moving it "
        "aside instead of nesting the replacement inside it."
    )
    logger.warning(f"     From: {final_path}")
    logger.warning(f"     To:   {destination}")

    try:
        os.makedirs(quarantine_root, exist_ok=True)
        shutil.move(final_path, destination)
    except OSError as exc:
        logger.error(f"  ❌ Could not move the existing folder aside: {exc}")
        return False

    logger.warning(
        "     The old file is hidden from Plex by the leading dot in "
        f"'{QUARANTINE_DIRNAME}'. Delete it once the replacement looks right."
    )
    return True


def process_with_prefix(
    folder_name: str, 
    full_path: str, 
    parsed: tuple
) -> bool:
    """Fast path: Process folder with existing YYRREE prefix."""
    year, round_num, session_type = parsed
    
    logger.info(f"Fast path: Prefix detected")
    logger.info(f"  Year: {year}, Round: {round_num}, Session: {session_type.full_display_name}")
    
    calendar = load_calendar(year)
    if not calendar:
        logger.error(f"No calendar for year {year}")
        return False
    
    round_info = get_round_by_number(calendar, round_num)
    if not round_info:
        logger.error(f"No round info for Round {round_num}")
        return False
    
    venue = round_info.get("venue", "Unknown")
    logger.info(f"  Venue: {venue}")
    
    # Check for combined qualifying
    video_files = get_video_files(full_path)
    video_count = len(video_files)
    session_name = session_type.display_name
    race_class = session_type.race_class
    
    if video_count == 2 and session_name in ["Qualifying 1", "Qualifying 2"]:
        logger.info(f"  Detected combined Q1+Q2 ({video_count} files)")
        season_root = ensure_round_folders(year, calendar)
        process_combined_qualifying(full_path, season_root, round_info, year, race_class)
        return True
    
    # Standard prefixed processing
    season_root = os.path.join(MOTOGP_LIBRARY_PATH, str(year))
    dest_parent = os.path.join(season_root, round_info["folder"])
    
    prefix = build_prefix(year, round_num, session_type)
    session_display = session_type.full_short_name
    final_folder_name = f"{prefix} {session_display}"
    final_path = os.path.join(dest_parent, final_folder_name)
    
    logger.info(f"  Destination: {final_path}")
    
    if DRY_RUN:
        logger.info("[DRY-RUN] Would move and process")
        return True
    
    os.makedirs(dest_parent, exist_ok=True)
    if not _clear_existing_session_folder(final_path):
        return False
    shutil.move(full_path, final_path)
    
    flatten_if_single_subdir(final_path)
    rename_files_to_match_folder(final_path)
    apply_all_artwork(final_path, round_info, session_type)
    refresh_plex_and_wait(10)
    apply_plex_tags(final_path)
    send_discord_notification(session_type, round_info, year)
    
    changes.add(final_path)
    logger.info(f"✅ Sorted to: {final_folder_name}")
    return True


def process_without_prefix(folder_name: str, full_path: str) -> bool:
    """Fallback path: Process folder without prefix using classification."""
    logger.info(f"Fallback path: Classification required")
    
    safe_folder = normalize_name(folder_name)
    
    race_class = detect_race_class(safe_folder)
    session_type = classify_session(safe_folder, race_class)
    
    logger.info(f"  Class: {race_class.display_name}, Session: {session_type.display_name}")
    
    year = extract_year(folder_name)
    if not year:
        logger.error(f"No year found in: {folder_name}")
        return False
    
    logger.info(f"  Year: {year}")
    
    calendar = load_calendar(year)
    if not calendar:
        logger.error(f"No calendar for year {year}")
        return False
    
    round_info = identify_round(safe_folder, calendar)
    if not round_info:
        logger.error(f"Could not identify round from: {folder_name}")
        return False
    
    round_num = round_info.get("round", 0)
    venue = round_info.get("venue", "Unknown")
    
    logger.info(f"  Round: {round_num} - {venue}")
    
    # Check for combined qualifying release
    video_files = get_video_files(full_path)
    video_count = len(video_files)
    
    if is_combined_qualifying_release(safe_folder, video_count, session_type):
        logger.info(f"  Detected combined Q1+Q2 qualifying release ({video_count} files)")
        season_root = ensure_round_folders(year, calendar)
        process_combined_qualifying(full_path, season_root, round_info, year, race_class)
        return True
    
    # Standard single-session processing
    prefix = build_prefix(year, round_num, session_type)
    logger.debug(f"  Generated prefix: {prefix}")
    
    season_root = ensure_round_folders(year, calendar)
    dest_parent = os.path.join(season_root, round_info["folder"])
    
    session_display = session_type.full_short_name
    final_folder_name = f"{prefix} {session_display}"
    final_path = os.path.join(dest_parent, final_folder_name)
    
    logger.info(f"  Destination: {final_path}")
    
    if DRY_RUN:
        logger.info("[DRY-RUN] Would move and process")
        return True
    
    os.makedirs(dest_parent, exist_ok=True)
    if not _clear_existing_session_folder(final_path):
        return False
    shutil.move(full_path, final_path)
    
    flatten_if_single_subdir(final_path)
    rename_files_to_match_folder(final_path)
    apply_all_artwork(final_path, round_info, session_type)
    refresh_plex_and_wait(10)
    apply_plex_tags(final_path)
    send_discord_notification(session_type, round_info, year)
    
    changes.add(final_path)
    logger.info(f"✅ Sorted to: {final_folder_name}")
    return True


# =============================================================================
# MAIN PROCESSING
# =============================================================================

def process_folder(source_path: str, dry_run: bool = False) -> bool:
    """
    Process a single folder. Returns True on success.
    
    This is the main entry point called by the watcher.
    """
    global DRY_RUN
    DRY_RUN = dry_run
    
    folder_name = os.path.basename(source_path)
    
    logger.info(f"{'─' * 50}")
    logger.info(f"Processing: {folder_name}")
    logger.info(f"{'─' * 50}")
    
    # Cleanup
    complete_dir = cleanup_folder(source_path)
    folder_name = os.path.basename(complete_dir)
    
    # Try to parse prefix from folder name
    parsed = parse_prefix(folder_name)
    
    if parsed:
        success = process_with_prefix(folder_name, complete_dir, parsed)
    else:
        # Need to classify - check for multi-session first
        safe_folder = normalize_name(folder_name)
        race_class = detect_race_class(safe_folder)
        session_type = classify_session(safe_folder, race_class)
        
        video_count = count_video_files(complete_dir)
        is_multi, multi_type = is_multi_session_download(folder_name, video_count, session_type)
        
        logger.debug(f"Video files: {video_count}, Multi-session: {is_multi} ({multi_type})")
        
        if is_multi and video_count > 1:
            year = extract_year(folder_name)
            if not year:
                logger.error(f"No year found in: {folder_name}")
                return False
            
            calendar = load_calendar(year)
            if not calendar:
                logger.error(f"No calendar for year {year}")
                return False
            
            round_info = identify_round(safe_folder, calendar)
            if not round_info:
                logger.error(f"Could not identify round from: {folder_name}")
                return False
            
            season_root = ensure_round_folders(year, calendar)
            
            if multi_type == 'multi_class':
                logger.info("Detected multi-class download")
                process_multi_session_download(complete_dir, season_root, round_info, year)
            elif multi_type == 'combined_quali':
                logger.info("Detected combined Q1+Q2 qualifying release")
                process_combined_qualifying(complete_dir, season_root, round_info, year, race_class)
            else:
                logger.info("Detected unknown multi-file download, attempting classification")
                process_multi_session_download(complete_dir, season_root, round_info, year)
            
            success = True
        else:
            success = process_without_prefix(folder_name, complete_dir)
    
    logger.info(f"{'─' * 50}")
    return success


def main():
    """Main entry point for standalone execution."""
    parser = argparse.ArgumentParser(
        description="MotoGP Sort - Standalone Sorter",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", "-s", required=True, help="Source folder to process")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Preview without changes")
    parser.add_argument("--debug", "-d", action="store_true", help="Enable debug logging")
    
    args = parser.parse_args()
    
    # Initialize logging
    init_logging(dry_run=args.dry_run, debug=args.debug)
    init_plex_tagger()
    
    source = args.source.rstrip("/")
    
    if not os.path.isdir(source):
        logger.error(f"Source not found: {source}")
        return 1
    
    success = process_folder(source, dry_run=args.dry_run)
    
    if not args.dry_run:
        trigger_plex_refresh()
    
    logger.info("Processing complete")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())