"""Reviewer node.

Self-review of the change produced by the coder. We re-use the same executor
backend (cursor / claude_code / ...) but in a deliberately read-only posture:
the reviewer is handed the diff and asked to render a verdict.

The reviewer's verdict drives a graph edge:

- ``PASS`` → proceed to reporter (open PR / mark ready, in later phases).
- ``FAIL`` → route back to coder (within retry budget).

Verdict parsing is intentionally simple — we look for a ``VERDICT:`` line in
the executor output. Anything other than ``PASS`` (case-insensitive) is
treated as ``FAIL``. Producing a structured field via prompt discipline is
better than parsing free-form prose.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent

from app.executors.base import ExecutorRequest
from app.langgraph_app.nodes.context import NodeContext
from app.langgraph_app.state import AgentState
from app.observability import get_logger

log = get_logger(__name__)

ReviewerCallable = Callable[[AgentState], dict]


REVIEW_PROMPT_TEMPLATE = dedent(
    """\
    You are the **Reviewer** for a long-running coding agent. The Coder has
    produced the diff below for the GitHub issue. Decide whether this change
    is ready to ship.

    ## Repository
    {repo}

    ## Issue #{issue_number} — {issue_title}

    {issue_body}

    ## Plan we agreed to
    {plan}

    ## Files this commit actually touches (authoritative, parsed from the diff headers)

    Use **this list** — not your reading of the raw diff — to decide which paths
    are added/modified/deleted. The raw diff below is shown for context, but it
    can include misleading content (e.g. a line `+.venv` inside `.gitignore` is
    an **ignore rule**, NOT a committed `.venv` file).

    {changed_files_summary}

    ## Diff (truncated to first {diff_chars} chars)
    ```diff
    {diff}
    ```

    ## Local test result
    {test_status_summary}

    ## Acceptance criteria (apply IN THIS ORDER — do not skip ahead)

    1. **Issue fulfilment.** Does the diff produce what the *issue body
       literally asks for*? That is the only mandatory bar. If yes, lean
       toward PASS even if the plan listed extra ideas the diff does not
       cover.

    2. **Diff correctness.** Are the changed files internally consistent —
       no obvious bugs, no broken imports, no factual errors in any prose
       that mentions concrete paths / commands / config keys / pytest
       markers / CI job names / function signatures? Cross-check those
       against the authoritative file list above; if you cannot verify a
       claim from the diff alone, name it as a *risk*, not a defect.

    3. **Local verification gate.** Honour `## Local test result` literally:
       - "All configured lint/test commands passed" → strong PASS signal.
       - "Some tests failed" → defect; cite which command in the verdict.
       - "No tests were run (no commands configured…)" → **NEUTRAL**. The
         project chose not to wire `commands.test` / `commands.lint`, and
         that choice is not something the Coder can fix in this PR. Do
         **not** convert "no evidence" into FAIL.

    4. **Plan compliance (advisory only).** The plan is a *guide*, not a
       contract. Subtasks the Coder did not implement are NOT grounds for
       FAIL unless they are necessary to satisfy criterion 1.

    5. **Follow-ups exemption.** Anything under a `## Follow-ups` heading
       in the plan is explicitly out-of-scope work that the Coder was told
       to skip. NEVER fail the diff for not doing them, and never list
       them in the verdict reason.

    ## Important reading rules (do not violate)

    - A path is "committed" only if it appears in the authoritative list above
      with op=added or op=modified. If a name like `.venv`, `node_modules`,
      `*.pyc` appears **only** inside the body of `.gitignore` / `.dockerignore`
      / `.npmignore`, it is an exclusion rule — that's the opposite of being
      committed, and is generally good hygiene.
    - Symlinks are flagged in the authoritative list with `mode=symlink`. If
      none are listed, no symlink was committed regardless of what the diff
      body mentions.
    - Deletions are flagged with op=deleted. Their content disappearing from
      the working tree is the *intent*, not a regression.

    ## Output requirements (strict, parsed mechanically)

    Reply with **two sections**:

    1. A short markdown review (≤ 50 lines): purpose, risk, anything the Coder
       should revisit. If you fail, the *Risks / revisit* bullets you write
       here are the next round's checklist — be concrete (path + line + what
       you actually verified), not vague.
    2. A final line as **plain text** (no backticks, asterisks, blockquote
       markers, or any other markdown formatting around it), in one of these
       exact forms:

           VERDICT: PASS
           VERDICT: FAIL — <one-line reason citing a concrete defect from the diff>

    The FAIL reason must reference something *in the diff* (path, missing
    feature relative to the issue body, factually wrong claim). It must
    NOT be "missing tests we never asked for", "did not finish a follow-up",
    or "did not fix unrelated CI breakage".

    Do not output anything after the VERDICT line.
    """
)

# Match the VERDICT line even when the model wraps it in markdown decorations
# (e.g. ``VERDICT: PASS``, **VERDICT: FAIL**, > VERDICT: PASS) — and even when
# the PASS/FAIL token itself is wrapped (e.g. ``VERDICT: `PASS` ``). The prompt
# asks for plain text, but real LLM output drifts; be permissive on the parse
# side so we never silently flip a true PASS to a pessimistic FAIL.
_VERDICT_RE = re.compile(
    r"^[\s`*_>\-]*VERDICT:\s*[`*_]*(PASS|FAIL)\b",
    re.IGNORECASE | re.MULTILINE,
)
_DIFF_TRUNCATE = 12_000


def reviewer_node(ctx: NodeContext) -> ReviewerCallable:
    def _node(state: AgentState) -> dict:
        diff = _load_diff(ctx.run_dir.execution_dir / "diff.patch")
        changed = state.get("changed_files") or []
        if not diff and not changed:
            log.info("reviewer.no_changes", reason="coder produced empty diff")
            scratch = {**(state.get("scratch") or {}), "review_verdict": "fail"}
            return {
                "current_step": "reviewer_failed",
                "scratch": scratch,
                "last_error": (
                    state.get("last_error")
                    or "Reviewer rejected: coder produced no changes."
                ),
            }

        prompt = _build_prompt(state, diff=diff, changed_files=changed)
        log.info(
            "reviewer.start",
            issue=state.get("issue_number"),
            executor=ctx.executor.name,
            diff_chars=len(diff),
        )
        result = ctx.executor.run(
            ExecutorRequest(
                kind="review",
                prompt=prompt,
                workspace=None,  # read-only
                artifact_dir=ctx.run_dir.review_dir,
                metadata={"issue_number": state.get("issue_number")},
            )
        )

        review_text = (result.output or "").strip() or "_Reviewer returned no output._"
        ctx.artifacts.write_text(
            ctx.run_dir.review_dir / "self_review.md",
            review_text + "\n",
        )

        verdict = _parse_verdict(review_text, executor_ok=result.ok)
        scratch = {**(state.get("scratch") or {}), "review_verdict": verdict}

        ctx.artifacts.append_jsonl(
            ctx.run_dir.tool_calls_jsonl,
            {
                "node": "reviewer",
                "executor": ctx.executor.name,
                "ok": result.ok,
                "verdict": verdict,
                "duration_seconds": result.duration_seconds,
            },
        )

        update: dict = {
            "current_step": "reviewer_done" if verdict == "pass" else "reviewer_failed",
            "scratch": scratch,
        }
        if verdict == "fail":
            update["last_error"] = (
                _extract_fail_reason(review_text)
                or "Reviewer voted FAIL (see review/self_review.md)."
            )
        else:
            # Keep last_error stable: don't clobber a non-empty value, but if
            # we're passing review, clear it so the reporter doesn't print
            # stale messages.
            update["last_error"] = ""
        return update

    return _node


def _build_prompt(
    state: AgentState, *, diff: str, changed_files: list[str]
) -> str:
    test_status = state.get("local_test_status", "unknown")
    test_summary = {
        "pass": "All configured lint/test commands passed.",
        "fail": "Some tests failed — see evidence/local_tests.log.",
        "unknown": (
            "No tests were run (no commands.test/commands.lint configured for "
            "this project, or tester was skipped). This is a project-level "
            "config choice, not a Coder failure — treat as NEUTRAL evidence "
            "per acceptance criterion 3."
        ),
    }.get(test_status, "Unknown.")

    return REVIEW_PROMPT_TEMPLATE.format(
        repo=state.get("repo", ""),
        issue_number=state.get("issue_number", -1),
        issue_title=state.get("issue_title", ""),
        issue_body=(state.get("issue_body") or "").strip() or "(empty)",
        plan=state.get("plan") or "(no plan recorded)",
        changed_files_summary=_summarize_changed_files(diff, changed_files),
        diff=(diff[:_DIFF_TRUNCATE] + ("\n... [truncated]" if len(diff) > _DIFF_TRUNCATE else "")),
        diff_chars=_DIFF_TRUNCATE,
        test_status_summary=test_summary,
    )


# git unified-diff header markers we care about. See ``git help diff-format``.
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_NEW_FILE_RE = re.compile(r"^new file mode (\d+)$")
_DELETED_FILE_RE = re.compile(r"^deleted file mode (\d+)$")
_NEW_MODE_RE = re.compile(r"^new mode (\d+)$")
_RENAME_FROM_RE = re.compile(r"^rename from (.+)$")
_RENAME_TO_RE = re.compile(r"^rename to (.+)$")
_GIT_MODE_SYMLINK = "120000"


def _summarize_changed_files(diff_text: str, fallback: list[str]) -> str:
    """Render an authoritative file-change list from the unified diff headers.

    Parsing ``diff --git`` / ``new file mode`` / ``deleted file mode`` / rename
    markers lets us tell the reviewer exactly which paths actually changed and
    whether each is a regular file or a symlink. This is the antidote to the
    reviewer hallucinating ``.venv`` got committed when it only appears as an
    ignore rule inside ``.gitignore``.

    Falls back to ``state['changed_files']`` (or "(none)") if the diff is
    empty / unparseable.
    """
    entries: list[tuple[str, str, str]] = []  # (path, op, mode_label)
    if not diff_text:
        if fallback:
            return "\n".join(f"- `{p}` — op=modified; mode=regular" for p in fallback)
        return "- (no files changed)"

    current_path: str | None = None
    current_op = "modified"
    current_mode = _GIT_MODE_SYMLINK  # placeholder; reset below
    current_mode_known = False
    rename_from: str | None = None

    def _flush() -> None:
        nonlocal current_path, current_op, current_mode, current_mode_known, rename_from
        if current_path is None:
            return
        mode_label = "symlink" if current_mode_known and current_mode == _GIT_MODE_SYMLINK else "regular"
        if not current_mode_known and current_op == "modified":
            mode_label = "regular"
        path = current_path
        op = current_op
        if rename_from and rename_from != current_path:
            op = f"renamed from {rename_from}"
        entries.append((path, op, mode_label))
        current_path = None
        current_op = "modified"
        current_mode_known = False
        rename_from = None

    for raw in diff_text.splitlines():
        m = _DIFF_HEADER_RE.match(raw)
        if m:
            _flush()
            current_path = m.group(2)
            continue
        if current_path is None:
            continue
        if m2 := _NEW_FILE_RE.match(raw):
            current_op = "added"
            current_mode = m2.group(1)
            current_mode_known = True
            continue
        if m2 := _DELETED_FILE_RE.match(raw):
            current_op = "deleted"
            current_mode = m2.group(1)
            current_mode_known = True
            continue
        if m2 := _NEW_MODE_RE.match(raw):
            current_mode = m2.group(1)
            current_mode_known = True
            continue
        if m2 := _RENAME_FROM_RE.match(raw):
            rename_from = m2.group(1)
            continue
        if m2 := _RENAME_TO_RE.match(raw):
            current_path = m2.group(1)
            current_op = "renamed"
            continue
    _flush()

    if not entries:
        if fallback:
            return "\n".join(f"- `{p}` — op=modified; mode=regular" for p in fallback)
        return "- (no files changed)"
    return "\n".join(
        f"- `{path}` — op={op}; mode={mode_label}" for path, op, mode_label in entries
    )


def _load_diff(patch_path: Path) -> str:
    if not patch_path.exists():
        return ""
    try:
        return patch_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _parse_verdict(text: str, *, executor_ok: bool) -> str:
    if not executor_ok:
        return "fail"
    m = _VERDICT_RE.search(text)
    if not m:
        # No structured verdict ⇒ pessimistic FAIL so a human reviews.
        return "fail"
    return m.group(1).lower()


def _extract_fail_reason(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().strip("`*_>").strip()
        if stripped.upper().startswith("VERDICT: FAIL"):
            _, _, reason = stripped.partition("—")
            if not reason:
                _, _, reason = stripped.partition("-")
            return reason.strip()[:500] or stripped[:500]
    return ""
