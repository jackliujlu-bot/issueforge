"""Deliverer node.

Phase 3: takes a reviewed worktree and turns it into a real Pull Request.

Flow:

1. Commit any pending changes in the worktree (deduped by tracked-file diff).
2. Push the branch to the configured remote.
3. ``gh pr create`` (or ``gh pr edit`` if a PR for this branch already exists).
4. Optionally enable auto-merge if ``github.auto_merge=true``.
5. Stamp ``state.pr_number / pr_url / final_status='pr_created'``.

Like every other node, deliverer is the only place that mutates remote state
of its kind. The reporter node consumes ``pr_number`` / ``pr_url`` to build
the GitHub-issue comment.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.github.pr_service import GitHubPRService, GitOperationError
from app.langgraph_app.nodes.context import NodeContext
from app.langgraph_app.state import AgentState
from app.observability import get_logger

log = get_logger(__name__)

DelivererCallable = Callable[[AgentState], dict]


def deliverer_node(ctx: NodeContext) -> DelivererCallable:
    def _node(state: AgentState) -> dict:
        workspace_path = state.get("workspace_path") or ""
        branch = state.get("branch") or ""
        if not workspace_path or not branch:
            return _bail(
                state,
                f"deliverer needs workspace_path and branch (got {workspace_path!r}, {branch!r})",
            )

        workspace = Path(workspace_path)
        if not workspace.exists():
            return _bail(state, f"workspace path missing: {workspace_path}")

        service = GitHubPRService(ctx.config.repo, ctx.config.github)
        commit_message = _build_commit_message(state)
        try:
            sha = service.commit_changes(workspace, files=None, message=commit_message)
            service.push_branch(
                workspace,
                branch=branch,
                remote=ctx.config.repo.push_remote,
                force=False,
            )
        except GitOperationError as exc:
            return _bail(state, f"Git operation failed: {exc}")

        pr_title, pr_body = _build_pr_text(state)
        try:
            pr = service.create_or_update(
                head_branch=branch,
                base_branch=ctx.config.repo.base_branch,
                title=pr_title,
                body=pr_body,
            )
        except Exception as exc:  # gh CLI errors are runtime
            return _bail(state, f"`gh pr create` failed: {exc}")

        if ctx.config.github.auto_merge and pr.number > 0:
            service.enable_auto_merge(pr.number)

        ctx.artifacts.append_log(
            ctx.run_dir.commands_log,
            f"deliverer.pr=#{pr.number} url={pr.url} head_sha={sha or 'reused'}",
        )
        ctx.artifacts.write_text(
            ctx.run_dir.root / "delivery.md",
            _build_delivery_md(pr=pr, sha=sha, state=state),
        )

        log.info(
            "deliverer.pr_ready",
            pr_number=pr.number,
            url=pr.url,
            branch=branch,
        )

        update: dict = {
            "pr_number": pr.number if pr.number > 0 else None,
            "current_step": "deliverer_done",
            "last_error": "",
            "scratch": {
                **(state.get("scratch") or {}),
                "pr_url": pr.url,
                "pr_head_sha": sha or "",
            },
        }
        return update

    return _node


def _bail(state: AgentState, reason: str) -> dict:
    log.error("deliverer.failed", reason=reason)
    return {
        "current_step": "deliverer_failed",
        "last_error": reason,
        "scratch": {**(state.get("scratch") or {}), "deliverer_note": reason},
    }


def _build_commit_message(state: AgentState) -> str:
    title = state.get("issue_title") or "agent change"
    issue_number = state.get("issue_number") or 0
    return (
        f"agent: {title}\n\n"
        f"Resolves #{issue_number}\n\n"
        f"This commit was prepared by issue-agent-worker; see the linked PR "
        f"description for the plan and verification evidence.\n"
    )


def _build_pr_text(state: AgentState) -> tuple[str, str]:
    issue_number = state.get("issue_number") or 0
    title = state.get("issue_title") or "agent change"
    pr_title = f"agent: {title} (#{issue_number})"

    plan_summary = (state.get("plan") or "").strip()
    if len(plan_summary) > 4000:
        plan_summary = plan_summary[:4000].rstrip() + "\n\n…(plan truncated for PR body)"

    changed = state.get("changed_files") or []
    changed_block = "\n".join(f"- `{f}`" for f in changed) or "- (no files)"

    test_status = state.get("local_test_status", "unknown")
    test_summary = {
        "pass": ":white_check_mark: local tests passed",
        "fail": ":x: local tests failed (see artifacts)",
        "unknown": "_local tests not run_",
    }.get(test_status, "unknown")

    review_verdict = (state.get("scratch") or {}).get("review_verdict", "")
    review_block = ""
    if review_verdict == "pass":
        review_block = ":white_check_mark: Self-review: PASS\n\n"
    elif review_verdict == "fail":
        review_block = ":warning: Self-review: FAIL\n\n"

    retries = int(state.get("retry_count", 0) or 0)
    max_retries = int(state.get("max_retries", 0) or 0)

    body = (
        f"Closes #{issue_number}\n\n"
        f"_Generated by issue-agent-worker. Retries used: {retries}/{max_retries}._\n\n"
        f"## Plan\n\n{plan_summary or '_(planner produced no plan)_'}\n\n"
        f"## Files changed\n\n{changed_block}\n\n"
        f"## Local test result\n\n{test_summary}\n\n"
        f"{review_block}"
        "---\n\n"
        "_This PR was opened by the agent. CI will gate the merge; any failure "
        "is handled automatically in Phase 4._\n"
    )
    return pr_title, body


def _build_delivery_md(*, pr: object, sha: str | None, state: AgentState) -> str:
    pr_number = getattr(pr, "number", -1)
    pr_url = getattr(pr, "url", "")
    head = getattr(pr, "head_branch", state.get("branch", ""))
    return (
        "# Delivery\n\n"
        f"- pr_number: {pr_number}\n"
        f"- pr_url: {pr_url}\n"
        f"- head_branch: {head}\n"
        f"- head_sha: {sha or '(reused)'}\n"
        f"- retries_used: {state.get('retry_count', 0)}/{state.get('max_retries', 0)}\n"
    )
