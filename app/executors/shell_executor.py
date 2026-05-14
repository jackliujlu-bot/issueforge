"""Shell executor: runs the prompt verbatim through a shell.

Useful for deterministic test/lint/build steps that the tester node delegates
to the executor layer (so all command execution is uniformly captured).
"""

from __future__ import annotations

from app.executors._subprocess import run_cli, standard_result, substitute_args
from app.executors.base import (
    CodeExecutor,
    ExecutorRequest,
    ExecutorResult,
    register_executor,
)


class ShellExecutor(CodeExecutor):
    name = "shell"

    def run(self, request: ExecutorRequest) -> ExecutorResult:
        cmd = self.entry.command or "bash"
        args = substitute_args(
            self.entry.args_template or ["-lc", "{prompt}"],
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


register_executor("shell", ShellExecutor)
