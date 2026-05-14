"""Docker sandbox runner.

Phase 3+ feature. Stub implementation; the interface is stable enough that
Coder/Tester nodes can program against it today.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


class DockerBackend(Protocol):
    def run(self, *, image: str, command: list[str], workdir: str) -> CommandResult: ...


class DockerRunner:
    def __init__(self, image: str = "", *, backend: DockerBackend | None = None) -> None:
        self.image = image
        self._backend = backend

    def run(self, command: list[str], workdir: str) -> CommandResult:
        if self._backend is None:
            raise NotImplementedError(
                "DockerRunner has no backend wired up; supply one or use sandbox.mode=local."
            )
        return self._backend.run(image=self.image, command=command, workdir=workdir)
