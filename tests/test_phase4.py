"""Phase 4 tests: CI service, Feishu webhook, ci-aware workflow path.

We use the same fake-gh-client pattern as Phase 3. The Temporal workflow is
exercised via a unit test of the helper that turns a CI poll into a coder
retry prompt — the full async workflow loop needs a Temporal server, which
lives in the live-verification step, not unit tests.
"""

from __future__ import annotations

from typing import Any

from app.config.models import GitHubConfig, RepoConfig
from app.feishu.message_parser import parse_feishu_message
from app.github._gh_cli import GhClient
from app.github.ci_service import GitHubCIService

# --------- shared fake ------------------------------------------------- #


class _FakeGh(GhClient):
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.responses = responses or {}

    def run(self, *args: str, input_text: str | None = None, timeout: float | None = 60.0):  # type: ignore[override]
        raise NotImplementedError

    def run_checked(self, *args: str, input_text: str | None = None, timeout: float | None = 60.0) -> str:  # type: ignore[override]
        self.calls.append(args)
        key = " ".join(args[:3])
        if key in self.responses and isinstance(self.responses[key], str):
            return self.responses[key]
        return ""

    def run_json(self, *args: str, timeout: float | None = 60.0) -> Any:  # type: ignore[override]
        self.calls.append(args)
        key = " ".join(args[:3])
        return self.responses.get(key, [])


# --------- CI service -------------------------------------------------- #


def test_ci_poll_pending_when_a_run_is_in_progress() -> None:
    fake = _FakeGh(
        responses={
            "run list --repo": [
                {
                    "databaseId": 1,
                    "status": "in_progress",
                    "conclusion": "",
                    "headBranch": "agent/issue-1",
                    "headSha": "abc",
                    "url": "https://example",
                    "workflowName": "ci",
                    "createdAt": "2024-01-01T00:00:00Z",
                }
            ]
        }
    )
    svc = GitHubCIService(
        RepoConfig(owner="acme", name="widget"), GitHubConfig(), client=fake
    )
    poll = svc.poll(head_branch="agent/issue-1")
    assert poll.status == "pending"
    assert poll.completed is False
    assert "ci" in poll.summary


def test_ci_poll_passes_when_all_runs_succeeded() -> None:
    fake = _FakeGh(
        responses={
            "run list --repo": [
                {
                    "databaseId": 1,
                    "status": "completed",
                    "conclusion": "success",
                    "headBranch": "agent/issue-1",
                    "headSha": "abc",
                    "url": "https://example/1",
                    "workflowName": "ci",
                    "createdAt": "2024-01-01T00:00:00Z",
                }
            ]
        }
    )
    svc = GitHubCIService(
        RepoConfig(owner="acme", name="widget"), GitHubConfig(), client=fake
    )
    poll = svc.poll(head_branch="agent/issue-1")
    assert poll.status == "passed"
    assert poll.completed is True


def test_ci_poll_collapses_re_run_history_keeping_latest() -> None:
    """A failed older run + a successful re-run should report as ``passed``."""
    fake = _FakeGh(
        responses={
            "run list --repo": [
                {
                    "databaseId": 1,
                    "status": "completed",
                    "conclusion": "failure",
                    "headBranch": "agent/issue-1",
                    "headSha": "abc",
                    "url": "https://example/1",
                    "workflowName": "ci",
                    "createdAt": "2024-01-01T00:00:00Z",
                },
                {
                    "databaseId": 2,
                    "status": "completed",
                    "conclusion": "success",
                    "headBranch": "agent/issue-1",
                    "headSha": "def",
                    "url": "https://example/2",
                    "workflowName": "ci",
                    "createdAt": "2024-01-02T00:00:00Z",
                },
            ]
        }
    )
    svc = GitHubCIService(
        RepoConfig(owner="acme", name="widget"), GitHubConfig(), client=fake
    )
    poll = svc.poll(head_branch="agent/issue-1")
    assert poll.status == "passed", poll


def test_ci_poll_failed_pulls_log_excerpt() -> None:
    fake_logs = ("error: thing broke\n" * 30) + "FINAL FAIL\n"
    fake = _FakeGh(
        responses={
            "run list --repo": [
                {
                    "databaseId": 7,
                    "status": "completed",
                    "conclusion": "failure",
                    "headBranch": "agent/issue-1",
                    "headSha": "abc",
                    "url": "https://example/7",
                    "workflowName": "ci",
                    "createdAt": "2024-01-01T00:00:00Z",
                }
            ],
            "run view 7": fake_logs,
        }
    )
    svc = GitHubCIService(
        RepoConfig(owner="acme", name="widget"), GitHubConfig(), client=fake
    )
    poll = svc.poll(head_branch="agent/issue-1")
    assert poll.status == "failed"
    assert poll.completed is True
    assert poll.failed_jobs == ["ci"]
    assert "FINAL FAIL" in poll.log_excerpts["ci"]


def test_ci_poll_unknown_when_no_runs_yet() -> None:
    fake = _FakeGh(responses={"run list --repo": []})
    svc = GitHubCIService(
        RepoConfig(owner="acme", name="widget"), GitHubConfig(), client=fake
    )
    poll = svc.poll(head_branch="agent/issue-1")
    assert poll.status == "unknown"
    assert poll.completed is False


def test_ci_poll_ignores_workflows_listed_in_ci_ignore_workflows() -> None:
    """Workflows matched by ``ci_ignore_workflows`` must not drive the verdict.

    Regression for the dimos case where the "Auto Merge" workflow (polling for
    a Codex reviewer 👍) kept the agent's CI gate stuck on `pending` for 30
    minutes even though the real test workflows had already passed.
    """
    fake = _FakeGh(
        responses={
            "run list --repo": [
                {
                    "databaseId": 1,
                    "status": "completed",
                    "conclusion": "success",
                    "headBranch": "agent/issue-1",
                    "headSha": "abc",
                    "url": "https://example/ci",
                    "workflowName": "ci",
                    "createdAt": "2024-01-01T00:00:00Z",
                },
                {
                    "databaseId": 2,
                    "status": "in_progress",
                    "conclusion": "",
                    "headBranch": "agent/issue-1",
                    "headSha": "abc",
                    "url": "https://example/am",
                    "workflowName": "Auto Merge",
                    "createdAt": "2024-01-01T00:00:00Z",
                },
            ]
        }
    )
    svc = GitHubCIService(
        RepoConfig(owner="acme", name="widget"),
        GitHubConfig(ci_ignore_workflows=["Auto Merge"]),
        client=fake,
    )
    poll = svc.poll(head_branch="agent/issue-1")
    assert poll.status == "passed", poll


def test_ci_poll_unknown_when_only_ignored_workflows_present() -> None:
    """If every run is ignored we return ``unknown`` rather than ``passed``."""
    fake = _FakeGh(
        responses={
            "run list --repo": [
                {
                    "databaseId": 1,
                    "status": "completed",
                    "conclusion": "failure",
                    "headBranch": "agent/issue-1",
                    "headSha": "abc",
                    "url": "https://example/am",
                    "workflowName": "Auto Merge",
                    "createdAt": "2024-01-01T00:00:00Z",
                }
            ]
        }
    )
    svc = GitHubCIService(
        RepoConfig(owner="acme", name="widget"),
        GitHubConfig(ci_ignore_workflows=["auto merge"]),
        client=fake,
    )
    poll = svc.poll(head_branch="agent/issue-1")
    assert poll.status == "unknown"
    assert poll.completed is False


# --------- workflow CI-failure formatter -------------------------------- #


def test_workflow_format_ci_failure_includes_log_excerpts() -> None:
    """The workflow's CI summary formatter is pure and unit-testable."""
    from app.temporal_app.activities import FetchCIStatusOutput
    from app.temporal_app.workflows import IssueAgentWorkflow

    payload = FetchCIStatusOutput(
        status="failed",
        completed=True,
        summary="CI failed:\n- ci: failure (https://example)",
        failed_jobs=["ci"],
        log_excerpts={"ci": "BOOM line 1\nBOOM line 2"},
    )
    formatted = IssueAgentWorkflow._format_ci_failure(payload)
    assert "CI failed" in formatted
    assert "BOOM line 1" in formatted
    assert "log excerpt" in formatted


# --------- Feishu message parser --------------------------------------- #


def test_feishu_parse_message_extracts_repo_and_task() -> None:
    text = """@bot
repo: acme/widget
priority: high
task:
Fix the login bug
- include tests
- do not change DB schema
"""
    parsed = parse_feishu_message(text)
    assert parsed.repo == "acme/widget"
    assert parsed.priority == "high"
    assert "Fix the login bug" in parsed.body
    assert "include tests" in parsed.body
    # title falls back to the first body line if not explicitly set
    assert parsed.title == "Fix the login bug"


def test_feishu_parse_message_handles_missing_repo_field() -> None:
    parsed = parse_feishu_message("task: do something quickly")
    assert parsed.repo == ""
    assert parsed.body.strip().startswith("do something quickly")


def test_feishu_parse_message_returns_empty_for_blank() -> None:
    parsed = parse_feishu_message("")
    assert parsed.body == ""
    assert parsed.repo == ""
