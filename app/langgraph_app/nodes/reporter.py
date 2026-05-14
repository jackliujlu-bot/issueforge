"""Reporter node.

Aggregates state into:
    - ``handoff.md`` on disk
    - ``pending_issue_comment`` on the state, which the Temporal activity will
      post back to GitHub. Putting the comment-text composition here means the
      orchestrator never has to know what's in the plan.

The reporter is also where ``final_status`` is finalised:

- ``last_error`` set                            → ``failed``
- ``stop_after=planning`` and no error          → ``planning_done``
- Phase 2: tests passed + review passed         → ``ready_for_review``
- Phase 2: review failed / tests failed (retry exhausted)
                                                → ``failed``
"""

from __future__ import annotations

from collections.abc import Callable

from app.langgraph_app.nodes.context import NodeContext
from app.langgraph_app.state import AgentState

ReporterCallable = Callable[[AgentState], dict]


def reporter_node(ctx: NodeContext) -> ReporterCallable:
    def _node(state: AgentState) -> dict:
        plan = state.get("plan") or "_(no plan was produced)_"
        last_error = state.get("last_error") or ""
        executor_name = state.get("executor") or ctx.executor.name
        retry = int(state.get("retry_count", 0) or 0)
        stop_after = ctx.config.workflow.stop_after

        final_status = _decide_final_status(state, stop_after=stop_after)

        comment = _build_comment(
            plan=plan,
            executor=executor_name,
            error=last_error,
            retry_count=retry,
            max_retries=int(state.get("max_retries", 0) or 0),
            stop_after=stop_after,
            final_status=final_status,
            state=state,
        )

        # Snapshot handoff after final_status is decided so the on-disk record
        # matches what we return to Temporal.
        handoff = _build_handoff({**state, "final_status": final_status})
        ctx.artifacts.write_text(ctx.run_dir.handoff_md, handoff)

        return {
            "pending_issue_comment": comment,
            "current_step": "reporter_done",
            "final_status": final_status,
        }

    return _node


def _decide_final_status(state: AgentState, *, stop_after: str) -> str:
    last_error = state.get("last_error") or ""
    if last_error:
        return "failed"
    if stop_after == "planning":
        return "planning_done"
    if stop_after == "coding":
        return "ready_for_review" if state.get("changed_files") else "failed"
    # testing / review / done
    test_status = state.get("local_test_status", "unknown")
    review_verdict = (state.get("scratch") or {}).get("review_verdict", "")
    if test_status == "fail":
        return "failed"
    if stop_after in ("review", "done") and review_verdict.lower() == "fail":
        return "failed"
    if stop_after == "done" and state.get("pr_number"):
        return "pr_created"
    return "ready_for_review"


def _build_comment(
    *,
    plan: str,
    executor: str,
    error: str,
    retry_count: int,
    max_retries: int,
    stop_after: str,
    final_status: str,
    state: AgentState,
) -> str:
    header = "## :robot: issue-agent-worker — round report\n\n"
    meta = (
        f"- executor: `{executor}`\n"
        f"- stop_after: `{stop_after}`\n"
        f"- final_status: `{final_status}`\n"
        f"- retries used: {retry_count}/{max_retries}\n\n"
    )

    sections: list[str] = [header, meta]

    if error:
        sections.append(
            "### :x: Failure\n\n"
            "```text\n"
            f"{error}\n"
            "```\n\n"
            "The plan and any partial work have been saved as artifacts and "
            "will be reused on retry.\n\n"
        )

    sections.append("### Plan\n\n" + plan + "\n\n")

    # Phase 2+ sections
    changed_files = state.get("changed_files") or []
    if changed_files:
        sections.append(
            "### Files changed\n\n" + "\n".join(f"- `{f}`" for f in changed_files) + "\n\n"
        )

    test_status = state.get("local_test_status", "unknown")
    if test_status != "unknown" or stop_after != "planning":
        sections.append("### Local test result\n\n")
        if test_status == "pass":
            sections.append(":white_check_mark: All configured lint/test commands passed.\n\n")
        elif test_status == "fail":
            sections.append(
                ":x: One or more verification commands failed; "
                "see `evidence/local_tests.log` in the artifact tree.\n\n"
            )
        else:
            sections.append("_No tests were run (commands.test / commands.lint empty)._\n\n")

    review_verdict = (state.get("scratch") or {}).get("review_verdict", "")
    if review_verdict:
        if review_verdict == "pass":
            sections.append(
                "### Self-review\n\n:white_check_mark: PASS — see `review/self_review.md`.\n\n"
            )
        else:
            sections.append("### Self-review\n\n:warning: FAIL — see `review/self_review.md`.\n\n")

    pr_number = state.get("pr_number")
    pr_url = (state.get("scratch") or {}).get("pr_url", "")
    if pr_number or pr_url:
        sections.append(f"### Pull request\n\n- number: #{pr_number}\n- url: {pr_url}\n\n")

    if stop_after == "planning":
        sections.append(
            "---\n\n"
            "_Phase 1 stops here by design (`workflow.stop_after=planning`). "
            "Coding nodes will run automatically once Phase 2 is enabled._\n"
        )
    elif final_status == "pr_created":
        sections.append(
            "---\n\n"
            "_PR opened. CI will gate the merge. If CI fails the agent will "
            "iterate automatically (Phase 4)._\n"
        )
    elif final_status == "ready_for_review":
        sections.append(
            "---\n\n"
            "_Tests passed and self-review is positive. The branch is on disk; "
            "set `workflow.stop_after=done` to also open a PR._\n"
        )
    elif final_status == "failed":
        sections.append(
            "---\n\n"
            "_Retry budget exhausted (or coder produced no changes). Human input "
            "needed — re-run after addressing the failure above._\n"
        )

    return "".join(sections)


def _build_handoff(state: AgentState) -> str:
    todo_items = state.get("todo") or ["(empty)"]
    todo_block = "\n".join(f"- {t}" for t in todo_items)
    changed = state.get("changed_files") or []
    changed_block = "\n".join(f"- {f}" for f in changed) if changed else "- (none)"
    pr_url = (state.get("scratch") or {}).get("pr_url", "")
    return (
        "# Handoff\n\n"
        f"- issue: #{state.get('issue_number')} — {state.get('issue_title', '')}\n"
        f"- repo: {state.get('repo')}\n"
        f"- executor: {state.get('executor')}\n"
        f"- branch: {state.get('branch') or '(not yet created)'}\n"
        f"- workspace_path: {state.get('workspace_path') or '(not yet created)'}\n"
        f"- pr_number: {state.get('pr_number') or '(no PR yet)'}\n"
        f"- pr_url: {pr_url or '(no PR yet)'}\n"
        f"- retry_count: {state.get('retry_count', 0)} / {state.get('max_retries', 0)}\n"
        f"- local_test_status: {state.get('local_test_status', 'unknown')}\n"
        f"- review_verdict: {(state.get('scratch') or {}).get('review_verdict') or '(not reviewed)'}\n"
        f"- final_status: {state.get('final_status', 'running')}\n"
        f"- last_error: {state.get('last_error') or '(none)'}\n\n"
        "## Plan snapshot\n\n"
        f"{state.get('plan') or '(no plan)'}\n\n"
        "## Todo\n\n"
        f"{todo_block}\n\n"
        "## Changed files (this round)\n\n"
        f"{changed_block}\n"
    )
