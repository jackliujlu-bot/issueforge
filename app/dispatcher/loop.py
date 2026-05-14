"""Dispatcher loop: convert ``agent:todo`` labels into running workflows.

This file implements the second half of the architecture's "GitHub Webhook /
Dispatcher → Start Temporal Workflow" link. Instead of relying on a public
webhook server (which requires inbound network access), we poll. The polling
loop runs inside the worker process — one long-running asyncio task alongside
the Temporal :class:`~temporalio.worker.Worker`.

Two responsibilities, both deliberately small:

1. **Pickup new todo issues** — list ``agent:todo`` issues and call
   :func:`app.temporal_app.client.start_issue_workflow`. Idempotent: the
   stable workflow id means re-dispatching is a no-op while a workflow is
   running, and a closed workflow is re-started (handled by the client
   helper).

2. **Recover stuck blocked issues** — list ``agent:blocked`` issues, inspect
   the associated PR's real CI status (filtered through
   ``github.ci_ignore_workflows`` so human-gated workflows don't poison the
   verdict), and either:

       - CI passed   → mark the issue ``agent:done`` and comment.
       - CI failed   → re-dispatch the workflow so the coder can retry with
         the failure log surfaced.
       - CI pending  → leave alone (will be checked again next cycle).

Recovery is rate-limited per-issue so a workflow that keeps hitting the same
genuine CI failure doesn't get re-dispatched in a tight loop. The limit is
purely in-process; restarting the worker resets the throttle, which is fine —
better to over-retry than to silently get stuck.

The loop is unit-tested via injectable dependencies (:class:`DispatcherDeps`);
the production wiring constructs the real GitHub services + Temporal client.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from app.config.models import AppConfig
from app.github.ci_service import CIPollResult
from app.github.issue_service import Issue
from app.observability import get_logger
from app.temporal_app.client import DispatchOutcome, WorkflowStatusLabel

log = get_logger(__name__)


# --------- Configuration ---------------------------------------------------- #


@dataclass
class DispatcherConfig:
    """Knobs for one dispatcher loop instance.

    Defaults are tuned for "lots of issues, slow CI" (the dimos case): poll
    every 30s, allow up to 10 dispatches per cycle, attempt blocked-recovery
    no more than once per 10 minutes per issue, orphan-scan every 5 minutes.
    """

    poll_interval_seconds: int = 30
    max_dispatch_per_cycle: int = 10
    auto_recover_blocked: bool = True
    blocked_recover_min_interval_seconds: int = 600
    # Orphan revival: re-dispatch issues whose label says in-flight but whose
    # Temporal workflow is absent / closed.
    revive_orphans: bool = True
    orphan_check_interval_seconds: int = 300
    orphan_revive_min_interval_seconds: int = 900
    # Optional safety cap: stop the loop after N cycles. ``0`` = unbounded.
    max_cycles: int = 0


# --------- Dependency injection (testability) ------------------------------- #


@dataclass
class _DispatchedHandle:
    """Outcome of one ``start_issue_workflow`` call. Kept small so the
    dispatcher loop has no temporalio-specific imports — easier to unit-test."""

    workflow_id: str
    outcome: DispatchOutcome


class _IssuesAPI(Protocol):
    def list_open_with_label(self, label: str, *, limit: int = ...) -> list[Issue]: ...
    def list_open_with_any_label(
        self, labels: list[str], *, limit_per_label: int = ...
    ) -> list[Issue]: ...
    def comment(self, issue_number: int, body: str) -> None: ...
    def transition_label(
        self,
        issue_number: int,
        *,
        to_label: str,
        from_labels: list[str] | None = ...,
    ) -> None: ...


class _PRAPI(Protocol):
    def find_for_branch(self, head_branch: str):  # noqa: ANN201 - structural
        ...


class _CIAPI(Protocol):
    def poll(
        self,
        *,
        pr_number: int | None = ...,
        head_branch: str | None = ...,
    ) -> CIPollResult: ...


Dispatcher = Callable[[int], Awaitable[_DispatchedHandle]]
WorkflowStatusFn = Callable[[str], Awaitable[WorkflowStatusLabel]]


@dataclass
class DispatcherDeps:
    """Pluggable seams for testing.

    Production code calls :func:`build_default_deps` to wire the real GitHub
    services + Temporal client into this struct.
    """

    issues: _IssuesAPI
    prs: _PRAPI
    ci: _CIAPI
    dispatch: Dispatcher
    # Inspect a Temporal workflow by id and return a string label. Used by the
    # orphan-revival scan; ``"absent"`` means "Temporal has no record of this
    # workflow id at all". Provide a no-op (e.g. ``lambda _: "absent"``) when
    # orphan revival is off in tests.
    workflow_status: WorkflowStatusFn
    # ``now()`` is overridable so the recovery throttle is testable without
    # ``time.sleep``.
    now: Callable[[], float] = field(default_factory=lambda: time.monotonic)


# --------- Iteration result types ------------------------------------------ #


class _BlockedDecision(Enum):
    NO_PR_YET = "no_pr_yet"
    CI_PENDING = "ci_pending"
    CI_PASSED = "ci_passed"
    CI_FAILED = "ci_failed"


@dataclass
class DispatcherStats:
    """Per-cycle counts. Surfaced to logs/metrics, not to users directly."""

    todo_seen: int = 0
    todo_dispatched: int = 0
    todo_attached_running: int = 0
    todo_restarted: int = 0
    blocked_seen: int = 0
    blocked_marked_done: int = 0
    blocked_redispatched: int = 0
    blocked_skipped_pending: int = 0
    blocked_skipped_throttled: int = 0
    orphan_scan_ran: bool = False
    orphan_seen: int = 0
    orphan_healthy: int = 0
    orphan_revived: int = 0
    orphan_skipped_throttled: int = 0
    errors: int = 0


@dataclass
class DispatcherIteration:
    """Full result of one cycle. Includes per-issue notes for debuggability."""

    stats: DispatcherStats = field(default_factory=DispatcherStats)
    notes: list[str] = field(default_factory=list)


# --------- Public API ------------------------------------------------------ #


async def run_one_iteration(
    *,
    config: AppConfig,
    dispatcher_config: DispatcherConfig,
    deps: DispatcherDeps,
    state: "_RecoveryState | None" = None,
) -> DispatcherIteration:
    """Execute one dispatcher cycle and return what happened.

    Idempotent and side-effect-typed: every state change (dispatch,
    relabel, comment) is logged + counted on the returned struct.
    """
    state = state or _RecoveryState()
    result = DispatcherIteration()

    await _handle_todo(
        config=config,
        dispatcher_config=dispatcher_config,
        deps=deps,
        result=result,
    )

    if dispatcher_config.auto_recover_blocked:
        await _handle_blocked(
            config=config,
            dispatcher_config=dispatcher_config,
            deps=deps,
            state=state,
            result=result,
        )

    if dispatcher_config.revive_orphans:
        await _handle_orphans(
            config=config,
            dispatcher_config=dispatcher_config,
            deps=deps,
            state=state,
            result=result,
        )

    return result


async def run_dispatcher_loop(
    *,
    config: AppConfig,
    dispatcher_config: DispatcherConfig | None = None,
    deps: DispatcherDeps | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the dispatcher until cancelled (or ``stop_event`` is set).

    Designed to be ``asyncio.create_task``'d alongside a Temporal worker. The
    loop catches and logs all exceptions so a transient GitHub outage doesn't
    take the worker process down with it.
    """
    cfg = dispatcher_config or DispatcherConfig()
    if deps is None:
        deps = build_default_deps(config)
    state = _RecoveryState()
    stop_event = stop_event or asyncio.Event()

    log.info(
        "dispatcher.starting",
        repo=config.repo.slug,
        poll_interval_seconds=cfg.poll_interval_seconds,
        auto_recover_blocked=cfg.auto_recover_blocked,
    )

    cycle = 0
    while not stop_event.is_set():
        cycle += 1
        try:
            outcome = await run_one_iteration(
                config=config,
                dispatcher_config=cfg,
                deps=deps,
                state=state,
            )
            stats = outcome.stats
            if (
                stats.todo_dispatched
                or stats.blocked_marked_done
                or stats.blocked_redispatched
                or stats.orphan_revived
                or stats.errors
            ):
                log.info(
                    "dispatcher.cycle",
                    cycle=cycle,
                    **stats.__dict__,
                )
            else:
                log.debug("dispatcher.cycle.quiet", cycle=cycle, **stats.__dict__)
        except asyncio.CancelledError:
            log.info("dispatcher.cancelled", cycle=cycle)
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("dispatcher.cycle_failed", cycle=cycle, error=str(exc))

        if cfg.max_cycles and cycle >= cfg.max_cycles:
            log.info("dispatcher.max_cycles_reached", cycle=cycle)
            return

        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=cfg.poll_interval_seconds
            )
        except asyncio.TimeoutError:
            continue
        else:
            break

    log.info("dispatcher.stopped")


# --------- Production deps wiring ----------------------------------------- #


def build_default_deps(config: AppConfig) -> DispatcherDeps:
    """Wire the real GitHub services + Temporal client into ``DispatcherDeps``."""
    from app.github.ci_service import GitHubCIService
    from app.github.issue_service import GitHubIssueService
    from app.github.pr_service import GitHubPRService
    from app.temporal_app.client import (
        describe_workflow_status,
        start_issue_workflow,
    )

    issues = GitHubIssueService(config.repo, config.github)
    prs = GitHubPRService(config.repo, config.github)
    ci = GitHubCIService(config.repo, config.github)

    async def _dispatch(issue_number: int) -> _DispatchedHandle:
        handle, outcome = await start_issue_workflow(
            config, issue_number=issue_number
        )
        return _DispatchedHandle(workflow_id=handle.id, outcome=outcome)

    async def _workflow_status(workflow_id: str) -> WorkflowStatusLabel:
        return await describe_workflow_status(config, workflow_id=workflow_id)

    return DispatcherDeps(
        issues=issues,
        prs=prs,
        ci=ci,
        dispatch=_dispatch,
        workflow_status=_workflow_status,
    )


# --------- Internal helpers ----------------------------------------------- #


@dataclass
class _RecoveryState:
    """In-process throttle for blocked-issue recovery and orphan revival.

    Restarting the worker resets this; that's deliberate — over-retry is
    safer than silent stalling.
    """

    last_recovery_attempt_at: dict[int, float] = field(default_factory=dict)
    last_orphan_attempt_at: dict[int, float] = field(default_factory=dict)
    last_orphan_scan_at: float = 0.0


async def _handle_todo(
    *,
    config: AppConfig,
    dispatcher_config: DispatcherConfig,
    deps: DispatcherDeps,
    result: DispatcherIteration,
) -> None:
    label = config.github.issue_label_todo
    try:
        todo_issues = deps.issues.list_open_with_label(
            label, limit=dispatcher_config.max_dispatch_per_cycle * 4
        )
    except Exception as exc:
        log.warning("dispatcher.todo_list_failed", error=str(exc))
        result.stats.errors += 1
        result.notes.append(f"failed to list label={label!r}: {exc}")
        return

    result.stats.todo_seen = len(todo_issues)
    dispatched = 0
    for issue in todo_issues:
        if dispatched >= dispatcher_config.max_dispatch_per_cycle:
            result.notes.append(
                f"hit max_dispatch_per_cycle={dispatcher_config.max_dispatch_per_cycle}; "
                f"deferring remaining {len(todo_issues) - dispatched} todo issues"
            )
            break
        if issue.number <= 0:
            continue
        try:
            handle = await deps.dispatch(issue.number)
        except Exception as exc:
            log.warning(
                "dispatcher.dispatch_failed",
                issue=issue.number,
                error=str(exc),
            )
            result.stats.errors += 1
            result.notes.append(f"#{issue.number}: dispatch failed: {exc}")
            continue
        dispatched += 1
        if handle.outcome == "started":
            result.stats.todo_dispatched += 1
        elif handle.outcome == "attached_running":
            result.stats.todo_attached_running += 1
        elif handle.outcome == "restarted_after_close":
            result.stats.todo_dispatched += 1
            result.stats.todo_restarted += 1
        log.info(
            "dispatcher.dispatched",
            issue=issue.number,
            workflow_id=handle.workflow_id,
            outcome=handle.outcome,
        )


async def _handle_blocked(
    *,
    config: AppConfig,
    dispatcher_config: DispatcherConfig,
    deps: DispatcherDeps,
    state: _RecoveryState,
    result: DispatcherIteration,
) -> None:
    label = config.github.issue_label_blocked
    try:
        blocked = deps.issues.list_open_with_label(label, limit=50)
    except Exception as exc:
        log.warning("dispatcher.blocked_list_failed", error=str(exc))
        result.stats.errors += 1
        result.notes.append(f"failed to list label={label!r}: {exc}")
        return

    result.stats.blocked_seen = len(blocked)
    now = deps.now()
    throttle_seconds = dispatcher_config.blocked_recover_min_interval_seconds

    for issue in blocked:
        last_attempt = state.last_recovery_attempt_at.get(issue.number, 0.0)
        if now - last_attempt < throttle_seconds:
            result.stats.blocked_skipped_throttled += 1
            continue

        # Mark the attempt up front so any exception below still counts; we
        # don't want a poison-pill issue to retry every cycle.
        state.last_recovery_attempt_at[issue.number] = now

        decision = await _classify_blocked_issue(
            config=config,
            deps=deps,
            issue=issue,
        )

        if decision is _BlockedDecision.NO_PR_YET:
            result.stats.blocked_skipped_pending += 1
            result.notes.append(
                f"#{issue.number}: no PR for agent branch yet — leaving as blocked"
            )
            continue

        if decision is _BlockedDecision.CI_PENDING:
            result.stats.blocked_skipped_pending += 1
            continue

        if decision is _BlockedDecision.CI_PASSED:
            try:
                deps.issues.comment(
                    issue.number,
                    _passed_comment(issue=issue),
                )
                deps.issues.transition_label(
                    issue.number,
                    to_label=config.github.issue_label_done,
                    from_labels=_terminal_label_set(config),
                )
            except Exception as exc:
                log.warning(
                    "dispatcher.blocked_complete_failed",
                    issue=issue.number,
                    error=str(exc),
                )
                result.stats.errors += 1
                continue
            result.stats.blocked_marked_done += 1
            log.info("dispatcher.blocked.passed", issue=issue.number)
            continue

        # CI_FAILED → re-dispatch the workflow. The IssueAgentWorkflow's first
        # activity is ``transition_issue_label`` from agent:todo → agent:running,
        # but it tolerates the source label being absent, so we can dispatch
        # straight from agent:blocked.
        try:
            handle = await deps.dispatch(issue.number)
        except Exception as exc:
            log.warning(
                "dispatcher.blocked_redispatch_failed",
                issue=issue.number,
                error=str(exc),
            )
            result.stats.errors += 1
            continue

        result.stats.blocked_redispatched += 1
        log.info(
            "dispatcher.blocked.redispatched",
            issue=issue.number,
            workflow_id=handle.workflow_id,
            outcome=handle.outcome,
        )


async def _handle_orphans(
    *,
    config: AppConfig,
    dispatcher_config: DispatcherConfig,
    deps: DispatcherDeps,
    state: _RecoveryState,
    result: DispatcherIteration,
) -> None:
    """Re-dispatch issues whose label is in-flight but whose workflow isn't.

    Two failure modes this covers:

    1. **Temporal DB reset.** The workflow id is unknown to Temporal
       (``"absent"``) but the issue is still labelled ``agent-running`` /
       ``agent-planning`` etc. — typical after switching Temporal from
       in-memory dev mode to a persistent DB, or after a crash that lost
       the DB. We re-dispatch; ``start_issue_workflow`` creates a fresh run.

    2. **Worker crashed mid-round.** The workflow finished (``"completed"`` /
       ``"failed"`` / ``"terminated"``) but its terminal-label transition
       didn't reach GitHub — so the issue stays on, say, ``agent-coding``
       forever. Re-dispatching restarts the workflow, which moves the label
       forward as soon as the first activity runs.

    Throttled per-issue by ``orphan_revive_min_interval_seconds`` so a
    chronically-orphaned issue doesn't get thrashed.

    The scan as a whole only runs at most once every
    ``orphan_check_interval_seconds`` to keep the gh-API budget reasonable;
    other cycles fall through cheaply.
    """
    now = deps.now()
    if now - state.last_orphan_scan_at < dispatcher_config.orphan_check_interval_seconds:
        return
    state.last_orphan_scan_at = now
    result.stats.orphan_scan_ran = True

    in_flight_labels = _in_flight_labels(config)
    try:
        issues = deps.issues.list_open_with_any_label(
            in_flight_labels, limit_per_label=50
        )
    except Exception as exc:
        log.warning("dispatcher.orphan_list_failed", error=str(exc))
        result.stats.errors += 1
        return

    result.stats.orphan_seen = len(issues)

    for issue in issues:
        # Skip if this issue has actually moved on (and just happens to have
        # one of our in-flight labels left over alongside agent-done etc.).
        if _has_terminal_label(config, issue):
            continue

        last_attempt = state.last_orphan_attempt_at.get(issue.number, 0.0)
        if now - last_attempt < dispatcher_config.orphan_revive_min_interval_seconds:
            result.stats.orphan_skipped_throttled += 1
            continue

        workflow_id = _workflow_id_for(config, issue.number)
        try:
            status = await deps.workflow_status(workflow_id)
        except Exception as exc:
            log.warning(
                "dispatcher.workflow_status_failed",
                issue=issue.number,
                workflow_id=workflow_id,
                error=str(exc),
            )
            result.stats.errors += 1
            continue

        if status == "running":
            result.stats.orphan_healthy += 1
            continue

        # Reserve the attempt timestamp up front so a recurring failure
        # doesn't tight-loop the issue.
        state.last_orphan_attempt_at[issue.number] = now

        try:
            handle = await deps.dispatch(issue.number)
        except Exception as exc:
            log.warning(
                "dispatcher.orphan_revive_failed",
                issue=issue.number,
                workflow_id=workflow_id,
                workflow_status=status,
                error=str(exc),
            )
            result.stats.errors += 1
            continue

        result.stats.orphan_revived += 1
        log.info(
            "dispatcher.orphan.revived",
            issue=issue.number,
            workflow_id=handle.workflow_id,
            outcome=handle.outcome,
            prior_workflow_status=status,
            labels=issue.labels,
        )


def _in_flight_labels(config: AppConfig) -> list[str]:
    """Labels that mean 'a workflow should be actively driving this issue'.

    ``agent-blocked`` is intentionally absent — that one's the responsibility
    of :func:`_handle_blocked`, which inspects CI rather than Temporal.
    """
    g = config.github
    return [
        g.issue_label_running,
        g.issue_label_planning,
        g.issue_label_coding,
        g.issue_label_testing,
        g.issue_label_pr_created,
        g.issue_label_ci_running,
    ]


def _has_terminal_label(config: AppConfig, issue: Issue) -> bool:
    g = config.github
    terminal = {g.issue_label_done, g.issue_label_failed}
    return any(lab in terminal for lab in issue.labels)


def _workflow_id_for(config: AppConfig, issue_number: int) -> str:
    """Inline copy of ``temporal.client.stable_workflow_id`` to avoid the
    import cost of pulling in the temporalio module just for an id format."""
    repo_slug = config.repo.slug or "repo"
    safe_repo = repo_slug.replace("/", "--")
    return f"issue-agent--{safe_repo}--issue-{issue_number}"


async def _classify_blocked_issue(
    *,
    config: AppConfig,
    deps: DispatcherDeps,
    issue: Issue,
) -> _BlockedDecision:
    branch = _agent_branch(config, issue.number)
    try:
        pr = deps.prs.find_for_branch(branch)
    except Exception as exc:
        log.warning(
            "dispatcher.find_pr_failed",
            issue=issue.number,
            branch=branch,
            error=str(exc),
        )
        return _BlockedDecision.NO_PR_YET

    if pr is None:
        return _BlockedDecision.NO_PR_YET

    try:
        poll = deps.ci.poll(pr_number=pr.number, head_branch=branch)
    except Exception as exc:
        log.warning(
            "dispatcher.ci_poll_failed",
            issue=issue.number,
            pr=pr.number,
            error=str(exc),
        )
        return _BlockedDecision.CI_PENDING

    if poll.status == "passed":
        return _BlockedDecision.CI_PASSED
    if poll.status == "failed":
        return _BlockedDecision.CI_FAILED
    # "pending" or "unknown" — wait it out.
    return _BlockedDecision.CI_PENDING


def _agent_branch(config: AppConfig, issue_number: int) -> str:
    prefix = (config.repo.working_branch_prefix or "agent").strip("/")
    return f"{prefix}/issue-{issue_number}"


def _terminal_label_set(config: AppConfig) -> list[str]:
    g = config.github
    return [
        g.issue_label_blocked,
        g.issue_label_ci_running,
        g.issue_label_pr_created,
        g.issue_label_review,
        g.issue_label_running,
        g.issue_label_planning,
        g.issue_label_coding,
        g.issue_label_testing,
    ]


def _passed_comment(*, issue: Issue) -> str:
    return (
        "Dispatcher: detected that the previously-blocked PR for this issue "
        "now reports a passing CI status (with ignored workflows filtered out). "
        "Marking the issue as done. If this is wrong, relabel manually."
    )
