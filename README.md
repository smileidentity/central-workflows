# Central workflows

Shared GitHub Actions workflows for Smile Identity's public and private repositories.

- `auto-author-assign.yml` adds the PR author as an assignee when a PR opens or reopens. It skips bot authors and keeps existing assignees.
- `stale.yml` labels PRs after 14 days without activity and removes the label when activity resumes. It does not mark issues or close issues or PRs.

`Check shared PR workflows` calls the released workflows in this repository too. It assigns PR authors and supports manual dry runs of stale handling.

## Use the workflows

Each consuming repository needs a caller in `.github/workflows/` with its own event or schedule. The caller jobs use:

```yaml
jobs:
  assign:
    uses: smileidentity/central-workflows/.github/workflows/auto-author-assign.yml@v1
```

```yaml
jobs:
  stale:
    uses: smileidentity/central-workflows/.github/workflows/stale.yml@v1
```

The author caller needs `pull-requests: write`. The stale caller needs `issues: write` and `pull-requests: write`. No shared secrets are required: each workflow uses the calling repository's `GITHUB_TOKEN`.

An internal distribution job holds the complete caller templates and opens PRs to distribute them. It excludes this repository so it cannot replace these implementations with callers to themselves.

Repository and organisation Actions policies must allow these workflows and the actions they use. The author workflow uses `pull_request_target` only for metadata operations; never add a checkout of PR code or execute code from a PR.

## Releases

`v1` is the maintained release branch. Update it to a tested commit from `main` for backwards-compatible fixes. Callers receive those fixes without a rollout PR. Changes to caller events, schedules or permissions need a new distribution PR.

Third-party actions are pinned to commit SHAs. Keep this public repository free of credentials and private configuration.

## Check changes

Run `actionlint .github/workflows/*.yml`. Test author assignment with a PR and run the stale workflow manually with `dry-run` enabled before advancing `v1`.
