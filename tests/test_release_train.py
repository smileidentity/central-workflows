"""Tests for scripts/release_train/release_train.py. Run: python3 -m unittest discover -s tests"""

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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


def sdk(version="13.2.0", *sections, breaking=(), open_versions=()):
    return {"tags": ["v13.0.0", f"v{version}"], "changelog": changelog(*sections), "breaking_prs": list(breaking),
            "open_versions": list(open_versions)}


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

    def test_landing_major_batch_isnt_bumped_again(self):
        # A person merged Go's v14.0.0 PR and the train tagged it. Java's PR is still open.
        breaking = [{"number": 9, "title": "x", "url": "u"}]
        sdks = {repo: sdk("13.2.0") for repo in t.SDK_REPOS}
        sdks["smileid-sdk-go"] = sdk("14.0.0")
        sdks["smileid-sdk-java"] = sdk("13.2.0", "Changed", breaking=breaking, open_versions=["14.0.0"])
        result = self.plan(sdks)
        self.assertEqual(result["version"], "14.0.0")
        self.assertEqual([u["repo"] for u in result["units"]], ["smileid-sdk-java"])

    def test_failed_merge_is_replanned_at_the_same_version(self):
        sdks = {repo: sdk("13.3.0") for repo in t.SDK_REPOS}
        sdks["smileid-sdk-js"] = sdk("13.2.0", "Added", open_versions=["13.3.0"])
        self.assertEqual(self.plan(sdks)["version"], "13.3.0")

    def test_mixed_cadences_keep_the_open_batch(self):
        # Monday planned Go (Fixed) and Rust (Added) at 13.3.0. On Tuesday only Go is due.
        sdks = {repo: sdk("13.2.0") for repo in t.SDK_REPOS}
        sdks["smileid-sdk-go"] = sdk("13.2.0", "Fixed", open_versions=["13.3.0"])
        sdks["smileid-sdk-rust"] = sdk("13.2.0", "Added", open_versions=["13.3.0"])
        cadences = dict(DAILY, **{"smileid-sdk-rust": "weekly"})
        result = self.plan(sdks, cadences=cadences)
        self.assertEqual(result["version"], "13.3.0")
        self.assertEqual([u["repo"] for u in result["units"]], ["smileid-sdk-go"])

    def test_a_bigger_bump_moves_past_the_open_batch(self):
        # The open batch is a patch, but a new SDK now needs a minor.
        sdks = {repo: sdk("13.2.0") for repo in t.SDK_REPOS}
        sdks["smileid-sdk-go"] = sdk("13.2.0", "Fixed", open_versions=["13.2.1"])
        sdks["smileid-sdk-java"] = sdk("13.2.0", "Added")
        self.assertEqual(self.plan(sdks)["version"], "13.3.0")

    def test_an_overtaken_open_pr_never_plans_below_a_release(self):
        # Go's weekly 13.2.0 PR is still open, but JS has since released 14.0.0.
        sdks = {repo: sdk("13.1.0", "Fixed") for repo in t.SDK_REPOS}
        sdks["smileid-sdk-go"] = sdk("13.1.0", "Fixed", open_versions=["13.2.0"])
        sdks["smileid-sdk-js"] = sdk("14.0.0")
        result = self.plan(sdks)
        self.assertEqual(result["version"], "14.0.1")
        self.assertIn("smileid-sdk-go", [u["repo"] for u in result["units"]])

    def test_an_sdk_that_released_the_batch_moves_it_on(self):
        # Go released 13.3.0 from the batch and now has a breaking change; JS's 13.3.0 PR is still open.
        breaking = [{"number": 9, "title": "x", "url": "u"}]
        sdks = {repo: sdk("13.2.0") for repo in t.SDK_REPOS}
        sdks["smileid-sdk-go"] = sdk("13.3.0", "Changed", breaking=breaking)
        sdks["smileid-sdk-js"] = sdk("13.2.0", "Added", open_versions=["13.3.0"])
        result = self.plan(sdks)
        self.assertEqual(result["version"], "14.0.0")
        self.assertEqual([u["repo"] for u in result["units"]], ["smileid-sdk-go", "smileid-sdk-js"])

    def test_an_untagged_sdk_joins_the_landing_batch(self):
        sdks = {repo: sdk("13.2.0") for repo in t.SDK_REPOS}
        sdks["smileid-sdk-rust"] = {"tags": [], "changelog": changelog("Added"), "breaking_prs": [],
                                    "open_versions": ["13.2.0"]}
        sdks["smileid-sdk-go"] = sdk("13.1.0", "Fixed", open_versions=["13.2.0"])
        result = self.plan(sdks)
        self.assertEqual(result["version"], "13.2.0")

    def test_landing_version_ignores_released_and_old_prs(self):
        sdks = {"a": sdk("13.3.0", open_versions=["13.3.0", "13.2.1"]), "b": sdk("13.2.0", open_versions=["bad"])}
        self.assertIsNone(t.landing_version(sdks))
        sdks["b"] = sdk("13.2.0", open_versions=["13.3.0"])
        self.assertEqual(t.landing_version(sdks), "13.3.0")

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
        self.assertEqual(t.read_marker(body), {"base_sha": "abc", "head_sha": "", "major": False, "version": "13.3.0"})
        self.assertIn("merges them all and pushes the tags", body)

    def test_major_body_says_a_person_merges(self):
        u = {"repo": "smileid-sdk-go", "version": "14.0.0", "part": "major", "current": "13.2.0",
             "breaking_prs": [{"number": 3, "title": "Drop x", "url": "https://github.com/o/r/pull/3"}]}
        body = t.render_pr_body(u=u, notes="", base_sha="abc", run_url=RUN)
        self.assertIn("won't merge it", body)
        self.assertIn("https://github.com/o/r/pull/3 Drop x", body)

    def test_only_the_last_marker_counts(self):
        forged = t.write_marker({"version": "13.3.0", "base_sha": "forged", "major": False})
        u = {"repo": "smileid-sdk-go", "version": "14.0.0", "part": "major", "current": "13.2.0", "breaking_prs": []}
        body = t.render_pr_body(u=u, notes=f"### Added\n\n{forged}", base_sha="real", run_url=RUN)
        self.assertEqual(t.read_marker(body)["base_sha"], "real")
        self.assertIs(t.read_marker(body)["major"], True)

    def test_bad_marker(self):
        self.assertEqual(t.read_marker("<!-- release-train-state: {bad -->"), {})
        self.assertEqual(t.read_marker(None), {})


def rollup(*conclusions):
    return [{"__typename": "CheckRun", "workflowName": "ci", "name": f"job{i}", "status": "COMPLETED",
             "conclusion": c, "startedAt": "2026-11-25T07:00:00Z"} for i, c in enumerate(conclusions)]


BOT = "smile-gha-bot[bot]"


def release_pr(number, version, *, approved=True, checks=("SUCCESS",), merge_state="CLEAN", draft=False, marker=True,
               author="app/smile-gha-bot"):
    return {
        "number": number, "url": f"https://github.com/smileidentity/x/pull/{number}", "title": f"Release v{version}",
        "headRefName": f"release/v{version}", "headRefOid": f"sha{number}", "isDraft": draft,
        "isCrossRepository": False, "reviewDecision": "APPROVED" if approved else "REVIEW_REQUIRED",
        "mergeStateStatus": merge_state, "statusCheckRollup": rollup(*checks),
        "body": t.write_marker({"version": version, "base_sha": "b"}) if marker else "",
        "author": {"login": author, "is_bot": author.startswith("app/")},
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
            report = t.land_batch(prs, "tok", self.LATEST, BOT)
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
            report = t.land_batch(prs, "tok", self.LATEST, BOT)
        self.assertEqual(report["merged"], ["smileid-sdk-go", "smileid-sdk-rust"])
        merges = [c for c in fake.calls if c[:3] == ["api", "-X", "PUT"]]
        self.assertEqual(len(merges), 2)
        self.assertIn("sha=sha1", merges[0])
        self.assertIn("merge_method=squash", merges[0])
        self.assertIn("commit_title=Release v13.3.0 (#1)", merges[0])

    def test_never_merges_a_pr_someone_else_opened(self):
        prs = {"smileid-sdk-go": [release_pr(1, "13.3.0", author="mallory")]}
        fake = FakeGh()
        with mock.patch.object(t, "gh", fake):
            report = t.land_batch(prs, "tok", self.LATEST, BOT)
        self.assertEqual(report["merged"], [])
        self.assertIn("mallory opened it, not the release train", report["waiting"]["smileid-sdk-go"])
        self.assertEqual(fake.calls, [])

    def test_nothing_merges_without_a_bot_name(self):
        self.assertIn("app/smile-gha-bot opened it, not the release train", t.blockers(release_pr(1, "13.3.0"), "13.3.0", ""))

    def test_by_train(self):
        self.assertTrue(t.by_train({"author": {"login": "app/smile-gha-bot", "is_bot": True}}, BOT))
        # Only gh's form for an app counts. Anyone could register a user login equal to the slug.
        for author in ({"login": "smile-gha-bot", "is_bot": False}, {"login": "app/smile-gha-bot", "is_bot": False},
                       {"login": "app/smile-gha-bot"}, {"login": "smile-gha-bot[bot]", "is_bot": True},
                       {"login": "app/other-bot", "is_bot": True}, {}):
            self.assertFalse(t.by_train({"author": author}, BOT), author)
        self.assertFalse(t.by_train({"author": {"login": "app/smile-gha-bot", "is_bot": True}}, "[bot]"))

    def test_a_user_named_like_the_bot_cant_get_a_pr_merged(self):
        prs = {"smileid-sdk-go": [release_pr(1, "13.3.0", author="smile-gha-bot")]}
        fake = FakeGh()
        with mock.patch.object(t, "gh", fake):
            report = t.land_batch(prs, "tok", self.LATEST, BOT)
        self.assertEqual(report["merged"], [])
        self.assertEqual(fake.calls, [])

    def test_a_hand_made_release_pr_never_joins_or_takes_over_the_batch(self):
        # Go has a person's release/v14.0.0 and a person's release/v13.3.0; JS has the train's approved v13.3.0.
        listed = {"smileid-sdk-go": [release_pr(1, "14.0.0", author="mallory"), release_pr(3, "13.3.0", author="mallory")],
                  "smileid-sdk-js": [release_pr(2, "13.3.0")]}
        calls = []

        def fake_gh(args, token, check=True, **kwargs):
            calls.append(args)
            if args[:2] == ["pr", "list"]:
                repo = args[args.index("--repo") + 1].split("/")[1]
                return json.dumps(listed.get(repo, []) if "open" in args else [])
            return (0, "{}", "") if not check else "{}"

        with mock.patch.object(t, "gh", fake_gh), \
                mock.patch.object(t, "run", lambda cmd, **kw: "sha\trefs/tags/v13.2.0\n"), \
                mock.patch("builtins.print"):
            report = t.land("tok", NOW, BOT)
        self.assertEqual(report["version"], "13.3.0")
        self.assertEqual(report["merged"], ["smileid-sdk-js"])
        self.assertTrue(any(c[:3] == ["api", "-X", "PUT"] and "repos/smileidentity/smileid-sdk-js/pulls/2/merge" in c
                            for c in calls))
        self.assertFalse(any("pulls/1/merge" in " ".join(c) or "pulls/3/merge" in " ".join(c) for c in calls))

    def test_rest_of_a_major_batch_stays_major_after_one_is_tagged(self):
        # Go's v14.0.0 was merged by hand and tagged; Java's PR is approved and green.
        latest = dict(self.LATEST, **{"smileid-sdk-go": "14.0.0"})
        fake = FakeGh()
        with mock.patch.object(t, "gh", fake):
            report = t.land_batch({"smileid-sdk-java": [release_pr(2, "14.0.0")]}, "tok", latest, BOT)
        self.assertTrue(report["major"])
        self.assertEqual(fake.calls, [])

    def test_an_untagged_sdk_doesnt_make_a_minor_batch_major(self):
        latest = {r: v for r, v in self.LATEST.items() if r != "smileid-sdk-rust"}
        prs = {"smileid-sdk-go": [release_pr(1, "13.3.0")], "smileid-sdk-rust": [release_pr(2, "13.3.0")]}
        with mock.patch.object(t, "gh", FakeGh()):
            report = t.land_batch(prs, "tok", latest, BOT)
        self.assertNotIn("major", report)
        self.assertEqual(report["merged"], ["smileid-sdk-go", "smileid-sdk-rust"])

    def test_marker_says_major(self):
        pr = release_pr(2, "13.3.0")
        pr["body"] = t.write_marker({"version": "13.3.0", "base_sha": "b", "major": True})
        with mock.patch.object(t, "gh", FakeGh()):
            report = t.land_batch({"smileid-sdk-java": [pr]}, "tok", self.LATEST, BOT)
        self.assertTrue(report["major"])

    def test_never_merges_a_major(self):
        prs = {"smileid-sdk-go": [release_pr(1, "14.0.0")]}
        fake = FakeGh()
        with mock.patch.object(t, "gh", fake):
            report = t.land_batch(prs, "tok", self.LATEST, BOT)
        self.assertTrue(report["major"])
        self.assertEqual(fake.calls, [])

    def test_only_the_newest_version_is_the_batch(self):
        prs = {"smileid-sdk-go": [release_pr(1, "13.3.0")], "smileid-sdk-java": [release_pr(2, "13.2.1")]}
        version, batch, stale = t.find_batch(prs)
        self.assertEqual(version, "13.3.0")
        self.assertEqual(list(batch), ["smileid-sdk-go"])
        self.assertEqual([p["number"] for p in stale], [2])

    def test_no_release_prs(self):
        self.assertEqual(t.land_batch({}, "tok", self.LATEST, BOT)["merged"], [])

    def test_draft_and_pending_checks_block(self):
        self.assertIn("it is a draft", t.blockers(release_pr(1, "13.3.0", draft=True), "13.3.0", BOT))
        pending = release_pr(1, "13.3.0")
        pending["statusCheckRollup"][0]["status"] = "IN_PROGRESS"
        self.assertIn("its checks are pending", t.blockers(pending, "13.3.0", BOT))
        self.assertIn("its checks are none", t.blockers(release_pr(1, "13.3.0", checks=()), "13.3.0", BOT))


class TagTest(unittest.TestCase):
    def merged(self, number, version, merged_at="2026-11-25T06:00:00Z"):
        return {"number": number, "url": f"u{number}", "headRefName": f"release/v{version}",
                "mergeCommit": {"oid": f"m{number}"}, "mergedAt": merged_at, "isCrossRepository": False,
                "author": {"login": "app/smile-gha-bot", "is_bot": True}}

    def tag(self, prs, latest):
        calls = []

        def fake(args, token, check=True, **kwargs):
            calls.append(args)
            if args[:2] == ["pr", "list"]:
                return json.dumps(prs)
            return (0, "{}", "")

        with mock.patch.object(t, "gh", fake):
            pushed = t.tag_merged("smileid-sdk-go", "tok", latest, NOW, BOT)
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
        person = dict(self.merged(5, "13.4.0"), author={"login": "mallory"})
        pushed, posts = self.tag([self.merged(4, "13.3.0"), old, fork, odd, person], "13.3.0")
        self.assertEqual(pushed, [])
        self.assertEqual(posts, [])


BUMP_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$1" > VERSION
echo "$1"
"""


def sh(cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **(env or {})}).stdout


class PrepareTest(unittest.TestCase):
    """Runs prepare with real git against a local bare repository; only gh is faked."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.origin = self.tmp / "smileidentity" / "smileid-sdk-go.git"
        sh(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)])
        seed = self.tmp / "seed"
        sh(["git", "init", "-q", "-b", "main", str(seed)])
        (seed / "scripts").mkdir()
        (seed / "scripts" / "bump-version.sh").write_text(BUMP_SCRIPT)
        (seed / "CHANGELOG.md").write_text(changelog("Added"))
        (seed / "VERSION").write_text("13.2.0\n")
        self.commit(seed, "seed")
        sh(["git", "push", "-q", str(self.origin), "main"], seed)
        self.seed = seed
        self.unit = {"repo": "smileid-sdk-go", "version": "13.3.0", "part": "minor", "current": "13.2.0",
                     "breaking_prs": []}
        self.gh_calls, self.open_prs, self.head_prs = [], [], []
        patcher = mock.patch.object(t, "GIT_BASE", str(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def commit(repo, message, author="t"):
        sh(["git", "add", "-A"], repo)
        sh(["git", "-c", f"user.name={author}", "-c", "user.email=t@example.com", "commit", "-qm", message], repo)

    def fake_gh(self, args, token, check=True, **kwargs):
        self.gh_calls.append(args)
        if args[:2] == ["pr", "list"] and "--head" in args:
            return json.dumps(self.head_prs)
        if args[:2] == ["pr", "list"]:
            return json.dumps(self.open_prs)
        if args[:2] == ["pr", "create"]:
            return "https://github.com/smileidentity/smileid-sdk-go/pull/9\n"
        if args[:1] == ["api"] and args[1].endswith("smileid-sdk-go"):
            return "main\n"
        return (0, "", "") if not check else ""

    def bump_job(self):
        """The bump job: clone with no write token, run the script, write the patch."""
        bump_dir, out = self.tmp / "bump", self.tmp / "bundle"
        shutil.rmtree(bump_dir, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)
        t.clone_sdk("smileid-sdk-go", str(bump_dir))
        t.make_patch(u=self.unit, workdir=str(bump_dir), out=str(out), today="2026-11-25")
        return out

    def publish_job(self, bundle):
        shutil.rmtree(self.tmp / "work", ignore_errors=True)
        with mock.patch.object(t, "gh", self.fake_gh), mock.patch("builtins.print"):
            return t.publish(u=self.unit, bundle=str(bundle), workdir=str(self.tmp / "work"), token="", bot_name=BOT,
                             bot_email="1+bot@users.noreply.github.com", run_url=RUN, reviewers=["alice"])

    def prepare(self):
        return self.publish_job(self.bump_job())

    def push_seed(self, message, author="t", branch="main"):
        self.commit(self.seed, message, author=author)
        sh(["git", "push", "-qf", str(self.origin), f"HEAD:refs/heads/{branch}"], self.seed)

    def remote(self, ref):
        return sh(["git", "ls-remote", str(self.origin), ref]).split()

    def test_opens_a_release_pr(self):
        result = self.prepare()
        self.assertEqual(result["status"], "opened")
        sha = self.remote("refs/heads/release/v13.3.0")[0]
        self.assertEqual(sh(["git", "show", f"{sha}:VERSION"], self.origin), "13.3.0\n")
        self.assertEqual(sh(["git", "log", "-1", "--format=%an %s", sha], self.origin).strip(),
                         f"{BOT} Release v13.3.0")
        create = next(c for c in self.gh_calls if c[:2] == ["pr", "create"])
        marker = t.read_marker(create[create.index("--body") + 1])
        self.assertEqual((marker["major"], marker["head_sha"]), (False, sha))
        self.assertIn(["api", "-X", "POST", "repos/smileidentity/smileid-sdk-go/pulls/9/requested_reviewers",
                       "-f", "reviewers[]=alice"], self.gh_calls)

    def test_bump_script_and_its_git_config_stay_in_the_bump_job(self):
        # The script plants a clean filter and a proxy in .git/config. They stay in
        # the bump job's clone; the publish job's clone never sees them.
        leak = self.tmp / "leak.txt"
        stealer = self.tmp / "steal.sh"
        stealer.write_text(f'echo "${{WRITE_TOKEN:-}}${{GH_TOKEN:-}}" >> {leak}\ncat\n')
        (self.seed / "scripts" / "bump-version.sh").write_text(BUMP_SCRIPT.replace(
            'echo "$1"',
            f'git config filter.steal.clean "sh {stealer}"\n'
            'git config http.proxy http://127.0.0.1:9\n'
            'echo "* filter=steal" > .gitattributes\n'
            'echo "$1"'))
        self.push_seed("evil")
        with mock.patch.dict(os.environ, {"WRITE_TOKEN": "secret-write", "GH_TOKEN": "secret-gh"}):
            self.prepare()
        # The filter ran in the bump job's git, without the job's environment.
        self.assertTrue(leak.exists())
        self.assertNotIn("secret", leak.read_text())
        config = sh(["git", "config", "--list", "--local"], self.tmp / "work")
        self.assertNotIn("steal", config)
        self.assertNotIn("http.proxy", config)

    def test_bump_script_runs_without_the_job_environment(self):
        (self.seed / "scripts" / "bump-version.sh").write_text(BUMP_SCRIPT.replace(
            'echo "$1"', 'env > seen-env.txt\necho "$1"'))
        self.push_seed("env")
        with mock.patch.dict(os.environ, {"READ_TOKEN": "secret-read", "GH_TOKEN": "secret-gh"}):
            bundle = self.bump_job()
        patch = (bundle / "patch.diff").read_text()
        self.assertIn("seen-env.txt", patch)
        self.assertNotIn("secret", patch)
        self.assertIn("BUMP_DATE=2026-11-25", patch)

    def test_bump_failure_is_reported(self):
        (self.seed / "scripts" / "bump-version.sh").write_text(
            "#!/usr/bin/env bash\necho 'nothing unreleased' >&2\nexit 1\n")
        self.push_seed("fail")
        with self.assertRaisesRegex(t.TrainError, "nothing unreleased"):
            self.bump_job()

    def test_leaves_an_up_to_date_pr_alone(self):
        bundle = self.bump_job()
        head = self.remote("refs/heads/main")[0]
        self.open_prs = [dict(release_pr(5, "13.3.0"), body=t.write_marker({"version": "13.3.0", "base_sha": head}))]
        self.assertEqual(self.publish_job(bundle)["status"], "unchanged")
        self.assertEqual(self.remote("refs/heads/release/v13.3.0"), [])

    def test_updates_its_own_open_pr(self):
        sh(["git", "checkout", "-qb", "release/v13.3.0"], self.seed)
        (self.seed / "VERSION").write_text("13.3.0 old\n")
        self.push_seed("older", author=BOT, branch="release/v13.3.0")
        ours = self.remote("refs/heads/release/v13.3.0")[0]
        self.open_prs = [dict(release_pr(5, "13.3.0"), headRefOid=ours,
                              body=t.write_marker({"version": "13.3.0", "base_sha": "old", "head_sha": ours}))]
        sh(["git", "checkout", "-q", "main"], self.seed)
        self.assertEqual(self.prepare()["status"], "updated")
        sha = self.remote("refs/heads/release/v13.3.0")[0]
        self.assertEqual(sh(["git", "show", f"{sha}:VERSION"], self.origin), "13.3.0\n")

    def test_never_throws_away_a_persons_commit_on_its_own_pr(self):
        sh(["git", "checkout", "-qb", "release/v13.3.0"], self.seed)
        (self.seed / "VERSION").write_text("13.3.0 fixed by hand\n")
        self.push_seed("a person's fix", author="alice", branch="release/v13.3.0")
        theirs = self.remote("refs/heads/release/v13.3.0")[0]
        self.open_prs = [dict(release_pr(5, "13.3.0"), headRefOid=theirs,
                              body=t.write_marker({"version": "13.3.0", "base_sha": "old", "head_sha": "a" * 40}))]
        sh(["git", "checkout", "-q", "main"], self.seed)
        with self.assertRaisesRegex(t.TrainError, "commits the release train didn't push"):
            self.prepare()
        self.assertEqual(self.remote("refs/heads/release/v13.3.0")[0], theirs)

    def test_never_pushes_over_a_persons_release_pr(self):
        self.head_prs = [dict(release_pr(4, "13.3.0", author="mallory"), isCrossRepository=False)]
        with self.assertRaisesRegex(t.TrainError, "mallory opened it, not the release train"):
            self.prepare()
        self.assertFalse(any(c[:2] == ["pr", "create"] for c in self.gh_calls))

    def test_never_pushes_over_a_branch_without_a_train_pr(self):
        # Even a commit whose author name is the bot's: anyone can set that.
        for author in ("mallory", BOT):
            with self.subTest(author=author):
                sh(["git", "checkout", "-qB", "release/v13.3.0", "main"], self.seed)
                (self.seed / "VERSION").write_text(f"13.3.0 {author}\n")
                self.push_seed("by hand", author=author, branch="release/v13.3.0")
                theirs = self.remote("refs/heads/release/v13.3.0")[0]
                sh(["git", "checkout", "-q", "main"], self.seed)
                with self.assertRaisesRegex(t.TrainError, "no open release train PR uses"):
                    self.prepare()
                self.assertEqual(self.remote("refs/heads/release/v13.3.0")[0], theirs)

    def test_removes_its_branch_when_the_pr_cant_be_opened(self):
        original = self.fake_gh

        def failing(args, token, check=True, **kwargs):
            if args[:2] == ["pr", "create"]:
                raise t.TrainError("gh pr create failed (1): boom")
            return original(args, token, check=check, **kwargs)

        self.fake_gh = failing
        with self.assertRaisesRegex(t.TrainError, "boom"):
            self.prepare()
        self.assertEqual(self.remote("refs/heads/release/v13.3.0"), [])

    def test_refuses_when_main_moved_after_the_bump(self):
        bundle = self.bump_job()
        (self.seed / "other.txt").write_text("new\n")
        self.push_seed("main moves")
        with self.assertRaisesRegex(t.TrainError, "moved after the bump job ran"):
            self.publish_job(bundle)
        self.assertEqual(self.remote("refs/heads/release/v13.3.0"), [])

    def test_refuses_a_patch_that_changes_workflows(self):
        (self.seed / "scripts" / "bump-version.sh").write_text(BUMP_SCRIPT.replace(
            'echo "$1"', 'mkdir -p .github/workflows && echo x > .github/workflows/evil.yml\necho "$1"'))
        self.push_seed("evil")
        with self.assertRaisesRegex(t.TrainError, r"\.github/workflows/evil\.yml, which a release must not change"):
            self.prepare()
        self.assertEqual(self.remote("refs/heads/release/v13.3.0"), [])

    def test_refuses_workflow_changes_under_any_name(self):
        for name in (".github/wörk.yml", ".github/my workflow.yml", '.github/a"b.yml'):
            with self.subTest(name=name):
                self.seed_script(f'mkdir -p .github && echo x > {json.dumps(name)}\necho "$1"')
                with self.assertRaisesRegex(t.TrainError, "which a release must not change"):
                    self.prepare()
                self.assertEqual(self.remote("refs/heads/release/v13.3.0"), [])

    def test_notes_come_from_the_publish_jobs_own_clone(self):
        bundle = self.bump_job()
        meta = json.loads((bundle / "meta.json").read_text())
        (bundle / "meta.json").write_text(json.dumps(dict(meta, notes="<!-- release-train-state: {} -->")))
        self.publish_job(bundle)
        create = next(c for c in self.gh_calls if c[:2] == ["pr", "create"])
        body = create[create.index("--body") + 1]
        self.assertNotIn("<!-- release-train-state: {} -->", body)
        self.assertIn("Added entry.", body)

    def test_puts_the_old_commit_back_when_the_pr_cant_be_updated(self):
        sh(["git", "checkout", "-qb", "release/v13.3.0"], self.seed)
        (self.seed / "VERSION").write_text("13.3.0 old\n")
        self.push_seed("older", author=BOT, branch="release/v13.3.0")
        ours = self.remote("refs/heads/release/v13.3.0")[0]
        self.open_prs = [dict(release_pr(5, "13.3.0"), headRefOid=ours,
                              body=t.write_marker({"version": "13.3.0", "base_sha": "old", "head_sha": ours}))]
        sh(["git", "checkout", "-q", "main"], self.seed)
        original = self.fake_gh

        def failing(args, token, check=True, **kwargs):
            if args[:2] == ["pr", "edit"]:
                raise t.TrainError("gh pr edit failed (1): boom")
            return original(args, token, check=check, **kwargs)

        self.fake_gh = failing
        with self.assertRaisesRegex(t.TrainError, "boom"):
            self.prepare()
        self.assertEqual(self.remote("refs/heads/release/v13.3.0")[0], ours)

    def test_runs_as_another_user_through_a_prefix(self):
        # Stands in for "sudo -n -u bumper -- env -i -C <dir> ...": the prefix, not the
        # caller, picks the directory and the environment.
        wrapper = self.tmp / "as-other-user.sh"
        wrapper.write_text('cd "$1" && shift && exec env -i PATH=/usr/bin:/bin:/usr/local/bin "$@"\n')
        clone = self.tmp / "other"
        t.clone_sdk("smileid-sdk-go", str(clone))
        out = self.tmp / "out"
        with mock.patch.dict(os.environ, {"WRITE_TOKEN": "secret"}):
            t.make_patch(u=self.unit, workdir=str(clone), out=str(out), today="2026-11-25",
                         runas=("sh", str(wrapper), str(clone)))
        self.assertIn("+13.3.0", (out / "patch.diff").read_text())
        self.assertNotIn("notes", json.loads((out / "meta.json").read_text()))

    def seed_script(self, body):
        (self.seed / "scripts" / "bump-version.sh").write_text(BUMP_SCRIPT.replace('echo "$1"', body))
        self.push_seed("script")

    def test_refuses_a_bundle_for_another_release(self):
        bundle = self.bump_job()
        meta = json.loads((bundle / "meta.json").read_text())
        (bundle / "meta.json").write_text(json.dumps(dict(meta, version="13.4.0")))
        with self.assertRaisesRegex(t.TrainError, "for another release"):
            self.publish_job(bundle)

    def test_clean_environment_drops_tokens(self):
        with mock.patch.dict(t.os.environ, {"WRITE_TOKEN": "secret", "GH_TOKEN": "secret", "PATH": "/usr/bin:/bin"}):
            out = t.run(["env"], inherit_env=False, env={"BUMP_DATE": "2026-11-25"})
        self.assertNotIn("secret", out)
        self.assertIn("BUMP_DATE=2026-11-25", out)
        self.assertIn("PATH=/usr/bin:/bin", out)


class AttentionTest(unittest.TestCase):
    LATEST = {repo: "13.2.0" for repo in t.SDK_REPOS}

    def test_major_release_needs_a_person(self):
        with mock.patch.object(t, "gh", FakeGh()):
            report = t.land_batch({"smileid-sdk-go": [release_pr(1, "14.0.0")]}, "tok", self.LATEST, BOT)
        (item,) = t.attention_items(report)
        self.assertIn("pull/1: v14.0.0 is a major release", item)

    def test_failing_checks_need_a_person_but_waiting_for_approval_does_not(self):
        prs = {"smileid-sdk-go": [release_pr(1, "13.3.0", checks=("FAILURE",))],
               "smileid-sdk-java": [release_pr(2, "13.3.0", approved=False)]}
        with mock.patch.object(t, "gh", FakeGh()):
            report = t.land_batch(prs, "tok", self.LATEST, BOT)
        (item,) = t.attention_items(report)
        self.assertIn("pull/1: its checks are failing", item)

    def test_merge_and_tag_failures_need_a_person(self):
        with mock.patch.object(t, "gh", FakeGh(fail=["pulls/1/merge"])):
            report = t.land_batch({"smileid-sdk-go": [release_pr(1, "13.3.0")]}, "tok", self.LATEST, BOT)
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
        self.assertIn("needs: [plan, bump, publish, land]", WORKFLOW)

    def job(self, name):
        start = WORKFLOW.index(f"\n  {name}:\n")
        following = re.search(r"\n  [a-z]+:\n", WORKFLOW[start + 1:])
        return WORKFLOW[start:start + 1 + following.start()] if following else WORKFLOW[start:]

    def test_the_bump_job_holds_no_write_secrets(self):
        bump = self.job("bump")
        self.assertIn("permissions: {}", bump)
        for secret in ("SMILE_GHA_BOT", "SPEC_SYNC_BUNDLE_KEY", "WRITE_TOKEN", "GH_TOKEN", "id-token"):
            self.assertNotIn(secret, bump)
        self.assertLess(bump.index("remove root access"), bump.index("release_train.py bump"))
        self.assertIn("--run-as bumper", bump)
        self.assertIn("ALL=(bumper) NOPASSWD: ALL", bump)

    def test_the_publish_job_never_runs_sdk_code(self):
        publish = self.job("publish")
        self.assertNotIn("bump-version", publish)
        self.assertNotIn("release_train.py bump", publish)

    def test_the_bundle_cert_matches_spec_sync(self):
        spec = (ROOT / ".github" / "workflows" / "sdk-spec-sync.yml").read_text()
        cert = re.compile(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)
        self.assertEqual(cert.findall(WORKFLOW), cert.findall(spec)[:1])

    def test_one_run_at_a_time(self):
        self.assertIn("group: sdk-release-train", WORKFLOW)
        self.assertIn("cancel-in-progress: false", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
