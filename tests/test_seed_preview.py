"""The synthetic WebUI demo data stays reusable without clobbering user data."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed_preview.py"


def _seed(output: Path, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SEED_SCRIPT), "--output", str(output), *extra_args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_seed_preview_generates_both_series_and_preserves_existing_seed(tmp_path):
    output = tmp_path / "preview"

    created = _seed(output)
    assert created.returncode == 0, created.stderr
    assert "Preview data created" in created.stdout
    assert (output / "PREVIEW_ONLY.txt").is_file()
    assert (output / "config" / "grabber_state.motogp.json").is_file()
    assert (output / "config" / "grabber_state.f1.json").is_file()
    assert (output / "library" / "motogp").is_dir()
    assert (output / "library" / "f1").is_dir()

    state_file = output / "config" / "grabber_state.motogp.json"
    original_state = state_file.read_bytes()
    repeated = _seed(output)

    assert repeated.returncode == 0, repeated.stderr
    assert "preserved" in repeated.stdout
    assert state_file.read_bytes() == original_state


def test_seed_preview_refuses_to_overwrite_unmarked_data(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("not preview data\n", encoding="utf-8")

    result = _seed(output)

    assert result.returncode != 0
    assert "refusing to overwrite" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "not preview data\n"


def test_seed_preview_reset_rebuilds_only_a_marked_preview(tmp_path):
    output = tmp_path / "preview"
    created = _seed(output)
    assert created.returncode == 0, created.stderr

    user_change = output / "local-preview-note.txt"
    user_change.write_text("temporary UI poking\n", encoding="utf-8")
    reset = _seed(output, "--reset")

    assert reset.returncode == 0, reset.stderr
    assert "Preview data created" in reset.stdout
    assert not user_change.exists()
    assert (output / "PREVIEW_ONLY.txt").is_file()
