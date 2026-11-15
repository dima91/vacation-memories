"""The image tags that CI publishes on a release (scripts/ci_image_tags.sh) and the job that uses them."""
import re
import subprocess

import pytest
from ci_workflow import ROOT, job_block, run_scripts

SCRIPT = ROOT / "scripts" / "ci_image_tags.sh"


def tags(image, tag):
    return subprocess.run(["bash", str(SCRIPT), image, tag], capture_output=True, text=True, stdin=subprocess.DEVNULL)


def test_a_release_gets_its_version_and_latest():
    result = tags("someone/vacation-memories", "v0.2.0")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["someone/vacation-memories:0.2.0", "someone/vacation-memories:latest"]


@pytest.mark.parametrize("tag, version", [("v0.2.0-rc1", "0.2.0-rc1"), ("v1.0.0-beta.2", "1.0.0-beta.2")])
def test_a_pre_release_never_gets_latest(tag, version):
    result = tags("someone/vacation-memories", tag)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f"someone/vacation-memories:{version}"]


def test_the_name_may_have_dots_underscores_and_dashes():
    assert tags("some.one/vacation_memories-app", "v10.20.30").returncode == 0


@pytest.mark.parametrize("image", [
    "",
    "vacation-memories",                      # no namespace
    "Someone/vacation-memories",              # uppercase
    "someone/Vacation-Memories",
    "someone/vacation-memories:latest",       # the version comes from the tag only
    "docker.io/someone/vacation-memories",    # no registry prefix
    "ghcr.io/someone/vacation-memories",
    "someone/vacation memories",
    "someone//vacation-memories",
    "/vacation-memories",
    "someone/",
    "someone/-vacation",
])
def test_an_unusable_image_name_is_refused(image):
    result = tags(image, "v0.2.0")
    assert result.returncode == 1 and result.stdout == ""
    assert "IMAGE_NAME" in result.stderr


@pytest.mark.parametrize("tag", ["", "0.2.0", "vfoo", "v1", "v1.2", "v01.2.3", "v1.2.3-", "v1.2.3 ", "v1.2.3;rm", "latest"])
def test_an_unusable_tag_is_refused(tag):
    result = tags("someone/vacation-memories", tag)
    assert result.returncode == 1 and result.stdout == ""
    assert "tag must look like" in result.stderr


def test_the_script_needs_both_arguments():
    result = subprocess.run(["bash", str(SCRIPT), "someone/vacation-memories"], capture_output=True, text=True)
    assert result.returncode == 2 and "usage" in result.stderr


# --- the workflow (plain text: there is no YAML library in the dev image) ---


def test_the_publish_job_runs_only_on_a_release_tag_after_both_test_jobs():
    block = job_block("publish-image")
    assert "if: startsWith(github.ref, 'refs/tags/v')" in block
    needs = re.search(r"^    needs: \[(.*)\]$", block, re.M)
    assert needs and sorted(part.strip() for part in needs.group(1).split(",")) == ["container-tests", "tests"]


def test_the_publish_job_builds_the_runtime_target_and_pushes_the_computed_tags():
    block = job_block("publish-image")
    assert "target: runtime" in block
    assert "push: true" in block
    assert "tags: ${{ steps.tags.outputs.tags }}" in block
    assert "APP_VERSION=" in block
    assert "scripts/ci_image_tags.sh" in block
    assert "docker/login-action@v3" in block and "docker/build-push-action@v6" in block


def test_the_image_name_and_the_credentials_come_from_the_repository_settings():
    block = job_block("publish-image")
    assert "IMAGE_NAME: ${{ vars.IMAGE_NAME }}" in block
    assert "username: ${{ secrets.DOCKERHUB_USERNAME }}" in block
    assert "password: ${{ secrets.DOCKERHUB_TOKEN }}" in block


def test_user_controlled_values_do_not_reach_the_shell_through_expressions():
    """`run:` steps read the tag and the image name from the environment, never from `${{ ... }}` in the script."""
    for run in run_scripts(job_block("publish-image")):
        assert "${{" not in run, run


def test_the_publish_job_has_read_only_repository_access_and_a_timeout():
    block = job_block("publish-image")
    assert re.search(r"^    permissions:\n      contents: read$", block, re.M)
    assert re.search(r"^    timeout-minutes: \d+$", block, re.M)
