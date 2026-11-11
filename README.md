# Central workflows

Shared GitHub Actions workflows for Smile Identity's public and private repositories.

- `auto-author-assign.yml` adds the PR author as an assignee when a PR opens or reopens. It skips bot authors and keeps existing assignees.
- `stale.yml` labels PRs after 14 days without activity and removes the label when activity resumes. It does not mark issues or close issues or PRs.
- `deploy-status.yml` records what each environment is running. After a deploy it updates a badge for that environment on the calling repository's `deploy-status` branch and, if given a Slack token and canvas, that environment's line on a shared Slack canvas.
- `merge-safety.yml` sorts each PR into a risk class from its changed files and author, and shows it as a `merge-safety/<class>` label and a job summary. It is advisory and always passes.

`Check shared PR workflows` calls this repository's workflows too, pinned by commit SHA like any other caller. Update its pins when you update the distributed callers. It assigns PR authors and supports manual dry runs of stale handling.

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

## Merge safety

`merge-safety.yml` has no caller. An org ruleset with a "require workflow" rule runs it on every PR, pinned to a commit SHA from `main`. The ruleset is in evaluate mode, so GitHub runs the workflow but never blocks a merge on it.

It reads the PR's file list and commits through the API and never checks out PR code. It gives each PR one class:

| Class | When |
|---|---|
| `docs-only` | Every file is documentation: Markdown and other prose anywhere, or images directly under `docs/` |
| `tests-only` | Every file is a test, fixture or documentation |
| `deps-patch`, `deps-minor` | Dependabot opened it, every commit is authored by Dependabot and signed by GitHub, and it changes only manifests, lockfiles and workflow pins. The class is the largest update type |
| `deps-major` | As above, with a major update or an update with no stated type, such as a bump to a commit SHA |
| `ci-config` | It changes `.github/`, other CI files or CODEOWNERS, plus at most tests and documentation |
| `code` | Anything else, including dependency changes by a person, instructions for coding agents such as `AGENTS.md` or `.github/copilot-instructions.md`, MDX, which can run code when a docs site builds, and code under `docs/` |
| `unknown` | The file list could not be read in full |

A file that is renamed counts under its old and new paths, so moving code into `docs/` is still a code change. Fork PRs get the summary but no label, because their token is read-only.

The classes are a first cut. Their labels over a few weeks show which ones are safe to auto-merge. Anyone who can triage can change a label, so anything that acts on the class must read this job's result for the PR's head commit, not the label.

Before the ruleset goes from evaluate to active, add a `merge_group` trigger, or repositories that use a merge queue cannot merge. Ruleset workflows run only on the default `pull_request` activity types, so a PR whose base branch changes keeps its old class until its next push.

## Releases

Callers pin each workflow to a full commit SHA from `main`, as we do for third-party actions. A change reaches a caller only when its SHA is updated. The internal distribution job updates the author and stale callers; deploy-status callers are updated by a PR in each repository. The `v1` branch is no longer updated.

Third-party actions inside these workflows are pinned to commit SHAs too. Keep this public repository free of credentials and private configuration.

## Check changes

Run `actionlint .github/workflows/*.yml`, `tests/deploy-status.sh` (needs `yq` and `xmllint`) and `tests/merge-safety.sh` (needs `yq` and `jq`). Before updating callers to a new commit, test it here. On a branch, change the pins in `pr-hygiene.yml` to that commit and run `Check shared PR workflows` manually from that branch with `dry-run` enabled. Author assignment uses `pull_request_target`, so it runs from `main`: merge the pin change, then open a PR to check it.
