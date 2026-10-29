# Central workflows

Shared GitHub Actions workflows for Smile Identity's public and private repositories.

- `auto-author-assign.yml` adds the PR author as an assignee when a PR opens or reopens. It skips bot authors and keeps existing assignees.
- `stale.yml` labels PRs after 14 days without activity and removes the label when activity resumes. It does not mark issues or close issues or PRs.
- `deploy-status.yml` records what each environment is running. After a deploy it updates a badge for that environment on the calling repository's `deploy-status` branch and, if given a Slack token and canvas, that environment's line on a shared Slack canvas.

`Check shared PR workflows` calls this repository's workflows too, pinned to the same commit as the distributed callers. It assigns PR authors and supports manual dry runs of stale handling.

## Use the workflows

Each consuming repository needs a caller in `.github/workflows/` with its own event or schedule. The caller jobs use:

```yaml
jobs:
  assign:
    uses: smileidentity/central-workflows/.github/workflows/auto-author-assign.yml@<full commit SHA from main>
```

```yaml
jobs:
  stale:
    uses: smileidentity/central-workflows/.github/workflows/stale.yml@<full commit SHA from main>
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
    uses: smileidentity/central-workflows/.github/workflows/deploy-status.yml@<full commit SHA from main>
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

## Releases

Callers pin each workflow to a full commit SHA from `main`, as we do for third-party actions. A change reaches a caller only when its SHA is updated. The internal distribution job updates the author and stale callers; deploy-status callers are updated by a PR in each repository.

Third-party actions inside these workflows are pinned to commit SHAs too. Keep this public repository free of credentials and private configuration.

## Check changes

Run `actionlint .github/workflows/*.yml` and `tests/deploy-status.sh` (needs `yq` and `xmllint`). Test author assignment with a PR and run the stale workflow manually with `dry-run` enabled before updating callers to the new commit.
