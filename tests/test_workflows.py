"""
Tests for the CI workflows and release automation.

Workflows cannot be run here (no Docker daemon, and the release notes API is not
reachable), so what is testable is the set of properties that have already caused
real failures:

  * a tag format that the container workflow cannot turn into :MAJOR.MINOR,
  * notes that are generated but never published,
  * a smoke test that does not actually assert the non-root behaviour it exists
    to protect.

When a workflow changes, these fail loudly rather than the next release failing
quietly.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SMOKE = WORKFLOWS / "container-smoke.yml"
RELEASE = WORKFLOWS / "release.yml"
PUBLISH = WORKFLOWS / "docker-publish.yml"
RELEASE_CONFIG = REPO_ROOT / ".github" / "release.yml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Every workflow is at least structurally valid
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_workflow_is_valid_yaml_with_a_trigger_and_jobs(path):
    data = load(path)
    # PyYAML reads the bare key `on:` as the boolean True.
    assert "on" in data or True in data, f"{path.name} has no trigger"
    assert data.get("jobs"), f"{path.name} has no jobs"


# ---------------------------------------------------------------------------
# Container smoke test (item 8)
# ---------------------------------------------------------------------------

def test_smoke_test_builds_and_runs_the_image():
    text = SMOKE.read_text()
    assert "docker/build-push-action" in text
    assert "load: true" in text, "the image must be loaded locally to run it"
    assert "docker run -d" in text


def test_smoke_test_asserts_the_app_is_not_root():
    """
    The privilege drop is the change most likely to regress silently: a missing
    setpriv or a misordered entrypoint would put the app back to root and
    nothing else would notice.
    """
    text = SMOKE.read_text()
    assert "docker top" in text
    assert "Running as root" in text


def test_smoke_test_checks_healthcheck_and_startup_output():
    text = SMOKE.read_text()
    assert "/healthz" in text
    assert "State.Health.Status" in text
    assert "Version: smoke" in text


def test_smoke_test_verifies_auth_rejects_unauthenticated_requests():
    """
    Guards the feature that would be silently absent if PyJWT or its crypto
    dependency were missing from the image.
    """
    text = SMOKE.read_text()
    assert "AUTH_ISSUER" in text
    assert '= "401"' in text
    assert "Cf-Access-Jwt-Assertion" in text


def test_smoke_test_asserts_development_files_are_absent():
    text = SMOKE.read_text()
    assert "backup" in text
    assert "cleanup_utility" in text


def test_smoke_test_checks_artwork_links_in_the_built_image():
    """Local calendar validation cannot tell if Docker COPY preserved symlinks."""
    text = SMOKE.read_text()
    assert "test -L" in text
    assert "utils.validate_calendar" in text
    assert "assets/external/2025" in text


def test_calendar_check_runs_when_artwork_changes():
    """A changed image/link can break a calendar without editing its JSON."""
    triggers = load(WORKFLOWS / "calendar-check.yml")
    pull_request = (triggers.get("on") or triggers.get(True))["pull_request"]
    assert "assets/**" in pull_request["paths"]


# ---------------------------------------------------------------------------
# Release automation (item 7)
# ---------------------------------------------------------------------------

def test_release_workflow_runs_on_a_version_tag():
    data = load(RELEASE)
    triggers = data.get("on") or data.get(True)
    assert "[0-9]*.[0-9]*.[0-9]*" in str(triggers)


def test_release_notes_are_generated_not_hand_written():
    """A hand-written body would silently omit merged changes."""
    text = RELEASE.read_text()
    assert "generate-notes" in text
    # generate-notes is POST; gh api defaults to GET and would 404.
    assert "--method POST" in text


def test_release_notes_use_the_categorised_config():
    text = RELEASE.read_text()
    assert "configuration_file_path=.github/release.yml" in text
    assert RELEASE_CONFIG.is_file()


def test_release_config_categorises_by_label():
    config = load(RELEASE_CONFIG)
    categories = config["changelog"]["categories"]
    assert any(c["title"] == "Security" for c in categories)
    # Everything unlabelled must still appear somewhere.
    assert any("*" in c.get("labels", []) for c in categories)


def test_release_validates_the_tag_format():
    """
    'V.2.2.1' did not match the container workflow's semver patterns, so it
    produced no :2.2 and no :latest. Catching it at release time is cheaper than
    noticing an untagged image later.
    """
    text = RELEASE.read_text()
    assert "^[0-9]+" in text
    assert "Bad tag format" in text


def test_release_publishes_and_updates_existing_releases():
    text = RELEASE.read_text()
    assert "gh release create" in text
    assert "gh release edit" in text, "re-running must not fail on an existing release"


def test_release_workflow_requests_pull_requests_write():
    """Opening a release-notes PR needs pull-requests: write on the token."""
    data = load(RELEASE)
    perms = data.get("permissions") or {}
    assert perms.get("pull-requests") == "write", perms
    assert perms.get("contents") == "write", perms


def test_release_commits_to_a_branch_never_main():
    """The notes file is committed to a release-notes branch, never pushed
    directly to main."""
    text = RELEASE.read_text()
    assert "release-notes/v" in text, "no release-notes branch target"
    assert "${{ steps.version.outputs.version }}" in text, "branch name must derive from the tag"
    assert "git push origin main" not in text
    assert "git push -u origin main" not in text


def test_release_notes_filename_comes_from_the_tag():
    """The committed file name matches RELEASE_NOTES_v<version>.md where the
    version is taken from the tag, never hard-coded."""
    text = RELEASE.read_text()
    assert 'file="RELEASE_NOTES_v${{ steps.version.outputs.version }}.md"' in text
    assert "RELEASE_NOTES_v${{ steps.version.outputs.version }}.md" in text
    assert "RELEASE_NOTES_v2.5.0" not in text


def test_artifact_path_and_committed_path_are_the_same_file():
    text = RELEASE.read_text()
    committed = 'RELEASE_NOTES_v${{ steps.version.outputs.version }}.md'
    assert committed in text
    assert "path: RELEASE_NOTES_v*.md" in text
    assert "steps.writeup.outputs.file" in text


def test_release_pr_is_idempotent_on_existing_branch_and_pr():
    """Re-runs must update the existing branch/PR instead of failing."""
    text = RELEASE.read_text()
    assert 'git fetch origin "$branch"' in text
    assert 'git checkout -B "$branch"' in text
    assert "gh pr list" in text
    assert "gh pr edit" in text
    assert "gh pr create" in text


def test_docker_publish_tag_patterns_agree_with_the_release_tag_format():
    """
    The two workflows must agree on what a version tag looks like, or the release
    is published and the image is not.
    """
    publish = PUBLISH.read_text()
    assert "type=semver,pattern={{version}}" in publish
    assert "type=semver,pattern={{major}}.{{minor}}" in publish


# ---------------------------------------------------------------------------
# PR label hygiene (release-notes defect)
# ---------------------------------------------------------------------------

LABEL_CHECK = WORKFLOWS / "pr-label-check.yml"


def test_pr_label_check_workflow_exists_and_runs_on_pr_to_main():
    """
    Unlabelled PRs produce a single \"Other changes\" line. A cheap check that
    fails when a PR to main has no labels keeps the next release body
    categorised. This is a release-notes defect, not housekeeping.
    """
    assert LABEL_CHECK.is_file(), "pr-label-check.yml must exist"
    data = load(LABEL_CHECK)
    triggers = data.get("on") or data.get(True)
    # Must run on pull_request targeting main.
    assert "pull_request" in triggers, f"must trigger on pull_request: {triggers}"
    pr_trigger = triggers["pull_request"]
    # pr_trigger can be a dict with branches/types or a list; normalise to string.
    assert "main" in str(pr_trigger), f"must target main: {pr_trigger}"
    # Must have a job that actually checks labels.
    jobs = data.get("jobs", {})
    assert jobs, "no jobs in pr-label-check.yml"
    # At least one job step must mention labels and fail on zero.
    text = LABEL_CHECK.read_text()
    assert "pull_request.labels" in text or "PR_LABELS" in text, "must read PR labels from context"
    assert "Missing PR label" in text or "no labels" in text.lower(), "must error when labels are missing"


def test_pr_label_check_uses_minimal_permissions():
    data = load(LABEL_CHECK)
    perms = data.get("permissions") or {}
    # Must not request write; label check is read-only.
    assert perms.get("contents") == "read", perms
    # pull-requests: read is enough to read labels; write would be excessive.
    assert perms.get("pull-requests") == "read", perms
    assert perms.get("contents") != "write"
    assert perms.get("pull-requests") != "write"


def test_pr_label_check_does_not_need_a_checkout_or_js_action():
    """
    The cheapest check needs no checkout and no JavaScript action — it runs
    with just the GitHub context and shell. Using an action would reintroduce
    the Node 20 deprecation surface this change is removing.
    """
    text = LABEL_CHECK.read_text()
    # Must not pull the repo; labels are in the event payload.
    assert "actions/checkout" not in text, "label check should not need checkout"
    # Ensure the workflow doesn't itself depend on a deprecated JS action.
    # It should be pure shell + jq.
    assert "jq" in text, "label check uses jq to count labels"


def test_release_notes_step_fails_when_no_pull_request_exists():
    """
    v2.6.0 and v2.5.6 both had a green release job with no release-notes PR:
    the `gh pr create` failure was swallowed by `|| echo`, so the job stayed
    green while the notes never reached main. The step must fail if the PR is
    missing, and the push must not be allowed to fail silently either.
    """
    text = RELEASE.read_text()
    assert "gh pr create" in text
    assert "gh pr edit" in text
    assert "Release-notes PR missing" in text
    assert "exit 1" in text
    # The exact swallows that caused the defect must be gone.
    assert 'gh pr create --title "$title" --body "$body" --head "$branch" --base main ||' not in text
    assert 'gh pr edit "$existing" --title "$title" --body "$body" ||' not in text
    assert 'git push origin "$branch" || true' not in text


def test_release_notes_commit_step_handles_an_existing_branch():
    """
    A re-run must fetch the existing release-notes branch and build on it.
    The v2.7.0 re-run failed because the step created a new branch from
    origin/main and the push was rejected as non-fast-forward.
    """
    text = RELEASE.read_text()
    assert 'git fetch origin "$branch"' in text
    assert 'git checkout -B "$branch" FETCH_HEAD' in text
    assert 'git checkout -B "$branch" origin/main' in text


def test_calendar_validation_is_not_a_per_series_loop():
    """
    The workflows used to loop `for calendar in config/motogp_*.json` and strip
    the prefix with sed, which gives the validator a MotoGP-shaped season
    argument. Adding an F1 file to that glob passes "f1" as a season and fails.

    The validator discovers its own input from series/, so the correct CI step
    is one invocation with no loop, and it is the same command in every
    workflow. This test exists because the f1_*.json glob was added while the
    loop was silently left behind, and only CI noticed.
    """
    commands = []
    for name in ("calendar-check.yml", "docker-publish.yml"):
        data = load(WORKFLOWS / name)
        for step in data["jobs"].get("validate", data["jobs"].get("test", {})).get("steps", []):
            if "alendar" in step.get("name", "") and "alidate" in step.get("name", ""):
                commands.append((name, step.get("run", "")))

    assert commands, "no calendar validation step found"
    for name, run in commands:
        assert "utils/validate_calendar.py" in run, name
        assert "for calendar in" not in run, (
            f"{name} still loops over calendar files; the validator discovers "
            f"them per series itself"
        )
        assert "sed 's/motogp_//'" not in run, name
