"""Tester node.

Phase 2 implementation: runs ``commands.lint`` and ``commands.test`` (in that
order, lint first because it's almost always faster) through the shell
executor against ``state.workspace_path``. Each command's exit code, stdout
and stderr are appended to ``evidence/local_tests.log`` so a human (or the
next agent round) can reconstruct what happened.

Behaviour summary:

- All commands pass    → ``local_test_status="pass"``, ``last_error=""``
- Any command fails    → ``local_test_status="fail"``, ``last_error`` set to
  a compact summary that's small enough to fit in the coder retry prompt.
- No commands configured → ``local_test_status="unknown"``, treated as pass by
  the routing layer (we don't fail-closed on missing config).
- Coder previously failed (``last_error`` set + workspace missing) → propagate
  the failure as a test failure so retry logic kicks in.

The tester never spawns the coder backend; it only uses the shell executor.
This separation lets you point coder at `cursor`/`claude_code`/etc without
changing how the project's verification commands run.

Targeted (changed-files-only) test runs:

Big repos take minutes to lint+test in full, which makes the coder/tester
retry loop unusable. We support two template tokens in any command:

- ``{changed_files}``  → space-quoted list of files the coder touched this
  round (relative to the workspace). Empty → command is **skipped** (logged).
- ``{test_targets}``   → heuristically-mapped test-file paths for the
  changed source files: tests that share the source file's stem (e.g.
  ``foo.py`` → ``tests/test_foo.py``), plus any changed file that already
  looks like a test. Empty → command is **skipped**.

A command without either token runs unchanged (legacy behaviour). This lets
project YAML opt into "fast local test" semantics with a one-line tweak:

    commands:
      lint:
        - "uv run ruff check {changed_files}"
      test:
        - "uv run pytest -x {test_targets}"
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.executors.base import ExecutorRequest
from app.langgraph_app.nodes.context import NodeContext
from app.langgraph_app.state import AgentState
from app.observability import get_logger

log = get_logger(__name__)

TesterCallable = Callable[[AgentState], dict]

# Cap per-command stderr we surface back in last_error so the next coder
# retry prompt doesn't get drowned. Full output stays in evidence/.
_STDERR_SNIPPET = 800

# Template tokens the tester substitutes per-command. Anything else passes
# through to the shell unchanged.
_TOKEN_CHANGED_FILES = "{changed_files}"
_TOKEN_CHANGED_PY_FILES = "{changed_py_files}"
_TOKEN_TEST_TARGETS = "{test_targets}"

# Directories we search for the test-file companion of a source module.
# Picked to match the conventions found in the projects we operate on.
_TEST_SEARCH_DIRS = ("tests", "test", "tests/unit", "tests/integration")


def tester_node(ctx: NodeContext) -> TesterCallable:
    def _node(state: AgentState) -> dict:
        commands = _planned_commands(ctx)
        if not commands:
            log.info(
                "tester.no_commands",
                reason="commands.lint and commands.test are both empty",
            )
            return {
                "local_test_status": "unknown",
                "current_step": "tester_skipped",
            }

        workspace = state.get("workspace_path") or ""
        if not workspace or not Path(workspace).exists():
            return _propagate_coder_failure(state, workspace=workspace)

        if ctx.shell is None:
            log.warning("tester.no_shell_executor")
            return {
                "local_test_status": "unknown",
                "current_step": "tester_skipped",
                "last_error": "shell executor is disabled in config; cannot run tests.",
            }

        # Pre-compute the substitution sets for {changed_files} / {test_targets}.
        changed_files = list(state.get("changed_files") or [])
        workspace_path = Path(workspace)
        substitutions = _build_substitutions(
            changed_files=changed_files,
            workspace=workspace_path,
        )

        evidence_path = ctx.run_dir.evidence_dir / "local_tests.log"
        ctx.artifacts.write_text(evidence_path, "")  # start fresh each round
        failed: list[tuple[str, int, str]] = []
        skipped_for_empty_token: list[tuple[str, str, str]] = []
        for label, raw_cmd in commands:
            resolved = _resolve_command(raw_cmd, substitutions)
            if resolved.skip:
                log.info(
                    "tester.command.skipped",
                    label=label,
                    command=raw_cmd,
                    reason=resolved.skip_reason,
                )
                skipped_for_empty_token.append((label, raw_cmd, resolved.skip_reason))
                _append_evidence(
                    ctx,
                    evidence_path,
                    label=label,
                    command=raw_cmd,
                    exit_code=0,
                    duration=0.0,
                    stdout=f"(skipped: {resolved.skip_reason})",
                    stderr="",
                )
                ctx.artifacts.append_jsonl(
                    ctx.run_dir.tool_calls_jsonl,
                    {
                        "node": "tester",
                        "label": label,
                        "command": raw_cmd,
                        "skipped": True,
                        "reason": resolved.skip_reason,
                    },
                )
                continue
            log.info(
                "tester.command.start",
                label=label,
                command=resolved.command,
                workspace=workspace,
                substituted=resolved.substituted,
            )
            ctx.artifacts.append_log(ctx.run_dir.commands_log, f"{label}: {resolved.command}")
            result = ctx.shell.run(
                ExecutorRequest(
                    kind="test",
                    prompt=resolved.command,
                    workspace=workspace_path,
                    artifact_dir=ctx.run_dir.evidence_dir,
                    metadata={"label": label},
                )
            )
            _append_evidence(
                ctx,
                evidence_path,
                label=label,
                command=resolved.command,
                exit_code=result.exit_code,
                duration=result.duration_seconds,
                stdout=result.output,
                stderr=str(result.metadata.get("stderr", "")),
            )
            ctx.artifacts.append_jsonl(
                ctx.run_dir.tool_calls_jsonl,
                {
                    "node": "tester",
                    "label": label,
                    "command": resolved.command,
                    "raw_command": raw_cmd,
                    "ok": result.ok,
                    "exit_code": result.exit_code,
                    "duration_seconds": result.duration_seconds,
                },
            )
            if not result.ok:
                stderr_snippet = (str(result.metadata.get("stderr", ""))[-_STDERR_SNIPPET:]).strip()
                failed.append((resolved.command, result.exit_code, stderr_snippet))

        if failed:
            return _failure_update(failed, evidence_path=evidence_path)

        n_ran = len(commands) - len(skipped_for_empty_token)
        log.info(
            "tester.pass",
            workspace=workspace,
            n_commands=n_ran,
            n_skipped=len(skipped_for_empty_token),
        )
        # "pass" with everything skipped is really "no signal" — flag it as
        # ``unknown`` so a reviewer that asked for a real test pass can spot
        # it. Routing layer treats unknown the same as pass either way.
        status = "pass" if n_ran > 0 else "unknown"
        return {
            "local_test_status": status,
            "last_error": "",
            "current_step": "tester_done",
            "evidence": _append_evidence_pointer(state, evidence_path),
        }

    return _node


def _planned_commands(ctx: NodeContext) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for cmd in ctx.config.commands.lint:
        if cmd.strip():
            out.append(("lint", cmd))
    for cmd in ctx.config.commands.test:
        if cmd.strip():
            out.append(("test", cmd))
    return out


# ---------- changed-files / test-targets substitution -------------------- #


@dataclass
class _Substitutions:
    changed_files: list[str]
    changed_py_files: list[str]
    test_targets: list[str]


@dataclass
class _ResolvedCommand:
    command: str
    substituted: bool
    skip: bool = False
    skip_reason: str = ""


def _build_substitutions(*, changed_files: list[str], workspace: Path) -> _Substitutions:
    """Compute the three substitution sets for the current round.

    - ``changed_files``     — every non-directory entry the coder touched.
                              Useful for generic tools (grep, custom shell).
    - ``changed_py_files``  — Python-only subset. Use this for ruff/mypy/etc.
                              that error out on .md / .yaml / .toml inputs.
    - ``test_targets``      — heuristically-mapped test files. Use this for
                              pytest so only the relevant suite runs.

    The mapping is intentionally cheap and heuristic — we'd rather over-match
    (run a handful of extra tests) than miss coverage.
    """
    cleaned = [f for f in changed_files if f and not f.endswith("/")]
    changed_py = [f for f in cleaned if f.endswith(".py")]
    test_targets = _derive_test_targets(cleaned, workspace=workspace)
    return _Substitutions(
        changed_files=cleaned,
        changed_py_files=changed_py,
        test_targets=test_targets,
    )


def _derive_test_targets(changed: list[str], *, workspace: Path) -> list[str]:
    """Map a list of changed source paths to candidate test paths.

    Rules (applied in order):

    1. If the path itself looks like a test (``test_*.py`` / ``*_test.py``
       or anywhere under a ``tests/`` directory) → include verbatim.
    2. If the path is a Python module ``X.py`` → search for ``test_X.py``
       inside the workspace's ``tests/``, ``test/``, ``tests/unit/`` and
       ``tests/integration/`` dirs.
    3. Non-Python files (docs, configs, .yaml etc.) → contribute nothing.

    De-duplicated; preserves first-seen order so the command line is
    deterministic across runs.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(path: str) -> None:
        if path and path not in seen:
            seen.add(path)
            out.append(path)

    for raw in changed:
        path = raw.strip()
        if not path:
            continue
        name = Path(path).name

        if _looks_like_test(path):
            _add(path)
            continue

        if not path.endswith(".py"):
            continue

        stem = Path(name).stem
        if stem.startswith("test_") or stem.endswith("_test"):
            _add(path)
            continue

        # Same-directory companion (e.g. src/foo.py ↔ src/test_foo.py)
        sibling = Path(path).with_name(f"test_{stem}.py")
        if (workspace / sibling).exists():
            _add(str(sibling))
            continue

        # Walk the conventional tests/ dirs.
        for tdir in _TEST_SEARCH_DIRS:
            candidate_a = Path(tdir) / f"test_{stem}.py"
            if (workspace / candidate_a).exists():
                _add(str(candidate_a))
            candidate_b = Path(tdir) / f"{stem}_test.py"
            if (workspace / candidate_b).exists():
                _add(str(candidate_b))

    return out


def _looks_like_test(path: str) -> bool:
    posix = path.replace("\\", "/")
    parts = posix.split("/")
    if any(p in {"tests", "test"} for p in parts):
        return True
    base = Path(posix).name
    return base.startswith("test_") or base.endswith("_test.py")


def _resolve_command(raw: str, subs: _Substitutions) -> _ResolvedCommand:
    """Substitute template tokens in ``raw`` and decide whether to skip.

    Logic:

    - If no recognized token is present → return command unchanged.
    - If a present token has an empty value list → skip the command (we
      don't want ``pytest`` / ``ruff`` with no args to silently fall back to
      the full suite — that defeats the "fast local test" point AND in the
      ruff case it would lint every file in the repo).
    - Otherwise substitute and run.
    """
    has_changed = _TOKEN_CHANGED_FILES in raw
    has_changed_py = _TOKEN_CHANGED_PY_FILES in raw
    has_targets = _TOKEN_TEST_TARGETS in raw
    if not (has_changed or has_changed_py or has_targets):
        return _ResolvedCommand(command=raw, substituted=False)

    if has_changed and not subs.changed_files:
        return _ResolvedCommand(
            command=raw,
            substituted=False,
            skip=True,
            skip_reason="{changed_files} is empty (coder produced no diff)",
        )
    if has_changed_py and not subs.changed_py_files:
        return _ResolvedCommand(
            command=raw,
            substituted=False,
            skip=True,
            skip_reason=(
                "{changed_py_files} is empty (round had no Python-file "
                "changes; nothing to lint with a Python-only tool)"
            ),
        )
    if has_targets and not subs.test_targets:
        return _ResolvedCommand(
            command=raw,
            substituted=False,
            skip=True,
            skip_reason=(
                "{test_targets} is empty (no test files matched the changed "
                "source files; nothing meaningful to run)"
            ),
        )

    changed_joined = " ".join(shlex.quote(f) for f in subs.changed_files)
    changed_py_joined = " ".join(shlex.quote(f) for f in subs.changed_py_files)
    targets_joined = " ".join(shlex.quote(f) for f in subs.test_targets)
    out = (
        raw.replace(_TOKEN_CHANGED_PY_FILES, changed_py_joined)
        .replace(_TOKEN_CHANGED_FILES, changed_joined)
        .replace(_TOKEN_TEST_TARGETS, targets_joined)
    )
    return _ResolvedCommand(command=out, substituted=True)


def _propagate_coder_failure(state: AgentState, *, workspace: str) -> dict:
    """If coder never produced a workspace, treat that as a test failure so retry kicks in."""
    log.warning("tester.missing_workspace", workspace=workspace)
    return {
        "local_test_status": "fail",
        "last_error": (
            state.get("last_error")
            or f"No workspace to test against (workspace_path={workspace!r}). "
            "Coder did not produce a checkout."
        ),
        "current_step": "tester_skipped",
    }


def _append_evidence(
    ctx: NodeContext,
    path: Path,
    *,
    label: str,
    command: str,
    exit_code: int,
    duration: float,
    stdout: str,
    stderr: str,
) -> None:
    header = f"\n===== {label}: {command} =====\nexit_code={exit_code}  duration={duration:.2f}s\n"
    sections = [header]
    if stdout.strip():
        sections.append("--- stdout ---\n" + stdout.rstrip() + "\n")
    if stderr.strip():
        sections.append("--- stderr ---\n" + stderr.rstrip() + "\n")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("".join(sections))


def _failure_update(failed: list[tuple[str, int, str]], *, evidence_path: Path) -> dict:
    summary_lines = []
    for cmd, exit_code, stderr in failed:
        line = f"`{cmd}` exit={exit_code}"
        if stderr:
            line += f"\n  stderr (last {_STDERR_SNIPPET} chars):\n  " + stderr.replace("\n", "\n  ")
        summary_lines.append(line)
    summary = "\n\n".join(summary_lines)
    return {
        "local_test_status": "fail",
        "last_error": (
            f"{len(failed)} verification command(s) failed:\n\n{summary}\n\n"
            f"(Full output: {evidence_path})"
        ),
        "current_step": "tester_failed",
    }


def _append_evidence_pointer(state: AgentState, evidence_path: Path) -> list[str]:
    existing = list(state.get("evidence") or [])
    pointer = str(evidence_path)
    if pointer not in existing:
        existing.append(pointer)
    return existing
