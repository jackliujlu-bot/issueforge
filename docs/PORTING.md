# Porting IssueForge to your own repository

> **Audience**: you've cloned IssueForge and want to point it at *your* GitHub
> repo, not at the example projects shipped in `configs/examples/`.

This guide is the only one you should need. By the end you'll have:

- A `configs/myproject.yaml` that describes your repo to the worker.
- Your repo on GitHub set up with the right labels + branch protection.
- The recommended `.github/workflows/ci.yml` + `auto-merge.yml` installed in
  *your* repo (copied from `recipes/github-workflows/`).
- A first agent-driven PR opened, CI green, auto-merged.

If anything in this guide doesn't work for your setup, that's a documentation
bug — please [open an issue](https://github.com/jackliujlu-bot/issueforge/issues).

---

## Mental model — what you actually have to provide

IssueForge is intentionally split into **two repositories**, and they have very
different jobs:

| Repo                                  | Who owns it             | What lives there                                  |
|---------------------------------------|-------------------------|---------------------------------------------------|
| **IssueForge** (this repo)            | platform team / you     | the worker process, prompts, retry loop, executors|
| **Your business repo** (e.g. `myorg/widget`) | application team        | your actual code, tests, CI, the issues to work on |

So this guide is mostly about three things:

1. **Telling the worker about your repo** — a YAML file in `configs/`.
2. **Setting up your repo to receive the worker's PRs** — labels, the two
   workflow YAMLs from `recipes/`, branch protection.
3. **Running the worker** — locally first, then long-running once you trust it.

Everything that varies between projects lives in (1). The worker's code has
**zero hardcoded references** to any specific repo — verified by a unit test
(`tests/test_config.py::test_no_hardcoded_repo_or_branch_in_app_code`).

---

## Prerequisites — what you need installed

| What                | Why                                                                | How                                                                                    |
|---------------------|--------------------------------------------------------------------|----------------------------------------------------------------------------------------|
| **Python ≥ 3.11**   | the worker is a Python package                                     | `pyenv install 3.12` or distro package                                                 |
| **uv** (recommended)| package manager IssueForge uses                                    | `curl -LsSf https://astral.sh/uv/install.sh | sh`                                      |
| **`gh` CLI**        | every GitHub call goes through `gh`                                | https://cli.github.com — then `gh auth login`                                          |
| **Temporal** (one of)| durable runtime — workflows survive worker restarts                | docker compose (recommended), OR `temporal` CLI for `temporal server start-dev`        |
| **A coding agent**  | the actual code-writing brain                                      | `cursor-agent` (default), or `claude`, or `codex` — must be on `$PATH`                 |
| **git ≥ 2.5**       | worktree-mode sandbox needs `git worktree`                         | distro package                                                                         |
| **Push access** to your business repo | the worker pushes branches and opens PRs                | confirm with `gh repo view <owner>/<repo>` and `gh repo --json viewerPermission`       |

You can run the **smoke test** below without any coding agent installed —
the stub executor will fill in for testing the plumbing.

---

## Step 0 — install IssueForge

```bash
git clone https://github.com/jackliujlu-bot/issueforge.git
cd issueforge
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Sanity check
pytest -q                      # should be all green
issueforge --help              # should print the CLI usage
```

---

## Step 1 — describe your repo (the only file you really write)

```bash
cp configs/examples/generic-python.yaml configs/myproject.yaml
$EDITOR configs/myproject.yaml
```

You only have to touch four places. Every other field has a sensible default.

```yaml
project:
  description: "<one-line description, prepended to the planner prompt>"

repo:
  owner: "<your-github-org>"        # e.g. "myorg"
  name:  "<your-repo>"              # e.g. "widget"
  base_branch: "main"               # or whatever your default branch is
  local_path: "/abs/path/to/repo"   # absolute path to your local clone

commands:
  setup:  [ "<how to install deps in a fresh checkout>" ]
  lint:   [ "<your lint command>" ]
  test:   [ "<your test command>" ]
```

The default config (`configs/default.yaml`) provides everything else. Don't
re-state values you don't want to change — the default is layered underneath
your overlay.

### Picking the right `commands.{setup,lint,test}`

This is the only project-specific judgment you have to make. Here's how to
think about it.

| If your project uses…  | `commands.setup` (one-shot per worktree)               | `commands.lint`                                                                                        | `commands.test`                                                          |
|------------------------|--------------------------------------------------------|--------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------|
| **uv**                 | `uv sync --frozen`                                     | `uv run pre-commit run --files {changed_files} \|\| uv run pre-commit run --files {changed_files}`     | `uv run pytest -x --timeout=60 {test_targets}`                           |
| **poetry**             | `poetry install --no-interaction --no-root`            | `poetry run pre-commit run --files {changed_files} \|\| poetry run pre-commit run --files {changed_files}` | `poetry run pytest -x --timeout=60 {test_targets}`                       |
| **pip + venv**         | `python -m venv .venv && .venv/bin/pip install -e .`   | `.venv/bin/ruff check {changed_py_files} && .venv/bin/ruff format --check {changed_py_files}`         | `.venv/bin/pytest -x --timeout=60 {test_targets}`                        |
| **pnpm / npm**         | `pnpm install --frozen-lockfile`                       | `pnpm exec biome check {changed_files}` *(or `pnpm exec eslint {changed_files}`)*                      | `pnpm exec vitest run --changed`                                         |
| **Go**                 | `go mod download`                                      | `gofmt -l {changed_files} \| (! grep .) && go vet ./...`                                              | `go test -count=1 ./...`                                                 |
| **Rust**               | `cargo fetch`                                          | `cargo fmt --check && cargo clippy --all-targets -- -D warnings`                                       | `cargo test --no-fail-fast`                                              |

#### Substitution tokens (the secret of fast local tests)

The strings inside `{...}` are expanded by IssueForge's tester *just before*
the command runs:

| Token                | Expands to                                                                |
|----------------------|---------------------------------------------------------------------------|
| `{changed_files}`    | every file the round modified (any extension)                             |
| `{changed_py_files}` | only the `*.py` paths from the round's diff                               |
| `{test_targets}`     | test files derived from the changed sources (sibling `test_<name>.py` lookup) |

**Empty token sets cause the command to *skip cleanly*** rather than fall
back to "run on the whole repo". This is what keeps each agent round under a
few seconds even on large monorepos. If you want to run the full suite, just
omit the token and pass an absolute path / pattern instead.

#### Two patterns that bite people

1. **Don't run the full test suite locally** unless your project is small.
   The agent has a wall-clock budget (typically `max_agent_rounds: 3`); a
   15-minute pytest run will eat most of it. Push real testing to GitHub
   Actions (the recipes in `recipes/github-workflows/ci.yml` make this fast).

2. **Run pre-commit twice in one shell** (`cmd || cmd`). Many pre-commit
   hooks auto-fix files and exit 1 when they do. The first invocation
   applies fixes, the second verifies the tree is now clean and exits 0.
   Without this, hooks like `ruff-format` or markdown link rewriters cause
   the local test to fail even though the agent is on the right track.
   See [Architecture decision: lint chain](#architecture-decision-lint-chain)
   below for the full story.

---

## Step 2 — set your business repo up to receive the worker's PRs

### 2.1 Create the agent labels

```bash
issueforge doctor --fix
```

`doctor --fix` reads `github.issue_label_*` from your YAML and creates whichever
ones are missing on the remote. By default the worker uses **hyphen-style**
labels (`agent-todo`, `agent-running`, …). If you'd rather use colon-style
(`agent:todo`), edit those lines in your YAML before running `doctor --fix`.

Verify:

```bash
gh label list --repo <owner>/<repo> | grep agent
```

You should see 12 labels (todo, queued, running, planning, coding, testing,
pr-created, ci-running, review, blocked, failed, done).

### 2.2 Install the recommended workflows in your business repo

The `recipes/` directory in this repo contains two GitHub Actions workflow
files that work *with* IssueForge's expectations. Copy them into your repo:

```bash
# from the root of your business repo:
mkdir -p .github/workflows
cp /path/to/issueforge/recipes/github-workflows/ci.yml         .github/workflows/ci.yml
cp /path/to/issueforge/recipes/github-workflows/auto-merge.yml .github/workflows/auto-merge.yml
git add .github/workflows && git commit -m "ci: add IssueForge-friendly CI + auto-merge"
git push
```

Read the headers of both files — they explain what each does and the small
tweaks you'll likely want (e.g. the Python version in `tests-changed`).

### 2.3 Set up branch protection

In your repo's settings → *Branches* → *Add rule for `main`*:

- ☑ Require status checks to pass before merging
  - Add `ci-complete` (the only check you need).
- ☑ Require branches to be up to date before merging *(optional)*
- *(leave the rest at their defaults)*

The point of `ci-complete` as the *single* required check is that it stays
green when `tests-changed` is correctly skipped (e.g. on docs-only PRs).
That's what makes the agent able to ship documentation without a contrived
"fake" passing test.

### 2.4 Repository settings

In *General → Pull Requests*:

- ☑ Allow squash merging (the auto-merge workflow uses `--squash`)
- ☑ Automatically delete head branches

In *Actions → General → Workflow permissions*:

- ☑ Read and write permissions
- ☑ Allow GitHub Actions to create and approve pull requests

These two are needed for `auto-merge.yml` to actually merge.

---

## Step 3 — bootstrap and verify

The worker has a single command that runs every check + a smoke test of the
agent loop on a real issue:

```bash
issueforge bootstrap
```

It does five phases in sequence; each one stops the chain on failure with
a specific actionable error:

| Phase     | What it does                                                                                                  |
|-----------|---------------------------------------------------------------------------------------------------------------|
| CONFIG    | Confirms `AGENT_WORKER_CONFIG` points at a valid YAML.                                                        |
| PREFLIGHT | 13 checks: `gh` auth, repo access, push permission, labels, local checkout, executor binary on `$PATH`, dirs writable. Auto-fixes what it can (creates labels, mkdir, clones the repo on first run if needed). |
| SMOKE     | Runs an offline planner round on a fake issue. Proves the LangGraph state machine works.                      |
| LIVE_READ | Pulls a real GitHub issue (auto-discovered or `--test-issue N`) and runs a planner round against your real codebase. **No comment is posted, no PR is opened.** Proves the agent can think about your code. |
| FULL      | (opt-in via `--full`) brings up Temporal + worker + dispatches a real workflow + opens a real PR.             |

Common combinations:

```bash
issueforge bootstrap                       # interactive, stops at LIVE_READ
issueforge bootstrap --yes                 # CI-style, no prompts
issueforge bootstrap --no-live-read        # if you have no issues yet
issueforge bootstrap --full                # take it all the way to a real PR
```

**Sample output of a successful run**:

```
✓ CONFIG: Using configs/myproject.yaml                            (0.0s)
✓ PREFLIGHT: all 13 checks passed                                 (3.5s)
✓ SMOKE: plan.md written (682 bytes)                              (0.2s)
✓ LIVE_READ: planned for #31 (4255 bytes)                        (21.4s)
· FULL: opt-in only

✓ Bootstrap complete.
```

---

## Step 4 — run for real

You have three usage modes. Pick the one that matches your maturity.

### Mode A — manual one-shot per issue (no Temporal)

Best for prompt engineering / debugging:

```bash
# Dispatch one issue, run all phases, print the result, exit:
issueforge run-once --issue 42
```

No Temporal needed. Won't survive a process crash mid-round. The PR (if
`workflow.stop_after: done`) is real.

### Mode B — long-running worker, manual dispatch (Temporal)

Best for "I want resilience but I'll tell it which issues to work on":

```bash
# Terminal 1 — bring up Temporal
docker compose up -d temporal temporal-ui

# Terminal 2 — start the worker process (no dispatcher)
issueforge worker

# Terminal 3 — dispatch issues you've labeled `agent-todo`
issueforge run-issue --issue 42 --wait
issueforge run-issue --issue 43 --wait
```

If the worker crashes, restart it; Temporal replays the workflow from where it
left off. The agent's per-issue state on disk (`runs/<owner>--<repo>--issue-<n>/`)
is also preserved.

### Mode C — fully automatic (Temporal + dispatcher)

Best for production: just label any issue `agent-todo` and forget about it.

```bash
docker compose up -d                  # temporal + worker (with dispatcher)
```

Or on the host:

```bash
docker compose up -d temporal temporal-ui
issueforge worker --with-dispatcher
```

The dispatcher polls `gh issue list --label agent-todo` every 30s and starts
a workflow per issue. It also recovers `agent-blocked` issues whose PR has
since reached a real CI verdict, and revives orphaned workflows after a
worker crash.

---

## Step 5 — tune the noisy bits for your repo

These are the levers you'll likely want to nudge once you've watched the
agent for a few rounds.

### `github.ci_ignore_workflows`

Some repos run "CI" workflows that aren't real code-quality gates — e.g.
"Auto Merge" workflows that poll for a 👍 reaction, or docs-preview deploys.
Without filtering, the worker waits on / reads the absence of those as a
failure and re-runs the coder.

Add their names to `ci_ignore_workflows` in your YAML (case-insensitive
substring match):

```yaml
github:
  ci_ignore_workflows:
    - "Auto Merge"
    - "Docs Preview"
```

### `workflow.max_agent_rounds`

Default is 3. If you find the agent regularly bumping into the cap on real
issues, raise it — but each round is one full plan→code→test→review cycle,
so it does cost wall-clock and tokens.

### `workflow.ci_max_wait_seconds`

How long the worker will wait for GitHub CI to complete before parking the
issue as `agent-blocked`. The dispatcher will pick parked issues up again
on subsequent cycles, so this is more about "don't burn worker CPU spinning
on a stuck CI" than about hard timeouts.

---

## Architecture decision: lint chain

You will see this pattern in the example configs and probably want it in
your own:

```yaml
commands:
  lint:
    - "uv run pre-commit run --files {changed_files} || uv run pre-commit run --files {changed_files}"
```

**Why two invocations chained with `||`?** Most pre-commit hooks (ruff-format,
trailing-whitespace, end-of-file-fixer, markdown link rewriters, license
header insertion, …) **auto-fix files and then exit 1**. That's
`pre-commit`'s convention: "I changed something; the commit is not clean
until you re-run me."

If you only run pre-commit once locally, the agent's round looks like a
failure even though the agent did the right thing — and once the PR is
pushed, the GitHub Actions `lint` job (which is also `pre-commit`) sees the
*unfixed* tree, exits 1, and the agent-PR ships red.

Running twice with `||` is the elegant fix:

1. First invocation applies fixes (exit 1).
2. Second invocation verifies the tree is now clean (exit 0).
3. The shell `||` short-circuits to the second's exit code → tester sees
   PASS.
4. IssueForge's deliverer commits via `git add -A`, so any auto-fix from
   step 1 is included in the agent's commit.
5. GitHub's `lint` job sees a tree pre-commit is already happy with → green.

If `pre-commit` legitimately fails on something a hook can't auto-fix, the
**second** invocation will exit non-zero too (the hook reports the failure
identically across both runs), and the chain correctly returns failure.

---

## Architecture decision: test command

You will see this in the example configs:

```yaml
commands:
  test:
    - "uv run python -c 'import myproject'"
    - "uv run pytest -x --timeout=60 dimos/types/"
```

…and not just `uv run pytest -x`. Why?

The agent has a wall-clock budget (default `max_agent_rounds=3`, with each
round capped by per-step timeouts). On a project of any meaningful size,
running the full suite per round means each round takes 5–20 min and the
agent can do at most one or two attempts before bumping into the cap.

Three honest options:

1. **Skip local tests entirely**: `commands.test: []`. Rely on GitHub
   Actions as the merge gate. Honest, fast, but the reviewer doesn't have
   diff-correlated test evidence to look at.

2. **Tiny smoke set**: an `import` check plus a small directory of fast,
   no-side-effect tests. The example above is exactly this — `import dimos`
   catches "the agent broke the package" in 60 ms, and `dimos/types/` is a
   pure-Python type-primitives test directory that runs in ~2 s.

3. **`{test_targets}` substitution**: only run the tests sibling to changed
   sources. Fast on small to medium diffs; expands to a lot of files on
   wide refactors.

Pick one of (2) or (3). Don't run the full suite locally just because you
can — it's a real cost.

---

## Cheat sheet — useful commands

```bash
# Print the merged config (default + project YAML + env)
issueforge show-config

# List which executor backends the worker can see
issueforge list-executors

# Print the on-disk artifact dir for one issue
issueforge artifact-path --issue 42 --repo myorg/widget

# Start one-off run, no Temporal, no PR push (dry run)
issueforge run-once --issue 42 --dry-run

# Real run, real PR
issueforge run-issue --issue 42 --wait

# Long-running mode
issueforge worker --with-dispatcher

# Single dispatcher cycle, JSON summary, exit
issueforge dispatcher --once
```

---

## When something goes wrong

In rough order of likelihood:

| Symptom                                   | What to check                                                                                                    |
|-------------------------------------------|------------------------------------------------------------------------------------------------------------------|
| `gh: command not found`                   | Install GitHub CLI; `gh auth login` (≠ `gh auth setup-git`).                                                     |
| `cursor-agent: command not found`         | Set `CURSOR_AGENT_BIN` in `.env`, or change `executor.default` to `claude_code`/`codex`.                          |
| Temporal connection refused               | `docker compose ps` → are `temporal` + `temporal-ui` healthy? Tail `docker compose logs temporal`.               |
| Worker silent after start                 | The dispatcher only logs INFO when it has work to do; quiet cycles are DEBUG. Tail with `--log-level DEBUG`.     |
| All issues fail with "missing label"      | `issueforge doctor --fix` — creates the missing labels.                                                          |
| Issue stuck on `agent-planning` for 10+ min | Check Temporal UI (http://localhost:8233) for the workflow — it might be waiting on a hung activity. `temporal workflow terminate` will reset it; the dispatcher's orphan-revival picks it up next cycle. |
| Lint passes locally but GitHub `lint` fails | You probably aren't running pre-commit. See [the lint chain section](#architecture-decision-lint-chain).         |
| Tests pass locally but GitHub tests fail  | The local set is intentionally small (see [the test section](#architecture-decision-test-command)). The slow parts are on GitHub. |
| Auto-merge.yml never fires                | (1) Check *Actions* permissions in the repo: needs read+write. (2) PR must be from `agent/*` or have `auto-merge` label. (3) Check the workflow's logs in *Actions* tab. |

If none of those help, open an issue with:

- The output of `issueforge show-config` (redact tokens).
- The contents of `runs/<owner>--<repo>--issue-<n>/handoff.md` if a round failed.
- The Temporal workflow ID (UI shows it) and the recent `worker.log` lines.

Welcome aboard.
