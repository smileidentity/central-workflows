#!/usr/bin/env bash
# Usage: commit-state.sh MESSAGE -- COMMAND [ARGS...]
#
# Checks out the spec-sync-state branch (or starts it with no history), runs
# COMMAND inside it, and commits and pushes whatever COMMAND changed. Several
# runs can write at once, so a rejected push starts again from the new tip.
# Each record is its own file, so retries never conflict.
#
# Environment: GH_TOKEN (push access), GITHUB_SERVER_URL and GITHUB_REPOSITORY,
# or STATE_REMOTE to use another remote (the tests use a local one).
set -euo pipefail

message=${1:?usage: commit-state.sh MESSAGE -- COMMAND...}
shift
[ "${1:-}" = -- ] && shift
[ $# -gt 0 ] || { echo "usage: commit-state.sh MESSAGE -- COMMAND..." >&2; exit 2; }

branch=${STATE_BRANCH:-spec-sync-state}
remote=${STATE_REMOTE:-$GITHUB_SERVER_URL/$GITHUB_REPOSITORY.git}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

git init -q "$work"
cd "$work"
git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
if [ -n "${GH_TOKEN:-}" ]; then
  # Pass the token through the environment, not argv, and mask its base64 form in logs.
  auth=$(printf 'x-access-token:%s' "$GH_TOKEN" | base64 | tr -d '\n')
  echo "::add-mask::$auth"
  export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraheader GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $auth"
fi
git remote add origin "$remote"

for attempt in 1 2 3 4 5; do
  # Compare the full ref name: ls-remote patterns also match the end of other refs.
  if ! heads=$(git ls-remote origin "refs/heads/$branch"); then
    sleep $(( attempt * 2 )); continue
  fi
  if [ -n "$(awk -v ref="refs/heads/$branch" '$2 == ref' <<<"$heads")" ]; then
    git fetch -q --depth 1 origin "refs/heads/$branch" || { sleep $(( attempt * 2 )); continue; }
    git checkout -q -f -B "$branch" FETCH_HEAD
    git clean -q -fdx
  else
    git checkout -q -f --orphan "new-$attempt"
    git rm -rqf --ignore-unmatch . > /dev/null
    git clean -q -fdx
  fi
  "$@"
  git add -A
  if git diff --cached --quiet; then
    echo "Nothing to commit to $branch."; exit 0
  fi
  git commit -q -m "$message"
  if git push -q origin "HEAD:refs/heads/$branch"; then
    echo "Pushed to $branch: $message"; exit 0
  fi
  sleep $(( attempt * 2 ))
done
echo "::error::Could not push to the $branch branch"
exit 1
