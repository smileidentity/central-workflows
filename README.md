# Central workflows

Shared GitHub Actions workflows for Smile Identity's public and private repositories.

- `auto-author-assign.yml` adds the PR author as an assignee when a PR opens or reopens. It skips bot authors and keeps existing assignees.
- `stale.yml` labels PRs after 14 days without activity and removes the label when activity resumes. It does not mark issues or close issues or PRs.
- `deploy-status.yml` records what each environment is running. After a deploy it updates a badge for that environment on the calling repository's `deploy-status` branch and, if given a Slack token and canvas, that environment's line on a shared Slack canvas.
- `sdk-spec-sync-record.yml` and `sdk-spec-sync.yml` keep the V3 server-side SDKs in step with the API spec. They run in this repository only; nothing calls them. See [Sync the SDKs with the API spec](#sync-the-sdks-with-the-api-spec).
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

## Sync the SDKs with the API spec

When a lambda release changes the SDK spec, lambda sends a `sdk-spec-changed` repository dispatch here with the tag, the tag it was compared with, an `oasdiff` change summary, whether the change is breaking, and the lambda PR numbers.

1. `sdk-spec-sync-record.yml` checks the payload and stores it as `state/pending/<tag>.json` on the `spec-sync-state` branch. It does nothing else, so many releases in a day cause no noise in the SDKs.
2. `sdk-spec-sync.yml` runs at 06:00 UTC each day. It runs one job per repository: the seven `smileid-sdk-*` SDKs and `smileid-sdk-fixtures`. Each job gives Claude (`anthropics/claude-code-action`, on Amazon Bedrock) the repository's `AGENTS.md`, the public spec `api-reference/specs/v3/openapi.yaml` at the old and new tags (api-reference only tags releases that change the public spec, so each lambda tag maps to the newest api-reference tag at or before it), the change summaries and the lambda PR descriptions. Claude makes the change with tests, golden fixtures and coverage, and runs the repository's checks. The prompt is `scripts/spec_sync/prompt.md`.
3. A separate publish job, on a fresh runner, clones the repository again and checks the change (no edits under `.github/`, no coverage exclusions, no conflict markers, and the branch hasn't moved since Claude started). It then commits it as `SMILE_GHA_BOT` and updates the repository's open `spec-sync/rolling` PR, or opens one as a draft labelled `spec-sync`. Each breaking release gets its own draft PR on `spec-sync/breaking-<tag>`, also labelled `breaking`. Each run rewrites the PR description; it never comments.
4. When every job succeeds, the records move to `state/processed/`. If any job fails, they stay pending and the next run tries again. A job where Claude reports it is blocked still succeeds: its PR lists the gaps and stays a draft for a person to finish, and the records are processed.
5. Every hour, the workflow marks draft spec-sync PRs ready for review once all their checks pass and Claude reported no gaps. It also rewrites the pinned "SDK spec sync status" issue, which shows each repository's open spec-sync PRs, spec tag and last run result.

Claude never shares a runner with a write token or with `SMILE_GHA_BOT`'s private key. Its job gets read-only tokens from a separate app, `SPEC_SYNC_READER`, and the Bedrock credentials. The token steps don't revoke their tokens, which expire after an hour. Before Claude starts, the job turns off passwordless sudo and stops Docker. The runner process holds the job's secrets in memory, and without root, Claude's Bash can't read it. When Claude finishes, a step with no token or AWS credential in its environment bundles the change as a binary patch, running git with hooks and fsmonitor turned off. The bundle is encrypted to the certificate in `sdk-spec-sync.yml`, because this repository's artifacts are public and two of the target repositories are private. The publish job runs this repository's own scripts, decrypts the bundle with `SPEC_SYNC_BUNDLE_KEY`, and accepts only plain files. It checks that the bundle is for its own repository and branch, and applies the patch to a fresh clone.

A compromised Claude step can propose a bad change, and the same checks and a person's review apply to it as to any other spec-sync PR. It can also use what its own runner holds until the job ends: read tokens for its SDK, lambda and fixtures, which expire within an hour, and Bedrock credentials that can only call Claude. If it got round the root lockdown, it could read the reader app's key, which still only reads. That assumes GitHub sends a runner only the secrets its own job references. If the runner also received the secrets used by the publish and status jobs, the root lockdown would be the only thing keeping `SMILE_GHA_BOT`'s key from Claude. It can't push or approve anything. Claude's output is not printed, because this repository's logs are public. Lambda PR descriptions are read on the runner and never published. The change summaries on the `spec-sync-state` branch come from `v3-sdk.yaml`. That spec is the public spec with some parts removed, so the summaries describe only what api-reference already shows.

To clear a backlog, run `SDK spec sync` by hand with `from_tag` set to the spec tag the SDKs already match. Add `scope`, such as `AML operations`, to open a separate `spec-sync/<scope>` PR for one part of the spec. Use `repos`, such as `go`, to try one repository; pending records then stay pending.

Claude runs on Smile ID's Amazon Bedrock account, so there is no Anthropic API key. The sync job swaps GitHub's OIDC token for AWS credentials that last 3 hours and can only call Claude.

The workflows need these secrets and variables. Until they exist, the daily run skips with a warning that names what is missing, and records stay pending.

- `SPEC_SYNC_BEDROCK_ROLE_ARN` (variable): an IAM role in the Bedrock account, set up like `sievals-ci-bedrock`.
  - Its trust policy allows `token.actions.githubusercontent.com` with `aud` `sts.amazonaws.com` and `sub` exactly `repo:smileidentity@24418837/central-workflows@1360252165:ref:refs/heads/main`. This repository uses GitHub's immutable subject format, with the owner and repository IDs, so `repo:smileidentity/central-workflows:...` never matches. This repository is public, so the role must not trust pull requests, other branches or a wildcard.
  - Its only policy allows `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on the model in `SDK_SPEC_SYNC_MODEL` (the inference profile and the foundation models behind it), `bedrock:GetInferenceProfile` on that profile, and `bedrock:ListInferenceProfiles`, which Claude Code uses at startup. Claude Code's background requests to its smaller model use the same model.
  - Its maximum session duration is at least 3 hours, because a sync job can run for 2.5 hours.
  - Claude's step can request its own OIDC token, so no other AWS role may trust this repository.
- `SPEC_SYNC_BEDROCK_REGION` (variable): the region the role calls Bedrock in, where access to the model is enabled.
- `SDK_SPEC_SYNC_MODEL` (variable): the Bedrock inference profile ID of the Claude model, such as `global.anthropic.claude-...`. When it changes, update the role's policy first, or the sync fails with `AccessDeniedException`.
- `SPEC_SYNC_READER_CLIENT_ID` and `SPEC_SYNC_READER_PRIVATE_KEY` (secrets): a GitHub App used only by Claude's job. Give it contents read and pull requests read access, and nothing else, and install it on the SDK, fixtures and lambda repositories.
- `SMILE_GHA_BOT_CLIENT_ID` and `SMILE_GHA_BOT_PRIVATE_KEY` (organisation secrets): used only by the publish and status jobs. An organisation admin must give this repository access to them. The app must be installed on the SDK, fixtures and lambda repositories. It needs contents, pull requests and issues write access on the SDK and fixtures repositories, checks and commit statuses read access, and pull requests read access on lambda.
- `SPEC_SYNC_BUNDLE_KEY` (secret): the private key that matches `BUNDLE_CERT` in `sdk-spec-sync.yml`. To replace both, run `openssl req -x509 -newkey rsa:4096 -nodes -keyout key.pem -out cert.pem -days 3650 -subj "/CN=central-workflows spec-sync bundle"`. Paste `cert.pem` into the workflow, store `key.pem` as the secret, then delete it.

The `spec-sync` and `breaking` labels must exist in every target repository, including `smileid-sdk-fixtures`.

Merge order: smileid-sdk-rust#2 and #3, and smileid-sdk-fixtures#1, before the Bedrock variables are set. Merge this workflow before lambda#5217, or lambda's dispatches are lost until a `from_tag` run.

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

Run `actionlint .github/workflows/*.yml`, `tests/deploy-status.sh` (needs `yq` and `xmllint`), `tests/merge-safety.sh` (needs `yq` and `jq`), `python3 -m unittest discover -s tests` and `tests/spec-sync-state.sh`. The `Check workflows` workflow runs them on every PR. Before updating callers to a new commit, test it here. On a branch, change the pins in `pr-hygiene.yml` to that commit and run `Check shared PR workflows` manually from that branch with `dry-run` enabled. Author assignment uses `pull_request_target`, so it runs from `main`: merge the pin change, then open a PR to check it.
