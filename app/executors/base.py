"""Code-executor abstract interface and registry.

Design rules:
    1. The interface is the same for *every* coding agent we ever plug in.
    2. Executors don't talk to Temporal, LangGraph, GitHub, or the artifact
       store. They take a prompt + workspace, do their thing, and return a
       structured :class:`ExecutorResult`.
    3. Executors are instantiated by name through :func:`build_executor`. New
       backends are registered via :func:`register_executor` so user-installed
       plugins can extend the system without forking this repo.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.config.models import AppConfig, ExecutorEntry

ExecutorTaskKind = Literal["plan", "code", "test", "review", "report", "freeform"]


@dataclass
class ExecutorRequest:
    """Input to a code executor.

    Attributes:
        kind: high-level intent so executors can pick a system prompt.
        prompt: free-form task description / instructions.
        workspace: directory the executor is allowed to modify. ``None`` means
            "no workspace, just produce text" (useful for planner / reviewer).
        artifact_dir: writable directory for executor output (logs, plan files).
        context_files: optional files the executor should read before acting.
        env: extra environment variables exposed to the subprocess.
        metadata: pass-through dict for executor-specific extensions.
    """

    kind: ExecutorTaskKind
    prompt: str
    workspace: Path | None = None
    artifact_dir: Path | None = None
    context_files: list[Path] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutorResult:
    """Structured output from a code executor.

    Attributes:
        ok: True if the executor completed without process-level failure.
            Logical failures (LLM refused, tests failed) live inside ``output``
            and ``metadata``; tester nodes interpret them.
        output: textual answer / streamed thinking. Always set.
        diff: unified diff of files changed in ``workspace``, if any.
        changed_files: list of files modified in ``workspace`` (relative paths).
        exit_code: process exit code from the underlying CLI (0 for in-process
            executors).
        duration_seconds: wall time of the call.
        metadata: free-form dict for executor-specific data.
    """

    ok: bool
    output: str
    diff: str = ""
    changed_files: list[str] = field(default_factory=list)
    exit_code: int = 0
    duration_seconds: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class UnknownExecutorError(KeyError):
    """Raised when a requested executor is neither built-in nor registered."""


class CodeExecutor(ABC):
    """Abstract base. Subclasses must be cheap to construct (no I/O)."""

    name: str = ""

    def __init__(self, entry: ExecutorEntry) -> None:
        self.entry = entry

    @abstractmethod
    def run(self, request: ExecutorRequest) -> ExecutorResult:
        """Execute the request synchronously."""

    def healthcheck(self) -> tuple[bool, str]:
        """Return ``(ok, message)``. Default: enabled-only check."""
        if not self.entry.enabled:
            return False, f"executor '{self.name}' is disabled in config"
        return True, "ok"


_REGISTRY: dict[str, Callable[[ExecutorEntry], CodeExecutor]] = {}


def register_executor(
    name: str, factory: Callable[[ExecutorEntry], CodeExecutor]
) -> None:
    """Register a new executor backend.

    Idempotent: registering the same name twice replaces the prior factory so
    tests can override built-ins.
    """
    _REGISTRY[name] = factory


def build_executor(name: str, config: AppConfig) -> CodeExecutor:
    """Construct an executor by name from an :class:`AppConfig`."""
    if name not in _REGISTRY:
        raise UnknownExecutorError(
            f"Unknown executor {name!r}. Registered: {sorted(_REGISTRY)}"
        )
    entry = config.executor.entry(name)
    if not entry.enabled:
        raise UnknownExecutorError(
            f"Executor {name!r} is disabled in config (executor.{name}.enabled=false)."
        )
    return _REGISTRY[name](entry)


def known_executors() -> list[str]:
    return sorted(_REGISTRY)
