#!/usr/bin/env python3
"""Logic behind sdk-release-train.yml. Standard library only.

All seven V3 SDKs share one version number. The train releases only the SDKs
with unreleased changes, all at one version per run: the highest bump any of
them needs, applied to the highest version any SDK has released. SDKs with
nothing to release skip that number.

Subcommands:
  plan     work out which SDKs are due a release, and the shared version
  prepare  run the SDK's bump script and open or update its release PR
  land     merge an approved, green batch of release PRs, then tag every
           merged release PR that has no tag yet

A person approves every release PR. The train merges a batch only when every
PR in it is approved and green, and never merges a major release. Approving a
release PR approves its tag: the SDK's release.yml publishes from the tag.

Every call to git or gh goes through run(), so tests can replace it.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import subprocess
import sys

ORG = "smileidentity"
SDK_REPOS = [
    "smileid-sdk-go",
    "smileid-sdk-java",
    "smileid-sdk-js",
    "smileid-sdk-php",
    "smileid-sdk-python",
    "smileid-sdk-ruby",
    "smileid-sdk-rust",
]
LABEL = "release"
BREAKING_LABEL = "breaking"
BRANCH_PREFIX = "release/v"
CADENCES = ("manual", "on-merge", "daily", "weekly", "monthly")
PARTS = ("patch", "minor", "major")
MARKER = "release-train-state"
# Release PRs merged longer ago than this are never tagged by the train.
TAG_WINDOW_DAYS = 14
GIT_BASE = os.environ.get("RELEASE_TRAIN_GIT_BASE", "https://github.com")
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class TrainError(Exception):
    """A problem the workflow should report as an error annotation."""


# ---------------------------------------------------------------------------
# Running commands


def run(cmd, *, cwd=None, env=None, check=True, input_text=None):
    """Run a command and return its stdout. Tests replace this function."""
    result = subprocess.run(
        cmd, cwd=cwd, env={**os.environ, **(env or {})}, input=input_text,
        text=True, capture_output=True, check=False,
    )
    if check and result.returncode != 0:
        raise TrainError(f"{' '.join(cmd[:3])} failed ({result.returncode}): {result.stderr.strip()[-2000:]}")
    return result.stdout if check else (result.returncode, result.stdout, result.stderr)


def gh(args, token, **kwargs):
    return run(["gh", *args], env={"GH_TOKEN": token}, **kwargs)


def git_auth_env(token):
    """Pass the token to git in the environment, never in argv or .git/config."""
    if not token:
        return {}
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    print(f"::add-mask::{auth}", flush=True)
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth}",
    }


GIT_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


def git(args, cwd, token=None, **kwargs):
    return run(["git", *GIT_SAFE, *args], cwd=cwd, env=git_auth_env(token), **kwargs)


# ---------------------------------------------------------------------------
# Versions and changelogs


def parse_version(text):
    match = VERSION_RE.match(text or "")
    if not match:
        raise TrainError(f"'{text}' is not a version like 13.2.0")
    return tuple(int(part) for part in match.groups())


def format_version(parts):
    return ".".join(str(p) for p in parts)


def bump(version, part):
    major, minor, patch = parse_version(version)
    if part == "major":
        return format_version((major + 1, 0, 0))
    if part == "minor":
        return format_version((major, minor + 1, 0))
    if part == "patch":
        return format_version((major, minor, patch + 1))
    raise TrainError(f"unknown part {part!r}")


def latest_release(tags):
    """The highest vX.Y.Z tag, as a version string, or None."""
    versions = [tuple(int(p) for p in m.groups()) for m in (TAG_RE.match(t) for t in tags) if m]
    return format_version(max(versions)) if versions else None


def unreleased(changelog):
    """The text under '## [Unreleased]', up to the next '## ' heading."""
    lines, inside = [], False
    for line in (changelog or "").splitlines():
        if re.match(r"^## \[Unreleased\]\s*$", line):
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside:
            lines.append(line)
    return "\n".join(lines).strip()


def needed_part(notes, breaking_prs):
    """The bump a set of unreleased notes needs, or None when there is nothing to release.

    A merged PR labelled breaking means major, which is the rule each SDK's
    release check enforces. Added or Deprecated entries mean minor. Anything
    else, such as Fixed, Changed or Security, is a patch.
    """
    if not notes:
        return None
    if breaking_prs:
        return "major"
    headings = {h.strip().lower() for h in re.findall(r"^###\s+(.+)$", notes, re.MULTILINE)}
    if headings & {"added", "deprecated"}:
        return "minor"
    return "patch"


def highest_part(parts):
    return max(parts, key=PARTS.index)


# ---------------------------------------------------------------------------
# Cadence


def short(repo):
    return repo.removeprefix("smileid-sdk-")


def parse_cadence(default, overrides_json):
    if default not in CADENCES:
        raise TrainError(f"SDK_RELEASE_CADENCE is {default!r}; use one of {', '.join(CADENCES)}")
    overrides = {}
    if (overrides_json or "").strip():
        try:
            raw = json.loads(overrides_json)
        except json.JSONDecodeError as e:
            raise TrainError(f"SDK_RELEASE_CADENCE_OVERRIDES is not JSON: {e}") from e
        if not isinstance(raw, dict):
            raise TrainError('SDK_RELEASE_CADENCE_OVERRIDES must be a JSON object, such as {"rust": "weekly"}')
        names = {short(r) for r in SDK_REPOS}
        for key, value in raw.items():
            if key not in names:
                raise TrainError(f"SDK_RELEASE_CADENCE_OVERRIDES names {key!r}, which is not an SDK")
            if value not in CADENCES:
                raise TrainError(f"SDK_RELEASE_CADENCE_OVERRIDES sets {key} to {value!r}; use one of {', '.join(CADENCES)}")
            overrides[key] = value
    return {repo: overrides.get(short(repo), default) for repo in SDK_REPOS}


def is_due(cadence, *, today, daily_run):
    """Whether a scheduled run should release an SDK with this cadence.

    Hourly runs only release on-merge SDKs; the daily run releases the rest
    when their day comes round. Manual runs skip this check.
    """
    if cadence == "on-merge":
        return True
    if not daily_run:
        return False
    if cadence == "daily":
        return True
    if cadence == "weekly":
        return today.weekday() == 0
    if cadence == "monthly":
        return today.weekday() == 0 and today.day <= 7
    return False


def select_repos(repos):
    if not (repos or "").strip():
        return list(SDK_REPOS)
    chosen = []
    for name in (p.strip() for p in repos.split(",")):
        if not name:
            continue
        repo = name if name.startswith("smileid-sdk-") else f"smileid-sdk-{name}"
        if repo not in SDK_REPOS:
            raise TrainError(f"'{name}' is not an SDK; use go, java, js, php, python, ruby or rust")
        if repo not in chosen:
            chosen.append(repo)
    return chosen


# ---------------------------------------------------------------------------
# Planning


def plan(sdks, *, cadences, today, daily_run, manual, repos=""):
    """Choose the SDKs to release and the shared version.

    sdks maps each repository to {"tags": [...], "changelog": text,
    "breaking_prs": [...]}; a repository that couldn't be read is missing.
    Returns {"version", "part", "units": [...], "skipped": {repo: reason}}.
    """
    chosen = select_repos(repos) if manual else list(SDK_REPOS)
    current = [v for v in (latest_release(info["tags"]) for info in sdks.values()) if v]
    if not current:
        raise TrainError("No SDK has a vX.Y.Z release tag, so there is no version to build on")
    base = max(current, key=parse_version)
    units, parts, skipped = [], [], {}
    for repo in SDK_REPOS:
        if repo not in chosen:
            continue
        if repo not in sdks:
            skipped[repo] = "could not be read"
            continue
        cadence = cadences[repo]
        if not manual and not is_due(cadence, today=today, daily_run=daily_run):
            skipped[repo] = f"cadence is {cadence}, not due"
            continue
        info = sdks[repo]
        notes = unreleased(info["changelog"])
        part = needed_part(notes, info["breaking_prs"])
        if part is None:
            skipped[repo] = "nothing under Unreleased"
            continue
        parts.append(part)
        units.append({"repo": repo, "part": part, "current": latest_release(info["tags"]),
                      "breaking_prs": info["breaking_prs"]})
    if not units:
        return {"version": "", "part": "", "units": [], "skipped": skipped}
    part = highest_part(parts)
    version = bump(base, part)
    for u in units:
        u["version"] = version
    return {"version": version, "part": part, "units": units, "skipped": skipped}


def fetch_sdk(repo, token):
    """Tags, CHANGELOG.md on the default branch, and breaking PRs since the last tag."""
    tags = [line.split("refs/tags/", 1)[1] for line in
            run(["git", "ls-remote", "--tags", "--refs", f"{GIT_BASE}/{ORG}/{repo}.git"],
                env=git_auth_env(token)).splitlines() if "refs/tags/" in line]
    content = json.loads(gh(["api", f"repos/{ORG}/{repo}/contents/CHANGELOG.md"], token))
    changelog = base64.b64decode(content["content"]).decode()
    latest = latest_release(tags)
    breaking = []
    if latest:
        # The commit date of the last release; a PR labelled breaking that merged
        # after it is unreleased. Each SDK's release check repeats this exactly.
        since = gh(["api", f"repos/{ORG}/{repo}/commits/v{latest}", "--jq", ".commit.committer.date"], token).strip()
        found = json.loads(gh(["pr", "list", "--repo", f"{ORG}/{repo}", "--state", "merged", "--label", BREAKING_LABEL,
                               "--search", f"merged:>{since}", "--json", "number,title,url", "--limit", "100"], token)
                           or "[]")
        breaking = [{"number": p["number"], "title": p["title"], "url": p["url"]} for p in found]
    return {"tags": tags, "changelog": changelog, "breaking_prs": breaking}


# ---------------------------------------------------------------------------
# Release PRs


def read_marker(body):
    match = re.search(rf"<!-- {MARKER}: (.*?) -->", body or "")
    if not match:
        return {}
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def write_marker(value):
    return f"<!-- {MARKER}: {json.dumps(value, sort_keys=True)} -->"


def release_title(version):
    return f"Release v{version}"


def render_pr_body(*, u, notes, base_sha, run_url):
    major = u["part"] == "major"
    lines = [
        f"Releases v{u['version']} of this SDK. All SDKs with unreleased changes release at v{u['version']} together.",
        "",
        "## What merging does",
        "",
        "Approving this pull request approves the release.",
        "",
    ]
    if major:
        lines += [
            "This is a major release, so the release train won't merge it. Merge it yourself once it's approved; "
            "the train then pushes the tag.",
        ]
    else:
        lines += [
            "Once every release pull request in this batch is approved and its checks pass, the release train merges "
            "them all and pushes the tags. The SDK's release workflow then publishes the package.",
        ]
    lines += [
        "",
        "A new commit on `main` before then makes the train rebuild this pull request, which dismisses approvals.",
        "",
        "## Version",
        "",
        f"- Last release: v{u['current']}" if u.get("current") else "- Last release: none",
        f"- This SDK needs a {u['part']} release.",
    ]
    if u.get("breaking_prs"):
        lines.append("- Pull requests labelled breaking since the last release:")
        lines += [f"  - {p['url']} {p['title']}" for p in u["breaking_prs"]]
    lines += ["", "## Changes", "", notes or "(none)", ""]
    lines += [
        f"Made by the [release train]({run_url}).",
        "",
        write_marker({"version": u["version"], "base_sha": base_sha}),
    ]
    return "\n".join(lines)


def open_release_prs(repo, token):
    out = gh(["pr", "list", "--repo", f"{ORG}/{repo}", "--label", LABEL, "--state", "open",
              "--json", "number,url,title,body,headRefName,isCrossRepository,headRefOid", "--limit", "20"], token)
    return [pr for pr in json.loads(out or "[]")
            if not pr.get("isCrossRepository", True) and pr.get("headRefName", "").startswith(BRANCH_PREFIX)]


def prepare(*, u, workdir, token, bot_name, bot_email, run_url, reviewers, today):
    """Run the SDK's bump script at the shared version and open or update its release PR.

    Leaves an open release PR alone when it is already at this version and built
    on the current default branch, so its approvals are kept.
    """
    repo, version = u["repo"], u["version"]
    full = f"{ORG}/{repo}"
    branch = f"{BRANCH_PREFIX}{version}"
    git(["clone", "--quiet", f"{GIT_BASE}/{full}.git", workdir], cwd=None, token=token)
    base_sha = git(["rev-parse", "HEAD"], cwd=workdir).strip()
    existing = open_release_prs(repo, token)
    for pr in existing:
        marker = read_marker(pr.get("body"))
        if pr["headRefName"] == branch and marker.get("base_sha") == base_sha and marker.get("version") == version:
            print(f"{pr['url']} is already v{version} on the current {repo} main; left alone.")
            return {"repo": repo, "status": "unchanged", "url": pr["url"]}

    notes = unreleased(git(["show", "HEAD:CHANGELOG.md"], cwd=workdir))
    git(["checkout", "--quiet", "-B", branch], cwd=workdir)
    # The bump script is the SDK's own, from its default branch. It gets no token.
    code, out, err = run(["bash", "scripts/bump-version.sh", version], cwd=workdir,
                         env={"BUMP_DATE": today, "RELEASE_DATE": today, "BUMP_VERSION_DATE": today,
                              "GH_TOKEN": "", "GITHUB_TOKEN": ""}, check=False)
    if code != 0:
        raise TrainError(f"{repo}: scripts/bump-version.sh {version} failed: {(err or out).strip()[-1500:]}")
    printed = (out or "").strip().splitlines()[-1:] or [""]
    if printed[0].strip() != version:
        raise TrainError(f"{repo}: scripts/bump-version.sh printed {printed[0]!r}, not {version}")
    git(["add", "--all"], cwd=workdir)
    git(["-c", f"user.name={bot_name}", "-c", f"user.email={bot_email}", "commit", "--quiet", "-m",
         f"Release v{version}"], cwd=workdir)
    git(["push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{branch}"], cwd=workdir, token=token)

    body = render_pr_body(u=u, notes=notes, base_sha=base_sha, run_url=run_url)
    current = next((pr for pr in existing if pr["headRefName"] == branch), None)
    if current:
        gh(["pr", "edit", str(current["number"]), "--repo", full, "--title", release_title(version), "--body", body],
           token)
        url, status = current["url"], "updated"
    else:
        url = gh(["pr", "create", "--repo", full, "--base", default_branch(repo, token), "--head", branch,
                  "--title", release_title(version), "--body", body, "--label", LABEL], token).strip()
        status = "opened"
        number = url.rstrip("/").rsplit("/", 1)[-1]
        for reviewer in reviewers:
            code, _, err = gh(["api", "-X", "POST", f"repos/{full}/pulls/{number}/requested_reviewers",
                               "-f", f"reviewers[]={reviewer}"], token, check=False)
            if code != 0:
                print(f"::warning::Could not ask {reviewer} to review {url}: {err.strip()[-300:]}")
    # A release PR for an older version is out of date: this one replaces it.
    for pr in existing:
        if pr["headRefName"] != branch:
            gh(["pr", "close", str(pr["number"]), "--repo", full, "--delete-branch", "--comment",
                f"Replaced by {url}, which releases v{version}."], token)
            print(f"Closed {pr['url']}: replaced by {url}")
    print(f"{status.capitalize()} {url}")
    return {"repo": repo, "status": status, "url": url}


def default_branch(repo, token):
    return gh(["api", f"repos/{ORG}/{repo}", "--jq", ".default_branch"], token).strip()


# ---------------------------------------------------------------------------
# Landing: merge an approved batch, then tag


def checks_state(rollup):
    """Summarise a statusCheckRollup as success, pending, failure or none."""
    if not rollup:
        return "none"
    state = "success"
    latest, running = {}, set()
    for item in rollup:
        if item.get("__typename") == "StatusContext" or "state" in item and "status" not in item:
            value = (item.get("state") or "").upper()
            if value in ("PENDING", "EXPECTED"):
                state = "pending"
            elif value != "SUCCESS":
                return "failure"
            continue
        key = (item.get("workflowName") or "", item.get("name") or "")
        if (item.get("status") or "").upper() != "COMPLETED":
            state = "pending"
            running.add(key)
        elif key not in latest or (item.get("startedAt") or "") > (latest[key].get("startedAt") or ""):
            latest[key] = item
    for key, item in latest.items():
        if key not in running and (item.get("conclusion") or "").upper() not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            return "failure"
    return state


def pr_version(pr):
    version = pr["headRefName"].removeprefix(BRANCH_PREFIX)
    return version if VERSION_RE.match(version) else None


def blockers(pr, version):
    """Why a release PR can't merge yet; empty when it can."""
    problems = []
    if pr.get("isDraft"):
        problems.append("it is a draft")
    if pr.get("reviewDecision") != "APPROVED":
        problems.append("it is not approved")
    checks = checks_state(pr.get("statusCheckRollup"))
    if checks != "success":
        problems.append(f"its checks are {checks}")
    # BEHIND means main moved on: the next planned run rebuilds the PR.
    merge_state = (pr.get("mergeStateStatus") or "").upper()
    if merge_state not in ("CLEAN", "HAS_HOOKS"):
        problems.append(f"its merge state is {merge_state.lower() or 'unknown'}")
    if read_marker(pr.get("body")).get("version") != version:
        problems.append("the release train did not make it")
    return problems


def find_batch(prs_by_repo):
    """The open release PRs at the newest version across SDKs, and any older ones left behind."""
    versions = {pr_version(pr) for prs in prs_by_repo.values() for pr in prs if pr_version(pr)}
    if not versions:
        return None, {}, []
    newest = max(versions, key=parse_version)
    batch, stale = {}, []
    for repo, prs in prs_by_repo.items():
        for pr in prs:
            if pr_version(pr) == newest:
                batch[repo] = pr
            else:
                stale.append(pr)
    return newest, batch, stale


def land_batch(prs_by_repo, token, latest_versions):
    """Merge the batch when every PR in it is approved and green. Returns a report."""
    version, batch, stale = find_batch(prs_by_repo)
    for pr in stale:
        print(f"::warning::{pr['url']} is an older release PR than v{version}; the next planned run closes it.")
    if not batch:
        return {"version": None, "merged": [], "waiting": {}}
    waiting = {repo: blockers(pr, version) for repo, pr in batch.items()}
    waiting = {repo: why for repo, why in waiting.items() if why}
    base = max(latest_versions.values(), key=parse_version) if latest_versions else None
    if base and parse_version(version)[0] > parse_version(base)[0]:
        print(f"::notice::v{version} is a major release, so the train won't merge it. Merge each PR yourself once "
              "it's approved; the train pushes the tags.")
        return {"version": version, "merged": [], "waiting": waiting, "major": True}
    if waiting:
        for repo, why in waiting.items():
            print(f"{batch[repo]['url']} can't merge yet: {'; '.join(why)}.")
        print(f"Waiting: the v{version} batch merges once every PR in it is approved and green.")
        return {"version": version, "merged": [], "waiting": waiting}
    merged = []
    for repo, pr in batch.items():
        full = f"{ORG}/{repo}"
        code, _, err = gh(["api", "-X", "PUT", f"repos/{full}/pulls/{pr['number']}/merge",
                           "-f", "merge_method=squash", "-f", f"sha={pr['headRefOid']}",
                           "-f", f"commit_title={release_title(version)} (#{pr['number']})"], token, check=False)
        if code != 0:
            print(f"::error::Could not merge {pr['url']}: {err.strip()[-500:]}")
            continue
        merged.append(repo)
        print(f"Merged {pr['url']}")
        gh(["api", "-X", "DELETE", f"repos/{full}/git/refs/heads/{pr['headRefName']}"], token, check=False)
    return {"version": version, "merged": merged, "waiting": {}}


def tag_merged(repo, token, latest, now):
    """Push the tag for each recently merged release PR that has none. Returns the tags pushed."""
    full = f"{ORG}/{repo}"
    out = gh(["pr", "list", "--repo", full, "--label", LABEL, "--state", "merged", "--json",
              "number,url,headRefName,mergeCommit,mergedAt,isCrossRepository", "--limit", "20"], token)
    pushed = []
    cutoff = now - dt.timedelta(days=TAG_WINDOW_DAYS)
    for pr in sorted(json.loads(out or "[]"), key=lambda p: p.get("mergedAt") or ""):
        version = pr_version(pr)
        if not version or pr.get("isCrossRepository", True) or not pr.get("mergeCommit"):
            continue
        merged_at = dt.datetime.fromisoformat(pr["mergedAt"].replace("Z", "+00:00"))
        if merged_at < cutoff:
            continue
        if latest and parse_version(version) <= parse_version(latest):
            continue
        sha = pr["mergeCommit"]["oid"]
        code, _, err = gh(["api", "-X", "POST", f"repos/{full}/git/refs", "-f", f"ref=refs/tags/v{version}",
                           "-f", f"sha={sha}"], token, check=False)
        if code != 0:
            print(f"::error::Could not push v{version} to {full} at {sha}: {err.strip()[-500:]}")
            continue
        print(f"Pushed v{version} to {full} at {sha}, from {pr['url']}")
        pushed.append(f"v{version}")
        latest = version
    return pushed


def land(token, now, repos=None):
    repos = repos or list(SDK_REPOS)
    prs_by_repo, latest_versions = {}, {}
    for repo in repos:
        try:
            out = gh(["pr", "list", "--repo", f"{ORG}/{repo}", "--label", LABEL, "--state", "open", "--json",
                      "number,url,title,body,headRefName,headRefOid,isDraft,isCrossRepository,reviewDecision,"
                      "mergeStateStatus,statusCheckRollup", "--limit", "20"], token)
            prs_by_repo[repo] = [pr for pr in json.loads(out or "[]") if not pr.get("isCrossRepository", True)
                                 and pr_version(pr)]
            tags = [line.split("refs/tags/", 1)[1] for line in
                    run(["git", "ls-remote", "--tags", "--refs", f"{GIT_BASE}/{ORG}/{repo}.git"],
                        env=git_auth_env(token)).splitlines() if "refs/tags/" in line]
            latest_versions[repo] = latest_release(tags)
        except TrainError as e:
            print(f"::warning::Could not read {repo}, so its release PRs were left alone: {e}")
    latest_versions = {r: v for r, v in latest_versions.items() if v}
    report = land_batch(prs_by_repo, token, latest_versions)
    tags = {}
    for repo in repos:
        if repo not in latest_versions and repo not in prs_by_repo:
            continue
        try:
            pushed = tag_merged(repo, token, latest_versions.get(repo), now)
        except TrainError as e:
            print(f"::warning::Could not check {repo} for untagged releases: {e}")
            continue
        if pushed:
            tags[repo] = pushed
    report["tags"] = tags
    return report


# ---------------------------------------------------------------------------
# Summaries and CLI


def render_plan_summary(result, cadences):
    lines = ["## Release train plan", ""]
    if result["units"]:
        lines.append(f"Version for this run: **v{result['version']}** (a {result['part']} release).")
    else:
        lines.append("Nothing to release in this run.")
    lines += ["", "| SDK | Cadence | Last release | Result |", "|---|---|---|---|"]
    units = {u["repo"]: u for u in result["units"]}
    for repo in SDK_REPOS:
        u = units.get(repo)
        outcome = f"release v{u['version']} ({u['part']})" if u else result["skipped"].get(repo, "not selected")
        last = f"v{u['current']}" if u and u.get("current") else ""
        lines.append(f"| {short(repo)} | {cadences.get(repo, '')} | {last} | {outcome} |")
    return "\n".join(lines) + "\n"


def write_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def write_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    print(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--cadence", default="")
    p.add_argument("--overrides", default="")
    p.add_argument("--repos", default="")
    p.add_argument("--event", required=True, help="schedule or workflow_dispatch")
    p.add_argument("--daily-run", default="false")
    p = sub.add_parser("prepare")
    p.add_argument("--unit", required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument("--bot-name", required=True)
    p.add_argument("--bot-email", required=True)
    p.add_argument("--run-url", required=True)
    p.add_argument("--reviewers", default="")
    sub.add_parser("land")
    args = parser.parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    try:
        if args.command == "plan":
            token = os.environ.get("READ_TOKEN", "")
            cadences = parse_cadence(args.cadence or "manual", args.overrides)
            manual = args.event == "workflow_dispatch"
            sdks = {}
            for repo in SDK_REPOS:
                try:
                    sdks[repo] = fetch_sdk(repo, token)
                except TrainError as e:
                    print(f"::warning::Could not read {repo}, so it isn't released in this run: {e}")
            result = plan(sdks, cadences=cadences, today=now.date(), daily_run=args.daily_run == "true",
                          manual=manual, repos=args.repos)
            write_summary(render_plan_summary(result, cadences))
            write_output("has_work", "true" if result["units"] else "false")
            write_output("matrix", json.dumps({"include": result["units"]}, separators=(",", ":")))
        elif args.command == "prepare":
            u = json.loads(args.unit)
            reviewers = [r.strip() for r in args.reviewers.split(",") if r.strip()]
            prepare(u=u, workdir=args.workdir, token=os.environ["WRITE_TOKEN"], bot_name=args.bot_name,
                    bot_email=args.bot_email, run_url=args.run_url, reviewers=reviewers,
                    today=now.strftime("%Y-%m-%d"))
        elif args.command == "land":
            report = land(os.environ["WRITE_TOKEN"], now)
            lines = ["## Release train: merge and tag", ""]
            if report.get("version"):
                if report.get("merged"):
                    lines.append(f"Merged the v{report['version']} release PRs: {', '.join(map(short, report['merged']))}.")
                elif report.get("major"):
                    lines.append(f"v{report['version']} is a major release; merge its PRs by hand.")
                else:
                    lines.append(f"The v{report['version']} batch is waiting:")
                    lines += [f"- {short(r)}: {', '.join(w)}" for r, w in report["waiting"].items()]
            else:
                lines.append("No open release PRs.")
            for repo, pushed in report["tags"].items():
                lines.append(f"- Pushed {', '.join(pushed)} to {short(repo)}.")
            write_summary("\n".join(lines) + "\n")
    except TrainError as e:
        print(f"::error::{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
