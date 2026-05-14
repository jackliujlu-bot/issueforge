"""Heuristic project detection.

Given a path to a (possibly empty) checkout, guess sensible defaults for
``commands.setup`` / ``commands.lint`` / ``commands.test`` / ``commands.build``
and the base branch. The wizard offers these as suggestions; the user is free
to override them.

The detection rules are intentionally conservative. We only emit a command if
we see direct evidence of the toolchain (``pyproject.toml`` for Python,
``package.json`` for Node, etc.) and we never combine languages we can't
verify will share a working directory.

Detection is offline and stateless. We don't shell out — we just read a few
filenames so this works on a fresh checkout without first running ``setup``.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CommandSuggestion:
    """One toolchain's worth of suggested commands."""

    toolchain: str  # e.g. "python-uv", "node-pnpm", "rust"
    setup: list[str] = field(default_factory=list)
    lint: list[str] = field(default_factory=list)
    test: list[str] = field(default_factory=list)
    build: list[str] = field(default_factory=list)


def detect_commands(repo_path: Path | str) -> CommandSuggestion:
    """Inspect ``repo_path`` and suggest setup/lint/test/build commands.

    Returns an empty ``CommandSuggestion(toolchain="unknown")`` if no toolchain
    can be identified (e.g. for ``mode=scaffold`` against an empty directory).
    Callers should treat the empty case as "ask the user to fill in manually".
    """
    path = Path(repo_path).expanduser()
    if not path.exists() or not path.is_dir():
        return CommandSuggestion(toolchain="unknown")

    # ---- Python ---------------------------------------------------------- #
    # Prefer uv if uv.lock is present; the dimos workflow plus most modern
    # Python projects already standardise on it.
    if (path / "pyproject.toml").exists():
        if (path / "uv.lock").exists() or _pyproject_uses(path, "uv"):
            return CommandSuggestion(
                toolchain="python-uv",
                setup=["uv sync"],
                lint=["uv run ruff check", "uv run ruff format --check"],
                test=["uv run pytest"],
                build=[],
            )
        if (path / "poetry.lock").exists() or _pyproject_uses(path, "poetry"):
            return CommandSuggestion(
                toolchain="python-poetry",
                setup=["poetry install"],
                lint=["poetry run ruff check"],
                test=["poetry run pytest"],
                build=[],
            )
        # Plain pyproject (pip / hatch / setuptools).
        return CommandSuggestion(
            toolchain="python-pip",
            setup=["pip install -e .[dev]"],
            lint=["ruff check"],
            test=["pytest"],
            build=[],
        )
    if (path / "requirements.txt").exists():
        return CommandSuggestion(
            toolchain="python-pip",
            setup=["pip install -r requirements.txt"],
            lint=["ruff check"],
            test=["pytest"],
            build=[],
        )

    # ---- Node ------------------------------------------------------------ #
    if (path / "pnpm-lock.yaml").exists():
        return CommandSuggestion(
            toolchain="node-pnpm",
            setup=["pnpm install --frozen-lockfile"],
            lint=["pnpm run lint"],
            test=["pnpm test"],
            build=["pnpm build"],
        )
    if (path / "yarn.lock").exists():
        return CommandSuggestion(
            toolchain="node-yarn",
            setup=["yarn install --frozen-lockfile"],
            lint=["yarn lint"],
            test=["yarn test"],
            build=["yarn build"],
        )
    if (path / "package-lock.json").exists() or (path / "package.json").exists():
        return CommandSuggestion(
            toolchain="node-npm",
            setup=["npm ci"],
            lint=["npm run lint"],
            test=["npm test"],
            build=["npm run build"],
        )

    # ---- Rust ------------------------------------------------------------ #
    if (path / "Cargo.toml").exists():
        return CommandSuggestion(
            toolchain="rust",
            setup=[],
            lint=["cargo clippy --all-targets -- -D warnings"],
            test=["cargo test"],
            build=["cargo build --release"],
        )

    # ---- Go -------------------------------------------------------------- #
    if (path / "go.mod").exists():
        return CommandSuggestion(
            toolchain="go",
            setup=["go mod download"],
            lint=["go vet ./..."],
            test=["go test ./..."],
            build=["go build ./..."],
        )

    # ---- Make catch-all -------------------------------------------------- #
    if (path / "Makefile").exists():
        return CommandSuggestion(
            toolchain="make",
            setup=["make install"],
            lint=["make lint"],
            test=["make test"],
            build=["make build"],
        )

    return CommandSuggestion(toolchain="unknown")


def _pyproject_uses(repo_path: Path, marker: str) -> bool:
    """Coarse check: does pyproject.toml mention a build/lock marker tool name?

    We avoid importing tomllib on the 3.10 path; this is purely substring.
    """
    try:
        text = (repo_path / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    needle = marker.lower()
    return any(needle in line.lower() for line in text.splitlines())


def detect_base_branch(repo_path: Path | str) -> str:
    """Return the default branch of a local git checkout, or ``""`` on failure.

    Prefers ``origin/HEAD`` (matches what GitHub considers default) and falls
    back to the currently checked-out branch.
    """
    path = Path(repo_path).expanduser()
    if not (path / ".git").exists():
        return ""

    # `git symbolic-ref refs/remotes/origin/HEAD` returns "refs/remotes/origin/main".
    rc, out, _ = _git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and out.strip().startswith("origin/"):
        return out.strip().removeprefix("origin/")

    rc, out, _ = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if rc == 0 and out.strip() and out.strip() != "HEAD":
        return out.strip()
    return ""


def detect_repo_slug_from_remote(repo_path: Path | str) -> str:
    """Return ``owner/name`` parsed from ``origin``'s URL, or ``""``."""
    path = Path(repo_path).expanduser()
    if not (path / ".git").exists():
        return ""
    rc, out, _ = _git(path, "remote", "get-url", "origin")
    if rc != 0:
        return ""
    return _parse_github_slug(out.strip())


def _parse_github_slug(url: str) -> str:
    """Convert ``git@github.com:acme/widget.git`` or ``https://github.com/acme/widget``
    into ``acme/widget``. Returns empty string on unrecognised URLs."""
    if not url:
        return ""
    cleaned = url.strip()
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    if cleaned.startswith("git@"):
        # git@github.com:acme/widget
        _, _, tail = cleaned.partition(":")
    elif "://" in cleaned:
        # https://github.com/acme/widget or ssh://git@github.com/acme/widget
        _, _, tail = cleaned.partition("//")
        _, _, tail = tail.partition("/")
    else:
        tail = cleaned
    parts = [p for p in tail.split("/") if p]
    if len(parts) < 2:
        return ""
    return f"{parts[-2]}/{parts[-1]}"


def detect_repo_from_gh_dir(cwd: Path | str) -> str:
    """If ``gh`` is logged in and ``cwd`` is inside a GitHub-tracked repo,
    return ``owner/name``. Otherwise empty string."""
    try:
        completed = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    try:
        data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return ""
    return str(data.get("nameWithOwner", ""))


def _git(cwd: Path, *args: str) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127, "", ""
    return completed.returncode, completed.stdout, completed.stderr
