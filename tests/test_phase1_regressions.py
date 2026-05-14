"""Regression tests for Phase 1 verification.

Two bugs were observed during the first end-to-end Phase 1 run and are now
covered here so they cannot regress silently:

1. ``build_checkpointer`` used to call ``SqliteSaver.from_conn_string()`` and
   return the raw context manager, which made ``StateGraph.compile()`` blow up
   with ``TypeError: Invalid checkpointer provided``. The shared conftest forces
   the memory backend on every other test, so the sqlite path was effectively
   uncovered.

2. The reporter wrote ``handoff.md`` before deciding the final ``final_status``,
   so the on-disk snapshot reported ``running`` while the node returned
   ``planning_done``. Anyone reading ``handoff.md`` after a Phase 1 round saw
   a status that contradicted the workflow result.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.config import load_config
from app.config.models import LangGraphConfig
from app.langgraph_app.checkpoint import build_checkpointer
from app.langgraph_app.graph import AgentRoundInput, run_agent_round


def test_sqlite_checkpointer_returns_a_saver_instance(tmp_path: Path) -> None:
    """``build_checkpointer`` must hand back an entered saver, not a contextmanager.

    LangGraph's ``StateGraph.compile`` accepts only ``BaseCheckpointSaver``,
    ``True``/``False``, or ``None``; a raw ``_GeneratorContextManager`` (which
    is what ``SqliteSaver.from_conn_string`` returns) breaks compilation.
    """
    db_path = tmp_path / "checkpoints" / "regression.sqlite"
    cfg = LangGraphConfig(checkpoint_backend="sqlite", checkpoint_db=db_path)

    saver = build_checkpointer(cfg)

    assert isinstance(saver, BaseCheckpointSaver), (
        f"Expected a BaseCheckpointSaver subclass, got {type(saver).__name__}. "
        "Likely regressed back to returning SqliteSaver.from_conn_string() "
        "without entering the contextmanager."
    )
    assert db_path.parent.exists(), "checkpoint dir should be created"


def test_run_agent_round_with_sqlite_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full Phase 1 round must succeed against the sqlite backend.

    The shared conftest fixture pins the backend to ``memory``; this test opts
    back into sqlite so the previously-broken code path is exercised. If
    ``build_checkpointer`` ever regresses to returning a contextmanager,
    ``StateGraph.compile`` raises ``TypeError`` here.
    """
    monkeypatch.setenv("AGENT_WORKER__LANGGRAPH__CHECKPOINT_BACKEND", "sqlite")
    monkeypatch.setenv(
        "LANGGRAPH_CHECKPOINT_DB",
        str(tmp_path / "checkpoints" / "sqlite-roundtrip.sqlite"),
    )
    from app.config import reset_cached_config

    reset_cached_config()
    cfg = load_config()
    cfg.system.artifact_root = tmp_path / "runs"

    out = run_agent_round(
        config=cfg,
        round_input=AgentRoundInput(
            repo="acme/widget",
            issue_number=7,
            issue_title="Phase 1 sqlite round",
            issue_body="Exercise the sqlite checkpointer end-to-end.",
        ),
    )

    assert out.final_status == "planning_done"
    db_file = tmp_path / "checkpoints" / "sqlite-roundtrip.sqlite"
    assert db_file.exists(), "sqlite checkpoint db must be written"
    assert db_file.stat().st_size > 0, "sqlite checkpoint db must be non-empty"


def test_handoff_md_records_the_final_status_returned_by_reporter(
    tmp_path: Path,
) -> None:
    """``handoff.md`` on disk must agree with ``AgentRoundOutput.final_status``.

    The reporter computes ``final_status=planning_done`` for the Phase 1
    ``stop_after=planning`` mode; the on-disk handoff snapshot used to capture
    the pre-reporter state (``running``), confusing anyone (or any agent)
    trying to resume from artifacts.
    """
    cfg = load_config()
    cfg.system.artifact_root = tmp_path / "runs"

    out = run_agent_round(
        config=cfg,
        round_input=AgentRoundInput(
            repo="acme/widget",
            issue_number=99,
            issue_title="Handoff consistency",
            issue_body="Body irrelevant for this assertion.",
        ),
    )

    handoff = (
        tmp_path / "runs" / "acme--widget--issue-99" / "handoff.md"
    ).read_text()

    assert out.final_status == "planning_done"
    assert f"- final_status: {out.final_status}" in handoff, (
        "handoff.md must record the same final_status the reporter returned. "
        "If you see 'final_status: running' here, the reporter is writing "
        "handoff before computing the final status."
    )
