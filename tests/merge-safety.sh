#!/usr/bin/env bash
# Runs the merge-safety classify step against fixture file lists and commits.
# Needs bash, jq and yq (mikefarah).
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
script=$(yq '.jobs.merge-safety.steps[] | select(.id == "classify") | .run' "$root/.github/workflows/merge-safety.yml")
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
failed=0

dependabot() { jq -nc --arg m "$2" --argjson v "${3:-true}" '{login: "dependabot[bot]", verified: $v, message: $m}' >> "$1/commits.jsonl"; }
human() { jq -nc '{login: "octocat", verified: false, message: "Change"}' >> "$1/commits.jsonl"; }
patch='updated-dependencies:
- dependency-name: left-pad
  update-type: version-update:semver-patch'
minor='updated-dependencies:
- dependency-name: right-pad
  update-type: version-update:semver-minor'
major='updated-dependencies:
- dependency-name: up-pad
  update-type: version-update:semver-major'

# check NAME EXPECTED_CLASS AUTHOR FILE... (FILE may be "new<TAB>old" for a rename)
# Set FETCH or CHANGED before calling to override the defaults; commits come from setup_commits.
check() {
  local name=$1 want=$2 author=$3 dir="$tmp/$1"
  shift 3
  mkdir -p "$dir"
  : > "$dir/files.tsv"
  for f in "$@"; do printf '%s\n' "$f" >> "$dir/files.tsv"; done
  [[ -f $dir/commits.jsonl ]] || human "$dir"
  local got
  got=$(cd "$dir" && : > out && : > summary &&
    FETCH=${FETCH:-success} AUTHOR=$author CHANGED_FILES=${CHANGED:-$#} HEAD_SHA=abc RULES=test \
    GITHUB_OUTPUT=out GITHUB_STEP_SUMMARY=summary bash -eo pipefail -c "$script" > /dev/null &&
    sed -n 's/^class=//p' out)
  if [[ $got == "$want" ]]; then echo "ok   $name: $got"; else echo "FAIL $name: got '$got', want '$want'" >&2; failed=1; fi
}

check docs docs-only octocat README.md docs/guide.md docs/img/flow.png .github/pull_request_template.md
check tests-and-docs tests-only octocat src/__tests__/a.test.ts tests/fixtures/x.json README.md
check rspec tests-only octocat spec/models/user_spec.rb spec/spec_helper.rb
check openapi-spec-is-code code octocat spec/openapi.yaml
check ios-tests tests-only octocat SmileIDTests/CaptureTests.swift
check android-tests tests-only octocat lib/src/test/java/FooTest.kt lib/src/androidTest/BarTest.kt
check ci ci-config octocat .github/workflows/ci.yml README.md tests/a_test.go
check codeowners ci-config octocat .github/CODEOWNERS
check agent-instructions code octocat AGENTS.md
check skill code octocat skills/triage/SKILL.md
check code code octocat src/index.ts README.md
check rename-out-of-code code octocat $'docs/old.ts\tsrc/old.ts'
check human-lockfile code octocat package-lock.json
check empty unknown octocat
CHANGED=3001 check truncated unknown octocat README.md
FETCH=failure check fetch-failed unknown octocat README.md

mkdir -p "$tmp/dep-patch" && dependabot "$tmp/dep-patch" "$patch"
check dep-patch deps-patch 'dependabot[bot]' package.json package-lock.json

mkdir -p "$tmp/dep-group" && dependabot "$tmp/dep-group" "$patch
$minor"
check dep-group deps-minor 'dependabot[bot]' web/package.json web/yarn.lock

mkdir -p "$tmp/dep-major" && dependabot "$tmp/dep-major" "$minor
$major"
check dep-major deps-major 'dependabot[bot]' go.mod go.sum

mkdir -p "$tmp/dep-no-type" && dependabot "$tmp/dep-no-type" "Bump left-pad from 1.0.0 to 1.0.1"
check dep-no-type deps-major 'dependabot[bot]' Gemfile.lock

mkdir -p "$tmp/dep-actions" && dependabot "$tmp/dep-actions" "$minor"
check dep-actions deps-minor 'dependabot[bot]' .github/workflows/ci.yml .github/actions/setup/action.yml

mkdir -p "$tmp/dep-unsigned" && dependabot "$tmp/dep-unsigned" "$patch" false
check dep-unsigned code 'dependabot[bot]' package.json

mkdir -p "$tmp/dep-human-commit" && dependabot "$tmp/dep-human-commit" "$patch" && human "$tmp/dep-human-commit"
check dep-human-commit code 'dependabot[bot]' package.json

mkdir -p "$tmp/dep-extra-file" && dependabot "$tmp/dep-extra-file" "$patch"
check dep-extra-file code 'dependabot[bot]' package.json src/index.ts

exit $failed
