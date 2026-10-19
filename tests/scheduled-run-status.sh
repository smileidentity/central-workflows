#!/usr/bin/env bash
# Runs the scheduled-run-status workflow's steps against a fake gh and curl.
# Needs bash, jq and yq (mikefarah).
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
workflow="$root/.github/workflows/scheduled-run-status.yml"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

step() { yq ".jobs.report.steps[] | select(.id == \"$1\") | .run" "$workflow"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# The fake gh records each call, one argument per line and a blank line after, and answers
# the default branch and open issue lookups from $DEFAULT and $OPEN.
mkdir -p "$tmp/bin"
cat > "$tmp/bin/gh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" '' >> "$CALLS"
case "$1 $2" in
  'api repos/smileidentity/example') echo "$DEFAULT" ;;
  'issue list') [ -z "$OPEN" ] || echo "$OPEN" ;;
  'issue create') echo https://github.com/smileidentity/example/issues/9 ;;
  'label create') [ "$3" != "$EXISTING" ] || { echo 'already exists' >&2; exit 1; } ;;
esac
EOF
cat > "$tmp/bin/curl" <<'EOF'
#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  case $1 in
    --data) printf '%s\n' "$2" > "$CALLS.slack" ;;
    -K) cat "$2" > "$CALLS.url" ;;
  esac
  shift
done
exit "${CURL_EXIT:-0}"
EOF
chmod +x "$tmp/bin/gh" "$tmp/bin/curl"

export PATH="$tmp/bin:$PATH" CALLS="$tmp/calls" GITHUB_OUTPUT="$tmp/output"
export GITHUB_SERVER_URL=https://github.com GITHUB_REPOSITORY=smileidentity/example GITHUB_RUN_ID=42
export GITHUB_WORKFLOW='Nightly API tests' GITHUB_WORKFLOW_REF=smileidentity/example/.github/workflows/nightly.yml@refs/heads/main
export GITHUB_REF=refs/heads/main DEFAULT=main OPEN='' EXISTING='' TEAM=team:api
run_url=https://github.com/smileidentity/example/actions/runs/42

report() { (: > "$CALLS"; : > "$GITHUB_OUTPUT"; FAILED=$1 bash -eo pipefail -c "$(step issue)"); }
# Each gh call as one line, its arguments separated by |.
calls() { awk 'BEGIN { RS = ""; FS = "\n" } { line = $1; for (i = 2; i <= NF; i++) line = line "|" $i; print line }' "$CALLS"; }
message() { sed -n 's/^message=//p' "$GITHUB_OUTPUT"; }

# Only unattended runs are reported.
[ "$(yq '.jobs.report.if' "$workflow")" = "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'" ] \
  || fail "the job does not limit itself to scheduled and manual runs"

# First failure: create both labels and open an issue that carries them.
report true > /dev/null
calls | grep -q '^issue|list|--label|red-lane:nightly|--state|open|' || fail "did not look for the issue by its workflow label"
calls | grep -q '^label|create|red-lane:nightly|--force|' || fail "did not create or update the workflow label"
calls | grep -q '^label|create|team:api$' || fail "did not create the team label"
calls | grep -q "^issue|create|--title|Nightly API tests is failing|--label|red-lane:nightly|--label|team:api|--body|The scheduled \`nightly.yml\` workflow failed: $run_url" \
  || fail "issue was not opened with both labels and the run link"
message | grep -q 'Nightly API tests failed. .*Issue: https://github.com/smileidentity/example/issues/9$' || fail "message does not link the new issue"

# Labels that already exist do not stop the issue opening.
EXISTING=team:api report true > /dev/null || fail "an existing label failed the step"
calls | grep -q '^issue|create|' || fail "no issue after an existing label"

# Failure with an issue open: comment on it, open nothing.
OPEN=7 report true > /dev/null
calls | grep -q "^issue|comment|7|--body|Failed again: $run_url$" || fail "did not comment on the open issue"
calls | grep -q '^issue|create|' && fail "opened a second issue"
message | grep -q 'failed again. .*issues/7$' || fail "message does not link the open issue"

# Pass with an issue open: close it with the run link.
OPEN=7 report false > /dev/null
calls | grep -q "^issue|close|7|--comment|Passed: $run_url$" || fail "did not close the open issue"
message | grep -q 'passed again' || fail "no message for the closed issue"

# Pass with nothing open: no writes and no message.
report false > /dev/null
[ "$(calls | grep -vcE '^api[|]|^issue[|]list[|]')" -eq 0 ] || fail "a quiet pass wrote something"
[ -z "$(message)" ] || fail "a quiet pass left a message"

# A manual run on another branch neither opens nor closes the issue.
GITHUB_REF=refs/heads/fix/flaky OPEN=7 report false > /dev/null
[ "$(calls | grep -vc '^api|')" -eq 0 ] || fail "a run on a branch touched issues"

# Nor does a manual run on a tag named after the default branch.
GITHUB_REF=refs/tags/main OPEN=7 report false > /dev/null
[ "$(calls | grep -vc '^api|')" -eq 0 ] || fail "a run on a tag named main touched issues"

# Long workflow file names that share a prefix still get valid labels of their own.
long_label() {
  GITHUB_WORKFLOW_REF="smileidentity/example/.github/workflows/$1@refs/heads/main" report true > /dev/null
  calls | sed -n 's/^issue|list|--label|\([^|]*\)|.*/\1/p'
}
europe=$(long_label nightly-integration-tests-for-production-europe.yml)
us=$(long_label nightly-integration-tests-for-production-us.yml)
[ ${#europe} -le 50 ] && [ ${#us} -le 50 ] || fail "long labels are over 50 characters: '$europe', '$us'"
[ "$europe" != "$us" ] || fail "two long names share the label '$us'"
[ "${us:0:41}" = 'red-lane:nightly-integration-tests-for-pr' ] || fail "long label lost its readable prefix: '$us'"

# Slack: skipped without a webhook, escaped with one, and a failure only warns.
yq '.jobs.report.steps[] | select(.id == "slack") | .if' "$workflow" | grep -q "steps.issue.outputs.message != ''" || fail "Slack runs without a message"
slack() { (rm -f "$CALLS.slack"; MESSAGE='a <b> & c' bash -eo pipefail -c "$(step slack)"); }
SLACK_WEBHOOK_URL='' slack > /dev/null
[ ! -e "$CALLS.slack" ] || fail "Slack was called without a webhook"
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/x slack > /dev/null
[ "$(cat "$CALLS.slack")" = '{"text":"a &lt;b&gt; &amp; c"}' ] || fail "Slack text was not escaped: $(cat "$CALLS.slack")"
grep -q 'url = "https://hooks.slack.com/services/x"' "$CALLS.url" || fail "webhook was not passed to curl"
CURL_EXIT=22 SLACK_WEBHOOK_URL=https://hooks.slack.com/services/x slack > "$tmp/out" || fail "a Slack failure failed the step"
grep -q '::warning::' "$tmp/out" || fail "a Slack failure did not warn"

echo "All scheduled-run-status checks passed"
