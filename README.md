# Central workflows

Shared GitHub Actions workflows for Smile Identity's public and private repositories.

- `auto-author-assign.yml` adds the PR author as an assignee when a PR opens or reopens. It skips bot authors and keeps existing assignees.
- `stale.yml` labels PRs after 14 days without activity and removes the label when activity resumes. It does not mark issues or close issues or PRs.
- `deploy-status.yml` records what each environment is running. After a deploy it updates a badge for that environment on the calling repository's `deploy-status` branch and, if given a Slack token and canvas, that environment's line on a shared Slack canvas.
- `scheduled-run-status.yml` keeps one issue open while a scheduled workflow fails. A failed run opens the issue or comments on it, and the next passing run closes it. Given a Slack webhook, it posts each change to Slack too.

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

## Record deploy status

Add a job after the deploy job, so it runs only when the deploy succeeds:

```yaml
  deploy-status:
    needs: deploy
    permissions:
      contents: write
    uses: smileidentity/central-workflows/.github/workflows/deploy-status.yml@v1
    with:
      environment: production
      ref: ${{ github.ref_name }}
      sha: ${{ github.sha }}
      canvas-id: ${{ vars.DEPLOY_STATUS_CANVAS_ID }}
    secrets:
      slack-token: ${{ secrets.SLACK_BOT_TOKEN }}
```

Pass the ref and commit that were actually deployed, which may differ from the ref the workflow ran from.

The badge for each environment is `<environment>.svg` on the `deploy-status` branch. A tag shows green and a branch shows orange. Show it in a private repository's README with:

```markdown
[![production](https://github.com/OWNER/REPO/blob/deploy-status/production.svg?raw=true)](https://github.com/OWNER/REPO/deployments/production)
```

The image only loads for people who can read the repository, and only on github.com. The link goes to the environment's deployment history, which exists when the deploy job uses a GitHub environment of that name; otherwise link to the deploy workflow instead.

The Slack step needs a bot token with the `canvases:read` and `canvases:write` scopes, and the bot needs edit access to the canvas. Each environment gets one line under a heading for its repository. The workflow adds the heading and the line on the first deploy and replaces the line after that. A Slack failure shows as a warning and does not fail the job.

## Report scheduled run status

Add a job at the end of the scheduled workflow that needs every other job:

```yaml
  report:
    needs: [build, test]
    if: ${{ !cancelled() }}
    permissions:
      issues: write
    uses: smileidentity/central-workflows/.github/workflows/scheduled-run-status.yml@v1
    with:
      failed: ${{ contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled') }}
      team: team:api
    secrets:
      slack-webhook-url: ${{ secrets.NIGHTLY_SLACK_WEBHOOK_URL }}
```

List every job in `needs`: a job left out cannot turn the report red. `failed` counts cancelled jobs as well as failed ones, so a job that stops part-way still opens an issue. `!cancelled()` skips the report when someone cancels the whole run.

The job only acts on `schedule` and `workflow_dispatch` runs of the default branch, so the same workflow can also run on pull requests. A manual run on another branch leaves the issue alone.

It finds the issue by the label `red-lane:<workflow file name without extension>`, and adds the `team` label when it opens one. It creates both labels if they do not exist. A name too long for GitHub's 50-character limit is cut and ends in a short hash of the full name. Do not remove the `red-lane` label from an open issue, or the next failure opens a second one. If you rename the workflow file, close its open issue by hand.

The caller job needs `issues: write`, which works even when the workflow sets tighter defaults. The workflow uses the calling repository's `GITHUB_TOKEN`. The Slack webhook is optional: leave out `secrets` to skip Slack. A Slack failure shows as a warning and does not fail the job.

## Releases



`v1` is the maintained release branch. Update it to a tested commit from `main` for backwards-compatible fixes. Callers receive those fixes without a rollout PR. Changes to caller events, schedules or permissions need a new distribution PR.

Third-party actions are pinned to commit SHAs. Keep this public repository free of credentials and private configuration.

## Check changes

Run `actionlint .github/workflows/*.yml`, `tests/deploy-status.sh` (needs `yq` and `xmllint`) and `tests/scheduled-run-status.sh` (needs `yq` and `jq`). Test author assignment with a PR and run the stale workflow manually with `dry-run` enabled before advancing `v1`.
