#!/usr/bin/env bash
# Runs the deploy-status workflow's steps against a local repository and a fake Slack API.
# Needs bash, git, yq (mikefarah) and xmllint.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
workflow="$root/.github/workflows/deploy-status.yml"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

step() { yq ".jobs.status.steps[] | select(.id == \"$1\") | .run" "$workflow"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

git init -q --bare "$tmp/remote/smileidentity/example.git"
seed="$tmp/seed"
git init -q -b main "$seed"
git -C "$seed" -c user.name=t -c user.email=t@example.com commit -q --allow-empty -m seed
git -C "$seed" tag v1.2.3
# A branch whose name ends in deploy-status must not be mistaken for the badge branch.
git -C "$seed" branch ci/deploy-status
git -C "$seed" push -q "$tmp/remote/smileidentity/example.git" main v1.2.3 ci/deploy-status

export GITHUB_SERVER_URL="file://$tmp/remote" GITHUB_REPOSITORY=smileidentity/example
export GITHUB_ACTOR=octocat GITHUB_RUN_ID=42 GH_TOKEN=unused SHA=0123456789abcdef
export LABEL=

# A badge run is the check step followed by the badge step, as in the job.
badge() { (cd "$tmp" && rm -rf status && export ENVIRONMENT=$1 REF=$2 && bash -eo pipefail -c "$(step validate)" && bash -eo pipefail -c "$(step badge)"); }
show() { git --git-dir="$tmp/remote/smileidentity/example.git" show "deploy-status:$1"; }

# A tag deploy creates the branch and a green badge.
badge production v1.2.3
show production.svg > "$tmp/p.svg"
xmllint --noout "$tmp/p.svg" || fail "production badge is not valid XML"
grep -q '>v1.2.3<' "$tmp/p.svg" || fail "production badge does not show the tag"
grep -q '#4c1' "$tmp/p.svg" || fail "tag badge is not green"

# A branch deploy to another environment keeps the first badge, escapes the name and is orange.
badge staging 'feat/a&b<c>'
show production.svg > /dev/null || fail "staging deploy removed the production badge"
show staging.svg > "$tmp/s.svg"
xmllint --noout "$tmp/s.svg" || fail "branch badge is not valid XML"
grep -q 'feat/a&amp;b&lt;c&gt;' "$tmp/s.svg" || fail "branch name is not escaped"
grep -q '#fe7d37' "$tmp/s.svg" || fail "branch badge is not orange"

# Redeploying the same ref still records a new commit, so the time in the tooltip moves on.
before=$(git --git-dir="$tmp/remote/smileidentity/example.git" rev-parse deploy-status)
badge production v1.2.3
after=$(git --git-dir="$tmp/remote/smileidentity/example.git" rev-parse deploy-status)
[ "$before" != "$after" ] || fail "redeploy did not update the badge"

# Another deploy pushes between this one's fetch and push: retry, and keep both badges.
# A global pre-push hook plays the other deploy, once.
mkdir -p "$tmp/hooks" "$tmp/home"
cat > "$tmp/hooks/pre-push" <<'EOF2'
#!/usr/bin/env bash
[ -e "$RACE_DONE" ] && exit 0
touch "$RACE_DONE"
git clone -q --branch deploy-status "$RACE_REMOTE" "$RACE_DIR"
cd "$RACE_DIR" && echo '<svg/>' > other.svg && git add other.svg
git -c user.name=t -c user.email=t@example.com commit -qm other && git push -q origin deploy-status
EOF2
chmod +x "$tmp/hooks/pre-push"
printf '[core]\n\thooksPath = %s\n' "$tmp/hooks" > "$tmp/home/.gitconfig"
HOME="$tmp/home" RACE_DONE="$tmp/race-done" RACE_REMOTE="$tmp/remote/smileidentity/example.git" RACE_DIR="$tmp/racer" badge race v1.2.3
[ -e "$tmp/race-done" ] || fail "the competing push never ran"
show other.svg > /dev/null || fail "the competing deploy's badge was lost"
show race.svg > /dev/null || fail "the badge was not pushed after a rejected push"

# Without a commit, the tooltip leaves it out.
(export SHA=; badge nosha v1.2.3)
show nosha.svg | grep -q '<title>nosha: v1.2.3, deployed' || fail "badge without a commit has the wrong title"

# Environment names become file names, so anything path-like is refused.
if badge ../escape v1.2.3 > /dev/null 2>&1; then fail "path-like environment was accepted"; fi

# The commit goes into the SVG and the canvas line, so only a hex id is accepted.
if (export SHA='<x&y'; badge badsha v1.2.3) > /dev/null 2>&1; then fail "non-hex sha was accepted"; fi

# The Slack step must not run when the check fails, even though it runs after a failed badge.
yq '.jobs.status.steps[] | select(.id == "slack") | .if' "$workflow" | grep -q "steps.validate.outcome == 'success'" \
  || fail "Slack step does not depend on the input check"

# Slack: a fake curl records each call's method and body and answers from canned replies.
mkdir -p "$tmp/bin"
cat > "$tmp/bin/curl" <<'EOF'
#!/usr/bin/env bash
url=${*: -1}; body=
while [ $# -gt 0 ]; do
  case $1 in
    --data) body=$2 ;;
    -H) case $2 in @*) cat "${2#@}" > "$CALLS.auth" ;; esac ;;
  esac
  shift
done
method=${url##*/}
printf '%s %s\n' "$method" "$body" >> "$CALLS"
if [ -f "$REPLIES/$method.once" ]; then cat "$REPLIES/$method.once"; rm "$REPLIES/$method.once"; exit; fi
case $body in *'"section_types":["h2"]'*) cat "$REPLIES/$method.h2" ;; *) cat "$REPLIES/$method" ;; esac
EOF
chmod +x "$tmp/bin/curl"
export CALLS="$tmp/calls" REPLIES="$tmp/replies" CANVAS_ID=F123 SLACK_TOKEN=xoxb-test
mkdir -p "$REPLIES"
echo '{"ok":true}' > "$REPLIES/canvases.edit"
slack() { (: > "$CALLS"; ENVIRONMENT=production REF=v1.2.3 PATH="$tmp/bin:$PATH" bash -eo pipefail -c "$(step slack)"); }

key=$(printf 'example/production' | sha256sum | cut -c1-8)
edits() { grep '^canvases.edit ' "$CALLS"; }

# The environment's line exists: replace it.
echo '{"ok":true,"sections":[{"id":"temp:C:line"}]}' > "$REPLIES/canvases.sections.lookup"
slack
grep -q "^canvases.sections.lookup .*\"section_types\":\[\"list\"\],\"contains_text\":\"$key\"" "$CALLS" || fail "lookup did not search for the line's id"
edits | grep -q '"operation":"replace".*"section_id":"temp:C:line"' || fail "existing line was not replaced"
edits | grep -q "production\*\*: \`v1.2.3\` (0123456).* · id $key" || fail "line does not show the ref, commit and id"
[ "$(edits | wc -l)" -eq 1 ] || fail "expected one edit"
[ "$(cat "$CALLS.auth")" = 'Authorization: Bearer xoxb-test' ] || fail "Slack token was not sent as a bearer header"

# A ref cannot break out of its code span.
(: > "$CALLS"; ENVIRONMENT=production REF='a`b](https://x)' PATH="$tmp/bin:$PATH" bash -eo pipefail -c "$(step slack)") > /dev/null
edits | grep -q '`ab](https://x)`' || fail "backtick in the ref was not removed"

# No line yet, but the repo has a heading: add the line under it.
echo '{"ok":true,"sections":[]}' > "$REPLIES/canvases.sections.lookup"
echo '{"ok":true,"sections":[{"id":"temp:C:head"}]}' > "$REPLIES/canvases.sections.lookup.h2"
slack
edits | grep -q '"operation":"insert_after".*"section_id":"temp:C:head"' || fail "line was not added under the repo heading"

# Neither: add a heading and the line at the end.
echo '{"ok":true,"sections":[]}' > "$REPLIES/canvases.sections.lookup.h2"
slack
edits | grep -q '"operation":"insert_at_end".*"markdown":"## example\\n- \*\*production' || fail "heading and line were not added at the end"
edits | grep -q '"section_id"' && fail "insert_at_end sent a section id"

# Another deploy is editing the canvas: try again.
echo '{"ok":true,"sections":[{"id":"temp:C:line"}]}' > "$REPLIES/canvases.sections.lookup"
echo '{"ok":false,"error":"canvas_editing_locked"}' > "$REPLIES/canvases.edit.once"
slack > "$tmp/out" 2>&1
[ "$(edits | wc -l)" -eq 2 ] || fail "locked edit was not retried"
grep -q '::warning::' "$tmp/out" && fail "a retried edit still warned"

echo '{"ok":false,"error":"missing_scope"}' > "$REPLIES/canvases.sections.lookup"
slack > "$tmp/out" 2>&1 || fail "a Slack error failed the step"
grep -q '::warning::.*missing_scope' "$tmp/out" || fail "Slack error was not reported as a warning"

(: > "$CALLS"; SLACK_TOKEN= ENVIRONMENT=production REF=v1.2.3 PATH="$tmp/bin:$PATH" bash -eo pipefail -c "$(step slack)") > /dev/null
[ ! -s "$CALLS" ] || fail "Slack was called without a token"

echo "All deploy-status checks passed"
