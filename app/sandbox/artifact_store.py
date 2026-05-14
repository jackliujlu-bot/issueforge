"""Per-issue artifact directory layout.

This is the on-disk projection of an agent run, mirroring the layout described
in the architecture doc::

    runs/
    └── issue-<issue_number>/
        ├── input/         issue.md, repo_snapshot.txt
        ├── planning/      plan.md, todo.md, assumptions.md
        ├── execution/     commands.log, tool_calls.jsonl, changed_files.txt
        ├── evidence/      local_tests.log, ci_logs.md
        ├── review/        self_review.md, risk_report.md
        └── handoff.md

The store is intentionally filesystem-only: any worker, on any machine that
mounts the same volume, can pick up where another left off. This is the third
layer of the recovery story (Temporal history + LangGraph checkpoint + on-disk
artifacts).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class IssueRunDir:
    """Filesystem locations for a single issue run."""

    root: Path

    @property
    def input_dir(self) -> Path:
        return self.root / "input"

    @property
    def planning_dir(self) -> Path:
        return self.root / "planning"

    @property
    def execution_dir(self) -> Path:
        return self.root / "execution"

    @property
    def evidence_dir(self) -> Path:
        return self.root / "evidence"

    @property
    def review_dir(self) -> Path:
        return self.root / "review"

    @property
    def issue_md(self) -> Path:
        return self.input_dir / "issue.md"

    @property
    def plan_md(self) -> Path:
        return self.planning_dir / "plan.md"

    @property
    def todo_md(self) -> Path:
        return self.planning_dir / "todo.md"

    @property
    def assumptions_md(self) -> Path:
        return self.planning_dir / "assumptions.md"

    @property
    def commands_log(self) -> Path:
        return self.execution_dir / "commands.log"

    @property
    def tool_calls_jsonl(self) -> Path:
        return self.execution_dir / "tool_calls.jsonl"

    @property
    def handoff_md(self) -> Path:
        return self.root / "handoff.md"


class ArtifactStore:
    """Manages ``runs/<key>/`` directories.

    The store does not own concurrency control. Each agent run is keyed by a
    stable string (typically ``issue-<n>`` or ``<owner>--<repo>--issue-<n>``) so
    a restarted worker reuses the same directory.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, key: str) -> IssueRunDir:
        safe_key = key.replace("/", "--").strip()
        if not safe_key:
            raise ValueError("artifact key must be non-empty")
        d = IssueRunDir(self.root / safe_key)
        for sub in (d.input_dir, d.planning_dir, d.execution_dir, d.evidence_dir, d.review_dir):
            sub.mkdir(parents=True, exist_ok=True)
        return d

    def write_text(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def append_log(self, path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] {line.rstrip()}\n")

    @staticmethod
    def issue_key(repo_slug: str, issue_number: int) -> str:
        slug = repo_slug.replace("/", "--") if repo_slug else "repo"
        return f"{slug}--issue-{issue_number}"
