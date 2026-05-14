"""Pydantic models for application configuration.

These models mirror ``configs/default.yaml`` and are the single source of truth
for "what is configurable". Adding a new knob is a 3-step change:

1. Add a field here.
2. Add the default to ``configs/default.yaml``.
3. Read it via ``get_config().<section>.<field>`` from your code.

No code outside this package should reach for ``os.environ`` for runtime config;
all environment overrides flow through :func:`app.config.loader.load_config`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SystemConfig(_StrictModel):
    name: str = "issue-agent-worker"
    artifact_root: Path = Path("./runs")
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"


class ProjectConfig(_StrictModel):
    """High-level project intent.

    The two questions the user must answer before starting the worker:
      1. What are we doing — optimizing an existing codebase, or scaffolding a
         new one from scratch?
      2. What does this worker exist to do? (one-line description, surfaced in
         the planner prompt and ``show-config`` output)

    Everything else (which repo, which branch, how to push, what tests to run)
    is captured by ``repo`` / ``commands`` / ``github`` / ``executor``.
    """

    # ``existing``: the agent works on an already-existing repository. Reads
    #   code, makes incremental changes, opens PRs against ``repo.base_branch``.
    #   ``repo.local_path`` should point at a checkout (or doctor will offer to
    #   clone for you).
    # ``scaffold``: the agent starts a fresh project from zero. The worker will
    #   initialise ``repo.local_path`` as an empty git repo if it does not
    #   exist. Useful for greenfield builds.
    mode: Literal["existing", "scaffold"] = "existing"

    # One-line human-readable summary of the worker's job for this project.
    # Prepended to planner prompts so the LLM has standing context, and shown
    # in ``agent-worker show-config`` so operators can see at a glance what a
    # given config file is for.
    description: str = ""


class RepoConfig(_StrictModel):
    provider: Literal["github"] = "github"
    owner: str = ""
    name: str = ""
    clone_url: str = ""
    base_branch: str = "main"
    working_branch_prefix: str = "agent"
    push_remote: str = "origin"
    local_path: str = ""
    # When True, the worktree backend runs ``git fetch <push_remote>
    # <base_branch>`` and branches the agent from the freshly-fetched
    # ``<remote>/<base_branch>`` ref (rather than the user's local
    # ``<base_branch>``). Prevents the "stale local main" failure mode where
    # commits sitting on the local branch but not pushed to the remote get
    # accidentally included in the agent's PR. Set to False if the user
    # explicitly wants the agent to inherit unpushed local work.
    auto_fetch_base: bool = True

    @property
    def slug(self) -> str:
        if not self.owner or not self.name:
            return ""
        return f"{self.owner}/{self.name}"

    @property
    def resolved_clone_url(self) -> str:
        if self.clone_url:
            return self.clone_url
        if self.owner and self.name:
            return f"git@github.com:{self.owner}/{self.name}.git"
        return ""


class GitHubConfig(_StrictModel):
    cli: str = "gh"
    issue_label_todo: str = "agent:todo"
    issue_label_queued: str = "agent:queued"
    issue_label_running: str = "agent:running"
    issue_label_planning: str = "agent:planning"
    issue_label_coding: str = "agent:coding"
    issue_label_testing: str = "agent:testing"
    issue_label_pr_created: str = "agent:pr-created"
    issue_label_ci_running: str = "agent:ci-running"
    issue_label_review: str = "agent:review"
    issue_label_blocked: str = "agent:blocked"
    issue_label_failed: str = "agent:failed"
    issue_label_done: str = "agent:done"
    pr_draft: bool = False
    auto_merge: bool = False
    delete_branch_after_merge: bool = True
    # CI poll filter: workflow names (case-insensitive substring match) that
    # should be ignored when judging CI status. Use this to exclude human-gated
    # workflows (e.g. "Auto Merge" that polls for a reviewer 👍) which would
    # otherwise keep the agent waiting indefinitely or be misread as failures.
    ci_ignore_workflows: list[str] = Field(default_factory=list)


class CommandsConfig(_StrictModel):
    setup: list[str] = Field(default_factory=list)
    lint: list[str] = Field(default_factory=list)
    test: list[str] = Field(default_factory=list)
    build: list[str] = Field(default_factory=list)


class ExecutorEntry(_StrictModel):
    """Per-executor settings. Concrete executors decide how to interpret ``args_template``.

    The ``args_template`` may contain ``{prompt}``, ``{workspace}``, ``{model}`` placeholders
    which the executor substitutes at runtime.
    """

    enabled: bool = False
    command: str = ""
    args_template: list[str] = Field(default_factory=list)
    model: str = ""
    timeout_seconds: int = 1800
    extra: dict[str, Any] = Field(default_factory=dict)


class ExecutorConfig(_StrictModel):
    default: str = "cursor"
    cursor: ExecutorEntry = Field(default_factory=ExecutorEntry)
    claude_code: ExecutorEntry = Field(default_factory=ExecutorEntry)
    codex: ExecutorEntry = Field(default_factory=ExecutorEntry)
    openhands: ExecutorEntry = Field(default_factory=ExecutorEntry)
    shell: ExecutorEntry = Field(default_factory=ExecutorEntry)
    stub: ExecutorEntry = Field(default_factory=lambda: ExecutorEntry(enabled=True))

    def entry(self, name: str) -> ExecutorEntry:
        if not hasattr(self, name):
            raise KeyError(f"Unknown executor: {name!r}. Add it to ExecutorConfig.")
        return getattr(self, name)


class SandboxConfig(_StrictModel):
    mode: Literal["local", "worktree", "docker"] = "local"
    worktree_root: Path = Path("./worktrees")
    docker_image: str = ""
    docker_extra_args: list[str] = Field(default_factory=list)


class TemporalConfig(_StrictModel):
    host: str = "localhost:7233"
    namespace: str = "default"
    task_queue: str = "issue-agent-worker"


class DispatcherSection(_StrictModel):
    """Knobs for the auto-dispatcher loop (``agent-worker dispatcher`` /
    ``agent-worker worker --with-dispatcher``).

    The dispatcher periodically lists open ``agent:todo`` issues and starts
    the matching Temporal workflows for them. It also (optionally) recovers
    issues stuck on ``agent:blocked`` by checking the real CI state of their
    PR and either marking them done or re-dispatching the workflow. And, if
    ``revive_orphans`` is on, it sweeps for issues whose label says they're
    in-flight (``agent-running``, ``agent-planning``, etc.) but whose Temporal
    workflow is absent or closed — the symptom of a worker / Temporal crash.

    Defaults are tuned for the dimos case (slow CI, lots of issues):
    poll every 30s, dispatch up to 10 issues per cycle, re-attempt blocked
    recovery no more than once every 10 minutes per issue, orphan-scan every
    5 minutes.
    """

    enabled: bool = False
    poll_interval_seconds: int = 30
    max_dispatch_per_cycle: int = 10
    auto_recover_blocked: bool = True
    blocked_recover_min_interval_seconds: int = 600
    # Orphan revival: detect issues whose label says they're in-flight but
    # whose Temporal workflow is absent (server DB reset) or closed (worker
    # crashed mid-round, label didn't transition). Re-dispatch them so they
    # don't hang in agent-planning / agent-coding forever.
    revive_orphans: bool = True
    # How often to run the orphan scan. Cheap-ish (one gh call per in-flight
    # label, one Temporal describe per matched issue) but doesn't need to run
    # every dispatcher cycle — 5 minutes is plenty given the failure mode
    # only happens on a crash.
    orphan_check_interval_seconds: int = 300
    # How long to wait between successive revival attempts for the same
    # issue, so a workflow that keeps getting orphaned doesn't get thrashed.
    orphan_revive_min_interval_seconds: int = 900


class WorkflowConfig(_StrictModel):
    temporal: TemporalConfig = Field(default_factory=TemporalConfig)
    max_retries: int = 5
    max_agent_rounds: int = 10
    local_test_required: bool = False
    ci_required: bool = False
    require_human_review: bool = True
    stop_after: Literal["planning", "coding", "testing", "review", "done"] = "planning"
    # Phase 4: how often to ask GitHub for the CI status, and how long to wait
    # before giving up on the run. Picked so a 2h CI is comfortable.
    ci_poll_interval_seconds: int = 30
    ci_max_wait_seconds: int = 7200
    # Auto-dispatcher (poll GitHub for new agent:todo issues and recover from
    # agent:blocked). Disabled by default — opt in via project YAML or
    # ``agent-worker worker --with-dispatcher``.
    dispatcher: DispatcherSection = Field(default_factory=DispatcherSection)


class LangGraphConfig(_StrictModel):
    checkpoint_backend: Literal["sqlite", "memory"] = "sqlite"
    checkpoint_db: Path = Path("./checkpoints/langgraph.sqlite")


class FeishuConfig(_StrictModel):
    enabled: bool = False
    default_repo: str = ""
    webhook_path: str = "/feishu/webhook"
    port: int = 8080
    # When set, every webhook request is verified with HMAC-SHA256 over the
    # timestamp+nonce+body using this shared secret. Required for production;
    # leave empty in dev to skip verification.
    verify_token: str = ""
    # Default labels added to issues created from Feishu messages.
    default_labels: list[str] = Field(default_factory=lambda: ["agent-todo"])
    # If true, after creating the issue we immediately start the Temporal
    # workflow. False = "queue only" mode (a separate dispatcher picks it up).
    auto_start_workflow: bool = True


class PoliciesConfig(_StrictModel):
    require_human_review_labels: list[str] = Field(default_factory=list)
    refuse_patterns: list[str] = Field(default_factory=list)


class AppConfig(_StrictModel):
    """Top-level configuration tree.

    Access via ``get_config()`` after the loader has merged YAML, env, and CLI overrides.
    """

    system: SystemConfig = Field(default_factory=SystemConfig)
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    repo: RepoConfig = Field(default_factory=RepoConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    commands: CommandsConfig = Field(default_factory=CommandsConfig)
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    langgraph: LangGraphConfig = Field(default_factory=LangGraphConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    policies: PoliciesConfig = Field(default_factory=PoliciesConfig)

    @model_validator(mode="after")
    def _validate_executor_default(self) -> AppConfig:
        try:
            entry = self.executor.entry(self.executor.default)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        if not entry.enabled:
            # The stub executor is always available; warn-by-error for clarity.
            raise ValueError(
                f"executor.default={self.executor.default!r} but that executor is disabled. "
                "Either enable it or change executor.default."
            )
        return self
