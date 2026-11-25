"""Tests for scripts/release_train/release_train.py. Run: python3 -m unittest discover -s tests"""

import datetime as dt
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "release_train"))

import release_train as t

WORKFLOW = (ROOT / ".github" / "workflows" / "sdk-release-train.yml").read_text()
NOW = dt.datetime(2026, 11, 25, 7, 0, tzinfo=dt.timezone.utc)  # a Wednesday
RUN = "https://github.com/smileidentity/central-workflows/actions/runs/1"


def changelog(*sections):
    body = "\n\n".join(f"### {name}\n\n- {name} entry." for name in sections)
    return f"# Changelog\n\n## [Unreleased]\n\n{body}\n\n## [13.2.0] - 2026-11-24\n\n### Added\n\n- Old.\n"


def sdk(version="13.2.0", *sections, breaking=()):
    return {"tags": ["v13.0.0", f"v{version}"], "changelog": changelog(*sections), "breaking_prs": list(breaking)}


DAILY = {repo: "daily" for repo in t.SDK_REPOS}


class VersionTest(unittest.TestCase):
    def test_bump(self):
        self.assertEqual(t.bump("13.2.0", "patch"), "13.2.1")
        self.assertEqual(t.bump("13.2.5", "minor"), "13.3.0")
        self.assertEqual(t.bump("13.2.5", "major"), "14.0.0")

    def test_bad_versions(self):
        for bad in ("v13.2.0", "13.2", "13.02.0", "13.2.0-rc.1", ""):
            with self.assertRaises(t.TrainError, msg=bad):
                t.parse_version(bad)

    def test_latest_release_ignores_other_tags(self):
        self.assertEqual(t.latest_release(["v13.10.0", "v13.9.0", "v14.0.0-rc.1", "latest", "13.20.0"]), "13.10.0")
        self.assertIsNone(t.latest_release(["latest"]))


class ChangelogTest(unittest.TestCase):
    def test_unreleased_stops_at_the_next_release(self):
        self.assertEqual(t.unreleased(changelog("Fixed")), "### Fixed\n\n- Fixed entry.")

    def test_empty_unreleased(self):
        self.assertEqual(t.unreleased(changelog()), "")
        self.assertEqual(t.unreleased("# Changelog\n"), "")

    def test_needed_part(self):
        self.assertIsNone(t.needed_part("", []))
        self.assertEqual(t.needed_part("### Fixed\n\n- x", []), "patch")
        self.assertEqual(t.needed_part("### Changed\n\n- x", []), "patch")
        self.assertEqual(t.needed_part("### Fixed\n\n- x\n\n### Added\n\n- y", []), "minor")
        self.assertEqual(t.needed_part("### Deprecated\n\n- x", []), "minor")
        self.assertEqual(t.needed_part("### Fixed\n\n- x", [{"number": 1}]), "major")


class CadenceTest(unittest.TestCase):
    def test_defaults_and_overrides(self):
        cadences = t.parse_cadence("daily", '{"rust": "weekly"}')
        self.assertEqual(cadences["smileid-sdk-rust"], "weekly")
        self.assertEqual(cadences["smileid-sdk-go"], "daily")

    def test_bad_settings(self):
        for default, overrides in (("hourly", ""), ("daily", "[]"), ("daily", "{bad"), ("daily", '{"kotlin": "daily"}'),
                                   ("daily", '{"go": "sometimes"}')):
            with self.assertRaises(t.TrainError, msg=(default, overrides)):
                t.parse_cadence(default, overrides)

    def test_is_due(self):
        wednesday, monday, later_monday = dt.date(2026, 11, 25), dt.date(2026, 12, 7), dt.date(2026, 12, 14)
        self.assertTrue(t.is_due("daily", today=wednesday, daily_run=True))
        self.assertFalse(t.is_due("daily", today=wednesday, daily_run=False))
        self.assertTrue(t.is_due("on-merge", today=wednesday, daily_run=False))
        self.assertFalse(t.is_due("weekly", today=wednesday, daily_run=True))
        self.assertTrue(t.is_due("weekly", today=monday, daily_run=True))
        self.assertTrue(t.is_due("monthly", today=monday, daily_run=True))
        self.assertFalse(t.is_due("monthly", today=later_monday, daily_run=True))
        self.assertFalse(t.is_due("manual", today=monday, daily_run=True))


class PlanTest(unittest.TestCase):
    def plan(self, sdks, **kwargs):
        options = {"cadences": DAILY, "today": NOW.date(), "daily_run": True, "manual": False}
        options.update(kwargs)
        return t.plan(sdks, **options)

    def test_highest_bump_on_highest_version(self):
        sdks = {repo: sdk("13.2.0") for repo in t.SDK_REPOS}
        sdks["smileid-sdk-go"] = sdk("13.2.0", "Fixed")
        sdks["smileid-sdk-java"] = sdk("13.1.0", "Added")
        result = self.plan(sdks)
        self.assertEqual(result["version"], "13.3.0")
        self.assertEqual([u["repo"] for u in result["units"]], ["smileid-sdk-go", "smileid-sdk-java"])
        self.assertTrue(all(u["version"] == "13.3.0" for u in result["units"]))
        self.assertEqual(result["units"][1]["current"], "13.1.0")
        self.assertEqual(result["skipped"]["smileid-sdk-js"], "nothing under Unreleased")

    def test_patch_only(self):
        sdks = {repo: sdk("13.2.0") for repo in t.SDK_REPOS}
        sdks["smileid-sdk-php"] = sdk("13.2.0", "Fixed")
        self.assertEqual(self.plan(sdks)["version"], "13.2.1")

    def test_breaking_pr_makes_a_major(self):
        sdks = {repo: sdk("13.2.0", "Fixed") for repo in t.SDK_REPOS}
        sdks["smileid-sdk-rust"] = sdk("13.2.0", "Changed", breaking=[{"number": 9, "title": "x", "url": "u"}])
        result = self.plan(sdks)
        self.assertEqual(result["version"], "14.0.0")
        self.assertEqual(result["part"], "major")

    def test_nothing_to_release(self):
        result = self.plan({repo: sdk("13.2.0") for repo in t.SDK_REPOS})
        self.assertEqual(result["units"], [])
        self.assertEqual(result["version"], "")

    def test_cadence_and_unreadable_repos(self):
        sdks = {repo: sdk("13.2.0", "Added") for repo in t.SDK_REPOS if repo != "smileid-sdk-js"}
        cadences = dict(DAILY, **{"smileid-sdk-rust": "weekly", "smileid-sdk-go": "manual"})
        result = self.plan(sdks, cadences=cadences)
        self.assertEqual(result["skipped"]["smileid-sdk-js"], "could not be read")
        self.assertEqual(result["skipped"]["smileid-sdk-rust"], "cadence is weekly, not due")
        self.assertEqual(result["skipped"]["smileid-sdk-go"], "cadence is manual, not due")

    def test_hourly_run_only_releases_on_merge_sdks(self):
        sdks = {repo: sdk("13.2.0", "Added") for repo in t.SDK_REPOS}
        cadences = dict(DAILY, **{"smileid-sdk-python": "on-merge"})
        result = self.plan(sdks, cadences=cadences, daily_run=False)
        self.assertEqual([u["repo"] for u in result["units"]], ["smileid-sdk-python"])

    def test_manual_run_ignores_cadence_and_limits_repos(self):
        sdks = {repo: sdk("13.2.0", "Added") for repo in t.SDK_REPOS}
        cadences = {repo: "manual" for repo in t.SDK_REPOS}
        result = self.plan(sdks, cadences=cadences, daily_run=False, manual=True, repos="go, rust")
        self.assertEqual([u["repo"] for u in result["units"]], ["smileid-sdk-go", "smileid-sdk-rust"])

    def test_version_builds_on_sdks_not_releasing(self):
        # Go released 13.4.0 alone; everyone else is behind, so the next shared version is after 13.4.0.
        sdks = {repo: sdk("13.2.0", "Fixed") for repo in t.SDK_REPOS}
        sdks["smileid-sdk-go"] = sdk("13.4.0")
        self.assertEqual(self.plan(sdks)["version"], "13.4.1")

    def test_bad_repo_name(self):
        with self.assertRaises(t.TrainError):
            t.select_repos("go,kotlin")

    def test_no_tags_anywhere(self):
        sdks = {repo: {"tags": [], "changelog": changelog("Added"), "breaking_prs": []} for repo in t.SDK_REPOS}
        with self.assertRaises(t.TrainError):
            self.plan(sdks)


class MarkerTest(unittest.TestCase):
    def test_round_trip(self):
        u = {"repo": "smileid-sdk-go", "version": "13.3.0", "part": "minor", "current": "13.2.0", "breaking_prs": []}
        body = t.render_pr_body(u=u, notes="### Added\n\n- x", base_sha="abc", run_url=RUN)
        self.assertEqual(t.read_marker(body), {"base_sha": "abc", "version": "13.3.0"})
        self.assertIn("merges them all and pushes the tags", body)

    def test_major_body_says_a_person_merges(self):
        u = {"repo": "smileid-sdk-go", "version": "14.0.0", "part": "major", "current": "13.2.0",
             "breaking_prs": [{"number": 3, "title": "Drop x", "url": "https://github.com/o/r/pull/3"}]}
        body = t.render_pr_body(u=u, notes="", base_sha="abc", run_url=RUN)
        self.assertIn("won't merge it", body)
        self.assertIn("https://github.com/o/r/pull/3 Drop x", body)

    def test_bad_marker(self):
        self.assertEqual(t.read_marker("<!-- release-train-state: {bad -->"), {})
        self.assertEqual(t.read_marker(None), {})


def rollup(*conclusions):
    return [{"__typename": "CheckRun", "workflowName": "ci", "name": f"job{i}", "status": "COMPLETED",
             "conclusion": c, "startedAt": "2026-11-25T07:00:00Z"} for i, c in enumerate(conclusions)]


def release_pr(number, version, *, approved=True, checks=("SUCCESS",), merge_state="CLEAN", draft=False, marker=True):
    return {
        "number": number, "url": f"https://github.com/smileidentity/x/pull/{number}", "title": f"Release v{version}",
        "headRefName": f"release/v{version}", "headRefOid": f"sha{number}", "isDraft": draft,
        "isCrossRepository": False, "reviewDecision": "APPROVED" if approved else "REVIEW_REQUIRED",
        "mergeStateStatus": merge_state, "statusCheckRollup": rollup(*checks),
        "body": t.write_marker({"version": version, "base_sha": "b"}) if marker else "",
    }


class FakeGh:
    """Records gh calls and answers the ones the land code makes."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def __call__(self, args, token, check=True, **kwargs):
        self.calls.append(args)
        text = " ".join(args)
        if any(f in text for f in self.fail):
            return (1, "", "boom") if not check else (_ for _ in ()).throw(t.TrainError("boom"))
        return (0, "{}", "") if not check else "{}"


class LandTest(unittest.TestCase):
    LATEST = {repo: "13.2.0" for repo in t.SDK_REPOS}

    def test_waits_until_every_pr_is_approved_and_green(self):
        prs = {"smileid-sdk-go": [release_pr(1, "13.3.0")],
               "smileid-sdk-java": [release_pr(2, "13.3.0", approved=False)],
               "smileid-sdk-js": [release_pr(3, "13.3.0", checks=("SUCCESS", "FAILURE"))],
               "smileid-sdk-php": [release_pr(4, "13.3.0", merge_state="BEHIND")],
               "smileid-sdk-ruby": [release_pr(5, "13.3.0", marker=False)]}
        fake = FakeGh()
        with mock.patch.object(t, "gh", fake):
            report = t.land_batch(prs, "tok", self.LATEST)
        self.assertEqual(report["merged"], [])
        self.assertEqual(set(report["waiting"]), {"smileid-sdk-java", "smileid-sdk-js", "smileid-sdk-php", "smileid-sdk-ruby"})
        self.assertIn("it is not approved", report["waiting"]["smileid-sdk-java"])
        self.assertIn("its checks are failure", report["waiting"]["smileid-sdk-js"])
        self.assertIn("its merge state is behind", report["waiting"]["smileid-sdk-php"])
        self.assertIn("the release train did not make it", report["waiting"]["smileid-sdk-ruby"])
        self.assertEqual(fake.calls, [])

    def test_merges_the_whole_batch(self):
        prs = {"smileid-sdk-go": [release_pr(1, "13.3.0")], "smileid-sdk-rust": [release_pr(2, "13.3.0")]}
        fake = FakeGh()
        with mock.patch.object(t, "gh", fake):
            report = t.land_batch(prs, "tok", self.LATEST)
        self.assertEqual(report["merged"], ["smileid-sdk-go", "smileid-sdk-rust"])
        merges = [c for c in fake.calls if c[:3] == ["api", "-X", "PUT"]]
        self.assertEqual(len(merges), 2)
        self.assertIn("sha=sha1", merges[0])
        self.assertIn("merge_method=squash", merges[0])
        self.assertIn("commit_title=Release v13.3.0 (#1)", merges[0])

    def test_never_merges_a_major(self):
        prs = {"smileid-sdk-go": [release_pr(1, "14.0.0")]}
        fake = FakeGh()
        with mock.patch.object(t, "gh", fake):
            report = t.land_batch(prs, "tok", self.LATEST)
        self.assertTrue(report["major"])
        self.assertEqual(fake.calls, [])

    def test_only_the_newest_version_is_the_batch(self):
        prs = {"smileid-sdk-go": [release_pr(1, "13.3.0")], "smileid-sdk-java": [release_pr(2, "13.2.1")]}
        version, batch, stale = t.find_batch(prs)
        self.assertEqual(version, "13.3.0")
        self.assertEqual(list(batch), ["smileid-sdk-go"])
        self.assertEqual([p["number"] for p in stale], [2])

    def test_no_release_prs(self):
        self.assertEqual(t.land_batch({}, "tok", self.LATEST)["merged"], [])

    def test_draft_and_pending_checks_block(self):
        self.assertIn("it is a draft", t.blockers(release_pr(1, "13.3.0", draft=True), "13.3.0"))
        pending = release_pr(1, "13.3.0")
        pending["statusCheckRollup"][0]["status"] = "IN_PROGRESS"
        self.assertIn("its checks are pending", t.blockers(pending, "13.3.0"))
        self.assertIn("its checks are none", t.blockers(release_pr(1, "13.3.0", checks=()), "13.3.0"))


class TagTest(unittest.TestCase):
    def merged(self, number, version, merged_at="2026-11-25T06:00:00Z"):
        return {"number": number, "url": f"u{number}", "headRefName": f"release/v{version}",
                "mergeCommit": {"oid": f"m{number}"}, "mergedAt": merged_at, "isCrossRepository": False}

    def tag(self, prs, latest):
        calls = []

        def fake(args, token, check=True, **kwargs):
            calls.append(args)
            if args[:2] == ["pr", "list"]:
                return json.dumps(prs)
            return (0, "{}", "")

        with mock.patch.object(t, "gh", fake):
            pushed = t.tag_merged("smileid-sdk-go", "tok", latest, NOW)
        return pushed, [c for c in calls if c[:3] == ["api", "-X", "POST"]]

    def test_tags_a_merged_release(self):
        pushed, posts = self.tag([self.merged(7, "13.3.0")], "13.2.0")
        self.assertEqual(pushed, ["v13.3.0"])
        self.assertIn("ref=refs/tags/v13.3.0", posts[0])
        self.assertIn("sha=m7", posts[0])

    def test_skips_tagged_old_and_foreign_prs(self):
        old = self.merged(1, "13.4.0", merged_at="2026-10-01T00:00:00Z")
        fork = dict(self.merged(2, "13.4.0"), isCrossRepository=True)
        odd = dict(self.merged(3, "13.4.0"), headRefName="release/next")
        pushed, posts = self.tag([self.merged(4, "13.3.0"), old, fork, odd], "13.3.0")
        self.assertEqual(pushed, [])
        self.assertEqual(posts, [])


class PrepareTest(unittest.TestCase):
    def test_leaves_an_up_to_date_pr_alone(self):
        u = {"repo": "smileid-sdk-go", "version": "13.3.0", "part": "minor", "current": "13.2.0", "breaking_prs": []}
        existing = [dict(release_pr(5, "13.3.0"), body=t.write_marker({"version": "13.3.0", "base_sha": "head"}))]
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "rev-parse" in cmd:
                return "head\n"
            return ""

        with mock.patch.object(t, "run", fake_run), mock.patch.object(t, "open_release_prs", lambda r, tok: existing):
            result = t.prepare(u=u, workdir="/tmp/x", token="tok", bot_name="b", bot_email="e", run_url=RUN,
                               reviewers=[], today="2026-11-25")
        self.assertEqual(result["status"], "unchanged")
        self.assertFalse(any("bump-version.sh" in " ".join(c) for c in calls))

    def test_bump_failure_is_reported(self):
        u = {"repo": "smileid-sdk-go", "version": "13.3.0", "part": "minor", "current": "13.2.0", "breaking_prs": []}

        def fake_run(cmd, check=True, **kwargs):
            if "bump-version.sh" in " ".join(cmd):
                return (1, "", "nothing unreleased")
            return "x\n" if check else (0, "", "")

        with mock.patch.object(t, "run", fake_run), mock.patch.object(t, "open_release_prs", lambda r, tok: []):
            with self.assertRaisesRegex(t.TrainError, "nothing unreleased"):
                t.prepare(u=u, workdir="/tmp/x", token="tok", bot_name="b", bot_email="e", run_url=RUN,
                          reviewers=[], today="2026-11-25")

    def test_bump_script_gets_no_token(self):
        u = {"repo": "smileid-sdk-go", "version": "13.3.0", "part": "minor", "current": "13.2.0", "breaking_prs": []}
        seen = {}

        def fake_run(cmd, check=True, env=None, **kwargs):
            if "bump-version.sh" in " ".join(cmd):
                seen.update(env or {})
                return (0, "13.3.0\n", "")
            if cmd[:2] == ["gh", "pr"] and "create" in cmd:
                return "https://github.com/smileidentity/smileid-sdk-go/pull/9\n"
            return "main\n" if check else (0, "", "")

        with mock.patch.object(t, "run", fake_run), mock.patch.object(t, "open_release_prs", lambda r, tok: []):
            result = t.prepare(u=u, workdir="/tmp/x", token="tok", bot_name="b", bot_email="e", run_url=RUN,
                               reviewers=["alice"], today="2026-11-25")
        self.assertEqual(result["status"], "opened")
        self.assertEqual(seen.get("GH_TOKEN"), "")
        self.assertNotIn("GIT_CONFIG_VALUE_0", seen)


class AttentionTest(unittest.TestCase):
    LATEST = {repo: "13.2.0" for repo in t.SDK_REPOS}

    def test_major_release_needs_a_person(self):
        with mock.patch.object(t, "gh", FakeGh()):
            report = t.land_batch({"smileid-sdk-go": [release_pr(1, "14.0.0")]}, "tok", self.LATEST)
        (item,) = t.attention_items(report)
        self.assertIn("pull/1: v14.0.0 is a major release", item)

    def test_failing_checks_need_a_person_but_waiting_for_approval_does_not(self):
        prs = {"smileid-sdk-go": [release_pr(1, "13.3.0", checks=("FAILURE",))],
               "smileid-sdk-java": [release_pr(2, "13.3.0", approved=False)]}
        with mock.patch.object(t, "gh", FakeGh()):
            report = t.land_batch(prs, "tok", self.LATEST)
        (item,) = t.attention_items(report)
        self.assertIn("pull/1: its checks are failing", item)

    def test_merge_and_tag_failures_need_a_person(self):
        with mock.patch.object(t, "gh", FakeGh(fail=["pulls/1/merge"])):
            report = t.land_batch({"smileid-sdk-go": [release_pr(1, "13.3.0")]}, "tok", self.LATEST)
        report["tag_failed"] = {"smileid-sdk-rust": ["couldn't push the v13.3.0 tag for u. See the run log."]}
        items = t.attention_items(report)
        self.assertEqual(len(items), 2)
        self.assertIn("couldn't merge it", items[0])
        self.assertTrue(items[1].startswith("rust: couldn't push"))

    def test_nothing_to_report(self):
        self.assertEqual(t.attention_items({"version": None, "merged": [], "waiting": {}}), [])


class AttentionIssueTest(unittest.TestCase):
    def sync(self, items, open_issues, body=None):
        calls, posts = [], []

        def fake(args, token, check=True, **kwargs):
            calls.append(args)
            if args[:2] == ["issue", "list"]:
                return json.dumps(open_issues)
            if args[:2] == ["issue", "create"]:
                return "https://github.com/smileidentity/central-workflows/issues/9\n"
            return ""

        with mock.patch.object(t, "gh", fake), mock.patch.object(t, "post_slack", lambda w, text: posts.append(text)):
            result = t.sync_attention_issue("smileidentity/central-workflows", "tok", items, RUN, "hook")
        return result, [c[:2] for c in calls], posts

    def test_opens_once_and_posts_to_slack(self):
        result, calls, posts = self.sync(["a"], [])
        self.assertEqual(result, "opened")
        self.assertIn(["issue", "create"], calls)
        self.assertEqual(len(posts), 1)
        self.assertIn("issues/9", posts[0])

    def test_rewrites_only_when_the_items_change(self):
        same = t.render_attention(["a"], "https://example.com/older-run")
        result, calls, posts = self.sync(["a"], [{"number": 9, "url": "u", "body": same}])
        self.assertEqual((result, posts), ("open", []))
        self.assertNotIn(["issue", "edit"], calls)
        result, calls, posts = self.sync(["a", "b"], [{"number": 9, "url": "u", "body": same}])
        self.assertIn(["issue", "edit"], calls)
        self.assertEqual(posts, [])

    def test_closes_when_resolved(self):
        result, calls, posts = self.sync([], [{"number": 9, "url": "u", "body": "x"}])
        self.assertEqual(result, "closed")
        self.assertIn(["issue", "close"], calls)
        self.assertEqual(posts, ["Resolved: u"])

    def test_quiet_when_nothing_is_wrong(self):
        self.assertEqual(self.sync([], [])[0], "none")

    def test_slack_failure_is_only_a_warning(self):
        with mock.patch.object(t.urllib.request, "urlopen", side_effect=OSError("down")):
            t.post_slack("https://hooks.example.com/x", "hi")
        t.post_slack("", "skipped")


class WorkflowTest(unittest.TestCase):
    def test_daily_cron_matches_plan_cron(self):
        crons = re.findall(r"^\s*- cron: '([^']+)'", WORKFLOW, re.MULTILINE)
        (plan_cron,) = re.findall(r"^\s+PLAN_CRON: '([^']+)'", WORKFLOW, re.MULTILINE)
        self.assertEqual(crons[0], plan_cron)
        self.assertIn(f"github.event.schedule == '{plan_cron}'", WORKFLOW)

    def test_failed_runs_are_reported(self):
        self.assertIn("uses: ./.github/workflows/scheduled-run-status.yml", WORKFLOW)
        self.assertIn("needs: [plan, prepare, land]", WORKFLOW)

    def test_one_run_at_a_time(self):
        self.assertIn("group: sdk-release-train", WORKFLOW)
        self.assertIn("cancel-in-progress: false", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
