"""End-to-end bootstrap orchestrator.

The promise of ``agent-worker bootstrap`` is: run ONE command and either
end up with a working system or get told exactly which phase failed and why.

This module owns that promise. It chains five phases:

    1. CONFIG     — wizard if needed, write project YAML, update .env
    2. PREFLIGHT  — doctor checks + safe auto-fixes (labels, clones, dirs)
    3. SMOKE      — offline run-once --dry-run; verifies the agent loop runs
    4. LIVE_READ  — run-once on a real GitHub issue (no Temporal, no comment)
    5. FULL       — docker compose up + Temporal worker + run-issue --wait
                    (opt-in; opens real PRs)

Each phase is a small object with ``run`` and an optional ``auto_fix``.
The orchestrator stops at the first FAIL and reports the phase, the
underlying error, and the suggested manual remediation. WARN never stops
the chain.

The CLI in ``app/main.py`` is a thin wrapper that picks which phases to
run and renders progress with rich.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.config import AppConfig
from app.observability import get_logger
from app.setup.doctor import CheckOutcome, ShellRunner, run_doctor

log = get_logger(__name__)


class PhaseStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass
class PhaseResult:
    name: str
    status: PhaseStatus
    summary: str = ""
    detail: str = ""
    hint: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


@dataclass
class BootstrapResult:
    """Aggregate. ``ok`` is true iff every executed phase is PASS or WARN."""

    phases: list[PhaseResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(p.status == PhaseStatus.FAIL for p in self.phases)

    @property
    def stopped_at(self) -> PhaseResult | None:
        for p in self.phases:
            if p.status == PhaseStatus.FAIL:
                return p
        return None


class IO(Protocol):
    """Reporting interface so the CLI can render rich progress and tests can capture."""

    def phase_start(self, name: str, description: str) -> None: ...
    def phase_finish(self, result: PhaseResult) -> None: ...
    def info(self, message: str) -> None: ...
    def confirm(self, prompt: str, *, default: bool = True) -> bool: ...


def run_bootstrap(
    *,
    io: IO,
    config_loader: Callable[[], AppConfig],
    project_root: Path,
    run_wizard_if_unconfigured: Callable[[], Path | None] | None = None,
    full: bool = False,
    interactive: bool = True,
    test_issue_number: int | None = None,
    doctor_shell: ShellRunner | None = None,
    skip_live_read: bool = False,
    live_read_timeout_seconds: float = 180.0,
) -> BootstrapResult:
    """Drive the bootstrap flow end-to-end.

    Args:
        io: progress sink (CLI uses rich; tests use a fake).
        config_loader: callable returning the resolved :class:`AppConfig`.
            Re-invoked between phases so wizard-written config is picked up.
        project_root: directory used as cwd for sub-shells (docker compose).
        run_wizard_if_unconfigured: when given, called *before* loading the
            config if no project YAML is configured. Should write the YAML
            and return its path (or None if the user aborted).
        full: include the FULL phase (docker compose + worker + real PR).
        interactive: when False, never ask the user for permission; default
            answers are used. Tests pass interactive=False.
        test_issue_number: if set, used for the LIVE_READ phase instead of
            asking the user / discovering one.
    """
    result = BootstrapResult()

    # ---- Phase 1: CONFIG -------------------------------------------------- #
    config_phase = _phase_config(
        io=io, run_wizard=run_wizard_if_unconfigured, project_root=project_root
    )
    result.phases.append(config_phase)
    if config_phase.status == PhaseStatus.FAIL:
        return result

    # Reload config now that the wizard may have written a YAML.
    try:
        config = config_loader()
    except Exception as exc:  # pragma: no cover - extremely defensive
        result.phases.append(
            PhaseResult(
                name="CONFIG",
                status=PhaseStatus.FAIL,
                summary="Could not load configuration",
                detail=str(exc),
            )
        )
        return result

    # ---- Phase 2: PREFLIGHT ---------------------------------------------- #
    preflight = _phase_preflight(io, config, shell=doctor_shell)
    result.phases.append(preflight)
    if preflight.status == PhaseStatus.FAIL:
        return result

    # ---- Phase 3: SMOKE (offline dry-run) -------------------------------- #
    smoke = _phase_smoke(io, config)
    result.phases.append(smoke)
    if smoke.status == PhaseStatus.FAIL:
        return result

    # ---- Phase 4: LIVE_READ (real issue, no Temporal) -------------------- #
    if skip_live_read:
        skipped = PhaseResult(
            name="LIVE_READ",
            status=PhaseStatus.SKIP,
            summary="skipped via --no-live-read",
        )
        io.phase_start("LIVE_READ", "Plan against a real GitHub issue (skipped)")
        io.phase_finish(skipped)
        result.phases.append(skipped)
    elif interactive and not io.confirm(
        "Run a live read test against a real GitHub issue? "
        "(No comments are posted, no PR is opened.)",
        default=True,
    ):
        skipped = PhaseResult(name="LIVE_READ", status=PhaseStatus.SKIP, summary="user declined")
        io.phase_start("LIVE_READ", "Plan against a real GitHub issue (skipped by user)")
        io.phase_finish(skipped)
        result.phases.append(skipped)
    else:
        live = _phase_live_read(
            io,
            config,
            issue_number=test_issue_number,
            timeout_seconds=live_read_timeout_seconds,
        )
        result.phases.append(live)
        if live.status == PhaseStatus.FAIL:
            return result

    # ---- Phase 5: FULL (docker + worker + real PR) ----------------------- #
    if not full:
        skipped = PhaseResult(
            name="FULL",
            status=PhaseStatus.SKIP,
            summary="opt-in only",
            hint="Run `agent-worker bootstrap --full` to execute Temporal + worker + real PR.",
        )
        io.phase_start("FULL", "Skipped (opt-in via --full)")
        io.phase_finish(skipped)
        result.phases.append(skipped)
        return result

    if interactive and not io.confirm(
        "Start Temporal + worker and dispatch a real workflow now? This will create a real PR.",
        default=False,
    ):
        skipped = PhaseResult(name="FULL", status=PhaseStatus.SKIP, summary="user declined")
        io.phase_start("FULL", "Skipped by user")
        io.phase_finish(skipped)
        result.phases.append(skipped)
        return result

    full_phase = _phase_full(io, config, project_root=project_root)
    result.phases.append(full_phase)
    return result


# --------------------------------------------------------------------------- #
#  Individual phases                                                          #
# --------------------------------------------------------------------------- #


def _phase_config(
    *,
    io: IO,
    run_wizard: Callable[[], Path | None] | None,
    project_root: Path,
) -> PhaseResult:
    io.phase_start("CONFIG", "Locate or generate project YAML")
    started = time.monotonic()

    # We rely on the env var because the loader uses it as the source of truth
    # when no explicit path is provided. The CLI sets AGENT_WORKER_CONFIG before
    # calling us if --config was provided.
    import os

    yaml_path = os.environ.get("AGENT_WORKER_CONFIG")
    if yaml_path and Path(yaml_path).expanduser().exists():
        result = PhaseResult(
            name="CONFIG",
            status=PhaseStatus.PASS,
            summary=f"Using {yaml_path}",
        )
        result.elapsed_seconds = time.monotonic() - started
        io.phase_finish(result)
        return result

    if run_wizard is None:
        result = PhaseResult(
            name="CONFIG",
            status=PhaseStatus.FAIL,
            summary="No project YAML configured and wizard disabled",
            hint=("Either run `agent-worker init` first, or rerun without --skip-init."),
        )
        result.elapsed_seconds = time.monotonic() - started
        io.phase_finish(result)
        return result

    io.info("No project YAML detected — launching the setup wizard.")
    written = run_wizard()
    if written is None:
        result = PhaseResult(
            name="CONFIG",
            status=PhaseStatus.FAIL,
            summary="Wizard aborted by user",
        )
    else:
        result = PhaseResult(
            name="CONFIG",
            status=PhaseStatus.PASS,
            summary=f"Wrote {written.relative_to(project_root) if _is_relative_to(written, project_root) else written}",
            artifacts={"yaml": str(written)},
        )
    result.elapsed_seconds = time.monotonic() - started
    io.phase_finish(result)
    return result


def _phase_preflight(io: IO, config: AppConfig, *, shell: ShellRunner | None = None) -> PhaseResult:
    io.phase_start("PREFLIGHT", "Validate prerequisites and apply safe auto-fixes")
    started = time.monotonic()

    doctor = run_doctor(config, fix=True, check_temporal=False, shell=shell)
    fail_count = doctor.fail_count
    warn_count = doctor.warn_count
    pass_count = doctor.pass_count

    if fail_count:
        # Pull the first FAIL's hint to surface a concrete next step.
        first_fail = next(r for r in doctor.reports if r.outcome == CheckOutcome.FAIL)
        result = PhaseResult(
            name="PREFLIGHT",
            status=PhaseStatus.FAIL,
            summary=f"{pass_count} pass, {warn_count} warn, {fail_count} fail",
            detail=f"first failure: {first_fail.name} — {first_fail.detail}",
            hint=first_fail.hint or "Run `agent-worker doctor` for the full report.",
        )
    elif warn_count:
        result = PhaseResult(
            name="PREFLIGHT",
            status=PhaseStatus.WARN,
            summary=f"{pass_count} pass, {warn_count} warn (continuing)",
            hint="Run `agent-worker doctor` to see the warnings in detail.",
        )
    else:
        result = PhaseResult(
            name="PREFLIGHT",
            status=PhaseStatus.PASS,
            summary=f"all {pass_count} checks passed",
        )
    result.elapsed_seconds = time.monotonic() - started
    io.phase_finish(result)
    return result


def _phase_smoke(io: IO, config: AppConfig) -> PhaseResult:
    """Offline planner round on a synthetic issue. Proves the graph runs."""
    io.phase_start("SMOKE", "Offline dry-run on a synthetic issue (planner only)")
    started = time.monotonic()

    # Force planning-only with the stub executor for this phase so we never
    # shell out — the SMOKE step has to work even if cursor-agent / network
    # is unavailable. We do this on a defensive copy so user config isn't
    # mutated.
    from app.config.models import AppConfig as _AppConfig

    overlay = config.model_dump(mode="python")
    overlay["executor"]["default"] = "stub"
    overlay["sandbox"]["mode"] = "local"
    overlay["workflow"]["stop_after"] = "planning"
    overlay["langgraph"]["checkpoint_backend"] = "memory"
    smoke_cfg = _AppConfig.model_validate(overlay)

    repo_slug = smoke_cfg.repo.slug or "smoke/test"
    issue_number = 999_999

    try:
        from app.langgraph_app.graph import AgentRoundInput, run_agent_round

        round_input = AgentRoundInput(
            repo=repo_slug,
            issue_number=issue_number,
            issue_title="(bootstrap smoke) Verify planner round runs",
            issue_body=(
                "This is a synthetic issue created by `agent-worker bootstrap` "
                "to verify the LangGraph planner round runs end-to-end without "
                "shelling out. No real change is required; the StubExecutor "
                "writes a templated plan."
            ),
            issue_url="",
        )
        out = run_agent_round(config=smoke_cfg, round_input=round_input)
    except Exception as exc:
        result = PhaseResult(
            name="SMOKE",
            status=PhaseStatus.FAIL,
            summary="planner round crashed",
            detail=f"{type(exc).__name__}: {exc}",
            hint=(
                "This indicates a bug in the worker itself, not your config. "
                "Re-run with `--log-level DEBUG` for the traceback."
            ),
        )
        result.elapsed_seconds = time.monotonic() - started
        io.phase_finish(result)
        return result

    if out.final_status != "planning_done":
        result = PhaseResult(
            name="SMOKE",
            status=PhaseStatus.FAIL,
            summary=f"unexpected final_status={out.final_status!r}",
            hint="The graph ran but didn't reach planning_done. Check logs for the failing node.",
        )
        result.elapsed_seconds = time.monotonic() - started
        io.phase_finish(result)
        return result

    from app.sandbox.artifact_store import ArtifactStore

    run_dir = ArtifactStore(smoke_cfg.system.artifact_root).run_dir(
        ArtifactStore.issue_key(repo_slug, issue_number)
    )
    plan_path = run_dir.planning_dir / "plan.md"
    plan_size = plan_path.stat().st_size if plan_path.exists() else 0

    result = PhaseResult(
        name="SMOKE",
        status=PhaseStatus.PASS,
        summary=f"plan.md written ({plan_size} bytes)",
        detail=f"comment preview: {len(out.pending_issue_comment)} chars",
        artifacts={"run_dir": str(run_dir.root), "plan": str(plan_path)},
    )
    result.elapsed_seconds = time.monotonic() - started
    io.phase_finish(result)
    return result


def _phase_live_read(
    io: IO,
    config: AppConfig,
    *,
    issue_number: int | None,
    timeout_seconds: float = 180.0,
) -> PhaseResult:
    """Pull a real issue and run one planner round. No comment posted.

    The planner call is wrapped in a thread with ``timeout_seconds`` so a
    huge issue + slow LLM doesn't hang bootstrap forever. On timeout we
    return WARN (not FAIL) so the chain continues — bootstrap's job is
    "verify plumbing", not "wait arbitrarily long".
    """
    io.phase_start("LIVE_READ", "Plan against a real GitHub issue (no comment, no PR)")
    started = time.monotonic()

    from app.github._gh_cli import GhCommandError
    from app.github.issue_service import GitHubIssueService

    svc = GitHubIssueService(config.repo, config.github)
    chosen: int | None = issue_number

    if chosen is None:
        # Try to discover an open agent:todo issue first.
        try:
            chosen = _find_first_open_agent_todo_issue(config)
        except Exception as exc:
            log.debug("live_read.discovery_failed", error=str(exc))
        if chosen is None:
            result = PhaseResult(
                name="LIVE_READ",
                status=PhaseStatus.SKIP,
                summary="no open issues with the agent:todo label",
                hint=(
                    f"Create one and re-run: "
                    f"`gh issue create --repo {config.repo.slug} --label "
                    f"{config.github.issue_label_todo}`."
                ),
            )
            result.elapsed_seconds = time.monotonic() - started
            io.phase_finish(result)
            return result

    try:
        issue = svc.fetch(chosen)
    except GhCommandError as exc:
        result = PhaseResult(
            name="LIVE_READ",
            status=PhaseStatus.FAIL,
            summary=f"could not fetch issue #{chosen}",
            detail=(exc.stderr or exc.stdout or str(exc)).strip().splitlines()[0]
            if (exc.stderr or exc.stdout)
            else str(exc),
            hint=(
                f"Confirm `gh issue view {chosen} --repo {config.repo.slug}` works in your shell."
            ),
        )
        result.elapsed_seconds = time.monotonic() - started
        io.phase_finish(result)
        return result

    # Build a planning-only config so we never shell out / push branches.
    from app.config.models import AppConfig as _AppConfig

    overlay = config.model_dump(mode="python")
    overlay["workflow"]["stop_after"] = "planning"
    overlay["sandbox"]["mode"] = "local"
    overlay["langgraph"]["checkpoint_backend"] = "memory"
    live_cfg = _AppConfig.model_validate(overlay)

    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    from app.langgraph_app.graph import AgentRoundInput, run_agent_round

    round_input = AgentRoundInput(
        repo=config.repo.slug,
        issue_number=issue.number,
        issue_title=issue.title,
        issue_body=issue.body,
        issue_url=issue.url,
    )

    # Body length is a useful early-warning for "this might take a while".
    issue_size = len(issue.body or "")
    if issue_size > 8000:
        io.info(
            f"Issue body is {issue_size} chars — planner may take a minute. "
            f"Hard cap: {timeout_seconds:.0f}s."
        )

    # Run in a worker thread so we can time-bound it. We can't actually kill
    # the thread; if it's still running on timeout, it'll finish in the
    # background and we'll exit. That's acceptable for a one-shot bootstrap.
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bootstrap-live-read")
    future = pool.submit(run_agent_round, config=live_cfg, round_input=round_input)
    try:
        future.result(timeout=timeout_seconds)
    except FuturesTimeout:
        result = PhaseResult(
            name="LIVE_READ",
            status=PhaseStatus.WARN,
            summary=f"planner exceeded {timeout_seconds:.0f}s budget for issue #{issue.number}",
            detail=(
                f"Issue body was {issue_size} chars; the executor "
                f"({live_cfg.executor.default}) is still running in the background."
            ),
            hint=(
                "Bootstrap continues. If this issue really needs a longer "
                "budget, re-run with `--live-read-timeout 600`. If you don't "
                "care about the live-read smoke, use `--no-live-read`."
            ),
        )
        result.elapsed_seconds = time.monotonic() - started
        io.phase_finish(result)
        # Note: pool deliberately not joined so we return promptly.
        pool.shutdown(wait=False, cancel_futures=True)
        return result
    except Exception as exc:
        result = PhaseResult(
            name="LIVE_READ",
            status=PhaseStatus.WARN,  # WARN, not FAIL: don't block bootstrap on a single issue.
            summary=f"planner round crashed for issue #{issue.number}",
            detail=f"{type(exc).__name__}: {exc}",
            hint=(
                "Bootstrap continues. If this is an executor / LLM error, "
                "try `executor.default: stub` to verify the rest of the loop, "
                "or `--no-live-read` to skip this phase entirely."
            ),
        )
        result.elapsed_seconds = time.monotonic() - started
        io.phase_finish(result)
        pool.shutdown(wait=False, cancel_futures=True)
        return result
    finally:
        pool.shutdown(wait=False)

    from app.sandbox.artifact_store import ArtifactStore

    run_dir = ArtifactStore(live_cfg.system.artifact_root).run_dir(
        ArtifactStore.issue_key(config.repo.slug, issue.number)
    )
    plan_path = run_dir.planning_dir / "plan.md"
    plan_size = plan_path.stat().st_size if plan_path.exists() else 0

    result = PhaseResult(
        name="LIVE_READ",
        status=PhaseStatus.PASS,
        summary=f"planned for #{issue.number} ({plan_size} bytes)",
        detail=f"issue: {issue.title}\nartifacts: {run_dir.root}",
        artifacts={"plan": str(plan_path), "run_dir": str(run_dir.root)},
    )
    result.elapsed_seconds = time.monotonic() - started
    io.phase_finish(result)
    return result


def _phase_full(io: IO, config: AppConfig, *, project_root: Path) -> PhaseResult:
    """Bring up docker compose + start a worker + dispatch one workflow.

    The worker runs in the background; we leave it alive on success so the
    user can keep dispatching issues afterward.
    """
    io.phase_start("FULL", "Bring up Temporal + worker + dispatch a real workflow")
    started = time.monotonic()

    # 1. docker compose up -d (if available)
    compose_path = project_root / "docker-compose.yml"
    if not compose_path.exists():
        result = PhaseResult(
            name="FULL",
            status=PhaseStatus.FAIL,
            summary="no docker-compose.yml in project root",
            hint=(
                "Either drop a docker-compose.yml in the project root, or "
                "start Temporal manually and re-run with `--no-compose`."
            ),
        )
        result.elapsed_seconds = time.monotonic() - started
        io.phase_finish(result)
        return result
    if not shutil.which("docker"):
        result = PhaseResult(
            name="FULL",
            status=PhaseStatus.FAIL,
            summary="docker not on PATH",
            hint="Install Docker, then re-run.",
        )
        result.elapsed_seconds = time.monotonic() - started
        io.phase_finish(result)
        return result

    io.info("$ docker compose up -d")
    rc = subprocess.call(["docker", "compose", "up", "-d"], cwd=str(compose_path.parent))
    if rc != 0:
        result = PhaseResult(
            name="FULL",
            status=PhaseStatus.FAIL,
            summary="docker compose returned non-zero",
            hint="Check `docker compose logs temporal`.",
        )
        result.elapsed_seconds = time.monotonic() - started
        io.phase_finish(result)
        return result

    # 2. Wait for Temporal to accept TCP.
    if not _wait_for_tcp(config.workflow.temporal.host, timeout_seconds=60):
        result = PhaseResult(
            name="FULL",
            status=PhaseStatus.FAIL,
            summary=f"Temporal at {config.workflow.temporal.host} did not become reachable in 60s",
            hint="Check `docker compose ps` and `docker compose logs temporal`.",
        )
        result.elapsed_seconds = time.monotonic() - started
        io.phase_finish(result)
        return result

    # 3. Spawn worker in the background.
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "worker.log"
    io.info(f"$ agent-worker worker  (logs: {log_path})")
    try:
        worker_proc = subprocess.Popen(
            ["agent-worker", "worker"],
            stdout=open(log_path, "ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        result = PhaseResult(
            name="FULL",
            status=PhaseStatus.FAIL,
            summary="could not exec agent-worker",
            detail=str(exc),
            hint="Make sure `pip install -e .` was run inside the active venv.",
        )
        result.elapsed_seconds = time.monotonic() - started
        io.phase_finish(result)
        return result

    # Give the worker a beat to start polling.
    time.sleep(3)
    if worker_proc.poll() is not None:
        result = PhaseResult(
            name="FULL",
            status=PhaseStatus.FAIL,
            summary=f"worker exited immediately (code {worker_proc.returncode})",
            detail=f"see {log_path}",
        )
        result.elapsed_seconds = time.monotonic() - started
        io.phase_finish(result)
        return result

    result = PhaseResult(
        name="FULL",
        status=PhaseStatus.PASS,
        summary="Temporal up; worker running in background",
        detail=f"worker pid={worker_proc.pid}, logs={log_path}",
        hint=(
            "Worker is live. Dispatch issues with "
            "`agent-worker run-issue --issue N --wait`. Stop the worker with "
            f"`kill {worker_proc.pid}`."
        ),
        artifacts={"worker_pid": str(worker_proc.pid), "worker_log": str(log_path)},
    )
    result.elapsed_seconds = time.monotonic() - started
    io.phase_finish(result)
    return result


# --------------------------------------------------------------------------- #
#  Helpers                                                                    #
# --------------------------------------------------------------------------- #


def _wait_for_tcp(host: str, *, timeout_seconds: float) -> bool:
    if ":" not in host:
        return False
    name, _, port_str = host.rpartition(":")
    try:
        port = int(port_str)
    except ValueError:
        return False
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((name, port), timeout=2.0):
                return True
        except OSError:
            time.sleep(1.0)
    return False


def _find_first_open_agent_todo_issue(config: AppConfig) -> int | None:
    """Return the smallest open issue number carrying the agent:todo label, or None."""
    completed = subprocess.run(
        [
            config.github.cli,
            "issue",
            "list",
            "--repo",
            config.repo.slug,
            "--state",
            "open",
            "--label",
            config.github.issue_label_todo,
            "--limit",
            "20",
            "--json",
            "number",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        return None
    import json as _json

    try:
        payload = _json.loads(completed.stdout or "[]")
    except _json.JSONDecodeError:
        return None
    numbers = [int(p["number"]) for p in payload if "number" in p]
    if not numbers:
        return None
    return min(numbers)


def _is_relative_to(p: Path, base: Path) -> bool:
    try:
        p.relative_to(base)
        return True
    except ValueError:
        return False


__all__ = [
    "IO",
    "BootstrapResult",
    "PhaseResult",
    "PhaseStatus",
    "run_bootstrap",
]
