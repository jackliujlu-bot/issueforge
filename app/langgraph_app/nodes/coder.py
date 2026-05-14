"""Coder node.

Drives one coding round: ensures a worktree, builds a coder prompt from the
plan / todo / prior failure, hands it to the configured code executor, and
collects the resulting diff / changed_files into state.

The node is the only place in the graph that *mutates the repo*. Tester and
reviewer are read-only.

Routing semantics (decided in ``graph.py``):

- coder always proceeds to tester (or, in ``stop_after=coding`` mode, to
  reporter). Executor failures surface as ``last_error`` so the tester / report
  layer can see them.
- ``retry_count`` is bumped here whenever we re-enter coder with prior failure
  signal (test fail or review fail). The conditional edges only route back if
  ``retry_count < max_retries``, so by the time we enter again, retrying is
  authorised.

Resume semantics: if a previous coder run already produced commits on the
agent branch (visible as ``HEAD ahead of base_branch``) AND there's no prior
failure signal, we skip the executor call and reuse those commits. That lets
restarts after a transient push/PR failure (Phase 3+) pick up where they left
off without spending more LLM time.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from textwrap import dedent

from app.executors.base import ExecutorRequest
from app.langgraph_app.nodes.context import NodeContext
from app.langgraph_app.state import AgentState
from app.observability import get_logger
from app.sandbox.worktree import Worktree

log = get_logger(__name__)

CoderCallable = Callable[[AgentState], dict]


CODER_PROMPT_TEMPLATE = dedent(
    """\
    You are the **Coder** for a long-running coding agent. The Planner has
    already produced a plan; your job is to make the smallest correct change
    that satisfies the issue, in this single workspace, using your tools.

    ## Repository
    {repo}

    ## Branch
    {branch} (based on {base_branch})

    ## Issue #{issue_number} — {issue_title}

    {issue_body}

    ## Plan
    {plan}

    ## Todo (subtasks to drive your work)
    {todo}
    {retry_section}

    ## Fact-checking discipline (MANDATORY — failing this is the #1 cause of review rejection)

    Before writing or modifying **any** prose, code reference, example, table,
    or doc section that mentions an existing file / config key / command / CLI
    flag / pytest marker / CI workflow / env variable / function / class /
    script in this repository, you **MUST** first read the relevant file(s)
    inside {workspace} to confirm what they actually contain. Concrete rules:

    - Citing `pyproject.toml` (markers, addopts, dependencies, scripts) →
      open and read `pyproject.toml` first; quote the actual `[tool.*]` block.
    - Describing a GitHub Actions workflow → read `.github/workflows/*.yml`;
      do not invent job names, matrix axes, or runner labels.
    - Describing a `bin/<script>` → open the script; quote its exact `exec`
      line or relevant flags; do not invent `--numprocesses=auto` etc.
    - Citing a pytest marker / function / class / CLI option →
      `rg -n "<name>"` inside {workspace} to confirm the spelling exists in
      the real source, not just in the issue body or the plan.

    **Never invent** paths, marker names, command-line flags, default values,
    or shell behaviour. If you cannot verify a fact in the workspace, **leave
    it out** or write `TODO: verify ...` so the reviewer can see the gap.
    Inventing plausible-sounding details is the most expensive failure mode
    of this loop because the reviewer cross-checks against the real tree.

    ## Rules
    - Edit files **only inside** {workspace}.
    - Keep the change focused. No drive-by refactors.
    - Commit nothing yourself; the orchestrator handles git.
    - When you are done, end with a one-sentence summary line:
      `SUMMARY: <one-sentence description of the change>`

    Begin.
    """
)


RETRY_SECTION_TEMPLATE = dedent(
    """\

    ## Previous failure (retry {retry_count} of {max_retries}) — read this carefully

    The reviewer **rejected your previous attempt**. Every concrete defect
    listed below is a thing you MUST address this round — either fix it, or
    remove the offending content if you cannot verify it. Do not just reword
    the previous output; for every factual claim the reviewer disputed, open
    the referenced file in the workspace and confirm what it actually says
    BEFORE writing anything.

    ### Reviewer's one-line verdict
    ```text
    {last_error}
    ```

    ### Reviewer's full notes from the previous round
    The text below is the reviewer's complete review markdown (truncated if
    very long). Pay special attention to any "Risks / revisit" bullets — those
    are concrete, named problems the reviewer found by cross-checking against
    the real source tree. Each is a checklist item for this round.

    {reviewer_review}

    ### What you must do this round
    1. For every file path, config key, marker name, or CI detail the reviewer
       disputed, **open the actual file in the workspace and read it** before
       rewriting.
    2. Correct or delete every factual claim that doesn't match what you read.
    3. Re-issue the change. Keep edits minimal — only touch what's needed to
       address the review.
    """
)


def coder_node(ctx: NodeContext) -> CoderCallable:
    def _node(state: AgentState) -> dict:
        if ctx.worktree is None:
            return _coder_skipped(
                state,
                reason=(
                    "sandbox.mode!=worktree; coder needs an isolated checkout. "
                    "Configure sandbox.mode=worktree and repo.local_path."
                ),
            )

        retry_count = int(state.get("retry_count", 0) or 0)
        prior_error = state.get("last_error", "") or ""
        is_retry = bool(prior_error)
        if is_retry:
            retry_count += 1

        branch = state.get("branch") or _branch_name(
            ctx.config.repo.working_branch_prefix,
            int(state["issue_number"]),
        )
        try:
            worktree = ctx.worktree.ensure(
                repo_slug=state.get("repo", ""),
                branch=branch,
                base_branch=ctx.config.repo.base_branch,
                issue_key=_issue_key_from_state(state),
            )
        except Exception as exc:
            log.error("coder.worktree_failed", error=str(exc))
            return {
                "last_error": f"Failed to provision worktree: {exc}",
                "current_step": "coder_failed",
                "retry_count": retry_count,
            }

        # Bring the worktree's dependencies online before either coder or tester
        # touches it. ``commands.setup`` is typically ``uv sync`` / ``npm
        # install`` — without this, downstream ``uv run ruff`` / ``uv run
        # pytest`` (the tester's verification commands) blow up with
        # ``ruff: No such file or directory`` and the agent burns its retry
        # budget on a missing-binary error rather than on real code issues.
        # Cached via a marker file so we run setup once per worktree.
        setup_ok, setup_err = _ensure_workspace_setup(
            ctx=ctx,
            workspace=worktree.path,
            setup_commands=list(ctx.config.commands.setup or []),
        )
        if not setup_ok:
            return {
                "last_error": setup_err,
                "current_step": "setup_failed",
                "retry_count": retry_count,
                "branch": branch,
                "workspace_path": str(worktree.path),
            }

        # Resume: if the worktree already has commits ahead of base AND we
        # don't have a prior-failure signal, reuse the existing work instead
        # of burning more cursor-agent time.
        ahead_diff, ahead_files = _ahead_of_base(worktree)
        if ahead_files and not is_retry:
            log.info(
                "coder.resumed_existing_branch",
                workspace=str(worktree.path),
                files=ahead_files,
            )
            _persist_diff(ctx, files=ahead_files, diff=ahead_diff)
            ctx.artifacts.append_log(
                ctx.run_dir.commands_log,
                f"coder.reused_existing files={len(ahead_files)}",
            )
            ctx.artifacts.append_jsonl(
                ctx.run_dir.tool_calls_jsonl,
                {
                    "node": "coder",
                    "executor": ctx.executor.name,
                    "reused_existing": True,
                    "changed_files": ahead_files,
                    "retry_count": retry_count,
                },
            )
            return {
                "branch": branch,
                "workspace_path": str(worktree.path),
                "current_step": "coder_done",
                "executor": ctx.executor.name,
                "retry_count": retry_count,
                "changed_files": ahead_files,
                "last_executor_output": "(reused commits already on branch)",
                "last_error": "",
            }

        prompt = _build_prompt(
            state,
            ctx=ctx,
            worktree=worktree,
            is_retry=is_retry,
            retry_count=retry_count,
        )
        ctx.artifacts.append_log(
            ctx.run_dir.commands_log,
            f"coder.invoke executor={ctx.executor.name} workspace={worktree.path}",
        )

        log.info(
            "coder.start",
            issue=state.get("issue_number"),
            executor=ctx.executor.name,
            retry=retry_count,
            workspace=str(worktree.path),
        )

        result = ctx.executor.run(
            ExecutorRequest(
                kind="code",
                prompt=prompt,
                workspace=worktree.path,
                artifact_dir=ctx.run_dir.execution_dir,
                metadata={
                    "issue_number": state.get("issue_number"),
                    "branch": branch,
                    "retry_count": retry_count,
                },
            )
        )

        ctx.artifacts.append_jsonl(
            ctx.run_dir.tool_calls_jsonl,
            {
                "node": "coder",
                "executor": ctx.executor.name,
                "ok": result.ok,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
                "changed_files": result.changed_files,
                "retry_count": retry_count,
            },
        )

        # Snapshot the diff and changed-files list to disk so a later round
        # (or a human) can audit exactly what coder did this time.
        _persist_diff(ctx, files=result.changed_files, diff=result.diff)

        update: dict = {
            "branch": branch,
            "workspace_path": str(worktree.path),
            "current_step": "coder_done",
            "executor": ctx.executor.name,
            "retry_count": retry_count,
            "changed_files": result.changed_files,
            "last_executor_output": (result.output or "")[:5000],
        }
        if not result.ok:
            stderr = result.metadata.get("stderr", "")
            update["last_error"] = (
                f"Coder executor failed (exit_code={result.exit_code}). "
                f"stderr={str(stderr)[:500]}"
            )
        else:
            # Clear the inherited error so the tester sees a fresh slate.
            update["last_error"] = ""
        return update

    return _node


def _branch_name(prefix: str, issue_number: int) -> str:
    safe_prefix = (prefix or "agent").strip("/").strip()
    return f"{safe_prefix}/issue-{issue_number}"


def _issue_key_from_state(state: AgentState) -> str:
    repo = (state.get("repo") or "").replace("/", "--") or "repo"
    return f"{repo}--issue-{state['issue_number']}"


def _build_prompt(
    state: AgentState,
    *,
    ctx: NodeContext,
    worktree: Worktree,
    is_retry: bool,
    retry_count: int,
) -> str:
    todo_lines = state.get("todo") or []
    todo_block = "\n".join(f"- {t}" for t in todo_lines) or "- (planner produced no subtasks)"
    retry_section = ""
    if is_retry:
        retry_section = RETRY_SECTION_TEMPLATE.format(
            retry_count=retry_count,
            max_retries=state.get("max_retries", 0),
            last_error=(state.get("last_error") or "").strip()[:1500] or "(no detail)",
            reviewer_review=_load_last_reviewer_review(ctx),
        )
    return CODER_PROMPT_TEMPLATE.format(
        repo=state.get("repo", ""),
        issue_number=state.get("issue_number", -1),
        issue_title=state.get("issue_title", ""),
        issue_body=(state.get("issue_body") or "").strip() or "(empty issue body)",
        plan=state.get("plan") or "(planner produced no plan)",
        todo=todo_block,
        branch=worktree.branch,
        base_branch=worktree.base_branch,
        workspace=worktree.path,
        retry_section=retry_section,
    )


_REVIEWER_REVIEW_TRUNCATE = 4_000


def _load_last_reviewer_review(ctx: NodeContext) -> str:
    """Return the markdown of the most recent reviewer pass, if any.

    The reviewer node writes ``runs/<key>/review/self_review.md`` at the end of
    every review. On a retry round we surface that whole text to the coder so
    the model sees every concrete defect (not just the one-line VERDICT reason
    that ``state['last_error']`` carries). Truncated to keep the prompt small.
    """
    try:
        review_path = ctx.run_dir.review_dir / "self_review.md"
    except AttributeError:
        return "(reviewer notes unavailable — run_dir missing review_dir)"
    if not review_path.exists():
        return "(no previous reviewer notes on disk)"
    try:
        text = review_path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"(could not read previous reviewer notes: {exc})"
    text = text.strip()
    if not text:
        return "(previous reviewer notes were empty)"
    if len(text) > _REVIEWER_REVIEW_TRUNCATE:
        text = text[:_REVIEWER_REVIEW_TRUNCATE] + "\n\n_... [truncated]_"
    return text


def _ahead_of_base(worktree: Worktree) -> tuple[str, list[str]]:
    """Return ``(diff, files)`` for commits on the worktree's branch ahead of base.

    Returns empty tuple when the worktree is at-or-behind base (which is the
    normal first-round state). Silent on errors — the caller treats "no info"
    the same as "no prior work".
    """
    base = worktree.base_branch
    if not base:
        return "", []
    # Prefer the specific ref the worktree was created against (e.g.
    # ``feipeng/dev``); fall back to the bare ref and then ``origin/<base>``.
    candidates = []
    if worktree.base_ref:
        candidates.append(worktree.base_ref)
    candidates.extend([base, f"origin/{base}"])
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            files_out = subprocess.run(
                ["git", "diff", "--name-only", f"{candidate}...HEAD"],
                cwd=str(worktree.path),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if files_out.returncode != 0:
            continue
        files = [f for f in files_out.stdout.splitlines() if f]
        if not files:
            return "", []
        diff_out = subprocess.run(
            ["git", "diff", f"{candidate}...HEAD"],
            cwd=str(worktree.path),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return diff_out.stdout, files
    return "", []


def _persist_diff(ctx: NodeContext, *, files: list[str], diff: str) -> None:
    ctx.artifacts.write_text(
        ctx.run_dir.execution_dir / "changed_files.txt",
        "\n".join(files) + ("\n" if files else ""),
    )
    if diff:
        ctx.artifacts.write_text(
            ctx.run_dir.execution_dir / "diff.patch",
            diff,
        )


def _coder_skipped(state: AgentState, *, reason: str) -> dict:
    log.warning("coder.skipped", reason=reason, issue=state.get("issue_number"))
    return {
        "current_step": "coder_skipped",
        "last_error": reason,
        "scratch": {**(state.get("scratch") or {}), "coder_note": reason},
    }


# Name of the marker file dropped into the artifact dir (not the worktree)
# once ``commands.setup`` has finished successfully. The marker MUST live
# outside the worktree — otherwise cursor-agent's ``git add -A`` picks it up
# and pollutes the diff. Tests check for it; production reads / writes it.
WORKSPACE_SETUP_MARKER = ".setup-done"


def _setup_marker_path(ctx: NodeContext):  # type: ignore[no-untyped-def]
    """Where to put the per-workspace setup-done marker.

    Sits next to the artifact tree (``runs/<key>/execution/.setup-done``) so
    it survives worktree rebuilds but is invisible to git. We also include
    the workspace path's last component in the file so a worktree wipe gets
    a fresh re-setup — but only if the user manually clears the artifact tree.
    """
    return ctx.run_dir.execution_dir / WORKSPACE_SETUP_MARKER


def _ensure_workspace_setup(
    *,
    ctx: NodeContext,
    workspace,  # pathlib.Path; loosely-typed to avoid an import dance
    setup_commands: list[str],
) -> tuple[bool, str]:
    """Run ``commands.setup`` against ``workspace`` exactly once.

    The marker file ``runs/<key>/execution/.setup-done`` short-circuits
    subsequent rounds so we only pay the dependency-install cost on the first
    coder invocation. The marker is OUTSIDE the worktree to keep it out of
    the agent's git diff. Returns ``(True, "")`` on success / cache hit, or
    ``(False, err_summary)`` if a setup command fails — the caller surfaces
    ``err_summary`` as ``last_error`` and the workflow's retry layer decides
    whether to fail or back off.

    No-op when ``commands.setup`` is empty (e.g. for projects that don't need
    a setup step), or when the shell executor is disabled.
    """
    if not setup_commands:
        return True, ""
    if ctx.shell is None:
        log.warning(
            "coder.setup.skipped_no_shell",
            reason="shell executor disabled; cannot run commands.setup",
        )
        return True, ""

    marker = _setup_marker_path(ctx)
    if marker.exists():
        log.debug("coder.setup.cached", workspace=str(workspace), marker=str(marker))
        return True, ""

    log.info(
        "coder.setup.start",
        workspace=str(workspace),
        n_commands=len(setup_commands),
    )
    ctx.artifacts.append_log(
        ctx.run_dir.commands_log,
        f"setup.begin n_commands={len(setup_commands)} workspace={workspace}",
    )

    for cmd in setup_commands:
        if not cmd.strip():
            continue
        result = ctx.shell.run(
            ExecutorRequest(
                kind="setup",
                prompt=cmd,
                workspace=workspace,
                artifact_dir=ctx.run_dir.execution_dir,
                metadata={"label": "setup"},
            )
        )
        ctx.artifacts.append_log(
            ctx.run_dir.commands_log,
            f"setup: {cmd} (exit={result.exit_code} duration={result.duration_seconds:.1f}s)",
        )
        ctx.artifacts.append_jsonl(
            ctx.run_dir.tool_calls_jsonl,
            {
                "node": "coder",
                "phase": "setup",
                "command": cmd,
                "ok": result.ok,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
            },
        )
        if not result.ok:
            stderr = str(result.metadata.get("stderr", "")).strip()[-800:]
            err = (
                f"Workspace setup failed running `{cmd}` "
                f"(exit={result.exit_code}). stderr (last 800 chars):\n{stderr}"
            )
            log.warning(
                "coder.setup.failed",
                command=cmd,
                exit_code=result.exit_code,
            )
            return False, err

    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("ok\n")
    except OSError as exc:
        # Setup actually succeeded; failing to write the marker just means
        # we'll redundantly re-run setup next round. Don't fail the round.
        log.warning(
            "coder.setup.marker_write_failed",
            marker=str(marker),
            error=str(exc),
        )

    log.info("coder.setup.done", workspace=str(workspace))
    return True, ""
