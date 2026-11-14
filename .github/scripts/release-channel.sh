#!/usr/bin/env bash
# What a pushed v* tag publishes. Prints key=value lines for $GITHUB_OUTPUT:
#
#   prerelease=true|false  the tag has a pre-release suffix (v0.2.0-rc.1, v0.2.0-beta)
#   latest=true|false      the image tag `latest` and GitHub's "Latest release" move here
#
# `latest` moves only to a stable tag (vMAJOR.MINOR.PATCH and nothing after it) that is
# also the highest stable tag in the repository: v0.2.0-rc.1 never moves it, v0.2.0 does,
# and a v0.1.4 hotfix pushed after v0.2.0 does not take it back to the old line.
# Needs the repository's tags (actions/checkout with fetch-depth: 0).
#
# Usage: release-channel.sh <tag>      (in CI: "$GITHUB_REF_NAME")
set -euo pipefail

tag="${1:?usage: release-channel.sh <tag>}"
stable_re='^v[0-9]+\.[0-9]+\.[0-9]+$'

prerelease=true
latest=false
if [[ "$tag" =~ $stable_re ]]; then
  prerelease=false
  # The highest of every stable tag and this one (the tag may not exist locally yet).
  top="$( { git tag --list 'v*'; echo "$tag"; } | grep -E "$stable_re" | sort -V | tail -n 1)"
  if [[ "$tag" == "$top" ]]; then
    latest=true
  fi
fi

echo "prerelease=$prerelease"
echo "latest=$latest"
