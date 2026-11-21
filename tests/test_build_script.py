"""scripts/build.sh, run in a copy of the repository layout with a fake `docker` and `git` that only log their calls."""
import shutil
import subprocess

import pytest
from ci_workflow import ROOT

# Functions, not executables: pytest's temporary folder may be mounted noexec. BASH_ENV loads them into the script.
FAKES = """
docker() {
    echo "docker $*" >> "$CALLS"
    echo "env ${IMAGE_REPO:-} ${APP_VERSION:-} ${HOST_UID:-}:${HOST_GID:-}" >> "$CALLS.env"
    if [[ $1 == compose && $2 == run ]]; then
        mkdir -p dist && echo launcher > dist/vacation-memories
    fi
    if [[ $1 == image && $2 == ls ]]; then
        printf '%s\\n' ${IMAGES:-}
    fi
}
git() {
    case $* in
        "rev-parse --short=12 HEAD") echo abcdef123456 ;;
        "rev-parse HEAD") echo abcdef1234567890 ;;
        "remote get-url origin") echo https://example.test/repo ;;
    esac
}
"""


@pytest.fixture
def sandbox(tmp_path):
    (tmp_path / "scripts").mkdir()
    for name in ("build.sh", "ci_image_tags.sh"):
        shutil.copy(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    (tmp_path / "fakes.sh").write_text(FAKES)
    return tmp_path


def build(sandbox, *args, env=None):
    environment = {"PATH": "/usr/bin:/bin", "BASH_ENV": str(sandbox / "fakes.sh"), "CALLS": str(sandbox / "calls.log"),
                   **(env or {})}
    result = subprocess.run(["bash", str(sandbox / "scripts" / "build.sh"), *args], cwd=sandbox, env=environment,
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    calls = (sandbox / "calls.log").read_text().splitlines() if (sandbox / "calls.log").exists() else []
    return result, calls


def test_a_release_builds_the_image_with_every_tag_and_the_launcher(sandbox):
    result, calls = build(sandbox, "someone", "v1.2.3")
    assert result.returncode == 0, result.stderr
    image = "someone/vacation-memories"
    build_call = calls[0]
    assert build_call.startswith("docker build --target runtime --build-arg APP_VERSION=1.2.3 ")
    for tag in ("1.2.3", "1.2", "1", "latest", "sha-abcdef123456"):
        assert f"-t {image}:{tag}" in build_call
    assert "org.opencontainers.image.revision=abcdef1234567890" in build_call
    assert "org.opencontainers.image.source=https://example.test/repo" in build_call
    assert calls[1:] == ["docker compose --profile dev build launcher-build", "docker compose run --rm -T launcher-build"]
    launcher = sandbox / "dist" / "vacation-memories-1.2.3-linux-x86_64"
    assert launcher.read_text() == "launcher\n" and not (sandbox / "dist" / "vacation-memories").exists()
    assert (sandbox / "dist" / f"{launcher.name}.sha256").read_text().endswith(f"  {launcher.name}\n")


def test_nothing_is_pushed_without_the_flag(sandbox):
    _, calls = build(sandbox, "someone", "v1.2.3")
    assert not [call for call in calls if call.startswith("docker push")]


def test_push_sends_every_tag_after_the_build(sandbox):
    result, calls = build(sandbox, "someone", "v1.2.3", "--push", "--skip-launcher")
    assert result.returncode == 0, result.stderr
    pushed = [call.removeprefix("docker push ") for call in calls if call.startswith("docker push")]
    assert pushed == [f"someone/vacation-memories:{tag}" for tag in ("1.2.3", "1.2", "1", "latest", "sha-abcdef123456")]
    assert calls[0].startswith("docker build") and len(calls) == 6


def test_a_pre_release_never_moves_latest(sandbox):
    _, calls = build(sandbox, "someone", "v1.2.3-rc1", "--skip-launcher")
    assert "APP_VERSION=1.2.3-rc1" in calls[0] and ":latest" not in calls[0] and "vacation-memories:1.2 " not in calls[0]


def test_a_branch_gets_its_name_and_the_launcher_points_to_it(sandbox):
    result, calls = build(sandbox, "someone", "feature/New_UI")
    assert result.returncode == 0, result.stderr
    assert "APP_VERSION=feature-new_ui" in calls[0] and "-t someone/vacation-memories:feature-new_ui" in calls[0]
    assert (sandbox / "dist" / "vacation-memories-feature-new_ui-linux-x86_64").exists()


def test_skip_image_only_builds_the_launcher_and_skip_launcher_only_the_image(sandbox):
    _, calls = build(sandbox, "someone", "v1.2.3", "--skip-image", "--push")
    assert calls == ["docker compose --profile dev build launcher-build", "docker compose run --rm -T launcher-build"]
    (sandbox / "calls.log").unlink()
    shutil.rmtree(sandbox / "dist")
    _, calls = build(sandbox, "someone", "v1.2.3", "--skip-launcher")
    assert len(calls) == 1 and calls[0].startswith("docker build") and not (sandbox / "dist").exists()


def test_the_launcher_is_built_for_the_image_and_version(sandbox):
    build(sandbox, "someone", "v1.2.3", "--skip-image", env={"HOST_UID": "1234", "HOST_GID": "5678"})
    assert "env someone/vacation-memories 1.2.3 1234:5678" in (sandbox / "calls.log.env").read_text().splitlines()


def test_the_github_outputs_are_written(sandbox):
    output = sandbox / "github_output"
    result, _ = build(sandbox, "someone", "v1.2.3", env={"GITHUB_OUTPUT": str(output)})
    assert result.returncode == 0, result.stderr
    text = output.read_text()
    assert "version=1.2.3\n" in text and "launcher=vacation-memories-1.2.3-linux-x86_64\n" in text
    assert "tags<<EOF\nsomeone/vacation-memories:1.2.3\n" in text


@pytest.mark.parametrize("args, code", [(["someone"], 2), ([], 2), (["a", "b", "c"], 2), (["someone", "v1.2.3", "--nope"], 2),
                                        (["Some One", "v1.2.3"], 1), (["someone", "latest"], 1)])
def test_bad_arguments_are_refused_before_anything_runs(sandbox, args, code):
    result, calls = build(sandbox, *args)
    assert result.returncode == code and calls == []
    assert result.stderr


LS = "docker image ls --format {{.Repository}}:{{.Tag}} --filter reference="


@pytest.mark.parametrize("flags, pattern", [(["--clean-by-ns", "someone"], "someone/vacation-memories:*"),
                                            (["--clean-by-version", "1.2.3"], "*/vacation-memories:1.2.3"),
                                            (["--clean-by-version", "1.2.3", "--clean-by-ns", "someone"],
                                             "someone/vacation-memories:1.2.3")])
def test_clean_removes_the_images_matching_the_pattern_and_builds_nothing(sandbox, flags, pattern):
    result, calls = build(sandbox, *flags, "--yes", env={"IMAGES": "someone/vacation-memories:1.2.3 someone/vacation-memories:1"})
    assert result.returncode == 0, result.stderr
    assert calls == [LS + pattern, "docker rmi someone/vacation-memories:1.2.3 someone/vacation-memories:1"]


def test_clean_with_no_match_removes_nothing(sandbox):
    result, calls = build(sandbox, "--clean-by-ns", "someone", "--yes")
    assert result.returncode == 0 and "no image matches" in result.stdout
    assert [call for call in calls if call.startswith("docker rmi")] == []


def test_clean_without_a_terminal_needs_yes(sandbox):
    result, calls = build(sandbox, "--clean-by-ns", "someone", env={"IMAGES": "someone/vacation-memories:1"})
    assert result.returncode == 1 and "--yes" in result.stderr
    assert [call for call in calls if call.startswith("docker rmi")] == []


@pytest.mark.parametrize("args", [["--clean-by-ns"], ["--clean-by-ns", "some*"], ["--clean-by-version", "1.?"],
                                  ["--clean-by-version", "[1]"], ["--clean-by-ns", "someone", "someone", "v1.2.3"],
                                  ["--clean-by-ns", "someone", "--push"]])
def test_bad_clean_arguments_are_refused_before_anything_runs(sandbox, args):
    result, calls = build(sandbox, *args, "--yes")
    assert result.returncode == 2 and calls == []
    assert result.stderr


def test_help_prints_the_header(sandbox):
    result, calls = build(sandbox, "--help")
    assert result.returncode == 0 and "Usage: scripts/build.sh NAMESPACE TAG" in result.stdout and calls == []
