#!/usr/bin/env python3
"""Logic behind sdk-release-train.yml. Standard library only.

All seven V3 SDKs share one version number. The train releases only the SDKs
with unreleased changes, all at one version per run: the highest bump any of
them needs, applied to the highest version any SDK has released. SDKs with
nothing to release skip that number.

Subcommands:
  plan     work out which SDKs are due a release, and the shared version
  clone    clone an SDK for the bump job (the only step there with a token, read-only)
  bump     run the SDK's bump script with no token and write the change as a patch
  publish  apply the patch to a fresh clone, push it and open or update the release PR
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
import urllib.request

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


# What a command run without the job's environment still gets. No variable
# that could hold a token or key is passed on.
CLEAN_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "RUNNER_TEMP")


def run(cmd, *, cwd=None, env=None, check=True, input_text=None, inherit_env=True):
    """Run a command and return its stdout. Tests replace this function.

    With inherit_env=False the command gets only CLEAN_ENV_KEYS from this
    process's environment, plus env.
    """
    base = os.environ if inherit_env else {k: os.environ[k] for k in CLEAN_ENV_KEYS if k in os.environ}
    result = subprocess.run(
        cmd, cwd=cwd, env={**base, **(env or {})}, input=input_text,
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
    "breaking_prs": [...], "open_versions": [...]}; open_versions are the
    versions of the train's open release PRs. A repository that couldn't be
    read is missing. Returns {"version", "part", "units": [...], "skipped": {repo: reason}}.

    While a batch is still landing, its version is the one to build on: an SDK
    whose PR in it is still open is planned at that version again, so a merge
    and tag in one SDK doesn't push the others up a whole version.
    """
    chosen = select_repos(repos) if manual else list(SDK_REPOS)
    current = [v for v in (latest_release(info["tags"]) for info in sdks.values()) if v]
    if not current:
        raise TrainError("No SDK has a vX.Y.Z release tag, so there is no version to build on")
    landing = landing_version(sdks)
    # A tag at the landing version comes from that batch, so it doesn't move the base.
    # (No tag is above it: landing_version ignores a batch something has gone past.)
    below = [v for v in current if not landing or parse_version(v) < parse_version(landing)]
    base = max(below or current, key=parse_version)
    units, parts, needs, skipped = [], [], [], {}
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
        mine = latest_release(info["tags"])
        # An SDK that already released the landing batch builds on that release,
        # so new changes in it move the batch on rather than joining it.
        if landing and not mine:
            # An SDK with no release yet can take any version, so it joins the landing batch.
            needs.append(landing)
        else:
            own_base = mine if mine and landing and parse_version(mine) >= parse_version(landing) else base
            needs.append(bump(own_base, part))
        parts.append(part)
        units.append({"repo": repo, "part": part, "current": mine, "breaking_prs": info["breaking_prs"]})
    if not units:
        return {"version": "", "part": "", "units": [], "skipped": skipped}
    part = highest_part(parts)
    version = max(needs, key=parse_version)
    # Join the landing batch when its version covers what every SDK here needs.
    if landing and parse_version(landing) >= parse_version(version):
        version = landing
    for u in units:
        u["version"] = version
    return {"version": version, "part": part, "units": units, "skipped": skipped}


def landing_version(sdks):
    """The version of a batch that is still landing, or None.

    That is the newest open train PR version that its own SDK hasn't released,
    and that no SDK has gone past. A PR left open below a released version
    belongs to a batch that was overtaken, so it doesn't count.
    """
    tags = [v for v in (latest_release(info["tags"]) for info in sdks.values()) if v]
    highest = max(tags, key=parse_version) if tags else None
    found = []
    for info in sdks.values():
        mine = latest_release(info["tags"])
        found += [v for v in info.get("open_versions", [])
                  if VERSION_RE.match(v) and (not mine or parse_version(v) > parse_version(mine))
                  and (not highest or parse_version(v) >= parse_version(highest))]
    return max(found, key=parse_version) if found else None


def fetch_sdk(repo, token, bot):
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
    open_versions = [pr_version(pr) for pr in open_release_prs(repo, token, bot) if pr_version(pr)]
    return {"tags": tags, "changelog": changelog, "breaking_prs": breaking, "open_versions": open_versions}


# ---------------------------------------------------------------------------
# Release PRs


def read_marker(body):
    """The state the train wrote at the end of a PR description.

    Only the last marker counts: the description includes changelog notes, and
    a marker inside them must not override the train's own.
    """
    found = re.findall(rf"<!-- {MARKER}: (.*?) -->", body or "")
    if not found:
        return {}
    try:
        value = json.loads(found[-1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def write_marker(value):
    return f"<!-- {MARKER}: {json.dumps(value, sort_keys=True)} -->"


def release_title(version):
    return f"Release v{version}"


def is_major_for(version, current):
    """Whether version is a new major for an SDK whose last release is current.

    An SDK with no release yet isn't judged here: its first release can't break
    anyone, and it mustn't hold back a batch.
    """
    return bool(current) and parse_version(version)[0] > parse_version(current)[0]


def render_pr_body(*, u, notes, base_sha, run_url, head_sha=""):
    major = is_major_for(u["version"], u.get("current"))
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
        f"- This SDK's changes need a {u['part']} release.",
    ]
    if u.get("breaking_prs"):
        lines.append("- Pull requests labelled breaking since the last release:")
        lines += [f"  - {p['url']} {p['title']}" for p in u["breaking_prs"]]
    lines += ["", "## Changes", "", notes or "(none)", ""]
    lines += [
        f"Made by the [release train]({run_url}).",
        "",
        write_marker({"version": u["version"], "base_sha": base_sha, "head_sha": head_sha, "major": major}),
    ]
    return "\n".join(lines)


def by_train(pr, bot):
    """Whether the train's bot opened the PR.

    gh shows a GitHub App author as app/<slug> with is_bot set. Only that form
    counts: the bare slug is how gh shows a user, and anyone could register a
    user login equal to the app's slug.
    """
    slug = (bot or "").removesuffix("[bot]")
    author = pr.get("author") or {}
    return bool(slug) and author.get("is_bot") is True and author.get("login") == f"app/{slug}"


def open_release_prs(repo, token, bot):
    """The open release PRs the train opened. A person's release PR is never updated or closed."""
    out = gh(["pr", "list", "--repo", f"{ORG}/{repo}", "--label", LABEL, "--state", "open",
              "--json", "number,url,title,body,headRefName,isCrossRepository,headRefOid,author", "--limit", "20"],
             token)
    return [pr for pr in json.loads(out or "[]")
            if not pr.get("isCrossRepository", True) and pr.get("headRefName", "").startswith(BRANCH_PREFIX)
            and by_train(pr, bot)]


def clone_sdk(repo, workdir, token=""):
    """Clone the SDK's default branch. The token is passed in the environment, never stored in .git/config."""
    git(["clone", "--quiet", f"{GIT_BASE}/{ORG}/{repo}.git", workdir], cwd=None, token=token)


def make_patch(*, u, workdir, out, today, runas=()):
    """Run the SDK's bump script in its clone and write the change as a patch, for the publish job.

    This runs in the bump job, which holds no write token: the script is the
    SDK's own code, and could do anything the job can. It runs without the
    job's environment, and so does every git command after it, because the
    script could have changed .git/config. Writes out/patch.diff and
    out/meta.json.

    runas is a command prefix, such as sudo -n -u <user> env -i ..., that runs
    the script and every git command as an unprivileged user of their own. In
    the workflow that user can't read the job's processes or files, and all its
    processes are killed before anything else in the job runs.
    """
    repo, version = u["repo"], u["version"]
    prefix = list(runas)
    # With a prefix, its env -C changes directory as the other user, who may own a home this user can't enter.
    where = None if prefix else workdir
    base_sha = run([*prefix, "git", *GIT_SAFE, "rev-parse", "HEAD"], cwd=where, inherit_env=False).strip()
    dates = {"BUMP_DATE": today, "RELEASE_DATE": today, "BUMP_VERSION_DATE": today}
    if prefix:
        # env -i in the prefix clears the environment, so pass the dates as arguments to it.
        command = [*prefix, *(f"{k}={v}" for k, v in dates.items()), "bash", "scripts/bump-version.sh", version]
        dates = {}
    else:
        command = ["bash", "scripts/bump-version.sh", version]
    code, stdout, err = run(command, cwd=where, env=dates, check=False, inherit_env=False)
    if code != 0:
        raise TrainError(f"{repo}: scripts/bump-version.sh {version} failed: {(err or stdout).strip()[-1500:]}")
    printed = (stdout or "").strip().splitlines()[-1:] or [""]
    if printed[0].strip() != version:
        raise TrainError(f"{repo}: scripts/bump-version.sh printed {printed[0]!r}, not {version}")
    run([*prefix, "git", *GIT_SAFE, "add", "--all"], cwd=where, inherit_env=False)
    patch = run([*prefix, "git", *GIT_SAFE, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv"],
                cwd=where, inherit_env=False)
    if not patch.strip():
        raise TrainError(f"{repo}: scripts/bump-version.sh {version} changed nothing")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "patch.diff"), "w", encoding="utf-8") as f:
        f.write(patch)
    with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"repo": repo, "version": version, "base_sha": base_sha}, f)


SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def read_bundle(bundle, u):
    """Read and check the bump job's patch and meta.json."""
    try:
        with open(os.path.join(bundle, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        with open(os.path.join(bundle, "patch.diff"), encoding="utf-8") as f:
            patch = f.read()
    except (OSError, ValueError) as e:
        raise TrainError(f"{u['repo']}: the bump job's bundle is missing or unreadable, so nothing was pushed: {e}")
    if not isinstance(meta, dict) or (meta.get("repo"), meta.get("version")) != (u["repo"], u["version"]):
        raise TrainError(f"{u['repo']}: the bump job's bundle is for another release, so nothing was pushed")
    if not SHA_RE.match(str(meta.get("base_sha", ""))):
        raise TrainError(f"{u['repo']}: the bump job's bundle has no valid base commit, so nothing was pushed")
    return meta, patch


def publish(*, u, bundle, workdir, token, bot_name, bot_email, run_url, reviewers):
    """Apply the bump job's patch to a fresh clone, push it and open or update the release PR.

    This job holds the write token, so it never runs the SDK's code: only git
    and gh, on a clone whose .git/config nothing else has touched. Leaves an
    open release PR alone when it is already at this version and built on the
    current default branch, so its approvals are kept.
    """
    repo, version = u["repo"], u["version"]
    full = f"{ORG}/{repo}"
    branch = f"{BRANCH_PREFIX}{version}"
    meta, patch = read_bundle(bundle, u)
    clone_sdk(repo, workdir, token)
    base_sha = git(["rev-parse", "HEAD"], cwd=workdir).strip()
    if base_sha != meta["base_sha"]:
        raise TrainError(f"{repo}: the default branch moved after the bump job ran, so nothing was pushed. "
                         "The next run bumps it again.")
    existing = open_release_prs(repo, token, bot_name)
    current = next((pr for pr in existing if pr["headRefName"] == branch), None)
    if current:
        marker = read_marker(current.get("body"))
        if marker.get("base_sha") == base_sha and marker.get("version") == version:
            print(f"{current['url']} is already v{version} on the current {repo} main; left alone.")
            return {"repo": repo, "status": "unchanged", "url": current["url"]}
        # Someone pushed to the train's branch: don't throw their commit away.
        if current.get("headRefOid") != marker.get("head_sha"):
            raise TrainError(f"{current['url']} has commits the release train didn't push, so it wasn't rebuilt. "
                             "Close the PR, or reset its branch, and the next run carries on.")
    check_branch_is_ours(repo, branch, workdir, token, bot_name, existing)
    # The notes come from this job's own clone, never from the bundle.
    notes = unreleased(git(["show", "HEAD:CHANGELOG.md"], cwd=workdir))
    if current:
        # Keep the PR's current commit, to put back if the PR can't be updated.
        git(["fetch", "--quiet", "origin", f"refs/heads/{branch}"], cwd=workdir, token=token)

    git(["checkout", "--quiet", "-B", branch], cwd=workdir)
    git(["apply", "--index", "--binary", "-"], cwd=workdir, input_text=patch)
    # Raw paths: git quotes unusual names unless told not to, and a quoted
    # ".github/..." wouldn't start with ".github/".
    touched = [path for path in git(["-c", "core.quotePath=false", "diff", "--cached", "--no-renames",
                                     "--name-only", "-z"], cwd=workdir).split("\0") if path]
    bad = [path for path in touched if path.startswith(".github/") or path == ".gitmodules"]
    if bad:
        raise TrainError(f"{repo}: the bump changed {', '.join(bad)}, which a release must not change, so nothing "
                         "was pushed")
    git(["-c", f"user.name={bot_name}", "-c", f"user.email={bot_email}", "commit", "--quiet", "-m",
         f"Release v{version}"], cwd=workdir)
    head_sha = git(["rev-parse", "HEAD"], cwd=workdir).strip()
    git(["push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{branch}"], cwd=workdir, token=token)

    body = render_pr_body(u=u, notes=notes, base_sha=base_sha, head_sha=head_sha, run_url=run_url)
    if current:
        try:
            gh(["pr", "edit", str(current["number"]), "--repo", full, "--title", release_title(version), "--body",
                body], token)
        except TrainError as e:
            # The marker would no longer match the branch, and later runs would refuse the PR: put the old commit back.
            code, _, err = git(["push", "--quiet", "--force", "origin", f"{current['headRefOid']}:refs/heads/{branch}"],
                               cwd=workdir, token=token, check=False)
            if code != 0:
                raise TrainError(f"{e} The PR's branch now has a commit its description doesn't record, so later "
                                 f"runs will refuse it: close {current['url']}. ({err.strip()[-300:]})") from e
            raise
        url, status = current["url"], "updated"
    else:
        try:
            url = gh(["pr", "create", "--repo", full, "--base", default_branch(repo, token), "--head", branch,
                      "--title", release_title(version), "--body", body, "--label", LABEL], token).strip()
        except TrainError as e:
            # Without a PR, the branch would block the next run: remove it.
            code, _, err = git(["push", "--quiet", "origin", "--delete", branch], cwd=workdir, token=token,
                               check=False)
            if code != 0:
                raise TrainError(f"{e} The {branch} branch is still there and blocks the next run: delete it by "
                                 f"hand. ({err.strip()[-300:]})") from e
            raise
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


def check_branch_is_ours(repo, branch, workdir, token, bot, existing):
    """Refuse to force-push over a branch a person uses.

    The branch is ours only when one of the train's open PRs uses it, or when it
    doesn't exist. A commit's author name proves nothing, since anyone can set
    it, so any other existing branch stops the job. A run that pushes but then
    can't open the PR deletes its branch, so it doesn't block the next run.
    """
    if any(pr["headRefName"] == branch for pr in existing):
        return
    out = gh(["pr", "list", "--repo", f"{ORG}/{repo}", "--head", branch, "--state", "open",
              "--json", "url,isCrossRepository,author", "--limit", "20"], token)
    for pr in json.loads(out or "[]"):
        if not pr.get("isCrossRepository", True) and not by_train(pr, bot):
            login = (pr.get("author") or {}).get("login") or "unknown"
            raise TrainError(f"{pr['url']} uses {branch} and {login} opened it, not the release train, so nothing "
                             "was pushed. Merge or close it, and the next run carries on.")
    if git(["ls-remote", "origin", f"refs/heads/{branch}"], cwd=workdir, token=token).strip():
        raise TrainError(f"{repo} already has a {branch} branch that no open release train PR uses, so nothing was "
                         "pushed. Delete the branch if it isn't needed, and the next run carries on.")


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


def blockers(pr, version, bot):
    """Why a release PR can't merge yet; empty when it can."""
    problems = []
    # The marker alone proves nothing: anyone who can edit the PR can write it.
    if not by_train(pr, bot):
        login = (pr.get("author") or {}).get("login") or "unknown"
        problems.append(f"{login} opened it, not the release train")
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


def land_batch(prs_by_repo, token, latest_versions, bot):
    """Merge the batch when every PR in it is approved and green. Returns a report."""
    version, batch, stale = find_batch(prs_by_repo)
    for pr in stale:
        print(f"::warning::{pr['url']} is an older release PR than v{version}; the next planned run closes it.")
    if not batch:
        return {"version": None, "merged": [], "waiting": {}}
    urls = {repo: pr["url"] for repo, pr in batch.items()}
    waiting = {repo: blockers(pr, version, bot) for repo, pr in batch.items()}
    waiting = {repo: why for repo, why in waiting.items() if why}
    # Judge each SDK against its own last release: once a person merges one PR of a
    # major batch and the train tags it, the others are still major for their SDKs.
    if any(read_marker(pr.get("body")).get("major") or is_major_for(version, latest_versions.get(repo))
           for repo, pr in batch.items()):
        print(f"::notice::v{version} is a major release, so the train won't merge it. Merge each PR yourself once "
              "it's approved; the train pushes the tags.")
        return {"version": version, "merged": [], "waiting": waiting, "major": True, "urls": urls}
    if waiting:
        for repo, why in waiting.items():
            print(f"{batch[repo]['url']} can't merge yet: {'; '.join(why)}.")
        print(f"Waiting: the v{version} batch merges once every PR in it is approved and green.")
        return {"version": version, "merged": [], "waiting": waiting, "urls": urls}
    merged, failed = [], []
    for repo, pr in batch.items():
        full = f"{ORG}/{repo}"
        code, _, err = gh(["api", "-X", "PUT", f"repos/{full}/pulls/{pr['number']}/merge",
                           "-f", "merge_method=squash", "-f", f"sha={pr['headRefOid']}",
                           "-f", f"commit_title={release_title(version)} (#{pr['number']})"], token, check=False)
        if code != 0:
            print(f"::error::Could not merge {pr['url']}: {err.strip()[-500:]}")
            failed.append(repo)
            continue
        merged.append(repo)
        print(f"Merged {pr['url']}")
        gh(["api", "-X", "DELETE", f"repos/{full}/git/refs/heads/{pr['headRefName']}"], token, check=False)
    return {"version": version, "merged": merged, "waiting": {}, "urls": urls, "merge_failed": failed}


def tag_merged(repo, token, latest, now, bot, errors=None):
    """Push the tag for each recently merged release PR that has none. Returns the tags pushed."""
    full = f"{ORG}/{repo}"
    out = gh(["pr", "list", "--repo", full, "--label", LABEL, "--state", "merged", "--json",
              "number,url,headRefName,mergeCommit,mergedAt,isCrossRepository,author", "--limit", "20"], token)
    pushed = []
    cutoff = now - dt.timedelta(days=TAG_WINDOW_DAYS)
    for pr in sorted(json.loads(out or "[]"), key=lambda p: p.get("mergedAt") or ""):
        version = pr_version(pr)
        if not version or pr.get("isCrossRepository", True) or not pr.get("mergeCommit") or not by_train(pr, bot):
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
            if errors is not None:
                errors.append(f"couldn't push the v{version} tag for {pr['url']}. See the run log.")
            continue
        print(f"Pushed v{version} to {full} at {sha}, from {pr['url']}")
        pushed.append(f"v{version}")
        latest = version
    return pushed


# ---------------------------------------------------------------------------
# Alerts: one issue in this repository while a release needs a person


ATTENTION_LABEL = "sdk-release-needs-a-person"
ATTENTION_TITLE = "SDK release needs a person"


def attention_items(report):
    """What only a person can fix. Waiting for an approval is normal and isn't listed."""
    version, urls = report.get("version"), report.get("urls", {})
    items = []
    if report.get("major"):
        for repo, url in urls.items():
            items.append(f"{url}: v{version} is a major release. Review it, then merge it yourself; "
                         "the train pushes the tag.")
    for repo, why in report.get("waiting", {}).items():
        if any("checks are failure" in w for w in why) and not report.get("major"):
            items.append(f"{urls[repo]}: its checks are failing, so the v{version} batch can't merge.")
    for repo in report.get("merge_failed", []):
        items.append(f"{urls[repo]}: the train couldn't merge it. See the run log.")
    for repo, errors in report.get("tag_failed", {}).items():
        items += [f"{short(repo)}: {e}" for e in errors]
    return items


def render_attention(items, run_url):
    return "\n".join([
        "The SDK release train has found something only a person can fix. The train rewrites this issue each hour "
        "and closes it once nothing is left.",
        "",
        *[f"- {item}" for item in items],
        "",
        f"Last checked by {run_url}.",
    ]) + "\n"


def post_slack(webhook, text):
    """Post to a Slack incoming webhook. A failure is a warning, never an error."""
    if not webhook:
        return
    try:
        request = urllib.request.Request(webhook, data=json.dumps({"text": text}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20):
            pass
    except Exception as e:  # noqa: BLE001 - Slack must never fail the run
        print(f"::warning::Could not post to Slack: {type(e).__name__}")


def sync_attention_issue(repo_full, token, items, run_url, webhook=""):
    """Open, rewrite or close the issue. Slack hears only when it opens or closes."""
    found = json.loads(gh(["issue", "list", "--repo", repo_full, "--label", ATTENTION_LABEL, "--state", "open",
                           "--json", "number,url,body", "--limit", "5"], token) or "[]")
    issue = found[0] if found else None
    if items:
        body = render_attention(items, run_url)
        if issue:
            # Only the run link changed: leave the issue alone so watchers aren't notified.
            if issue.get("body", "").split("Last checked by")[0] != body.split("Last checked by")[0]:
                gh(["issue", "edit", str(issue["number"]), "--repo", repo_full, "--body", body], token)
            return "open"
        gh(["label", "create", ATTENTION_LABEL, "--repo", repo_full, "--force", "--color", "d93f0b",
            "--description", "The SDK release train needs a person"], token)
        url = gh(["issue", "create", "--repo", repo_full, "--title", ATTENTION_TITLE, "--label", ATTENTION_LABEL,
                  "--body", body], token).strip()
        print(f"::warning::An SDK release needs a person: {url}")
        post_slack(webhook, f"An SDK release needs a person: {url}\n" + "\n".join(f"- {i}" for i in items))
        return "opened"
    if issue:
        gh(["issue", "close", str(issue["number"]), "--repo", repo_full, "--comment",
            f"Nothing needs a person any more, as of {run_url}."], token)
        post_slack(webhook, f"Resolved: {issue['url']}")
        return "closed"
    return "none"


def land(token, now, bot, repos=None):
    repos = repos or list(SDK_REPOS)
    prs_by_repo, latest_versions = {}, {}
    for repo in repos:
        try:
            out = gh(["pr", "list", "--repo", f"{ORG}/{repo}", "--label", LABEL, "--state", "open", "--json",
                      "number,url,title,body,headRefName,headRefOid,isDraft,isCrossRepository,reviewDecision,"
                      "mergeStateStatus,statusCheckRollup,author", "--limit", "20"], token)
            # A person's release PR, such as one cut by hand, never joins a batch.
            prs_by_repo[repo] = [pr for pr in json.loads(out or "[]") if not pr.get("isCrossRepository", True)
                                 and pr_version(pr) and by_train(pr, bot)]
            tags = [line.split("refs/tags/", 1)[1] for line in
                    run(["git", "ls-remote", "--tags", "--refs", f"{GIT_BASE}/{ORG}/{repo}.git"],
                        env=git_auth_env(token)).splitlines() if "refs/tags/" in line]
            latest_versions[repo] = latest_release(tags)
        except TrainError as e:
            print(f"::warning::Could not read {repo}, so its release PRs were left alone: {e}")
    latest_versions = {r: v for r, v in latest_versions.items() if v}
    report = land_batch(prs_by_repo, token, latest_versions, bot)
    tags, tag_failed = {}, {}
    for repo in repos:
        if repo not in latest_versions and repo not in prs_by_repo:
            continue
        try:
            errors = []
            pushed = tag_merged(repo, token, latest_versions.get(repo), now, bot, errors)
            if errors:
                tag_failed[repo] = errors
        except TrainError as e:
            print(f"::warning::Could not check {repo} for untagged releases: {e}")
            continue
        if pushed:
            tags[repo] = pushed
    report["tags"] = tags
    report["tag_failed"] = tag_failed
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
    p = sub.add_parser("clone")
    p.add_argument("--unit", required=True)
    p.add_argument("--workdir", required=True)
    p = sub.add_parser("bump")
    p.add_argument("--unit", required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--run-as", default="", help="unprivileged user to run the bump script as, through sudo")
    p = sub.add_parser("publish")
    p.add_argument("--unit", required=True)
    p.add_argument("--bundle", required=True)
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
            bot = os.environ.get("RELEASE_TRAIN_BOT", "")
            if not bot.removesuffix("[bot]"):
                raise TrainError("RELEASE_TRAIN_BOT has no app slug, so the plan can't tell which open release PRs "
                                 "are the train's. Check the bot-slug step's app-slug output.")
            cadences = parse_cadence(args.cadence or "manual", args.overrides)
            manual = args.event == "workflow_dispatch"
            sdks = {}
            for repo in SDK_REPOS:
                try:
                    sdks[repo] = fetch_sdk(repo, token, bot)
                except TrainError as e:
                    print(f"::warning::Could not read {repo}, so it isn't released in this run: {e}")
            result = plan(sdks, cadences=cadences, today=now.date(), daily_run=args.daily_run == "true",
                          manual=manual, repos=args.repos)
            write_summary(render_plan_summary(result, cadences))
            write_output("has_work", "true" if result["units"] else "false")
            write_output("matrix", json.dumps({"include": result["units"]}, separators=(",", ":")))
        elif args.command == "clone":
            u = json.loads(args.unit)
            clone_sdk(u["repo"], args.workdir, os.environ.get("READ_TOKEN", ""))
        elif args.command == "bump":
            runas = ()
            if args.run_as:
                home = f"/home/{args.run_as}"
                runas = ("sudo", "-n", "-u", args.run_as, "--", "env", "-i", "-C", args.workdir, f"HOME={home}",
                         f"TMPDIR={home}/tmp", "PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8")
            try:
                make_patch(u=json.loads(args.unit), workdir=args.workdir, out=args.out,
                           today=now.strftime("%Y-%m-%d"), runas=runas)
            finally:
                if args.run_as:
                    # Nothing the script started may outlive this step.
                    run(["sudo", "-n", "-u", args.run_as, "--", "kill", "-KILL", "-1"], check=False)
        elif args.command == "publish":
            u = json.loads(args.unit)
            reviewers = [r.strip() for r in args.reviewers.split(",") if r.strip()]
            publish(u=u, bundle=args.bundle, workdir=args.workdir, token=os.environ["WRITE_TOKEN"],
                    bot_name=args.bot_name, bot_email=args.bot_email, run_url=args.run_url, reviewers=reviewers)
        elif args.command == "land":
            bot = os.environ.get("RELEASE_TRAIN_BOT", "")
            if not bot.removesuffix("[bot]"):
                raise TrainError("RELEASE_TRAIN_BOT has no app slug, so no release PR would count as the train's "
                                 "own. Check the write-token step's app-slug output.")
            report = land(os.environ["WRITE_TOKEN"], now, bot)
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
            items = attention_items(report)
            if items:
                lines += ["", "Needs a person:", *[f"- {i}" for i in items]]
            write_summary("\n".join(lines) + "\n")
            sync_attention_issue(os.environ["GITHUB_REPOSITORY"], os.environ["ISSUE_TOKEN"], items,
                                 os.environ.get("RUN_URL", ""), os.environ.get("SLACK_WEBHOOK_URL", ""))
    except TrainError as e:
        print(f"::error::{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
