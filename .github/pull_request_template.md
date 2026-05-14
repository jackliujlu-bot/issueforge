<!--
Thanks for sending a PR! A few things to keep in mind:

- Keep the change focused. If you've found two unrelated improvements,
  split them into two PRs — they're easier to review and easier to revert.
- IssueForge has a hard portability rule: nothing project-specific belongs
  in the worker code. If you find yourself adding a string like a repo
  name, a branch name, or a specific command, that belongs in YAML.
- The CI gate is `ci-complete` (lint + tests). It must be green for the PR
  to be considered.
-->

## What this changes

<!-- One-paragraph summary of the change. -->

## Why

<!-- The problem you're solving. The "why" matters more than the "what"
     for review. -->

## Behavior before / after

<!-- If user-visible: what would a user notice differently? Worker logs,
     CLI output, label transitions, YAML schema, etc. -->

## How I tested it

<!-- pytest output, manual run trace, screenshots — anything that gives
     reviewers confidence. If you didn't add a test, say why. -->

## Checklist

- [ ] No project-specific names / paths added to worker code (`rg -n 'feipeng1234|/dimos|/home/' app/` is empty for new lines).
- [ ] `pytest -q` passes locally.
- [ ] `ruff check app tests` is clean.
- [ ] If you changed a public CLI flag / YAML field / artifact path: docs/PORTING.md and docs/ARCHITECTURE.md are updated.
- [ ] If you changed configuration loading: `issueforge show-config` still prints sensible output.
