# Recipes — drop-in files for the repo IssueForge operates on

These files belong to the **business repository** (the one IssueForge writes
PRs against), not to the IssueForge worker repo itself. Copy them as-is —
nothing here is hardcoded to a specific project. Read each file's header to
see what (if anything) you need to tweak.

## What's here

```
github-workflows/
├── ci.yml          ← copy to <your-repo>/.github/workflows/ci.yml
└── auto-merge.yml  ← copy to <your-repo>/.github/workflows/auto-merge.yml
```

### `ci.yml` — the lean CI gate IssueForge plays well with

Three jobs:

1. `lint` — `pre-commit` + (optional) `mypy`.
2. `tests-changed` — `pytest` only on tests sibling to the `.py` files the PR
   actually changes. Skips entirely when no `.py` changed.
3. `ci-complete` — aggregate status check; this is the one your branch
   protection rule should target.

Why this shape: see the comment at the top of `ci.yml`. TL;DR — heavy CI
matrices (5+ Python versions, codecov upload, self-hosted runners,
fail-fast) frequently go red for reasons orthogonal to the agent's diff,
and IssueForge will burn retries trying to "fix" them.

### `auto-merge.yml` — squash-merge once CI is green

Auto-merges PRs whose head branch is `agent/*` (i.e. IssueForge-opened) or
that carry the `auto-merge` label. Triggered by `workflow_run` on `ci`'s
completion, so it only fires after CI is genuinely green.

## How to install in your repo

```bash
# from the root of your business repo:
mkdir -p .github/workflows
curl -fsSL https://raw.githubusercontent.com/jackliujlu-bot/issueforge/main/recipes/github-workflows/ci.yml \
    > .github/workflows/ci.yml
curl -fsSL https://raw.githubusercontent.com/jackliujlu-bot/issueforge/main/recipes/github-workflows/auto-merge.yml \
    > .github/workflows/auto-merge.yml
git add .github/workflows && git commit -m "ci: add IssueForge-friendly CI + auto-merge"
```

Or just clone IssueForge and `cp` from `recipes/`.

## After installing

1. **Branch protection**: in your repo settings, set "Require status checks
   to pass before merging" with `ci-complete` as the only required check.
   That keeps `tests-changed` skipping cleanly on docs-only PRs.
2. **Repo settings**: in *General → Pull Requests*, enable "Allow squash
   merging" (auto-merge.yml uses `--squash`) and check "Automatically
   delete head branches".
3. **Test it**: open a small PR, watch the workflow run, confirm
   `ci-complete` goes green. Then open one with the `auto-merge` label and
   confirm it self-merges.

See [docs/PORTING.md](../docs/PORTING.md) in the IssueForge repo for the
full end-to-end porting guide.
