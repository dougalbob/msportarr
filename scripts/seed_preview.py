#!/usr/bin/env python3
"""Create isolated, synthetic Motorsportarr data for a local UI preview.

This is intentionally outside the runtime image and never touches /config,
production library paths, download clients, Plex, or external services.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PREVIEW_MARKER = (
    "Synthetic Motorsportarr UI preview. No real media, service credentials, "
    "downloads, or Plex connection.\n"
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path,
        default=REPO / ".preview-data" / "runtime",
        help="isolated preview data directory (default: .preview-data/runtime)",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="rebuild an existing output only when its PREVIEW_ONLY marker matches",
    )
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if output.exists():
        if not output.is_dir():
            parser.error(f"refusing to replace non-directory output: {output}")
        marker = output / "PREVIEW_ONLY.txt"
        is_our_preview = (
            marker.is_file()
            and marker.read_text(encoding="utf-8") == PREVIEW_MARKER
        )
        if is_our_preview and not args.reset:
            print(f"Existing synthetic preview data preserved at {output}")
            print("Use --reset only if you explicitly want to rebuild it.")
            return 0
        if any(output.iterdir()) and not is_our_preview:
            parser.error(
                "refusing to overwrite non-empty directory without our "
                f"preview marker: {output}"
            )
        if is_our_preview and args.reset:
            shutil.rmtree(output)

    config_dir = output / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    library_roots = {
        "motogp": output / "library" / "motogp",
        "f1": output / "library" / "f1",
    }
    downloads_roots = {
        "motogp": output / "downloads" / "motogp",
        "f1": output / "downloads" / "f1",
    }
    for path in (*library_roots.values(), *downloads_roots.values()):
        path.mkdir(parents=True, exist_ok=True)

    # Use the calendars shipped by this exact checkout, not invented fixtures.
    calendars = {}
    for source in sorted((REPO / "config").glob("*_20*.json")):
        target = config_dir / source.name
        shutil.copy2(source, target)
        prefix, season = source.stem.rsplit("_", 1)
        if prefix in ("motogp", "f1"):
            calendars[(prefix, int(season))] = json.loads(target.read_text(encoding="utf-8"))

    # Set the private config path before importing application helpers. The demo
    # settings deliberately omit Prowlarr/SABnzbd/Plex URLs and credentials.
    os.environ["CONFIG_DIR"] = str(config_dir)
    os.environ["MOTORSPORTARR_SERIES"] = "motogp"
    sys.path.insert(0, str(REPO))
    from series import runtime
    from motogp_common import round_runs_session

    quote = lambda value: json.dumps(str(value))
    settings = f"""# SYNTHETIC PREVIEW ONLY — no external service credentials are configured.
library:
  motogp_path: {quote(library_roots['motogp'])}
  downloads_path: {quote(downloads_roots['motogp'])}
  f1_path: {quote(library_roots['f1'])}
  f1_downloads_path: {quote(downloads_roots['f1'])}
grabber:
  current_season: 2026
  required_quality: 1080p
  max_grabs_per_run: 22
  search_delay_minutes: 240
webui:
  quick_status_log_lines: 100
timezone: Europe/London
"""
    (config_dir / "settings.yml").write_text(settings, encoding="utf-8")
    (output / "PREVIEW_ONLY.txt").write_text(PREVIEW_MARKER, encoding="utf-8")

    # The 2026 weekends have just finished on this session's clock (2026-11-09).
    # Populate representative library/state data: most sessions are in the
    # library, one is still downloading, and one search found no release.
    now = datetime.now().replace(microsecond=0)
    selected_weekends = {"motogp": (2026, 20), "f1": (2026, 21)}
    for series_key, (season, round_number) in selected_weekends.items():
        calendar = calendars[(series_key, season)]
        round_info = next(r for r in calendar["rounds"] if r["round"] == round_number)
        rt = runtime(series_key)
        timed_keys = {
            key
            for day in (round_info.get("sessions") or {}).values()
            if isinstance(day, dict)
            for key in day
        }
        candidates = []
        for session, wanted in rt.default_desired_sessions.items():
            if not wanted or not round_runs_session(
                round_info, session, calendar, series=series_key
            ):
                continue
            calendar_key = rt.session_to_calendar_key.get(session)
            if calendar_key and calendar_key in timed_keys:
                candidates.append(session)

        if not candidates:
            raise RuntimeError(f"No timed default sessions found for {series_key}")

        # Always include the feature race in the sample library so the "latest
        # scheduled addition" card has a truthful on-disk counterpart.
        race = next(
            (s for s in candidates if s.name in {"MOTOGP_RACE", "RACE"}),
            candidates[-1],
        )
        other = [s for s in candidates if s != race]
        downloading = other.pop(0) if other else None
        failed = other.pop(0) if other else None
        in_library = [s for s in other] + [race]

        grabbed: dict[str, dict] = {}
        searched: dict[str, str] = {}
        failed_state: dict[str, dict] = {}
        failures: dict[str, dict] = {}
        activity = {"events": {}, "completed_history": []}

        # Build synthetic session folders with a clear marker, not playable
        # empty video files. The WebUI deliberately identifies sessions by the
        # YYRREE folder prefix and does not need actual media for this preview.
        round_folder = round_info.get("folder") or f"Round {round_number} {round_info['venue']}"
        round_dir = library_roots[series_key] / str(season) / round_folder
        round_dir.mkdir(parents=True, exist_ok=True)

        for index, session in enumerate(in_library):
            key = f"{season}_R{round_number:02d}_E{rt.element_number(session, season):02d}"
            title = f"[PREVIEW SEED] {rt.series.display_name} {season} {round_info['venue']} {session.full_short_name} 1080p"
            grabbed_at = now - timedelta(minutes=(len(in_library) - index) * 17)
            grabbed[key] = {
                "timestamp": grabbed_at.isoformat(),
                "release": title,
                "mode": "scheduled",
            }
            searched[key] = (grabbed_at - timedelta(minutes=12)).isoformat()
            folder = f"{rt.build_prefix(season, round_number, session)} {session.full_short_name}"
            session_dir = round_dir / folder
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "PREVIEW_ONLY.txt").write_text(
                "Synthetic folder used only to preview library status; no video is present.\n",
                encoding="utf-8",
            )
            activity["completed_history"].append({
                "id": key,
                "name": session.full_short_name,
                "stage": "complete",
                "stage_icon": "✅",
                "stage_color": "#27ae60",
                "stage_label": "Complete",
                "details": "Ready in synthetic preview library",
                "timestamp": (grabbed_at + timedelta(minutes=5)).isoformat(),
                "round": round_number,
                "venue": round_info["venue"],
                "progress": None,
                "error_message": None,
                "release_name": title,
                "nzo_id": None,
            })

        if downloading is not None:
            key = f"{season}_R{round_number:02d}_E{rt.element_number(downloading, season):02d}"
            title = f"[PREVIEW SEED] {rt.series.display_name} {season} {round_info['venue']} {downloading.full_short_name} 1080p"
            timestamp = (now - timedelta(minutes=23)).isoformat()
            grabbed[key] = {"timestamp": timestamp, "release": title, "mode": "scheduled", "nzo_id": "PREVIEW-QUEUE"}
            searched[key] = (now - timedelta(minutes=31)).isoformat()
            activity["events"][key] = {
                "id": key,
                "name": downloading.full_short_name,
                "stage": "downloading",
                "stage_icon": "⬇️",
                "stage_color": "#f39c12",
                "stage_label": "Downloading",
                "details": "Synthetic SABnzbd queue item — 67%",
                "timestamp": timestamp,
                "round": round_number,
                "venue": round_info["venue"],
                "progress": 67,
                "error_message": None,
                "release_name": title,
                "nzo_id": "PREVIEW-QUEUE",
            }

        if failed is not None:
            key = f"{season}_R{round_number:02d}_E{rt.element_number(failed, season):02d}"
            timestamp = (now - timedelta(minutes=46)).isoformat()
            record = {"timestamp": timestamp, "reason": "Synthetic preview: no matching release found"}
            failed_state[key] = record
            failures[f"{key}@{timestamp}"] = record
            searched[key] = timestamp

        # Sort newest completed event first, matching the real activity store.
        activity["completed_history"].sort(key=lambda e: e["timestamp"], reverse=True)
        state = {
            "searched": searched,
            "grabbed": grabbed,
            "failed": failed_state,
            "failures": failures,
            "last_run": (now - timedelta(minutes=9)).isoformat(),
            "last_scheduled_check": (now - timedelta(minutes=9)).isoformat(),
        }
        write_json(config_dir / f"grabber_state.{series_key}.json", state)
        write_json(config_dir / f"activity_events.{series_key}.json", activity)

    # The WebUI process is MotoGP-bound, so the settings form's shared file is
    # the default series store. Include both vocabulary maps for a clear preview.
    mgp_runtime = runtime("motogp")
    f1_runtime = runtime("f1")
    write_json(config_dir / "webui_settings.motogp.json", {
        "sessions": {s.name: bool(enabled) for s, enabled in mgp_runtime.default_desired_sessions.items()},
        "f1_sessions": {s.name: bool(enabled) for s, enabled in f1_runtime.default_desired_sessions.items()},
    })
    # Also seed the series-native file used by a standalone F1 pipeline process.
    write_json(config_dir / "webui_settings.f1.json", {
        "sessions": {s.name: bool(enabled) for s, enabled in f1_runtime.default_desired_sessions.items()},
    })

    print(f"Preview data created at {output}")
    print(f"Config: {config_dir}")
    print(f"MotoGP library: {library_roots['motogp']}")
    print(f"Formula 1 library: {library_roots['f1']}")
    for key, (season, round_number) in selected_weekends.items():
        print(f"{key}: {season} Round {round_number}; synthetic folders, queue and stats")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
