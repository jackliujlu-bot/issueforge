"""Shared per-round context for graph nodes.

We thread a :class:`NodeContext` through every node so they can reach the
config, the artifact store, and an executor *without* importing them directly.
This is what makes nodes unit-testable: in tests we pass a context built from
fakes.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config.models import AppConfig
from app.executors.base import CodeExecutor
from app.sandbox.artifact_store import ArtifactStore, IssueRunDir
from app.sandbox.worktree import WorktreeManager


@dataclass
class NodeContext:
    config: AppConfig
    executor: CodeExecutor
    artifacts: ArtifactStore
    run_dir: IssueRunDir
    # ``worktree`` is None in sandbox.mode=local (Phase 1 default). Phase 2
    # nodes that need an isolated checkout must assert it before using.
    worktree: WorktreeManager | None = None
    # The shell executor is used by the tester node to run configured commands.
    # Kept separate from the primary ``executor`` so swapping the coder
    # backend (cursor → claude_code) doesn't change how tests are invoked.
    shell: CodeExecutor | None = None
