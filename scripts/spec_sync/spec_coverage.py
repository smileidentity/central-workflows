#!/usr/bin/env python3
"""Check an SDK's spec-coverage.json against the whole public spec. Standard library only.

spec-sync only sees what changed between two spec tags, so an operation that
was already in the spec when an SDK started syncing never shows up in a diff.
This check closes that gap. Each SDK keeps a spec-coverage.json that maps every
public operation in the spec, and every response header it returns, to the SDK
method or value that exposes it, or marks it unsupported with a reason. The
check fails when the spec has something the file doesn't account for, or the
file names something the spec doesn't have.

An unsupported entry may add "backfill": false to say it should never be
added. spec-sync's backfill runs add everything else that is unsupported.

Usage:
  spec_coverage.py --manifest spec-coverage.json --spec openapi.json [--spec-tag v3.1215.0]

The spec must be JSON (convert the YAML with `yq -o=json`). With --spec-tag,
the manifest's spec_tag must equal it. Exits 1 and lists the problems if there
are any. The SDKs run it through the spec-coverage action in this repository,
and spec-sync runs it to find gaps for Claude and to check Claude's result.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MANIFEST = "spec-coverage.json"
HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def hidden(node):
    return isinstance(node, dict) and (node.get("x-internal") is True or bool(node.get("x-smile-sdk-exclude")))


def spec_operations(spec):
    """{operationId: {"path": "POST /v3/x", "headers": [response header names]}} for every public operation."""
    ops = {}
    for path, item in ((spec or {}).get("paths") or {}).items():
        if not isinstance(item, dict) or hidden(item):
            continue
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict) or hidden(op):
                continue
            key = f"{method.upper()} {path}"
            headers = {}
            for response in ((op.get("responses") or {}).values()):
                found = response.get("headers") if isinstance(response, dict) else None
                for name, header in (found.items() if isinstance(found, dict) else ()):
                    if not hidden(header):
                        headers.setdefault(name.lower(), name)
            ops[op.get("operationId") or key] = {"path": key, "headers": sorted(headers.values(), key=str.lower)}
    return ops


def _reason(value):
    """An unsupported entry: {"unsupported": "<reason>"}, optionally with "backfill": false."""
    return (isinstance(value, dict) and set(value) <= {"unsupported", "backfill"}
            and isinstance(value.get("unsupported"), str) and bool(value["unsupported"].strip())
            and value.get("backfill", False) is False)


def _excluded(value):
    """Unsupported on purpose: a maintainer marked it "backfill": false, so backfill runs leave it alone."""
    return _reason(value) and "backfill" in value


def check(manifest, spec, spec_tag=None):
    """The problems with a manifest, as plain-English strings. An empty list means it matches the spec."""
    if not isinstance(manifest, dict):
        return [f"{MANIFEST} must be a JSON object."]
    problems = []
    tag = manifest.get("spec_tag")
    if not isinstance(tag, str) or not TAG_RE.match(tag):
        problems.append(f"{MANIFEST} needs a `spec_tag`, the api-reference release tag it was checked against, "
                        "such as `v3.1215.0`.")
    elif spec_tag and tag != spec_tag:
        problems.append(f"{MANIFEST} is for the spec at `{tag}`, not `{spec_tag}`. Check it against `{spec_tag}` "
                        "and update `spec_tag`.")
    entries = manifest.get("operations")
    if not isinstance(entries, dict):
        return problems + [f"{MANIFEST} needs an `operations` object keyed by operationId."]

    ops = spec_operations(spec)
    for op_id in sorted(set(ops) - set(entries)):
        problems.append(f"`{op_id}` ({ops[op_id]['path']}) is in the spec but not in {MANIFEST}. Add the SDK "
                        "method that calls it, or mark it unsupported with a reason.")
    for op_id in sorted(set(entries) - set(ops)):
        problems.append(f"`{op_id}` is in {MANIFEST} but is not a public operation in the spec. Remove it.")

    for op_id in sorted(set(entries) & set(ops)):
        entry, op = entries[op_id], ops[op_id]
        if _reason(entry):
            continue
        if not isinstance(entry, dict) or "unsupported" in entry:
            problems.append(f"`{op_id}`: give either `method` with `path` and `response_headers`, or only "
                            "`unsupported` with a reason, plus `\"backfill\": false` if it should never be added.")
            continue
        if not isinstance(entry.get("method"), str) or not entry["method"].strip():
            problems.append(f"`{op_id}`: `method` must name the SDK method that calls it.")
        if entry.get("path") != op["path"]:
            problems.append(f"`{op_id}`: `path` must be `{op['path']}`, as in the spec.")
        headers = entry.get("response_headers", {})
        if not isinstance(headers, dict):
            problems.append(f"`{op_id}`: `response_headers` must be an object keyed by header name.")
            continue
        listed = {name.lower(): name for name in headers}
        for name in op["headers"]:
            value = headers.get(listed.get(name.lower(), name))
            if name.lower() not in listed:
                problems.append(f"`{op_id}`: the spec returns the `{name}` header, but {MANIFEST} doesn't say where "
                                "a caller reads it. Name the result or error field that exposes it, or mark it "
                                "unsupported with a reason.")
            elif not (isinstance(value, str) and value.strip()) and not _reason(value):
                problems.append(f"`{op_id}`: the `{name}` header needs the field that exposes it, or "
                                "`{\"unsupported\": \"<reason>\"}`.")
        spec_headers = {name.lower() for name in op["headers"]}
        for name in sorted(listed.values(), key=str.lower):
            if name.lower() not in spec_headers:
                problems.append(f"`{op_id}`: the spec doesn't return a `{name}` header. Remove it.")
    return problems


def _entries(manifest):
    entries = manifest.get("operations") if isinstance(manifest, dict) else None
    return entries if isinstance(entries, dict) else {}


def entry_for(manifest, operation, header=None):
    """The manifest's value for an operation, or for one of its response headers (matched without case)."""
    entry = _entries(manifest).get(operation)
    if header is None or not isinstance(entry, dict) or _reason(entry):
        return entry if header is None else None
    headers = entry.get("response_headers")
    if not isinstance(headers, dict):
        return None
    listed = {name.lower(): value for name, value in headers.items()}
    return listed.get(header.lower())


def supported_as(manifest, operation, header=None):
    """How the SDK exposes an operation (its method) or a header (its field), or None if it doesn't."""
    value = entry_for(manifest, operation, header)
    if header is None:
        value = value.get("method") if isinstance(value, dict) and not _reason(value) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def backfill_targets(manifest, spec):
    """What a backfill run should add to an SDK.

    Every public operation and response header in the spec that the manifest
    leaves out or marks unsupported, except those a maintainer marked
    "backfill": false. Headers of an unsupported operation come with it, so
    they aren't listed on their own.
    """
    targets = []
    for op_id, op in sorted(spec_operations(spec).items()):
        entry = entry_for(manifest, op_id)
        if entry is None or _reason(entry):
            if not _excluded(entry):
                why = "not in spec-coverage.json" if entry is None else f"unsupported: {entry['unsupported']}"
                targets.append({"id": f"operation {op_id}", "operation": op_id, "path": op["path"], "status": why})
            continue
        for name in op["headers"]:
            value = entry_for(manifest, op_id, name)
            if (value is not None and not _reason(value)) or _excluded(value):
                continue
            why = "not in spec-coverage.json" if value is None else f"unsupported: {value['unsupported']}"
            targets.append({"id": f"header {op_id} {name}", "operation": op_id, "header": name, "path": op["path"],
                            "status": why})
    return targets


def summary(manifest):
    """Counts for the report: operations supported and unsupported."""
    entries = manifest.get("operations") if isinstance(manifest, dict) else None
    if not isinstance(entries, dict):
        return ""
    unsupported = sorted(k for k, v in entries.items() if _reason(v))
    text = f"{len(entries) - len(unsupported)} operations supported, {len(unsupported)} unsupported"
    return text + (f": {', '.join(unsupported)}" if unsupported else "")


def load_json(path, what):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise SystemExit(f"{what} not found: {path}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"{what} is not valid JSON: {path}: {e}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--manifest", default=MANIFEST)
    p.add_argument("--spec", required=True, help="the public spec as JSON")
    p.add_argument("--spec-tag", help="fail unless the manifest's spec_tag is this tag")
    args = p.parse_args(argv)
    manifest = load_json(args.manifest, "The coverage manifest")
    problems = check(manifest, load_json(args.spec, "The spec"), args.spec_tag)
    if problems:
        print(f"{args.manifest} doesn't match the spec ({len(problems)} problem(s)):")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print(f"{args.manifest} matches the spec at {manifest['spec_tag']}: {summary(manifest)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
