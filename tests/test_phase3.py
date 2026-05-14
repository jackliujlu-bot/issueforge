"""Phase 3 tests: PR service git ops, deliverer node, stop_after=done flow.

All hermetic: a tiny tmp git repo + a fake gh client. The PR service touches
git directly (subprocess) for commit/push; we redirect "push" by using a
local ``bare.git`` repo as origin.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.config import load_config, reset_cached_config
from app.config.models import ExecutorEntry, GitHubConfig, RepoConfig
from app.executors.base import (
    CodeExecutor,
    ExecutorRequest,
    ExecutorResult,
    register_executor,
)
from app.executors.stub_executor import StubExecutor
from app.github._gh_cli import GhClient
from app.github.pr_service import GitHubPRService
from app.langgraph_app.graph import AgentRoundInput, run_agent_round
from app.langgraph_app.nodes.context import NodeContext
from app.langgraph_app.nodes.deliverer import deliverer_node as _deliverer_node
from app.langgraph_app.state import make_initial_state
from app.sandbox.artifact_store import ArtifactStore

# --------- fixtures ------------------------------------------------------ #


def _make_repo_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare ``origin.git`` and a working repo pointing at it, with an
    initial commit on ``main``."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "t@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=repo, check=True)
    return repo, bare


class _FakeGh(GhClient):
    """Records gh CLI calls. JSON responses are scripted via ``responses``."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.inputs: list[str | None] = []
        self.responses = responses or {}

    def run(self, *args: str, input_text: str | None = None, timeout: float | None = 60.0):  # type: ignore[override]
        raise NotImplementedError("not used in tests")

    def run_checked(
        self, *args: str, input_text: str | None = None, timeout: float | None = 60.0
    ) -> str:  # type: ignore[override]
        self.calls.append(args)
        self.inputs.append(input_text)
        key = " ".join(args[:3])
        if key in self.responses and isinstance(self.responses[key], str):
            return self.responses[key]
        return "https://github.com/acme/widget/pull/123\n"

    def run_json(self, *args: str, timeout: float | None = 60.0) -> Any:  # type: ignore[override]
        self.calls.append(args)
        key = " ".join(args[:3])
        return self.responses.get(key, [])


def _state(workspace: str = "", branch: str = "agent/issue-1") -> dict:
    return {
        **make_initial_state(
            repo="acme/widget",
            issue_number=1,
            issue_title="fix login bug",
            issue_body="b",
            issue_url="",
            artifact_dir="",
            executor="stub",
            max_retries=3,
        ),
        "plan": "## Objective\nfix\n## Subtasks\n- T1: do it",
        "todo": ["T1: do it"],
        "workspace_path": workspace,
        "branch": branch,
        "changed_files": ["foo.txt"],
        "local_test_status": "pass",
        "scratch": {"review_verdict": "pass"},
    }


def _ctx(tmp_path: Path, *, repo_local_path: str = "") -> NodeContext:
    reset_cached_config()
    cfg = load_config()
    cfg.system.artifact_root = tmp_path / "runs"
    cfg.repo.owner = "acme"
    cfg.repo.name = "widget"
    cfg.repo.base_branch = "main"
    cfg.repo.push_remote = "origin"
    if repo_local_path:
        cfg.repo.local_path = repo_local_path
    artifacts = ArtifactStore(cfg.system.artifact_root)
    run_dir = artifacts.run_dir("acme--widget--issue-1")
    return NodeContext(
        config=cfg,
        executor=_NoopExecutor(ExecutorEntry(enabled=True)),
        artifacts=artifacts,
        run_dir=run_dir,
    )


class _NoopExecutor(CodeExecutor):
    name = "stub"

    def run(self, request: ExecutorRequest) -> ExecutorResult:
        return ExecutorResult(ok=True, output="noop")


# --------- PR service ---------------------------------------------------- #


def test_pr_service_commits_and_pushes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _bare = _make_repo_with_origin(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "agent/issue-1"], cwd=repo, check=True)
    (repo / "foo.txt").write_text("new content\n")

    svc = GitHubPRService(
        RepoConfig(owner="acme", name="widget", base_branch="main", push_remote="origin"),
        GitHubConfig(),
        client=_FakeGh(),
    )
    monkeypatch.setenv("AGENT_WORKER_GIT_AUTHOR_NAME", "issue-agent-worker")
    monkeypatch.setenv("AGENT_WORKER_GIT_AUTHOR_EMAIL", "test@local")

    sha = svc.commit_changes(repo, files=None, message="agent: add foo")
    assert sha is not None and len(sha) >= 7
    svc.push_branch(repo, branch="agent/issue-1", remote="origin")

    # Verify origin received the commit on the branch.
    show = subprocess.run(
        ["git", "log", "-1", "--format=%H", "agent/issue-1"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert show.stdout.strip() == sha


def test_pr_service_commit_returns_none_when_clean(tmp_path: Path) -> None:
    repo, _ = _make_repo_with_origin(tmp_path)
    svc = GitHubPRService(
        RepoConfig(owner="acme", name="widget", base_branch="main"),
        GitHubConfig(),
        client=_FakeGh(),
    )
    sha = svc.commit_changes(repo, files=None, message="empty")
    assert sha is None


def test_pr_service_create_or_update_uses_existing(tmp_path: Path) -> None:
    fake = _FakeGh(
        responses={
            "pr list --repo": [
                {
                    "number": 42,
                    "url": "https://github.com/acme/widget/pull/42",
                    "title": "old title",
                    "headRefName": "agent/issue-1",
                    "baseRefName": "main",
                    "state": "OPEN",
                }
            ]
        }
    )
    svc = GitHubPRService(
        RepoConfig(owner="acme", name="widget", base_branch="main"),
        GitHubConfig(),
        client=fake,
    )
    pr = svc.create_or_update(
        head_branch="agent/issue-1",
        base_branch="main",
        title="new title",
        body="new body",
    )
    assert pr.number == 42
    assert any("pr" == c[0] and "edit" == c[1] for c in fake.calls)
    assert any(i == "new body" for i in fake.inputs if i is not None)


def test_pr_service_create_when_none_exists(tmp_path: Path) -> None:
    fake = _FakeGh(
        responses={
            "pr list --repo": [],  # then again on re-fetch
        }
    )
    svc = GitHubPRService(
        RepoConfig(owner="acme", name="widget", base_branch="main"),
        GitHubConfig(),
        client=fake,
    )
    pr = svc.create_or_update(
        head_branch="agent/issue-1",
        base_branch="main",
        title="new",
        body="body",
    )
    # Both `pr list` AND `pr create` should appear in calls.
    assert any(c[:2] == ("pr", "create") for c in fake.calls)
    assert pr.url.endswith("/pull/123")


# --------- deliverer node ----------------------------------------------- #


def test_deliverer_node_commits_pushes_and_writes_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _bare = _make_repo_with_origin(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "agent/issue-1"], cwd=repo, check=True)
    (repo / "foo.txt").write_text("new\n")

    ctx = _ctx(tmp_path)
    fake = _FakeGh(
        responses={
            "pr list --repo": [
                {
                    "number": 7,
                    "url": "https://example/pull/7",
                    "title": "agent: fix login bug (#1)",
                    "headRefName": "agent/issue-1",
                    "baseRefName": "main",
                    "state": "OPEN",
                }
            ]
        }
    )

    # Monkeypatch the PR service inside deliverer to use our fake gh.
    from app.langgraph_app.nodes import deliverer as deliverer_mod

    real_cls = deliverer_mod.GitHubPRService

    class _Patched(real_cls):  # type: ignore[misc, valid-type]
        def __init__(self, repo, github, *, client=None):  # type: ignore[no-untyped-def]
            super().__init__(repo, github, client=fake)

    monkeypatch.setattr(deliverer_mod, "GitHubPRService", _Patched)
    monkeypatch.setenv("AGENT_WORKER_GIT_AUTHOR_NAME", "issue-agent-worker")
    monkeypatch.setenv("AGENT_WORKER_GIT_AUTHOR_EMAIL", "test@local")

    state = _state(workspace=str(repo), branch="agent/issue-1")
    update = _deliverer_node(ctx)(state)

    assert update["current_step"] == "deliverer_done"
    assert update["pr_number"] == 7
    assert update["scratch"]["pr_url"].endswith("/pull/7")
    # delivery.md written
    assert (ctx.run_dir.root / "delivery.md").exists()
    body = (ctx.run_dir.root / "delivery.md").read_text()
    assert "pr_number: 7" in body
    # gh pr edit invoked with our new body
    assert any(c[:2] == ("pr", "edit") for c in fake.calls)


def test_deliverer_node_bails_without_workspace(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    update = _deliverer_node(ctx)({**_state(workspace="", branch="")})
    assert update["current_step"] == "deliverer_failed"
    assert "workspace_path" in update["last_error"]


# --------- integration: stop_after=done full round --------------------- #


def test_run_agent_round_stop_after_done_uses_deliverer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive planner→coder→tester→reviewer→deliverer→reporter end-to-end.

    Uses a tmp git repo + a recording fake gh + a stub executor that produces
    a real diff and a PASS verdict.
    """
    repo, _bare = _make_repo_with_origin(tmp_path)
    # Wire env so AppConfig picks up our tmp repo / origin.
    monkeypatch.setenv("AGENT_WORKER__REPO__OWNER", "acme")
    monkeypatch.setenv("AGENT_WORKER__REPO__NAME", "widget")
    monkeypatch.setenv("AGENT_WORKER__REPO__LOCAL_PATH", str(repo))
    monkeypatch.setenv("AGENT_WORKER__REPO__BASE_BRANCH", "main")
    monkeypatch.setenv("AGENT_WORKER__REPO__PUSH_REMOTE", "origin")
    monkeypatch.setenv("AGENT_WORKER__SANDBOX__MODE", "worktree")
    monkeypatch.setenv("AGENT_WORKER__SANDBOX__WORKTREE_ROOT", str(tmp_path / "wts"))
    monkeypatch.setenv("AGENT_WORKER__WORKFLOW__STOP_AFTER", "done")
    monkeypatch.setenv("AGENT_WORKER__COMMANDS__TEST", '["true"]')
    monkeypatch.setenv("AGENT_WORKER__COMMANDS__LINT", "[]")
    monkeypatch.setenv("AGENT_WORKER__EXECUTOR__DEFAULT", "stub")
    monkeypatch.setenv("AGENT_WORKER_GIT_AUTHOR_NAME", "issue-agent-worker")
    monkeypatch.setenv("AGENT_WORKER_GIT_AUTHOR_EMAIL", "test@local")

    class _Fake(CodeExecutor):
        name = "stub"

        def run(self, request: ExecutorRequest) -> ExecutorResult:
            if request.kind == "plan":
                return ExecutorResult(
                    ok=True,
                    output="## Objective\nDo it\n\n## Subtasks\n- T1: foo\n",
                )
            if request.kind == "code" and request.workspace is not None:
                (request.workspace / "phase3.txt").write_text("phase3\n")
                subprocess.run(
                    ["git", "add", "-N", "phase3.txt"],
                    cwd=request.workspace,
                    check=False,
                )
                return ExecutorResult(
                    ok=True,
                    output="SUMMARY: added phase3.txt",
                    diff="--- a/phase3.txt\n+++ b/phase3.txt\n+phase3\n",
                    changed_files=["phase3.txt"],
                )
            # review kind
            return ExecutorResult(ok=True, output="Looks good.\n\nVERDICT: PASS")

    register_executor("stub", _Fake)

    # Patch PR service so we don't need a real GitHub.
    fake_gh = _FakeGh(
        responses={
            "pr list --repo": [],  # no existing PR
        }
    )

    from app.langgraph_app.nodes import deliverer as deliverer_mod

    real_cls = deliverer_mod.GitHubPRService

    class _Patched(real_cls):  # type: ignore[misc, valid-type]
        def __init__(self, repo, github, *, client=None):  # type: ignore[no-untyped-def]
            super().__init__(repo, github, client=fake_gh)

    monkeypatch.setattr(deliverer_mod, "GitHubPRService", _Patched)

    reset_cached_config()
    cfg = load_config()
    cfg.system.artifact_root = tmp_path / "runs"

    try:
        out = run_agent_round(
            config=cfg,
            round_input=AgentRoundInput(
                repo="acme/widget",
                issue_number=1,
                issue_title="phase 3 smoke",
                issue_body="body",
            ),
        )
    finally:
        register_executor("stub", StubExecutor)

    # Final status should reflect the PR creation.
    assert out.final_status in ("pr_created", "ready_for_review")
    # changed file made it in
    assert "phase3.txt" in out.pending_issue_comment

    # The PR creation call should have happened.
    assert any(c[:2] == ("pr", "create") for c in fake_gh.calls)

    # delivery.md captured the PR URL pattern.
    delivery = (tmp_path / "runs" / "acme--widget--issue-1" / "delivery.md").read_text()
    assert "pr_url:" in delivery
