"""Code-executor layer.

A :class:`CodeExecutor` is anything that, given a prompt and a workspace
directory, produces code edits. The interface is intentionally narrow so we
can swap Cursor Agent for Claude Code / Codex / OpenHands / Shell tools without
touching LangGraph or Temporal.
"""

from app.executors.base import (
    CodeExecutor,
    ExecutorRequest,
    ExecutorResult,
    ExecutorTaskKind,
    UnknownExecutorError,
    build_executor,
    register_executor,
)
from app.executors.claude_code_executor import ClaudeCodeExecutor
from app.executors.codex_executor import CodexExecutor
from app.executors.cursor_agent_executor import CursorAgentExecutor
from app.executors.openhands_executor import OpenHandsExecutor
from app.executors.shell_executor import ShellExecutor
from app.executors.stub_executor import StubExecutor

__all__ = [
    "ClaudeCodeExecutor",
    "CodeExecutor",
    "CodexExecutor",
    "CursorAgentExecutor",
    "ExecutorRequest",
    "ExecutorResult",
    "ExecutorTaskKind",
    "OpenHandsExecutor",
    "ShellExecutor",
    "StubExecutor",
    "UnknownExecutorError",
    "build_executor",
    "register_executor",
]
