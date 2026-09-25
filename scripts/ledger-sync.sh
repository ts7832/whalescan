#!/usr/bin/env bash
# Sync data/ledger/ (a working clone of the `ledger` branch) with its remote.
#
# Unlike scripts/publish-data.sh, this NEVER force-pushes: the ledger's history is the permanent,
# append-only audit trail of every call WHALESCAN has made (Track Record spec §5, §9). A concurrent
# writer's push is handled by fetch + rebase + retry, same as the snapshot workflow's own commit step.
#
# Env overrides (used by tests to run fully isolated, against a local bare repo instead of GitHub):
#   LEDGER_REPO_URL — the remote to sync with (default: GITHUB_TOKEN/GITHUB_REPOSITORY, else `origin`)
#   LEDGER_DIR       — the local working clone (default: data/ledger, relative to the repo root)
set -euo pipefail
cd "$(dirname "$0")/.."

mode="${1:?usage: ledger-sync.sh pull|push}"
case "$mode" in
  pull|push) ;;
  *) echo "usage: ledger-sync.sh pull|push" >&2; exit 1 ;;
esac

url="${LEDGER_REPO_URL:-}"
if [ -z "$url" ]; then
  if [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
    url="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
  else
    url="$(git remote get-url origin)"
  fi
fi
dir="${LEDGER_DIR:-data/ledger}"

if [ ! -d "$dir/.git" ]; then
  mkdir -p "$(dirname "$dir")"
  if git ls-remote --exit-code --heads "$url" ledger >/dev/null 2>&1; then
    git clone -q -b ledger --single-branch "$url" "$dir"
  else
    git init -q -b ledger "$dir"
  fi
  git -C "$dir" config user.name whalescan-bot
  git -C "$dir" config user.email whalescan-bot@users.noreply.github.com
fi
if git -C "$dir" remote get-url origin >/dev/null 2>&1; then
  git -C "$dir" remote set-url origin "$url"
else
  git -C "$dir" remote add origin "$url"
fi

if [ "$mode" = pull ]; then
  git -C "$dir" fetch -q origin ledger 2>/dev/null && git -C "$dir" merge -q --ff-only origin/ledger || true
  exit 0
fi

# push
git -C "$dir" add -A
if git -C "$dir" diff --cached --quiet; then
  exit 0  # nothing changed: no empty commit
fi
git -C "$dir" commit -q -m "ledger $(date -u +%Y-%m-%dT%H:%MZ)"
for _ in 1 2 3; do
  if git -C "$dir" push -q origin ledger:ledger 2>/dev/null; then
    exit 0
  fi
  git -C "$dir" fetch -q origin ledger
  git -C "$dir" rebase -q origin/ledger
  sleep 2
done
echo "ledger-sync: push failed after 3 attempts" >&2
exit 1
