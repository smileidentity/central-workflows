"""Tests for scripts/spec_sync/spec_sync.py. Run: python3 -m unittest discover -s tests"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "spec_sync"))

import spec_sync as s

TEMPLATE = (ROOT / "scripts" / "spec_sync" / "prompt.md").read_text()
NOW = "2026-11-09 06:00 UTC"
RUN = "https://github.com/smileidentity/central-workflows/actions/runs/1"


def record(tag, previous, breaking=False, prs=(), summary="Added a field."):
    return {"tag": tag, "previous_tag": previous, "breaking": breaking, "lambda_prs": list(prs), "summary": summary}


def write_state(root, kind, *records):
    folder = Path(root) / "state" / kind
    folder.mkdir(parents=True, exist_ok=True)
    for r in records:
        (folder / f"{r['tag']}.json").write_text(json.dumps(r))


class PayloadTest(unittest.TestCase):
    def test_accepts_the_contract(self):
        r = s.validate_payload({"tag": "v3.1140.0", "previous_tag": "v3.1139.0", "summary": "x",
                                "breaking": "true", "lambda_prs": [5100, "5099", 5100]})
        self.assertEqual(r, {"tag": "v3.1140.0", "previous_tag": "v3.1139.0", "summary": "x",
                             "breaking": True, "lambda_prs": [5099, 5100]})

    def test_defaults(self):
        r = s.validate_payload({"tag": "v3.2.0", "previous_tag": "v3.1.9"})
        self.assertEqual((r["summary"], r["breaking"], r["lambda_prs"]), ("", False, []))

    def test_rejects_bad_payloads(self):
        bad = [
            None,
            {"tag": "main", "previous_tag": "v3.1.0"},
            {"tag": "v3.1.0", "previous_tag": "v3.1.0"},
            {"tag": "v3.1.0", "previous_tag": "v3.2.0"},
            {"tag": "v3.2.0", "previous_tag": "v3.1.0", "breaking": "yes"},
            {"tag": "v3.2.0", "previous_tag": "v3.1.0", "lambda_prs": "5100"},
            {"tag": "v3.2.0", "previous_tag": "v3.1.0", "lambda_prs": [True]},
            {"tag": "v3.2.0", "previous_tag": "v3.1.0", "lambda_prs": ["5100; rm -rf /"]},
            {"tag": "v3.2.0", "previous_tag": "v3.1.0", "lambda_prs": [0]},
            {"tag": "v3.2.0", "previous_tag": "v3.1.0", "summary": 5},
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(s.SpecSyncError):
                s.validate_payload(payload)

    def test_truncates_long_summaries(self):
        r = s.validate_payload({"tag": "v3.2.0", "previous_tag": "v3.1.0", "summary": "a" * 100_000})
        self.assertLess(len(r["summary"]), s.MAX_SUMMARY + 100)
        self.assertTrue(r["summary"].endswith("(Truncated by central-workflows.)"))

    def test_tags_sort_as_versions(self):
        self.assertEqual(s.latest_tag(["v3.999.0", "v3.1000.0", "junk", "v2.9999.9"]), "v3.1000.0")
        self.assertIsNone(s.latest_tag(["junk"]))


class StateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        patcher = mock.patch("builtins.print")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_record_then_process(self):
        event = {"client_payload": record("v3.10.0", "v3.9.0", prs=[1])}
        path = s.record_dispatch(event, self.dir, RUN, NOW)
        saved = json.loads(path.read_text())
        self.assertEqual((saved["tag"], saved["received_at"], saved["run_url"]), ("v3.10.0", NOW, RUN))
        self.assertEqual([r["tag"] for r in s.load_records(self.dir, "pending")], ["v3.10.0"])

        self.assertEqual(s.mark_processed(self.dir, ["v3.10.0", "v3.11.0"], NOW, RUN), ["v3.10.0"])
        self.assertEqual(s.load_records(self.dir, "pending"), [])
        self.assertEqual(s.load_records(self.dir, "processed")[0]["processed_by"], RUN)
        self.assertEqual(s.find_record(self.dir, "v3.10.0")["tag"], "v3.10.0")

        # A repeat of a processed release is ignored.
        self.assertIsNone(s.record_dispatch(event, self.dir, RUN, NOW))
        self.assertEqual(s.load_records(self.dir, "pending"), [])

    def test_records_load_in_version_order(self):
        write_state(self.dir, "pending", record("v3.100.0", "v3.99.0"), record("v3.99.0", "v3.98.0"))
        self.assertEqual([r["tag"] for r in s.load_records(self.dir, "pending")], ["v3.99.0", "v3.100.0"])
        self.assertEqual(s.load_records(self.dir, "missing"), [])

    def test_record_command(self):
        event = Path(self.dir) / "event.json"
        event.write_text(json.dumps({"client_payload": {"tag": "nope"}}))
        with mock.patch("builtins.print"):
            self.assertEqual(s.main(["record", "--event", str(event), "--state-dir", self.dir, "--run-url", RUN]), 1)
        event.write_text(json.dumps({"client_payload": record("v3.2.0", "v3.1.0")}))
        with mock.patch("builtins.print"):
            self.assertEqual(s.main(["record", "--event", str(event), "--state-dir", self.dir, "--run-url", RUN]), 0)
        self.assertTrue((Path(self.dir) / "state" / "pending" / "v3.2.0.json").exists())


class PlanTest(unittest.TestCase):
    pending = (
        record("v3.10.0", "v3.9.0", prs=[1]),
        record("v3.11.0", "v3.10.0", breaking=True, prs=[2]),
        record("v3.12.0", "v3.11.0", prs=[3, 1]),
    )

    def test_daily_run(self):
        p = s.plan(self.pending, latest_api_tag="v3.12.0")
        self.assertTrue(p["has_work"])
        self.assertTrue(p["mark_processed"])
        self.assertEqual(p["tags"], ["v3.10.0", "v3.11.0", "v3.12.0"])
        rolling, breaking = p["units"]
        self.assertEqual((rolling["branch"], rolling["base_tag"], rolling["spec_tag"]),
                         ("spec-sync/rolling", "v3.9.0", "v3.12.0"))
        self.assertEqual((rolling["tags"], rolling["lambda_prs"]), (["v3.10.0", "v3.12.0"], [1, 3]))
        self.assertEqual((breaking["branch"], breaking["base_tag"], breaking["spec_tag"], breaking["kind"]),
                         ("spec-sync/breaking-v3.11.0", "v3.10.0", "v3.11.0", "breaking"))
        self.assertEqual(len(p["matrix"]["include"]), 2 * len(s.ALL_REPOS))
        self.assertEqual(p["matrix"]["include"][0]["repo"], "smileid-sdk-go")

    def test_nothing_pending(self):
        p = s.plan([], latest_api_tag="v3.12.0")
        self.assertFalse(p["has_work"])
        self.assertFalse(p["mark_processed"])
        self.assertEqual(p["reason"], "Nothing is pending.")

    def test_hourly_run_does_no_sync(self):
        p = s.plan(self.pending, sync_due=False)
        self.assertFalse(p["has_work"])
        self.assertEqual(p["matrix"], {"include": []})

    def test_some_repos_leave_records_pending(self):
        p = s.plan(self.pending, repos="go, smileid-sdk-fixtures,go")
        self.assertEqual(sorted({i["repo"] for i in p["matrix"]["include"]}),
                         ["smileid-sdk-fixtures", "smileid-sdk-go"])
        self.assertFalse(p["mark_processed"])
        with self.assertRaises(s.SpecSyncError):
            s.plan(self.pending, repos="cobol")

    def test_from_tag_backlog(self):
        p = s.plan(self.pending, from_tag="v3.10.0", latest_api_tag="v3.15.0")
        rolling = p["units"][0]
        self.assertEqual((rolling["base_tag"], rolling["spec_tag"], rolling["tags"]),
                         ("v3.10.0", "v3.15.0", ["v3.12.0"]))
        # v3.10.0 is at or before from_tag, which the SDKs already match, so it is done too.
        self.assertEqual(p["tags"], ["v3.10.0", "v3.11.0", "v3.12.0"])
        self.assertEqual([u["kind"] for u in p["units"]], ["rolling", "breaking"])

    def test_from_tag_with_nothing_pending(self):
        p = s.plan([], from_tag="v3.1.0", latest_api_tag="v3.15.0")
        self.assertEqual(p["units"][0]["tags"], [])
        self.assertFalse(p["mark_processed"])
        self.assertFalse(s.plan([], from_tag="v3.15.0", latest_api_tag="v3.15.0")["has_work"])

    def test_from_tag_errors(self):
        for kwargs in ({"from_tag": "v3.16.0", "latest_api_tag": "v3.15.0"},
                       {"from_tag": "v3.1.0", "latest_api_tag": None},
                       {"from_tag": "latest", "latest_api_tag": "v3.15.0"},
                       {"scope": "AML", "latest_api_tag": "v3.15.0"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(s.SpecSyncError):
                s.plan(self.pending, **kwargs)

    def test_scoped_run(self):
        p = s.plan(self.pending, from_tag="v3.1.0", scope="AML operations!", latest_api_tag="v3.15.0")
        (u,) = p["units"]
        self.assertEqual((u["kind"], u["branch"], u["scope"]), ("scoped", "spec-sync/aml-operations", "AML operations!"))
        self.assertEqual(p["tags"], [])
        self.assertFalse(p["mark_processed"])
        with self.assertRaises(s.SpecSyncError):
            s.slug("!!!")

    def test_plan_command_writes_outputs(self):
        state = tempfile.mkdtemp()
        write_state(state, "pending", *self.pending)
        tags = Path(state) / "tags.txt"
        tags.write_text("v3.12.0\nv3.9.0\nv3.10.0\nv3.11.0\n")
        out = Path(state) / "out.txt"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(out)}), mock.patch("builtins.print"):
            self.assertEqual(s.main(["plan", "--state-dir", state, "--api-tags-file", str(tags)]), 0)
        text = out.read_text()
        self.assertIn("has_work<<SPEC_SYNC_EOF\ntrue\n", text)
        self.assertIn('tags<<SPEC_SYNC_EOF\n["v3.10.0","v3.11.0","v3.12.0"]\n', text)
        matrix = json.loads(text.split("matrix<<SPEC_SYNC_EOF\n")[1].split("\n")[0])
        self.assertEqual(len(matrix["include"]), 16)


    def test_lambda_tags_map_to_api_reference_tags(self):
        # api-reference only has tags for releases that changed the public spec.
        api_tags = ["v3.8.0", "v3.11.0", "junk"]
        p = s.plan(self.pending, latest_api_tag="v3.11.0", api_tags=api_tags)
        rolling, breaking = p["units"]
        self.assertEqual((rolling["base_tag"], rolling["spec_tag"]), ("v3.8.0", "v3.11.0"))
        self.assertEqual((breaking["base_tag"], breaking["spec_tag"]), ("v3.8.0", "v3.11.0"))
        self.assertEqual(p["tags"], ["v3.10.0", "v3.11.0", "v3.12.0"])

    def test_unit_with_no_public_spec_change_is_skipped(self):
        p = s.plan([record("v3.12.0", "v3.11.0")], latest_api_tag="v3.10.0", api_tags=["v3.10.0"])
        self.assertFalse(p["has_work"])
        self.assertEqual(p["tags"], ["v3.12.0"])

    def test_tag_older_than_every_api_reference_tag(self):
        with self.assertRaises(s.SpecSyncError):
            s.plan(self.pending, latest_api_tag="v3.20.0", api_tags=["v3.20.0"])


class BodyTest(unittest.TestCase):
    def test_marker_round_trip(self):
        value = {"tags": ["v3.1.0"], "note": "a -- b --> c"}
        body = s.write_marker(s.PR_MARKER, value) + "\n\n<!-- spec-sync-state {\"tags\": [\"v9.9.9\"]} -->"
        self.assertNotIn("-->", body.split("\n")[0][:-3])
        self.assertEqual(s.read_marker(body, s.PR_MARKER), value)
        self.assertEqual(s.read_marker("no marker", s.PR_MARKER), {})
        self.assertEqual(s.read_marker("<!-- spec-sync-state {bad} -->", s.PR_MARKER), {})
        self.assertEqual(s.read_marker(None, s.PR_MARKER), {})

    def body(self, records, result=None, ready_ok=True, kind="rolling", summary="Added `sandbox_status`."):
        u = {"kind": kind, "scope": "AML", "branch": "spec-sync/rolling"}
        return s.render_pr_body(repo="smileid-sdk-go", u=u, tags=[r["tag"] for r in records], spec_tag="v3.12.0",
                                lambda_prs=[5100], records=records, summary=summary,
                                result=result or {"checks": [{"name": "go test", "passed": True}],
                                                  "changed_line_coverage": 100},
                                ready_ok=ready_ok, run_url=RUN, now=NOW)

    def test_body(self):
        body = self.body([record("v3.12.0", "v3.11.0", summary="### GET /v3/address\n- added")])
        self.assertEqual(s.read_marker(body, s.PR_MARKER)["spec_tag"], "v3.12.0")
        for text in ("api-reference/blob/v3.12.0/specs/v3/openapi.yaml", "lambda#5100", "| go test | passed |",
                     "Changed-line coverage: 100%.", "Added `sandbox_status`.", "GET /v3/address",
                     "marks this pull request ready", RUN):
            self.assertIn(text, body)
        self.assertNotIn("## Gaps", body)

    def test_body_with_gaps_and_kinds(self):
        body = self.body([], result={"gaps": ["Coverage is 97%."], "checks": []}, ready_ok=False, kind="breaking",
                         summary="")
        for text in ("stays a draft", "## Gaps\n\n- Coverage is 97%.", "breaking changes",
                     "Claude did not report any checks.", "Claude did not write a summary."):
            self.assertIn(text, body)
        self.assertIn("It covers only this part of the spec: AML.", self.body([], kind="scoped"))
        self.assertEqual(s.pr_title({"kind": "scoped", "scope": "AML"}, "v3.1.0"), "Sync AML with the API spec at v3.1.0")
        self.assertTrue(s.pr_title({"kind": "breaking"}, "v3.1.0").startswith("Breaking:"))

    def test_body_stays_under_the_limit(self):
        records = [record(f"v3.{i}.0", f"v3.{i - 1}.0", summary="x" * 50_000) for i in range(1, 5)]
        body = self.body(records, summary="y" * 40_000)
        self.assertLess(len(body), 65_536)
        self.assertIn("Further changes are in the", body)
        self.assertIn("(Summary truncated.)", body)


class GuardTest(unittest.TestCase):
    diff = """diff --git a/lib/a.py b/lib/a.py
--- a/lib/a.py
+++ b/lib/a.py
@@ -1 +1,4 @@
+x = 1  # pragma: no cover
+if y:  # c8 ignore next
+<<<<<<< HEAD
+ok = 2
--- a/old.txt
+++ /dev/null
"""

    def test_finds_problems(self):
        problems = s.guard_problems([".github/workflows/ci.yml", "lib/a.py"], self.diff)
        self.assertEqual(len(problems), 4)
        self.assertIn(".github/workflows/ci.yml", problems[0])
        self.assertTrue(any("conflict" in p for p in problems))

    def test_clean_diff(self):
        clean = "+++ b/src/lib.rs\n+fn coverage_report() {}\n+let a = \"=======\";\n"
        self.assertEqual(s.guard_problems(["src/lib.rs"], clean), [])
        self.assertTrue(s.guard_problems(["src/lib.rs"], "+++ b/src/lib.rs\n+#[coverage(off)]\n"))


class StatusTest(unittest.TestCase):
    def test_checks_state(self):
        ok = {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}
        skipped = {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED"}
        running = {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""}
        failed = {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}
        ctx_ok = {"__typename": "StatusContext", "state": "SUCCESS"}
        ctx_pending = {"__typename": "StatusContext", "state": "PENDING"}
        ctx_error = {"__typename": "StatusContext", "state": "ERROR"}
        self.assertEqual(s.checks_state([]), "none")
        self.assertEqual(s.checks_state(None), "none")
        self.assertEqual(s.checks_state([ok, skipped, ctx_ok]), "success")
        self.assertEqual(s.checks_state([ok, running]), "pending")
        self.assertEqual(s.checks_state([ctx_pending, ok]), "pending")
        self.assertEqual(s.checks_state([running, failed]), "failure")
        self.assertEqual(s.checks_state([ctx_error]), "failure")

    def test_promote(self):
        green = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]
        listed = [
            {"number": 3, "url": "u3", "isDraft": True, "headRefName": "spec-sync/rolling", "statusCheckRollup": green,
             "body": s.write_marker(s.PR_MARKER, {"ready_ok": True, "spec_tag": "v3.12.0"})},
            {"number": 4, "url": "u4", "isDraft": True, "headRefName": "spec-sync/breaking-v3.11.0",
             "statusCheckRollup": green, "body": s.write_marker(s.PR_MARKER, {"ready_ok": False})},
            {"number": 5, "url": "u5", "isDraft": True, "headRefName": "spec-sync/aml", "statusCheckRollup": [],
             "body": s.write_marker(s.PR_MARKER, {"ready_ok": True})},
        ]
        calls = []

        def fake_gh(args, token, **kwargs):
            calls.append(args)
            return json.dumps(listed) if args[:2] == ["pr", "list"] else ""

        with mock.patch.object(s, "gh", fake_gh), mock.patch("builtins.print"):
            found = s.promote_prs(["smileid-sdk-go"], "t")
        self.assertEqual([c[:3] for c in calls if c[1] == "ready"], [["pr", "ready", "3"]])
        prs = found["smileid-sdk-go"]
        self.assertEqual([(p["number"], p["draft"], p["checks"]) for p in prs],
                         [(3, False, "success"), (4, True, "success"), (5, True, "none")])

    def test_last_runs(self):
        previous = {"smileid-sdk-go": {"result": "updated", "run_url": "old", "at": "then"},
                    "smileid-sdk-php": {"result": "failed", "run_url": "old", "at": "then"}}
        results = [{"repo": "smileid-sdk-java", "status": "updated"}, {"repo": "smileid-sdk-java", "status": "blocked"},
                   {"repo": "smileid-sdk-js", "status": "no_change"}, {"repo": "smileid-sdk-ruby", "status": "failed"},
                   {"repo": "smileid-sdk-ruby", "status": "updated"}, {"repo": "smileid-sdk-rust", "status": "updated"}]
        repos = ["smileid-sdk-java", "smileid-sdk-js", "smileid-sdk-ruby", "smileid-sdk-rust", "smileid-sdk-php"]
        runs = s.update_last_runs(previous, results, repos, RUN, NOW, "failure")
        self.assertEqual({k: v["result"] for k, v in runs.items()},
                         {"smileid-sdk-go": "updated", "smileid-sdk-java": "blocked", "smileid-sdk-js": "no change",
                          "smileid-sdk-ruby": "failed", "smileid-sdk-rust": "updated", "smileid-sdk-php": "failed"})
        self.assertEqual(s.update_last_runs({}, [], ["smileid-sdk-go"], RUN, NOW, "cancelled")["smileid-sdk-go"]["result"],
                         "cancelled")

    def test_issue_body(self):
        prs = {"smileid-sdk-go": [{"number": 3, "url": "u3", "draft": False, "checks": "success",
                                   "branch": "spec-sync/rolling", "spec_tag": "v3.12.0"},
                                  {"number": 4, "url": "u4", "draft": True, "checks": "failure",
                                   "branch": "spec-sync/breaking-v3.11.0", "spec_tag": "v3.11.0"}]}
        runs = {"smileid-sdk-go": {"result": "updated", "run_url": RUN, "at": NOW}}
        body = s.render_status_issue(prs=prs, runs=runs, pending=["v3.13.0"], last_processed="v3.12.0",
                                     run_url=RUN, now=NOW)
        self.assertEqual(s.read_marker(body, s.ISSUE_MARKER), {"runs": runs})
        self.assertIn("| `smileid-sdk-go` | [#3](u3) ready, checks green<br>[#4](u4) draft, checks failing | `v3.12.0` | "
                      f"updated, [{NOW}]({RUN}) |", body)
        self.assertIn("| `smileid-sdk-php` | none | `v3.12.0` | not run yet |", body)
        self.assertIn("Pending lambda releases: `v3.13.0`.", body)
        empty = s.render_status_issue(prs={}, runs={}, pending=[], last_processed="", run_url=RUN, now=NOW)
        self.assertIn("| `smileid-sdk-go` | none | unknown | not run yet |", empty)

    def test_status_command(self):
        state = tempfile.mkdtemp()
        write_state(state, "pending", record("v3.13.0", "v3.12.0"))
        write_state(state, "processed", record("v3.12.0", "v3.11.0"))
        results = Path(state) / "results" / "spec-sync-result-0"
        results.mkdir(parents=True)
        (results / "job-result.json").write_text(json.dumps({"repo": "smileid-sdk-go", "status": "updated"}))
        calls = []

        def fake_gh(args, token, check=True, **kwargs):
            calls.append((args, token))
            if args[:2] == ["issue", "list"]:
                return json.dumps([{"number": 8, "title": s.STATUS_TITLE,
                                    "body": s.write_marker(s.ISSUE_MARKER, {"runs": {"smileid-sdk-php": {
                                        "result": "failed", "run_url": "old", "at": "then"}}})}])
            return "[]" if args[:2] == ["pr", "list"] else ""

        env = {"APP_TOKEN": "app", "ISSUE_TOKEN": "issue", "GITHUB_REPOSITORY": "o/r"}
        with mock.patch.object(s, "gh", fake_gh), mock.patch.dict(os.environ, env), mock.patch("builtins.print"):
            self.assertEqual(s.main(["status", "--state-dir", state, "--results-dir", str(results.parent),
                                     "--planned-repos", '["smileid-sdk-go"]', "--sync-result", "success",
                                     "--run-url", RUN, "--body-file", str(Path(state) / "b.md")]), 0)
        self.assertEqual(len([c for c in calls if c[0][:2] == ["pr", "list"] and c[1] == "app"]), len(s.ALL_REPOS))
        body = (Path(state) / "b.md").read_text()
        runs = s.read_marker(body, s.ISSUE_MARKER)["runs"]
        self.assertEqual({k: v["result"] for k, v in runs.items()}, {"smileid-sdk-go": "updated", "smileid-sdk-php": "failed"})
        self.assertIn("Pending lambda releases: `v3.13.0`. Last processed: `v3.12.0`.", body)
        self.assertIn((["issue", "edit", "8", "--repo", "o/r", "--body-file", str(Path(state) / "b.md")], "issue"), calls)

    def test_status_issue_create_and_edit(self):
        calls = []

        def fake_gh(args, token, check=True, **kwargs):
            calls.append(args)
            if args[:2] == ["issue", "list"]:
                return json.dumps([{"number": 7, "title": s.STATUS_TITLE, "body": "made by hand"},
                                   {"number": 8, "title": s.STATUS_TITLE, "body": "<!-- spec-sync-status {} -->"}])
            if args[:2] == ["issue", "create"]:
                return "https://github.com/o/r/issues/9\n"
            if args[:2] == ["issue", "pin"]:
                return (1, "", "not allowed")
            return ""

        body_file = Path(tempfile.mkdtemp()) / "b.md"
        with mock.patch.object(s, "gh", fake_gh), mock.patch("builtins.print"):
            issue = s.find_status_issue("o/r", "t")
            self.assertEqual(issue["number"], 8)
            self.assertEqual(s.write_status_issue("o/r", "t", "body", issue, body_file), 8)
            self.assertEqual(s.write_status_issue("o/r", "t", "body", None, body_file), 9)
        self.assertEqual([c[:2] for c in calls],
                         [["issue", "list"], ["issue", "edit"], ["issue", "create"], ["issue", "pin"]])


def sh(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


class FakeGitHub:
    """Stands in for gh: remembers PRs and records every call."""

    def __init__(self, open_pr=None):
        self.calls = []
        self.open_pr = open_pr

    def __call__(self, args, token, check=True, **kwargs):
        self.calls.append(args)
        if args[:2] == ["repo", "view"]:
            return "main\n"
        if args[:2] == ["pr", "list"]:
            return json.dumps([self.open_pr] if self.open_pr else [])
        if args[:2] == ["pr", "view"]:
            if args[2] == "404":
                return (1, "", "not found")
            return (0, json.dumps({"number": int(args[2]), "title": "Add phone", "body": "Secret details"}), "")
        if args[:2] == ["pr", "create"]:
            self.body = Path(args[args.index("--body-file") + 1]).read_text()
            return "https://github.com/smileidentity/smileid-sdk-go/pull/12\n"
        if args[:2] == ["pr", "edit"]:
            self.body = Path(args[args.index("--body-file") + 1]).read_text()
        return ""


class PrepareAndPublishTest(unittest.TestCase):
    """Runs prepare and publish against a local bare repository with a fake gh."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote" / "smileidentity" / "smileid-sdk-go.git"
        sh(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)])
        seed = self.tmp / "seed"
        sh(["git", "init", "-q", "-b", "main", str(seed)])
        (seed / ".github").mkdir()
        (seed / ".github" / "ci.yml").write_text("on: push\n")
        (seed / "client.go").write_text("package smileid\n")
        sh(["git", "add", "-A"], seed)
        sh(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "seed"], seed)
        sh(["git", "push", "-q", str(self.remote), "main"], seed)
        self.work = self.tmp / "work"
        sh(["git", "clone", "-q", str(self.remote), str(self.work)])
        self.ctx = self.tmp / "ctx"
        self.out = self.tmp / "out"
        self.state = self.tmp / "state"
        write_state(self.state, "pending", record("v3.12.0", "v3.11.0", prs=[5100, 404], summary="Added phone."))
        write_state(self.state, "processed", record("v3.10.0", "v3.9.0", summary="Added address."))
        self.unit = {"repo": "smileid-sdk-go", "kind": "rolling", "branch": "spec-sync/rolling",
                     "base_tag": "v3.11.0", "spec_tag": "v3.12.0", "tags": ["v3.12.0"],
                     "lambda_prs": [404, 5100], "scope": ""}
        patcher = mock.patch.object(s, "GIT_BASE", f"file://{self.tmp / 'remote'}")
        patcher.start()
        self.addCleanup(patcher.stop)

    def prepare(self, fake):
        with mock.patch.object(s, "gh", fake):
            self.prepared = s.prepare(u=self.unit, repo="smileid-sdk-go", workdir=self.work, ctx=self.ctx,
                                      state_dir=self.state, template=TEMPLATE, token="", read_token="r")
        return self.prepared

    def package(self):
        self.runs = getattr(self, "runs", 0) + 1
        self.bundle = self.tmp / f"bundle{self.runs}"
        return s.package(u=self.unit, workdir=self.work, ctx=self.ctx, base=self.prepared["base"],
                         default_sha=self.prepared["default_sha"], dest=self.bundle)

    def publish(self, fake, package=True):
        """Package the sync job's tree, then publish it from a fresh repository, as the publish job does."""
        if package:
            self.package()
        fresh = self.tmp / f"publish{self.runs}"
        sh(["git", "init", "-q", str(fresh)])
        with mock.patch.object(s, "gh", fake), mock.patch("builtins.print"):
            return s.publish(u=self.unit, workdir=fresh, bundle=self.bundle, out=self.out, state_dir=self.state,
                             write_token="", bot_name="bot[bot]", bot_email="1+bot[bot]@users.noreply.github.com",
                             run_url=RUN, now=NOW)

    def claude(self, status="changed", files=None, gaps=()):
        for name, text in (files if files is not None else {"phone.go": "package smileid\n"}).items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (self.ctx / "out" / "result.json").write_text(json.dumps(
            {"status": status, "checks": [{"name": "go test", "passed": True}], "changed_line_coverage": 100,
             "gaps": list(gaps)}))
        (self.ctx / "out" / "summary.md").write_text("Added `VerifyPhone`.")

    def remote_files(self, branch):
        return sh(["git", "--git-dir", str(self.remote), "ls-tree", "--name-only", branch]).split()

    def test_opens_a_draft_pr(self):
        fake = FakeGitHub()
        self.prepare(fake)
        task = (self.ctx / "TASK.md").read_text()
        self.assertNotIn("{{", task)
        for text in ("smileidentity/smileid-sdk-go", f"{self.ctx}/spec/v3.12.0/openapi.yaml",
                     "rolling spec-sync pull request", "There is no open pull request yet.", f"{self.ctx}/fixtures"):
            self.assertIn(text, task)
        self.assertIn("Added phone.", (self.ctx / "summaries.md").read_text())
        lambda_text = (self.ctx / "lambda-prs.md").read_text()
        self.assertIn("lambda#5100: Add phone", lambda_text)
        self.assertIn("lambda#404\n\nNot available: not found", lambda_text)

        self.claude()
        outcome = self.publish(fake)
        self.assertEqual(outcome["status"], "updated")
        self.assertEqual(outcome["pr_url"], "https://github.com/smileidentity/smileid-sdk-go/pull/12")
        self.assertIn("phone.go", self.remote_files("spec-sync/rolling"))
        create = next(c for c in fake.calls if c[:2] == ["pr", "create"])
        self.assertIn("--draft", create)
        self.assertEqual([create[i + 1] for i, a in enumerate(create) if a == "--label"], ["spec-sync"])
        self.assertIn("Sync with the API spec at v3.12.0", create)
        self.assertTrue(s.read_marker(fake.body, s.PR_MARKER)["ready_ok"])
        self.assertNotIn("Secret details", fake.body)
        author = sh(["git", "--git-dir", str(self.remote), "log", "-1", "--format=%an", "spec-sync/rolling"]).strip()
        self.assertEqual(author, "bot[bot]")
        saved = json.loads((self.out / "job-result.json").read_text())
        self.assertEqual(saved["status"], "updated")

    def test_updates_the_open_pr(self):
        # An earlier run left the rolling branch, and main has moved on since.
        sh(["git", "checkout", "-qb", "spec-sync/rolling"], self.work)
        (self.work / "address.go").write_text("package smileid\n")
        sh(["git", "add", "-A"], self.work)
        sh(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "earlier"], self.work)
        sh(["git", "push", "-q", "origin", "spec-sync/rolling"], self.work)
        sh(["git", "checkout", "-q", "main"], self.work)
        (self.work / "readme.md").write_text("new\n")
        sh(["git", "add", "-A"], self.work)
        sh(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "main moves"], self.work)
        sh(["git", "push", "-q", "origin", "main"], self.work)

        marker = s.write_marker(s.PR_MARKER, {"tags": ["v3.10.0"], "spec_tag": "v3.10.0", "lambda_prs": [1]})
        fake = FakeGitHub({"number": 12, "url": "u12", "isDraft": False, "body": marker})
        state = self.prepare(fake)
        self.assertFalse(state["conflict"])
        self.assertIn("already covers v3.10.0", (self.ctx / "TASK.md").read_text())
        self.claude(status="blocked", gaps=["Coverage is 98%."])
        outcome = self.publish(fake)
        self.assertEqual(outcome["status"], "blocked")
        self.assertEqual(sorted(self.remote_files("spec-sync/rolling")),
                         [".github", "address.go", "client.go", "phone.go", "readme.md"])
        self.assertFalse(any(c[:2] == ["pr", "create"] for c in fake.calls))
        self.assertIn(["pr", "ready", "12", "--repo", "smileidentity/smileid-sdk-go", "--undo"], fake.calls)
        marker = s.read_marker(fake.body, s.PR_MARKER)
        self.assertEqual((marker["tags"], marker["spec_tag"], marker["lambda_prs"], marker["ready_ok"]),
                         (["v3.10.0", "v3.12.0"], "v3.12.0", [1, 404, 5100], False))
        self.assertIn("Added address.", fake.body)
        self.assertIn("Coverage is 98%.", fake.body)

    def test_merge_conflict_is_left_for_claude(self):
        sh(["git", "checkout", "-qb", "spec-sync/rolling"], self.work)
        (self.work / "client.go").write_text("package smileid // rolling\n")
        sh(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "rolling"], self.work)
        sh(["git", "push", "-q", "origin", "spec-sync/rolling"], self.work)
        sh(["git", "checkout", "-q", "main"], self.work)
        (self.work / "client.go").write_text("package smileid // main\n")
        sh(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "main"], self.work)
        sh(["git", "push", "-q", "origin", "main"], self.work)

        fake = FakeGitHub({"number": 12, "url": "u12", "isDraft": True, "body": ""})
        self.assertTrue(self.prepare(fake)["conflict"])
        self.assertIn("Resolve every conflict marker", (self.ctx / "TASK.md").read_text())
        # Claude leaves a conflict marker behind, so nothing is pushed.
        self.claude(files={})
        with self.assertRaises(s.SpecSyncError):
            self.publish(fake)
        # Claude resolves it.
        self.claude(files={"client.go": "package smileid // both\n"})
        self.assertEqual(self.publish(fake)["status"], "updated")
        log = sh(["git", "--git-dir", str(self.remote), "log", "-1", "--format=%P", "spec-sync/rolling"]).split()
        self.assertEqual(len(log), 2, "the merge commit keeps both parents")

    def test_refuses_protected_changes(self):
        fake = FakeGitHub()
        self.prepare(fake)
        self.claude(files={".github/ci.yml": "on: pull_request\n"})
        with self.assertRaises(s.SpecSyncError), mock.patch("builtins.print"):
            self.publish(fake)
        self.assertEqual(json.loads((self.out / "job-result.json").read_text())["status"], "failed")
        self.assertNotIn("spec-sync/rolling", sh(["git", "--git-dir", str(self.remote), "branch"]))

    def test_no_change_and_missing_result(self):
        fake = FakeGitHub()
        self.prepare(fake)
        with self.assertRaises(s.SpecSyncError):
            self.publish(fake)
        self.claude(status="no_change", files={})
        self.assertEqual(self.publish(fake)["status"], "no_change")
        self.assertFalse(any(c[:2] == ["pr", "create"] for c in fake.calls))

    def test_breaking_pr_gets_its_label(self):
        self.unit.update(kind="breaking", branch="spec-sync/breaking-v3.12.0")
        fake = FakeGitHub()
        self.prepare(fake)
        self.assertIn("for breaking changes", (self.ctx / "TASK.md").read_text())
        self.claude()
        self.publish(fake)
        create = next(c for c in fake.calls if c[:2] == ["pr", "create"])
        self.assertEqual([create[i + 1] for i, a in enumerate(create) if a == "--label"], ["spec-sync", "breaking"])
        self.assertIn("Breaking: sync with the API spec at v3.12.0", create)

    def test_fixtures_task(self):
        self.unit.update(repo="smileid-sdk-fixtures")
        os.symlink(self.remote, self.remote.parent / "smileid-sdk-fixtures.git")
        with mock.patch.object(s, "gh", FakeGitHub()):
            s.prepare(u=self.unit, repo="smileid-sdk-fixtures", workdir=self.work, ctx=self.ctx,
                      state_dir=self.state, template=TEMPLATE, token="", read_token="")
        task = (self.ctx / "TASK.md").read_text()
        self.assertIn("scripts/build_cases.py", task)
        self.assertIn("no token could read lambda", (self.ctx / "lambda-prs.md").read_text())

    def test_publish_without_a_bundle(self):
        self.runs, self.bundle = 1, self.tmp / "no-bundle"
        with self.assertRaises(s.SpecSyncError):
            self.publish(FakeGitHub(), package=False)
        self.assertEqual(json.loads((self.out / "job-result.json").read_text())["status"], "failed")

    def test_refuses_a_bundle_for_another_job(self):
        fake = FakeGitHub()
        self.prepare(fake)
        self.claude()
        self.package()
        meta = json.loads((self.bundle / "meta.json").read_text())
        (self.bundle / "meta.json").write_text(json.dumps({**meta, "branch": "main"}))
        with self.assertRaises(s.SpecSyncError):
            self.publish(fake, package=False)
        self.assertNotIn("spec-sync/rolling", sh(["git", "--git-dir", str(self.remote), "branch"]))

    def test_refuses_a_job_that_is_not_a_spec_sync_branch(self):
        self.unit.update(branch="main")
        self.runs, self.bundle = 1, self.tmp / "no-bundle"
        with self.assertRaises(s.SpecSyncError):
            self.publish(FakeGitHub(), package=False)

    def test_refuses_when_the_branch_moved(self):
        sh(["git", "checkout", "-qb", "spec-sync/rolling"], self.work)
        sh(["git", "push", "-q", "origin", "spec-sync/rolling"], self.work)
        sh(["git", "checkout", "-q", "main"], self.work)
        fake = FakeGitHub({"number": 12, "url": "u12", "isDraft": True, "body": ""})
        self.prepare(fake)
        self.claude()
        self.package()
        # Someone pushes to the PR branch while Claude works.
        other = self.tmp / "other"
        sh(["git", "clone", "-q", "-b", "spec-sync/rolling", str(self.remote), str(other)])
        (other / "manual.go").write_text("package smileid\n")
        sh(["git", "add", "-A"], other)
        sh(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "manual"], other)
        sh(["git", "push", "-q", "origin", "spec-sync/rolling"], other)
        with self.assertRaisesRegex(s.SpecSyncError, "changed while Claude worked"):
            self.publish(fake, package=False)
        self.assertNotIn("phone.go", self.remote_files("spec-sync/rolling"))

    def test_package_runs_no_hooks_or_fsmonitor(self):
        fake = FakeGitHub()
        self.prepare(fake)
        self.claude()
        planted = self.tmp / "planted"
        hook = self.tmp / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch {planted}\n")
        hook.chmod(0o755)
        sh(["git", "config", "core.fsmonitor", str(hook)], self.work)
        hooks = self.work / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in ("pre-commit", "post-index-change", "reference-transaction"):
            (hooks / name).write_text(hook.read_text())
            (hooks / name).chmod(0o755)
        self.package()
        self.assertFalse(planted.exists(), "a hook or fsmonitor from the working tree ran")
        self.assertIn("phone.go", (self.bundle / "change.patch").read_text())


class TemplateTest(unittest.TestCase):
    def test_unknown_placeholder(self):
        with self.assertRaises(s.SpecSyncError):
            s.render_task("{{nope}}", {})

    def test_git_auth_env(self):
        self.assertEqual(s.git_auth_env(""), {})
        with mock.patch("builtins.print"):
            env = s.git_auth_env("secret")
        self.assertNotIn("secret", json.dumps(env))
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")

    def test_run(self):
        self.assertEqual(s.run(["echo", "hi"]), "hi\n")
        self.assertEqual(s.run(["false"], check=False)[0], 1)
        with self.assertRaises(s.SpecSyncError):
            s.run(["false"])


if __name__ == "__main__":
    unittest.main()
