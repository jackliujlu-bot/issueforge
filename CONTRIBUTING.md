# Contributing to IssueForge

Thanks for your interest. IssueForge is an OSS project; PRs are welcome,
but please skim this short doc first — it'll save us both time.

## Before you start

1. If you're trying to **use** IssueForge on your own repo and stuck,
   that's not a contribution; please file a *Porting help* issue instead.
2. If you've found a bug, please file a bug report (and ideally a failing
   test) before sending the fix PR. Bug reports without reproduction steps
   are hard to act on.
3. If you're proposing a substantial feature, please open a *Feature
   request* issue first to talk it through. Spending two days on a PR we
   can't merge is bad for everyone.

## Local dev setup

```bash
git clone https://github.com/<your-fork>/issueforge.git
cd issueforge

uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Sanity check
pytest -q                       # should be green
ruff check app tests            # should be clean
```

If you don't have `uv`, plain `pip install -e ".[dev]"` works too.

## The portability rule

The single hardest constraint to remember: **the worker code has zero
hardcoded references to any specific project**. There's a unit test that
enforces this:

```bash
pytest tests/test_config.py::test_no_hardcoded_repo_or_branch_in_app_code -v
```

If it fails on your branch, you've leaked a project-specific string into
`app/`. The fix is almost always to move that string into YAML and read
it from `AppConfig` instead.

## Running just one test

```bash
pytest tests/test_phase2.py -k "lint" -v
```

The full suite runs in ~5 seconds (it's hermetic — fakes for executor and
gh CLI; no network).

## Debugging the worker against your own repo

If you need to reproduce a bug in the actual end-to-end flow, the fastest
loop is:

```bash
# Terminal 1 — local Temporal
docker compose up -d temporal temporal-ui

# Terminal 2 — worker with debug logging
issueforge --log-level DEBUG worker --with-dispatcher

# Terminal 3 — dispatch a single issue
issueforge run-issue --issue 42 --wait
```

The worker writes structured logs to stderr by default. To inspect a
single round in detail without re-running, look at the on-disk artifacts:

```bash
issueforge artifact-path --issue 42 --repo myorg/widget
# /workspace/runs/myorg--widget--issue-42

cat $(issueforge artifact-path --issue 42 --repo myorg/widget)/handoff.md
```

## Code style

- `ruff format` + `ruff check` (configured in `pyproject.toml`).
- `mypy --strict` (in flight; some pre-existing debt).
- Use `structlog`-style key=value logging, not f-strings into the message.
- Type-hint everything. `from __future__ import annotations` at the top
  of every file is fine.
- Keep functions short. If you're writing more than ~50 lines and not
  iterating, refactor.

## Commit messages

Imperative mood, lowercase first word of the subject, body explains the
"why":

```
fix: planner no longer over-scopes docs-only issues

The PLAN_PROMPT_TEMPLATE didn't constrain subtasks to what the issue
literally asks for; the planner was adding "fix CI" tasks the agent
couldn't do, which the reviewer then enforced and the round failed.

Tightens the prompt with an explicit "Scope discipline" section and a
"Follow-ups" bucket the reviewer is told not to enforce.
```

## PR checklist

The PR template has the full list, but the recurring asks:

- [ ] No project-specific names / paths added to worker code (`rg -n
      'feipeng1234|/dimos|/home/' app/` empty for new lines).
- [ ] `pytest -q` green.
- [ ] `ruff check app tests` clean.
- [ ] If you changed a public CLI flag / YAML field / artifact path:
      `docs/PORTING.md` and `docs/ARCHITECTURE.md` updated.
- [ ] One topic per PR. Two unrelated improvements = two PRs.

## Releasing (maintainers)

We follow [Semantic Versioning](https://semver.org/):

- **Patch** (0.x.Y): bug fixes, doc changes, internal refactors that don't
  affect behavior.
- **Minor** (0.X.0): new features, new YAML fields with backward-compatible
  defaults, new CLI subcommands.
- **Major** (X.0.0): breaking config / CLI changes.

Release process:

1. Update `CHANGELOG.md` (move "Unreleased" → new version heading).
2. Bump `version` in `pyproject.toml`.
3. `git tag -s v0.x.y && git push --tags`.
4. The release workflow (when set up) builds + publishes to PyPI.

## Code of conduct

Be excellent to each other. Concretely: assume good faith, critique the
code not the person, when reviewing remember the contributor doesn't
have your context. We don't have a long-form CoC document yet; if a
specific incident requires one, file an issue and we'll write one.
