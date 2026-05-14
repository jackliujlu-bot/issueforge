"""Deterministic stub executor.

Used when no real coding agent is configured (e.g. CI smoke tests, unit tests,
or when the user just wants to see the system run end-to-end without burning
LLM credits). Always succeeds, emits a templated plan, never touches the
filesystem outside ``artifact_dir``.
"""

from __future__ import annotations

from app.executors.base import (
    CodeExecutor,
    ExecutorRequest,
    ExecutorResult,
    register_executor,
)


class StubExecutor(CodeExecutor):
    name = "stub"

    def run(self, request: ExecutorRequest) -> ExecutorResult:
        if request.kind == "plan":
            output = self._stub_plan(request.prompt)
        elif request.kind == "review":
            output = "Self-review (stub): no concerns flagged. Safe to proceed."
        else:
            output = (
                f"Stub executor invoked for kind={request.kind}. "
                "No real coding agent is configured."
            )
        return ExecutorResult(
            ok=True,
            output=output,
            metadata={"executor": "stub", "kind": request.kind},
        )

    @staticmethod
    def _stub_plan(prompt: str) -> str:
        excerpt = prompt.strip().splitlines()[:3]
        first_lines = "\n".join(f"> {line}" for line in excerpt) or "> (no issue body)"
        return (
            "# Plan (stub)\n"
            "\n"
            "**Source prompt (first 3 lines):**\n"
            f"{first_lines}\n"
            "\n"
            "## Objective\n"
            f"{prompt.strip().splitlines()[0][:200] if prompt.strip() else '(no objective)'}\n"
            "\n"
            "## Subtasks\n"
            "- T1: Investigate the issue and gather context.\n"
            "- T2: Identify minimal change set.\n"
            "- T3: Implement and test the change.\n"
            "- T4: Open a PR with evidence.\n"
            "\n"
            "## Verification\n"
            "- Run the project's `commands.test` block.\n"
            "- Confirm CI passes.\n"
            "\n"
            "_This plan was produced by the StubExecutor. "
            "Configure a real executor (e.g. `executor.default: cursor`) for production use._\n"
        )


register_executor("stub", StubExecutor)
