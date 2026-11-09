"""
Tests for build-context and repository hygiene.

Two of these exist because the problem was invisible rather than broken:

  * the build context carried ~1.1 GB of `.git` and `assets/` on every build,
    which nothing surfaced because the build still worked;
  * a utility that deletes media sat at the repository root named
    `test_cleanup_utility.py`, so `pytest` could collect it.

The rest guard the principle behind them: nothing that only exists for
development may end up in a runtime image, and no committed file may contain a
live credential.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from series import all_series  # noqa: E402
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _ignore_patterns():
    if not DOCKERIGNORE.exists():
        return []
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _copy_sources():
    """Paths the Dockerfile COPYs into the image."""
    sources = []
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^COPY\s+(?:--\S+\s+)*(.+?)\s+\S+\s*$", line)
        if match:
            sources.append(match.group(1).strip())
    return sources


def _is_ignored(relative_path: str) -> bool:
    """Approximate Docker's .dockerignore matching, which is all we need."""
    import fnmatch

    for pattern in _ignore_patterns():
        stem = pattern.rstrip("/")
        if fnmatch.fnmatch(relative_path, pattern):
            return True
        if relative_path == stem or relative_path.startswith(stem + "/"):
            return True
        if fnmatch.fnmatch(relative_path, stem + "/*"):
            return True
    return False


# ---------------------------------------------------------------------------
# Build context
# ---------------------------------------------------------------------------

def test_dockerignore_exists():
    assert DOCKERIGNORE.is_file(), "the whole 1.1 GB checkout is sent to the daemon"


@pytest.mark.parametrize("path", [
    ".git",
    "aicontextual_notes",
    "tests",
    "scripts",
    "docs",
    "AGENTS.md",
    "SANDBOX.md",
    "test_prowlarr.py",
    "test_cleanup_utility.py",
    "motogp_grabber.py.backup",
    ".venv",
    ".preview-data/runtime/config",
    "config/settings.yml",
])
def test_development_only_paths_are_excluded_from_the_build_context(path):
    assert _is_ignored(path), f"{path} would still be sent to the Docker daemon"


def test_generated_preview_runtime_is_ignored_by_git_and_docker():
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/.preview-data/" in gitignore
    assert _is_ignored(".preview-data/runtime/config")


def test_dockerignore_does_not_exclude_anything_the_image_needs():
    """
    The dangerous direction: ignoring a COPY source produces a confusing build
    failure. Every source the Dockerfile copies must survive the ignore list.
    """
    excluded = [s for s in _copy_sources() if _is_ignored(s)]
    assert not excluded, f".dockerignore excludes files the image needs: {excluded}"


def test_season_calendars_still_reach_the_image():
    """config/ is copied wholesale; the ignore rules must not eat the calendars."""
    calendars = [
        path for prefix in (spec.calendar_prefix for spec in all_series())
        for path in sorted((REPO_ROOT / "config").glob(f"{prefix}_*.json"))
    ]
    assert {path.name for path in calendars} >= {"motogp_2026.json", "f1_2026.json"}
    for calendar in calendars:
        assert not _is_ignored(f"config/{calendar.name}"), calendar.name


# ---------------------------------------------------------------------------
# Nothing destructive or developer-only in the image
# ---------------------------------------------------------------------------

def test_the_cleanup_utility_is_not_in_the_image():
    """
    It calls shutil.rmtree on library folders. It must not be reachable from
    inside the container, where it would run as root.
    """
    assert not (REPO_ROOT / "test_cleanup_utility.py").exists(), (
        "it was moved to scripts/ so pytest cannot collect it"
    )
    copied = " ".join(_copy_sources())
    assert "scripts" not in copied
    assert "cleanup_utility" not in copied
    assert _is_ignored("scripts/cleanup_utility.py")


def test_no_test_files_are_copied_into_the_image():
    copied = " ".join(_copy_sources())
    assert "test_" not in copied
    assert "tests" not in copied.split()


def test_the_repository_root_has_no_scratch_scripts():
    """
    `pytest` walks the root. A root-level test_*.py that is not a test either
    breaks collection or, worse, does something destructive when imported.
    """
    strays = [
        p.name
        for p in REPO_ROOT.glob("test_*.py")
        if p.is_file()
    ]
    assert not strays, f"scratch scripts at the repo root: {strays}"


def test_backup_files_are_not_tracked():
    """
    Six *.backup files were committed and drifted out of date. Git already holds
    every previous version; a file named .backup is a second, worse history.
    """
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:  # not a git checkout (e.g. an sdist)
        pytest.skip("not a git repository")
    tracked = [
        line for line in result.stdout.splitlines()
        if re.search(r"\.(backup|bak|orig|rej)(\d+)?$", line)
    ]
    assert not tracked, f"backup files are tracked: {tracked}"


# ---------------------------------------------------------------------------
# No credentials in the working tree
# ---------------------------------------------------------------------------

#: Matches `KEY = "longopaquevalue"` style literals, which is how a real key
#: leaks into a source file. Excludes the placeholders and env lookups that
#: legitimately appear in examples and config code.
_SECRET_LITERAL = re.compile(
    r"""(?:api_?key|token|apikey|password|secret)["']?\s*[:=]\s*["']([A-Za-z0-9_\-]{16,})["']""",
    re.IGNORECASE,
)

_ALLOWED = re.compile(
    r"your_|example|placeholder|changeme|dummy|test-|fake|\*\*\*|\$\{|<|>",
    re.IGNORECASE,
)

_TEXT_SUFFIXES = {".py", ".sh", ".yml", ".yaml", ".json", ".md", ".txt", ".xml", ".html", ".js"}


def _tracked_text_files():
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        pytest.skip("not a git repository")
    for name in result.stdout.splitlines():
        path = REPO_ROOT / name
        if path.suffix in _TEXT_SUFFIXES and path.is_file():
            yield path


def test_no_credential_literals_are_committed():
    """
    A real Prowlarr URL and API key were committed in test_prowlarr.py and the
    repository is public. This scans the working tree for the same mistake.

    It cannot look at history - that is a separate, manual job - but it stops
    the next one.
    """
    offenders = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in _SECRET_LITERAL.finditer(text):
            value = match.group(1)
            if _ALLOWED.search(value) or _ALLOWED.search(match.group(0)):
                continue
            # A run of the same character, or pure hex that is obviously a
            # placeholder, is not a credential.
            if len(set(value.lower())) <= 3:
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)[:40]}")
    assert not offenders, "possible committed credentials:\n" + "\n".join(offenders)
