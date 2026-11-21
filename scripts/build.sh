#!/usr/bin/env bash
# Builds the runtime image and the single-file launcher, on a PC and in CI (.github/workflows/ci.yml).
#
# Usage: scripts/build.sh NAMESPACE TAG [--push] [--skip-image] [--skip-launcher]
#        scripts/build.sh [--clean-by-ns NS] [--clean-by-version V] [--yes]
#   NAMESPACE        Docker Hub namespace; the image is NAMESPACE/vacation-memories
#   TAG              the git ref: vX.Y.Z or vX.Y.Z-prerelease for a release, or any other name (develop, feature/x)
#   --push           push every image tag after the build (log in to the registry first)
#   --skip-image     do not build the image (the launcher still points to NAMESPACE/vacation-memories:<version>)
#   --skip-launcher  do not build the launcher
#
# Clean mode (builds nothing): removes the local tags matching NS/vacation-memories:V, where an omitted NS or V matches
# anything, so --clean-by-ns removes NS/vacation-memories:*, --clean-by-version removes */vacation-memories:V and both
# together (AND) remove NS/vacation-memories:V. It lists the tags and asks for confirmation; --yes skips the question
# (required without a terminal). NS and V cannot contain * ? [. An image used by a container is not removed.
#
# Image tags (scripts/ci_image_tags.sh): a release gets X.Y.Z, X.Y, X and latest, a pre-release only X.Y.Z-pre, any
# other ref its own name; every build also gets sha-<commit> when git can tell the commit.
# The launcher is written as dist/vacation-memories-<version>-linux-x86_64 with its .sha256 (needs the dev profile of
# compose.yaml; HOST_UID and HOST_GID default to the current user). With $GITHUB_OUTPUT set, it also writes
# `version`, `tags` and `launcher`.
set -euo pipefail

usage() { sed -n '2,/^set /p' "$0" | sed '$d; s/^# \{0,1\}//'; }

push= skip_image= skip_launcher= yes= clean= clean_ns='*' clean_version='*'
args=()
while (($#)); do
    case $1 in
        --push) push=1 ;;
        --skip-image) skip_image=1 ;;
        --skip-launcher) skip_launcher=1 ;;
        --yes) yes=1 ;;
        --clean-by-ns | --clean-by-version)
            if [[ -z ${2:-} || $2 == -* || $2 == *[*?[]* ]]; then
                echo "$1 needs a value that does not start with - and has no * ? [" >&2
                exit 2
            fi
            clean=1
            if [[ $1 == --clean-by-ns ]]; then clean_ns=$2; else clean_version=$2; fi
            shift ;;
        -h | --help) usage; exit 0 ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *) args+=("$1") ;;
    esac
    shift
done

if [[ -n $clean ]]; then
    if ((${#args[@]})) || [[ -n $push$skip_image$skip_launcher ]]; then
        usage >&2
        exit 2
    fi
    found=$(docker image ls --format '{{.Repository}}:{{.Tag}}' --filter "reference=$clean_ns/vacation-memories:$clean_version")
    if [[ -z $found ]]; then
        echo "no image matches $clean_ns/vacation-memories:$clean_version"
        exit 0
    fi
    echo "$found"
    if [[ -z $yes ]]; then
        [[ -t 0 ]] || { echo "no terminal: pass --yes to remove without asking" >&2; exit 1; }
        read -r -p "remove these images? [y/N] " answer
        [[ $answer == [yY] ]] || { echo "aborted"; exit 1; }
    fi
    mapfile -t found <<< "$found"
    docker rmi "${found[@]}"
    exit 0
fi

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
