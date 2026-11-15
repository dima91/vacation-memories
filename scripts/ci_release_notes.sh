#!/usr/bin/env bash
# Release notes of a version: the body of its section in CHANGELOG.md (used by .github/workflows/ci.yml).
#
# Usage: scripts/ci_release_notes.sh VERSION [CHANGELOG]
#   VERSION    X.Y.Z or X.Y.Z-prerelease, without the leading v
#   CHANGELOG  the changelog to read (default: CHANGELOG.md at the root of the repository)
#
# Prints what is under the heading `## [VERSION]` up to the next `## [` heading, without the heading itself.
# Exits 1 with a message on stderr when the version is not usable, the section is missing, or it has no content.
# `[Unreleased]` is never used: rename it to the version before tagging.
set -euo pipefail

if (($# < 1 || $# > 2)); then
    echo "usage: ${0##*/} VERSION [CHANGELOG]" >&2
    exit 2
fi
version=$1
changelog=${2:-$(dirname "$0")/../CHANGELOG.md}

if [[ ! $version =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z]+([.-][0-9A-Za-z]+)*)?$ ]]; then
    echo "the version must look like MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH-prerelease, without the v: '$version'" >&2
    exit 1
fi
if [[ ! -f $changelog ]]; then
    echo "changelog not found: $changelog" >&2
    exit 1
fi

status=0
body=$(awk -v heading="## [$version]" '
    !found && index($0, heading) == 1 { found = 1; next }
    found && /^## \[/ { exit }
    found { print }
    END { if (!found) exit 3 }
' "$changelog") || status=$?

if ((status == 3)); then
    echo "$changelog has no section '## [$version]': rename [Unreleased] to the version before tagging" >&2
    exit 1
elif ((status != 0)); then
    echo "could not read $changelog" >&2
    exit 1
fi

# Trailing blank lines are already gone ($(...) strips them); drop the leading ones.
body=${body#"${body%%[![:space:]]*}"}

if ! grep -Eqv '^(#+ .*|[[:space:]]*)$' <<<"$body"; then
    echo "the section '## [$version]' of $changelog has no content" >&2
    exit 1
fi

printf '%s\n' "$body"
