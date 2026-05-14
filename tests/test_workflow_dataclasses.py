"""Make sure Temporal dataclasses + workflow class import without a Temporal server."""

from __future__ import annotations

from app.temporal_app.activities import (
    LoadIssueInput,
    PostCommentInput,
    RunAgentRoundInput,
    TransitionLabelInput,
)
from app.temporal_app.client import stable_workflow_id
from app.temporal_app.workflows import IssueAgentInput, IssueAgentWorkflow


def test_workflow_id_is_stable_per_repo_issue() -> None:
    assert stable_workflow_id("acme/widget", 42) == "issue-agent--acme--widget--issue-42"
    assert stable_workflow_id("", 7) == "issue-agent--repo--issue-7"


def test_workflow_dataclasses_are_constructible() -> None:
    payload = IssueAgentInput(issue_number=1)
    assert payload.issue_number == 1
    assert payload.label_running.startswith("agent:")
    assert payload.label_done.startswith("agent:")
    assert payload.max_agent_rounds > 0

    LoadIssueInput(issue_number=1)
    PostCommentInput(issue_number=1, body="hi")
    RunAgentRoundInput(repo="acme/widget", issue_number=1, title="t", body="b")
    TransitionLabelInput(issue_number=1, to_label="agent:x", from_labels=["agent:y"])


def test_workflow_class_metadata() -> None:
    # Smoke test: the @workflow.defn decorator exposes the class for the worker registry.
    assert IssueAgentWorkflow.__name__ == "IssueAgentWorkflow"
