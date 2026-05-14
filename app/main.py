"""issue-agent-worker CLI.

Subcommands:

    --- setup / preflight (run these first) ---
    init              Interactive wizard: writes configs/<project>.yaml.
    doctor            Validate every prerequisite (gh auth, push perms, labels,
                      executor binary, Temporal). With --fix, applies safe
                      auto-fixes (creates labels, clones repo, mkdir dirs).
    bootstrap         init + doctor --fix in one shot — for first-time setup.
    start             Convenience launcher: doctor + docker compose up + worker.

    --- inspection ---
    show-config       Print the resolved configuration (after YAML+env+CLI merging).
    list-executors    Show registered executors and which one is the default.
    artifact-path     Print the on-disk artifact directory for an issue.

    --- execution ---
    run-issue         Start (or attach to) an issue agent workflow on Temporal.
    run-once          Run one LangGraph round in-process (no Temporal).
    worker            Start a Temporal worker that picks up workflows.
    dispatcher        Auto-poll GitHub for new agent:todo issues and start
                      workflows for them. Can run standalone or be embedded in
                      the worker via ``agent-worker worker --with-dispatcher``.
    feishu-server     Start the Feishu webhook server (requires the [feishu] extra).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from app.config import AppConfig, load_config
from app.observability import configure_logging, get_logger

app = typer.Typer(
    name="agent-worker",
    no_args_is_help=True,
    add_completion=False,
    help="Long-running coding-agent runtime CLI.",
)

console = Console()
log = get_logger(__name__)


# --------- shared option helper ------------------------------------------ #

def _bootstrap(config_path: str | None, log_level: str | None) -> AppConfig:
    cfg = load_config(config_path=config_path)
    configure_logging(
        level=log_level or cfg.system.log_level,
        fmt=cfg.system.log_format,
    )
    return cfg


ConfigOpt = typer.Option(
    None, "--config", "-c", help="Path to project YAML overlay (overrides AGENT_WORKER_CONFIG)."
)
LogOpt = typer.Option(None, "--log-level", help="Override log level (DEBUG/INFO/WARNING).")


# --------- show-config --------------------------------------------------- #

@app.command("show-config")
def show_config(
    config_path: str | None = ConfigOpt,
    log_level: str | None = LogOpt,
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Print the resolved configuration."""
    cfg = _bootstrap(config_path, log_level)
    payload = cfg.model_dump(mode="json")
    if as_json:
        console.print_json(json.dumps(payload))
        return
    _render_config(payload)


def _render_config(payload: dict) -> None:
    for section, value in payload.items():
        table = Table(title=section, show_header=True, header_style="bold")
        table.add_column("key")
        table.add_column("value", overflow="fold")
        if isinstance(value, dict):
            for k, v in value.items():
                table.add_row(str(k), _format_value(v))
        else:
            table.add_row(section, _format_value(value))
        console.print(table)


def _format_value(value: object) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


# --------- list-executors ------------------------------------------------ #

@app.command("list-executors")
def list_executors(
    config_path: str | None = ConfigOpt,
    log_level: str | None = LogOpt,
) -> None:
    """List registered executors and their enabled state."""
    cfg = _bootstrap(config_path, log_level)
    from app.executors import build_executor  # noqa: F401  (forces registration imports)
    from app.executors.base import known_executors

    table = Table(title="Code Executors", show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("enabled")
    table.add_column("default")
    table.add_column("command")

    default = cfg.executor.default
    for name in known_executors():
        entry = cfg.executor.entry(name)
        table.add_row(
            name,
            "yes" if entry.enabled else "no",
            "★" if name == default else "",
            entry.command or "(unset)",
        )
    console.print(table)


# --------- run-once ------------------------------------------------------ #

@app.command("run-once")
def run_once(
    issue: int = typer.Option(..., "--issue", "-i", help="GitHub issue number."),
    repo: str | None = typer.Option(
        None, "--repo", help="owner/name override (defaults to config)."
    ),
    config_path: str | None = ConfigOpt,
    log_level: str | None = LogOpt,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Don't fetch the issue or post a comment; use a fake issue body."
    ),
) -> None:
    """Run a single LangGraph round synchronously, without Temporal.

    Useful for local validation: it exercises the full graph (executor, artifacts,
    reporter) but skips the durable runtime.
    """
    cfg = _bootstrap(config_path, log_level)
    if repo:
        owner, _, name = repo.partition("/")
        if not owner or not name:
            raise typer.BadParameter("--repo must be 'owner/name'")
        cfg.repo.owner = owner
        cfg.repo.name = name

    from app.langgraph_app.graph import AgentRoundInput, run_agent_round

    if dry_run:
        round_input = AgentRoundInput(
            repo=cfg.repo.slug or "dry-run/dry-run",
            issue_number=issue,
            issue_title=f"(dry-run) issue {issue}",
            issue_body="This is a dry-run synthetic issue body. Replace me with a real one.",
            issue_url="",
        )
    else:
        from app.github.issue_service import GitHubIssueService

        svc = GitHubIssueService(cfg.repo, cfg.github)
        fetched = svc.fetch(issue)
        round_input = AgentRoundInput(
            repo=cfg.repo.slug,
            issue_number=fetched.number,
            issue_title=fetched.title,
            issue_body=fetched.body,
            issue_url=fetched.url,
        )

    result = run_agent_round(config=cfg, round_input=round_input)

    console.rule(f"[bold]Round result for issue #{issue}")
    console.print(f"final_status: [bold]{result.final_status}[/]")
    console.print(f"comment length: {len(result.pending_issue_comment)} chars")
    console.print()
    console.print(result.pending_issue_comment or "(no comment produced)")


# --------- run-issue ----------------------------------------------------- #

@app.command("run-issue")
def run_issue(
    issue: int = typer.Option(..., "--issue", "-i", help="GitHub issue number."),
    config_path: str | None = ConfigOpt,
    log_level: str | None = LogOpt,
    wait: bool = typer.Option(
        False, "--wait", help="Block until the workflow completes; print the result."
    ),
) -> None:
    """Dispatch an :class:`IssueAgentWorkflow` to Temporal.

    The workflow id is stable per (repo, issue), so re-running this command for
    the same issue attaches to the existing run instead of starting a duplicate.
    """
    cfg = _bootstrap(config_path, log_level)

    if not cfg.repo.slug:
        raise typer.BadParameter("repo.owner / repo.name must be set in config.")

    async def _go() -> None:
        from app.temporal_app.client import start_issue_workflow

        handle, outcome = await start_issue_workflow(cfg, issue_number=issue)
        marker = {
            "started": "Workflow started",
            "attached_running": "Workflow already running (attached)",
            "restarted_after_close": "Prior workflow closed; restarted fresh",
        }.get(outcome, "Workflow dispatched")
        console.print(
            f"[bold]{marker}:[/] {handle.id} on task queue "
            f"{cfg.workflow.temporal.task_queue!r}"
        )
        if wait:
            result = await handle.result()
            console.print_json(json.dumps(result.__dict__))

    asyncio.run(_go())


# --------- worker -------------------------------------------------------- #

@app.command("worker")
def worker_cmd(
    config_path: str | None = ConfigOpt,
    log_level: str | None = LogOpt,
    with_dispatcher: bool = typer.Option(
        False,
        "--with-dispatcher",
        help=(
            "Also start the issue dispatcher loop in this process. The "
            "dispatcher polls GitHub for new ``agent:todo`` issues and "
            "starts the matching Temporal workflows. Overrides "
            "``workflow.dispatcher.enabled``."
        ),
    ),
    no_dispatcher: bool = typer.Option(
        False,
        "--no-dispatcher",
        help="Disable the dispatcher even if config has it enabled.",
    ),
) -> None:
    """Run the Temporal worker process.

    By default the dispatcher inside the worker is governed by
    ``workflow.dispatcher.enabled`` in your config. Use ``--with-dispatcher``
    / ``--no-dispatcher`` to override per-invocation (useful when running
    several workers but only one should drive the dispatcher loop).
    """
    _bootstrap(config_path, log_level)
    from app.temporal_app.worker import run_worker

    override: bool | None = None
    if with_dispatcher and no_dispatcher:
        raise typer.BadParameter(
            "--with-dispatcher and --no-dispatcher are mutually exclusive."
        )
    if with_dispatcher:
        override = True
    elif no_dispatcher:
        override = False

    asyncio.run(run_worker(with_dispatcher=override))


# --------- dispatcher --------------------------------------------------- #

@app.command("dispatcher")
def dispatcher_cmd(
    config_path: str | None = ConfigOpt,
    log_level: str | None = LogOpt,
    once: bool = typer.Option(
        False,
        "--once",
        help="Run a single dispatch iteration and exit (useful for cron / debug).",
    ),
    interval: int | None = typer.Option(
        None,
        "--interval",
        help="Seconds between cycles. Overrides workflow.dispatcher.poll_interval_seconds.",
    ),
    max_cycles: int = typer.Option(
        0,
        "--max-cycles",
        help="Stop after N cycles (0 = unbounded). Handy for one-shot CI runs.",
    ),
) -> None:
    """Run the issue dispatcher loop standalone (no Temporal worker).

    The dispatcher polls GitHub for ``agent:todo`` issues and starts the
    corresponding Temporal workflows. It also recovers ``agent:blocked``
    issues whose PRs have since reached a real CI verdict. This is the
    "I keep adding issues, the system keeps picking them up" half of the
    architecture.

    Typical use:

    \b
      # alongside a running worker:
      agent-worker worker          # one terminal
      agent-worker dispatcher      # another terminal — same effect as
                                   # `worker --with-dispatcher`, just split.

    For a one-shot run from cron / a debugger:

    \b
      agent-worker dispatcher --once
    """
    cfg = _bootstrap(config_path, log_level)

    async def _go() -> None:
        from app.dispatcher import (
            DispatcherConfig,
            build_default_deps,
            run_dispatcher_loop,
            run_one_iteration,
        )

        section = cfg.workflow.dispatcher
        dispatcher_config = DispatcherConfig(
            poll_interval_seconds=interval or section.poll_interval_seconds,
            max_dispatch_per_cycle=section.max_dispatch_per_cycle,
            auto_recover_blocked=section.auto_recover_blocked,
            blocked_recover_min_interval_seconds=section.blocked_recover_min_interval_seconds,
            revive_orphans=section.revive_orphans,
            orphan_check_interval_seconds=section.orphan_check_interval_seconds,
            orphan_revive_min_interval_seconds=section.orphan_revive_min_interval_seconds,
            max_cycles=max_cycles if max_cycles > 0 else 0,
        )

        if once:
            deps = build_default_deps(cfg)
            iteration = await run_one_iteration(
                config=cfg,
                dispatcher_config=dispatcher_config,
                deps=deps,
            )
            console.print_json(json.dumps(iteration.stats.__dict__))
            if iteration.notes:
                console.rule("[dim]notes")
                for n in iteration.notes:
                    console.print(f"  [dim]{n}[/]")
            return

        console.print(
            f"[bold]Dispatcher loop starting[/] (interval="
            f"{dispatcher_config.poll_interval_seconds}s, "
            f"auto_recover_blocked={dispatcher_config.auto_recover_blocked})"
        )
        console.print("Press Ctrl+C to stop.")
        try:
            await run_dispatcher_loop(
                config=cfg,
                dispatcher_config=dispatcher_config,
            )
        except KeyboardInterrupt:
            console.print("[dim]Dispatcher stopped by user.[/]")

    asyncio.run(_go())


# --------- feishu-server ------------------------------------------------- #

@app.command("feishu-server")
def feishu_server(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int | None = typer.Option(None, "--port"),
    config_path: str | None = ConfigOpt,
    log_level: str | None = LogOpt,
) -> None:
    """Start the Feishu webhook server (requires `pip install .[feishu]`)."""
    cfg = _bootstrap(config_path, log_level)
    try:
        import uvicorn  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - install hint
        raise typer.BadParameter(
            "uvicorn not installed; run `pip install issue-agent-worker[feishu]`."
        ) from exc

    from app.feishu.webhook_server import build_app

    api = build_app(cfg.feishu)
    uvicorn.run(api, host=host, port=port or cfg.feishu.port)


# --------- artifact path helper (handy for debugging) ------------------- #

@app.command("artifact-path")
def artifact_path(
    issue: int = typer.Option(..., "--issue", "-i"),
    repo: str | None = typer.Option(None, "--repo"),
    config_path: str | None = ConfigOpt,
) -> None:
    """Print the on-disk artifact directory for a given issue."""
    cfg = _bootstrap(config_path, None)
    repo_slug = repo or cfg.repo.slug or "repo"
    from app.sandbox.artifact_store import ArtifactStore

    store = ArtifactStore(cfg.system.artifact_root)
    run_dir = store.run_dir(ArtifactStore.issue_key(repo_slug, issue))
    console.print(str(run_dir.root))


# --------- init (interactive wizard) ------------------------------------ #

@app.command("init")
def init_cmd(
    output: str | None = typer.Option(
        None, "--output", "-o",
        help="Where to write the project YAML (default: prompts you, suggests configs/<repo>.yaml).",
    ),
    no_dotenv: bool = typer.Option(
        False, "--no-dotenv", help="Don't update .env with AGENT_WORKER_CONFIG."
    ),
    use: str | None = typer.Option(
        None, "--use", help="Skip menu/wizard and just use this YAML (e.g. configs/examples/generic-python.yaml)."
    ),
    log_level: str | None = LogOpt,
) -> None:
    """Interactive setup. Three paths, depending on what's already in configs/:

    \b
      1. Use an existing YAML as-is (skip the Q&A entirely)
      2. Clone an existing YAML to a new file (then hand-edit)
      3. Run the full wizard from scratch

    If you already know which YAML you want, ``agent-worker init --use
    configs/foo.yaml`` skips the menu and just points .env at that file.
    """
    configure_logging(level=log_level or "INFO", fmt="console")
    from app.setup.wizard import (
        ConfigChoice,
        WizardAnswers,
        choose_config_strategy,
        clone_existing_yaml,
        run_wizard,
        update_dotenv,
        write_project_yaml,
    )

    project_root = _project_root()
    io = _RichWizardIO(console)

    # ---- Fast path: --use bypasses the menu entirely. ------------------- #
    if use:
        chosen = (project_root / use).resolve() if not Path(use).is_absolute() else Path(use)
        if not chosen.exists():
            console.print(f"[red]✗ {chosen} does not exist.[/]")
            raise typer.Exit(code=1)
        _activate_yaml(chosen, project_root, no_dotenv=no_dotenv)
        return

    # ---- Otherwise: present the menu ----------------------------------- #
    choice: ConfigChoice = choose_config_strategy(io, project_root=project_root)

    if choice.strategy == "use_existing":
        assert choice.source_path is not None
        _activate_yaml(choice.source_path, project_root, no_dotenv=no_dotenv)
        return

    if choice.strategy == "clone_and_edit":
        assert choice.source_path is not None and choice.new_name is not None
        try:
            cloned = clone_existing_yaml(
                choice.source_path,
                new_name=choice.new_name,
                project_root=project_root,
            )
        except FileExistsError as exc:
            console.print(f"[red]✗ {exc}[/]")
            raise typer.Exit(code=1) from exc
        console.print(
            f"[green]✓[/] Cloned [bold]{choice.source_path.name}[/] → "
            f"[bold]{cloned.relative_to(project_root)}[/]."
        )
        _activate_yaml(cloned, project_root, no_dotenv=no_dotenv)
        console.print(
            f"  Edit it: [cyan]$EDITOR {cloned.relative_to(project_root)}[/]"
        )
        return

    # ---- Wizard path --------------------------------------------------- #
    if output:
        io._defaults["Where to write the project YAML"] = output

    answers: WizardAnswers = run_wizard(io, project_root=project_root, cwd=Path.cwd())
    yaml_path = write_project_yaml(answers, project_root)
    console.print(f"[green]✓[/] Wrote [bold]{yaml_path.relative_to(project_root)}[/].")

    if answers.update_dotenv and not no_dotenv:
        env_path = project_root / answers.dotenv_path
        update_dotenv(env_path, config_path=yaml_path.relative_to(project_root))
        console.print(
            f"[green]✓[/] Updated [bold]{env_path.relative_to(project_root)}[/] "
            f"(AGENT_WORKER_CONFIG={yaml_path.relative_to(project_root)})."
        )

    console.rule("[bold]Next steps")
    console.print("1. Review the YAML and tweak anything you'd like.")
    console.print("2. Run [bold cyan]agent-worker doctor[/] to validate prerequisites.")
    console.print(
        "3. If anything is missing, run [bold cyan]agent-worker doctor --fix[/] "
        "to apply safe auto-fixes (creates labels, clones repo, makes dirs)."
    )
    console.print(
        "4. When doctor is green, run [bold cyan]agent-worker start[/] to "
        "boot Temporal + worker."
    )


def _activate_yaml(yaml_path: Path, project_root: Path, *, no_dotenv: bool) -> None:
    """Point .env at ``yaml_path`` and tell the user what we did."""
    from app.setup.wizard import update_dotenv

    rel = yaml_path.relative_to(project_root) if _is_inside(yaml_path, project_root) else yaml_path
    console.print(f"[green]✓[/] Selected [bold]{rel}[/].")
    if no_dotenv:
        console.print(
            f"  [dim]Set this in your shell:[/] export AGENT_WORKER_CONFIG={rel}"
        )
        return
    env_path = project_root / ".env"
    update_dotenv(env_path, config_path=rel)
    os.environ["AGENT_WORKER_CONFIG"] = str(rel)
    console.print(
        f"[green]✓[/] Updated [bold].env[/] (AGENT_WORKER_CONFIG={rel})."
    )
    console.print()
    console.print(
        "Next: [cyan]agent-worker doctor[/] (or skip straight to "
        "[cyan]agent-worker bootstrap[/])."
    )


# --------- doctor ------------------------------------------------------- #

@app.command("doctor")
def doctor_cmd(
    config_path: str | None = ConfigOpt,
    log_level: str | None = LogOpt,
    fix: bool = typer.Option(
        False, "--fix", help="Apply safe auto-fixes (create labels, clone repo, mkdir dirs)."
    ),
    skip_temporal: bool = typer.Option(
        False, "--skip-temporal", help="Don't probe Temporal (use during init)."
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Treat WARN as failure when computing exit code."
    ),
) -> None:
    """Run all preflight checks and report PASS / WARN / FAIL.

    Exit code 0 = no FAILs; 1 = at least one FAIL (or any WARN with --strict).
    Run with ``--fix`` to apply auto-fixes for the actionable WARNs / FAILs:
    create missing labels, clone the repo, ``git init`` for scaffold mode,
    create the artifact / checkpoint directories.
    """
    cfg = _bootstrap(config_path, log_level)
    from app.setup.doctor import run_doctor

    result = run_doctor(cfg, fix=fix, check_temporal=not skip_temporal)
    _render_doctor(result)
    if strict and result.warn_count:
        raise typer.Exit(code=1)
    raise typer.Exit(code=result.exit_code)


def _render_doctor(result: object) -> None:
    """Render a DoctorResult as a rich table."""
    from app.setup.doctor import CheckOutcome, DoctorResult

    assert isinstance(result, DoctorResult)
    table = Table(title="agent-worker doctor", show_header=True, header_style="bold")
    table.add_column("check", style="bold")
    table.add_column("status", justify="center")
    table.add_column("detail", overflow="fold")
    for r in result.reports:
        color = {
            CheckOutcome.PASS: "green",
            CheckOutcome.WARN: "yellow",
            CheckOutcome.FAIL: "red",
            CheckOutcome.SKIP: "dim",
        }[r.outcome]
        detail = r.detail
        if r.fix_applied:
            detail += f"\n[dim]fixed:[/] {r.fix_applied}"
        if r.hint:
            detail += f"\n[dim]hint:[/] {r.hint}"
        table.add_row(r.name, f"[{color}]{r.outcome.value}[/]", detail)
    console.print(table)
    console.print(
        f"[bold]Summary:[/] {result.pass_count} pass, "
        f"[yellow]{result.warn_count} warn[/], "
        f"[red]{result.fail_count} fail[/]"
    )


# --------- bootstrap (the one command users have to know) -------------- #

@app.command("bootstrap")
def bootstrap_cmd(
    config_path: str | None = ConfigOpt,
    log_level: str | None = LogOpt,
    skip_init: bool = typer.Option(
        False, "--skip-init",
        help="Don't run the wizard even if AGENT_WORKER_CONFIG is unset.",
    ),
    full: bool = typer.Option(
        False, "--full",
        help="Also run the FULL phase: docker compose up + Temporal worker + dispatch a real workflow (opens a real PR).",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Don't ask for confirmation between phases (CI / scripted use).",
    ),
    test_issue: int | None = typer.Option(
        None, "--test-issue",
        help="Issue number to use for the LIVE_READ phase. Default: auto-discover the smallest open agent:todo issue.",
    ),
    no_live_read: bool = typer.Option(
        False, "--no-live-read",
        help="Skip the LIVE_READ phase entirely. Useful on slow networks or for first-time setup against a brand-new repo with no issues yet.",
    ),
    live_read_timeout: float = typer.Option(
        180.0, "--live-read-timeout",
        help="Hard cap (seconds) on the LIVE_READ planner round. Exceeding it is a WARN, not a FAIL — bootstrap continues.",
    ),
) -> None:
    """Configure, validate, and test in one shot.

    The promise: run this command, follow the prompts (or pass --yes), and
    you'll either end up with a working system or be told exactly which
    phase failed and what to do.

    Phases (each one stops the chain on FAIL):

    \b
      1. CONFIG     - launch the wizard if no project YAML is set
      2. PREFLIGHT  - doctor checks + safe auto-fixes
      3. SMOKE      - offline planner round (no network, no shell-out)
      4. LIVE_READ  - planner round on a real GitHub issue (no comment, no PR)
      5. FULL       - docker compose up + Temporal worker + dispatch a real
                      workflow (only with --full)
    """
    configure_logging(level=log_level or "INFO", fmt="console")
    from app.setup.orchestrator import run_bootstrap

    project_root = _project_root()

    # Make AGENT_WORKER_CONFIG visible to the orchestrator.
    if config_path:
        os.environ["AGENT_WORKER_CONFIG"] = config_path

    # Wizard hook: present the same use-existing / clone / wizard menu the
    # standalone `init` does, then write .env so the rest of bootstrap picks
    # the chosen YAML up.
    def _wizard_callback() -> Path | None:
        if skip_init:
            return None
        from app.setup.wizard import (
            choose_config_strategy,
            clone_existing_yaml,
            run_wizard,
            update_dotenv,
            write_project_yaml,
        )

        io = _RichWizardIO(console)
        choice = choose_config_strategy(io, project_root=project_root)

        # use_existing / clone_and_edit: just point .env, no wizard.
        if choice.strategy == "use_existing":
            assert choice.source_path is not None
            return _set_env_for_yaml(choice.source_path, project_root)

        if choice.strategy == "clone_and_edit":
            assert choice.source_path is not None and choice.new_name is not None
            try:
                cloned = clone_existing_yaml(
                    choice.source_path,
                    new_name=choice.new_name,
                    project_root=project_root,
                )
            except FileExistsError as exc:
                console.print(f"[red]✗ {exc}[/]")
                return None
            console.print(
                f"[green]✓[/] Cloned [bold]{choice.source_path.name}[/] → "
                f"[bold]{cloned.relative_to(project_root)}[/]. Edit by hand later if needed."
            )
            return _set_env_for_yaml(cloned, project_root)

        # wizard path
        answers = run_wizard(io, project_root=project_root, cwd=Path.cwd())
        yaml_path = write_project_yaml(answers, project_root)
        if answers.update_dotenv:
            env_path = project_root / answers.dotenv_path
            update_dotenv(
                env_path,
                config_path=yaml_path.relative_to(project_root) if _is_inside(yaml_path, project_root) else yaml_path,
            )
            from dotenv import load_dotenv
            load_dotenv(env_path, override=True)
        return yaml_path

    def _set_env_for_yaml(yaml_path: Path, project_root: Path) -> Path:
        """Update .env + os.environ + reload dotenv so all downstream phases
        see AGENT_WORKER_CONFIG immediately."""
        from dotenv import load_dotenv

        from app.setup.wizard import update_dotenv

        rel = (
            yaml_path.relative_to(project_root)
            if _is_inside(yaml_path, project_root) else yaml_path
        )
        env_path = project_root / ".env"
        update_dotenv(env_path, config_path=rel)
        os.environ["AGENT_WORKER_CONFIG"] = str(rel)
        load_dotenv(env_path, override=True)
        console.print(
            f"[green]✓[/] Pointing [bold].env[/] at [bold]{rel}[/]."
        )
        return yaml_path

    def _config_loader() -> AppConfig:
        # Honour explicit --config; otherwise let the loader read AGENT_WORKER_CONFIG.
        return load_config(config_path=config_path)

    bootstrap_io = _RichBootstrapIO(console, interactive=not yes)
    result = run_bootstrap(
        io=bootstrap_io,
        config_loader=_config_loader,
        project_root=project_root,
        run_wizard_if_unconfigured=_wizard_callback,
        full=full,
        interactive=not yes,
        test_issue_number=test_issue,
        skip_live_read=no_live_read,
        live_read_timeout_seconds=live_read_timeout,
    )
    _render_bootstrap_summary(result)

    if not result.ok:
        raise typer.Exit(code=1)


def _is_inside(p: Path, base: Path) -> bool:
    try:
        p.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _render_bootstrap_summary(result: object) -> None:
    """Render the BootstrapResult as a final table + next-step hint."""
    from app.setup.orchestrator import BootstrapResult, PhaseStatus

    assert isinstance(result, BootstrapResult)
    table = Table(
        title="bootstrap summary",
        show_header=True,
        header_style="bold",
    )
    table.add_column("phase", style="bold")
    table.add_column("status", justify="center")
    table.add_column("result", overflow="fold")
    for p in result.phases:
        color = {
            PhaseStatus.PASS: "green",
            PhaseStatus.WARN: "yellow",
            PhaseStatus.FAIL: "red",
            PhaseStatus.SKIP: "dim",
        }[p.status]
        body = p.summary
        if p.detail:
            body += f"\n[dim]{p.detail}[/]"
        if p.hint:
            body += f"\n[dim]hint:[/] {p.hint}"
        if p.elapsed_seconds:
            body += f"\n[dim]took {p.elapsed_seconds:.1f}s[/]"
        table.add_row(p.name, f"[{color}]{p.status.value}[/]", body)
    console.print(table)

    if result.ok:
        console.print()
        console.print("[bold green]✓ Bootstrap complete.[/]")
        console.print()
        console.print("Next:")
        console.print(
            "  • [cyan]agent-worker run-issue --issue N --wait[/]  — dispatch a real workflow"
        )
        console.print(
            "  • [cyan]agent-worker doctor[/]                       — re-validate any time"
        )
        console.print(
            "  • [cyan]agent-worker start[/]                        — bring up worker (already up if you used --full)"
        )
    else:
        stopped = result.stopped_at
        console.print()
        if stopped:
            console.print(
                f"[bold red]✗ Bootstrap stopped at {stopped.name}.[/] {stopped.summary}"
            )
            if stopped.hint:
                console.print(f"[dim]→ {stopped.hint}[/]")


class _RichBootstrapIO:
    """:class:`orchestrator.IO` impl backed by rich + typer.confirm."""

    def __init__(self, console: Console, *, interactive: bool) -> None:
        self._console = console
        self._interactive = interactive

    def phase_start(self, name: str, description: str) -> None:
        self._console.rule(f"[bold]{name}[/] · {description}")

    def phase_finish(self, result: object) -> None:
        from app.setup.orchestrator import PhaseResult, PhaseStatus

        assert isinstance(result, PhaseResult)
        color = {
            PhaseStatus.PASS: "green",
            PhaseStatus.WARN: "yellow",
            PhaseStatus.FAIL: "red",
            PhaseStatus.SKIP: "dim",
        }[result.status]
        marker = {
            PhaseStatus.PASS: "✓",
            PhaseStatus.WARN: "!",
            PhaseStatus.FAIL: "✗",
            PhaseStatus.SKIP: "·",
        }[result.status]
        self._console.print(
            f"[{color}]{marker}[/] {result.name}: {result.summary}"
            + (f"  [dim]({result.elapsed_seconds:.1f}s)[/]" if result.elapsed_seconds else "")
        )
        if result.detail:
            for line in result.detail.splitlines():
                self._console.print(f"  [dim]{line}[/]")
        if result.hint and result.status != PhaseStatus.PASS:
            self._console.print(f"  [dim]hint:[/] {result.hint}")

    def info(self, message: str) -> None:
        self._console.print(f"  [dim]{message}[/]")

    def confirm(self, prompt: str, *, default: bool = True) -> bool:
        if not self._interactive:
            return default
        suffix = "[Y/n]" if default else "[y/N]"
        self._console.print(f"  {prompt} {suffix}: ", end="")
        try:
            raw = input().strip().lower()
        except EOFError:
            return default
        if not raw:
            return default
        return raw in {"y", "yes"}


# --------- start (convenience launcher) -------------------------------- #

@app.command("start")
def start_cmd(
    config_path: str | None = ConfigOpt,
    log_level: str | None = LogOpt,
    no_compose: bool = typer.Option(
        False, "--no-compose", help="Don't try to start docker compose."
    ),
    no_doctor: bool = typer.Option(
        False, "--no-doctor", help="Skip the preflight check.",
    ),
    foreground: bool = typer.Option(
        True, "--foreground/--background",
        help="Run the worker in the foreground (default) or as a detached process.",
    ),
) -> None:
    """Convenience launcher: doctor → docker compose up -d → worker.

    Quick way to bring everything up after a fresh ``init`` / ``bootstrap``.
    The ``worker`` subcommand is what does the actual work; this one just
    chains the prerequisites.
    """
    cfg = _bootstrap(config_path, log_level)

    if not no_doctor:
        console.rule("[bold]Preflight (read-only doctor)")
        from app.setup.doctor import run_doctor

        result = run_doctor(cfg, fix=False, check_temporal=not no_compose)
        _render_doctor(result)
        if result.fail_count:
            console.print(
                "[red]Doctor found FAILs.[/] Fix them or re-run with "
                "`agent-worker bootstrap` to apply auto-fixes."
            )
            raise typer.Exit(code=1)

    if not no_compose:
        compose_path = _project_root() / "docker-compose.yml"
        if not compose_path.exists():
            console.print(
                f"[yellow]No docker-compose.yml at {compose_path}; skipping.[/]"
            )
        elif not shutil.which("docker"):
            console.print("[yellow]docker not on PATH; skipping `docker compose up -d`.[/]")
        else:
            console.rule("[bold]docker compose up -d")
            rc = subprocess.call(
                ["docker", "compose", "up", "-d"], cwd=str(compose_path.parent)
            )
            if rc != 0:
                console.print(
                    "[yellow]docker compose returned non-zero; continuing anyway.[/]"
                )

    console.rule("[bold]Starting Temporal worker")
    if foreground:
        from app.temporal_app.worker import run_worker

        asyncio.run(run_worker())
    else:
        log_dir = _project_root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "worker.log"
        subprocess.Popen(
            ["agent-worker", "worker"],
            stdout=open(log_file, "ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        console.print(
            f"[green]Worker started in background.[/] Tail logs: "
            f"[cyan]tail -f {log_file.relative_to(_project_root())}[/]"
        )


# --------- helpers ------------------------------------------------------ #

def _project_root() -> Path:
    """Return the directory used for relative ``configs/`` and ``.env`` paths.

    Order of preference:
      1. ``$AGENT_WORKER_PROJECT_ROOT`` if set (explicit override).
      2. The current working directory — the right answer when the worker is
         installed as a package and the user is in their own project dir.

    We deliberately do *not* default to "the directory containing app/main.py";
    that breaks portability the moment someone ``pip install``s us. If someone
    is hacking on the worker repo itself, they'll naturally run from its root,
    so cwd still does the right thing.
    """
    override = os.environ.get("AGENT_WORKER_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path.cwd()


class _RichWizardIO:
    """:class:`WizardIO` impl backed by rich + typer.prompt."""

    def __init__(self, console: Console) -> None:
        self._console = console
        self._defaults: dict[str, str] = {}

    def ask(self, prompt: str, *, default: str = "", choices: list[str] | None = None) -> str:
        # Allow init_cmd to pre-seed defaults for specific prompts.
        if prompt in self._defaults:
            default = self._defaults[prompt]
        suffix = ""
        if choices:
            suffix = f" [{'/'.join(choices)}]"
        if default:
            full = f"{prompt}{suffix} [dim]({default})[/]: "
        else:
            full = f"{prompt}{suffix}: "
        while True:
            self._console.print(full, end="")
            try:
                raw = input().strip()
            except EOFError:
                raw = ""
            value = raw or default
            if choices and value not in choices:
                self._console.print(
                    f"[red]Please enter one of: {', '.join(choices)}[/]"
                )
                continue
            return value

    def ask_bool(self, prompt: str, *, default: bool = False) -> bool:
        default_str = "Y/n" if default else "y/N"
        while True:
            self._console.print(f"{prompt} [dim]({default_str})[/]: ", end="")
            try:
                raw = input().strip().lower()
            except EOFError:
                raw = ""
            if not raw:
                return default
            if raw in {"y", "yes", "true", "1"}:
                return True
            if raw in {"n", "no", "false", "0"}:
                return False
            self._console.print("[red]Please answer y or n.[/]")

    def info(self, message: str) -> None:
        # markup=False so single-letter [c]/[w]/[q] choices in our menu strings
        # aren't parsed as rich style tags and silently hidden.
        self._console.print(message, markup=False)

    def warn(self, message: str) -> None:
        self._console.print(f"[yellow]{message}[/]")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
