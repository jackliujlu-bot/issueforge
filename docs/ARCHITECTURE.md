# Architecture

> **Audience**: contributors and people debugging the worker itself.
> If you just want to use IssueForge on your own repo, read
> [PORTING.md](PORTING.md) first.

## The whole flow in one diagram

```
ingestion       Feishu webhook | GitHub Issue webhook | manual CLI
↓
task store      GitHub Issue (labels are the state machine)
↓
durable runtime Temporal Workflow + Activities + Worker
↓
agent brain     LangGraph StateGraph (planner / coder / tester / reviewer / reporter)
↓
executors       cursor / claude_code / codex / openhands / shell / stub  (pluggable)
↓
sandbox         local | git worktree | docker
↓
delivery        Branch → PR → GitHub Actions CI → merge
```

## Why this much machinery

A long-running coding agent has very different needs from a one-shot
script:

- **Durable** — a process crash mid-PR-creation must not lose the work.
  Temporal owns this.
- **Stateful between rounds** — round 2 of the same issue needs to know
  what round 1 already did, even if rounds were minutes apart and ran
  in different processes. LangGraph checkpoints + on-disk artifacts own
  this.
- **Pluggable agent brand** — `cursor-agent`, `claude`, `codex` all have
  different CLIs. Swap is a config change, not a code change.
- **Repeatable across projects** — every project-specific knob is in YAML,
  never in the worker code. Asserted by a unit test.

## Three layers of recovery

| Layer                        | What it stores                              | Lifetime                  |
|------------------------------|---------------------------------------------|---------------------------|
| Temporal workflow history    | every Activity invocation + result          | until workflow completes  |
| LangGraph SQLite checkpoint  | full `AgentState` after each node           | persists across restarts  |
| Per-issue artifact tree      | `runs/<owner>--<repo>--issue-<n>/...`       | persists forever          |

Lose any one of the three and the others can reconstruct it. This is what
makes "kill the worker, restart, watch it pick up where it left off" work.

## Stable IDs

```
workflow_id   = "issue-agent--<owner--repo>--issue-<n>"
thread_id     = "<owner:repo>:issue-<n>"
artifact_key  = "<owner--repo>--issue-<n>"
```

All derived from `(owner, repo, issue_number)`. Re-dispatching the same
issue **attaches** to the existing workflow instead of starting a new one.
That's how the dispatcher's "scan every 30s" stays idempotent.

## Boundary discipline (enforced in code)

- **Temporal activities never reach into LangGraph internals.** They call
  `run_agent_round` and that's it.
- **LangGraph nodes never touch GitHub or the network.** The reporter
  writes a `pending_issue_comment` field on the state; the workflow
  posts it.
- **Executors never touch Temporal or LangGraph.** They take a prompt,
  return a result. Mocked easily for tests.
- **Configuration is the only inter-layer contract.** `executor.<name>.{enabled,
  command, args_template, model, timeout_seconds}` is the schema every
  executor implements; swapping one for another is a YAML edit.

## Repository layout

```
app/
├── config/             pydantic models + YAML/env/CLI loader
├── github/             gh-CLI-backed Issue / PR / CI services
├── executors/          cursor / claude_code / codex / openhands / shell / stub
├── sandbox/            ArtifactStore (real), WorktreeManager + DockerRunner
├── langgraph_app/
│   ├── state.py        AgentState TypedDict
│   ├── graph.py        StateGraph wiring + run_agent_round
│   ├── checkpoint.py   SQLite/Memory checkpointer factory
│   └── nodes/          planner / coder / tester / reviewer / reporter
├── temporal_app/       IssueAgentWorkflow + activities + worker + client
├── feishu/             message parser + FastAPI webhook server
├── observability/      structlog config
├── dispatcher/         GitHub-issue polling loop
├── setup/              `bootstrap` / `doctor` / `init` orchestrators
└── main.py             Typer CLI

configs/
├── default.yaml        shipped defaults — never edit per-project
└── examples/
    ├── generic-python.yaml   clean template, fill in 4 placeholders
    └── dimos.yaml            mature real-world example with all knobs explained

recipes/github-workflows/
├── ci.yml              copy to YOUR repo's .github/workflows/
└── auto-merge.yml      copy to YOUR repo's .github/workflows/
```

## State machine on GitHub (label-driven)

```
agent:todo
   ↓ Dispatcher / manual run-issue
agent:running         ← Temporal workflow started
   ↓
agent:planning        ← LangGraph planner running
   ↓
agent:coding          ← Coder calling executor
   ↓
agent:testing         ← Local tester running commands.{lint,test}
   ↓
agent:pr-created      ← Deliverer pushed branch + opened PR
   ↓
agent:ci-running      ← Watching real GitHub CI
   ↓
agent:review or agent:done    or    agent:failed / agent:blocked
```

All label names are configurable; the worker has no hardcoded label
strings — it reads each one from `github.issue_label_*`.

## Phase milestones (history)

| Phase | Scope                                                          | Status        |
|-------|----------------------------------------------------------------|---------------|
| 1     | Issue → Temporal → LangGraph Planner → comment plan back       | ✅ implemented |
| 2     | git worktree + Cursor Agent edits + local tests                | ✅ implemented |
| 3     | Branch / commit / push + PR creation                           | ✅ implemented |
| 4     | CI failure → automatic re-fix loop + Feishu intake             | ✅ implemented |
| Auto-dispatcher | Poll GitHub for `agent:todo` + recover `agent:blocked` | ✅ implemented |

## Configuration loading order

```
low ┌────────────────────────────────────────┐ high
    │ 1. configs/default.yaml                │
    │ 2. project YAML (AGENT_WORKER_CONFIG)  │
    │ 3. .env file                           │
    │ 4. environment variables               │
    │ 5. CLI flags                           │
    └────────────────────────────────────────┘
```

Deep merge for dicts; lists are replaced wholesale (not concatenated).

### Two env-var styles

**Short names** (common keys):
```bash
TEMPORAL_HOST=temporal.prod:7233
TEMPORAL_TASK_QUEUE=my-queue
ARTIFACT_ROOT=/var/agent/runs
LANGGRAPH_CHECKPOINT_DB=/var/agent/lg.sqlite
CURSOR_AGENT_BIN=/usr/local/bin/cursor-agent
AGENT_WORKER_CONFIG=configs/myproject.yaml
```

**Generic pattern** for any nested field — `AGENT_WORKER__SECTION__FIELD`:
```bash
AGENT_WORKER__REPO__OWNER=acme
AGENT_WORKER__EXECUTOR__DEFAULT=claude_code
AGENT_WORKER__WORKFLOW__MAX_RETRIES=3
AGENT_WORKER__COMMANDS__TEST='["pytest","ruff check"]'   # list = JSON
```

Values auto-typed: `true/false` → bool, integers → int/float, leading `[`/`{` → JSON, else string.

## Extension points

### Add a new executor

```python
# app/executors/myagent.py
from .base import Executor, ExecutorResult, register_executor

class MyAgent(Executor):
    def run(self, prompt: str, *, workspace, ...) -> ExecutorResult: ...

register_executor("myagent", MyAgent)
```

Then in YAML:
```yaml
executor:
  default: myagent
  myagent:
    enabled: true
    command: my-agent
    args_template: ["{prompt}"]
    timeout_seconds: 1800
```

### Add a new LangGraph node

Add a file under `app/langgraph_app/nodes/` and wire it in `graph.py`'s
edge list. The graph builder selects edges by `workflow.stop_after`, so
your new node integrates without touching the workflow YAML.

### Add a new Temporal activity

Add a method to `app/temporal_app/activities.py` and call it from the
workflow with `await workflow.execute_activity(...)`. Activities are
side-effecting (network, disk, subprocess); workflows are deterministic
orchestration only.

## Artifacts (the agent's external memory)

Per-issue:

```
runs/<owner>--<repo>--issue-<n>/
├── input/
│   └── issue.md              ← raw issue body
├── planning/
│   ├── plan.md               ← planner output
│   ├── todo.md               ← extracted subtasks (what coder sees)
│   └── assumptions.md
├── execution/
│   ├── commands.log
│   ├── tool_calls.jsonl      ← every executor invocation
│   └── changed_files.txt
├── evidence/
│   ├── local_tests.log
│   └── ci_logs.md            ← GitHub Actions failure summary
├── review/
│   ├── self_review.md
│   └── risk_report.md
└── handoff.md                ← what round N+1 / next process needs to know
```

This is the agent's external memory — when it crashes after several hours
of work and a new process starts, reading `handoff.md` is how it picks up
where the dead one left off.
