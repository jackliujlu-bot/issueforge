"""End-to-end test of one LangGraph round, using the StubExecutor.

This is the smoke test that proves Phase 1 wiring is alive: AppConfig →
ArtifactStore → executor → planner → reporter, with a real LangGraph
checkpointer.
"""

from __future__ import annotations

from pathlib import Path

from app.config import load_config
from app.langgraph_app.graph import AgentRoundInput, run_agent_round


def test_run_agent_round_phase1(tmp_path: Path) -> None:
    cfg = load_config()
    cfg.system.artifact_root = tmp_path / "runs"

    out = run_agent_round(
        config=cfg,
        round_input=AgentRoundInput(
            repo="acme/widget",
            issue_number=42,
            issue_title="Fix something",
            issue_body="The thing is broken because of reasons.",
            issue_url="https://example.test/issues/42",
        ),
    )

    assert out.final_status == "planning_done"
    assert "Plan" in out.pending_issue_comment
    plan_md = tmp_path / "runs" / "acme--widget--issue-42" / "planning" / "plan.md"
    assert plan_md.exists()
    handoff = tmp_path / "runs" / "acme--widget--issue-42" / "handoff.md"
    assert handoff.exists()
    assert "issue: #42" in handoff.read_text()
