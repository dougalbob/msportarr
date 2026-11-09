"""
Release-readiness checks (Candidate B of docs/IMPROVEMENT_PLAN.md).

During v2.11.0 the version tag was pushed before the Unraid template's pinned
`TemplateURL` had been bumped to it. The template test caught the mismatch -
correctly - and a follow-up PR plus a rebuild were needed before the release
worked. The hazard is ordering, so what is tested here is the ordering:

* the pre-flight script fails for every wrong state and passes for the right
  one (a check that cannot fail is worse than no check);
* `release.yml` refuses to publish a tag whose own tree does not pin it, and
  refuses to call the release shipped until the image tags resolve;
* the readiness workflow runs on every PR to main, so the failure is a red PR
  rather than a half-made release.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_release_readiness.py"
RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"
PREP = REPO_ROOT / ".github" / "workflows" / "release-prep.yml"
TEMPLATE = REPO_ROOT / "unraid" / "msportarr.xml"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_release_readiness as readiness  # noqa: E402


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def current_pin() -> str:
    return readiness.template_pin(TEMPLATE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The script's decisions
# ---------------------------------------------------------------------------

def test_a_valid_version_is_accepted():
    report = readiness.Report()
    assert readiness.check_version("1.0.0", report) is True
    assert report.ok


@pytest.mark.parametrize("bad", ["v2.12.0", "v1.0.0", "V.2.2.1", "1.0", "1.0.0-rc1", "latest", ""])
def test_anything_but_XYZ_is_rejected(bad):
    """
    `V.2.2.1` matched nothing in the container workflow's semver patterns and
    shipped images without a :2.2 tag, and `v*` tags belong to the legacy
    motogparr series. That is the failure this pins.
    """
    report = readiness.Report()
    assert readiness.check_version(bad, report) is False
    assert not report.ok
    assert "not X.Y.Z" in report.failures[0]


def test_a_matching_template_pin_passes():
    report = readiness.Report()
    assert readiness.check_template_pin(
        TEMPLATE.read_text(encoding="utf-8"), current_pin(), report
    ) is True
    assert report.ok


def test_a_stale_template_pin_fails_and_names_the_order_to_follow():
    report = readiness.Report()
    text = TEMPLATE.read_text(encoding="utf-8")
    assert readiness.check_template_pin(text, "99.0.0", report) is False
    message = report.failures[0]
    # The message has to say what to do, not just that something is wrong.
    assert "merge that PR first" in message
    assert "Tagging first" in message


def test_a_floating_template_url_is_not_a_pin():
    report = readiness.Report()
    text = (
        "<Container><TemplateURL>https://raw.githubusercontent.com/dougalbob/"
        "msportarr/main/unraid/msportarr.xml</TemplateURL></Container>"
    )
    assert readiness.template_pin(text) == "main"
    # `main` is a pin that matches no version, so preparing any version fails.
    assert readiness.check_template_pin(text, "1.0.0", report) is False


def test_a_template_without_the_expected_url_fails():
    report = readiness.Report()
    assert readiness.check_template_pin("<Container/>", "1.0.0", report) is False
    assert "no TemplateURL" in report.failures[0]


def test_a_missing_dockerfile_copy_source_fails(tmp_path):
    """
    The Dockerfile copies an explicit list. A renamed module leaves a stale
    line, and the image builds and then fails at import time.
    """
    (tmp_path / "Dockerfile").write_text(
        "COPY motogp_webui.py .\nCOPY motogp_gone.py .\n"
    )
    (tmp_path / "motogp_webui.py").write_text("")

    report = readiness.Report()
    assert readiness.check_copy_sources(tmp_path, report) is False
    assert "motogp_gone.py" in report.failures[0]


def test_dockerfile_copy_parsing_ignores_flags_and_finds_real_paths():
    sources = readiness.dockerfile_copy_sources(
        "COPY --chown=99:100 utils/ ./utils/\nCOPY motogp_common.py .\nRUN echo hi\n"
    )
    assert sources == ["utils/", "motogp_common.py"]


def test_an_already_published_tag_is_refused():
    """
    Repairing a failed check by moving the tag is the workaround this forbids:
    the published image would come from a tree the pre-flight never saw.
    """
    report = readiness.Report()
    assert readiness.check_tag_is_new("1.1.0", ["1.0.0", "1.1.0"], report) is False
    assert "do not move the tag" in report.failures[0]


def test_an_unreadable_remote_is_a_warning_not_a_pass_silently():
    """
    With no git remote the check cannot answer. It must say so - a silent pass
    would claim a verification that never ran.
    """
    report = readiness.Report()
    assert readiness.check_tag_is_new("1.0.0", None, report) is True
    assert report.warnings and "Skipping" in report.warnings[0]


def test_the_repository_as_committed_is_ready_for_its_pinned_version():
    """
    The end-to-end case CI runs on every PR: whatever version the template
    pins must pass, once the tag does not exist yet.
    """
    report = readiness.run(current_pin(), REPO_ROOT)
    # The pin is the version being prepared, so the template check must pass.
    assert not any("TemplateURL" in failure for failure in report.failures), report.render()


def test_check_mode_exits_zero_for_an_unreleased_next_version(tmp_path, capsys):
    """The maintainer's pre-flight stays strict; CI's question is a different one."""
    template = tmp_path / "unraid"
    template.mkdir()
    (template / "msportarr.xml").write_text(
        "<Container><TemplateURL>https://raw.githubusercontent.com/dougalbob/"
        "msportarr/9.9.9/unraid/msportarr.xml</TemplateURL></Container>"
    )
    (tmp_path / "Dockerfile").write_text("")

    code = readiness.main(
        ["--mode", "check", "--version", "9.9.9", "--repo-root", str(tmp_path)]
    )
    assert code == 0


def test_the_script_exits_non_zero_and_annotates_on_failure(tmp_path, capsys):
    """
    The ::error:: line is what survives to a check-run annotation, which is the
    only part of a CI log reachable from the sandbox. A bare exit 1 is not
    diagnosable.
    """
    code = readiness.main(["--version", "nope", "--repo-root", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert "::error title=Release not ready::" in captured.err


def test_the_script_runs_as_a_subprocess():
    """It is invoked by CI as a script, so importing it is not enough."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--version", "nope", "--repo-root", str(REPO_ROOT)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "Release not ready" in result.stderr


# ---------------------------------------------------------------------------
# The workflows
# ---------------------------------------------------------------------------

def test_release_publishing_refuses_a_tag_its_tree_does_not_pin():
    text = RELEASE.read_text(encoding="utf-8")
    assert "The tagged tree must pin its own tag" in text
    assert "Template pin does not match the tag" in text
    # And it must be before the publish step, or it is a comment, not a gate.
    assert text.index("The tagged tree must pin its own tag") < text.index(
        "Publish the release"
    )


def test_release_confirms_the_image_tags_before_calling_it_shipped():
    """
    "Green release job" is not "the release shipped": users run the image.
    The step must poll the package tags for both the version and :latest,
    and must fail when either is missing.
    """
    data = load(RELEASE)
    names = [s.get("name") or s.get("uses") for s in data["jobs"]["release"]["steps"]]

    check = next(i for i, n in enumerate(names)
                 if n and "Confirm the container image" in n)
    publish = next(i for i, n in enumerate(names) if n == "Publish the release")
    explain = next(i for i, n in enumerate(names) if n == "Explain where the file went")

    # After the release is published (so it can report on it) and before the
    # summary step that claims success.
    assert publish < check < explain

    text = RELEASE.read_text(encoding="utf-8")
    assert "packages/container" in text
    assert ':latest was not moved' in text
    assert "Image missing" in text
    # packages: read is required for that API call.
    assert load(RELEASE)["permissions"]["packages"] == "read"


def test_release_retry_stays_safe():
    """
    A re-run must not fail merely because the release or the PR already
    exists - v2.5.6/v2.6.0 had green jobs with no notes PR, and v2.7.0's
    re-run failed on a non-fast-forward push.
    """
    text = RELEASE.read_text(encoding="utf-8")
    assert 'if gh release view "$tag" >/dev/null 2>&1' in text
    assert "gh release edit" in text
    assert 'git fetch origin "$branch"' in text


def test_readiness_workflow_runs_on_every_pr_to_main():
    assert PREP.is_file(), "release-prep.yml must exist"
    triggers = load(PREP).get("on") or load(PREP).get(True)
    assert "pull_request" in triggers
    assert "main" in str(triggers["pull_request"])


def test_readiness_workflow_checks_the_pinned_version_and_tags_exist():
    text = PREP.read_text(encoding="utf-8")
    # Reads the version from the template rather than a second variable.
    assert "unraid/msportarr.xml" in text
    assert "check_release_readiness.py" in text
    # Tags must be fetched, or the "pinned version is published" notice is
    # never available.
    assert "git fetch --tags" in text


def test_readiness_workflow_runs_in_check_mode_not_prepare_mode():
    """
    Caught by this workflow's first real run: on an ordinary PR to main the
    template is pinned to the last release, so the prepare-mode "the tag must
    not exist yet" rule failed every PR. CI asks a different question from the
    maintainer, and the mode has to say so.
    """
    text = PREP.read_text(encoding="utf-8")
    assert "--mode check" in text
    assert "--mode prepare" not in text


def test_check_mode_accepts_an_already_published_pin():
    """
    The case that broke CI: the pin names a released tag. Prepare mode refuses
    it (re-tagging is the workaround being prevented); check mode accepts it,
    because that is what every install is supposed to fetch.
    """
    prepare = readiness.Report()
    assert readiness.check_tag_is_new("1.0.0", ["1.0.0"], prepare) is False

    check = readiness.Report()
    assert readiness.check_tag_exists("1.0.0", ["1.0.0"], check) is True
    assert check.ok
    assert "published tag" in check.notes[0]


def test_check_mode_warns_but_passes_for_the_unreleased_bump():
    """
    The bump PR pins a tag that does not exist until after its merge. Failing
    it would break the very sequence this protects; staying silent would claim
    a verification that did not happen.
    """
    check = readiness.Report()
    assert readiness.check_tag_exists("1.1.0", ["1.0.0"], check) is True
    assert check.ok
    assert check.warnings and "not on the remote yet" in check.warnings[0]


def test_the_current_tree_is_consistent_in_check_mode():
    """
    The end-to-end case CI runs on every PR, including the bump PR itself.

    A warning is allowed and expected: while the pin names a version whose tag
    has not been pushed yet - which is exactly the state of the *bump* PR - the
    unpublished-version notice fires. Asserting no warnings at all made this
    test fail the very PR it exists to support. Failure is what must not
    happen; the warning is how the notice reaches the maintainer.
    """
    report = readiness.run(current_pin(), REPO_ROOT, prepare=False)
    assert report.ok, report.render()
    assert all(
        "not on the remote yet" in warning for warning in report.warnings
    ), report.render()


def test_readiness_workflow_is_read_only_and_needs_no_docker():
    data = load(PREP)
    perms = data.get("permissions") or {}
    assert perms.get("contents") == "read", perms
    text = PREP.read_text(encoding="utf-8")
    # No image build: the point is a seconds-long check on every PR, not a
    # second container-smoke job.
    for action in ("docker/build-push-action", "docker run", "docker build"):
        assert action not in text, f"readiness must not {action}"



def test_the_release_checklist_documents_the_order():
    """
    The automation covers the mechanical part. The documented order is what a
    maintainer follows when a check fails, so it must name the same sequence.
    """
    plan = (REPO_ROOT / "docs" / "IMPROVEMENT_PLAN.md").read_text(encoding="utf-8")
    assert "Release checklist" in plan
    checklist = plan.split("Release checklist", 1)[1].lower()
    for phrase in ("templateurl", "tag the merged commit", ":latest"):
        assert phrase in checklist, f"release checklist is missing: {phrase}"
