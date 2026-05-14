"""Executor registry and shared subprocess helper tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import load_config
from app.config.models import ExecutorEntry
from app.executors import (
    StubExecutor,
    build_executor,
    register_executor,
)
from app.executors._subprocess import substitute_args
from app.executors.base import (
    CodeExecutor,
    ExecutorRequest,
    ExecutorResult,
    UnknownExecutorError,
)


def test_substitute_args_replaces_placeholders() -> None:
    args = substitute_args(
        ["--workspace", "{workspace}", "--print", "{prompt}", "--model", "{model}"],
        prompt="hello",
        workspace=Path("/tmp/work"),
        model="m1",
    )
    assert args == ["--workspace", "/tmp/work", "--print", "hello", "--model", "m1"]


def test_substitute_args_unknown_placeholder_raises() -> None:
    with pytest.raises(ValueError):
        substitute_args(["{nope}"], prompt="x", workspace=None, model="")


def test_build_executor_returns_stub_by_default() -> None:
    cfg = load_config()
    exec_ = build_executor("stub", cfg)
    assert isinstance(exec_, StubExecutor)
    assert exec_.name == "stub"


def test_build_executor_unknown_name_raises() -> None:
    cfg = load_config()
    with pytest.raises(UnknownExecutorError):
        build_executor("does-not-exist", cfg)


def test_stub_executor_produces_plan() -> None:
    cfg = load_config()
    exec_ = build_executor("stub", cfg)
    result = exec_.run(ExecutorRequest(kind="plan", prompt="fix login bug"))
    assert result.ok
    assert "Plan (stub)" in result.output
    assert "fix login bug" in result.output


def test_register_executor_can_replace_for_tests() -> None:
    class _Recorder(CodeExecutor):
        name = "stub"

        def __init__(self, entry: ExecutorEntry) -> None:
            super().__init__(entry)
            self.calls: list[ExecutorRequest] = []

        def run(self, request: ExecutorRequest) -> ExecutorResult:
            self.calls.append(request)
            return ExecutorResult(ok=True, output="recorded")

    register_executor("stub", _Recorder)
    cfg = load_config()
    rec = build_executor("stub", cfg)
    out = rec.run(ExecutorRequest(kind="plan", prompt="x"))
    assert out.output == "recorded"

    # Restore
    register_executor("stub", StubExecutor)
