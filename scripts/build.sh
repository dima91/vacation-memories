#!/usr/bin/env bash
# Builds the runtime image and the single-file launcher, on a PC and in CI (.github/workflows/ci.yml).
#
# Usage: scripts/build.sh NAMESPACE TAG [--push] [--skip-image] [--skip-launcher]
#   NAMESPACE        Docker Hub namespace; the image is NAMESPACE/vacation-memories
#   TAG              the git ref: vX.Y.Z or vX.Y.Z-prerelease for a release, or any other name (develop, feature/x)
#   --push           push every image tag after the build (log in to the registry first)
#   --skip-image     do not build the image (the launcher still points to NAMESPACE/vacation-memories:<version>)
#   --skip-launcher  do not build the launcher
#
# Image tags (scripts/ci_image_tags.sh): a release gets X.Y.Z, X.Y, X and latest, a pre-release only X.Y.Z-pre, any
# other ref its own name; every build also gets sha-<commit> when git can tell the commit.
# The launcher is written as dist/vacation-memories-<version>-linux-x86_64 with its .sha256 (needs the dev profile of
# compose.yaml; HOST_UID and HOST_GID default to the current user). With $GITHUB_OUTPUT set, it also writes
# `version`, `tags` and `launcher`.
set -euo pipefail

usage() { sed -n '2,/^set /p' "$0" | sed '$d; s/^# \{0,1\}//'; }

push= skip_image= skip_launcher=
args=()
for arg in "$@"; do
    case $arg in
        --push) push=1 ;;
        --skip-image) skip_image=1 ;;
        --skip-launcher) skip_launcher=1 ;;
        -h | --help) usage; exit 0 ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) args+=("$arg") ;;
    esac
done
if ((${#args[@]} != 2)); then
    usage >&2
    exit 2
fi
namespace=${args[0]}
ref=${args[1]}
image=$namespace/vacation-memories

cd "$(dirname "$0")/.."

mapfile -t lines < <(bash scripts/ci_image_tags.sh "$image" "$ref")
version=${lines[0]}
tags=("${lines[@]:1}")
sha=$(git rev-parse --short=12 HEAD 2> /dev/null || true)
[[ -n $sha ]] && tags+=("$image:sha-$sha")

if [[ -z $skip_image ]]; then
    labels=()
    source_url=${GITHUB_SERVER_URL:+$GITHUB_SERVER_URL/${GITHUB_REPOSITORY:-}}
    source_url=${source_url:-$(git remote get-url origin 2> /dev/null || true)}
    [[ -n $source_url ]] && labels+=(--label "org.opencontainers.image.source=$source_url")
    revision=${GITHUB_SHA:-$(git rev-parse HEAD 2> /dev/null || true)}
    [[ -n $revision ]] && labels+=(--label "org.opencontainers.image.revision=$revision")

    tag_args=()
    for t in "${tags[@]}"; do tag_args+=(-t "$t"); done
    docker build --target runtime --build-arg "APP_VERSION=$version" "${labels[@]}" "${tag_args[@]}" .

    if [[ -n $push ]]; then
        for t in "${tags[@]}"; do docker push "$t"; done
    fi
fi

launcher=
if [[ -z $skip_launcher ]]; then
    export HOST_UID=${HOST_UID:-$(id -u)} HOST_GID=${HOST_GID:-$(id -g)}
    export IMAGE_REPO=$image APP_VERSION=$version
    docker compose --profile dev build launcher-build
    docker compose run --rm -T launcher-build
    launcher=vacation-memories-$version-linux-x86_64
    mv dist/vacation-memories "dist/$launcher"
    (cd dist && sha256sum "$launcher" > "$launcher.sha256")
fi

echo "version: $version"
printf 'image tag: %s\n' "${tags[@]}"
[[ -n $skip_image ]] && echo "image: skipped" || echo "image pushed: ${push:-no}"
[[ -n $launcher ]] && echo "launcher: dist/$launcher (+ .sha256)"

if [[ -n ${GITHUB_OUTPUT:-} ]]; then
    {
        echo "version=$version"
        echo "launcher=$launcher"
        echo "tags<<EOF"
        printf '%s\n' "${tags[@]}"
        echo "EOF"
    } >> "$GITHUB_OUTPUT"
fi
