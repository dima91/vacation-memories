#!/usr/bin/env bash
# Version and image references to publish for a git ref (used by scripts/build.sh).
#
# Usage: scripts/ci_image_tags.sh IMAGE_NAME TAG
#   IMAGE_NAME  <namespace>/<image_name> on Docker Hub, without a registry prefix
#   TAG         a release tag vMAJOR.MINOR.PATCH[-prerelease], or any other ref name (develop, feature/x, ...)
#
# The first line printed is the version (APP_VERSION): the release tag without the leading v, or the ref made usable as
# an image tag (lowercase, anything outside [a-z0-9._-] turned into -). Then one image reference per line:
#   release         IMAGE_NAME:X.Y.Z, and when it has no pre-release part also IMAGE_NAME:X.Y, :X and :latest
#   any other ref   IMAGE_NAME:<ref>
# Exits 1 with a message on stderr when an argument is not usable. `latest` and refs that look like a version without the
# v are refused: they would overwrite a release tag.
set -euo pipefail
export LC_ALL=C

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

release='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z]+([.-][0-9A-Za-z]+)*)?$'
if [[ $tag =~ $release ]]; then
    version=${tag#v}
    echo "$version"
    echo "$image:$version"
    if [[ $version != *-* ]]; then
        major=${version%%.*}
        minor=${version#*.}
        echo "$image:$major.${minor%%.*}"
        echo "$image:$major"
        echo "$image:latest"
    fi
    exit 0
fi

if [[ $tag =~ ^v?[0-9]+(\.|$) || ${tag,,} == latest ]]; then
    echo "the tag must look like vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-prerelease for a release; '$tag' is not usable as a ref name either" >&2
    exit 1
fi
ref=${tag,,}
ref=$(sed 's/^[-.]*//' <<< "${ref//[^a-z0-9._-]/-}")
ref=${ref:0:128}
if [[ -z $ref ]]; then
    echo "the tag cannot be used as an image tag: '$tag'" >&2
    exit 1
fi
echo "$ref"
echo "$image:$ref"
