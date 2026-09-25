#!/usr/bin/env bash
# Publish the sweep's alert files to the `data` branch (read by the dashboard via raw.githubusercontent.com).
# The branch holds a single commit that is replaced each time, so it never grows.
set -euo pipefail
cd "$(dirname "$0")/.."
url="$(git remote get-url origin)"
if [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
  url="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
fi
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/snapshot"
cp data/snapshot/meta.json data/snapshot/signals.json data/snapshot/contacts.json "$tmp/snapshot/"
git -C "$tmp" init -q -b data
git -C "$tmp" add snapshot
git -C "$tmp" -c user.name=whalescan-bot -c user.email=whalescan-bot@users.noreply.github.com \
  commit -q -m "alerts $(date -u +%Y-%m-%dT%H:%MZ)"
git -C "$tmp" push -q -f "$url" data
echo "published alerts to the data branch"
