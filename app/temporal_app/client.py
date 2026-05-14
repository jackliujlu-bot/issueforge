"""Temporal client helpers."""

from __future__ import annotations

from typing import Literal

from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowHandle,
)

from app.config.models import AppConfig
from app.observability import get_logger
from app.temporal_app.workflows import IssueAgentInput, IssueAgentWorkflow

log = get_logger(__name__)


def stable_workflow_id(repo_slug: str, issue_number: int) -> str:
    """Stable workflow id ⇒ retrying the dispatcher is a no-op.

    Temporal will reject (or reuse, depending on policy) duplicate IDs, which
    is exactly the deduplication behaviour we want.
    """
    safe_repo = repo_slug.replace("/", "--") if repo_slug else "repo"
    return f"issue-agent--{safe_repo}--issue-{issue_number}"


async def build_client(config: AppConfig) -> Client:
    return await Client.connect(
        config.workflow.temporal.host,
        namespace=config.workflow.temporal.namespace,
    )


# Workflow status as a stable string the dispatcher / tests can branch on
# without importing the Temporal enum directly. ``"absent"`` means Temporal
# has no record of this workflow id at all (typical after a Temporal DB
# reset — the agent issue's label is still in-flight but no workflow exists
# to drive it forward, i.e. the orphan case).
WorkflowStatusLabel = Literal[
    "running", "completed", "failed", "canceled", "terminated",
    "continued_as_new", "timed_out", "absent",
]


async def describe_workflow_status(
    config: AppConfig,
    *,
    workflow_id: str,
    client: Client | None = None,
) -> WorkflowStatusLabel:
    """Inspect the Temporal workflow with ``workflow_id`` and report its state.

    Used by the dispatcher's orphan-recovery scan: an issue whose GitHub
    label is in-flight (``agent-running``, ``agent-planning``, etc.) but whose
    workflow is ``"absent"`` or no longer ``"running"`` is an orphan that
    needs a fresh dispatch.
    """
    cli = client or await build_client(config)
    try:
        handle = cli.get_workflow_handle(workflow_id)
        desc = await handle.describe()
    except Exception as exc:
        log.debug(
            "workflow.describe_absent", workflow_id=workflow_id, error=str(exc)
        )
        return "absent"

    status = desc.status
    if status == WorkflowExecutionStatus.RUNNING:
        return "running"
    if status == WorkflowExecutionStatus.COMPLETED:
        return "completed"
    if status == WorkflowExecutionStatus.FAILED:
        return "failed"
    if status == WorkflowExecutionStatus.CANCELED:
        return "canceled"
    if status == WorkflowExecutionStatus.TERMINATED:
        return "terminated"
    if status == WorkflowExecutionStatus.CONTINUED_AS_NEW:
        return "continued_as_new"
    if status == WorkflowExecutionStatus.TIMED_OUT:
        return "timed_out"
    log.warning(
        "workflow.describe_unknown_status",
        workflow_id=workflow_id,
        status=str(status),
    )
    return "absent"


DispatchOutcome = Literal["started", "attached_running", "restarted_after_close"]


async def start_issue_workflow(
    config: AppConfig,
    *,
    issue_number: int,
    workflow_input: IssueAgentInput | None = None,
    reuse_existing: bool = True,
    client: Client | None = None,
) -> tuple[WorkflowHandle, DispatchOutcome]:
    """Start (or attach to) the workflow for ``issue_number``.

    Returns ``(handle, outcome)`` where ``outcome`` is one of:

    - ``"started"`` — no prior workflow with this id existed; we created one.
    - ``"attached_running"`` — a workflow with this id is currently RUNNING;
      we return the existing handle without starting a new run.
    - ``"restarted_after_close"`` — a prior workflow with this id is closed
      (COMPLETED / FAILED / CANCELED / TERMINATED / TIMED_OUT). We started a
      fresh run with the same id. This is how the dispatcher resurrects an
      issue that was stuck on ``agent-blocked``.

    ``reuse_existing=False`` skips the inspection and always starts a new run
    (which Temporal will reject if a workflow with this id is still RUNNING).
    """
    cli = client or await build_client(config)

    workflow_id = stable_workflow_id(config.repo.slug, issue_number)
    payload = workflow_input or IssueAgentInput(
        issue_number=issue_number,
        repo=config.repo.slug,
        label_todo=config.github.issue_label_todo,
        label_running=config.github.issue_label_running,
        label_planning=config.github.issue_label_planning,
        label_coding=config.github.issue_label_coding,
        label_pr_created=config.github.issue_label_pr_created,
        label_ci_running=config.github.issue_label_ci_running,
        label_review=config.github.issue_label_review,
        label_blocked=config.github.issue_label_blocked,
        label_failed=config.github.issue_label_failed,
        label_done=config.github.issue_label_done,
        max_agent_rounds=config.workflow.max_agent_rounds,
        ci_poll_interval_seconds=config.workflow.ci_poll_interval_seconds,
        ci_max_wait_seconds=config.workflow.ci_max_wait_seconds,
    )

    if reuse_existing:
        try:
            handle = cli.get_workflow_handle(workflow_id)
            desc = await handle.describe()
        except Exception as exc:
            log.debug(
                "dispatch.describe_failed",
                workflow_id=workflow_id,
                error=str(exc),
            )
        else:
            if desc.status == WorkflowExecutionStatus.RUNNING:
                return handle, "attached_running"
            # Closed (COMPLETED / FAILED / CANCELED / TERMINATED / TIMED_OUT /
            # CONTINUED_AS_NEW): fall through to start a fresh run with the
            # same stable id. Temporal's default WorkflowIDReusePolicy
            # (ALLOW_DUPLICATE) permits this once the prior run is closed.
            log.info(
                "dispatch.restart_after_close",
                workflow_id=workflow_id,
                prior_status=str(desc.status),
            )
            handle = await cli.start_workflow(
                IssueAgentWorkflow.run,
                payload,
                id=workflow_id,
                task_queue=config.workflow.temporal.task_queue,
            )
            return handle, "restarted_after_close"

    handle = await cli.start_workflow(
        IssueAgentWorkflow.run,
        payload,
        id=workflow_id,
        task_queue=config.workflow.temporal.task_queue,
    )
    return handle, "started"
