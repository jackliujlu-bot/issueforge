"""LangGraph agent state.

The state is intentionally JSON-serialisable so it survives checkpoint
serialisation and is safe to ship across Temporal activity boundaries.
"""

from __future__ import annotations

from typing import Literal, TypedDict

FinalStatus = Literal[
    "running",
    "blocked",
    "planning_done",
    "pr_created",
    "ready_for_review",
    "failed",
    "done",
]

LocalTestStatus = Literal["unknown", "pass", "fail"]
CIStatus = Literal["unknown", "pending", "pass", "fail"]


class SubtaskState(TypedDict, total=False):
    id: str
    title: str
    type: str  # analysis | code | test | docs | review
    files_hint: list[str]
    depends_on: list[str]
    done_when: list[str]
    verify_commands: list[str]
    risk_level: str
    status: Literal["pending", "in_progress", "done", "blocked"]


class AgentState(TypedDict, total=False):
    """Full agent state. All fields are optional in TypedDict-with-total-False
    so we can update incrementally without producing partial validation errors.
    """

    repo: str
    issue_number: int
    issue_title: str
    issue_body: str
    issue_url: str

    branch: str
    workspace_path: str
    artifact_dir: str

    plan: str
    todo: list[str]
    subtasks: list[SubtaskState]
    assumptions: list[str]
    current_step: str

    executor: str

    local_test_status: LocalTestStatus
    ci_status: CIStatus

    last_error: str
    last_executor_output: str
    evidence: list[str]
    changed_files: list[str]

    pr_number: int | None
    retry_count: int
    max_retries: int

    need_human: bool
    final_status: FinalStatus

    # Reporter writes here when it has a comment ready for the orchestrator to post.
    pending_issue_comment: str

    # Free-form scratchpad for nodes that want to leave breadcrumbs.
    scratch: dict[str, str]


def make_initial_state(
    *,
    repo: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    issue_url: str,
    artifact_dir: str,
    executor: str,
    max_retries: int,
    workspace_path: str = "",
    branch: str = "",
) -> AgentState:
    return {
        "repo": repo,
        "issue_number": issue_number,
        "issue_title": issue_title,
        "issue_body": issue_body,
        "issue_url": issue_url,
        "branch": branch,
        "workspace_path": workspace_path,
        "artifact_dir": artifact_dir,
        "plan": "",
        "todo": [],
        "subtasks": [],
        "assumptions": [],
        "current_step": "load_context",
        "executor": executor,
        "local_test_status": "unknown",
        "ci_status": "unknown",
        "last_error": "",
        "last_executor_output": "",
        "evidence": [],
        "changed_files": [],
        "pr_number": None,
        "retry_count": 0,
        "max_retries": max_retries,
        "need_human": False,
        "final_status": "running",
        "pending_issue_comment": "",
        "scratch": {},
    }
