#!/usr/bin/env bash
# Image references to publish for a release tag (used by the `publish-image` job of .github/workflows/ci.yml).
#
# Usage: scripts/ci_image_tags.sh IMAGE_NAME TAG
#   IMAGE_NAME  <namespace>/<image_name> on Docker Hub, without a registry prefix (the repository variable)
#   TAG         the git tag, vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-prerelease
#
# Prints IMAGE_NAME:VERSION (the tag without the leading v) and, for a version without a pre-release part,
# IMAGE_NAME:latest, one per line. Exits 1 with a message on stderr when an argument is not usable.
set -euo pipefail

if (($# != 2)); then
    echo "usage: ${0##*/} IMAGE_NAME TAG" >&2
    exit 2
fi
image=$1
tag=$2

component='[a-z0-9]+([._-][a-z0-9]+)*'
if [[ ! $image =~ ^$component/$component$ ]]; then
    echo "IMAGE_NAME must be <namespace>/<image_name> in lowercase, with no registry prefix and no tag: '$image'" >&2
    exit 1
fi

if [[ ! $tag =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z]+([.-][0-9A-Za-z]+)*)?$ ]]; then
    echo "the tag must look like vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-prerelease: '$tag'" >&2
    exit 1
fi

version=${tag#v}
echo "$image:$version"
if [[ $version != *-* ]]; then
    echo "$image:latest"
fi
