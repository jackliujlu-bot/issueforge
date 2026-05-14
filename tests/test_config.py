"""Config loader: precedence and portability invariants."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import load_config, reset_cached_config


def test_defaults_load_without_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    # Strip the autouse owner/name to simulate a brand-new install.
    monkeypatch.delenv("AGENT_WORKER__REPO__OWNER", raising=False)
    monkeypatch.delenv("AGENT_WORKER__REPO__NAME", raising=False)
    reset_cached_config()
    cfg = load_config()
    assert cfg.system.name == "issue-agent-worker"
    assert cfg.repo.base_branch == "main"
    # Default executor is the stub-friendly cursor entry; we set executor.default=stub
    # in autouse fixture, so the default still resolves.
    assert cfg.executor.default == "stub"


def test_project_yaml_overlays_default(tmp_path: Path) -> None:
    overlay = tmp_path / "project.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "repo": {"owner": "acme", "name": "widget", "base_branch": "develop"},
                "commands": {"test": ["pytest -q"]},
            }
        )
    )
    cfg = load_config(config_path=str(overlay))
    assert cfg.repo.owner == "acme"
    assert cfg.repo.base_branch == "develop"
    assert cfg.commands.test == ["pytest -q"]


def test_env_overrides_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    overlay = tmp_path / "project.yaml"
    overlay.write_text(yaml.safe_dump({"repo": {"owner": "fromyaml", "name": "x"}}))
    monkeypatch.setenv("AGENT_WORKER__REPO__OWNER", "fromenv")
    cfg = load_config(config_path=str(overlay))
    assert cfg.repo.owner == "fromenv"


def test_cli_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_WORKER__REPO__OWNER", "fromenv")
    cfg = load_config(cli_overrides={"repo": {"owner": "fromcli"}})
    assert cfg.repo.owner == "fromcli"


def test_short_env_aliases_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_HOST", "temporal.prod:7233")
    monkeypatch.setenv("CURSOR_AGENT_BIN", "/usr/local/bin/cursor-agent")
    cfg = load_config()
    assert cfg.workflow.temporal.host == "temporal.prod:7233"
    assert cfg.executor.cursor.command == "/usr/local/bin/cursor-agent"


def test_default_executor_must_be_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv("AGENT_WORKER__EXECUTOR__DEFAULT", "claude_code")
    monkeypatch.setenv("AGENT_WORKER__EXECUTOR__CLAUDE_CODE__ENABLED", "false")
    with pytest.raises(ValidationError):
        load_config()


def test_repo_slug_and_clone_url() -> None:
    cfg = load_config()
    assert cfg.repo.slug == "acme/widget"
    assert cfg.repo.resolved_clone_url.endswith(":acme/widget.git")


def test_no_hardcoded_repo_or_branch_in_app_code() -> None:
    """Portability invariant: search the app/ tree for forbidden hardcoded
    project identifiers. If this test fails someone slipped a literal in.
    """
    import re

    forbidden = re.compile(r"feipeng1234|/dimos[/\"' ]|base_branch\s*=\s*['\"]dev['\"]")
    here = Path(__file__).resolve().parent.parent / "app"
    offenders: list[str] = []
    for path in here.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if forbidden.search(text):
            offenders.append(str(path))
    assert not offenders, f"hardcoded project values in: {offenders}"
