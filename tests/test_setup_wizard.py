"""Tests for the wizard. Drive it with a scripted IO so we don't need a TTY."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import load_config
from app.setup.wizard import (
    ConfigChoice,
    WizardAnswers,
    choose_config_strategy,
    clone_existing_yaml,
    discover_existing_configs,
    run_wizard,
    update_dotenv,
    write_project_yaml,
)


class ScriptedIO:
    """:class:`WizardIO` that pulls answers from a queued list."""

    def __init__(self, answers: list[str | bool]) -> None:
        self._answers = list(answers)
        self.info_log: list[str] = []
        self.warn_log: list[str] = []

    def ask(self, prompt: str, *, default: str = "", choices: list[str] | None = None) -> str:
        if not self._answers:
            raise AssertionError(f"out of scripted answers at prompt: {prompt!r}")
        value = self._answers.pop(0)
        if value == "":  # explicit accept-default
            value = default
        assert isinstance(value, str), f"expected str answer for {prompt!r}, got {value!r}"
        if choices and value not in choices:
            raise AssertionError(
                f"scripted answer {value!r} not in choices {choices!r} for {prompt!r}"
            )
        return value

    def ask_bool(self, prompt: str, *, default: bool = False) -> bool:
        if not self._answers:
            raise AssertionError(f"out of scripted answers at prompt: {prompt!r}")
        value = self._answers.pop(0)
        if value == "":
            return default
        assert isinstance(value, bool), f"expected bool answer for {prompt!r}, got {value!r}"
        return value

    def info(self, message: str) -> None:
        self.info_log.append(message)

    def warn(self, message: str) -> None:
        self.warn_log.append(message)


def test_wizard_full_existing_repo_flow(tmp_path: Path) -> None:
    project_root = tmp_path
    cwd = tmp_path / "checkout"
    cwd.mkdir()

    io = ScriptedIO(
        [
            "existing",                              # project_mode
            "Long-running coding agent for acme/widget.",  # description
            "acme/widget",                            # repo slug
            "",                                        # custom clone url (default empty)
            "main",                                    # base branch
            str(cwd),                                  # local path
            "uv sync",                                 # setup
            "uv run ruff check",                       # lint
            "uv run pytest",                           # test
            "",                                        # build (empty)
            "cursor",                                  # executor
            "worktree",                                # sandbox mode
            False,                                     # feishu_enabled
            "configs/widget.yaml",                     # output path
            True,                                      # update dotenv
        ]
    )
    answers = run_wizard(io, project_root=project_root, cwd=cwd)

    assert answers.project_mode == "existing"
    assert answers.repo_owner == "acme"
    assert answers.repo_name == "widget"
    assert answers.repo_base_branch == "main"
    assert answers.repo_local_path == str(cwd)
    assert answers.commands_setup == ["uv sync"]
    assert answers.commands_test == ["uv run pytest"]
    assert answers.commands_build == []
    assert answers.executor_default == "cursor"
    assert answers.sandbox_mode == "worktree"
    assert answers.feishu_enabled is False
    assert answers.output_yaml_path == Path("configs/widget.yaml")


def test_wizard_scaffold_mode_offers_init_path(tmp_path: Path) -> None:
    io = ScriptedIO(
        [
            "scaffold",
            "Greenfield service.",
            "acme/newthing",
            "",
            "main",
            str(tmp_path / "newthing"),
            "",  # setup
            "",  # lint
            "",  # test
            "",  # build
            "stub",  # executor
            "local",  # sandbox
            False,
            "configs/newthing.yaml",
            False,
        ]
    )
    answers = run_wizard(io, project_root=tmp_path, cwd=tmp_path)
    assert answers.project_mode == "scaffold"
    assert answers.repo_local_path == str(tmp_path / "newthing")


def test_wizard_warns_on_invalid_slug(tmp_path: Path) -> None:
    io = ScriptedIO(
        [
            "existing",
            "",
            "no-slash-here",
            "",
            "main",
            "",
            "",
            "",
            "",
            "",
            "stub",
            "local",
            False,
            "configs/x.yaml",
            False,
        ]
    )
    answers = run_wizard(io, project_root=tmp_path, cwd=tmp_path)
    assert answers.repo_owner == "no-slash-here"
    assert answers.repo_name == ""
    assert any("not a valid owner/name slug" in w for w in io.warn_log)


def test_write_project_yaml_includes_header_and_round_trips(tmp_path: Path) -> None:
    answers = WizardAnswers(
        project_mode="existing",
        project_description="hello",
        repo_owner="acme",
        repo_name="widget",
        repo_base_branch="dev",
        repo_local_path=str(tmp_path),
        commands_setup=["uv sync"],
        commands_test=["uv run pytest"],
        executor_default="cursor",
        sandbox_mode="worktree",
        output_yaml_path=Path("configs/widget.yaml"),
    )
    out = write_project_yaml(answers, project_root=tmp_path)
    assert out.exists()
    text = out.read_text()
    assert "# Generated by `agent-worker init`" in text
    assert "# Project mode: existing" in text
    payload = yaml.safe_load(text)
    assert payload["project"]["description"] == "hello"
    assert payload["repo"]["base_branch"] == "dev"
    assert payload["commands"]["test"] == ["uv run pytest"]


def test_write_project_yaml_loads_back_into_appconfig(tmp_path: Path) -> None:
    answers = WizardAnswers(
        project_mode="existing",
        project_description="loop",
        repo_owner="acme",
        repo_name="widget",
        repo_base_branch="main",
        repo_local_path=str(tmp_path),
        commands_test=["pytest"],
        executor_default="stub",
        sandbox_mode="local",
        output_yaml_path=Path("configs/round-trip.yaml"),
    )
    out = write_project_yaml(answers, project_root=tmp_path)
    cfg = load_config(config_path=str(out))
    assert cfg.project.mode == "existing"
    assert cfg.project.description == "loop"
    assert cfg.repo.slug == "acme/widget"
    assert cfg.commands.test == ["pytest"]


def test_update_dotenv_creates_file_if_missing(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    update_dotenv(env_path, config_path=Path("configs/x.yaml"))
    assert env_path.read_text() == "AGENT_WORKER_CONFIG=configs/x.yaml\n"


def test_update_dotenv_replaces_existing_line(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "AGENT_WORKER_CONFIG=old.yaml\nGITHUB_TOKEN=secret\n"
    )
    update_dotenv(env_path, config_path=Path("configs/new.yaml"))
    text = env_path.read_text()
    assert "AGENT_WORKER_CONFIG=configs/new.yaml" in text
    assert "GITHUB_TOKEN=secret" in text
    assert "old.yaml" not in text


def test_update_dotenv_appends_when_key_absent(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("GITHUB_TOKEN=secret\n")
    update_dotenv(env_path, config_path=Path("configs/x.yaml"))
    text = env_path.read_text()
    assert "AGENT_WORKER_CONFIG=configs/x.yaml" in text
    assert "GITHUB_TOKEN=secret" in text


# --------------------------------------------------------------------------- #
#  Discovery + import-existing menu                                           #
# --------------------------------------------------------------------------- #


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload))


def test_discover_skips_default_yaml_and_returns_overlays(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "configs" / "default.yaml", {"system": {"name": "x"}})
    _write_yaml(
        tmp_path / "configs" / "alpha.yaml",
        {
            "project": {"mode": "existing", "description": "alpha service"},
            "repo": {"owner": "acme", "name": "alpha", "base_branch": "dev"},
            "executor": {"default": "cursor"},
        },
    )
    _write_yaml(
        tmp_path / "configs" / "beta.yaml",
        {
            "project": {"mode": "scaffold"},
            "repo": {"owner": "acme", "name": "beta"},
        },
    )
    previews = discover_existing_configs(tmp_path)
    names = [p.path.name for p in previews]
    assert names == ["alpha.yaml", "beta.yaml"]
    alpha = previews[0]
    assert alpha.repo_slug == "acme/alpha"
    assert alpha.base_branch == "dev"
    assert alpha.executor_default == "cursor"
    assert alpha.project_mode == "existing"


def test_discover_returns_empty_when_no_configs(tmp_path: Path) -> None:
    assert discover_existing_configs(tmp_path) == []


def test_discover_skips_unparseable_yaml(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "broken.yaml").write_text(": : : not yaml")
    _write_yaml(cfg_dir / "good.yaml", {"repo": {"owner": "acme", "name": "x"}})
    previews = discover_existing_configs(tmp_path)
    assert [p.path.name for p in previews] == ["good.yaml"]


class _ScriptedIO:
    """Minimal :class:`WizardIO` impl that pulls answers from a list."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.info_log: list[str] = []
        self.warn_log: list[str] = []

    def ask(self, prompt: str, *, default: str = "", choices=None) -> str:
        if not self._answers:
            raise AssertionError(f"out of scripted answers at {prompt!r}")
        v = self._answers.pop(0)
        return v if v != "" else default

    def ask_bool(self, prompt: str, *, default: bool = False) -> bool:
        if not self._answers:
            raise AssertionError("out of scripted bool answers")
        v = self._answers.pop(0)
        return bool(v) if v != "" else default

    def info(self, msg: str) -> None:
        self.info_log.append(msg)

    def warn(self, msg: str) -> None:
        self.warn_log.append(msg)


def test_choose_strategy_returns_wizard_when_no_overlays(tmp_path: Path) -> None:
    io = _ScriptedIO([])
    choice = choose_config_strategy(io, project_root=tmp_path)
    assert choice == ConfigChoice(strategy="wizard")
    assert any("running the wizard" in m for m in io.info_log)


def test_choose_strategy_use_existing_by_number(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "configs" / "alpha.yaml",
        {"repo": {"owner": "acme", "name": "alpha"}},
    )
    _write_yaml(
        tmp_path / "configs" / "beta.yaml",
        {"repo": {"owner": "acme", "name": "beta"}},
    )
    io = _ScriptedIO(["2"])
    choice = choose_config_strategy(io, project_root=tmp_path)
    assert choice.strategy == "use_existing"
    assert choice.source_path is not None
    assert choice.source_path.name == "beta.yaml"


def test_choose_strategy_w_runs_wizard_even_with_overlays(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "configs" / "alpha.yaml", {"repo": {"owner": "a", "name": "b"}}
    )
    io = _ScriptedIO(["w"])
    choice = choose_config_strategy(io, project_root=tmp_path)
    assert choice.strategy == "wizard"


def test_choose_strategy_clone_path(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "configs" / "example-foo.yaml",
        {"repo": {"owner": "acme", "name": "foo"}},
    )
    io = _ScriptedIO(["c", ""])  # c = clone, then accept default new name
    choice = choose_config_strategy(io, project_root=tmp_path)
    assert choice.strategy == "clone_and_edit"
    assert choice.source_path is not None
    assert choice.source_path.name == "example-foo.yaml"
    assert choice.new_name == "my-foo.yaml"


def test_choose_strategy_retries_on_garbage(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / "configs" / "alpha.yaml", {"repo": {"owner": "a", "name": "b"}}
    )
    io = _ScriptedIO(["banana", "9", "1"])
    choice = choose_config_strategy(io, project_root=tmp_path)
    assert choice.strategy == "use_existing"
    assert any("Unrecognized" in m or "try again" in m for m in io.info_log)


def test_clone_existing_yaml_copies_and_returns_dest(tmp_path: Path) -> None:
    src = tmp_path / "configs" / "src.yaml"
    _write_yaml(src, {"repo": {"owner": "x", "name": "y"}})
    dest = clone_existing_yaml(src, new_name="cloned.yaml", project_root=tmp_path)
    assert dest == (tmp_path / "configs" / "cloned.yaml").resolve()
    assert dest.exists()
    assert "owner: x" in dest.read_text()


def test_clone_existing_yaml_refuses_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "configs" / "src.yaml"
    _write_yaml(src, {"repo": {"owner": "x", "name": "y"}})
    clone_existing_yaml(src, new_name="cloned.yaml", project_root=tmp_path)
    with pytest.raises(FileExistsError):
        clone_existing_yaml(src, new_name="cloned.yaml", project_root=tmp_path)


def test_clone_existing_yaml_appends_yaml_suffix(tmp_path: Path) -> None:
    src = tmp_path / "configs" / "src.yaml"
    _write_yaml(src, {"a": 1})
    dest = clone_existing_yaml(src, new_name="no-suffix", project_root=tmp_path)
    assert dest.name == "no-suffix.yaml"
