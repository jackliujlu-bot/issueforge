"""Shared subprocess helpers for CLI-backed executors.

Centralises:
    - placeholder substitution in ``args_template``
    - capture of stdout/stderr with timeout
    - per-call git diff snapshot to populate ``ExecutorResult.diff`` and
      ``changed_files`` even for executors that don't emit a diff themselves.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from app.executors.base import ExecutorRequest, ExecutorResult


def substitute_args(
    template: Sequence[str],
    *,
    prompt: str,
    workspace: Path | None,
    model: str,
    extra: dict[str, str] | None = None,
) -> list[str]:
    extra = extra or {}
    mapping = {
        "prompt": prompt,
        "workspace": str(workspace) if workspace else "",
        "model": model,
        **extra,
    }
    out: list[str] = []
    for token in template:
        try:
            out.append(token.format(**mapping))
        except KeyError as exc:
            raise ValueError(
                f"args_template references unknown placeholder {{{exc.args[0]}}}: {token!r}"
            ) from exc
    return out


def run_cli(
    *,
    command: str,
    args: list[str],
    cwd: Path | None,
    env: dict[str, str] | None,
    timeout: int,
    input_text: str | None = None,
) -> tuple[int, str, str, float]:
    if shutil.which(command) is None and not Path(command).exists():
        return 127, "", f"executable not found on PATH: {command}", 0.0

    started = time.monotonic()
    try:
        completed = subprocess.run(
            [command, *args],
            cwd=str(cwd) if cwd else None,
            env=_merged_env(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        return (
            124,
            (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            f"timeout after {timeout}s",
            elapsed,
        )
    elapsed = time.monotonic() - started
    return completed.returncode, completed.stdout, completed.stderr, elapsed


def _merged_env(extra: dict[str, str] | None) -> dict[str, str] | None:
    if not extra:
        return None
    import os

    base = dict(os.environ)
    base.update(extra)
    return base


def collect_git_changes(workspace: Path | None) -> tuple[str, list[str]]:
    """Return (unified_diff, list_of_changed_files) for ``workspace``.

    The diff is computed against ``HEAD`` so we capture the *real* working-tree
    delta even if a CLI tool (notably ``cursor-agent``) left the index in an
    exotic state. The steps:

    1. ``git reset --mixed HEAD`` — discard any staged changes so the index
       reflects HEAD. The working tree is untouched.
    2. ``git add -N .`` — mark untracked files as intent-to-add so they appear
       in ``git diff`` with their full content.
    3. ``git diff HEAD --no-color`` — emit the unified diff.

    This mutates the throwaway worktree's index, never the user's checkout.
    Safe to call on a non-git workspace (returns empty).
    """
    if workspace is None or not (workspace / ".git").exists():
        return "", []
    try:
        subprocess.run(
            ["git", "reset", "--mixed", "-q", "HEAD"],
            cwd=str(workspace),
            capture_output=True,
            timeout=30,
            check=False,
        )
        subprocess.run(
            ["git", "add", "-N", "."],
            cwd=str(workspace),
            capture_output=True,
            timeout=30,
            check=False,
        )
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--no-color"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        files = subprocess.run(
            ["git", "diff", "HEAD", "--name-only"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "", []
    return diff.stdout, [f for f in files.stdout.splitlines() if f]


def standard_result(
    request: ExecutorRequest,
    *,
    exit_code: int,
    stdout: str,
    stderr: str,
    duration: float,
    extra_metadata: dict[str, object] | None = None,
) -> ExecutorResult:
    """Pack subprocess output into an :class:`ExecutorResult`."""
    diff, changed = collect_git_changes(request.workspace)
    metadata: dict[str, object] = {"stderr": stderr}
    if extra_metadata:
        metadata.update(extra_metadata)
    return ExecutorResult(
        ok=exit_code == 0,
        output=stdout,
        diff=diff,
        changed_files=changed,
        exit_code=exit_code,
        duration_seconds=round(duration, 3),
        metadata=metadata,
    )
