# IssueForge

> **A long-running coding agent that turns GitHub issues into merged PRs.**
>
> Label an issue `agent-todo`. IssueForge plans the work, runs your coding
> agent (Cursor / Claude Code / Codex), opens a PR, watches CI, fixes what
> fails, and merges the result. All driven by labels on the issue.

```text
GitHub Issue (agent-todo)
    │
    ▼
Temporal Workflow ── durable, survives crashes ──┐
    │                                            │
    ▼                                            │
LangGraph agent loop  (planner → coder → tester → reviewer → deliverer)
    │                                            │
    ▼                                            │
Cursor / Claude / Codex   (pluggable, swap with one config line)
    │                                            │
    ▼                                            │
git worktree   (isolated; never touches your main checkout)
    │                                            │
    ▼                                            │
Pull Request ─→ GitHub Actions CI ─→ auto-merge ─┘
```

[![CI](https://github.com/jackliujlu-bot/issueforge/actions/workflows/ci.yml/badge.svg)](https://github.com/jackliujlu-bot/issueforge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](pyproject.toml)

---

## Why does this exist

A coding agent that runs in your IDE for ten minutes is one tool. A coding
agent that runs for a week against a real codebase, surviving crashes and
flaky CI and conflicting reviewer feedback, is a different tool. IssueForge
is the second tool.

What it brings on top of "just run `cursor-agent` in a loop":

- **Durable runtime** — a process kill mid-PR-creation doesn't lose the
  work. Temporal owns the workflow, LangGraph owns the per-round
  reasoning, the agent's plans/diffs/tests live in
  `runs/<repo>--issue-<N>/`. Lose any one of the three and the others
  reconstruct it.
- **CI-aware** — when the PR's CI goes red, IssueForge babysits the loop:
  reads the failure, lets the agent retry, gives up cleanly after N rounds.
- **Truly portable** — there are zero hardcoded references to any specific
  project in the worker code. Asserted by a unit test
  ([`test_no_hardcoded_repo_or_branch_in_app_code`](tests/test_config.py)).
  All project-specific logic — repo name, lint command, test command,
  ignored CI workflows — lives in one YAML file.
- **Pluggable agent backend** — `executor.default: cursor` → `claude_code`
  → `codex` → your-custom-thing-here is one config line. New backends
  drop a file into `app/executors/`.

---

## Quickstart

This is the *short* version. The complete walkthrough — including how to
adapt it to your own repo's lint and test commands — is in
**[docs/PORTING.md](docs/PORTING.md)**.

### 1. Install

```bash
git clone https://github.com/jackliujlu-bot/issueforge.git
cd issueforge
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

You'll also need:

- `gh` CLI logged in (`gh auth login`)
- A coding agent on `$PATH` (`cursor-agent`, or set `executor.default` to
  `claude_code` / `codex`)
- For the full Temporal-backed mode: Docker

### 2. Point it at your repo

```bash
cp configs/examples/generic-python.yaml configs/myproject.yaml
$EDITOR configs/myproject.yaml          # fill in 4 placeholders
echo "AGENT_WORKER_CONFIG=configs/myproject.yaml" > .env
```

The four placeholders: `repo.owner`, `repo.name`, `repo.local_path`, and
your project's `commands.{setup,lint,test}`. See
[PORTING.md](docs/PORTING.md#picking-the-right-commandssetuplinttest) for
ready-to-use values for `uv` / `poetry` / `pip` / `pnpm` / `Go` / `Rust`.

### 3. Bootstrap

```bash
issueforge bootstrap
```

This runs five phases (config check → 13-point preflight → offline
plumbing smoke → real-issue planner test → optional full Temporal run).
Each phase prints `✓` or a specific actionable error. There are no other
commands to memorise — re-run `bootstrap` after fixing whatever it complained
about.

```text
✓ CONFIG: Using configs/myproject.yaml                  (0.0s)
✓ PREFLIGHT: all 13 checks passed                       (3.5s)
✓ SMOKE: plan.md written (682 bytes)                    (0.2s)
✓ LIVE_READ: planned for #31 (4255 bytes)              (21.4s)
· FULL: opt-in only

✓ Bootstrap complete.
```

### 4. Run for real

```bash
# One-shot: pick one issue, run the whole pipeline, print the result
issueforge run-once --issue 42

# Long-running: watch GitHub for `agent-todo` issues
docker compose up -d                 # Temporal + worker + dispatcher
# OR on the host:
issueforge worker --with-dispatcher
```

Open the Temporal UI at <http://localhost:8233> to watch workflows.

### 5. (Highly recommended) install the recipe workflows in your repo

`recipes/github-workflows/` contains two pre-vetted GitHub Actions workflow
files that play well with IssueForge:

- `ci.yml` — a lean lint + tests-only-on-changed-files gate (avoids the
  matrix/codecov/self-hosted noise that makes the agent burn retries on
  things it can't fix).
- `auto-merge.yml` — squash-merges PRs whose head is `agent/*` once CI
  goes green, so IssueForge's PRs land hand-free.

```bash
cp -r recipes/github-workflows/. /path/to/your/repo/.github/workflows/
```

See [recipes/README.md](recipes/README.md) for the post-install checklist
(branch protection, repo settings).

---

## What it actually does, command by command

The full label state machine on the issue:

```
agent-todo
   ↓ dispatcher picks it up
agent-running → agent-planning → agent-coding → agent-testing
   ↓
agent-pr-created → agent-ci-running
   ↓
agent-done   (success: PR merged)
agent-failed (gave up after max_agent_rounds; review the run/ dir)
agent-blocked (waiting on human signal — apply `auto-merge` label to ship it)
```

All label names are configurable. The worker reads them from
`github.issue_label_*` in your YAML; nothing is baked into the code.

---

## Documentation

- **[docs/PORTING.md](docs/PORTING.md)** — the porting guide. Read this first
  if you want to use IssueForge on your own repo.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — internals: the three
  recovery layers, boundary discipline, extension points.
- **[docs/usage.md](docs/usage.md)** — older Chinese-language usage manual,
  more depth on individual commands and CLI flags.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — developer setup, how to debug,
  how to send a PR.
- **[CHANGELOG.md](CHANGELOG.md)** — release notes.

---

## Status

| Phase | Scope                                                          | Status        |
|-------|----------------------------------------------------------------|---------------|
| 1     | Issue → Temporal → planner → comment plan                      | ✅ implemented |
| 2     | git worktree + coding agent edits + local tests                | ✅ implemented |
| 3     | Branch / commit / push + PR creation                           | ✅ implemented |
| 4     | CI failure → automatic re-fix loop + Feishu intake (stub)      | ✅ implemented |
| Auto-dispatcher | Poll for `agent-todo` + recover `agent-blocked`      | ✅ implemented |

Real-world soak: the development build has driven a fork of a 200-file
robotics codebase end-to-end (issue → PR → green CI → merge) without
human intervention. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for
the design choices that survived that soak.

---

## Project layout

```
app/                 worker source — pure logic, zero project-specific data
├── config/          pydantic models + YAML/env/CLI loader
├── github/          gh-CLI-backed Issue / PR / CI services
├── executors/       cursor, claude_code, codex, openhands, shell, stub
├── sandbox/         git worktree + docker isolation
├── langgraph_app/   planner / coder / tester / reviewer / reporter nodes
├── temporal_app/    durable workflow orchestration
├── dispatcher/      GitHub-issue polling loop
└── setup/           bootstrap / doctor / init wizards

configs/
├── default.yaml     shipped defaults — never edit per-project
└── examples/
    ├── generic-python.yaml   clean template, 4 placeholders
    └── dimos.yaml            mature example with every knob explained

recipes/github-workflows/    drop-in workflows for YOUR business repo
docs/                        PORTING.md + ARCHITECTURE.md + usage.md
tests/                       hermetic — fake executors, no API keys needed
```

---

## License

[MIT](LICENSE) © IssueForge contributors.
