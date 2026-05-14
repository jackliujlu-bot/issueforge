"""Phase 2 end-to-end tests.

Covers, against an ephemeral git repo:

- ``GitWorktreeBackend.ensure / cleanup`` idempotency + branch handling.
- ``coder`` node: writes ``changed_files.txt`` + ``diff.patch``, threads the
  workspace through to the executor, increments ``retry_count`` on subsequent
  invocations.
- ``tester`` node: runs configured commands via the shell executor, records
  evidence, sets ``local_test_status`` correctly on pass / fail.
- ``reviewer`` node: parses ``VERDICT: PASS|FAIL`` from executor output and
  drives ``scratch.review_verdict``.
- ``run_agent_round`` with ``stop_after=testing`` end-to-end on a tiny tmp
  repo using a fake coder + the real shell executor for tests.

All tests are hermetic: no network, no real cursor-agent, no Temporal.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.config import load_config, reset_cached_config
from app.config.models import ExecutorEntry
from app.executors.base import (
    CodeExecutor,
    ExecutorRequest,
    ExecutorResult,
    register_executor,
)
from app.executors.stub_executor import StubExecutor
from app.langgraph_app.graph import AgentRoundInput, run_agent_round
from app.langgraph_app.nodes.coder import coder_node as _coder_node
from app.langgraph_app.nodes.context import NodeContext
from app.langgraph_app.nodes.reviewer import reviewer_node as _reviewer_node

# Aliased: pytest's default `python_functions = ["test"]` is a prefix match,
# so `tester_node` (starts with "test") would otherwise be collected as a test.
from app.langgraph_app.nodes.tester import tester_node as _tester_node
from app.langgraph_app.state import make_initial_state
from app.sandbox.artifact_store import ArtifactStore
from app.sandbox.worktree import (
    GitCommandError,
    GitWorktreeBackend,
    WorktreeManager,
)

# --------- helpers ------------------------------------------------------- #


def _make_source_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo at ``tmp_path / "source"`` with one commit on ``main``."""
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


class _RecordingExecutor(CodeExecutor):
    """Coder/reviewer double that returns a scripted result and remembers calls.

    The first ``run`` returns ``script[0]``, the second ``script[1]``, etc.
    Past the end of the list it returns the last entry.
    """

    name = "recording"

    def __init__(
        self,
        entry: ExecutorEntry,
        *,
        script: list[ExecutorResult] | None = None,
        write_file: tuple[str, str] | None = None,
    ) -> None:
        super().__init__(entry)
        self.script = script or [ExecutorResult(ok=True, output="ok")]
        self.write_file = write_file
        self.calls: list[ExecutorRequest] = []

    def run(self, request: ExecutorRequest) -> ExecutorResult:
        self.calls.append(request)
        if self.write_file and request.workspace is not None:
            rel, content = self.write_file
            target = request.workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            subprocess.run(["git", "add", "-N", rel], cwd=request.workspace, check=False)
        idx = min(len(self.calls) - 1, len(self.script) - 1)
        return self.script[idx]


def _ctx(
    tmp_path: Path,
    *,
    executor: CodeExecutor,
    worktree: WorktreeManager | None,
    shell: CodeExecutor | None = None,
    commands_lint: list[str] | None = None,
    commands_test: list[str] | None = None,
    stop_after: str = "testing",
) -> NodeContext:
    reset_cached_config()
    cfg = load_config()
    cfg.system.artifact_root = tmp_path / "runs"
    cfg.workflow.stop_after = stop_after  # type: ignore[assignment]
    cfg.commands.lint = commands_lint or []
    cfg.commands.test = commands_test or []
    artifacts = ArtifactStore(cfg.system.artifact_root)
    run_dir = artifacts.run_dir("acme--widget--issue-1")
    return NodeContext(
        config=cfg,
        executor=executor,
        artifacts=artifacts,
        run_dir=run_dir,
        worktree=worktree,
        shell=shell,
    )


def _state(workspace: str = "") -> dict:
    return {
        **make_initial_state(
            repo="acme/widget",
            issue_number=1,
            issue_title="t",
            issue_body="b",
            issue_url="",
            artifact_dir="",
            executor="recording",
            max_retries=3,
        ),
        "plan": "do the thing",
        "todo": ["T1: do it"],
        "workspace_path": workspace,
    }


# --------- worktree backend --------------------------------------------- #


def test_git_worktree_backend_creates_and_reuses(tmp_path: Path) -> None:
    src = _make_source_repo(tmp_path)
    backend = GitWorktreeBackend(src)

    wt1 = backend.ensure(
        repo_slug="acme/widget",
        branch="agent/issue-1",
        base_branch="main",
        target_path=tmp_path / "worktrees" / "issue-1",
    )
    assert wt1.path.exists()
    assert (wt1.path / "README.md").exists()
    assert (wt1.path / ".git").exists()

    # idempotent reuse — second call returns the same Worktree, no error
    wt2 = backend.ensure(
        repo_slug="acme/widget",
        branch="agent/issue-1",
        base_branch="main",
        target_path=tmp_path / "worktrees" / "issue-1",
    )
    assert wt2.path == wt1.path

    backend.cleanup(wt1)
    assert not wt1.path.exists()


def test_git_worktree_backend_rejects_wrong_branch_reuse(tmp_path: Path) -> None:
    src = _make_source_repo(tmp_path)
    backend = GitWorktreeBackend(src)
    backend.ensure(
        repo_slug="acme/widget",
        branch="agent/issue-1",
        base_branch="main",
        target_path=tmp_path / "worktrees" / "issue-1",
    )
    with pytest.raises(GitCommandError):
        backend.ensure(
            repo_slug="acme/widget",
            branch="agent/issue-2",  # different branch, same path
            base_branch="main",
            target_path=tmp_path / "worktrees" / "issue-1",
        )


def test_git_worktree_backend_prefers_remote_when_local_main_diverged(
    tmp_path: Path,
) -> None:
    """The "stale local main" failure mode (regression for #56).

    Scenario: developer's ``dimos2/main`` carries one un-pushed commit that
    the agent shouldn't ship. Without remote-preference, the agent branches
    from local main → that un-pushed commit lands in the PR → reviewer
    rejects the scope creep. With remote-preference, the agent branches
    from ``origin/main`` → PR contains only the agent's actual work.
    """
    # Set up: a "remote" repo with one commit; clone it locally; add one
    # local-only commit on top so local main is ahead of origin/main.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)

    src = _make_source_repo(tmp_path)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=src, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=src, check=True)

    # Add a local-only commit (NOT pushed) — this is the "stale local main".
    (src / "local_only.txt").write_text("must not ship\n")
    subprocess.run(["git", "add", "local_only.txt"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-qm", "local-only commit"], cwd=src, check=True)

    backend = GitWorktreeBackend(src, preferred_remote="origin")
    wt = backend.ensure(
        repo_slug="acme/widget",
        branch="agent/issue-1",
        base_branch="main",
        target_path=tmp_path / "wt" / "issue-1",
    )

    # The agent's worktree must NOT contain the un-pushed local commit.
    assert not (wt.path / "local_only.txt").exists(), (
        "agent worktree branched from local main and inherited an unpushed "
        "commit — should have used origin/main instead"
    )
    # And the base_ref should be the remote-tracking ref.
    assert wt.base_ref == "origin/main", f"expected base_ref to be origin/main, got {wt.base_ref!r}"


def test_git_worktree_backend_falls_back_when_no_remote(tmp_path: Path) -> None:
    """A repo with no remote (greenfield local scaffold) must still produce
    a working worktree — fetch is skipped, local main is used.
    """
    src = _make_source_repo(tmp_path)  # no remote configured
    backend = GitWorktreeBackend(src, preferred_remote="origin")
    wt = backend.ensure(
        repo_slug="acme/widget",
        branch="agent/issue-1",
        base_branch="main",
        target_path=tmp_path / "wt" / "issue-1",
    )
    assert wt.path.exists()
    # base_ref points at the local branch since there's no remote
    assert wt.base_ref == "main"


def test_git_worktree_backend_skip_fetch_when_auto_fetch_disabled(
    tmp_path: Path,
) -> None:
    """``auto_fetch_base=False`` preserves the legacy "use local main"
    semantics: any un-pushed local commits get inherited by the agent."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    src = _make_source_repo(tmp_path)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=src, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=src, check=True)
    (src / "local_only.txt").write_text("ok with local\n")
    subprocess.run(["git", "add", "local_only.txt"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-qm", "local-only commit"], cwd=src, check=True)

    backend = GitWorktreeBackend(src, preferred_remote="origin", auto_fetch_base=False)
    wt = backend.ensure(
        repo_slug="acme/widget",
        branch="agent/issue-1",
        base_branch="main",
        target_path=tmp_path / "wt" / "issue-1",
    )
    # With auto_fetch disabled but prefer_remote still on, we still favor
    # the remote ref WHEN ONE EXISTS. So in this test the agent's branch is
    # off origin/main (cached locally as origin/main from the earlier push).
    # The un-pushed local commit must NOT appear in the worktree.
    assert not (wt.path / "local_only.txt").exists()


def test_git_worktree_backend_missing_base_branch(tmp_path: Path) -> None:
    src = _make_source_repo(tmp_path)
    backend = GitWorktreeBackend(src)
    with pytest.raises(GitCommandError):
        backend.ensure(
            repo_slug="acme/widget",
            branch="agent/issue-1",
            base_branch="nonexistent",
            target_path=tmp_path / "worktrees" / "issue-1",
        )


def test_worktree_manager_requires_backend(tmp_path: Path) -> None:
    mgr = WorktreeManager(tmp_path / "worktrees")
    with pytest.raises(NotImplementedError):
        mgr.ensure(
            repo_slug="acme/widget",
            branch="agent/issue-1",
            base_branch="main",
            issue_key="acme--widget--issue-1",
        )


# --------- coder node --------------------------------------------------- #


def test_coder_node_uses_worktree_and_persists_diff(tmp_path: Path) -> None:
    src = _make_source_repo(tmp_path)
    backend = GitWorktreeBackend(src)
    mgr = WorktreeManager(tmp_path / "worktrees", backend=backend)

    executor = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[
            ExecutorResult(
                ok=True,
                output="did the thing\nSUMMARY: added foo",
                diff="--- a/foo.txt\n+++ b/foo.txt\n@@ +1 @@\n+new\n",
                changed_files=["foo.txt"],
            )
        ],
        write_file=("foo.txt", "new\n"),
    )
    ctx = _ctx(tmp_path, executor=executor, worktree=mgr)
    update = _coder_node(ctx)(_state())

    assert update["current_step"] == "coder_done"
    assert update["branch"] == "agent/issue-1"
    assert update["changed_files"] == ["foo.txt"]
    assert update["last_error"] == ""
    # one call so far, retry_count stays at 0
    assert update["retry_count"] == 0

    # On-disk artefacts
    changed_files_txt = ctx.run_dir.execution_dir / "changed_files.txt"
    diff_patch = ctx.run_dir.execution_dir / "diff.patch"
    assert changed_files_txt.read_text().strip() == "foo.txt"
    assert "+new" in diff_patch.read_text()

    # Executor was given the worktree as workspace
    assert executor.calls[0].kind == "code"
    assert executor.calls[0].workspace is not None
    assert (executor.calls[0].workspace / "foo.txt").exists()


def test_tester_changed_py_files_filters_to_python_only(tmp_path: Path) -> None:
    """``{changed_py_files}`` must only include ``*.py`` paths.

    Regression for #55: when ``ruff check {changed_files}`` saw a Markdown
    file in the substitution, ruff errored out, the coder then tried to
    'fix' it by editing ``pyproject.toml`` exclude rules, and the reviewer
    correctly rejected the broad scope creep. The fix: have Python-only
    tools use ``{changed_py_files}`` instead.
    """
    from app.langgraph_app.nodes.tester import _build_substitutions, _resolve_command

    subs = _build_substitutions(
        changed_files=[
            "src/foo.py",
            "docs/readme.md",
            "config.yaml",
            "src/bar.py",
        ],
        workspace=tmp_path,
    )
    assert subs.changed_py_files == ["src/foo.py", "src/bar.py"]

    resolved = _resolve_command("uv run ruff check {changed_py_files}", subs)
    assert resolved.command == "uv run ruff check src/foo.py src/bar.py"


def test_tester_changed_py_files_skips_command_when_no_python_changed(
    tmp_path: Path,
) -> None:
    """Docs-only round: ``{changed_py_files}`` is empty → skip, don't
    fall back to running ruff on the whole repo."""
    from app.langgraph_app.nodes.tester import _build_substitutions, _resolve_command

    subs = _build_substitutions(
        changed_files=["docs/architecture.md", "README.md"], workspace=tmp_path
    )
    resolved = _resolve_command("uv run ruff check {changed_py_files}", subs)
    assert resolved.skip is True


def test_tester_substitutes_changed_files_token(tmp_path: Path) -> None:
    """``{changed_files}`` in a lint command must become the actual files
    the coder touched. Empty changed_files → command is skipped, not run."""
    from app.langgraph_app.nodes.tester import _build_substitutions, _resolve_command

    workspace = tmp_path / "wt"
    workspace.mkdir()
    subs = _build_substitutions(
        changed_files=["src/foo.py", "src/bar.py"],
        workspace=workspace,
    )
    resolved = _resolve_command("uv run ruff check {changed_files}", subs)
    assert resolved.skip is False
    assert resolved.command == "uv run ruff check src/foo.py src/bar.py"


def test_tester_skips_command_with_empty_changed_files(tmp_path: Path) -> None:
    """Project YAML opts in to fast tests with ``{changed_files}``; on a round
    where the coder produced no diff, we must not silently run the full suite —
    skip the command instead so the agent sees ``unknown`` test status."""
    from app.langgraph_app.nodes.tester import _build_substitutions, _resolve_command

    subs = _build_substitutions(changed_files=[], workspace=tmp_path)
    resolved = _resolve_command("uv run ruff check {changed_files}", subs)
    assert resolved.skip is True
    assert "empty" in resolved.skip_reason


def test_tester_derives_test_targets_from_changed_source_files(tmp_path: Path) -> None:
    """A source file change should map to its companion test file.

    Heuristic: ``src/foo.py`` → ``tests/test_foo.py`` if that file exists.
    Pure docs / config files contribute nothing to ``{test_targets}``.
    """
    from app.langgraph_app.nodes.tester import _build_substitutions, _resolve_command

    workspace = tmp_path / "wt"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "src" / "foo.py").write_text("def f(): pass\n")
    (workspace / "tests" / "test_foo.py").write_text("def test_f(): pass\n")
    (workspace / "README.md").write_text("hi\n")

    subs = _build_substitutions(changed_files=["src/foo.py", "README.md"], workspace=workspace)
    # README.md doesn't have a test, foo.py does → only one target.
    assert subs.test_targets == ["tests/test_foo.py"]

    resolved = _resolve_command("uv run pytest -x {test_targets}", subs)
    assert resolved.command == "uv run pytest -x tests/test_foo.py"


def test_tester_passes_test_file_change_through_verbatim(tmp_path: Path) -> None:
    """If the coder *modifies a test file*, that path itself is the target —
    no need to look for ``test_test_foo.py``."""
    from app.langgraph_app.nodes.tester import _build_substitutions

    workspace = tmp_path / "wt"
    workspace.mkdir()
    subs = _build_substitutions(
        changed_files=["tests/test_foo.py", "tests/integration/test_bar.py"],
        workspace=workspace,
    )
    assert subs.test_targets == [
        "tests/test_foo.py",
        "tests/integration/test_bar.py",
    ]


def test_tester_skips_test_command_when_no_test_targets(tmp_path: Path) -> None:
    """Docs-only PR: changed_files has .md only, test_targets is empty.

    ``pytest {test_targets}`` must SKIP, not fall back to the full suite.
    """
    from app.langgraph_app.nodes.tester import _build_substitutions, _resolve_command

    workspace = tmp_path / "wt"
    workspace.mkdir()
    subs = _build_substitutions(
        changed_files=["docs/architecture.md", "README.md"],
        workspace=workspace,
    )
    assert subs.test_targets == []

    resolved = _resolve_command("uv run pytest -x {test_targets}", subs)
    assert resolved.skip is True


def test_tester_leaves_legacy_commands_unchanged(tmp_path: Path) -> None:
    """A command with no template tokens must run verbatim — backward
    compatibility for projects that haven't opted into the new tokens yet."""
    from app.langgraph_app.nodes.tester import _build_substitutions, _resolve_command

    subs = _build_substitutions(changed_files=["any.py"], workspace=tmp_path)
    resolved = _resolve_command("uv run pytest", subs)
    assert resolved.command == "uv run pytest"
    assert resolved.skip is False
    assert resolved.substituted is False


def test_tester_quotes_paths_with_spaces(tmp_path: Path) -> None:
    """Pathnames with spaces must be shell-quoted so a single bad filename
    doesn't break the whole command line."""
    from app.langgraph_app.nodes.tester import _build_substitutions, _resolve_command

    subs = _build_substitutions(changed_files=["weird path/foo bar.py"], workspace=tmp_path)
    resolved = _resolve_command("uv run ruff check {changed_files}", subs)
    assert "'weird path/foo bar.py'" in resolved.command


def test_tester_end_to_end_targeted_runs_only_matched_tests(tmp_path: Path) -> None:
    """Full tester invocation with ``{test_targets}`` runs the substituted
    command against the worktree and persists evidence correctly."""
    from app.langgraph_app.nodes.tester import tester_node

    src = _make_source_repo(tmp_path)
    backend = GitWorktreeBackend(src)
    mgr = WorktreeManager(tmp_path / "worktrees", backend=backend)

    # Pre-create a fake worktree with the layout we need.
    wt = backend.ensure(
        repo_slug="acme/widget",
        branch="agent/issue-1",
        base_branch="main",
        target_path=tmp_path / "worktrees" / "issue-1",
    )
    (wt.path / "src").mkdir()
    (wt.path / "src" / "foo.py").write_text("def f(): return 1\n")
    (wt.path / "tests").mkdir()
    (wt.path / "tests" / "test_foo.py").write_text(
        "from src.foo import f\ndef test_f(): assert f() == 1\n"
    )

    shell = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[ExecutorResult(ok=True, output="all good", exit_code=0)],
    )
    ctx = _ctx(
        tmp_path,
        executor=_RecordingExecutor(ExecutorEntry(enabled=True)),
        worktree=mgr,
        shell=shell,
        commands_test=["uv run pytest {test_targets}"],
    )
    state = {
        **_state(workspace=str(wt.path)),
        "changed_files": ["src/foo.py"],
    }
    update = tester_node(ctx)(state)
    assert update["local_test_status"] == "pass"
    # The shell executor was given the substituted command, not the raw one.
    assert shell.calls and "tests/test_foo.py" in shell.calls[0].prompt


def test_coder_runs_setup_commands_once_per_worktree(tmp_path: Path) -> None:
    """``commands.setup`` (e.g. ``uv sync``) must run before the coder spawns
    the executor — otherwise downstream tester commands like ``uv run ruff``
    crash with ``No such file or directory``. Regression for #52.

    The first coder invocation runs every setup command and drops a marker
    file; subsequent invocations skip setup (cache hit) so we don't reinstall
    deps on every retry. The marker MUST live outside the worktree (regression
    for #55, where committing the marker polluted the PR diff and tricked the
    reviewer into rejecting an otherwise valid docs change).
    """
    from app.langgraph_app.nodes.coder import (
        WORKSPACE_SETUP_MARKER,
        _setup_marker_path,
    )

    src = _make_source_repo(tmp_path)
    backend = GitWorktreeBackend(src)
    mgr = WorktreeManager(tmp_path / "worktrees", backend=backend)

    executor = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[ExecutorResult(ok=True, output="SUMMARY: ok")],
    )
    shell = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[ExecutorResult(ok=True, output="setup ok")],
    )
    ctx = _ctx(tmp_path, executor=executor, worktree=mgr, shell=shell)
    ctx.config.commands.setup = ["echo setup-please"]

    # First call: setup runs, marker is created outside the worktree.
    update = _coder_node(ctx)(_state())
    assert update["current_step"] == "coder_done"
    workspace = Path(update["workspace_path"])
    expected_marker = _setup_marker_path(ctx)
    assert expected_marker.exists(), f"coder must drop the setup-done marker at {expected_marker}"
    assert not (workspace / WORKSPACE_SETUP_MARKER).exists(), (
        "marker MUST NOT live inside the worktree — that pollutes git diff "
        "and trips the reviewer (regression for #55)"
    )
    setup_calls = [c for c in shell.calls if c.metadata.get("label") == "setup"]
    assert len(setup_calls) == 1
    assert setup_calls[0].prompt == "echo setup-please"

    # Second call (simulates a retry round): setup is cached, no new shell call.
    update2 = _coder_node(ctx)({**_state(workspace=str(workspace)), "last_error": "x"})
    assert update2["current_step"] == "coder_done"
    setup_calls_after = [c for c in shell.calls if c.metadata.get("label") == "setup"]
    assert len(setup_calls_after) == 1, (
        "marker file should short-circuit setup on subsequent coder rounds"
    )


def test_coder_bails_with_setup_failed_when_setup_command_errors(tmp_path: Path) -> None:
    """If ``commands.setup`` fails, the coder must surface that as a clean
    ``setup_failed`` step + descriptive ``last_error`` — not silently
    proceed into the executor (which would then crash in confusing ways)."""
    src = _make_source_repo(tmp_path)
    backend = GitWorktreeBackend(src)
    mgr = WorktreeManager(tmp_path / "worktrees", backend=backend)

    executor = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[ExecutorResult(ok=True, output="should not run")],
    )
    shell = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[
            ExecutorResult(
                ok=False,
                exit_code=2,
                output="",
                metadata={"stderr": "No such file or directory: 'uv'"},
            )
        ],
    )
    ctx = _ctx(tmp_path, executor=executor, worktree=mgr, shell=shell)
    ctx.config.commands.setup = ["uv sync"]

    update = _coder_node(ctx)(_state())

    assert update["current_step"] == "setup_failed"
    assert "Workspace setup failed" in update["last_error"]
    assert "uv sync" in update["last_error"]
    assert executor.calls == [], "coder executor must not be invoked when setup has failed"


def test_coder_skips_setup_when_no_commands_configured(tmp_path: Path) -> None:
    """No ``commands.setup`` -> skip the setup phase entirely. Backward-compat
    for projects that have nothing to install."""
    src = _make_source_repo(tmp_path)
    backend = GitWorktreeBackend(src)
    mgr = WorktreeManager(tmp_path / "worktrees", backend=backend)

    executor = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[ExecutorResult(ok=True, output="SUMMARY: ok")],
    )
    shell = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[ExecutorResult(ok=True, output="should not be called")],
    )
    ctx = _ctx(tmp_path, executor=executor, worktree=mgr, shell=shell)
    ctx.config.commands.setup = []  # explicit empty

    update = _coder_node(ctx)(_state())
    assert update["current_step"] == "coder_done"
    assert not [c for c in shell.calls if c.metadata.get("label") == "setup"]


def test_coder_node_increments_retry_on_prior_error(tmp_path: Path) -> None:
    src = _make_source_repo(tmp_path)
    backend = GitWorktreeBackend(src)
    mgr = WorktreeManager(tmp_path / "worktrees", backend=backend)

    executor = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[ExecutorResult(ok=True, output="retry pass")],
    )
    ctx = _ctx(tmp_path, executor=executor, worktree=mgr)
    state = {
        **_state(),
        "last_error": "tests failed: foo",
        "retry_count": 0,
    }
    update = _coder_node(ctx)(state)
    assert update["retry_count"] == 1
    # prior error is cleared on success
    assert update["last_error"] == ""


def test_coder_node_without_worktree_skips_gracefully(tmp_path: Path) -> None:
    executor = _RecordingExecutor(ExecutorEntry(enabled=True))
    ctx = _ctx(tmp_path, executor=executor, worktree=None)
    update = _coder_node(ctx)(_state())
    assert update["current_step"] == "coder_skipped"
    assert "sandbox.mode" in update["last_error"]
    assert executor.calls == []


# --------- tester node -------------------------------------------------- #


def test_tester_node_passes_when_all_commands_succeed(tmp_path: Path) -> None:
    src = _make_source_repo(tmp_path)
    shell = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[ExecutorResult(ok=True, output="OK", exit_code=0)],
    )
    ctx = _ctx(
        tmp_path,
        executor=_RecordingExecutor(ExecutorEntry(enabled=True)),
        worktree=None,
        shell=shell,
        commands_test=["pytest"],
    )
    state = _state(workspace=str(src))
    update = _tester_node(ctx)(state)
    assert update["local_test_status"] == "pass"
    assert update["last_error"] == ""
    assert (ctx.run_dir.evidence_dir / "local_tests.log").exists()
    assert "pytest" in (ctx.run_dir.commands_log).read_text()


def test_tester_node_fails_and_carries_stderr(tmp_path: Path) -> None:
    src = _make_source_repo(tmp_path)
    shell = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[
            ExecutorResult(
                ok=False,
                output="",
                exit_code=1,
                metadata={"stderr": "Boom: test 3 failed"},
            )
        ],
    )
    ctx = _ctx(
        tmp_path,
        executor=_RecordingExecutor(ExecutorEntry(enabled=True)),
        worktree=None,
        shell=shell,
        commands_test=["pytest"],
    )
    state = _state(workspace=str(src))
    update = _tester_node(ctx)(state)
    assert update["local_test_status"] == "fail"
    assert "Boom: test 3 failed" in update["last_error"]


def test_tester_node_unknown_when_no_commands_configured(tmp_path: Path) -> None:
    src = _make_source_repo(tmp_path)
    ctx = _ctx(
        tmp_path,
        executor=_RecordingExecutor(ExecutorEntry(enabled=True)),
        worktree=None,
        shell=_RecordingExecutor(ExecutorEntry(enabled=True)),
        commands_test=[],
    )
    state = _state(workspace=str(src))
    update = _tester_node(ctx)(state)
    assert update["local_test_status"] == "unknown"


def test_tester_node_fails_when_workspace_missing(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        executor=_RecordingExecutor(ExecutorEntry(enabled=True)),
        worktree=None,
        shell=_RecordingExecutor(ExecutorEntry(enabled=True)),
        commands_test=["pytest"],
    )
    state = _state(workspace=str(tmp_path / "does-not-exist"))
    update = _tester_node(ctx)(state)
    assert update["local_test_status"] == "fail"


# --------- reviewer node ------------------------------------------------ #


def test_reviewer_node_passes_on_explicit_verdict(tmp_path: Path) -> None:
    src = _make_source_repo(tmp_path)
    backend = GitWorktreeBackend(src)
    mgr = WorktreeManager(tmp_path / "worktrees", backend=backend)
    coder_exec = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[
            ExecutorResult(
                ok=True,
                output="...",
                diff="--- a/x\n+++ b/x\n+y\n",
                changed_files=["x"],
            )
        ],
        write_file=("x", "y\n"),
    )
    review_exec = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[
            ExecutorResult(
                ok=True,
                output="Looks fine to me.\n\nVERDICT: PASS",
            )
        ],
    )

    # First run coder to populate diff
    ctx = _ctx(tmp_path, executor=coder_exec, worktree=mgr)
    _coder_node(ctx)(_state())

    # Then run reviewer with its own executor swapped in
    ctx_rev = NodeContext(
        config=ctx.config,
        executor=review_exec,
        artifacts=ctx.artifacts,
        run_dir=ctx.run_dir,
    )
    state = {**_state(), "changed_files": ["x"]}
    update = _reviewer_node(ctx_rev)(state)
    assert update["scratch"]["review_verdict"] == "pass"
    assert (ctx.run_dir.review_dir / "self_review.md").exists()


def test_reviewer_node_fails_when_no_verdict_emitted(tmp_path: Path) -> None:
    src = _make_source_repo(tmp_path)
    backend = GitWorktreeBackend(src)
    mgr = WorktreeManager(tmp_path / "worktrees", backend=backend)
    coder_exec = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[
            ExecutorResult(
                ok=True,
                output="...",
                diff="diff",
                changed_files=["x"],
            )
        ],
        write_file=("x", "y\n"),
    )
    review_exec = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[ExecutorResult(ok=True, output="I have no opinion")],
    )

    ctx = _ctx(tmp_path, executor=coder_exec, worktree=mgr)
    _coder_node(ctx)(_state())

    ctx_rev = NodeContext(
        config=ctx.config,
        executor=review_exec,
        artifacts=ctx.artifacts,
        run_dir=ctx.run_dir,
    )
    state = {**_state(), "changed_files": ["x"]}
    update = _reviewer_node(ctx_rev)(state)
    assert update["scratch"]["review_verdict"] == "fail"
    assert "no" in update["last_error"].lower() or "fail" in update["last_error"].lower()


def test_reviewer_node_fails_when_coder_made_no_changes(tmp_path: Path) -> None:
    review_exec = _RecordingExecutor(
        ExecutorEntry(enabled=True),
        script=[ExecutorResult(ok=True, output="VERDICT: PASS")],
    )
    ctx = _ctx(tmp_path, executor=review_exec, worktree=None)
    update = _reviewer_node(ctx)({**_state(), "changed_files": []})
    assert update["scratch"]["review_verdict"] == "fail"
    # executor should NOT have been invoked because there was no diff
    assert review_exec.calls == []


# --------- full-graph integration --------------------------------------- #


def test_full_round_stop_after_testing_real_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run planner→coder→tester through the live graph with a fake coder.

    We hijack the ``stub`` executor slot (always present in ``AppConfig``) so
    we don't have to extend the strict pydantic schema for the test. The shell
    executor really runs ``true`` against the worktree, exercising the
    conditional edges plus the artifact pipeline.
    """
    src = _make_source_repo(tmp_path)

    monkeypatch.setenv("AGENT_WORKER__REPO__LOCAL_PATH", str(src))
    monkeypatch.setenv("AGENT_WORKER__REPO__BASE_BRANCH", "main")
    monkeypatch.setenv("AGENT_WORKER__SANDBOX__MODE", "worktree")
    monkeypatch.setenv("AGENT_WORKER__SANDBOX__WORKTREE_ROOT", str(tmp_path / "wts"))
    monkeypatch.setenv("AGENT_WORKER__WORKFLOW__STOP_AFTER", "testing")
    monkeypatch.setenv("AGENT_WORKER__COMMANDS__TEST", '["true"]')
    monkeypatch.setenv("AGENT_WORKER__COMMANDS__LINT", "[]")
    monkeypatch.setenv("AGENT_WORKER__EXECUTOR__DEFAULT", "stub")

    class _Fake(CodeExecutor):
        name = "stub"

        def run(self, request: ExecutorRequest) -> ExecutorResult:
            if request.kind == "plan":
                return ExecutorResult(
                    ok=True,
                    output=("## Objective\nDo the thing\n\n## Subtasks\n- T1: foo\n"),
                )
            if request.workspace is not None:
                (request.workspace / "out.txt").write_text("hi\n")
                subprocess.run(["git", "add", "-N", "out.txt"], cwd=request.workspace, check=False)
            return ExecutorResult(
                ok=True,
                output="SUMMARY: wrote out.txt\nVERDICT: PASS",
                diff="--- a/out.txt\n+++ b/out.txt\n+hi\n",
                changed_files=["out.txt"],
            )

    register_executor("stub", _Fake)
    reset_cached_config()
    cfg = load_config()
    cfg.system.artifact_root = tmp_path / "runs"

    try:
        out = run_agent_round(
            config=cfg,
            round_input=AgentRoundInput(
                repo="acme/widget",
                issue_number=1,
                issue_title="Phase 2 smoke",
                issue_body="body",
            ),
        )
    finally:
        register_executor("stub", StubExecutor)

    assert out.final_status == "ready_for_review"
    assert "ready_for_review" in out.pending_issue_comment
    assert "out.txt" in out.pending_issue_comment

    run_root = tmp_path / "runs" / "acme--widget--issue-1"
    assert (run_root / "execution" / "diff.patch").exists()
    assert (run_root / "evidence" / "local_tests.log").exists()
    handoff = (run_root / "handoff.md").read_text()
    assert "local_test_status: pass" in handoff


# --------- prompt-contract regression tests ---------------------------- #
#
# These lock the prompt contracts that, when missing, caused real-world
# `agent-failed` rounds in production:
#
# - Issue #62: planner over-scoped a docs-only ask into "fix lint + tests +
#   md-babel"; reviewer then bound the coder to those side-quests and
#   FAIL-ed three times in a row even though the diff satisfied the issue
#   body. Root cause: PLAN_PROMPT_TEMPLATE had no scope-discipline rule,
#   REVIEW_PROMPT_TEMPLATE treated the plan as a binding contract and
#   converted "no tests configured" into a FAIL signal.
#
# - Issue #61: planner asserted invented marker / script / CI facts that
#   the coder copied verbatim into a doc; reviewer caught the drift and
#   FAIL-ed. Root cause: PLAN_PROMPT_TEMPLATE didn't constrain factual
#   claims to verified-from-workspace material.
#
# Each phrase asserted below is the load-bearing rule in the new prompt.
# If a future prompt rewrite drops one of these, the loop will silently
# regress to the old failure mode — these tests catch that at unit-test
# time instead of after a real PR cycle.


def test_plan_prompt_template_enforces_scope_and_fact_discipline() -> None:
    from app.langgraph_app.nodes.planner import PLAN_PROMPT_TEMPLATE

    text = PLAN_PROMPT_TEMPLATE

    # Scope discipline: subtasks are bounded by the issue body.
    assert "Scope discipline" in text
    assert "directly required" in text
    assert "## Follow-ups" in text
    assert "out of scope" in text
    # Explicit ban on the most common over-scoping patterns observed in
    # production failures (#62-style).
    assert "fix CI" in text and "make tests pass" in text and "fix lint" in text

    # Fact discipline: don't assert what you haven't verified in the workspace.
    assert "Fact discipline" in text
    assert "verify in workspace" in text
    assert "{workspace_hint}" in text  # hint is parameterised, not hard-coded

    # Output structure must keep ## Subtasks (todo extractor depends on it)
    # and must list ## Follow-ups *after* ## Verification so the existing
    # `_extract_todo` heuristic (stop at next ## heading) does not slurp
    # follow-up bullets into the coder's todo list. Use rfind to look at
    # the structure-defining mentions in the "Output requirements" section
    # rather than the earlier rule explanations.
    subtasks_at = text.rfind("## Subtasks")
    verification_at = text.rfind("## Verification")
    followups_at = text.rfind("## Follow-ups")
    assert 0 < subtasks_at < verification_at < followups_at, (
        "PLAN_PROMPT_TEMPLATE section order regressed; _extract_todo "
        "would slurp follow-ups into the coder's todo list."
    )


def test_plan_prompt_template_renders_with_state_keys() -> None:
    """The template still .format()-s cleanly with the keys planner_node passes.

    Regression guard for the {workspace_hint} placeholder we added: if it
    drifts out of sync with planner_node._make_planner, planning will
    KeyError at runtime instead of producing a plan.
    """
    from app.langgraph_app.nodes.planner import PLAN_PROMPT_TEMPLATE

    rendered = PLAN_PROMPT_TEMPLATE.format(
        repo="acme/widget",
        issue_number=42,
        issue_title="title",
        issue_body="body",
        workspace_hint="/tmp/checkout",
    )
    assert "/tmp/checkout" in rendered
    assert "{" not in rendered.replace("{}", ""), (
        "PLAN_PROMPT_TEMPLATE has an unfilled {placeholder}; planner_node will KeyError at runtime."
    )


def test_review_prompt_template_enforces_acceptance_ladder() -> None:
    from app.langgraph_app.nodes.reviewer import REVIEW_PROMPT_TEMPLATE

    text = REVIEW_PROMPT_TEMPLATE

    # The five-step acceptance ladder: issue fulfilment first, plan
    # compliance only as advisory, follow-ups exempt, neutral handling of
    # missing local-test evidence.
    assert "Acceptance criteria" in text
    assert "Issue fulfilment" in text
    assert "literally asks" in text  # criterion 1 wording
    assert "NEUTRAL" in text  # criterion 3 wording for "no tests configured"
    assert "advisory only" in text or "guide, not a contract" in text  # criterion 4
    assert "Follow-ups exemption" in text  # criterion 5

    # The verdict must cite a concrete defect from the diff, not a
    # missing optional task. This is what stopped #62 from converging.
    assert "concrete defect" in text
    assert "missing tests we never asked for" in text
    assert "did not finish a follow-up" in text


def test_review_prompt_neutral_test_status_summary_for_unconfigured_projects() -> None:
    """When commands.test/lint are empty, the test summary must signal NEUTRAL.

    Regression guard for the #62-style failure mode where the reviewer
    converted "no tests were run" into grounds for FAIL even though the
    project simply hadn't wired commands.test.
    """
    from app.langgraph_app.nodes.reviewer import _build_prompt

    state = {
        "repo": "acme/widget",
        "issue_number": 1,
        "issue_title": "t",
        "issue_body": "b",
        "plan": "p",
        "local_test_status": "unknown",
    }
    rendered = _build_prompt(state, diff="", changed_files=["x"])  # type: ignore[arg-type]
    assert "NEUTRAL" in rendered
    assert "project-level" in rendered or "config choice" in rendered
