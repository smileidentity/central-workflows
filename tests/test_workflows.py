"""Checks on the workflow files that actionlint can't make. Run: python3 -m unittest discover -s tests

The standard library has no YAML parser, so these read the files as text.
"""

import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
SPEC_SYNC = (WORKFLOWS / "sdk-spec-sync.yml").read_text()
# Five space-separated cron fields inside single quotes.
CRON_LITERAL = re.compile(r"'([0-9*/,-]+(?: [0-9*/,-]+){4})'")


def schedules(text):
    return re.findall(r"^\s*- cron: '([^']+)'", text, re.MULTILINE)


class SpecSyncCronTest(unittest.TestCase):
    """The daily cron is written in three places, because concurrency expressions can't read env."""

    def test_daily_cron_is_the_same_everywhere(self):
        daily, hourly = schedules(SPEC_SYNC)
        (sync_cron,) = re.findall(r"^\s+SYNC_CRON: '([^']+)'", SPEC_SYNC, re.MULTILINE)
        self.assertEqual(sync_cron, daily)
        self.assertNotEqual(hourly, daily)
        (group,) = re.findall(r"^concurrency:\n\s+group: (.+)$", SPEC_SYNC, re.MULTILINE)
        self.assertEqual(CRON_LITERAL.findall(group), [daily])

    def test_no_other_cron_literals(self):
        # Any cron written into an expression must be one of the schedules.
        crons = set(schedules(SPEC_SYNC))
        for line in SPEC_SYNC.splitlines():
            if "${{" in line:
                for literal in CRON_LITERAL.findall(line):
                    self.assertIn(literal, crons, line.strip())


class ToolchainTest(unittest.TestCase):
    def test_go_is_pinned_to_a_minor(self):
        # golangci-lint is pinned, and a new Go minor can break it.
        (version,) = re.findall(r"^\s+go-version: (.+)$", SPEC_SYNC, re.MULTILINE)
        self.assertRegex(version.strip(), r"^'\d+\.\d+\.x'$")


class ClaudePermissionTest(unittest.TestCase):
    def test_claude_runs_in_auto_mode(self):
        # The last --permission-mode wins. Auto mode sends every Bash command
        # through the safety review; default mode would run them unreviewed.
        modes = re.findall(r"--permission-mode (\S+)", SPEC_SYNC)
        self.assertEqual(set(modes), {"auto"})
        self.assertNotIn("--dangerously-skip-permissions", SPEC_SYNC)
        self.assertNotIn("bypassPermissions", SPEC_SYNC)

    def test_claude_cannot_use_network_tools(self):
        (denied,) = re.findall(r"--disallowedTools (\S+)", SPEC_SYNC)
        self.assertLessEqual({"WebFetch", "WebSearch"}, set(denied.split(",")))


class KeepaliveTest(unittest.TestCase):
    def test_every_scheduled_workflow_is_kept_alive(self):
        (listed,) = re.findall(r"^\s+WORKFLOWS: (.+)$", SPEC_SYNC, re.MULTILINE)
        scheduled = sorted(p.name for p in WORKFLOWS.glob("*.yml") if schedules(p.read_text()))
        self.assertEqual(sorted(listed.split()), scheduled)


class SpecCoverageActionTest(unittest.TestCase):
    """The action finds the checker by a relative path, so a move of either file must update both."""

    def test_the_action_runs_the_checker(self):
        action = WORKFLOWS.parent / "actions" / "spec-coverage"
        text = (action / "action.yml").read_text()
        (relative,) = re.findall(r"SCRIPT: \$\{\{ github\.action_path \}\}/(\S+)", text)
        self.assertTrue((action / relative).resolve().is_file(), relative)
        self.assertIn('python3 "$SCRIPT" --manifest "$MANIFEST"', text)


if __name__ == "__main__":
    unittest.main()
