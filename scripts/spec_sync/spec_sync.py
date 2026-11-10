#!/usr/bin/env python3
"""Logic behind the sdk-spec-sync workflows. Standard library only.

Subcommands:
  record          store a sdk-spec-changed dispatch as state/pending/<tag>.json
  plan            work out which SDK jobs to run from the pending records
  prepare         set up an SDK checkout and the context files for Claude
  publish         check Claude's changes, push them and create or update the PR
  mark-processed  move records from state/pending to state/processed
  status          mark green spec-sync PRs ready and rewrite the status issue

Each subcommand is a thin wrapper around functions that tests call directly.
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
from pathlib import Path

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
FIXTURES_REPO = "smileid-sdk-fixtures"
ALL_REPOS = SDK_REPOS + [FIXTURES_REPO]
ROLLING_BRANCH = "spec-sync/rolling"
LABEL = "spec-sync"
BREAKING_LABEL = "breaking"
STATUS_TITLE = "SDK spec sync status"
STATE_BRANCH = "spec-sync-state"
# Tests point this at local bare repositories.
GIT_BASE = os.environ.get("SPEC_SYNC_GIT_BASE", "https://github.com")

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# The contract truncates summaries at 50,000 characters; leave room for its note.
MAX_SUMMARY = 52_000
# GitHub refuses PR and issue bodies over 65,536 characters.
MAX_BODY = 60_000
MAX_CLAUDE_SUMMARY = 15_000
PR_MARKER = "spec-sync-state"
ISSUE_MARKER = "spec-sync-status"
COVERAGE_EXCLUSIONS = re.compile(
    r"nocov|pragma:\s*no\s*cover|c8\s+ignore|istanbul\s+ignore|@codeCoverageIgnore"
    r"|coverage\s*\(\s*off\s*\)|@Generated\b|lcov_excl|LCOV_EXCL",
    re.IGNORECASE,
)
CONFLICT_MARKER = re.compile(r"^(<{7}|>{7}|={7})( |$)")


class SpecSyncError(Exception):
    """A problem the workflow should report as an error annotation."""


# ---------------------------------------------------------------------------
# Running commands


def run(cmd, *, cwd=None, env=None, check=True, input_text=None):
    """Run a command and return its stdout. Tests replace this function."""
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise SpecSyncError(
            f"{' '.join(cmd[:3])} failed ({result.returncode}): {result.stderr.strip()[-2000:]}"
        )
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


def git(args, cwd, token=None, **kwargs):
    return run(["git", *args], cwd=cwd, env=git_auth_env(token), **kwargs)


# ---------------------------------------------------------------------------
# Tags and records


def tag_key(tag):
    match = TAG_RE.match(tag or "")
    if not match:
        raise SpecSyncError(f"'{tag}' is not a release tag like v3.1140.0")
    return tuple(int(part) for part in match.groups())


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise SpecSyncError(f"breaking must be true or false, not {value!r}")


def validate_payload(payload):
    """Check a dispatch payload against the contract and return a clean record."""
    if not isinstance(payload, dict):
        raise SpecSyncError("client_payload must be an object")
    tag = payload.get("tag")
    previous = payload.get("previous_tag")
    tag_key(tag)
    tag_key(previous)
    if tag_key(previous) >= tag_key(tag):
        raise SpecSyncError(f"previous_tag {previous} must be older than tag {tag}")
    summary = payload.get("summary", "")
    if not isinstance(summary, str):
        raise SpecSyncError("summary must be a string")
    if len(summary) > MAX_SUMMARY:
        summary = summary[:MAX_SUMMARY] + "\n\n(Truncated by central-workflows.)"
    prs = payload.get("lambda_prs") or []
    if not isinstance(prs, list):
        raise SpecSyncError("lambda_prs must be a list of pull request numbers")
    clean_prs = set()
    for pr in prs:
        text = str(pr)
        if isinstance(pr, bool) or not text.isdigit() or int(text) <= 0:
            raise SpecSyncError(f"lambda_prs contains {pr!r}, which is not a pull request number")
        clean_prs.add(int(text))
    return {
        "tag": tag,
        "previous_tag": previous,
        "summary": summary,
        "breaking": parse_bool(payload.get("breaking", False)),
        "lambda_prs": sorted(clean_prs),
    }


def record_dispatch(event, state_dir, run_url, now):
    """Write the dispatch to state/pending. Returns the path written, or None."""
    record = validate_payload(event.get("client_payload"))
    state = Path(state_dir) / "state"
    if (state / "processed" / f"{record['tag']}.json").exists():
        print(f"::notice::{record['tag']} was already processed; ignoring the repeat dispatch")
        return None
    record["received_at"] = now
    record["run_url"] = run_url
    path = state / "pending" / f"{record['tag']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(f"Recorded {record['tag']} (compared with {record['previous_tag']}, breaking: {record['breaking']})")
    return path


def load_records(state_dir, kind):
    folder = Path(state_dir) / "state" / kind
    records = []
    if folder.is_dir():
        for path in folder.glob("*.json"):
            record = json.loads(path.read_text())
            tag_key(record["tag"])
            records.append(record)
    return sorted(records, key=lambda r: tag_key(r["tag"]))


def find_record(state_dir, tag):
    for kind in ("pending", "processed"):
        path = Path(state_dir) / "state" / kind / f"{tag}.json"
        if path.exists():
            return json.loads(path.read_text())
    return None


def mark_processed(state_dir, tags, now, run_url):
    state = Path(state_dir) / "state"
    moved = []
    for tag in tags:
        tag_key(tag)
        source = state / "pending" / f"{tag}.json"
        if not source.exists():
            continue
        record = json.loads(source.read_text())
        record["processed_at"] = now
        record["processed_by"] = run_url
        target = state / "processed" / f"{tag}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        source.unlink()
        moved.append(tag)
    return moved


def latest_tag(tags):
    valid = [t for t in tags if TAG_RE.match(t)]
    return max(valid, key=tag_key) if valid else None


def slug(text):
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-")
    if not value:
        raise SpecSyncError("scope must contain letters or digits")
    return value


# ---------------------------------------------------------------------------
# Planning


def select_repos(repos):
    if not repos or not repos.strip():
        return list(ALL_REPOS)
    chosen = []
    for name in (part.strip() for part in repos.split(",")):
        if not name:
            continue
        full = name if name.startswith("smileid-sdk-") else f"smileid-sdk-{name}"
        if full not in ALL_REPOS:
            raise SpecSyncError(f"Unknown repository '{name}'. Use one of: {', '.join(ALL_REPOS)}")
        if full not in chosen:
            chosen.append(full)
    return chosen


def unit(kind, branch, base_tag, spec_tag, records, scope=""):
    prs = sorted({pr for r in records for pr in r["lambda_prs"]})
    return {
        "kind": kind,
        "branch": branch,
        "base_tag": base_tag,
        "spec_tag": spec_tag,
        "tags": [r["tag"] for r in records],
        "lambda_prs": prs,
        "scope": scope,
    }


def plan(pending, *, from_tag="", scope="", repos="", latest_api_tag=None, sync_due=True):
    """Return the matrix of jobs to run and the records they consume.

    Non-breaking records share the rolling PR. Each breaking record gets its own
    PR. A manual run with from_tag syncs everything from that tag to the newest
    api-reference tag. A manual run with scope makes one PR limited to that scope
    and leaves the pending records alone.
    """
    result = {"has_work": False, "mark_processed": False, "tags": [], "units": [], "matrix": {"include": []}}
    if not sync_due:
        result["reason"] = "Hourly run: only marking green PRs ready and updating the status issue."
        return result
    targets = select_repos(repos)
    if scope and not from_tag:
        raise SpecSyncError("A scoped run needs from_tag, the spec tag the SDKs already match")
    if from_tag:
        tag_key(from_tag)
        if not latest_api_tag:
            raise SpecSyncError("Could not find the latest api-reference tag")
        if tag_key(latest_api_tag) < tag_key(from_tag):
            raise SpecSyncError(f"from_tag {from_tag} is newer than the latest api-reference tag {latest_api_tag}")
        pending = [r for r in pending if tag_key(r["tag"]) > tag_key(from_tag)]

    units = []
    if scope:
        units.append(unit("scoped", f"spec-sync/{slug(scope)}", from_tag, latest_api_tag, [], scope))
        consumed = []
    else:
        normal = [r for r in pending if not r["breaking"]]
        breaking = [r for r in pending if r["breaking"]]
        if from_tag and (normal or tag_key(latest_api_tag) > tag_key(from_tag)):
            units.append(unit("rolling", ROLLING_BRANCH, from_tag, latest_api_tag, normal))
        elif normal:
            units.append(unit("rolling", ROLLING_BRANCH, normal[0]["previous_tag"], normal[-1]["tag"], normal))
        for record in breaking:
            units.append(
                unit("breaking", f"spec-sync/breaking-{record['tag']}", record["previous_tag"], record["tag"], [record])
            )
        consumed = [r["tag"] for r in pending]

    include = [{"repo": repo, **u} for u in units for repo in targets]
    result.update(
        has_work=bool(include),
        units=units,
        tags=consumed,
        # A run limited to some repositories has not synced the others.
        mark_processed=bool(consumed) and targets == ALL_REPOS,
        matrix={"include": include},
    )
    if not include:
        result["reason"] = "Nothing is pending."
    return result


# ---------------------------------------------------------------------------
# Pull request bodies


def read_marker(body, name):
    """Read the JSON in a <!-- name {...} --> comment at the top of a body."""
    match = re.search(r"<!--\s*" + re.escape(name) + r"\s+(\{.*?\})\s*-->", body or "", re.DOTALL)
    if not match:
        return {}
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def write_marker(name, value):
    # "--" would end the HTML comment early; JSON reads the escaped form back the same.
    text = json.dumps(value, sort_keys=True, separators=(",", ":")).replace("--", "-\\u002d")
    return f"<!-- {name} {text} -->"


def merge_tags(*groups):
    return sorted({t for group in groups for t in group if TAG_RE.match(t)}, key=tag_key)


def pr_title(u, spec_tag):
    if u["kind"] == "breaking":
        return f"Breaking: sync with the API spec at {spec_tag}"
    if u["kind"] == "scoped":
        return f"Sync {u['scope']} with the API spec at {spec_tag}"
    return f"Sync with the API spec at {spec_tag}"


def checks_table(result):
    rows = []
    for check in result.get("checks") or []:
        name = str(check.get("name", "")).replace("|", "/")[:80]
        outcome = "passed" if check.get("passed") else "failed"
        rows.append(f"| {name} | {outcome} |")
    if not rows:
        return "Claude did not report any checks."
    lines = ["| Check | Result |", "|---|---|", *rows]
    coverage = result.get("changed_line_coverage")
    if isinstance(coverage, (int, float)):
        lines += ["", f"Changed-line coverage: {coverage:g}%."]
    return "\n".join(lines)


def render_pr_body(*, repo, u, tags, spec_tag, lambda_prs, records, summary, result, ready_ok, run_url, now):
    state = {"tags": tags, "spec_tag": spec_tag, "lambda_prs": lambda_prs, "ready_ok": ready_ok, "kind": u["kind"]}
    spec_link = f"https://github.com/{ORG}/api-reference/blob/{spec_tag}/specs/v3/openapi.yaml"
    intro = [
        write_marker(PR_MARKER, state),
        (
            "This pull request comes from the spec-sync workflow in `smileidentity/central-workflows`. "
            "Each run rewrites this description; it does not add comments."
        ),
        "",
        f"It updates `{repo}` to match the V3 API spec at [`{spec_tag}`]({spec_link}).",
    ]
    if u["kind"] == "breaking":
        intro.append("It contains breaking changes, so it is kept apart from the rolling spec-sync pull request.")
    if u["kind"] == "scoped":
        intro.append(f"It covers only this part of the spec: {u['scope']}.")
    if tags:
        intro.append(f"Lambda releases covered: {', '.join(f'`{t}`' for t in tags)}.")
    if lambda_prs:
        intro.append(f"Lambda pull requests: {', '.join(f'lambda#{n}' for n in lambda_prs)}.")
    status_line = (
        "All of Claude's checks passed. The workflow marks this pull request ready for review once every CI check is green."
        if ready_ok
        else "This pull request stays a draft until the gaps below are fixed."
    )
    parts = ["\n".join(intro), status_line]
    summary = (summary or "").strip()
    if len(summary) > MAX_CLAUDE_SUMMARY:
        summary = summary[:MAX_CLAUDE_SUMMARY] + "\n\n(Summary truncated.)"
    parts.append("## What changed\n\n" + (summary or "Claude did not write a summary."))
    parts.append("## Checks run by Claude\n\n" + checks_table(result))
    gaps = [str(g) for g in result.get("gaps") or []]
    if gaps:
        parts.append("## Gaps\n\n" + "\n".join(f"- {g}" for g in gaps))
    footer = f"Last updated by [this run]({run_url}) on {now}."

    budget = MAX_BODY - sum(len(p) + 2 for p in parts) - len(footer) - 200
    details = []
    for record in records:
        block = (
            f"<details><summary>{record['tag']} (compared with {record['previous_tag']})</summary>\n\n"
            f"{record['summary'].strip() or 'No summary.'}\n\n</details>"
        )
        if len(block) > budget:
            details.append(
                f"Further changes are in the [`{STATE_BRANCH}` branch]"
                f"(https://github.com/{ORG}/central-workflows/tree/{STATE_BRANCH}/state)."
            )
            break
        details.append(block)
        budget -= len(block) + 1
    if details:
        parts.append("## Spec changes\n\n" + "\n".join(details))
    parts.append(footer)
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Guards


def added_lines(diff):
    """Yield (file, line) for each line a unified diff adds."""
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("+") and not line.startswith("+++") and current:
            yield current, line[1:]


def guard_problems(changed_files, diff):
    problems = []
    for name in changed_files:
        if name.startswith(".github/"):
            problems.append(f"{name}: spec-sync may not change files under .github/")
    for name, line in added_lines(diff):
        if COVERAGE_EXCLUSIONS.search(line):
            problems.append(f"{name}: adds a coverage exclusion: {line.strip()[:120]}")
        if CONFLICT_MARKER.match(line):
            problems.append(f"{name}: has an unresolved merge conflict marker")
    return problems


# ---------------------------------------------------------------------------
# prepare and publish


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def find_open_pr(repo, branch, token):
    out = gh(
        ["pr", "list", "--repo", f"{ORG}/{repo}", "--head", branch, "--state", "open",
         "--json", "number,url,isDraft,body", "--limit", "1"],
        token,
    )
    prs = json.loads(out or "[]")
    return prs[0] if prs else None


def lambda_pr_text(numbers, token):
    """Fetch lambda PR descriptions. They stay on the runner and are never published."""
    if not numbers:
        return "No lambda pull requests were listed for these releases.\n"
    sections = []
    for number in numbers:
        if not token:
            sections.append(f"## lambda#{number}\n\nNot available: no token could read lambda.\n")
            continue
        code, out, err = gh(
            ["pr", "view", str(number), "--repo", f"{ORG}/lambda", "--json", "number,title,body"],
            token, check=False,
        )
        if code != 0:
            sections.append(f"## lambda#{number}\n\nNot available: {err.strip()[:200]}\n")
            continue
        pr = json.loads(out)
        sections.append(f"## lambda#{number}: {pr.get('title', '')}\n\n{pr.get('body') or 'No description.'}\n")
    return "\n".join(sections)


def render_task(template, values):
    def substitute(match):
        key = match.group(1)
        if key not in values:
            raise SpecSyncError(f"The prompt template uses {{{{{key}}}}}, which has no value")
        return str(values[key])

    return re.sub(r"\{\{(\w+)\}\}", substitute, template)


def prepare(*, u, repo, workdir, ctx, state_dir, template, write_token, read_token):
    ctx = Path(ctx)
    (ctx / "out").mkdir(parents=True, exist_ok=True)
    default = gh(["repo", "view", f"{ORG}/{repo}", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
                 write_token).strip()
    branch = u["branch"]
    pr = find_open_pr(repo, branch, write_token)
    remote = f"{GIT_BASE}/{ORG}/{repo}.git"
    git(["fetch", "--quiet", remote, f"+refs/heads/{default}:refs/remotes/origin/{default}"], workdir, write_token)
    conflict = False
    if pr:
        git(["fetch", "--quiet", remote, f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], workdir, write_token)
        git(["checkout", "--quiet", "-B", branch, f"origin/{branch}"], workdir)
        code, _, _ = git(
            ["-c", "user.name=spec-sync", "-c", "user.email=spec-sync@invalid",
             "merge", "--no-edit", "--quiet", f"origin/{default}"],
            workdir, check=False,
        )
        conflict = code != 0
    else:
        git(["checkout", "--quiet", "-B", branch, f"origin/{default}"], workdir)
    head = git(["rev-parse", "HEAD"], workdir).strip()

    previous = read_marker(pr["body"], PR_MARKER) if pr else {}
    records = [r for r in (find_record(state_dir, t) for t in u["tags"]) if r]
    if records:
        summaries = "\n".join(
            f"## {r['tag']} (compared with {r['previous_tag']}, breaking: {str(r['breaking']).lower()})\n\n"
            f"{r['summary'].strip() or 'No summary.'}\n"
            for r in records
        )
    else:
        summaries = (
            "There is no change summary for this run. Compare the two spec files to find the changes.\n"
        )
    (ctx / "summaries.md").write_text(summaries)
    (ctx / "lambda-prs.md").write_text(lambda_pr_text(u["lambda_prs"], read_token))

    kind_notes = {
        "rolling": "This is the rolling spec-sync pull request. Do not make breaking changes to the public API. "
                   "If a change cannot be made without one, leave it out and list it under gaps.",
        "breaking": "This pull request is for breaking changes. Make the breaking change cleanly and describe "
                    "the migration for SDK users in your summary.",
        "scoped": f"Limit the change to this part of the spec: {u['scope']}. Leave other differences alone. "
                  "Do not make breaking changes to the public API.",
    }
    values = {
        "repo": repo,
        "ctx": str(ctx),
        "default_branch": default,
        "branch": branch,
        "base_tag": u["base_tag"],
        "spec_tag": u["spec_tag"],
        "tags": ", ".join(u["tags"]) or "none",
        "kind_note": kind_notes[u["kind"]],
        "existing_pr": f"An open pull request already covers {', '.join(previous.get('tags', [])) or 'earlier changes'}; "
                       "keep its work and add to it." if pr else "There is no open pull request yet.",
        "conflict_note": (
            f"Merging `{default}` into this branch left conflicts. Resolve every conflict marker first and "
            "`git add` the resolved files, but do not commit." if conflict else ""
        ),
        "repo_note": FIXTURES_NOTE if repo == FIXTURES_REPO else SDK_NOTE.format(ctx=ctx),
    }
    (ctx / "TASK.md").write_text(render_task(template, values))
    state = {"repo": repo, "default": default, "head": head, "pr": pr, "conflict": conflict,
             "previous": previous, "unit": u}
    (ctx / "state.json").write_text(json.dumps(state, indent=2))
    return state


SDK_NOTE = (
    "Persona cases live in the private `smileid-sdk-fixtures` repository. A read-only copy is at "
    "`{ctx}/fixtures` if this run could read it. Another job in this run updates that repository, so do not "
    "edit it. If the integration suite here needs a change to run new cases (for example, mapping a new "
    "operationId to an SDK method), make it, and list the persona cases the change needs in your summary."
)
FIXTURES_NOTE = (
    "This repository holds the shared persona cases. Follow its README: change `operations.json` (and "
    "`personas.json` only if lambda's test identities changed), rebuild with `python scripts/build_cases.py`, "
    "and run `python scripts/validate.py`. You cannot run the cases against dev here, so say so in your summary. "
    "There are no golden fixtures or coverage gates in this repository."
)


def publish(*, workdir, ctx, state_dir, write_token, bot_name, bot_email, run_url, now):
    ctx = Path(ctx)
    state = read_json(ctx / "state.json")
    if not state:
        raise SpecSyncError("prepare did not finish, so there is nothing to publish")
    u, repo, default, pr = state["unit"], state["repo"], state["default"], state["pr"]
    outcome = {"repo": repo, "branch": u["branch"], "kind": u["kind"], "spec_tag": u["spec_tag"],
               "status": "failed", "pr_url": pr["url"] if pr else ""}

    def finish(status, error=None):
        outcome["status"] = status
        (ctx / "out" / "job-result.json").write_text(json.dumps(outcome))
        if error:
            raise SpecSyncError(error)
        return outcome

    result = read_json(ctx / "out" / "result.json")
    if not isinstance(result, dict) or result.get("status") not in ("changed", "no_change", "blocked"):
        return finish("failed", "Claude did not write a valid result.json, so nothing was pushed")
    summary_path = ctx / "out" / "summary.md"
    summary = summary_path.read_text() if summary_path.exists() else ""

    # claude-code-action points origin at central-workflows; use explicit URLs from here on.
    remote = f"{GIT_BASE}/{ORG}/{repo}.git"
    git(["add", "-A"], workdir)
    changed_files = [f for f in git(["diff", "--cached", "--name-only", f"origin/{default}"], workdir).splitlines() if f]
    problems = guard_problems(changed_files, git(["diff", "--cached", f"origin/{default}"], workdir))
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return finish("failed", "The changes broke a spec-sync rule, so nothing was pushed")

    if not changed_files and not pr:
        print(f"No changes are needed in {repo}.")
        return finish("no_change")

    code, _, _ = git(["diff", "--cached", "--quiet"], workdir, check=False)
    merging = (Path(workdir) / ".git" / "MERGE_HEAD").exists()
    if code != 0 or merging:
        git(["-c", f"user.name={bot_name}", "-c", f"user.email={bot_email}", "commit", "--quiet",
             "--no-verify", "-m", f"Sync with the API spec at {u['spec_tag']}"], workdir)
    head = git(["rev-parse", "HEAD"], workdir).strip()
    pushed = head != state["head"] or not pr
    if pushed:
        push = ["push", "--quiet", remote, f"HEAD:refs/heads/{u['branch']}"]
        if not pr:
            # No open PR, so any branch left over from a merged or closed one is replaced.
            push.insert(1, "--force")
        git(push, workdir, write_token)

    previous = state.get("previous") or {}
    tags = merge_tags(previous.get("tags", []), u["tags"])
    spec_tag = max([u["spec_tag"], *([previous["spec_tag"]] if previous.get("spec_tag") else [])], key=tag_key)
    lambda_prs = sorted(set(previous.get("lambda_prs", [])) | set(u["lambda_prs"]))
    records = [r for r in (find_record(state_dir, t) for t in tags) if r]
    ready_ok = result["status"] != "blocked" and not result.get("gaps")
    if result["status"] == "no_change" and not pr:
        ready_ok = False
    body = render_pr_body(repo=repo, u=u, tags=tags, spec_tag=spec_tag, lambda_prs=lambda_prs, records=records,
                          summary=summary, result=result, ready_ok=ready_ok, run_url=run_url, now=now)
    body_file = ctx / "out" / "pr-body.md"
    body_file.write_text(body)
    title = pr_title(u, spec_tag)
    full = f"{ORG}/{repo}"
    if pr:
        gh(["pr", "edit", str(pr["number"]), "--repo", full, "--title", title, "--body-file", str(body_file)],
           write_token)
        if pushed and not pr["isDraft"]:
            # New commits need green checks again before review.
            gh(["pr", "ready", str(pr["number"]), "--repo", full, "--undo"], write_token)
        url = pr["url"]
    else:
        args = ["pr", "create", "--repo", full, "--draft", "--base", default, "--head", u["branch"],
                "--title", title, "--body-file", str(body_file), "--label", LABEL]
        if u["kind"] == "breaking":
            args += ["--label", BREAKING_LABEL]
        url = gh(args, write_token).strip().splitlines()[-1]
    outcome["pr_url"] = url
    print(f"{'Updated' if pr else 'Opened'} {url}")
    return finish("blocked" if result["status"] == "blocked" else "updated")


# ---------------------------------------------------------------------------
# Status: mark PRs ready and rewrite the pinned issue


def checks_state(rollup):
    """Summarise a statusCheckRollup as success, pending, failure or none."""
    if not rollup:
        return "none"
    state = "success"
    for item in rollup:
        if item.get("__typename") == "StatusContext" or "state" in item and "status" not in item:
            value = (item.get("state") or "").upper()
            if value in ("PENDING", "EXPECTED"):
                state = "pending"
            elif value != "SUCCESS":
                return "failure"
        else:
            if (item.get("status") or "").upper() != "COMPLETED":
                state = "pending"
            elif (item.get("conclusion") or "").upper() not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
                return "failure"
    return state


def promote_prs(repos, token):
    """Mark green draft spec-sync PRs ready. Returns the open PRs per repo."""
    found = {}
    for repo in repos:
        out = gh(
            ["pr", "list", "--repo", f"{ORG}/{repo}", "--label", LABEL, "--state", "open",
             "--json", "number,url,isDraft,body,headRefName,statusCheckRollup", "--limit", "50"],
            token,
        )
        prs = []
        for pr in json.loads(out or "[]"):
            marker = read_marker(pr.get("body"), PR_MARKER)
            checks = checks_state(pr.get("statusCheckRollup"))
            draft = pr.get("isDraft", False)
            if draft and checks == "success" and marker.get("ready_ok"):
                gh(["pr", "ready", str(pr["number"]), "--repo", f"{ORG}/{repo}"], token)
                print(f"Marked {pr['url']} ready for review")
                draft = False
            prs.append({"number": pr["number"], "url": pr["url"], "draft": draft, "checks": checks,
                        "branch": pr.get("headRefName", ""), "spec_tag": marker.get("spec_tag", "")})
        found[repo] = sorted(prs, key=lambda p: p["number"])
    return found


def update_last_runs(previous, job_results, planned_repos, run_url, now, sync_result):
    """Combine the last-run record from the issue with this run's job results."""
    runs = dict(previous)
    by_repo = {}
    for result in job_results:
        by_repo.setdefault(result["repo"], []).append(result["status"])
    for repo in planned_repos:
        statuses = by_repo.get(repo)
        if not statuses:
            status = "cancelled" if sync_result == "cancelled" else "failed"
        elif "failed" in statuses:
            status = "failed"
        elif "blocked" in statuses:
            status = "blocked"
        elif "updated" in statuses:
            status = "updated"
        else:
            status = "no change"
        runs[repo] = {"result": status, "run_url": run_url, "at": now}
    return runs


def render_status_issue(*, prs, runs, pending, last_processed, run_url, now):
    state = {"runs": runs}
    lines = [
        write_marker(ISSUE_MARKER, state),
        "The `sdk-spec-sync` workflow keeps this issue up to date. It edits this description in place and does not comment.",
        "",
        (
            f"Pending lambda releases: {', '.join(f'`{t}`' for t in pending) or 'none'}. "
            f"Last processed: {f'`{last_processed}`' if last_processed else 'none'}."
        ),
        "",
        "| Repository | Open spec-sync PRs | Spec tag | Last run |",
        "|---|---|---|---|",
    ]
    for repo in ALL_REPOS:
        repo_prs = prs.get(repo, [])
        cells = []
        for pr in repo_prs:
            state_text = "draft" if pr["draft"] else "ready"
            checks = {"success": "checks green", "pending": "checks running", "failure": "checks failing",
                      "none": "no checks yet"}[pr["checks"]]
            cells.append(f"[#{pr['number']}]({pr['url']}) {state_text}, {checks}")
        rolling = next((p for p in repo_prs if p["branch"] == ROLLING_BRANCH), None)
        spec_tag = (rolling or {}).get("spec_tag") or last_processed or ""
        run = runs.get(repo)
        run_text = f"{run['result']}, [{run['at']}]({run['run_url']})" if run else "not run yet"
        lines.append(
            f"| `{repo}` | {'<br>'.join(cells) or 'none'} | {f'`{spec_tag}`' if spec_tag else 'unknown'} | {run_text} |"
        )
    lines += ["", f"Updated by [this run]({run_url}) on {now}."]
    return "\n".join(lines) + "\n"


def find_status_issue(repo_full, token):
    out = gh(["issue", "list", "--repo", repo_full, "--state", "open", "--search", f'in:title "{STATUS_TITLE}"',
              "--json", "number,title,body", "--limit", "20"], token)
    for issue in json.loads(out or "[]"):
        if issue["title"] == STATUS_TITLE and f"<!-- {ISSUE_MARKER}" in (issue.get("body") or ""):
            return issue
    return None


def write_status_issue(repo_full, token, body, issue, body_file):
    Path(body_file).write_text(body)
    if issue:
        gh(["issue", "edit", str(issue["number"]), "--repo", repo_full, "--body-file", str(body_file)], token)
        return issue["number"]
    url = gh(["issue", "create", "--repo", repo_full, "--title", STATUS_TITLE, "--body-file", str(body_file)],
             token).strip().splitlines()[-1]
    number = url.rstrip("/").rsplit("/", 1)[-1]
    code, _, err = gh(["issue", "pin", number, "--repo", repo_full], token, check=False)
    if code != 0:
        print(f"::warning::Could not pin the status issue: {err.strip()[:200]}")
    return int(number)


# ---------------------------------------------------------------------------
# Command line


def now_utc():
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")


def write_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    if path:
        with open(path, "a") as handle:
            handle.write(f"{name}<<SPEC_SYNC_EOF\n{text}\nSPEC_SYNC_EOF\n")
    else:
        print(f"{name}={text}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("record")
    p.add_argument("--event", required=True)
    p.add_argument("--state-dir", default=".")
    p.add_argument("--run-url", required=True)

    p = sub.add_parser("plan")
    p.add_argument("--state-dir", required=True)
    p.add_argument("--from-tag", default="")
    p.add_argument("--scope", default="")
    p.add_argument("--repos", default="")
    p.add_argument("--api-tags-file", default="")
    p.add_argument("--sync-due", choices=["true", "false"], default="true")

    p = sub.add_parser("prepare")
    p.add_argument("--unit", required=True, help="the matrix entry as JSON")
    p.add_argument("--workdir", required=True)
    p.add_argument("--ctx", required=True)
    p.add_argument("--state-dir", required=True)
    p.add_argument("--template", required=True)

    p = sub.add_parser("publish")
    p.add_argument("--workdir", required=True)
    p.add_argument("--ctx", required=True)
    p.add_argument("--state-dir", required=True)
    p.add_argument("--bot-name", required=True)
    p.add_argument("--bot-email", required=True)
    p.add_argument("--run-url", required=True)

    p = sub.add_parser("mark-processed")
    p.add_argument("--state-dir", default=".")
    p.add_argument("--tags", required=True, help="JSON list of tags")
    p.add_argument("--run-url", required=True)

    p = sub.add_parser("status")
    p.add_argument("--state-dir", required=True)
    p.add_argument("--results-dir", required=True)
    p.add_argument("--planned-repos", default="[]", help="JSON list of repos this run synced")
    p.add_argument("--sync-result", default="skipped")
    p.add_argument("--run-url", required=True)
    p.add_argument("--body-file", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            event = json.loads(Path(args.event).read_text())
            record_dispatch(event, args.state_dir, args.run_url, now_utc())
        elif args.command == "plan":
            api_tags = Path(args.api_tags_file).read_text().split() if args.api_tags_file else []
            result = plan(load_records(args.state_dir, "pending"), from_tag=args.from_tag.strip(),
                          scope=args.scope.strip(), repos=args.repos, latest_api_tag=latest_tag(api_tags),
                          sync_due=args.sync_due == "true")
            for key in ("has_work", "mark_processed"):
                write_output(key, str(result[key]).lower())
            write_output("tags", result["tags"])
            write_output("matrix", result["matrix"])
            write_output("repos", sorted({i["repo"] for i in result["matrix"]["include"]}))
            print(result.get("reason") or f"Planned {len(result['matrix']['include'])} jobs for "
                  f"{len(result['units'])} pull request(s) per repository: "
                  + "; ".join(f"{u['branch']} {u['base_tag']}..{u['spec_tag']}" for u in result["units"]))
        elif args.command == "prepare":
            u = json.loads(args.unit)
            prepare(u=u, repo=u["repo"], workdir=args.workdir, ctx=args.ctx, state_dir=args.state_dir,
                    template=Path(args.template).read_text(), write_token=os.environ["WRITE_TOKEN"],
                    read_token=os.environ.get("READ_TOKEN", ""))
        elif args.command == "publish":
            publish(workdir=args.workdir, ctx=args.ctx, state_dir=args.state_dir,
                    write_token=os.environ["WRITE_TOKEN"], bot_name=args.bot_name, bot_email=args.bot_email,
                    run_url=args.run_url, now=now_utc())
        elif args.command == "mark-processed":
            moved = mark_processed(args.state_dir, json.loads(args.tags), now_utc(), args.run_url)
            print(f"Marked as processed: {', '.join(moved) or 'nothing'}")
        elif args.command == "status":
            token = os.environ["APP_TOKEN"]
            issue_token = os.environ["ISSUE_TOKEN"]
            repo_full = os.environ.get("GITHUB_REPOSITORY", f"{ORG}/central-workflows")
            prs = promote_prs(ALL_REPOS, token)
            results = [read_json(p, {}) for p in sorted(Path(args.results_dir).glob("**/job-result.json"))]
            issue = find_status_issue(repo_full, issue_token)
            previous = read_marker(issue["body"], ISSUE_MARKER).get("runs", {}) if issue else {}
            now = now_utc()
            runs = update_last_runs(previous, [r for r in results if r.get("repo")],
                                    json.loads(args.planned_repos), args.run_url, now, args.sync_result)
            processed = load_records(args.state_dir, "processed")
            body = render_status_issue(prs=prs, runs=runs,
                                       pending=[r["tag"] for r in load_records(args.state_dir, "pending")],
                                       last_processed=processed[-1]["tag"] if processed else "",
                                       run_url=args.run_url, now=now)
            number = write_status_issue(repo_full, issue_token, body, issue, args.body_file)
            print(f"Updated status issue #{number}")
    except SpecSyncError as error:
        print(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
