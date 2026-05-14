"""Tests for the doctor preflight checks.

We inject a fake shell runner so the checks run without ever touching ``gh``,
``git``, or the network. The point of the test suite is the *decision logic*
(when do we PASS / WARN / FAIL, when does --fix actually attempt the fix); the
real CLI integration is exercised in usage docs only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import load_config
from app.setup.doctor import CheckOutcome, run_doctor


@pytest.fixture(autouse=True)
def _allow_executor_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the autouse EXECUTOR__DEFAULT=stub so per-test executor wins."""
    monkeypatch.delenv("AGENT_WORKER__EXECUTOR__DEFAULT", raising=False)


def _make_config(
    tmp_path: Path,
    *,
    project_mode: str = "existing",
    description: str = "test worker",
    sandbox: str = "local",
    local_path: str = "",
    commands_test: list[str] | None = None,
    executor: str = "stub",
) -> object:
    overlay = {
        "project": {"mode": project_mode, "description": description},
        "repo": {
            "owner": "acme",
            "name": "widget",
            "base_branch": "main",
            "local_path": local_path,
        },
        "commands": {"test": commands_test or []},
        "executor": {"default": executor},
        "sandbox": {"mode": sandbox},
        "system": {"artifact_root": str(tmp_path / "runs")},
        "langgraph": {"checkpoint_db": str(tmp_path / "ckpt" / "lg.sqlite")},
    }
    import yaml

    overlay_path = tmp_path / "overlay.yaml"
    overlay_path.write_text(yaml.safe_dump(overlay))
    return load_config(config_path=str(overlay_path))


class FakeShell:
    """Map (cmd-tuple, ...) → (rc, stdout, stderr) responses."""

    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str, str]]) -> None:
        self._responses = responses
        self.calls: list[list[str]] = []

    def __call__(
        self, cmd: list[str], *, cwd: Path | None = None, timeout: float = 30.0
    ) -> tuple[int, str, str]:
        self.calls.append(list(cmd))
        key = tuple(cmd)
        if key in self._responses:
            return self._responses[key]
        # Try prefix matches so callers can stub by command name.
        for stub_key, response in self._responses.items():
            if tuple(cmd[: len(stub_key)]) == stub_key:
                return response
        return (127, "", f"unstubbed command: {cmd}")


def _by_name(reports: list, name: str):
    matching = [r for r in reports if r.name == name]
    assert matching, f"missing report named {name!r}"
    return matching[0]


def test_repo_identity_fails_when_owner_missing(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.repo.owner = ""
    cfg.repo.name = ""
    shell = FakeShell({})
    result = run_doctor(cfg, fix=False, check_temporal=False, shell=shell)
    report = _by_name(result.reports, "repo identity")
    assert report.outcome == CheckOutcome.FAIL


def test_full_happy_path_passes(tmp_path: Path) -> None:
    cfg = _make_config(
        tmp_path,
        sandbox="local",
        commands_test=["pytest"],
        executor="stub",
    )
    shell = FakeShell(
        {
            ("gh", "auth", "status"): (0, "", "Logged in to github.com as alice"),
            ("gh", "repo", "view", "acme/widget", "--json", "name,owner,visibility,defaultBranchRef"): (
                0,
                '{"name":"widget","owner":{"login":"acme"},"defaultBranchRef":{"name":"main"}}',
                "",
            ),
            ("gh", "api", "repos/acme/widget", "--jq", ".permissions"): (
                0,
                '{"admin":false,"push":true,"pull":true}',
                "",
            ),
            ("gh", "label", "list", "--repo", "acme/widget", "--limit", "200", "--json", "name"): (
                0,
                _all_labels_present_json(),
                "",
            ),
        }
    )
    result = run_doctor(cfg, fix=False, check_temporal=False, shell=shell)
    fails = [r for r in result.reports if r.outcome == CheckOutcome.FAIL]
    assert not fails, [r.name + ": " + r.detail for r in fails]


def test_push_permission_fails_loudly(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    shell = FakeShell(
        {
            ("gh", "auth", "status"): (0, "", "Logged in"),
            ("gh", "repo", "view"): (
                0,
                '{"name":"widget","owner":{"login":"acme"},"defaultBranchRef":{"name":"main"}}',
                "",
            ),
            ("gh", "api", "repos/acme/widget", "--jq", ".permissions"): (
                0,
                '{"admin":false,"push":false,"pull":true}',
                "",
            ),
            ("gh", "label", "list"): (0, _all_labels_present_json(), ""),
        }
    )
    result = run_doctor(cfg, fix=False, check_temporal=False, shell=shell)
    push = _by_name(result.reports, "push permission on repo")
    assert push.outcome == CheckOutcome.FAIL
    assert "push=false" in push.detail or "push" in push.detail


def test_missing_labels_warn_without_fix(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    shell = FakeShell(
        {
            ("gh", "auth", "status"): (0, "", "Logged in"),
            ("gh", "repo", "view"): (
                0,
                '{"name":"widget","owner":{"login":"acme"},"defaultBranchRef":{"name":"main"}}',
                "",
            ),
            ("gh", "api", "repos/acme/widget", "--jq", ".permissions"): (
                0,
                '{"push":true}',
                "",
            ),
            ("gh", "label", "list"): (
                0,
                '[{"name":"agent:todo"}]',  # only one of the required labels
                "",
            ),
        }
    )
    result = run_doctor(cfg, fix=False, check_temporal=False, shell=shell)
    labels = _by_name(result.reports, "required labels")
    assert labels.outcome == CheckOutcome.WARN
    assert "missing" in labels.detail


def test_missing_labels_creates_them_with_fix(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    shell = FakeShell(
        {
            ("gh", "auth", "status"): (0, "", "Logged in"),
            ("gh", "repo", "view"): (
                0,
                '{"name":"widget","owner":{"login":"acme"},"defaultBranchRef":{"name":"main"}}',
                "",
            ),
            ("gh", "api", "repos/acme/widget", "--jq", ".permissions"): (
                0,
                '{"push":true}',
                "",
            ),
            ("gh", "label", "list"): (0, "[]", ""),
            ("gh", "label", "create"): (0, "", ""),
        }
    )
    result = run_doctor(cfg, fix=True, check_temporal=False, shell=shell)
    labels = _by_name(result.reports, "required labels")
    assert labels.outcome == CheckOutcome.PASS
    assert labels.fix_applied
    create_calls = [c for c in shell.calls if c[:3] == ["gh", "label", "create"]]
    assert len(create_calls) == 12  # all required labels


def test_local_checkout_skips_when_local_sandbox(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, sandbox="local")
    shell = FakeShell(
        {
            ("gh",): (1, "", "no gh"),  # so the gh-dependent checks short-circuit
        }
    )
    result = run_doctor(cfg, fix=False, check_temporal=False, shell=shell)
    local = _by_name(result.reports, "local checkout")
    assert local.outcome == CheckOutcome.SKIP


def test_local_checkout_fails_when_worktree_without_path(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, sandbox="worktree", local_path="")
    shell = FakeShell({("gh",): (1, "", "no gh")})
    result = run_doctor(cfg, fix=False, check_temporal=False, shell=shell)
    local = _by_name(result.reports, "local checkout")
    assert local.outcome == CheckOutcome.FAIL
    assert "local_path" in local.detail


def test_local_checkout_passes_when_path_is_git(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)
    cfg = _make_config(tmp_path, sandbox="worktree", local_path=str(repo))
    shell = FakeShell({("gh",): (1, "", "no gh")})
    result = run_doctor(cfg, fix=False, check_temporal=False, shell=shell)
    local = _by_name(result.reports, "local checkout")
    assert local.outcome == CheckOutcome.PASS


def test_scaffold_inits_path_with_fix(tmp_path: Path) -> None:
    target = tmp_path / "newrepo"
    cfg = _make_config(
        tmp_path, project_mode="scaffold", sandbox="local", local_path=str(target)
    )
    git_inits: list[list[str]] = []

    def shell(cmd: list[str], *, cwd: Path | None = None, timeout: float = 30.0):
        if cmd[0] == "gh":
            return (1, "", "no gh")
        if cmd[:2] == ["git", "init"]:
            git_inits.append(cmd)
            (target / ".git").mkdir(parents=True, exist_ok=True)
            return (0, "", "")
        return (127, "", "")

    result = run_doctor(cfg, fix=True, check_temporal=False, shell=shell)
    local = _by_name(result.reports, "local checkout")
    assert local.outcome == CheckOutcome.PASS
    assert git_inits, "expected git init to be invoked"
    assert (target / ".git").exists()


def test_executor_warns_when_binary_missing(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, executor="cursor")
    cfg.executor.cursor.command = "definitely-not-a-real-bin-xyz"
    shell = FakeShell({("gh",): (1, "", "")})
    result = run_doctor(cfg, fix=False, check_temporal=False, shell=shell)
    exec_report = _by_name(result.reports, "default executor binary")
    assert exec_report.outcome == CheckOutcome.WARN
    assert "PATH" in exec_report.detail


def test_executor_skips_check_for_stub(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, executor="stub")
    shell = FakeShell({("gh",): (1, "", "")})
    result = run_doctor(cfg, fix=False, check_temporal=False, shell=shell)
    exec_report = _by_name(result.reports, "default executor binary")
    assert exec_report.outcome == CheckOutcome.PASS
    assert "stub" in exec_report.detail


def test_filesystem_creates_dirs_with_fix(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    shell = FakeShell({("gh",): (1, "", "")})
    # Pre-condition: artifact_root and checkpoint dir do not exist.
    artifact_root = Path(cfg.system.artifact_root)
    ckpt_parent = Path(cfg.langgraph.checkpoint_db).parent
    assert not artifact_root.exists()
    assert not ckpt_parent.exists()
    result = run_doctor(cfg, fix=True, check_temporal=False, shell=shell)
    artifact = _by_name(result.reports, "artifact_root writable")
    ckpt = _by_name(result.reports, "checkpoint dir writable")
    assert artifact.outcome == CheckOutcome.PASS
    assert ckpt.outcome == CheckOutcome.PASS
    assert artifact_root.exists()
    assert ckpt_parent.exists()


def test_warn_when_no_description(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, description="")
    shell = FakeShell({("gh",): (1, "", "")})
    result = run_doctor(cfg, fix=False, check_temporal=False, shell=shell)
    intent = _by_name(result.reports, "project intent")
    assert intent.outcome == CheckOutcome.WARN


def test_exit_code_reflects_fails(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.repo.owner = ""
    cfg.repo.name = ""
    shell = FakeShell({})
    result = run_doctor(cfg, fix=False, check_temporal=False, shell=shell)
    assert result.exit_code == 1


def _all_labels_present_json() -> str:
    """Return a JSON array of every required label name, matching default.yaml."""
    return (
        "["
        '{"name":"agent:todo"},'
        '{"name":"agent:queued"},'
        '{"name":"agent:running"},'
        '{"name":"agent:planning"},'
        '{"name":"agent:coding"},'
        '{"name":"agent:testing"},'
        '{"name":"agent:pr-created"},'
        '{"name":"agent:ci-running"},'
        '{"name":"agent:review"},'
        '{"name":"agent:blocked"},'
        '{"name":"agent:failed"},'
        '{"name":"agent:done"}'
        "]"
    )
