"""The release notes taken from CHANGELOG.md (scripts/ci_release_notes.sh) and the `release` job that publishes them."""
import re
import subprocess

import pytest
from ci_workflow import ROOT, job_block, run_scripts, workflow_text

SCRIPT = ROOT / "scripts" / "ci_release_notes.sh"

CHANGELOG = """# Changelog

## [Unreleased]

### Added

- Something not released yet.

## [0.3.0] - 2026-12-01

Intro line.

### Added

- Feature A.
- Feature B.

### Fixed

- Bug C.

## [0.3.0-rc1] - 2026-11-20

- Candidate notes.

## [0.2.0] - 2026-11-13

### Added

- Old feature.

## [0.1.0] - 2026-11-01

### Changed

## [0.0.9] - 2026-10-01
"""


def notes(version, changelog=None, tmp_path=None):
    arguments = ["bash", str(SCRIPT), version]
    if changelog is not None:
        path = tmp_path / "CHANGELOG.md"
        path.write_text(changelog, encoding="utf-8")
        arguments.append(str(path))
    return subprocess.run(arguments, capture_output=True, text=True, stdin=subprocess.DEVNULL)


def test_the_section_of_the_version_is_returned_without_its_heading(tmp_path):
    result = notes("0.3.0", CHANGELOG, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Intro line.\n")
    assert "- Feature A.\n- Feature B." in result.stdout and "- Bug C." in result.stdout
    assert "## [" not in result.stdout


def test_it_stops_at_the_next_section_and_never_uses_unreleased(tmp_path):
    output = notes("0.3.0", CHANGELOG, tmp_path).stdout
    assert "Candidate notes" not in output and "Something not released yet" not in output
    assert output.endswith("- Bug C.\n")


def test_a_pre_release_has_its_own_section(tmp_path):
    result = notes("0.3.0-rc1", CHANGELOG, tmp_path)
    assert result.returncode == 0 and result.stdout == "- Candidate notes.\n"


def test_a_version_is_not_confused_with_a_longer_one(tmp_path):
    changelog = "## [0.3.0-rc1] - d\n\n- rc notes\n\n## [0.3.0] - d\n\n- final notes\n"
    assert notes("0.3.0", changelog, tmp_path).stdout == "- final notes\n"
    assert notes("0.3", changelog, tmp_path).returncode == 1


def test_the_last_section_of_the_file_is_read(tmp_path):
    assert notes("0.2.0", "## [0.2.0] - d\n\n- last one", tmp_path).stdout == "- last one\n"


def test_a_missing_section_is_an_error_that_names_the_fix(tmp_path):
    result = notes("0.4.0", CHANGELOG, tmp_path)
    assert result.returncode == 1 and result.stdout == ""
    assert "no section '## [0.4.0]'" in result.stderr and "Unreleased" in result.stderr


def test_a_section_with_only_headings_is_an_error(tmp_path):
    for version in ("0.1.0", "0.0.9"):
        result = notes(version, CHANGELOG, tmp_path)
        assert result.returncode == 1 and "no content" in result.stderr


@pytest.mark.parametrize("version", ["", "v0.3.0", "0.3", "1.2.3-", "latest", "0.3.0 ", "01.2.3"])
def test_an_unusable_version_is_an_error(version, tmp_path):
    result = notes(version, CHANGELOG, tmp_path)
    assert result.returncode == 1 and "version must look like" in result.stderr


def test_a_missing_changelog_is_an_error(tmp_path):
    result = subprocess.run(["bash", str(SCRIPT), "0.3.0", str(tmp_path / "nope.md")], capture_output=True, text=True)
    assert result.returncode == 1 and "changelog not found" in result.stderr


def test_the_script_needs_a_version():
    assert subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True).returncode == 2


def test_the_real_changelog_has_the_first_release():
    result = subprocess.run(["bash", str(SCRIPT), "0.1.0"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("First release.")


# --- the workflow (plain text) ---


def test_the_release_job_comes_after_the_image_and_runs_only_on_a_release_tag():
    block = job_block("release")
    assert "if: startsWith(github.ref, 'refs/tags/v')" in block
    assert re.search(r"^    needs: \[publish-image\]$", block, re.M)
    assert re.search(r"^    timeout-minutes: \d+$", block, re.M)


def test_only_the_release_job_can_write_to_the_repository():
    text = workflow_text()
    assert text.count("contents: write") == 1
    assert "contents: write" in job_block("release")
    assert re.search(r"^permissions:\n  contents: read$", text, re.M)
    for name in ("tests", "container-tests", "publish-image"):
        assert "write" not in job_block(name).split("steps:")[0], name


def test_the_changelog_is_checked_before_anything_is_published():
    block = job_block("tests")
    step = re.search(r"- name: Check that the changelog describes the release\n(?P<body>(?:        .*\n)+)", block)
    assert step, "the tests job does not check the changelog"
    assert "if: startsWith(github.ref, 'refs/tags/v')" in step.group("body")
    assert 'scripts/ci_release_notes.sh "${GITHUB_REF_NAME#v}"' in step.group("body")
    assert "publish-image" not in block


def test_the_launcher_is_built_for_the_image_that_was_pushed():
    block = job_block("release")
    assert "IMAGE_NAME: ${{ vars.IMAGE_NAME }}" in block
    assert 'scripts/build.sh "${IMAGE_NAME%%/*}" "$TAG" --skip-image' in block


def test_the_release_attaches_the_files_named_with_the_version_and_their_checksum():
    block = job_block("release")
    assert "name=vacation-memories-$version-linux-x86_64" in block
    assert block.count('"dist/$name" "dist/$name.sha256"') == 1


def test_the_release_uses_the_changelog_notes_and_marks_pre_releases():
    block = job_block("release")
    assert 'scripts/ci_release_notes.sh "${TAG#v}" > notes.md' in block
    assert "GH_TOKEN: ${{ github.token }}" in block
    assert '[[ $version == *-* ]] && prerelease=--prerelease' in block
    assert "--prerelease=false" in block
    assert 'gh release create "$TAG"' in block and "--notes-file notes.md" in block and "--verify-tag" in block
    assert 'gh release edit "$TAG"' in block and 'gh release upload "$TAG"' in block and "--clobber" in block


def test_user_controlled_values_do_not_reach_the_shell_through_expressions_in_the_release_job():
    scripts = run_scripts(job_block("release"))
    assert len(scripts) >= 4
    for run in scripts:
        assert "${{" not in run, run
