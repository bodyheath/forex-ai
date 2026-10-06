#!/usr/bin/env bash
# Commit and push ONLY data/mechanical_path/ to origin, safely.
#
# Why not the other workflows' `git reset --soft origin/main` + `git add data/`:
# that keeps the runner's (older) working tree and stages all of data/, which
# can silently revert files other workflows changed on origin since this job
# checked out. This job owns exactly one directory, so: snapshot it, reset the
# whole tree to origin's current tip, restore the snapshot, stage only that
# directory. Nothing outside it can be reverted, history stays linear (no merge
# or rebase), and a concurrent push just triggers a re-sync and retry.
#
# Usage: scripts/commit_mechanical_path.sh [branch=main] [remote=origin]
# Env:   RETRY_SLEEP (seconds between push attempts, default 15)
set -euo pipefail

BRANCH="${1:-main}"
REMOTE="${2:-origin}"
DIR="data/mechanical_path"
SNAP="$(mktemp -d)"
trap 'rm -rf "$SNAP"' EXIT

mkdir -p "$DIR"
cp -R "$DIR/." "$SNAP/"

resync_and_stage() {
  git fetch "$REMOTE" "$BRANCH" --quiet
  git reset --hard "$REMOTE/$BRANCH" --quiet
  mkdir -p "$DIR"
  cp -R "$SNAP/." "$DIR/"
  git add "$DIR"
}

commit_if_changed() {
  if git diff --staged --quiet; then
    return 1
  fi
  git commit --quiet -m "chore: mechanical path scan $(date -u +'%Y-%m-%d %H:%M UTC')"
}

resync_and_stage
if ! commit_if_changed; then
  echo "Nothing to commit -- skipping push"
  exit 0
fi

for i in 1 2 3; do
  echo "Push attempt $i of 3"
  if git push "$REMOTE" "HEAD:$BRANCH"; then
    echo "Push succeeded on attempt $i"
    exit 0
  fi
  echo "Push failed -- re-syncing to $REMOTE/$BRANCH and retrying (no merge, no rebase)"
  sleep "${RETRY_SLEEP:-15}"
  resync_and_stage
  commit_if_changed || { echo "Remote already contains these changes"; exit 0; }
done

echo "All 3 push attempts failed -- manual review needed." >&2
exit 1
