"""Temporal workflow definitions.

Workflows are deterministic; only call activities, never touch the network or
filesystem directly. Long-running waits (CI polling, human reply) live here.

Phases:
- Phase 1 (`stop_after=planning`): one round → planner-only → label
  ``agent:blocked`` and return.
- Phase 2 (`stop_after=testing|review`): one round → coder/tester → label
  ``agent:review`` if tests pass, ``agent:failed`` otherwise.
- Phase 3 (`stop_after=done` and round produces ``pr_created``): the workflow
  transitions to ``agent:ci-running`` and starts polling CI.
- Phase 4 (CI loop): while CI fails and rounds remain, hand the CI log
  summary back into a fresh LangGraph round and let the agent iterate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.temporal_app.activities import (
        FetchCIStatusInput,
        FetchCIStatusOutput,
        LoadIssueInput,
        LoadIssueOutput,
        PostCommentInput,
        RunAgentRoundInput,
        RunAgentRoundOutput,
        TransitionLabelInput,
    )


@dataclass
class IssueAgentInput:
    issue_number: int
    repo: str = ""  # informational; activities use the resolved RepoConfig.
    # Label names — defaults match the colon-style; project YAML can override.
    label_todo: str = "agent:todo"
    label_running: str = "agent:running"
    label_planning: str = "agent:planning"
    label_coding: str = "agent:coding"
    label_pr_created: str = "agent:pr-created"
    label_ci_running: str = "agent:ci-running"
    label_review: str = "agent:review"
    label_blocked: str = "agent:blocked"
    label_failed: str = "agent:failed"
    label_done: str = "agent:done"

    # Workflow knobs (mirror AppConfig.workflow).
    max_agent_rounds: int = 6
    ci_poll_interval_seconds: int = 30
    ci_max_wait_seconds: int = 7200  # 2h ceiling per CI cycle


@dataclass
class IssueAgentResult:
    final_status: str
    issue_number: int
    last_error: str = ""
    pr_number: int | None = None
    pr_url: str = ""
    rounds_used: int = 0


@workflow.defn(name="IssueAgentWorkflow")
class IssueAgentWorkflow:
    """One workflow per (repo, issue) pair. Idempotent given a stable workflow_id."""

    DEFAULT_ACTIVITY_TIMEOUT = timedelta(minutes=30)
    SHORT_TIMEOUT = timedelta(seconds=120)
    CI_FETCH_TIMEOUT = timedelta(minutes=5)

    @workflow.run
    async def run(self, params: IssueAgentInput) -> IssueAgentResult:
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=5),
            maximum_interval=timedelta(minutes=2),
            maximum_attempts=3,
        )

        # 1. agent:todo → agent:running
        await self._transition(
            params.issue_number, params.label_running, [params.label_todo], retry
        )

        # 2. Load issue.
        issue: LoadIssueOutput = await workflow.execute_activity(
            "load_issue",
            LoadIssueInput(issue_number=params.issue_number),
            start_to_close_timeout=self.SHORT_TIMEOUT,
            retry_policy=retry,
            result_type=LoadIssueOutput,
        )

        # 3. running → planning.
        await self._transition(
            params.issue_number, params.label_planning, [params.label_running], retry
        )

        prior_failure = ""
        rounds_used = 0
        last_pr_number: int | None = None
        last_pr_url = ""
        last_branch = ""
        last_error = ""

        for round_idx in range(params.max_agent_rounds):
            rounds_used = round_idx + 1
            # On retry rounds, swap planning → coding label so observers can tell.
            if round_idx == 1:
                await self._transition(
                    params.issue_number, params.label_coding, [params.label_planning], retry
                )

            round_result: RunAgentRoundOutput = await workflow.execute_activity(
                "run_agent_round",
                RunAgentRoundInput(
                    repo=issue.repo,
                    issue_number=issue.issue_number,
                    title=issue.title,
                    body=issue.body,
                    url=issue.url,
                    prior_failure=prior_failure,
                ),
                start_to_close_timeout=self.DEFAULT_ACTIVITY_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=2),
                result_type=RunAgentRoundOutput,
            )
            last_error = round_result.last_error
            last_pr_number = round_result.pr_number or last_pr_number
            last_pr_url = round_result.pr_url or last_pr_url
            last_branch = round_result.branch or last_branch

            if round_result.pending_issue_comment.strip():
                await workflow.execute_activity(
                    "post_issue_comment",
                    PostCommentInput(
                        issue_number=params.issue_number,
                        body=round_result.pending_issue_comment,
                    ),
                    start_to_close_timeout=self.SHORT_TIMEOUT,
                    retry_policy=retry,
                )

            final = round_result.final_status

            if final == "failed":
                await self._transition_terminal(
                    params, retry, target=params.label_failed
                )
                return self._result(
                    final, params.issue_number, last_error, last_pr_number,
                    last_pr_url, rounds_used,
                )

            if final == "planning_done":
                await self._transition_terminal(
                    params, retry, target=params.label_blocked
                )
                return self._result(
                    final, params.issue_number, last_error, last_pr_number,
                    last_pr_url, rounds_used,
                )

            if final == "ready_for_review":
                # Phase 2 stop (no deliverer ran).
                await self._transition_terminal(
                    params, retry, target=params.label_review
                )
                return self._result(
                    final, params.issue_number, last_error, last_pr_number,
                    last_pr_url, rounds_used,
                )

            if final == "pr_created":
                # Phase 3+4: PR opened → wait for CI to settle.
                await self._transition_terminal(
                    params, retry, target=params.label_ci_running
                )
                ci_outcome, ci_summary = await self._wait_for_ci(
                    params=params,
                    pr_number=last_pr_number or 0,
                    head_branch=last_branch,
                    retry=retry,
                )
                if ci_outcome == "passed":
                    await self._transition_terminal(
                        params, retry, target=params.label_done
                    )
                    return self._result(
                        "done", params.issue_number, "", last_pr_number,
                        last_pr_url, rounds_used,
                    )
                if ci_outcome == "timeout":
                    await self._transition_terminal(
                        params, retry, target=params.label_blocked
                    )
                    return self._result(
                        "blocked", params.issue_number,
                        "CI did not complete within ci_max_wait_seconds.",
                        last_pr_number, last_pr_url, rounds_used,
                    )
                # ci_outcome == "failed": feed CI summary back into next round.
                prior_failure = ci_summary
                if rounds_used >= params.max_agent_rounds:
                    break
                await self._transition_terminal(
                    params, retry, target=params.label_coding
                )
                continue

            # Unknown / unhandled final_status — bail loudly.
            await self._transition_terminal(
                params, retry, target=params.label_failed
            )
            return self._result(
                "failed", params.issue_number,
                f"Unhandled final_status={final!r}",
                last_pr_number, last_pr_url, rounds_used,
            )

        # Exhausted retry budget.
        await self._transition_terminal(
            params, retry, target=params.label_failed
        )
        return self._result(
            "failed", params.issue_number,
            f"Exhausted {params.max_agent_rounds} rounds; last_error={last_error}",
            last_pr_number, last_pr_url, rounds_used,
        )

    # ---------- helpers ------------------------------------------------- #

    async def _transition(
        self,
        issue_number: int,
        to_label: str,
        from_labels: list[str],
        retry: RetryPolicy,
    ) -> None:
        await workflow.execute_activity(
            "transition_issue_label",
            TransitionLabelInput(
                issue_number=issue_number,
                to_label=to_label,
                from_labels=from_labels,
            ),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry,
        )

    async def _transition_terminal(
        self,
        params: IssueAgentInput,
        retry: RetryPolicy,
        *,
        target: str,
    ) -> None:
        # Remove any of the in-flight labels we might be transitioning from.
        from_labels = [
            params.label_running,
            params.label_planning,
            params.label_coding,
            params.label_pr_created,
            params.label_ci_running,
        ]
        await self._transition(params.issue_number, target, from_labels, retry)

    async def _wait_for_ci(
        self,
        *,
        params: IssueAgentInput,
        pr_number: int,
        head_branch: str,
        retry: RetryPolicy,
    ) -> tuple[str, str]:
        """Poll CI status until it completes or we hit the time budget.

        Returns ``(outcome, summary)`` where outcome ∈ {passed, failed, timeout}.
        ``summary`` is human-readable, suitable as a coder retry prompt.
        """
        elapsed = 0
        poll_interval = max(5, params.ci_poll_interval_seconds)
        max_wait = max(poll_interval * 4, params.ci_max_wait_seconds)
        while elapsed <= max_wait:
            poll: FetchCIStatusOutput = await workflow.execute_activity(
                "fetch_ci_status",
                FetchCIStatusInput(pr_number=pr_number, head_branch=head_branch),
                start_to_close_timeout=self.CI_FETCH_TIMEOUT,
                retry_policy=retry,
                result_type=FetchCIStatusOutput,
            )
            if poll.completed:
                summary = self._format_ci_failure(poll)
                return ("passed" if poll.status == "passed" else "failed", summary)
            await workflow.sleep(timedelta(seconds=poll_interval))
            elapsed += poll_interval
        return ("timeout", "CI did not complete within the budget.")

    @staticmethod
    def _format_ci_failure(poll: FetchCIStatusOutput) -> str:
        sections = [poll.summary or "CI failed (no summary)."]
        for job, excerpt in poll.log_excerpts.items():
            sections.append(f"### `{job}` log excerpt\n\n```\n{excerpt}\n```")
        return "\n\n".join(sections)

    @staticmethod
    def _result(
        final_status: str,
        issue_number: int,
        last_error: str,
        pr_number: int | None,
        pr_url: str,
        rounds_used: int,
    ) -> IssueAgentResult:
        return IssueAgentResult(
            final_status=final_status,
            issue_number=issue_number,
            last_error=last_error,
            pr_number=pr_number,
            pr_url=pr_url,
            rounds_used=rounds_used,
        )
