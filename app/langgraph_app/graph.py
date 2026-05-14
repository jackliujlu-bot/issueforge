"""LangGraph graph builder + single-round runner.

The graph is intentionally minimal in Phase 1::

    load_context -> planner -> reporter -> END

Future phases extend it::

    load_context -> planner -> coder -> tester -> reviewer -> reporter -> END
                                       ^         |
                                       +-- retry +
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.graph import END, StateGraph

from app.config.models import AppConfig
from app.executors.base import build_executor
from app.langgraph_app.checkpoint import build_checkpointer, thread_id_for
from app.langgraph_app.nodes import (
    NodeContext,
    coder_node,
    deliverer_node,
    planner_node,
    reporter_node,
    reviewer_node,
    tester_node,
)
from app.langgraph_app.state import AgentState, make_initial_state
from app.observability import get_logger
from app.sandbox.artifact_store import ArtifactStore
from app.sandbox.worktree import GitWorktreeBackend, WorktreeManager

log = get_logger(__name__)


@dataclass
class AgentRoundInput:
    repo: str
    issue_number: int
    issue_title: str
    issue_body: str
    issue_url: str = ""


@dataclass
class AgentRoundOutput:
    state: AgentState
    pending_issue_comment: str
    final_status: str


def _load_context_node(ctx: NodeContext):
    """First node: copies the round input into the agent state."""

    def _node(state: AgentState) -> dict:
        return {
            "current_step": "context_loaded",
            "executor": ctx.executor.name,
        }

    return _node


def _retries_exhausted(state: AgentState) -> bool:
    return state.get("retry_count", 0) >= state.get("max_retries", 0)


def _route_after_tester(state: AgentState) -> str:
    """Phase 2 control flow: testing failure routes back to coder or to reporter."""
    if state.get("local_test_status") == "fail":
        return "reporter" if _retries_exhausted(state) else "coder"
    return "reviewer"


def _route_after_reviewer(state: AgentState) -> str:
    """Decide what happens after a self-review verdict.

    Returns ``"coder"`` (retry), ``"deliverer"`` (ship it), or ``"reporter"``
    (bail without shipping). Phase 2 (`stop_after=review`) doesn't have a
    deliverer node so the graph maps ``"deliverer"`` to ``"reporter"``
    transparently.
    """
    verdict = (state.get("scratch") or {}).get("review_verdict", "pass").lower()
    if verdict == "fail":
        return "reporter" if _retries_exhausted(state) else "coder"
    return "deliverer"


def build_graph(ctx: NodeContext) -> Any:
    """Build the StateGraph.

    The graph topology depends on ``workflow.stop_after``:

    - ``planning``: load_context → planner → reporter → END (Phase 1)
    - ``coding``:   ... → coder → reporter → END  (no retry — diagnostic only)
    - ``testing``:  ... → coder → tester → {coder | reviewer | reporter}, with retry
    - ``review``:   ... → coder → tester → reviewer → {coder | reporter}, with retry
    - ``done``:     same as ``review`` until Phase 3 plugs in PR creation.

    Retry edges only fire when ``retry_count < max_retries``. Once exhausted the
    failing node routes to reporter, which marks ``final_status=failed``.
    """
    g: StateGraph = StateGraph(AgentState)

    g.add_node("load_context", _load_context_node(ctx))
    g.add_node("planner", planner_node(ctx))
    g.add_node("coder", coder_node(ctx))
    g.add_node("tester", tester_node(ctx))
    g.add_node("reviewer", reviewer_node(ctx))
    g.add_node("deliverer", deliverer_node(ctx))
    g.add_node("reporter", reporter_node(ctx))

    g.set_entry_point("load_context")
    g.add_edge("load_context", "planner")

    stop_after = ctx.config.workflow.stop_after
    if stop_after == "planning":
        g.add_edge("planner", "reporter")
    elif stop_after == "coding":
        g.add_edge("planner", "coder")
        g.add_edge("coder", "reporter")
    elif stop_after == "testing":
        g.add_edge("planner", "coder")
        g.add_edge("coder", "tester")
        g.add_conditional_edges(
            "tester",
            _route_after_tester,
            {"coder": "coder", "reviewer": "reporter", "reporter": "reporter"},
        )
    elif stop_after == "review":
        g.add_edge("planner", "coder")
        g.add_edge("coder", "tester")
        g.add_conditional_edges(
            "tester",
            _route_after_tester,
            {"coder": "coder", "reviewer": "reviewer", "reporter": "reporter"},
        )
        # No deliverer in this mode: collapse "deliverer" → "reporter".
        g.add_conditional_edges(
            "reviewer",
            _route_after_reviewer,
            {"coder": "coder", "deliverer": "reporter", "reporter": "reporter"},
        )
    elif stop_after == "done":
        # Phase 3: include deliverer between reviewer and reporter when review passes.
        g.add_edge("planner", "coder")
        g.add_edge("coder", "tester")
        g.add_conditional_edges(
            "tester",
            _route_after_tester,
            {"coder": "coder", "reviewer": "reviewer", "reporter": "reporter"},
        )
        g.add_conditional_edges(
            "reviewer",
            _route_after_reviewer,
            {"coder": "coder", "deliverer": "deliverer", "reporter": "reporter"},
        )
        g.add_edge("deliverer", "reporter")

    g.add_edge("reporter", END)

    checkpointer = build_checkpointer(ctx.config.langgraph)
    return g.compile(checkpointer=checkpointer)


def _build_worktree_manager(config: AppConfig) -> WorktreeManager | None:
    """Return a manager appropriate for ``config.sandbox.mode``.

    - ``local``  : no worktree (Phase 1; planner-only flows). Returns None.
    - ``worktree``: real GitWorktreeBackend rooted at ``sandbox.worktree_root``,
      sourced from ``repo.local_path``.
    - ``docker`` : not implemented yet (Phase 3). Returns None for now so
      callers can decide what to do.
    """
    if config.sandbox.mode != "worktree":
        return None
    if not config.repo.local_path:
        raise ValueError(
            "sandbox.mode=worktree requires repo.local_path to point at an "
            "existing checkout of the business repo."
        )
    backend = GitWorktreeBackend(
        Path(config.repo.local_path),
        preferred_remote=config.repo.push_remote,
        auto_fetch_base=config.repo.auto_fetch_base,
    )
    return WorktreeManager(config.sandbox.worktree_root, backend=backend)


def _build_shell_executor(config: AppConfig):  # type: ignore[no-untyped-def]
    """Tester always runs configured commands through the shell executor."""
    from app.executors.base import build_executor as _build

    if not config.executor.shell.enabled:
        return None
    return _build("shell", config)


def run_agent_round(
    *,
    config: AppConfig,
    round_input: AgentRoundInput,
) -> AgentRoundOutput:
    """One synchronous LangGraph round.

    Called from the Temporal activity. Each call resumes the same thread_id, so
    repeated invocations against the same issue accumulate history.
    """
    artifact_root = config.system.artifact_root
    artifacts = ArtifactStore(artifact_root)
    run_key = ArtifactStore.issue_key(round_input.repo, round_input.issue_number)
    run_dir = artifacts.run_dir(run_key)

    artifacts.write_text(
        run_dir.issue_md,
        f"# {round_input.issue_title}\n\n{round_input.issue_body or '(empty)'}\n",
    )

    executor = build_executor(config.executor.default, config)
    worktree = _build_worktree_manager(config)
    shell = _build_shell_executor(config)
    ctx = NodeContext(
        config=config,
        executor=executor,
        artifacts=artifacts,
        run_dir=run_dir,
        worktree=worktree,
        shell=shell,
    )
    graph = build_graph(ctx)

    initial_state = make_initial_state(
        repo=round_input.repo,
        issue_number=round_input.issue_number,
        issue_title=round_input.issue_title,
        issue_body=round_input.issue_body,
        issue_url=round_input.issue_url,
        artifact_dir=str(run_dir.root),
        executor=config.executor.default,
        max_retries=config.workflow.max_retries,
    )

    thread_id = thread_id_for(round_input.repo, round_input.issue_number)
    result_state: AgentState = graph.invoke(
        initial_state, config={"configurable": {"thread_id": thread_id}}
    )

    log.info(
        "graph.round_complete",
        issue=round_input.issue_number,
        status=result_state.get("final_status"),
        thread_id=thread_id,
    )

    return AgentRoundOutput(
        state=result_state,
        pending_issue_comment=result_state.get("pending_issue_comment", ""),
        final_status=result_state.get("final_status", "running"),
    )
