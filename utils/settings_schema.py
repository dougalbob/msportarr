#!/usr/bin/env python3
"""
settings.yml schema
===================

The problem this solves is not typos, it is *silence*. A misspelled key in
settings.yml is simply not found by the loader, which falls back to its default
and carries on. Nothing errors, nothing warns, and the app behaves as if the line
had never been written. That is the same class of bug as the env vars that were
documented but never read: the configuration says one thing and the process does
another.

So this module describes what a settings.yml is allowed to contain, and reports
everything that does not fit:

    unknown-key   a key nothing reads      (typo, or a setting that was removed)
    wrong-type    a value the loader cannot use (list where a string is expected)
    bad-value     a value that parses but is nonsense (unknown quality, bad tz)

It deliberately does NOT reject the file. A configuration that the app can
partly use is better than a container that refuses to start, and the loader
already has defaults for everything. Warnings go to the startup report and to
the WebUI's validation status, which is where configuration problems already
surface.

Two rules keep this honest:

  * Every section/key here must correspond to something the code actually reads
    (directly, or through ENV_SPEC). A schema that drifts from the code becomes
    a second, lying source of truth. A test checks the schema against the
    getters.
  * ``uiadminpassword`` and ``auth.*`` are not here. Credentials and the
    allowlist are not settings.yml's business; see utils/auth.py.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Type checks
# ---------------------------------------------------------------------------

def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str_list(value: Any) -> bool:
    if isinstance(value, str):
        return True  # comma-separated is accepted by the getter
    return isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)


def _is_positive_int(value: Any) -> bool:
    return _is_int(value) and value > 0


#: Settings that happen to be typed as strings but where an unquoted number in
#: YAML is perfectly usable - a library id is the obvious one. `library_id: 16`
#: and `library_id: "16"` behave identically, so reporting the first as an error
#: would be crying wolf and would teach people to ignore this validator.
_COERCIBLE_TO_STRING = {
    ("plex", "library_id"),
    ("plex", "f1_library_id"),
}


def _is_string_or_number(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


TYPES = {
    "string": (_is_str, "a string"),
    "integer": (_is_int, "a whole number"),
    "positive integer": (_is_positive_int, "a positive whole number"),
    "string or list of strings": (_is_str_list, "a string, or a list of strings"),
}


@dataclass(frozen=True)
class Setting:
    """One permitted settings.yml key."""

    section: str
    key: Optional[str]          # None = the section itself is a scalar
    kind: str
    description: str
    choices: Tuple[str, ...] = ()
    #: True when a user could reasonably expect to edit this in the UI.
    documented: bool = True


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------

SETTINGS: Tuple[Setting, ...] = (
    # --- services -----------------------------------------------------------
    Setting("prowlarr", "url", "string", "Prowlarr base URL"),
    Setting("prowlarr", "api_key", "string", "Prowlarr API key"),
    Setting("sabnzbd", "url", "string", "SABnzbd base URL"),
    Setting("sabnzbd", "api_key", "string", "SABnzbd API key"),
    Setting("plex", "url", "string", "Plex base URL"),
    Setting("plex", "token", "string", "Plex token"),
    # One Plex library identity per series. The un-prefixed keys are MotoGP's,
    # which is what a pre-hybrid settings.yml has and still means.
    Setting("plex", "library_id", "string", "Plex library section id"),
    Setting("plex", "library_name", "string", "Plex library display name"),
    Setting("plex", "path_mapping.container", "string",
            "Path prefix as this container sees it"),
    Setting("plex", "path_mapping.plex", "string",
            "The same prefix as Plex sees it"),
    Setting("plex", "f1_library_id", "string", "Formula 1 Plex library section id"),
    Setting("plex", "f1_library_name", "string", "Formula 1 Plex library display name"),
    Setting("plex", "f1_path_mapping.container", "string",
            "Formula 1 path prefix as this container sees it"),
    Setting("plex", "f1_path_mapping.plex", "string",
            "The same Formula 1 prefix as Plex sees it"),

    # --- paths --------------------------------------------------------------
    Setting("library", "motogp_path", "string", "MotoGP library path in-container"),
    Setting("library", "f1_path", "string", "Formula 1 library path in-container"),
    # `downloads_path` is MotoGP's and stays the shared name an existing
    # settings.yml uses; the per-series names are separate settings so that one
    # series never resolves to another's folder.
    Setting("library", "downloads_path", "string", "Completed-downloads path"),
    Setting("library", "motogp_downloads_path", "string",
            "MotoGP completed-downloads path (overrides library.downloads_path)"),
    Setting("library", "f1_downloads_path", "string",
            "Formula 1 completed-downloads path"),

    # --- grabber ------------------------------------------------------------
    Setting("grabber", "allowed_groups", "string or list of strings",
            "Release groups to accept"),
    Setting("grabber", "required_quality", "string", "Required video quality",
            choices=("720p", "1080p", "2160p")),
    Setting("grabber", "max_grabs_per_run", "positive integer",
            "Maximum episodes grabbed per scheduled run"),
    Setting("grabber", "search_delay_minutes", "positive integer",
            "Minutes to wait before searching after a session ends"),
    Setting("webui", "quick_status_log_lines", "positive integer",
            "Number of log lines shown in Quick Status"),
    Setting("grabber", "current_season", "positive integer",
            "Season the scheduler should track"),

    # --- misc ---------------------------------------------------------------
    Setting("discord", "webhook_url", "string", "Discord webhook URL (optional)"),
    Setting("timezone", None, "string", "IANA timezone, e.g. Europe/London"),
)

_BY_PATH = {
    (s.section, s.key): s for s in SETTINGS
}

#: Sections that may exist in the file but contain no settings of their own.
#: ``auth`` is referenced by ENV_SPEC for documentation purposes only; putting
#: auth in settings.yml is not supported on purpose.
KNOWN_EMPTY_SECTIONS = ("auth",)


@dataclass
class Issue:
    """A single problem found in settings.yml."""

    path: str            # e.g. "grabber.max_grabs_per_runn"
    kind: str            # unknown-key | wrong-type | bad-value
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass
class Report:
    issues: List[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": [
                {"path": i.path, "kind": i.kind, "message": i.message}
                for i in self.issues
            ],
            # Short strings, for the startup log and the WebUI banner.
            "messages": [str(i) for i in self.issues],
        }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _suggest(section: str, key: str) -> Optional[str]:
    """Nearest known key in the same section, for a typo message."""
    candidates = [s.key for s in SETTINGS if s.section == section and s.key]
    if not candidates:
        return None
    matches = difflib.get_close_matches(key, candidates, n=1, cutoff=0.6)
    if not matches:
        return None
    return f"{section}.{matches[0]}"


def _check_scalar(path: str, setting: Setting, value: Any, out: Report) -> None:
    checker, described = TYPES[setting.kind]

    # An empty value (``webhook_url:`` with nothing after it) means "not set",
    # which is the loader's own convention. It is not a type error.
    if value is None:
        return

    if (setting.section, setting.key) in _COERCIBLE_TO_STRING and _is_string_or_number(value):
        checker = _is_string_or_number
        described = "a string or number"

    if not checker(value):
        out.issues.append(Issue(
            path, "wrong-type",
            f"expected {described}, got {type(value).__name__} ({value!r})"
        ))
        return

    if setting.choices and isinstance(value, str) and value not in setting.choices:
        out.issues.append(Issue(
            path, "bad-value",
            f"{value!r} is not one of {', '.join(setting.choices)}"
        ))


def _has_keys_under(section: str, path: str) -> bool:
    """True when the schema defines at least one key inside this dotted path."""
    prefix = path.split(".", 1)[1] if "." in path else path
    return any(
        setting.section == section
        and setting.key
        and (setting.key == prefix or setting.key.startswith(prefix + "."))
        for setting in SETTINGS
    )


def _walk(section: str, node: Any, out: Report, prefix: str = "") -> int:
    """
    Walk a section, reporting unknown keys and checking known ones.

    Returns how many keys in this subtree the schema knows about. That count is
    what distinguishes a nested block like ``plex.path_mapping`` (whose children
    are known, so the block itself is fine) from a genuinely unknown block. The
    first version tested "did we add any issues", which reported every valid
    nested block as unknown.
    """
    if not isinstance(node, dict):
        # A section that should be a mapping, e.g. `plex: "http://..."`.
        if (section, None) in _BY_PATH:
            _check_scalar(section, _BY_PATH[(section, None)], node, out)
            return 1
        out.issues.append(Issue(
            section, "wrong-type",
            f"expected a section (mapping), got {type(node).__name__}"
        ))
        return 0

    known = 0

    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else f"{section}.{key}"
        # The schema keys nested settings with dots, e.g. "path_mapping.plex".
        dotted = path.split(".", 1)[1] if "." in path else key

        setting = _BY_PATH.get((section, dotted))
        if setting is not None:
            known += 1
            _check_scalar(path, setting, value, out)
            continue

        # A nested block whose children may be settings (path_mapping). Recurse
        # so an error names the leaf rather than the block. If the block has no
        # known children at all, report the block once and drop the per-leaf
        # noise - listing every line of an unknown block is not more helpful.
        if isinstance(value, dict):
            # Does the schema know ANY key under this path? "path_mapping.plexx"
            # has no known children, but "path_mapping" is still a real block
            # because "path_mapping.container" exists - so test the prefix, not
            # whether the children validated.
            if _has_keys_under(section, path):
                known += 1
                _walk(section, value, out, prefix=path)
            else:
                out.issues.append(Issue(
                    path, "unknown-key", "unknown section; nothing reads it"
                ))
            continue

        suggestion = _suggest(section, key)
        message = "unknown key; nothing reads it"
        if suggestion:
            message += f" (did you mean {suggestion}?)"
        out.issues.append(Issue(path, "unknown-key", message))

    return known


def validate_settings(settings: Dict[str, Any]) -> Report:
    """Check a loaded settings.yml against the schema."""
    report = Report()

    if not isinstance(settings, dict):
        report.issues.append(Issue(
            "<file>", "wrong-type",
            f"settings.yml must be a mapping, got {type(settings).__name__}"
        ))
        return report

    known_sections = {s.section for s in SETTINGS} | set(KNOWN_EMPTY_SECTIONS)

    for section, node in settings.items():
        if section not in known_sections:
            suggestion = difflib.get_close_matches(
                section, sorted(known_sections), n=1, cutoff=0.6
            )
            message = "unknown section; nothing reads it"
            if suggestion:
                message += f" (did you mean {suggestion[0]}?)"
            report.issues.append(Issue(section, "unknown-key", message))
            continue

        if section in KNOWN_EMPTY_SECTIONS:
            if isinstance(node, dict) and node:
                report.issues.append(Issue(
                    section, "unknown-key",
                    "authentication is configured with AUTH_* environment "
                    "variables, not settings.yml"
                ))
            continue

        _walk(section, node, report)

    return report


def validate_file(path) -> Report:
    """Convenience: load a file (tolerating YAML errors) and validate it."""
    import yaml

    try:
        with open(path, "r") as handle:
            data = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        return Report()
    except Exception as exc:  # noqa: BLE001 - malformed YAML is itself an issue
        report = Report()
        report.issues.append(Issue("<file>", "wrong-type", f"could not parse YAML: {exc}"))
        return report

    return validate_settings(data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    """python -m utils.settings_schema [path]"""
    import os
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    path = args[0] if args else os.path.join(
        os.environ.get("CONFIG_DIR", "/config"), "settings.yml"
    )

    report = validate_file(path)
    if report.ok:
        print(f"{path}: no problems found")
        return 0

    print(f"{path}: {len(report.issues)} problem(s)")
    for issue in report.issues:
        print(f"  {issue}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
