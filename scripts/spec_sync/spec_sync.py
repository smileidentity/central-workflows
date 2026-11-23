#!/usr/bin/env python3
"""Logic behind the sdk-spec-sync workflows. Standard library only.

Subcommands:
  record          store a sdk-spec-changed dispatch as state/pending/<tag>.json
  plan            work out which SDK jobs to run from the records still pending for each repository
  prepare         set up an SDK checkout and the context files for Claude
  package         bundle Claude's change for the publish job
  publish         check the sync job's patch, push it and create or update the PR
  mark-processed  record which repositories have synced each release; move a record
                to state/processed once every repository has
  status          mark green spec-sync PRs ready and rewrite the status issue

spec_coverage.py, beside this file, checks an SDK's spec-coverage.json against
the whole spec. prepare lists what it finds for Claude, and publish checks it again.

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

import spec_coverage

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
KINDS = ("rolling", "breaking", "scoped")
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


# Never run hooks or fsmonitor from a repository: the sync job's working tree
# was writable by Claude.
GIT_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


def git(args, cwd, token=None, **kwargs):
    return run(["git", *GIT_SAFE, *args], cwd=cwd, env=git_auth_env(token), **kwargs)


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
    # A repeat dispatch for the same tag keeps the repositories that already synced
    # it, unless what they synced has changed.
    previous = read_json(path, {})
    if isinstance(previous, dict) and isinstance(previous.get("processed_repos"), dict) and previous["processed_repos"]:
        if all(previous.get(k) == record[k] for k in ("previous_tag", "breaking", "summary", "lambda_prs")):
            record["processed_repos"] = previous["processed_repos"]
        else:
            print(f"::notice::{record['tag']} changed since it was first recorded, so every repository syncs it again")
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


def processed_repos(record):
    """The repositories that have already synced a pending record.

    Records written before this was tracked have no processed_repos, so every
    repository still needs them.
    """
    done = record.get("processed_repos")
    return set(done) if isinstance(done, dict) else set()


def mark_processed(state_dir, done, now, run_url):
    """Record that repositories have synced releases.

    done maps each repository to the tags it synced. A list of tags means every
    repository. A record moves to state/processed once every repository has
    synced it. Returns the tags moved.
    """
    if isinstance(done, list):
        done = {repo: done for repo in ALL_REPOS}
    by_tag = {}
    for repo, tags in done.items():
        if repo not in ALL_REPOS:
            raise SpecSyncError(f"Unknown repository '{repo}'")
        for tag in tags:
            tag_key(tag)
            by_tag.setdefault(tag, set()).add(repo)
    state = Path(state_dir) / "state"
    moved = []
    for tag in sorted(by_tag, key=tag_key):
        source = state / "pending" / f"{tag}.json"
        if not source.exists():
            continue
        record = json.loads(source.read_text())
        repos = record.get("processed_repos") if isinstance(record.get("processed_repos"), dict) else {}
        for repo in sorted(by_tag[tag] - set(repos)):
            repos[repo] = {"at": now, "by": run_url}
        record["processed_repos"] = dict(sorted(repos.items()))
        if set(repos) >= set(ALL_REPOS):
            record["processed_at"] = now
            record["processed_by"] = run_url
            target = state / "processed" / f"{tag}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
            source.unlink()
            moved.append(tag)
        else:
            source.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return moved


# A job counts as done when it published a change, found none needed, closed a
# PR that no longer changed anything, or opened a draft PR listing what Claude
# could not finish for a person to pick up.
DONE_STATUSES = ("updated", "no_change", "closed", "blocked")


def synced_by_run(matrix, settled, job_results):
    """Work out which tags each repository synced in this run.

    The tags come from the plan's matrix, not from the job results, so a result
    can only confirm a job the plan asked for. A job counts only if every result
    for it is done.
    """
    statuses = {}
    for result in job_results:
        if isinstance(result, dict):
            statuses.setdefault((result.get("repo"), result.get("branch")), []).append(result.get("status"))
    done = {repo: list(tags) for repo, tags in (settled or {}).items()}
    for entry in matrix.get("include", []):
        found = statuses.get((entry["repo"], entry["branch"]))
        if found and all(status in DONE_STATUSES for status in found):
            done.setdefault(entry["repo"], []).extend(entry["tags"])
    return {repo: merge_tags(tags) for repo, tags in done.items() if tags}


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


def api_tag_for(tag, api_tags):
    """The newest api-reference tag at or before a lambda tag.

    api-reference only gets a tag when a lambda release changes the public
    spec, so most lambda tags have no tag of their own there.
    """
    key = tag_key(tag)
    candidates = [t for t in api_tags if TAG_RE.match(t) and tag_key(t) <= key]
    if not candidates:
        raise SpecSyncError(f"api-reference has no tag at or before {tag}")
    return max(candidates, key=tag_key)


def plan_units(pending, *, from_tag, latest_api_tag):
    """The pull requests to make from the pending records, and the tags they need no job for."""
    if from_tag:
        # The SDKs already match from_tag, so older pending records are done too.
        already = [r["tag"] for r in pending if tag_key(r["tag"]) <= tag_key(from_tag)]
        pending = [r for r in pending if tag_key(r["tag"]) > tag_key(from_tag)]
    else:
        already = []
    units = []
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
    return units, already


def map_units(units, api_tags):
    """Map lambda tags to api-reference tags. Returns the units to run and the tags of those dropped.

    A unit whose old and new tags map to the same api-reference tag changes
    nothing in the public spec, so it needs no job and its records are done.
    """
    if api_tags is None:
        return units, []
    kept, dropped = [], []
    for u in units:
        u["base_tag"] = api_tag_for(u["base_tag"], api_tags)
        u["spec_tag"] = api_tag_for(u["spec_tag"], api_tags)
        if u["base_tag"] != u["spec_tag"] or u["kind"] == "scoped":
            kept.append(u)
        else:
            dropped += u["tags"]
    return kept, dropped


REFRESH_BRANCH = re.compile(r"^spec-sync/[A-Za-z0-9._-]{1,100}$")
MAX_SCOPE = 200


def refresh_problem(pr):
    """Why the workflow can't refresh an open PR (see open_pr_info), or None if it can.

    A refresh takes its branch and scope from the PR, not from the workflow or a
    maintainer, and the scope goes into Claude's prompt and the PR title. So only
    plain branch names and short, single-line scopes are accepted.
    """
    if not REFRESH_BRANCH.match(pr.get("branch", "")):
        return "its branch name is not a plain spec-sync/<name>"
    if pr.get("kind") not in KINDS:
        return "its description does not say what kind of spec-sync PR it is"
    if pr.get("kind") == "scoped":
        scope = pr.get("scope", "")
        if not scope:
            return "it is a scoped PR with no recorded scope"
        if len(scope) > MAX_SCOPE or not re.fullmatch(r"[^\r\n`<>$]+", scope):
            return "its scope is longer than 200 characters or has line breaks or characters such as ` < > $"
    return None


def refresh_units(open_prs, latest_api_tag, planned_branches):
    """Jobs that bring open spec-sync PRs up to the newest api-reference tag.

    A spec change that lands while a PR is open must reach that PR, or the PR
    merges code written against the old spec. The pending records can't show
    this: a scoped PR's code isn't on the default branch, so the rolling job
    finds nothing to change. Any open PR whose recorded spec tag is older than
    the newest one gets a refresh job, unless this run already has a job for its
    branch.
    """
    units = []
    if not latest_api_tag:
        return units
    for pr in open_prs or []:
        branch, spec_tag, kind = pr.get("branch", ""), pr.get("spec_tag", ""), pr.get("kind", "")
        if branch in planned_branches or not is_behind(spec_tag, latest_api_tag):
            continue
        problem = refresh_problem(pr)
        if problem:
            print(f"::warning::{pr.get('url') or branch} is behind the spec but was not refreshed: {problem}. "
                  "Refresh it by hand.")
            continue
        u = unit(kind, branch, spec_tag, latest_api_tag, [], pr.get("scope", "") if kind == "scoped" else "")
        u["refresh"] = True
        units.append(u)
    return units


def plan(pending, *, from_tag="", scope="", repos="", latest_api_tag=None, sync_due=True, api_tags=None,
         open_prs=None):
    """Return the matrix of jobs to run and the records they consume.

    Each repository gets jobs only for the records it has not yet synced.
    Non-breaking records share the rolling PR. Each breaking record gets its own
    PR. A manual run with from_tag syncs everything from that tag to the newest
    api-reference tag. A manual run with scope makes one PR limited to that scope
    and leaves the pending records alone.

    open_prs maps each repository to its open spec-sync PRs. Any of them that is
    behind the newest api-reference tag gets a refresh job (see refresh_units).

    settled maps each repository to the tags it needs no job for: records at or
    before from_tag, and records that change nothing in the public spec. The
    finish job marks those, and the tags of each job that succeeds, as processed
    for that repository.
    """
    result = {"has_work": False, "mark_processed": False, "tags": [], "units": [], "settled": {},
              "matrix": {"include": []}}
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

    include, units, settled = [], [], {}
    if scope:
        (scoped,) = map_units([unit("scoped", f"spec-sync/{slug(scope)}", from_tag, latest_api_tag, [], scope)],
                              api_tags)[0]
        units.append(scoped)
        include = [{"repo": repo, **scoped} for repo in targets]
    else:
        for repo in targets:
            repo_pending = [r for r in pending if repo not in processed_repos(r)]
            repo_units, already = plan_units(repo_pending, from_tag=from_tag, latest_api_tag=latest_api_tag)
            repo_units, unchanged = map_units(repo_units, api_tags)
            if already or unchanged:
                settled[repo] = merge_tags(already, unchanged)
            repo_units += refresh_units((open_prs or {}).get(repo), latest_api_tag, {u["branch"] for u in repo_units})
            for u in repo_units:
                include.append({"repo": repo, **u})
                if u not in units:
                    units.append(u)

    consumed = merge_tags(*settled.values(), *(i["tags"] for i in include))
    result.update(
        has_work=bool(include),
        units=units,
        tags=consumed,
        settled=settled,
        mark_processed=bool(consumed),
        matrix={"include": include},
    )
    if not include:
        result["reason"] = (
            f"No job is needed: {', '.join(consumed)} change nothing in the public spec." if consumed
            else "Nothing is pending."
        )
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


def section(body, heading):
    """The text under a level-2 heading in a body, up to the next level-2 heading."""
    match = re.search(r"^## " + re.escape(heading) + r"\n(.*?)(?=^## |\Z)", body or "", re.DOTALL | re.MULTILINE)
    if not match:
        return ""
    text = match.group(1).strip()
    # The footer follows the last section.
    return re.sub(r"\n*Last updated by \[this run\]\(.*?\) on [^\n]*\.\s*$", "", text).strip()


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
    if u["kind"] == "scoped":
        # A later run refreshes this PR with the same scope.
        state["scope"] = u["scope"]
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
# Spec changes
#
# The workflow lists every change between the two spec tags itself, and Claude
# must say what it did about each one. Without the list, a change Claude didn't
# notice goes unmentioned: Java's AML PR claimed spec v3.1192.0 but kept the
# v3.1188.0 types for AmlPersonMatch.


HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
# Keys that change only documentation, not what an SDK sends or parses.
DOC_KEYS = {"description", "summary", "example", "examples", "externalDocs", "title", "x-codeSamples"}
SPEC_CHANGE_OUTCOMES = ("changed", "already_matches", "not_applicable")
MAX_LISTED_GAPS = 20


def strip_docs(node):
    if isinstance(node, dict):
        return {k: strip_docs(v) for k, v in node.items() if k not in DOC_KEYS}
    if isinstance(node, list):
        return [strip_docs(v) for v in node]
    return node


def hidden(node):
    return isinstance(node, dict) and (node.get("x-internal") is True or bool(node.get("x-smile-sdk-exclude")))


def public_operations(spec):
    ops = {}
    for path, item in ((spec or {}).get("paths") or {}).items():
        if not isinstance(item, dict) or hidden(item):
            continue
        shared = strip_docs({k: v for k, v in item.items() if k not in HTTP_METHODS})
        for method in HTTP_METHODS:
            op = item.get(method)
            if isinstance(op, dict) and not hidden(op):
                ops[f"{method.upper()} {path}"] = {"shared": shared, **strip_docs(op)}
    return ops


def public_schemas(spec):
    schemas = ((spec or {}).get("components") or {}).get("schemas") or {}
    return {name: strip_docs(s) for name, s in schemas.items() if not hidden(s)}


def response_headers(op):
    """Each response header an operation declares, with the statuses that return it."""
    found = {}
    responses = (op or {}).get("responses") or {}
    for status in sorted(responses, key=str):
        response = responses[status]
        headers = response.get("headers") if isinstance(response, dict) else None
        for name in (headers or {}) if isinstance(headers, dict) else ():
            found.setdefault(name, []).append(str(status))
    return found


def changed_keys(old, new):
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


def spec_changes(old, new):
    """Every change between two OpenAPI documents that can affect an SDK.

    Returns a list of {"id", "change"}. Operations are compared whole, naming
    the parts that changed. Schemas are compared property by property, so a
    changed field type is its own entry. Documentation-only changes and anything
    marked x-internal or x-smile-sdk-exclude are left out.
    """
    changes = []

    def add(item_id, change):
        changes.append({"id": item_id, "change": change})

    old_ops, new_ops = public_operations(old), public_operations(new)
    for key in sorted(set(old_ops) | set(new_ops)):
        if key not in old_ops:
            add(f"operation {key}", "added")
        elif key not in new_ops:
            add(f"operation {key}", "removed")
        elif old_ops[key] != new_ops[key]:
            add(f"operation {key}", "changed: " + ", ".join(changed_keys(old_ops[key], new_ops[key])))

    # Response headers get their own entries. A header such as SmileID-Job-ID on
    # a 408 is easy to miss inside a whole-operation entry, and an SDK that drops
    # it leaves callers without a value the API sends them.
    for key in sorted(new_ops):
        before = response_headers(old_ops.get(key))
        after = response_headers(new_ops[key])
        for name in sorted(set(before) | set(after), key=str.lower):
            item = f"response header {name} on {key}"
            if name not in before:
                add(item, f"added: returned with {', '.join(after[name])}")
            elif name not in after:
                add(item, "removed")
            elif before[name] != after[name]:
                add(item, f"changed: now returned with {', '.join(after[name])}")

    old_schemas, new_schemas = public_schemas(old), public_schemas(new)
    for name in sorted(set(old_schemas) | set(new_schemas)):
        if name not in old_schemas:
            add(f"schema {name}", "added")
            continue
        if name not in new_schemas:
            add(f"schema {name}", "removed")
            continue
        before, after = old_schemas[name], new_schemas[name]
        if before == after:
            continue
        old_props = before.get("properties") or {} if isinstance(before, dict) else {}
        new_props = after.get("properties") or {} if isinstance(after, dict) else {}
        for prop in sorted(set(old_props) | set(new_props)):
            if prop not in old_props:
                add(f"schema {name}.{prop}", "added")
            elif prop not in new_props:
                add(f"schema {name}.{prop}", "removed")
            elif old_props[prop] != new_props[prop]:
                old_prop, new_prop = old_props[prop], new_props[prop]
                parts = changed_keys(old_prop, new_prop) if isinstance(old_prop, dict) and isinstance(new_prop, dict) \
                    else ["value"]
                add(f"schema {name}.{prop}", "changed: " + ", ".join(parts))
        if isinstance(before, dict) and isinstance(after, dict):
            rest = [k for k in changed_keys(before, after) if k != "properties"]
            if rest:
                add(f"schema {name}", "changed: " + ", ".join(rest))
        elif before != after:
            add(f"schema {name}", "changed")
    return changes


def load_spec(spec_dir, tag):
    """The spec at a tag, converted to JSON by the workflow (openapi.json beside openapi.yaml)."""
    path = Path(spec_dir) / tag / "openapi.json"
    data = read_json(path)
    return data if isinstance(data, dict) else None


def render_spec_changes(changes, base_tag, spec_tag):
    if not changes:
        return f"The workflow found no change between `{base_tag}` and `{spec_tag}` that can affect an SDK.\n"
    lines = [f"Changes between `{base_tag}` and `{spec_tag}`, listed by the workflow. Each needs an entry in "
             "`spec_changes` in result.json.", "", "| id | change |", "|---|---|"]
    lines += [f"| `{c['id']}` | {c['change']} |" for c in changes]
    return "\n".join(lines) + "\n"


def spec_change_gaps(changes, reported, changed_files):
    """Gaps for listed spec changes that Claude's result doesn't account for.

    Each change needs an entry with an outcome. "changed" must name files this
    PR changes; "already_matches" and "not_applicable" need a reason.
    """
    by_id = {}
    for entry in reported if isinstance(reported, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            by_id[entry["id"]] = entry
    changed = set(changed_files)
    missing, unproven = [], []
    for change in changes:
        entry = by_id.get(change["id"])
        outcome = entry.get("outcome") if entry else None
        if outcome not in SPEC_CHANGE_OUTCOMES:
            missing.append(change["id"])
        elif outcome == "changed":
            files = entry.get("files") if isinstance(entry.get("files"), list) else []
            if not any(isinstance(f, str) and f in changed for f in files):
                unproven.append(change["id"])
        elif not str(entry.get("reason") or "").strip():
            missing.append(change["id"])

    def listed(ids):
        shown = ", ".join(f"`{i}`" for i in ids[:MAX_LISTED_GAPS])
        return shown + (f" and {len(ids) - MAX_LISTED_GAPS} more" if len(ids) > MAX_LISTED_GAPS else "")

    gaps = []
    if missing:
        gaps.append(f"Claude did not account for {len(missing)} spec change(s) the workflow found: {listed(missing)}.")
    if unproven:
        gaps.append(f"Claude said it changed the code for {len(unproven)} spec change(s), but named no file this "
                    f"pull request changes: {listed(unproven)}.")
    return gaps


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
    """The open PR from this repository's own branch. --head also matches fork
    branches of the same name, and the SDK repositories are public."""
    out = gh(
        ["pr", "list", "--repo", f"{ORG}/{repo}", "--head", branch, "--state", "open",
         "--json", "number,url,isDraft,body,isCrossRepository", "--limit", "20"],
        token,
    )
    prs = [pr for pr in json.loads(out or "[]") if not pr.get("isCrossRepository", True)]
    return prs[0] if prs else None


SCOPED_TITLE = re.compile(r"^Sync (.+) with the API spec at v\d+\.\d+\.\d+$")


def open_pr_info(pr):
    """What a refresh needs from an open spec-sync PR, read from its description's marker."""
    marker = read_marker(pr.get("body"), PR_MARKER)
    branch = pr.get("headRefName", "")
    kind = marker.get("kind")
    if not kind:
        if branch == ROLLING_BRANCH:
            kind = "rolling"
        elif branch.startswith("spec-sync/breaking-"):
            kind = "breaking"
        else:
            kind = "scoped"
    scope = marker.get("scope", "")
    if kind == "scoped" and not scope:
        # PRs opened before the scope was recorded carry it only in their title.
        match = SCOPED_TITLE.match(pr.get("title", ""))
        scope = match.group(1) if match else ""
    return {"number": pr.get("number"), "url": pr.get("url", ""), "branch": branch, "kind": kind,
            "spec_tag": marker.get("spec_tag", ""), "scope": scope}


def list_open_prs(repos, token):
    """The open spec-sync PRs in each repository, for planning refreshes.

    A repository that can't be read is left out with a warning, so its PRs are
    only refreshed on a later run.
    """
    found = {}
    for repo in repos:
        try:
            out = gh(["pr", "list", "--repo", f"{ORG}/{repo}", "--label", LABEL, "--state", "open",
                      "--json", "number,url,title,body,headRefName,isCrossRepository", "--limit", "50"], token)
        except SpecSyncError as e:
            print(f"::warning::Could not list the spec-sync pull requests in {repo}, so none are refreshed: {e}")
            continue
        found[repo] = [open_pr_info(pr) for pr in json.loads(out or "[]")
                       if not pr.get("isCrossRepository", True)
                       and str(pr.get("headRefName", "")).startswith("spec-sync/")]
    return found


EMPTY_PR_COMMENT = (
    "Closed by the spec-sync workflow: this branch no longer changes anything compared with the default branch, "
    "so merging it would add nothing."
)


def close_empty_pr(repo, pr, token):
    gh(["pr", "close", str(pr["number"]), "--repo", f"{ORG}/{repo}", "--comment", EMPTY_PR_COMMENT,
        "--delete-branch"], token)
    print(f"Closed {pr['url']}: it no longer changes anything")


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


def default_branch(repo, token):
    return gh(["repo", "view", f"{ORG}/{repo}", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
              token).strip()


def prepare(*, u, repo, workdir, ctx, state_dir, template, token, read_token):
    """Set up the working tree and Claude's context. Needs only a read token.

    Returns the state, including `base` (the commit Claude's change is
    measured from) and `default_sha`, which the publish job checks.
    """
    ctx = Path(ctx)
    (ctx / "out").mkdir(parents=True, exist_ok=True)
    default = default_branch(repo, token)
    branch = u["branch"]
    pr = find_open_pr(repo, branch, token)
    remote = f"{GIT_BASE}/{ORG}/{repo}.git"
    git(["fetch", "--quiet", remote, f"+refs/heads/{default}:refs/remotes/origin/{default}"], workdir, token)
    default_sha = git(["rev-parse", f"origin/{default}"], workdir).strip()
    conflict = False
    if pr:
        git(["fetch", "--quiet", remote, f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], workdir, token)
        git(["checkout", "--quiet", "-B", branch, f"origin/{branch}"], workdir)
        code, _, _ = git(
            ["-c", "user.name=spec-sync", "-c", "user.email=spec-sync@invalid",
             "merge", "--no-edit", "--quiet", f"origin/{default}"],
            workdir, check=False,
        )
        conflict = code != 0
    else:
        git(["checkout", "--quiet", "-B", branch, f"origin/{default}"], workdir)
    base = git(["rev-parse", f"origin/{branch}" if pr else f"origin/{default}"], workdir).strip()

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
    kind_note = kind_notes[u["kind"]]
    if u.get("refresh"):
        kind_note += (
            f"\n\nThis run brings the open pull request up to date. It was written against the spec at "
            f"`{u['base_tag']}`, and the spec has changed since. "
        )
        if u["kind"] == "rolling":
            kind_note += "Make every change between the two spec tags, as for any rolling run."
        else:
            kind_note += (
                "Compare the two spec tags and update the code this pull request adds or changes wherever the "
                "spec change affects it: a changed type, field, enum, path or token product in an operation or "
                "model this pull request covers. Leave spec changes outside this pull request alone; the rolling "
                "spec-sync pull request handles them. If nothing this pull request covers changed, change nothing."
            )
    previous_summary = section(pr["body"], "What changed") if pr else ""
    if previous_summary:
        (ctx / "previous-summary.md").write_text(previous_summary + "\n")
    existing_pr = "There is no open pull request yet."
    if pr:
        existing_pr = (
            f"An open pull request already covers {', '.join(previous.get('tags', [])) or 'earlier changes'}; "
            "keep its work and add to it."
        )
        if previous_summary:
            existing_pr += (
                f" Its current summary is in `{ctx}/previous-summary.md`. Your `summary.md` replaces it, so "
                "describe the whole pull request, not only this run's change."
            )
    values = {
        "repo": repo,
        "ctx": str(ctx),
        "default_branch": default,
        "branch": branch,
        "base_tag": u["base_tag"],
        "spec_tag": u["spec_tag"],
        "tags": ", ".join(u["tags"]) or "none",
        "kind_note": kind_note,
        "existing_pr": existing_pr,
        "conflict_note": (
            f"Merging `{default}` into this branch left conflicts. Resolve every conflict marker first and "
            "`git add` the resolved files, but do not commit." if conflict else ""
        ),
        "repo_note": FIXTURES_NOTE if repo == FIXTURES_REPO else SDK_NOTE.format(ctx=ctx),
        "spec_changes_note": spec_changes_note(u, ctx, repo),
        "coverage_note": coverage_note(u, ctx, repo, workdir),
    }
    (ctx / "TASK.md").write_text(render_task(template, values))
    return {"repo": repo, "default": default, "base": base, "default_sha": default_sha, "pr": pr,
            "conflict": conflict, "previous": previous, "unit": u}


def checks_spec_changes(repo):
    """The fixtures repository has no models to keep in step, so only the SDKs are checked."""
    return repo in SDK_REPOS


def spec_changes_note(u, ctx, repo):
    """Write the workflow's list of spec changes for Claude, and say how to report on it."""
    if not checks_spec_changes(repo):
        return ""
    old, new = load_spec(Path(ctx) / "spec", u["base_tag"]), load_spec(Path(ctx) / "spec", u["spec_tag"])
    if old is None or new is None:
        return ("The workflow could not list the spec changes for this run, so compare the two spec files "
                "yourself and leave `spec_changes` out of result.json.")
    path = Path(ctx) / "spec-changes.md"
    path.write_text(render_spec_changes(spec_changes(old, new), u["base_tag"], u["spec_tag"]))
    return (
        f"`{path}` lists every change between the two spec tags that can affect an SDK, found by the workflow. "
        "Go through every entry, including ones you think need nothing: this list is how a change you would "
        "otherwise miss gets noticed, such as a field whose type changed. A `response header` entry is handled "
        "only when a caller can read that header's value from what the SDK returns for each listed status: the "
        "result for a success, and the error for an error status. In result.json, `spec_changes` must "
        "have one entry for each id in the list, with an `outcome`:\n"
        "   - `changed`: you changed the SDK for it. `files` lists the files you changed for it, relative to the "
        "repository root.\n"
        "   - `already_matches`: the SDK already matches the newer spec. `reason` says where, such as the file "
        "and type.\n"
        "   - `not_applicable`: it doesn't apply to this pull request, for example because it is outside its "
        "scope. `reason` says why.\n"
        "   The publish job checks every id has an entry, and that each `changed` entry names a file this pull "
        "request changes. A missing or unproven entry keeps the pull request a draft."
    )



def checks_coverage(u, repo, workdir):
    """Rolling and breaking runs bring the SDK up to the whole spec, so they must keep spec-coverage.json in step.

    A scoped run covers only part of the spec, and an SDK without the file has not adopted the check yet.
    """
    return repo in SDK_REPOS and u["kind"] != "scoped" and (Path(workdir) / spec_coverage.MANIFEST).is_file()


def coverage_problems(u, workdir, spec):
    manifest = read_json(Path(workdir) / spec_coverage.MANIFEST)
    if manifest is None:
        return [f"{spec_coverage.MANIFEST} is not valid JSON."]
    return spec_coverage.check(manifest, spec, u["spec_tag"])


def coverage_note(u, ctx, repo, workdir):
    """Tell Claude what spec-coverage.json is and what it is missing against the new spec."""
    if not checks_coverage(u, repo, workdir):
        return ""
    spec = load_spec(Path(ctx) / "spec", u["spec_tag"])
    if spec is None:
        return ""
    problems = coverage_problems(u, workdir, spec)
    path = Path(ctx) / "coverage-gaps.md"
    path.write_text("".join(f"- {p}\n" for p in problems) or "None.\n")
    script = Path(ctx) / "spec_coverage.py"
    script.write_text(Path(spec_coverage.__file__).read_text())
    return (
        f"`{spec_coverage.MANIFEST}` maps every public operation in the spec, and every response header it "
        "returns, to the SDK method or field that exposes it, or marks it unsupported with a reason. It covers the "
        "whole spec, not only what changed, so it also lists operations that were in the spec before syncing "
        f"started. Bring it up to `{u['spec_tag']}`: set `spec_tag` to `{u['spec_tag']}`, and add or change "
        "entries for every operation and header you add or change. "
        f"`{path}` lists what the file is missing against `{u['spec_tag']}` right now. For each, implement it "
        "with tests if it is part of this change. Otherwise leave the existing `unsupported` entry alone, or, for "
        "something new you can't implement here, add one with the reason and list it under gaps. Never mark "
        "something unsupported that the SDK already does. The publish job runs the same check, and any "
        "problem left keeps the pull request a draft. Run it yourself before you finish: "
        f"`python3 {script} --spec {Path(ctx) / 'spec' / u['spec_tag'] / 'openapi.json'} --spec-tag {u['spec_tag']}`, "
        "from the repository root."
    )

SDK_NOTE = (
    "The integration cases live in the private `smileid-sdk-fixtures` repository, as "
    "`cases/<operationId>.json`. They are generated from lambda's sandbox test identities, so they always "
    "match what sandbox returns. A read-only copy is at `{ctx}/fixtures` if this run could read it. Another "
    "job in this run updates that repository, so do not edit it. If the integration suite here needs a "
    "change to run the cases for an operation you add (for example, mapping its operationId to an SDK "
    "method), make it. Do not list persona cases as a gap: an operation that has a cases file is covered. "
    "Only if an operation you add has no cases file, say so in your summary, not as a gap: the cases are "
    "generated from sandbox, so this run can't add one. A lookup on an existing job, such as a GET for its "
    "status or news, needs no cases file of its own.\n\n"
    "Token products: an operation's `x-smile-token-product` is the `product` claim the API requires in its "
    "token. An operation without it requires a token with no `product` claim. Take each operation's product "
    "from this extension in the spec, not from a table kept by hand: if this SDK has a hand-kept table of "
    "paths to products, replace it with one taken from the spec, and keep the way the SDK sets the claim. "
    "An operation whose only change is this extension still needs this change. Check the claim, or its "
    "absence, in the golden fixture tests for every operation whose product changed."
)
FIXTURES_NOTE = (
    "This repository holds the shared integration cases. `personas.json` and `cases/` are generated: "
    "`personas.json` comes from lambda's sandbox test identities through `scripts/sync_personas.sh`, which a "
    "separate daily workflow runs, so never edit `personas.json` or `cases/` by hand. Follow the README: if an "
    "operation's inputs changed or there is a new operation that takes a persona, change `operations.json`, "
    "rebuild with `python scripts/build_cases.py`, and run `python scripts/validate.py`. You cannot run the "
    "cases against dev here, so say so in your summary. There are no golden fixtures or coverage gates in "
    "this repository."
)


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# The statuses Claude may report in result.json. "closed" is the workflow's own.
CLAUDE_STATUSES = ("changed", "no_change", "blocked")


def package(*, u, workdir, ctx, base, default_sha, dest):
    """Bundle Claude's change for the publish job. Runs with no token.

    The bundle is the working tree's diff from `base` as a binary patch,
    Claude's result and summary, and the commits the patch was made against.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    git(["add", "-A"], workdir)
    patch = git(["diff", "--cached", "--binary", "--full-index", base], workdir)
    (dest / "change.patch").write_text(patch)
    for name in ("result.json", "summary.md"):
        source = Path(ctx) / "out" / name
        if source.exists():
            (dest / name).write_bytes(source.read_bytes())
    meta = {"repo": u["repo"], "branch": u["branch"], "base": base, "default_sha": default_sha}
    (dest / "meta.json").write_text(json.dumps(meta))
    return meta


def publish(*, u, workdir, bundle, out, state_dir, write_token, bot_name, bot_email, run_url, now, spec_dir=None):
    """Check the sync job's patch and push it, from a fresh clone.

    Runs in its own job, after Claude's job has ended. Nothing it uses comes
    from Claude's runner except the bundle: a patch, Claude's result and
    summary, and the two commits the patch was made against. Each is checked
    before it is used.
    """
    out, bundle = Path(out), Path(bundle)
    out.mkdir(parents=True, exist_ok=True)
    repo = u["repo"]
    if repo not in ALL_REPOS or not str(u.get("branch", "")).startswith("spec-sync/"):
        raise SpecSyncError("The job does not describe a spec-sync branch, so nothing was pushed")
    default = default_branch(repo, write_token)
    if u["branch"] == default:
        raise SpecSyncError("The spec-sync branch is the default branch, so nothing was pushed")
    pr = find_open_pr(repo, u["branch"], write_token)
    outcome = {"repo": repo, "branch": u["branch"], "kind": u["kind"], "spec_tag": u["spec_tag"],
               "status": "failed", "pr_url": pr["url"] if pr else ""}

    def finish(status, error=None):
        outcome["status"] = status
        (out / "job-result.json").write_text(json.dumps(outcome))
        if error:
            raise SpecSyncError(error)
        return outcome

    meta = read_json(bundle / "meta.json")
    if not isinstance(meta, dict) or (meta.get("repo"), meta.get("branch")) != (repo, u["branch"]):
        return finish("failed", "The sync job's bundle is missing or is for another job, so nothing was pushed")
    base, default_sha = meta.get("base", ""), meta.get("default_sha", "")
    if not SHA_RE.match(base) or not SHA_RE.match(default_sha):
        return finish("failed", "The sync job's bundle has no valid base commits, so nothing was pushed")
    result = read_json(bundle / "result.json")
    if not isinstance(result, dict) or result.get("status") not in CLAUDE_STATUSES:
        return finish("failed", "Claude did not write a valid result.json, so nothing was pushed")
    summary_path = bundle / "summary.md"
    summary = summary_path.read_text() if summary_path.exists() else ""
    patch_path = bundle / "change.patch"
    patch = patch_path.read_text(errors="replace") if patch_path.exists() else ""

    remote = f"{GIT_BASE}/{ORG}/{repo}.git"
    git(["fetch", "--quiet", remote, f"+refs/heads/{default}:refs/remotes/origin/{default}"], workdir, write_token)
    code, _, _ = git(["merge-base", "--is-ancestor", default_sha, f"origin/{default}"], workdir, check=False)
    if code != 0:
        return finish("failed", f"{default_sha[:12]} is not on {default}, so nothing was pushed")
    if pr:
        git(["fetch", "--quiet", remote, f"+refs/heads/{u['branch']}:refs/remotes/origin/{u['branch']}"],
            workdir, write_token)
        expected = git(["rev-parse", f"origin/{u['branch']}"], workdir).strip()
    else:
        expected = default_sha
    if base != expected:
        return finish("failed", f"{u['branch']} changed while Claude worked, so nothing was pushed. The next run retries.")

    git(["checkout", "--quiet", "-B", u["branch"], base], workdir)
    # The sync job merged the default branch in. Record that merge; the patch
    # brings the merged and resolved files.
    code, _, _ = git(["merge-base", "--is-ancestor", default_sha, "HEAD"], workdir, check=False)
    merging = code != 0
    if merging:
        git(["-c", f"user.name={bot_name}", "-c", f"user.email={bot_email}",
             "merge", "--quiet", "--no-commit", "-s", "ours", default_sha], workdir)

    # The patch also carries the default branch's merged-in changes, so the
    # guards run after applying it, on the result compared with that branch.
    if patch.strip():
        patch_file = out / "change.patch"
        patch_file.write_text(patch)
        code, _, err = git(["apply", "--index", "--binary", "--whitespace=nowarn", str(patch_file)], workdir,
                           check=False)
        if code != 0:
            return finish("failed", f"The patch did not apply, so nothing was pushed: {err.strip()[:300]}")
    # Guard again on the result, compared with the default branch, as the PR shows it.
    changed_files = [f for f in git(["diff", "--cached", "--name-only", default_sha], workdir).splitlines() if f]
    problems = guard_problems(changed_files, git(["diff", "--cached", default_sha], workdir))
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return finish("failed", "The changes broke a spec-sync rule, so nothing was pushed")

    if not changed_files and not pr:
        print(f"No changes are needed in {repo}.")
        return finish("no_change")
    if not changed_files:
        # Everything the PR changed is now on the default branch, for example
        # because another PR made the same change. Merging it would add nothing.
        close_empty_pr(repo, pr, write_token)
        return finish("closed")

    code, _, _ = git(["diff", "--cached", "--quiet"], workdir, check=False)
    if code != 0 or merging:
        git(["-c", f"user.name={bot_name}", "-c", f"user.email={bot_email}", "commit", "--quiet",
             "--no-verify", "-m", f"Sync with the API spec at {u['spec_tag']}"], workdir)
    head = git(["rev-parse", "HEAD"], workdir).strip()
    pushed = head != base or not pr
    if pushed:
        push = ["push", "--quiet", "--no-verify", remote, f"HEAD:refs/heads/{u['branch']}"]
        if not pr:
            # No open PR, so any branch left over from a merged or closed one is replaced.
            push.insert(1, "--force")
        git(push, workdir, write_token)

    previous = read_marker(pr["body"], PR_MARKER) if pr else {}
    tags = merge_tags(previous.get("tags", []), u["tags"])
    spec_tag = max([u["spec_tag"], *([previous["spec_tag"]] if previous.get("spec_tag") else [])], key=tag_key)
    lambda_prs = sorted(set(previous.get("lambda_prs", [])) | set(u["lambda_prs"]))
    records = [r for r in (find_record(state_dir, t) for t in tags) if r]
    if checks_spec_changes(repo):
        # The specs come from the publish job's own download, not the sync job's runner.
        old = load_spec(spec_dir, u["base_tag"]) if spec_dir else None
        new = load_spec(spec_dir, u["spec_tag"]) if spec_dir else None
        if old is None or new is None:
            extra = [f"The publish job could not read the spec at `{u['base_tag']}` and `{u['spec_tag']}`, so it "
                     "could not check that every spec change was handled."]
        else:
            extra = spec_change_gaps(spec_changes(old, new), result.get("spec_changes"), changed_files)
        if checks_coverage(u, repo, workdir):
            if new is None:
                extra.append(f"The publish job could not read the spec at `{u['spec_tag']}`, so it could not check "
                             f"`{spec_coverage.MANIFEST}`.")
            else:
                problems = coverage_problems(u, workdir, new)
                if problems:
                    shown = " ".join(problems[:MAX_LISTED_GAPS])
                    more = len(problems) - MAX_LISTED_GAPS
                    extra.append(f"`{spec_coverage.MANIFEST}` doesn't match the spec at `{u['spec_tag']}`: {shown}"
                                 + (f" And {more} more." if more > 0 else ""))
        if extra:
            result = {**result, "gaps": [*(result.get("gaps") or []), *extra]}
    ready_ok = result["status"] != "blocked" and not result.get("gaps")
    if result["status"] == "no_change" and not pr:
        ready_ok = False
    if result["status"] == "no_change" and pr:
        # Claude changed nothing (at most the default branch was merged in), so
        # the PR's summary of its change still stands.
        summary = section(pr["body"], "What changed") or summary
    body = render_pr_body(repo=repo, u=u, tags=tags, spec_tag=spec_tag, lambda_prs=lambda_prs, records=records,
                          summary=summary, result=result, ready_ok=ready_ok, run_url=run_url, now=now)
    body_file = out / "pr-body.md"
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
    # A newer run of a workflow can cancel an older one on the same commit, for
    # example when a label is added just after a push. The rollup keeps both, so
    # judge each check by its latest run, unless a run of it is still going.
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


def is_behind(spec_tag, latest_api_tag):
    return bool(TAG_RE.match(spec_tag or "") and latest_api_tag and tag_key(spec_tag) < tag_key(latest_api_tag))


def promote_prs(repos, token, latest_api_tag=None):
    """Mark green draft spec-sync PRs ready. Returns the open PRs per repo.

    A PR written against an older spec than latest_api_tag goes back to draft,
    so it can't be merged with code the newer spec changed, until the next sync
    refreshes it. A PR that no longer changes anything is closed.
    """
    found = {}
    for repo in repos:
        # One repository that can't be read must not stop the others or the status issue.
        try:
            out = gh(
                ["pr", "list", "--repo", f"{ORG}/{repo}", "--label", LABEL, "--state", "open",
                 "--json", "number,url,title,isDraft,body,headRefName,statusCheckRollup,isCrossRepository,changedFiles",
                 "--limit", "50"],
                token,
            )
        except SpecSyncError as e:
            print(f"::warning::Could not list the spec-sync pull requests in {repo}: {e}")
            found[repo] = None
            continue
        prs = []
        for pr in json.loads(out or "[]"):
            if pr.get("isCrossRepository", True):
                continue
            if pr.get("changedFiles") == 0 and str(pr.get("headRefName", "")).startswith("spec-sync/"):
                try:
                    close_empty_pr(repo, pr, token)
                except SpecSyncError as e:
                    print(f"::warning::Could not close {pr['url']}, which no longer changes anything: {e}")
                else:
                    continue
            marker = read_marker(pr.get("body"), PR_MARKER)
            checks = checks_state(pr.get("statusCheckRollup"))
            draft = pr.get("isDraft", False)
            behind = is_behind(marker.get("spec_tag"), latest_api_tag)
            # A PR the workflow can't refresh is still held back, so it isn't merged with
            # code the newer spec changed, but it needs a person to refresh it.
            problem = refresh_problem(open_pr_info(pr)) if behind else None
            if problem:
                print(f"::warning::{pr['url']} is behind the spec and can't be refreshed automatically: {problem}. "
                      "Refresh it by hand.")
            if behind and not draft:
                try:
                    gh(["pr", "ready", str(pr["number"]), "--repo", f"{ORG}/{repo}", "--undo"], token)
                except SpecSyncError as e:
                    print(f"::warning::Could not move {pr['url']} back to draft: {e}")
                else:
                    print(f"Moved {pr['url']} back to draft: it was written against {marker.get('spec_tag')}, "
                          f"and the spec is now at {latest_api_tag}."
                          + (" It needs refreshing by hand." if problem else " The next sync refreshes it."))
                    draft = True
            if draft and not behind and checks == "success" and marker.get("ready_ok"):
                # One PR that can't be marked ready must not stop the others or the status issue.
                try:
                    gh(["pr", "ready", str(pr["number"]), "--repo", f"{ORG}/{repo}"], token)
                except SpecSyncError as e:
                    print(f"::warning::Could not mark {pr['url']} ready for review: {e}")
                else:
                    print(f"Marked {pr['url']} ready for review")
                    draft = False
            prs.append({"number": pr["number"], "url": pr["url"], "draft": draft, "checks": checks,
                        "branch": pr.get("headRefName", ""), "spec_tag": marker.get("spec_tag", ""),
                        "behind": behind, "refresh_problem": problem})
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
        elif "closed" in statuses:
            status = "closed an empty PR"
        else:
            status = "no change"
        runs[repo] = {"result": status, "run_url": run_url, "at": now}
    return runs


def render_status_issue(*, prs, runs, pending, last_processed, run_url, now, synced=None):
    """synced maps a pending tag to the number of repositories that have already synced it."""
    state = {"runs": runs}
    synced = synced or {}

    def pending_text(tag):
        count = synced.get(tag)
        return f"`{tag}` ({count} of {len(ALL_REPOS)} repositories done)" if count else f"`{tag}`"

    lines = [
        write_marker(ISSUE_MARKER, state),
        "The `sdk-spec-sync` workflow keeps this issue up to date. It edits this description in place and does not comment.",
        "",
        (
            f"Pending lambda releases: {', '.join(pending_text(t) for t in pending) or 'none'}. "
            f"Last processed: {f'`{last_processed}`' if last_processed else 'none'}."
        ),
        "",
        "| Repository | Open spec-sync PRs | Spec tag | Last run |",
        "|---|---|---|---|",
    ]
    for repo in ALL_REPOS:
        repo_prs = prs.get(repo, [])
        if repo_prs is None:
            run = runs.get(repo)
            run_text = f"{run['result']}, [{run['at']}]({run['run_url']})" if run else "not run yet"
            lines.append(f"| `{repo}` | could not be read in this run | unknown | {run_text} |")
            continue
        cells = []
        for pr in repo_prs:
            state_text = "draft" if pr["draft"] else "ready"
            checks = {"success": "checks green", "pending": "checks running", "failure": "checks failing",
                      "none": "no checks yet"}[pr["checks"]]
            cell = f"[#{pr['number']}]({pr['url']}) {state_text}, {checks}"
            if pr.get("behind"):
                cell += f", behind the spec (written against `{pr['spec_tag']}`), " + (
                    "can't be refreshed automatically: refresh it by hand" if pr.get("refresh_problem")
                    else "waiting for the next sync")
            cells.append(cell)
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

    p = sub.add_parser("package")
    p.add_argument("--unit", required=True, help="the matrix entry as JSON")
    p.add_argument("--workdir", required=True)
    p.add_argument("--ctx", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--default-sha", required=True)
    p.add_argument("--dest", required=True)

    p = sub.add_parser("publish")
    p.add_argument("--unit", required=True, help="the matrix entry as JSON")
    p.add_argument("--workdir", required=True, help="an empty git repository to publish from")
    p.add_argument("--bundle", required=True, help="the decrypted bundle from the sync job")
    p.add_argument("--out", required=True)
    p.add_argument("--state-dir", required=True)
    p.add_argument("--bot-name", required=True)
    p.add_argument("--bot-email", required=True)
    p.add_argument("--run-url", required=True)
    p.add_argument("--spec-dir", default="", help="<tag>/openapi.json for the unit's two spec tags")

    p = sub.add_parser("mark-processed")
    p.add_argument("--state-dir", default=".")
    p.add_argument("--tags", help="JSON list of tags that every repository has synced")
    p.add_argument("--matrix", help="the plan's matrix as JSON; with --results-dir, marks the jobs that succeeded")
    p.add_argument("--settled", default="{}", help="the plan's settled tags as JSON")
    p.add_argument("--results-dir", help="the publish jobs' job-result.json files")
    p.add_argument("--run-url", required=True)

    p = sub.add_parser("status")
    p.add_argument("--state-dir", required=True)
    p.add_argument("--results-dir", required=True)
    p.add_argument("--planned-repos", default="[]", help="JSON list of repos this run synced")
    p.add_argument("--sync-result", default="skipped")
    p.add_argument("--run-url", required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--api-tags-file", default="", help="api-reference tags; PRs behind the newest go back to draft")

    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            event = json.loads(Path(args.event).read_text())
            record_dispatch(event, args.state_dir, args.run_url, now_utc())
        elif args.command == "plan":
            api_tags = Path(args.api_tags_file).read_text().split() if args.api_tags_file else []
            open_prs = None
            sync_due = args.sync_due == "true"
            # OPEN_PRS_TOKEN reads the SDK repositories' PRs. Without it, open PRs aren't refreshed.
            if sync_due and not args.scope.strip():
                token = os.environ.get("OPEN_PRS_TOKEN", "")
                if token:
                    open_prs = list_open_prs(select_repos(args.repos), token)
                else:
                    print("::warning::No token could read the SDK repositories, so open spec-sync PRs that are "
                          "behind the spec are not refreshed in this run.")
            result = plan(load_records(args.state_dir, "pending"), from_tag=args.from_tag.strip(),
                          scope=args.scope.strip(), repos=args.repos, latest_api_tag=latest_tag(api_tags),
                          sync_due=sync_due, api_tags=api_tags if args.api_tags_file else None,
                          open_prs=open_prs)
            for key in ("has_work", "mark_processed"):
                write_output(key, str(result[key]).lower())
            write_output("tags", result["tags"])
            write_output("settled", result["settled"])
            write_output("matrix", result["matrix"])
            write_output("repos", sorted({i["repo"] for i in result["matrix"]["include"]}))
            print(result.get("reason") or f"Planned {len(result['matrix']['include'])} jobs: "
                  + "; ".join(f"{i['repo']} {i['branch']} {i['base_tag']}..{i['spec_tag']}"
                              + (" (refresh)" if i.get("refresh") else "") for i in result["matrix"]["include"]))
        elif args.command == "prepare":
            u = json.loads(args.unit)
            state = prepare(u=u, repo=u["repo"], workdir=args.workdir, ctx=args.ctx, state_dir=args.state_dir,
                            template=Path(args.template).read_text(), token=os.environ["REPO_TOKEN"],
                            read_token=os.environ.get("READ_TOKEN", ""))
            write_output("base", state["base"])
            write_output("default_sha", state["default_sha"])
        elif args.command == "package":
            meta = package(u=json.loads(args.unit), workdir=args.workdir, ctx=args.ctx, base=args.base,
                           default_sha=args.default_sha, dest=args.dest)
            print(f"Bundled the change to {meta['repo']} {meta['branch']} from {meta['base'][:12]}")
        elif args.command == "publish":
            publish(u=json.loads(args.unit), workdir=args.workdir, bundle=args.bundle, out=args.out,
                    state_dir=args.state_dir, write_token=os.environ["WRITE_TOKEN"], bot_name=args.bot_name,
                    bot_email=args.bot_email, run_url=args.run_url, now=now_utc(), spec_dir=args.spec_dir or None)
        elif args.command == "mark-processed":
            if args.tags:
                done = json.loads(args.tags)
            else:
                results = []
                if args.results_dir:
                    results = [read_json(p, {}) for p in sorted(Path(args.results_dir).glob("**/job-result.json"))]
                done = synced_by_run(json.loads(args.matrix or '{"include": []}'), json.loads(args.settled), results)
                for repo in ALL_REPOS:
                    if repo in done:
                        print(f"{repo} synced {', '.join(done[repo])}")
            moved = mark_processed(args.state_dir, done, now_utc(), args.run_url)
            print(f"Processed by every repository: {', '.join(moved) or 'nothing new'}")
        elif args.command == "status":
            token = os.environ["APP_TOKEN"]
            issue_token = os.environ["ISSUE_TOKEN"]
            repo_full = os.environ.get("GITHUB_REPOSITORY", f"{ORG}/central-workflows")
            api_tags = Path(args.api_tags_file).read_text().split() if args.api_tags_file else []
            prs = promote_prs(ALL_REPOS, token, latest_tag(api_tags))
            results = [read_json(p, {}) for p in sorted(Path(args.results_dir).glob("**/job-result.json"))]
            issue = find_status_issue(repo_full, issue_token)
            previous = read_marker(issue["body"], ISSUE_MARKER).get("runs", {}) if issue else {}
            now = now_utc()
            runs = update_last_runs(previous, [r for r in results if r.get("repo")],
                                    json.loads(args.planned_repos), args.run_url, now, args.sync_result)
            processed = load_records(args.state_dir, "processed")
            pending = load_records(args.state_dir, "pending")
            body = render_status_issue(prs=prs, runs=runs, pending=[r["tag"] for r in pending],
                                       last_processed=processed[-1]["tag"] if processed else "",
                                       run_url=args.run_url, now=now,
                                       synced={r["tag"]: len(processed_repos(r) & set(ALL_REPOS)) for r in pending})
            number = write_status_issue(repo_full, issue_token, body, issue, args.body_file)
            print(f"Updated status issue #{number}")
    except SpecSyncError as error:
        print(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
