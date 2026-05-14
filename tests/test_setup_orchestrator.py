"""Tests for the bootstrap orchestrator.

We exercise each phase via :func:`run_bootstrap` with a recording IO, a fake
shell (so PREFLIGHT doesn't actually call ``gh`` / ``git``), and a config the
wizard would have produced. The SMOKE phase actually runs through the
LangGraph stub flow — it's deliberately lightweight enough to do that
in-process without external services.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import AppConfig, load_config
from app.setup.orchestrator import (
    PhaseResult,
    PhaseStatus,
    run_bootstrap,
)


def _green_shell(
    cmd: list[str], *, cwd: Path | None = None, timeout: float = 30.0
) -> tuple[int, str, str]:
    """Fake shell that makes every doctor preflight pass.

    Returns canned, well-formed responses for the gh/git probes the doctor
    runs. Anything we haven't taught it returns rc=0 so optional probes
    (e.g. label-list with no remote) fall through harmlessly.
    """
    if cmd[:3] == ["gh", "auth", "status"]:
        return (0, "", "Logged in to github.com as test-user")
    if cmd[:3] == ["gh", "repo", "view"]:
        return (
            0,
            '{"name":"widget","owner":{"login":"acme"},"defaultBranchRef":{"name":"main"}}',
            "",
        )
    if cmd[:2] == ["gh", "api"]:
        return (0, '{"push":true,"admin":false,"pull":true}', "")
    if cmd[:3] == ["gh", "label", "list"]:
        # Pretend every label is already present.
        names = [
            "agent:todo",
            "agent:queued",
            "agent:running",
            "agent:planning",
            "agent:coding",
            "agent:testing",
            "agent:pr-created",
            "agent:ci-running",
            "agent:review",
            "agent:blocked",
            "agent:failed",
            "agent:done",
        ]
        body = ",".join(f'{{"name":"{n}"}}' for n in names)
        return (0, f"[{body}]", "")
    return (0, "", "")


class RecorderIO:
    def __init__(self, *, confirm_answer: bool = True) -> None:
        self.events: list[tuple[str, str]] = []
        self.results: list[PhaseResult] = []
        self.info_log: list[str] = []
        self.confirm_answer = confirm_answer

    def phase_start(self, name: str, description: str) -> None:
        self.events.append(("start", name))

    def phase_finish(self, result: PhaseResult) -> None:
        self.events.append(("finish", result.name))
        self.results.append(result)

    def info(self, message: str) -> None:
        self.info_log.append(message)

    def confirm(self, prompt: str, *, default: bool = True) -> bool:
        return self.confirm_answer

    def by_name(self, name: str) -> PhaseResult:
        for r in self.results:
            if r.name == name:
                return r
        raise AssertionError(
            f"phase {name!r} never finished. Got: {[r.name for r in self.results]}"
        )


@pytest.fixture
def configured_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a minimal valid YAML and point AGENT_WORKER_CONFIG at it."""
    yaml_path = tmp_path / "configs" / "test.yaml"
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "project": {"mode": "existing", "description": "test"},
                "repo": {"owner": "acme", "name": "widget", "base_branch": "main"},
                "commands": {"test": ["pytest"]},
                "executor": {"default": "stub"},
                "sandbox": {"mode": "local"},
                "system": {"artifact_root": str(tmp_path / "runs")},
                "langgraph": {
                    "checkpoint_backend": "memory",
                    "checkpoint_db": str(tmp_path / "lg.sqlite"),
                },
            }
        )
    )
    monkeypatch.setenv("AGENT_WORKER_CONFIG", str(yaml_path))
    return yaml_path


def _loader(yaml_path: Path):
    def _go() -> AppConfig:
        return load_config(config_path=str(yaml_path))

    return _go


def test_config_phase_uses_existing_yaml(tmp_path: Path, configured_yaml: Path) -> None:
    io = RecorderIO()
    run_bootstrap(
        io=io,
        config_loader=_loader(configured_yaml),
        project_root=tmp_path,
        run_wizard_if_unconfigured=lambda: None,
        full=False,
        interactive=False,
        doctor_shell=_green_shell,
    )
    cfg_phase = io.by_name("CONFIG")
    assert cfg_phase.status == PhaseStatus.PASS
    assert "test.yaml" in cfg_phase.summary


def test_config_phase_invokes_wizard_when_no_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_WORKER_CONFIG", raising=False)
    written = tmp_path / "configs" / "wiz.yaml"
    written.parent.mkdir(parents=True)
    written.write_text(
        yaml.safe_dump(
            {
                "project": {"mode": "existing", "description": "wiz"},
                "repo": {"owner": "acme", "name": "widget", "base_branch": "main"},
                "executor": {"default": "stub"},
                "sandbox": {"mode": "local"},
                "system": {"artifact_root": str(tmp_path / "runs")},
                "langgraph": {
                    "checkpoint_backend": "memory",
                    "checkpoint_db": str(tmp_path / "lg.sqlite"),
                },
            }
        )
    )

    wizard_called: list[Path] = []

    def fake_wizard() -> Path:
        wizard_called.append(written)
        monkeypatch.setenv("AGENT_WORKER_CONFIG", str(written))
        return written

    io = RecorderIO()
    run_bootstrap(
        io=io,
        config_loader=_loader(written),
        project_root=tmp_path,
        run_wizard_if_unconfigured=fake_wizard,
        full=False,
        interactive=False,
        doctor_shell=_green_shell,
    )
    assert wizard_called == [written]
    assert io.by_name("CONFIG").status == PhaseStatus.PASS


def test_config_phase_fails_when_no_yaml_and_wizard_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_WORKER_CONFIG", raising=False)
    io = RecorderIO()
    result = run_bootstrap(
        io=io,
        config_loader=lambda: load_config(),
        project_root=tmp_path,
        run_wizard_if_unconfigured=None,
        full=False,
        interactive=False,
    )
    config_phase = io.by_name("CONFIG")
    assert config_phase.status == PhaseStatus.FAIL
    # Subsequent phases must not run after a FAIL.
    assert [r.name for r in io.results] == ["CONFIG"]
    assert not result.ok
    assert result.stopped_at is not None
    assert result.stopped_at.name == "CONFIG"


def test_smoke_phase_runs_planner_round(tmp_path: Path, configured_yaml: Path) -> None:
    io = RecorderIO()
    run_bootstrap(
        io=io,
        config_loader=_loader(configured_yaml),
        project_root=tmp_path,
        run_wizard_if_unconfigured=lambda: None,
        full=False,
        interactive=False,
        doctor_shell=_green_shell,
    )
    smoke = io.by_name("SMOKE")
    assert smoke.status == PhaseStatus.PASS
    assert "plan.md" in smoke.summary
    plan_path = Path(smoke.artifacts["plan"])
    assert plan_path.exists()
    assert plan_path.stat().st_size > 0


def test_live_read_skipped_when_user_declines(tmp_path: Path, configured_yaml: Path) -> None:
    io = RecorderIO(confirm_answer=False)
    # interactive=True forces the orchestrator to use io.confirm.
    run_bootstrap(
        io=io,
        config_loader=_loader(configured_yaml),
        project_root=tmp_path,
        run_wizard_if_unconfigured=lambda: None,
        full=False,
        interactive=True,
        doctor_shell=_green_shell,
    )
    live = io.by_name("LIVE_READ")
    assert live.status == PhaseStatus.SKIP


def test_no_live_read_flag_skips_live_read(tmp_path: Path, configured_yaml: Path) -> None:
    io = RecorderIO()
    run_bootstrap(
        io=io,
        config_loader=_loader(configured_yaml),
        project_root=tmp_path,
        run_wizard_if_unconfigured=lambda: None,
        full=False,
        interactive=False,
        doctor_shell=_green_shell,
        skip_live_read=True,
    )
    live = io.by_name("LIVE_READ")
    assert live.status == PhaseStatus.SKIP
    assert "--no-live-read" in live.summary


_SMOKE_ISSUE = 999_999  # Mirror of the synthetic issue id _phase_smoke uses.


def test_live_read_timeout_returns_warn_not_fail(
    tmp_path: Path, configured_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planner that takes longer than the budget must be WARN, not FAIL.

    SMOKE and LIVE_READ both call ``run_agent_round``; the stub here lets
    SMOKE through (so the chain reaches LIVE_READ) and only slows down for
    the real issue number we feed in.
    """
    import time as _time

    from app.langgraph_app.graph import run_agent_round as _real_round
    from app.setup import orchestrator as _orch

    def conditional_slow(*, config, round_input):
        if round_input.issue_number == _SMOKE_ISSUE:
            return _real_round(config=config, round_input=round_input)
        _time.sleep(2.0)
        raise AssertionError("should have been cancelled by timeout")

    monkeypatch.setattr("app.langgraph_app.graph.run_agent_round", conditional_slow)
    monkeypatch.setattr(_orch, "_find_first_open_agent_todo_issue", lambda config: 1)

    from app.github.issue_service import Issue

    def fake_fetch(self, n):
        return Issue(number=n, title="t", body="b" * 50, labels=[], state="open")

    monkeypatch.setattr("app.github.issue_service.GitHubIssueService.fetch", fake_fetch)

    io = RecorderIO()
    result = run_bootstrap(
        io=io,
        config_loader=_loader(configured_yaml),
        project_root=tmp_path,
        run_wizard_if_unconfigured=lambda: None,
        full=False,
        interactive=False,
        doctor_shell=_green_shell,
        live_read_timeout_seconds=0.2,
    )
    live = io.by_name("LIVE_READ")
    assert live.status == PhaseStatus.WARN
    assert "exceeded" in live.summary
    assert any(p.name == "FULL" for p in result.phases)


def test_live_read_executor_crash_is_warn_not_fail(
    tmp_path: Path, configured_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planner crash should not stop the chain — bootstrap continues with a WARN."""
    from app.langgraph_app.graph import run_agent_round as _real_round
    from app.setup import orchestrator as _orch

    def conditional_boom(*, config, round_input):
        if round_input.issue_number == _SMOKE_ISSUE:
            return _real_round(config=config, round_input=round_input)
        raise RuntimeError("simulated executor crash")

    monkeypatch.setattr("app.langgraph_app.graph.run_agent_round", conditional_boom)
    monkeypatch.setattr(_orch, "_find_first_open_agent_todo_issue", lambda config: 7)

    from app.github.issue_service import Issue

    monkeypatch.setattr(
        "app.github.issue_service.GitHubIssueService.fetch",
        lambda self, n: Issue(number=n, title="t", body="b", labels=[], state="open"),
    )

    io = RecorderIO()
    result = run_bootstrap(
        io=io,
        config_loader=_loader(configured_yaml),
        project_root=tmp_path,
        run_wizard_if_unconfigured=lambda: None,
        full=False,
        interactive=False,
        doctor_shell=_green_shell,
    )
    live = io.by_name("LIVE_READ")
    assert live.status == PhaseStatus.WARN
    assert "crashed" in live.summary
    assert result.ok
    assert any(p.name == "FULL" for p in result.phases)


def test_full_phase_skipped_unless_full_flag(tmp_path: Path, configured_yaml: Path) -> None:
    io = RecorderIO()
    result = run_bootstrap(
        io=io,
        config_loader=_loader(configured_yaml),
        project_root=tmp_path,
        run_wizard_if_unconfigured=lambda: None,
        full=False,
        interactive=False,
        doctor_shell=_green_shell,
    )
    assert result.ok
    full = io.by_name("FULL")
    assert full.status == PhaseStatus.SKIP
    assert "opt-in" in full.summary


def test_preflight_fail_stops_chain(tmp_path: Path, configured_yaml: Path) -> None:
    """If doctor returns any FAIL, the chain stops and SMOKE never runs."""

    # A shell that makes the GitHub repo accessibility check fail loudly.
    def red_shell(cmd: list[str], *, cwd: Path | None = None, timeout: float = 30.0):
        if cmd[:3] == ["gh", "repo", "view"]:
            return (1, "", "GraphQL: Could not resolve to a Repository (404)")
        return _green_shell(cmd, cwd=cwd, timeout=timeout)

    io = RecorderIO()
    result = run_bootstrap(
        io=io,
        config_loader=_loader(configured_yaml),
        project_root=tmp_path,
        run_wizard_if_unconfigured=lambda: None,
        full=False,
        interactive=False,
        doctor_shell=red_shell,
    )
    assert not result.ok
    assert result.stopped_at is not None
    assert result.stopped_at.name == "PREFLIGHT"
    # SMOKE / LIVE_READ / FULL must not have run.
    names = [r.name for r in io.results]
    assert "SMOKE" not in names
    assert "LIVE_READ" not in names
