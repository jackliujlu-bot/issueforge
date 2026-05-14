"""Thin wrapper around the ``gh`` CLI.

Using ``gh`` instead of a Python GitHub SDK keeps deployment simple: the user's
existing ``gh auth login`` credentials work everywhere, and the same code path
runs on a developer laptop and inside CI.

Every call accepts ``check=True`` semantics by default; a non-zero exit raises
:class:`GhCommandError` with stdout/stderr captured.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass
class GhCommandError(RuntimeError):
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"`{' '.join(self.cmd)}` failed with code {self.returncode}\n"
            f"stdout:\n{self.stdout}\n"
            f"stderr:\n{self.stderr}"
        )


class GhClient:
    def __init__(self, binary: str = "gh") -> None:
        self.binary = binary

    def run(
        self,
        *args: str,
        input_text: str | None = None,
        timeout: float | None = 60.0,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [self.binary, *args]
        try:
            return subprocess.run(
                cmd,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GhCommandError(cmd=cmd, returncode=127, stdout="", stderr=str(exc)) from exc

    def run_checked(
        self,
        *args: str,
        input_text: str | None = None,
        timeout: float | None = 60.0,
    ) -> str:
        completed = self.run(*args, input_text=input_text, timeout=timeout)
        if completed.returncode != 0:
            raise GhCommandError(
                cmd=[self.binary, *args],
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        return completed.stdout

    def run_json(
        self,
        *args: str,
        timeout: float | None = 60.0,
    ) -> Any:
        out = self.run_checked(*args, timeout=timeout)
        if not out.strip():
            return None
        return json.loads(out)
