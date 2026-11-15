#!/usr/bin/env bash
# Container tests: the images, the compose files and the full application, on synthetic media only.
#
# Run it on the development PC; it needs only the Docker CLI with the Compose plugin (2.24.4 or later).
# It never reads .env or the environment of the shell: a private env file points every folder to
# ./.test-docker (ignored by git), Ollama is replaced by a simulated server (compose.test.yaml) and the
# compose project has its own name, so nothing of the normal setup is touched.
#
# Usage: scripts/test-docker.sh [-k REGEX] [--keep]
#   -k REGEX  run only the checks whose name matches the (extended) regular expression
#   --keep    keep ./.test-docker (media, working folders, logs) at the end
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2
ROOT=$PWD
SCRATCH=$ROOT/.test-docker
SCRATCH_REL=./.test-docker
TEST_ENV=$SCRATCH/test.env
PROJECT=vacation-memories-test
IMAGE=vacation-memories:local
CYCLE_CONTAINER=$PROJECT-cycle
STOP_CONTAINER=$PROJECT-stop
STOP_SECONDS=10
FILTER=
KEEP=

while (($#)); do
    case $1 in
        -k) FILTER=${2:?-k needs a regular expression}; shift 2 ;;
        --keep) KEEP=1; shift ;;
        -h | --help) sed -n '2,/^set /p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# Whatever the shell exports must not leak into the tests: only the env file counts.
for variable in MEDIA_DIR MUSIC_DIR WORK_DIR HOST_UID HOST_GID OLLAMA_MODEL OLLAMA_VULKAN ASSUME_TZ APP_VERSION \
    COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES; do
    unset "$variable"
done

# --- helpers ---

dc() { docker compose --env-file "$TEST_ENV" "$@"; }
dev() { dc run --rm -T --no-deps dev "$@"; }
app() { dc run --rm -T app "$@"; }
# `app` is in the `cli` profile (D13 of UI-SPEC.md): the profile is on wherever a check looks at its configuration.
compose_example() { docker compose --env-file "$ROOT/.env.example" --profile dev --profile cli "$@"; }
compose_files() { # compose.yaml plus the leading *.yaml arguments, then the compose command; the test env file
    local arguments=(--env-file "$TEST_ENV" --profile dev --profile cli -f compose.yaml)
    while [[ ${1:-} == *.yaml ]]; do arguments+=(-f "$1"); shift; done
    docker compose "${arguments[@]}" "$@"
}

# The Python snippet sees the JSON of `docker compose config` as `c`. The rest of the arguments is the
# compose command (a function above plus its options) that comes before `config`.
config_assert() {
    local code=$1
    shift
    "$@" config --format json | dev python -c "import json, sys
c = json.load(sys.stdin)
$code"
}

stamp() { [[ -e $SCRATCH/.$1 ]]; }
mark() { touch "$SCRATCH/.$1"; }

failures=0
checks=0

run_check() { # name function: runs the function, shows its output only if it fails
    local name=$1 function=$2 output
    if [[ -n $FILTER && ! $name =~ $FILTER ]]; then return; fi
    checks=$((checks + 1))
    if output=$("$function" 2>&1); then
        printf 'ok    %s\n' "$name"
    else
        failures=$((failures + 1))
        printf 'FAIL  %s\n' "$name"
        printf '%s\n' "$output" | tail -n 40 | sed 's/^/        /'
    fi
}

# --- set-up, each step once, on demand ---

ensure_scratch() {
    stamp scratch && return 0
    mkdir -p "$SCRATCH/media" "$SCRATCH/music" "$SCRATCH/work" || return 1
    cat >"$TEST_ENV" <<EOF
MEDIA_DIR=$SCRATCH_REL/media
MUSIC_DIR=$SCRATCH_REL/music
WORK_DIR=$SCRATCH_REL/work
HOST_UID=$(id -u)
HOST_GID=$(id -g)
OLLAMA_MODEL=fake-vision:test
COMPOSE_FILE=compose.yaml:compose.test.yaml
COMPOSE_PROJECT_NAME=$PROJECT
EOF
    mark scratch
}

ensure_built() {
    ensure_scratch || return 1
    stamp built && return 0
    dc --profile dev build && mark built
}

ensure_media() {
    ensure_built || return 1
    stamp media && return 0
    dev python -m vacation_memories.evaluate.synthetic_media "$SCRATCH_REL/media/vacation" --vacation \
        && dev ffmpeg -nostdin -loglevel error -f lavfi -i "sine=frequency=330:duration=8" -ar 44100 -ac 2 \
            "$SCRATCH_REL/music/track.wav" \
        && (cd "$SCRATCH/media" && find . -type f -exec sha256sum {} + | sort -k2 >"$SCRATCH/media.sha256") \
        && mark media
}

ensure_ollama() {
    ensure_built || return 1
    dc up -d --wait ollama
}

# The full job, in a container that is kept (no --rm) so that it can be inspected afterwards.
ensure_cycle() {
    ensure_media || return 1
    ensure_ollama || return 1
    stamp cycle && return 0
    docker rm -f "$CYCLE_CONTAINER" >/dev/null 2>&1
    dc run -T --name "$CYCLE_CONTAINER" app --duration 20 --resolution 480p --music /music/track.wav \
        --output cycle.mp4 >"$SCRATCH/cycle.out" 2>&1
    echo $? >"$SCRATCH/cycle.exit"
    mark cycle
}

cleanup() {
    docker rm -f "$CYCLE_CONTAINER" "$STOP_CONTAINER" >/dev/null 2>&1
    if [[ -f $TEST_ENV ]]; then
        dc down --volumes --remove-orphans >/dev/null 2>&1
    fi
    if [[ -z $KEEP && -n $SCRATCH && -d $SCRATCH ]]; then rm -rf "$SCRATCH"; fi
}

# Python that checks a rendered video the way the specification asks for the final file: the report of
# the application, and an independent FFprobe: codec, audio track, height and duration.
# Arguments: workdir (relative to the repository), output name, requested seconds, height.
verify_video() {
    dev python - "$@" <<'PY'
import json, subprocess, sys
from pathlib import Path

workdir, name, requested, height = Path("/src") / sys.argv[1], sys.argv[2], float(sys.argv[3]), int(sys.argv[4])
report = json.loads((workdir / "render_report.json").read_text())
failed = [check for check in report["checks"] if not check["ok"]]
assert report["ok"] and not failed, f"render_report.json: {failed}"
probe = subprocess.run(
    ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(workdir / name)],
    capture_output=True, text=True, check=True,
)
info = json.loads(probe.stdout)
streams = {stream["codec_type"]: stream for stream in info["streams"]}
assert streams["video"]["codec_name"] == "h264", streams["video"]["codec_name"]
assert streams["video"]["height"] == height, streams["video"]["height"]
assert "audio" in streams, "no audio stream"
duration = float(info["format"]["duration"])
assert abs(duration - requested) <= 0.05 * requested, f"duration {duration:.1f} s, requested {requested:g} s"
print(f"{name}: {streams['video']['width']}x{streams['video']['height']}, {duration:.1f} s, audio {streams['audio']['codec_name']}")
PY
}

# --- checks: images and tests ---

check_build() { ensure_scratch && dc --profile dev build && mark built; }

check_pytest() {
    ensure_built || return 1
    dc stop ollama >/dev/null 2>&1 # the -m ollama tests would find the simulated server and run against it
    dc run --rm -T dev pytest -q
}

check_doctor_no_ai() {
    ensure_built || return 1
    local output
    output=$(dc run --rm --no-deps -T app doctor --no-ai) || { echo "$output"; return 1; }
    echo "$output"
    grep -Eq '^\[ OK \] ffmpeg-encoders: libx264, aac$' <<<"$output" || { echo "libx264 and aac not confirmed"; return 1; }
    grep -Eq '^\[ OK \] ffmpeg-filters: all [0-9]+ required filters present$' <<<"$output" \
        || { echo "filters not confirmed"; return 1; }
    grep -Eq '^\[ OK \] image: image version ' <<<"$output" || { echo "image version not printed"; return 1; }
    grep -q 'ollama: not checked' <<<"$output" || { echo "Ollama was checked"; return 1; }
    [[ -z $(dc ps --status running --services) ]] || { echo "services were started"; return 1; }
}

check_image_user_and_entrypoint() {
    ensure_built || return 1
    local user entrypoint command label version
    user=$(docker image inspect -f '{{.Config.User}}' "$IMAGE")
    [[ -n $user && $user != root && $user != 0 ]] || { echo "the image runs as '$user'"; return 1; }
    [[ $(docker run --rm --entrypoint id "$IMAGE" -u) != 0 ]] || { echo "the container runs as root"; return 1; }
    entrypoint=$(docker image inspect -f '{{json .Config.Entrypoint}}' "$IMAGE")
    [[ $entrypoint == '["python","-m","vacation_memories"]' ]] || { echo "entrypoint: $entrypoint"; return 1; }
    command=$(docker image inspect -f '{{json .Config.Cmd}}' "$IMAGE")
    [[ $command == null || $command == '[]' ]] || { echo "the image has a CMD: $command"; return 1; }
    label=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' "$IMAGE")
    version=$(docker run --rm --entrypoint printenv "$IMAGE" IMAGE_VERSION)
    [[ -n $label && $label == "$version" ]] || { echo "version label '$label', IMAGE_VERSION '$version'"; return 1; }
    echo "user $user, version $label, $(docker image inspect -f '{{.Size}}' "$IMAGE" | awk '{printf "%.0f MiB", $1 / 1048576}')"
}

check_image_content() {
    ensure_built || return 1
    docker run --rm -i --entrypoint python "$IMAGE" - <<'PY'
import importlib.metadata
import os
import shutil
import sys

problems = []

# Nothing of the repository, of the local configuration or of the media is in the image.
unwanted_names = {".git", ".env", "tests", "fixtures", "dev-media", "eval", "SPEC.md", "CLAUDE.md", "compose.yaml", "Dockerfile"}
for top in ("/", "/app", "/work", "/src", "/media", "/music", "/root", "/home", "/srv", "/tmp"):
    try:
        problems += [os.path.join(top, name) for name in os.listdir(top) if name in unwanted_names]
    except OSError:  # missing, or not readable by the image user (/root)
        pass
app_files = sorted(
    os.path.relpath(os.path.join(folder, name), "/app")
    for folder, _, names in os.walk("/app") if "__pycache__" not in folder for name in names
)
# Only code, and the built interface the editor serves (`editor/static/`, UI-SPEC.md section 4).
problems += [f"/app/{name}" for name in app_files
             if not name.endswith(".py") and not name.startswith("vacation_memories/editor/static/")]
if os.listdir("/work"):
    problems.append("/work is not empty")

# No media file anywhere outside the system and the packages.
media = (".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mov", ".mkv", ".mp3", ".wav", ".m4a")
skipped = ("/proc", "/sys", "/dev", "/usr", "/opt/venv", "/var/lib/dpkg", "/etc")
for folder, folders, names in os.walk("/"):
    folders[:] = [name for name in folders if not os.path.join(folder, name).startswith(skipped)]
    problems += [os.path.join(folder, name) for name in names if name.lower().endswith(media)]

# The packages in the venv: headless OpenCV only, no test or build tools, no compiler.
distributions = {d.metadata["Name"].lower() for d in importlib.metadata.distributions()}
if "opencv-python" in distributions:
    problems.append("opencv-python (not headless) is installed")
if "opencv-python-headless" not in distributions:
    problems.append("opencv-python-headless is missing")
problems += [f"{name} is installed" for name in ("pytest", "pip", "setuptools") if name in distributions]
problems += [f"{name} is in the image" for name in ("gcc", "cc", "make") if shutil.which(name)]
import cv2  # noqa: F401  (the headless build needs no graphical library)

if problems:
    print("\n".join(problems))
    sys.exit(1)
print(f"{len(app_files)} files in /app; opencv-python-headless only; no media, .git, .env, fixtures, tests or compilers")
PY
}

# --- checks: compose files ---

check_config_valid() { compose_example config --quiet; }

check_config_missing_variables() {
    local variable output
    for variable in MEDIA_DIR HOST_UID; do
        grep -v "^$variable=" "$ROOT/.env.example" >"$SCRATCH/without-$variable.env"
        if output=$(docker compose --env-file "$SCRATCH/without-$variable.env" -f compose.yaml config --quiet 2>&1); then
            echo "config succeeded without $variable"
            return 1
        fi
        grep -q "$variable" <<<"$output" || { echo "the error does not name $variable: $output"; return 1; }
    done
}

check_config_missing_folders() {
    ensure_built || return 1
    local variable output
    for variable in MEDIA_DIR MUSIC_DIR WORK_DIR; do
        if output=$(env "$variable=$SCRATCH_REL/missing-$variable" docker compose --env-file "$TEST_ENV" \
            run --rm --no-deps -T app --version 2>&1); then
            echo "app started with a missing $variable"
            return 1
        fi
        [[ ! -e $SCRATCH/missing-$variable ]] || { echo "Docker created $SCRATCH/missing-$variable"; return 1; }
        grep -q "missing-$variable" <<<"$output" || { echo "the error does not name the folder: $output"; return 1; }
    done
}

check_config_gpu_override() {
    ensure_built || return 1
    config_assert '
devices = {name: service["devices"] for name, service in c["services"].items() if service.get("devices")}
assert list(devices) == ["ollama"], devices
assert [device["source"] for device in devices["ollama"]] == ["/dev/dri"], devices
assert "/dev/kfd" not in json.dumps(c)
assert c["services"]["ollama"]["environment"].get("OLLAMA_IGPU_ENABLE") == "1"
assert not [name for name, service in c["services"].items()
            if name != "ollama" and "OLLAMA_IGPU_ENABLE" in (service.get("environment") or {})]
' compose_example || return 1
    config_assert '
assert not [name for name, service in c["services"].items() if service.get("devices")]
assert "OLLAMA_IGPU_ENABLE" not in json.dumps(c)
assert "/dev/dri" not in json.dumps(c)
' compose_files
}

check_config_vulkan() {
    ensure_built || return 1
    config_assert 'assert c["services"]["ollama"]["environment"].get("OLLAMA_VULKAN") is None' compose_example || return 1
    config_assert 'assert c["services"]["ollama"]["environment"].get("OLLAMA_VULKAN") == "0"' \
        env OLLAMA_VULKAN=0 docker compose --env-file "$ROOT/.env.example"
}

check_config_selinux() {
    ensure_built || return 1
    config_assert '
labelled = sorted(name for name, service in c["services"].items() if "label=disable" in service.get("security_opt", []))
assert labelled == ["app", "dev", "editor"], labelled
' compose_files compose.selinux.yaml || return 1
    config_assert '
assert not [name for name, service in c["services"].items() if "label=disable" in service.get("security_opt", [])]
' compose_files
}

check_config_test_override() {
    ensure_built || return 1
    config_assert '
services = c["services"]
assert services["ollama"]["image"] == services["dev"]["image"], "Ollama is not replaced by the simulated server"
assert services["ollama-pull"]["entrypoint"] == ["true"], services["ollama-pull"]
assert not [name for name, volume in c.get("volumes", {}).items() if volume.get("external")], "an external volume is left"
models = [volume["source"] for volume in services["ollama"]["volumes"] if volume["target"] == "/root/.ollama"]
assert models == ["vacation-test-models"], models
assert list(services["ollama"]["networks"]) == ["backend"], services["ollama"]["networks"]
assert [name for name, service in services.items() if service.get("ports")] == ["nginx"], "a port is published"
assert float(services["app"]["cpus"]) == 1, services["app"].get("cpus")
assert int(services["app"]["mem_limit"]) == 1024**3, services["app"].get("mem_limit")
assert not [name for name, volume in c.get("volumes", {}).items() if volume.get("external")]
' dc --profile dev --profile cli
}

check_config_editor() {
    ensure_built || return 1
    config_assert '
services, networks = c["services"], c["networks"]
published = {name: service["ports"] for name, service in services.items() if service.get("ports")}
assert list(published) == ["nginx"], f"ports published by {list(published)}"
assert [(port["host_ip"], int(port["published"]), port["target"]) for port in published["nginx"]] == [("127.0.0.1", 8080, 8080)], published
assert list(services["editor"]["networks"]) == ["backend"], services["editor"]["networks"]
assert sorted(services["nginx"]["networks"]) == ["backend", "frontend"], services["nginx"]["networks"]
assert networks["backend"].get("internal") is True and not networks["frontend"].get("internal")
assert not [name for name, service in services.items() if "frontend" in service.get("networks", {}) and name != "nginx"]
depends = set(services["editor"].get("depends_on", {}))
assert not depends & {"ollama", "ollama-pull"}, f"editor depends on {depends}"
assert services["editor"]["healthcheck"]["test"][0] == "CMD"
assert services["editor"]["command"][0] == "serve"
assert services["editor"]["image"] == services["app"]["image"], "editor and app must be the same runtime image"
nginx = services["nginx"]
assert nginx["read_only"] is True and nginx["cap_drop"] == ["ALL"] and "no-new-privileges:true" in nginx["security_opt"]
assert [(volume["target"], volume.get("read_only")) for volume in nginx["volumes"]] == [("/etc/nginx/nginx.conf", True)], nginx["volumes"]
assert nginx["image"].split(":")[1] not in ("latest", ""), nginx["image"]
' compose_files || return 1
    # `app` is in the cli profile: a bare `docker compose up` does not start it, `run app` still works.
    local without_cli with_cli
    without_cli=$(docker compose --env-file "$TEST_ENV" config --services) || return 1
    with_cli=$(docker compose --env-file "$TEST_ENV" --profile cli config --services) || return 1
    grep -qx app <<<"$without_cli" && { echo "a bare docker compose up would start app"; return 1; }
    grep -qx app <<<"$with_cli" || { echo "app is not in the cli profile"; return 1; }
    for service in editor nginx; do
        grep -qx "$service" <<<"$without_cli" || { echo "$service is not started by docker compose up"; return 1; }
    done
    dc run --rm -T --no-deps app --version >/dev/null || { echo "docker compose run app no longer works"; return 1; }
}

# --- checks: services ---

check_ollama_not_published() {
    ensure_ollama || return 1
    local container published
    container=$(dc ps -q ollama)
    [[ -n $container ]] || { echo "the simulated Ollama is not running"; return 1; }
    published=$(docker inspect -f '{{range $port, $bindings := .NetworkSettings.Ports}}{{if $bindings}}{{$port}} {{end}}{{end}}' "$container")
    [[ -z $published ]] || { echo "published ports: $published"; return 1; }
}

check_doctor_require_ai() {
    ensure_ollama || return 1
    app doctor --require-ai
}

NETWORK_PROBE='
import socket, sys, urllib.request

with urllib.request.urlopen("http://ollama:11434/api/tags", timeout=10) as response:
    assert response.status == 200
print("reaches http://ollama:11434")
try:
    socket.create_connection(("1.1.1.1", 443), timeout=5).close()
except OSError as error:
    print(f"does not reach the Internet ({error.strerror or error})")
    sys.exit(0)
print("the container reaches the Internet")
sys.exit(1)
'

check_app_network() {
    ensure_ollama || return 1
    dc run --rm -T --no-deps --entrypoint python app -c "$NETWORK_PROBE"
}

check_dev_isolation() {
    ensure_ollama || return 1
    dev python -c "$NETWORK_PROBE" || return 1
    dev python -c '
import os
mount_points = [line.split()[1] for line in open("/proc/mounts")]
assert "/media" not in mount_points and not os.listdir("/media"), "MEDIA_DIR is mounted in dev"  # /media is an empty folder of Debian
print("no media folder in dev")
' || return 1
    local created=$SCRATCH/owner-check
    rm -f "$created"
    dev touch "$SCRATCH_REL/owner-check" || return 1
    [[ $(stat -c %u:%g "$created") == "$(id -u):$(id -g)" ]] \
        || { echo "file created in /src belongs to $(stat -c %u:%g "$created"), not to the host user"; return 1; }
    rm -f "$created"
}

# `ui-dev` (from U4): the interface's checks and tests, in a container with the repository mounted, no network and no media.
check_config_ui_dev() {
    config_assert '
service = c["services"]["ui-dev"]
assert service.get("profiles") == ["dev"], service.get("profiles")
assert service.get("network_mode") == "none" and not service.get("networks"), "ui-dev must have no network"
assert [volume["target"] for volume in service["volumes"]] == ["/src"], service["volumes"]
assert service["working_dir"] == "/src/frontend"
assert service["user"].count(":") == 1 and not service["user"].startswith("0"), service["user"]
assert "ui-dev" not in [name for name, other in c["services"].items() if not other.get("profiles")]
' compose_files || return 1
    local without_dev
    without_dev=$(docker compose --env-file "$TEST_ENV" config --services) || return 1
    ! grep -qx ui-dev <<<"$without_dev" || { echo "a bare docker compose up would start ui-dev"; return 1; }
}

# `e2e` (UI-SPEC.md, D14): the browser tests. It shares the network of nginx (so it is not on `frontend` itself), publishes
# nothing, mounts only the repository and is not started by a bare `docker compose up`. The tests themselves run on request:
# they need the editor, a synthetic vacation and a few minutes (`docker compose run --rm -T e2e`).
check_config_e2e() {
    config_assert '
service = c["services"]["e2e"]
assert service.get("profiles") == ["dev"], service.get("profiles")
assert service.get("network_mode") == "service:nginx" and not service.get("networks"), "e2e must share the network of nginx"
assert not service.get("ports"), "e2e must not publish a port"
assert [volume["target"] for volume in service["volumes"]] == ["/src"], service["volumes"]
assert service["working_dir"] == "/src/frontend/e2e"
assert service["user"].count(":") == 1 and not service["user"].startswith("0"), service["user"]
assert service["cap_drop"] == ["ALL"], service["cap_drop"]
assert "e2e" not in [name for name, other in c["services"].items() if not other.get("profiles")]
assert not [name for name, other in c["services"].items() if "frontend" in other.get("networks", {}) and name != "nginx"]
' compose_files || return 1
    local without_dev
    without_dev=$(docker compose --env-file "$TEST_ENV" config --services) || return 1
    ! grep -qx e2e <<<"$without_dev" || { echo "a bare docker compose up would start e2e"; return 1; }
}

check_ui_dev_isolation() {
    ensure_built || return 1
    local interfaces
    interfaces=$(dc run --rm -T --no-deps ui-dev ls /sys/class/net) || return 1
    [[ $interfaces == lo ]] || { echo "ui-dev has network interfaces: $interfaces"; return 1; }
    dc run --rm -T --no-deps ui-dev sh -c '! grep -q " /media " /proc/mounts && test -z "$(ls -A /media 2>/dev/null)"' \
        || { echo "MEDIA_DIR is mounted in ui-dev"; return 1; }
    local created=$SCRATCH/owner-check-ui
    rm -f "$created"
    dc run --rm -T --no-deps ui-dev touch "/src/$SCRATCH_REL/owner-check-ui" || return 1
    [[ $(stat -c %u:%g "$created") == "$(id -u):$(id -g)" ]] || { echo "file created by ui-dev does not belong to the host user"; return 1; }
    rm -f "$created"
}

check_ui_dev_checks() {
    ensure_built || return 1
    dc run --rm -T --no-deps ui-dev npm run check || return 1
    dc run --rm -T --no-deps ui-dev npm test || return 1
    [[ ! -e $ROOT/frontend/node_modules ]] || { echo "node_modules ended up in the repository"; return 1; }
}

# --- checks: the full job ---

check_cycle_exit() {
    ensure_cycle || return 1
    tail -n 15 "$SCRATCH/cycle.out"
    [[ $(<"$SCRATCH/cycle.exit") == 0 ]] || { echo "exit code $(<"$SCRATCH/cycle.exit")"; return 1; }
    ! grep -q 'AI analysis skipped' "$SCRATCH/work/run.log" || { echo "the AI analysis did not run"; return 1; }
}

check_cycle_video() {
    ensure_cycle || return 1
    verify_video "$SCRATCH_REL/work" cycle.mp4 20 480
}

check_cycle_media_unchanged() {
    ensure_cycle || return 1
    (cd "$SCRATCH/media" && find . -type f -exec sha256sum {} + | sort -k2 | diff -u "$SCRATCH/media.sha256" -) \
        && echo "$(wc -l <"$SCRATCH/media.sha256") originals unchanged"
}

check_cycle_nothing_outside_work() {
    ensure_cycle || return 1
    local changes
    # Docker itself adds the mount points of the volumes and the init binary; the root filesystem is read-only.
    changes=$(docker diff "$CYCLE_CONTAINER" \
        | grep -Ev '^. /(usr|usr/sbin|usr/sbin/docker-init|etc|etc/vacation-memories|etc/vacation-memories/compose.toml|media|music|work)$')
    [[ -z $changes ]] || { echo "the container wrote outside /work:"; echo "$changes"; return 1; }
    local owner
    owner=$(find "$SCRATCH/work" ! -user "$(id -u)" | head -n 3)
    [[ -z $owner ]] || { echo "files not owned by the host user:"; echo "$owner"; return 1; }
    local touched
    touched=$(find "$SCRATCH/media" "$SCRATCH/music" -type f -newer "$SCRATCH/media.sha256" | head -n 3)
    [[ -z $touched ]] || { echo "files modified or created in the media folders:"; echo "$touched"; return 1; }
}

check_cycle_limits() {
    ensure_cycle || return 1
    dev python - "$SCRATCH_REL/work/runtime_profile.json" <<'PY'
import json, sys

profile = json.load(open(sys.argv[1]))
limit, ram = profile["cpu"], profile["memory"]
assert limit["cgroup_limit"] is not None and limit["cgroup_limit"] <= 1, limit
assert limit["usable_cores"] <= 1, limit
assert ram["cgroup_headroom_bytes"] is not None and ram["cgroup_headroom_bytes"] <= 1024**3, ram
assert ram["usable_bytes"] <= 1024**3, ram
print(f"cores {limit['usable_cores']} (cgroup {limit['cgroup_limit']}), RAM headroom {ram['cgroup_headroom_bytes'] / 1024**2:.0f} MiB")
PY
}

check_app_hardening() {
    ensure_cycle || return 1
    docker inspect "$CYCLE_CONTAINER" | dev python -c '
import json, os, sys

container = json.load(sys.stdin)[0]
host, state = container["HostConfig"], container["Config"]
owner = os.stat("/src")
assert host["ReadonlyRootfs"] is True, "the root filesystem is writable"
assert host["CapDrop"] == ["ALL"], host["CapDrop"]
assert not host["CapAdd"], host["CapAdd"]
assert "no-new-privileges:true" in host["SecurityOpt"], host["SecurityOpt"]
assert host["Privileged"] is False
assert host["Init"] is True
assert state["User"] == f"{owner.st_uid}:{owner.st_gid}", state["User"]
assert container["Config"].get("StopTimeout") == 30, container["Config"].get("StopTimeout")
mounts = {mount["Destination"]: mount for mount in container["Mounts"]}
assert not [m for m in mounts.values() if "docker.sock" in m["Source"]], "the Docker socket is mounted"
assert mounts["/media"]["RW"] is False and mounts["/music"]["RW"] is False
assert mounts["/work"]["RW"] is True
assert not host["PortBindings"], host["PortBindings"]
print("read-only rootfs, cap_drop ALL, no-new-privileges, init, host user, /media and /music read-only, no socket")
' || return 1
    dc run --rm -T --no-deps --entrypoint python app -c '
import getpass, os

def writable(path):
    try:
        with open(path, "w"):
            pass
        os.remove(path)
        return True
    except OSError:
        return False

assert not writable("/probe"), "writes to /"
assert not writable("/etc/probe"), "writes to /etc"
assert not writable("/media/probe"), "writes to /media"
assert not writable("/music/probe"), "writes to /music"
assert writable("/work/probe"), "cannot write to /work"
assert getpass.getuser() == "app"
print("only /work is writable")
'
}

# --- checks: the editor (from U3) ---

# Calls to the editor through nginx, from inside the nginx container (the published port is on the host's loopback).
editor_get() { dc exec -T nginx wget -qO- --header "Host: 127.0.0.1:8080" "http://127.0.0.1:8080$1"; }
editor_post() { # path json
    dc exec -T nginx wget -qO- --header "Host: 127.0.0.1:8080" --header "Origin: http://127.0.0.1:8080" \
        --header "Content-Type: application/json" --post-data "$2" "http://127.0.0.1:8080$1"
}
json_value() { dev python -c "import json, sys; data = json.load(sys.stdin); print($1)"; }
json_check() { dev python -c "import json, sys
data = json.load(sys.stdin)
$1"; }
# PUT, which busybox wget does not do: from `dev`, which reaches the editor on the internal network.
editor_put() { # path json
    dev python - "$1" "$2" <<'PY'
import json, sys, urllib.request

request = urllib.request.Request("http://editor:8000" + sys.argv[1], data=sys.argv[2].encode(), method="PUT", headers={
    "Host": "127.0.0.1:8080", "Origin": "http://127.0.0.1:8080", "Content-Type": "application/json"})
print(urllib.request.urlopen(request, timeout=30).read().decode())
PY
}

# The editor and nginx up, with Ollama stopped: the editor must not need it.
ensure_editor() {
    ensure_media || return 1
    stamp editor && return 0
    dc stop ollama >/dev/null 2>&1
    dc up -d --wait editor nginx || return 1
    mark editor
}

wait_for_editor_job() { # project seconds: the state of the job once it is not running any more
    local deadline=$((SECONDS + $2)) state
    while ((SECONDS < deadline)); do
        state=$(editor_get "/api/projects/$1/status" | json_value 'data["job"]["state"] if data["job"] else "none"')
        case $state in running | stopping | none) sleep 1 ;; *) echo "$state"; return 0 ;; esac
    done
    echo timeout
    return 1
}

check_editor_serves() {
    ensure_editor || return 1
    [[ -z $(dc ps --status running -q ollama) ]] || { echo "Ollama is running: the editor started it"; return 1; }
    [[ $(editor_get /api/health | json_value 'data["status"]') == ok ]] || { echo "no health through nginx"; return 1; }
    local page
    page=$(editor_get /) || return 1
    grep -qi '<title>' <<<"$page" || { echo "the page is not served"; return 1; }
    if grep -Eiq '(https?:)?//[a-z0-9.-]+\.[a-z]{2,}' <<<"$page"; then
        echo "the served page has an external reference"
        return 1
    fi
    [[ $(editor_get /api/folders | json_value 'sorted(f["path"] for f in data["folders"])') == *vacation* ]] \
        || { echo "the folders of the media root are not listed"; return 1; }
    if dc exec -T nginx wget -qO- --header "Host: evil.example" http://127.0.0.1:8080/api/folders >/dev/null 2>&1; then
        echo "a foreign Host was served"
        return 1
    fi
    if dc exec -T nginx wget -qO- --header "Host: 127.0.0.1:8080" --header "Origin: http://evil.example" \
        --header "Content-Type: application/json" --post-data '{"folder": "vacation"}' \
        http://127.0.0.1:8080/api/projects >/dev/null 2>&1; then
        echo "a foreign Origin was served"
        return 1
    fi
}

# The page, its script and stylesheet as nginx serves them: no external reference (UI-SPEC.md, sections 3 and 15).
check_editor_page_assets() {
    ensure_editor || return 1
    local served=$SCRATCH/served page assets url fonts
    rm -rf "$served" && mkdir -p "$served"
    page=$(editor_get /) || return 1
    printf '%s' "$page" >"$served/index.html"
    assets=$(grep -Eo '/assets/[A-Za-z0-9._-]+\.(js|css)' <<<"$page")
    [[ $(wc -l <<<"$assets") == 2 && $assets == *.js* && $assets == *.css* ]] \
        || { echo "the page should load one script and one stylesheet, it loads: $assets"; return 1; }
    for url in $assets; do
        editor_get "$url" >"$served/$(basename "$url")" || { echo "cannot fetch $url"; return 1; }
    done
    fonts=$(grep -Eo 'url\("?\./[A-Za-z0-9._-]+\.woff2' "$served"/*.css | sed -E 's/.*\.\///')
    [[ -n $fonts ]] || { echo "the stylesheet bundles no font"; return 1; }
    for url in $fonts; do
        dc exec -T nginx wget -q -O /dev/null --header "Host: 127.0.0.1:8080" "http://127.0.0.1:8080/assets/$url" \
            || { echo "the font $url is not served"; return 1; }
    done
    dev python - "$SCRATCH_REL/served" <<'PY' || return 1
import json, pathlib, re, sys

allowed = json.loads(pathlib.Path("frontend/tools/allowed-urls.json").read_text())["prefixes"]
pattern = re.compile(r"(?:https?:)?//[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}[^\s\"'`)<>\\]*")
problems = []
for path in sorted(pathlib.Path(sys.argv[1]).iterdir()):
    for url in pattern.findall(path.read_text(encoding="utf-8")):
        if not url.startswith(tuple(allowed)):
            problems.append(f"{path.name}: {url}")
if problems:
    sys.exit("external references in the served page:\n" + "\n".join(problems))
print("no external reference in the page, the script and the stylesheet")
PY
}

check_editor_network() {
    ensure_editor || return 1
    ensure_ollama || return 1
    local container published
    for container in $(dc ps -q editor); do
        [[ -z $(docker port "$container") ]] || { echo "the editor publishes a port"; return 1; }
    done
    published=$(docker port "$(dc ps -q nginx)")
    [[ $published == *"127.0.0.1:8080"* && $(wc -l <<<"$published") == 1 ]] \
        || { echo "nginx publishes: $published"; return 1; }
    if dc exec -T editor python -c "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)" 2>/dev/null; then
        echo "the editor reaches the Internet"
        return 1
    fi
    dc exec -T editor python -c "import socket; socket.create_connection(('ollama', 11434), timeout=5)" \
        || { echo "the editor cannot reach Ollama on the internal network"; return 1; }
}

check_editor_hardening() {
    ensure_editor || return 1
    docker inspect "$(dc ps -q editor)" "$(dc ps -q nginx)" | dev python -c '
import json, os, sys

editor, nginx = json.load(sys.stdin)
owner = os.stat("/src")
for name, container in (("editor", editor), ("nginx", nginx)):
    host = container["HostConfig"]
    assert host["ReadonlyRootfs"] is True, f"{name}: writable root filesystem"
    assert host["CapDrop"] == ["ALL"] and not host["CapAdd"], f"{name}: capabilities"
    assert "no-new-privileges:true" in host["SecurityOpt"], f"{name}: no-new-privileges"
    assert host["Privileged"] is False, name
    assert not [m for m in container["Mounts"] if "docker.sock" in m["Source"]], f"{name}: Docker socket"
assert editor["HostConfig"]["Init"] is True and editor["Config"]["User"] == f"{owner.st_uid}:{owner.st_gid}"
assert editor["Config"].get("StopTimeout") == 30, editor["Config"].get("StopTimeout")
mounts = {mount["Destination"]: mount for mount in editor["Mounts"]}
assert mounts["/media"]["RW"] is False and mounts["/music"]["RW"] is False and mounts["/work"]["RW"] is True
assert editor["Config"]["Cmd"][0] == "serve"
assert not editor["HostConfig"]["PortBindings"]
assert [mount["Destination"] for mount in nginx["Mounts"]] == ["/etc/nginx/nginx.conf"], nginx["Mounts"]
assert nginx["Mounts"][0]["RW"] is False
print("editor: read-only rootfs, cap_drop ALL, host user, media and music read-only; nginx: only its configuration")
'
}

# The project of the synthetic vacation, analyzed and planned by a job (no AI), once; its id is kept for the checks.
ensure_editor_plan() {
    ensure_editor || return 1
    stamp editor-plan && { cat "$SCRATCH/editor.project"; return 0; }
    local project state
    project=$(editor_post /api/projects '{"folder": "vacation"}' | json_value 'data["id"]') || return 1
    editor_post "/api/projects/$project/jobs" '{"type": "plan", "ai": false}' >/dev/null || return 1
    state=$(wait_for_editor_job "$project" 300)
    [[ $state == done ]] || { echo "the plan job ended as $state" >&2; return 1; }
    echo "$project" >"$SCRATCH/editor.project" && mark editor-plan && echo "$project"
}

# An analysis and plan job and a preview, through nginx, on the synthetic vacation and without AI.
check_editor_jobs() {
    local project state
    project=$(ensure_editor_plan) || return 1
    editor_get "/api/projects/$project" | json_check '
assert data["plan"]["present"] and data["analysis"]["complete"], data
assert data["job"]["exit"]["key"] == "done", data["job"]
print("analysis and plan done:", data["analysis"]["files"])' || return 1
    editor_get "/api/projects/$project/plan" | json_check '
assert data["plan"]["schema_version"] == 2 and data["limits"]["elements"]
print("plan with", len(data["plan"]["elements"]), "elements")' || return 1
    editor_post "/api/projects/$project/jobs" '{"type": "preview"}' >/dev/null || return 1
    state=$(wait_for_editor_job "$project" 300)
    [[ $state == done ]] || { echo "the preview job ended as $state"; return 1; }
    dev python - "$SCRATCH_REL/work/projects/$project" <<'PY'
import subprocess, sys
from pathlib import Path

video = Path("/src") / sys.argv[1] / "memories_preview.mp4"
probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,height", "-of", "csv=p=0", str(video)],
                       capture_output=True, text=True, check=True)
assert "h264" in probe.stdout and "540" in probe.stdout, probe.stdout
print("preview", video.name, probe.stdout.split())
PY
}

# `docker stop` of the editor while an export renders: exit 143 within 10 s, no orphan FFmpeg, the job is told as
# interrupted; the same job run again finishes.
check_editor_stop() {
    local project state container started elapsed code deadline
    project=$(ensure_editor_plan) || return 1
    editor_put "/api/projects/$project/settings" '{"resolution": "720p"}' >/dev/null || return 1
    editor_post "/api/projects/$project/jobs" '{"type": "export", "name": "stopped"}' >/dev/null || return 1
    container=$(dc ps -q editor)
    deadline=$((SECONDS + 240))
    until docker top "$container" 2>/dev/null | grep -q ffmpeg; do
        [[ $(editor_get "/api/projects/$project/status" | json_value 'data["job"]["state"]') == running ]] \
            || { echo "the job ended before the rendering"; return 1; }
        ((SECONDS < deadline)) || { echo "FFmpeg did not start within 240 s"; return 1; }
        sleep 0.5
    done
    started=$(date +%s.%N)
    docker stop "$container" >/dev/null || return 1
    elapsed=$(awk -v start="$started" -v end="$(date +%s.%N)" 'BEGIN { printf "%.1f", end - start }')
    code=$(docker inspect -f '{{.State.ExitCode}}' "$container")
    echo "editor stopped during a render: exit $code in $elapsed s"
    [[ $code == 143 ]] || { echo "expected exit code 143"; return 1; }
    awk -v elapsed="$elapsed" -v limit="$STOP_SECONDS" 'BEGIN { exit !(elapsed < limit) }' \
        || { echo "stop took more than $STOP_SECONDS s"; return 1; }
    dc up -d --wait editor nginx >/dev/null || return 1
    editor_get "/api/projects/$project/status" | json_check '
job = data["job"]
assert job["state"] == "interrupted" and job["exit"]["code"] == 143, job' || return 1
    [[ ! -e $SCRATCH/work/projects/$project/stopped.mp4 ]] || { echo "a video was left as if finished"; return 1; }
    editor_post "/api/projects/$project/jobs" '{"type": "export", "name": "stopped"}' >/dev/null || return 1
    state=$(wait_for_editor_job "$project" 300)
    [[ $state == done ]] || { echo "the job run again ended as $state"; return 1; }
}

check_editor_media_unchanged() {
    ensure_editor || return 1
    (cd "$SCRATCH/media" && find . -type f -exec sha256sum {} + | sort -k2 | diff - "$SCRATCH/media.sha256") \
        || { echo "the media changed"; return 1; }
    [[ -z $(find "$SCRATCH/media" -newer "$SCRATCH/media.sha256" -type f) ]] || { echo "a media file was touched"; return 1; }
}

# --- checks: evaluation of the edit ---

# `evaluate prepare` on the synthetic media, with the simulated Ollama, once.
ensure_eval() {
    ensure_media || return 1
    ensure_ollama || return 1
    stamp eval && return 0
    app evaluate prepare --duration 15 --run-id docker >"$SCRATCH/eval.out" 2>&1
    echo $? >"$SCRATCH/eval.exit"
    mark eval
}

check_eval_prepare() {
    ensure_eval || return 1
    tail -n 8 "$SCRATCH/eval.out"
    [[ $(<"$SCRATCH/eval.exit") == 0 ]] || { echo "exit code $(<"$SCRATCH/eval.exit")"; return 1; }
    dev python - "$SCRATCH_REL/work/eval/docker" <<'PY'
import json, sys
from pathlib import Path

run = Path(sys.argv[1])
prepared = json.loads((run / "prepare.json").read_text())
assert prepared["ai"]["used"], "the AI was not used"
page = (run / "review.html").read_text()
assert "http://" not in page and "https://" not in page, "the page has an external reference"
for variant in prepared["variants"].values():
    assert (run / variant["video"]).stat().st_size > 0, variant["video"]
thumbs = [entry["thumb"] for entry in prepared["segments"].values()]
assert thumbs and all(thumb and (run / thumb).is_file() for thumb in thumbs), "thumbnails missing"
assert (run / "mapping.json").is_file()
print(f"{len(prepared['variants'])} previews, {len(thumbs)} thumbnails, a page without external references")
PY
}

check_eval_report() {
    ensure_eval || return 1
    [[ $(<"$SCRATCH/eval.exit") == 0 ]] || { echo "evaluate prepare failed"; return 1; }
    # The ratings, as the page exports them.
    dev python - "$SCRATCH_REL/work/eval/docker" <<'PY' || return 1
import json, sys
from pathlib import Path

run = Path(sys.argv[1])
prepared = json.loads((run / "prepare.json").read_text())
ratings = {"version": 1, "run_id": prepared["run_id"], "excluded": {}, "variants": {}}
for number, (letter, variant) in enumerate(sorted(prepared["variants"].items())):
    ratings["variants"][letter] = {
        "rating": 4 - number, "rank": number + 1, "rhythm": {},
        "clips": {clip["key"]: {"repetitive": index == 0, "length": "ok"} for index, clip in enumerate(variant["clips"])},
    }
for index, item in enumerate(prepared["excluded"]):
    ratings["excluded"][item["key"]] = {"important": index == 0}
(run / "ratings.json").write_text(json.dumps(ratings))
PY
    dc run --rm -T --no-deps app evaluate report --run-id docker || return 1
    dev python - "$SCRATCH_REL/work/eval/docker" <<'PY'
import json, re, sys
from pathlib import Path

run = Path(sys.argv[1])
text = (run / "eval_summary.json").read_text()
summary = json.loads(text)
assert {"full", "random"} <= set(summary["variants"]), list(summary["variants"])
for private in (r"IMG_", r"VID_", r"vacation/", r"_day\d", r"\.jpg", r"\.mp4", r"thumbs/", r"\d{4}-\d\d-\d\d"):
    assert not re.search(private, text), f"the summary contains {private!r}"
assert (run / "eval_report.md").is_file() and (run.parent / "eval_history.json").is_file()
print(f"cycle {summary['cycle']}: {', '.join(sorted(summary['variants']))}; the summary names no file")
PY
}

check_stop_and_resume() {
    ensure_media || return 1
    ensure_ollama || return 1
    local work=$SCRATCH/work-stop
    local job=(--duration 60 --resolution 720p --output stop.mp4)
    rm -rf "$work"
    mkdir -p "$work" || return 1
    docker rm -f "$STOP_CONTAINER" >/dev/null 2>&1
    export WORK_DIR=$SCRATCH_REL/work-stop
    dc run -d -T --name "$STOP_CONTAINER" app "${job[@]}" >/dev/null || return 1

    local deadline=$((SECONDS + 240))
    until docker top "$STOP_CONTAINER" 2>/dev/null | grep -q ffmpeg; do
        [[ $(docker inspect -f '{{.State.Running}}' "$STOP_CONTAINER") == true ]] \
            || { echo "the job ended before the rendering:"; docker logs "$STOP_CONTAINER" 2>&1 | tail -n 15; return 1; }
        ((SECONDS < deadline)) || { echo "FFmpeg did not start within 240 s"; return 1; }
        sleep 0.5
    done

    local started elapsed
    started=$(date +%s.%N)
    docker stop "$STOP_CONTAINER" >/dev/null || return 1
    elapsed=$(awk -v start="$started" -v end="$(date +%s.%N)" 'BEGIN { printf "%.1f", end - start }')
    local code
    code=$(docker inspect -f '{{.State.ExitCode}}' "$STOP_CONTAINER")
    echo "stopped during the rendering: exit $code in $elapsed s"
    [[ $code == 143 ]] || { echo "expected exit code 143"; return 1; }
    awk -v elapsed="$elapsed" -v limit="$STOP_SECONDS" 'BEGIN { exit !(elapsed < limit) }' \
        || { echo "stop took more than $STOP_SECONDS s"; return 1; }
    [[ $(docker top "$STOP_CONTAINER" 2>&1 | grep -c ffmpeg) == 0 ]] || { echo "FFmpeg left running"; return 1; }
    docker rm "$STOP_CONTAINER" >/dev/null

    dev python - "$SCRATCH_REL/work-stop" <<'PY' || return 1
import json, sys
from pathlib import Path

folder = Path(sys.argv[1])
names = sorted(path.name for path in folder.glob("*.json"))
for name in names:
    json.loads((folder / name).read_text())  # a truncated file would raise
state = json.loads((folder / "status.json").read_text())["state"]
assert state == "interrupted", state
print(f"{len(names)} JSON files intact, status {state}")
PY
    app "${job[@]}" --resume || return 1
    verify_video "$SCRATCH_REL/work-stop" stop.mp4 60 720
}

# --- run ---

trap cleanup EXIT

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    echo "Docker with the Compose plugin is required" >&2
    exit 2
fi
compose_version=$(docker compose version --short)
if [[ $(printf '%s\n2.24.4\n' "${compose_version#v}" | sort -V | head -n 1) != 2.24.4 ]]; then
    echo "Docker Compose 2.24.4 or later is required (found $compose_version)" >&2
    exit 2
fi

KEEP='' cleanup # what a run killed half-way left behind
ensure_scratch

run_check "build: images build" check_build
run_check "dev: pytest passes" check_pytest
run_check "app: doctor --no-ai, FFmpeg complete, no service started" check_doctor_no_ai
run_check "image: non-root user, entrypoint, version label" check_image_user_and_entrypoint
run_check "image: no media, .git, .env, fixtures, non-headless OpenCV" check_image_content
run_check "config: valid with .env.example" check_config_valid
run_check "config: clear error without MEDIA_DIR or HOST_UID" check_config_missing_variables
run_check "config: missing folders fail and are not created" check_config_missing_folders
run_check "config: GPU override only on ollama" check_config_gpu_override
run_check "config: OLLAMA_VULKAN only when set" check_config_vulkan
run_check "config: SELinux override only on app, dev and editor" check_config_selinux
run_check "config: test override replaces Ollama" check_config_test_override
run_check "ollama: no published port" check_ollama_not_published
run_check "app: doctor --require-ai with the simulated Ollama" check_doctor_require_ai
run_check "app: reaches Ollama, not the Internet" check_app_network
run_check "dev: no media, no Internet, reaches Ollama, files owned by the host user" check_dev_isolation
run_check "config: ui-dev in the dev profile, no network, only the repository mounted" check_config_ui_dev
run_check "config: e2e in the dev profile, in the network of nginx, no port, only the repository mounted" check_config_e2e
run_check "ui-dev: no network, no media, files owned by the host user" check_ui_dev_isolation
run_check "ui-dev: npm run check (tsc) and npm test pass" check_ui_dev_checks
run_check "cycle: exit 0 and AI used" check_cycle_exit
run_check "cycle: video passes the FFprobe checks" check_cycle_video
run_check "cycle: originals unchanged" check_cycle_media_unchanged
run_check "cycle: nothing written outside /work" check_cycle_nothing_outside_work
run_check "cycle: limits of 1 CPU and 1 GiB read by the profile" check_cycle_limits
run_check "app: hardening" check_app_hardening
run_check "config: editor and nginx, only 127.0.0.1:8080 published, app in the cli profile" check_config_editor
run_check "editor: serves the page through nginx with Ollama stopped, checks Host and Origin" check_editor_serves
run_check "editor: the served page, script and stylesheet have no external reference" check_editor_page_assets
run_check "editor: only backend, no Internet, reaches Ollama; nginx publishes only 127.0.0.1:8080" check_editor_network
run_check "editor: hardening, volumes read-only, nginx with only its configuration" check_editor_hardening
run_check "editor: analysis and plan, then a preview, through nginx" check_editor_jobs
run_check "editor: stop during a job, exit 143 within 10 s, the same job finishes" check_editor_stop
run_check "editor: originals unchanged" check_editor_media_unchanged
run_check "evaluate: prepare with the simulated Ollama, a page without external references" check_eval_prepare
run_check "evaluate: report with --no-deps, an anonymous summary" check_eval_report
run_check "stop: SIGTERM during rendering, then resume" check_stop_and_resume

echo
if ((checks == 0)); then
    echo "no check matches '$FILTER'"
    exit 2
fi
if ((failures)); then
    echo "$failures of $checks checks failed"
    exit 1
fi
echo "all $checks checks passed"
