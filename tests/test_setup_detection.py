"""Tests for app.setup.detection — the toolchain auto-suggester."""

from __future__ import annotations

from pathlib import Path

from app.setup.detection import (
    _parse_github_slug,
    detect_base_branch,
    detect_commands,
    detect_repo_slug_from_remote,
)


def test_detect_unknown_for_empty_dir(tmp_path: Path) -> None:
    suggestion = detect_commands(tmp_path)
    assert suggestion.toolchain == "unknown"
    assert suggestion.test == []


def test_detect_python_uv(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "uv.lock").write_text("")
    suggestion = detect_commands(tmp_path)
    assert suggestion.toolchain == "python-uv"
    assert "uv sync" in suggestion.setup
    assert "uv run pytest" in suggestion.test


def test_detect_python_pip_when_no_lock(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    suggestion = detect_commands(tmp_path)
    assert suggestion.toolchain == "python-pip"
    assert suggestion.setup == ["pip install -e .[dev]"]


def test_detect_node_pnpm(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "pnpm-lock.yaml").write_text("")
    suggestion = detect_commands(tmp_path)
    assert suggestion.toolchain == "node-pnpm"
    assert suggestion.setup == ["pnpm install --frozen-lockfile"]


def test_detect_rust(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\nversion='0'\n")
    suggestion = detect_commands(tmp_path)
    assert suggestion.toolchain == "rust"
    assert suggestion.test == ["cargo test"]


def test_detect_go(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    suggestion = detect_commands(tmp_path)
    assert suggestion.toolchain == "go"
    assert "go test ./..." in suggestion.test


def test_detect_makefile_fallback(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\techo hi\n")
    suggestion = detect_commands(tmp_path)
    assert suggestion.toolchain == "make"


def test_detect_base_branch_returns_empty_for_non_git(tmp_path: Path) -> None:
    assert detect_base_branch(tmp_path) == ""


def test_detect_repo_slug_returns_empty_for_non_git(tmp_path: Path) -> None:
    assert detect_repo_slug_from_remote(tmp_path) == ""


def test_parse_github_slug_ssh() -> None:
    assert _parse_github_slug("git@github.com:acme/widget.git") == "acme/widget"


def test_parse_github_slug_https() -> None:
    assert _parse_github_slug("https://github.com/acme/widget") == "acme/widget"


def test_parse_github_slug_https_with_dotgit() -> None:
    assert _parse_github_slug("https://github.com/acme/widget.git") == "acme/widget"


def test_parse_github_slug_invalid_returns_empty() -> None:
    assert _parse_github_slug("") == ""
    assert _parse_github_slug("not-a-url") == ""
