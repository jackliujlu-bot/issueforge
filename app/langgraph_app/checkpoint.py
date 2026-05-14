"""Checkpoint backend factory.

Defaults to SQLite so a single-machine deploy survives restarts. Pluggable so a
production deployment can swap to Postgres or Redis without touching nodes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config.models import LangGraphConfig


def build_checkpointer(cfg: LangGraphConfig) -> Any:
    """Return a LangGraph-compatible checkpointer.

    The return type is ``Any`` because the LangGraph public API exposes several
    checkpointer base classes across versions. Callers pass the result straight
    into ``StateGraph.compile(checkpointer=...)``.
    """
    if cfg.checkpoint_backend == "memory":
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()

    if cfg.checkpoint_backend == "sqlite":
        import sqlite3

        db_path = Path(cfg.checkpoint_db).expanduser().resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError as exc:  # pragma: no cover - install hint
            raise ImportError(
                "langgraph-checkpoint-sqlite is required for sqlite backend. "
                "Install with `pip install langgraph-checkpoint-sqlite` or change "
                "langgraph.checkpoint_backend to 'memory'."
            ) from exc
        # `SqliteSaver.from_conn_string` is a contextmanager and would be unsafe
        # to use here (we need the saver to outlive this function); instantiate
        # directly so the connection's lifetime is tied to the saver / process.
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        return SqliteSaver(conn)

    raise ValueError(f"Unknown checkpoint backend: {cfg.checkpoint_backend!r}")


def thread_id_for(repo_slug: str, issue_number: int) -> str:
    """Stable LangGraph thread id so reruns reuse the same checkpoint history."""
    safe = repo_slug.replace("/", ":") if repo_slug else "repo"
    return f"{safe}:issue-{issue_number}"
