#!/usr/bin/env bash
# Runs commit-state.sh and the record and mark-processed commands, as the
# workflows do, against a local bare repository. Needs bash, git and python3.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
tools="$root/scripts/spec_sync"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

remote="$tmp/central-workflows.git"
git init -q --bare "$remote"
export STATE_REMOTE="$remote" GH_TOKEN=
run_url=https://example.com/run/1

dispatch() {
  jq -n --arg tag "$1" --arg prev "$2" \
    '{client_payload: {tag: $tag, previous_tag: $prev, summary: "changed", breaking: false, lambda_prs: [1]}}' \
    > "$tmp/event-$1.json"
  bash "$tools/commit-state.sh" "Record $1 from lambda" -- \
    python3 "$tools/spec_sync.py" record --event "$tmp/event-$1.json" --state-dir . --run-url "$run_url"
}
files() { git --git-dir "$remote" ls-tree -r --name-only spec-sync-state | tr '\n' ' '; }

# The first record starts the branch with no history.
dispatch v3.2.0 v3.1.0 > /dev/null
[ "$(files)" = "state/pending/v3.2.0.json " ] || fail "first record: $(files)"
[ "$(git --git-dir "$remote" rev-list --count spec-sync-state)" = 1 ] || fail "branch should start with one commit"

# A second record keeps the first.
dispatch v3.3.0 v3.2.0 > /dev/null
[ "$(files)" = "state/pending/v3.2.0.json state/pending/v3.3.0.json " ] || fail "second record: $(files)"

# Two records sent at once both land, thanks to the retry.
dispatch v3.4.0 v3.3.0 > /dev/null 2>&1 & dispatch v3.5.0 v3.4.0 > /dev/null 2>&1 & wait
for tag in v3.4.0 v3.5.0; do
  files | grep -q "state/pending/$tag.json" || fail "concurrent record $tag was lost"
done

# Marking processed moves only the named records.
bash "$tools/commit-state.sh" "Mark processed" -- \
  python3 "$tools/spec_sync.py" mark-processed --state-dir . --tags '["v3.2.0","v3.3.0"]' --run-url "$run_url" > /dev/null
[ "$(files)" = "state/pending/v3.4.0.json state/pending/v3.5.0.json state/processed/v3.2.0.json state/processed/v3.3.0.json " ] \
  || fail "mark processed: $(files)"

# A repeat of a processed release changes nothing.
before=$(git --git-dir "$remote" rev-parse spec-sync-state)
out=$(dispatch v3.2.0 v3.1.0)
grep -q "Nothing to commit" <<<"$out" || fail "repeat dispatch should not commit: $out"
[ "$(git --git-dir "$remote" rev-parse spec-sync-state)" = "$before" ] || fail "repeat dispatch moved the branch"

# A bad payload fails and writes nothing.
jq -n '{client_payload: {tag: "main", previous_tag: "v3.1.0"}}' > "$tmp/event.json"
if bash "$tools/commit-state.sh" "Record bad" -- \
  python3 "$tools/spec_sync.py" record --event "$tmp/event.json" --state-dir . --run-url "$run_url" > /dev/null 2>&1; then
  fail "a bad payload should fail"
fi
[ "$(git --git-dir "$remote" rev-parse spec-sync-state)" = "$before" ] || fail "bad payload moved the branch"

echo "spec-sync state tests passed"
