"""Temporal activities.

Activities are the *outside-world* boundary of the workflow. They:
    - call GitHub
    - run LangGraph rounds
    - touch the filesystem

We register them as plain async functions (Temporal supports both styles), and
inject the AppConfig at worker construction so activities are deterministic
about which config they see.

Each activity is small and idempotent so Temporal can retry safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from temporalio import activity

from app.config import get_config, load_config
from app.config.models import AppConfig
from app.github.ci_service import CIPollResult, GitHubCIService
from app.github.issue_service import GitHubIssueService
from app.github.pr_service import GitHubPRService
from app.langgraph_app.graph import (
    AgentRoundInput,
    AgentRoundOutput,
    run_agent_round,
)

# --------- Activity input/output dataclasses ------------------------------- #


@dataclass
class LoadIssueInput:
    issue_number: int


@dataclass
class LoadIssueOutput:
    repo: str
    issue_number: int
    title: str
    body: str
    url: str
    labels: list[str]


@dataclass
class RunAgentRoundInput:
    repo: str
    issue_number: int
    title: str
    body: str
    url: str = ""
    prior_failure: str = ""  # Phase 4: CI log summary fed back in for retry


@dataclass
class RunAgentRoundOutput:
    final_status: str
    pending_issue_comment: str
    last_error: str
    pr_number: int | None = None
    pr_url: str = ""
    branch: str = ""


@dataclass
class PostCommentInput:
    issue_number: int
    body: str


@dataclass
class TransitionLabelInput:
    issue_number: int
    to_label: str
    from_labels: list[str]


@dataclass
class FetchCIStatusInput:
    pr_number: int
    head_branch: str = ""  # fallback when PR number isn't available yet


@dataclass
class FetchCIStatusOutput:
    status: str  # "pending" | "passed" | "failed" | "unknown"
    completed: bool
    summary: str = ""
    failed_jobs: list[str] = field(default_factory=list)
    log_excerpts: dict[str, str] = field(default_factory=dict)


# --------- Implementations ------------------------------------------------- #


class ActivityImpls:
    """Bound activity collection.

    Construct once per worker process. Holds the resolved :class:`AppConfig`
    and lazily-created GitHub clients. Methods are coroutines decorated with
    ``@activity.defn`` *via* :func:`build_activities`.
    """

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()
        self._issue_service: GitHubIssueService | None = None
        self._pr_service: GitHubPRService | None = None
        self._ci_service: GitHubCIService | None = None

    def _issues(self) -> GitHubIssueService:
        if self._issue_service is None:
            self._issue_service = GitHubIssueService(self.config.repo, self.config.github)
        return self._issue_service

    def _prs(self) -> GitHubPRService:
        if self._pr_service is None:
            self._pr_service = GitHubPRService(self.config.repo, self.config.github)
        return self._pr_service

    def _ci(self) -> GitHubCIService:
        if self._ci_service is None:
            self._ci_service = GitHubCIService(self.config.repo, self.config.github)
        return self._ci_service

    async def load_issue(self, payload: LoadIssueInput) -> LoadIssueOutput:
        issue = self._issues().fetch(payload.issue_number)
        return LoadIssueOutput(
            repo=self.config.repo.slug,
            issue_number=issue.number,
            title=issue.title,
            body=issue.body,
            url=issue.url,
            labels=issue.labels,
        )

    async def run_agent_round(self, payload: RunAgentRoundInput) -> RunAgentRoundOutput:
        body = payload.body
        if payload.prior_failure:
            body = (
                f"{body}\n\n---\n\n"
                "## Prior failure (from CI / previous round)\n\n"
                f"{payload.prior_failure}\n"
            )
        result: AgentRoundOutput = run_agent_round(
            config=self.config,
            round_input=AgentRoundInput(
                repo=payload.repo or self.config.repo.slug,
                issue_number=payload.issue_number,
                issue_title=payload.title,
                issue_body=body,
                issue_url=payload.url,
            ),
        )
        last_error = str(result.state.get("last_error") or "")
        pr_number = result.state.get("pr_number")
        scratch = result.state.get("scratch") or {}
        return RunAgentRoundOutput(
            final_status=result.final_status,
            pending_issue_comment=result.pending_issue_comment,
            last_error=last_error,
            pr_number=int(pr_number) if pr_number else None,
            pr_url=str(scratch.get("pr_url", "")),
            branch=str(result.state.get("branch") or ""),
        )

    async def post_issue_comment(self, payload: PostCommentInput) -> None:
        if not payload.body.strip():
            return
        self._issues().comment(payload.issue_number, payload.body)

    async def transition_issue_label(self, payload: TransitionLabelInput) -> None:
        self._issues().transition_label(
            payload.issue_number,
            to_label=payload.to_label,
            from_labels=payload.from_labels,
        )

    async def fetch_ci_status(self, payload: FetchCIStatusInput) -> FetchCIStatusOutput:
        poll: CIPollResult = self._ci().poll(
            pr_number=payload.pr_number or None,
            head_branch=payload.head_branch or None,
        )
        return FetchCIStatusOutput(
            status=poll.status,
            completed=poll.completed,
            summary=poll.summary,
            failed_jobs=poll.failed_jobs,
            log_excerpts=poll.log_excerpts,
        )


# --------- Worker registration ------------------------------------------- #


def build_activities(config: AppConfig | None = None) -> tuple[ActivityImpls, list[Any]]:
    """Return (impls, activity_callables) for ``Worker(activities=...)``.

    temporalio's ``activity.defn`` decorator can't be applied directly to bound
    methods (it tries to ``setattr`` an attribute on the callable, which fails
    on ``MethodType``). We wrap each bound method in a plain ``async def`` so
    the decorator has a real function to mark.
    """
    impls = ActivityImpls(config)

    async def load_issue(payload: LoadIssueInput) -> LoadIssueOutput:
        return await impls.load_issue(payload)

    async def run_agent_round(payload: RunAgentRoundInput) -> RunAgentRoundOutput:
        return await impls.run_agent_round(payload)

    async def post_issue_comment(payload: PostCommentInput) -> None:
        await impls.post_issue_comment(payload)

    async def transition_issue_label(payload: TransitionLabelInput) -> None:
        await impls.transition_issue_label(payload)

    async def fetch_ci_status(payload: FetchCIStatusInput) -> FetchCIStatusOutput:
        return await impls.fetch_ci_status(payload)

    callables = [
        activity.defn(name="load_issue")(load_issue),
        activity.defn(name="run_agent_round")(run_agent_round),
        activity.defn(name="post_issue_comment")(post_issue_comment),
        activity.defn(name="transition_issue_label")(transition_issue_label),
        activity.defn(name="fetch_ci_status")(fetch_ci_status),
    ]
    return impls, callables


def reload_config(path: str | None = None) -> AppConfig:
    """Reload config from disk (used by the worker on SIGHUP if you wire one up)."""
    return load_config(path)
