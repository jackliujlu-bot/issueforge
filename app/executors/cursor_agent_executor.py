"""Cursor Agent (cursor-agent CLI) executor.

The default executor in Phase 1. We invoke the headless ``cursor-agent`` CLI
with the prompt rendered through ``args_template``. The user can override the
binary path and the argument template entirely from YAML / env, so this also
works for forks or alternative Cursor distributions.

Example (default config):

    cursor-agent --print "<prompt>"

Example (with a model and workspace):

    args_template:
      - "--workspace"
      - "{workspace}"
      - "--model"
      - "{model}"
      - "--print"
      - "{prompt}"
"""

from __future__ import annotations

from app.executors._subprocess import run_cli, standard_result, substitute_args
from app.executors.base import (
    CodeExecutor,
    ExecutorRequest,
    ExecutorResult,
    register_executor,
)


class CursorAgentExecutor(CodeExecutor):
    name = "cursor"

    def run(self, request: ExecutorRequest) -> ExecutorResult:
        cmd = self.entry.command or "cursor-agent"
        args = substitute_args(
            self.entry.args_template or ["--print", "{prompt}"],
            prompt=request.prompt,
            workspace=request.workspace,
            model=self.entry.model,
        )
        exit_code, stdout, stderr, duration = run_cli(
            command=cmd,
            args=args,
            cwd=request.workspace,
            env=request.env,
            timeout=self.entry.timeout_seconds,
        )
        return standard_result(
            request,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            extra_metadata={"command": cmd, "args": args},
        )


register_executor("cursor", CursorAgentExecutor)
