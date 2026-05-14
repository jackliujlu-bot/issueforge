"""Planner node.

Responsibilities:
    - Read the issue title/body from state.
    - Ask the executor for a structured plan + subtasks.
    - Persist plan.md / todo.md into the artifact run dir.
    - Update state with plan, todo, subtasks, assumptions.

The planner does not write to GitHub directly; the reporter node aggregates
the comment payload so we have one place to control external side effects.
"""

from __future__ import annotations

from collections.abc import Callable
from textwrap import dedent

from app.executors.base import ExecutorRequest
from app.langgraph_app.nodes.context import NodeContext
from app.langgraph_app.state import AgentState
from app.observability import get_logger

log = get_logger(__name__)

PLAN_PROMPT_TEMPLATE = dedent(
    """\
    You are the **Planner** for a long-running coding agent. Your job is to take a
    GitHub issue and produce an actionable plan that the **Coder** agent can execute.

    ## Repository
    {repo}

    ## Issue #{issue_number} — {issue_title}

    {issue_body}

    ## Scope discipline (MANDATORY — top reason these plans fail review)

    The Coder, Reviewer and CI will all hold the resulting PR to **what this
    issue literally asks for**. Therefore:

    - Every entry in `## Subtasks` MUST be **directly required** to satisfy
      the issue body above. If you cannot point at a sentence in the issue
      that motivates a subtask, it does not belong in `## Subtasks`.
    - Tempting but out-of-scope work — fixing pre-existing CI flakes,
      refactors, performance wins, doc rewrites, lint debt, dependency bumps,
      modernisation — goes in the separate `## Follow-ups` section. The
      Coder will NOT execute those, and the Reviewer will NOT block PASS on
      them. They are notes for human triage / future issues.
    - If the issue is open-ended ("list X", "find Y", "investigate Z"), the
      single in-scope subtask is usually "produce the requested artifact"
      (a doc, a comment, a small spike). Do not invent supporting code or
      CI work just because you can think of some.
    - Do not include "fix CI" / "make tests pass" / "fix lint" in
      `## Subtasks` unless the issue body explicitly asks for that.

    ## Fact discipline (MANDATORY — second reason these plans fail review)

    Anything you write here gets copied into the Coder's prompt and often
    paraphrased into the PR / docs. So:

    - Do **not** assert specific filenames, pytest marker names, CI job
      names, script flags, env-var names, dependency versions, or function
      signatures unless you have just verified them in {workspace_hint}.
    - When you reference a path or command you are not 100% sure of, write
      it as `(verify in workspace)` rather than guessing. The Coder's
      fact-checking pass will fill it in.
    - The Reviewer cross-checks every concrete claim against the actual
      worktree the Coder uses; invented facts are the most expensive
      failure mode of this loop.

    ## Output requirements

    Reply with **Markdown** that contains, in order:

    1. A short `## Objective` paragraph (1-3 sentences). State the deliverable
       in the issue's own terms; do not silently expand the ask.
    2. A bullet list `## Assumptions` (≤5 items, each one line).
    3. A bullet list `## Subtasks` where each item has the form:
       - `T<n>`: <title> — type=<analysis|code|test|docs|review>; depends_on=[T..]; risk=<low|medium|high>
       Only in-scope tasks belong here (see "Scope discipline" above).
    4. A bullet list `## Verification` of concrete shell commands to run.
       List ONLY commands that already exist in this repo (project's
       configured `commands.test` / `commands.lint`, or commands you have
       verified by reading the workspace). Do not invent command names.
    5. A bullet list `## Follow-ups` (optional — omit the heading if empty)
       for out-of-scope ideas the issue does NOT ask for. Each item: one
       line, prefixed with `(out of scope)`. The Coder will skip these.
    6. A short `## Open Questions` list (omit the heading if there are none).

    Keep the response under 350 lines. Do **not** include code edits in this turn.
    """
)


def planner_node(ctx: NodeContext) -> PlannerCallable:
    return _make_planner(ctx)


def _make_planner(ctx: NodeContext) -> PlannerCallable:
    def _node(state: AgentState) -> dict:
        # Planner runs read-only over the *base* checkout so cursor / claude_code
        # can actually grep through the repo when forming the plan. The coder
        # node uses an isolated worktree instead — planner never mutates files.
        from pathlib import Path

        workspace = None
        if ctx.config.repo.local_path:
            local = Path(ctx.config.repo.local_path).expanduser()
            if local.exists():
                workspace = local

        prompt = PLAN_PROMPT_TEMPLATE.format(
            repo=state.get("repo", ""),
            issue_number=state.get("issue_number", -1),
            issue_title=state.get("issue_title", ""),
            issue_body=state.get("issue_body", "") or "(empty issue body)",
            workspace_hint=str(workspace) if workspace else "the repository checkout you can read",
        )

        log.info(
            "planner.start",
            issue=state.get("issue_number"),
            executor=ctx.executor.name,
            workspace=str(workspace) if workspace else "(none)",
        )

        result = ctx.executor.run(
            ExecutorRequest(
                kind="plan",
                prompt=prompt,
                workspace=workspace,
                artifact_dir=ctx.run_dir.planning_dir,
                metadata={"issue_number": state.get("issue_number")},
            )
        )

        plan_text = result.output.strip() or "_Planner returned no output._"

        ctx.artifacts.write_text(ctx.run_dir.plan_md, plan_text)
        ctx.artifacts.write_text(
            ctx.run_dir.todo_md,
            _extract_todo(plan_text),
        )

        ctx.artifacts.append_jsonl(
            ctx.run_dir.tool_calls_jsonl,
            {
                "node": "planner",
                "executor": ctx.executor.name,
                "ok": result.ok,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
            },
        )

        update: dict = {
            "plan": plan_text,
            "todo": _extract_todo_lines(plan_text),
            "current_step": "planner_done",
            "last_executor_output": plan_text[:5000],
        }
        if not result.ok:
            update["last_error"] = (
                f"Planner executor failed (exit_code={result.exit_code}). "
                f"stderr={result.metadata.get('stderr', '')[:500]}"
            )
            update["final_status"] = "failed"
        return update

    return _node


def _extract_todo(plan_text: str) -> str:
    """Pull the `## Subtasks` section into a standalone todo.md."""
    lines = plan_text.splitlines()
    out: list[str] = []
    in_section = False
    for line in lines:
        if line.strip().lower().startswith("## subtasks"):
            in_section = True
            out.append(line)
            continue
        if in_section:
            if line.startswith("## ") and not line.lower().startswith("## subtasks"):
                break
            out.append(line)
    if not out:
        return "# TODO\n\n_No subtasks were extracted from the plan._\n"
    return "# TODO\n\n" + "\n".join(out).strip() + "\n"


def _extract_todo_lines(plan_text: str) -> list[str]:
    """Return just the bullet text from the subtasks section."""
    todo_block = _extract_todo(plan_text)
    items: list[str] = []
    for raw in todo_block.splitlines():
        stripped = raw.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
    return items


PlannerCallable = Callable[[AgentState], dict]
