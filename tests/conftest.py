"""Test fixtures.

We isolate every test from the user's environment by:
  - resetting the cached config singleton
  - pointing artifact_root and checkpoint_db at temporary directories
  - clearing AGENT_WORKER__* env vars
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import AppConfig, load_config, reset_cached_config


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("AGENT_WORKER__") or key in {
            "AGENT_WORKER_CONFIG",
            "ARTIFACT_ROOT",
            "TEMPORAL_HOST",
            "TEMPORAL_NAMESPACE",
            "TEMPORAL_TASK_QUEUE",
            "LANGGRAPH_CHECKPOINT_DB",
            "CURSOR_AGENT_BIN",
        }:
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DB", str(tmp_path / "checkpoints" / "lg.sqlite"))
    monkeypatch.setenv("AGENT_WORKER__LANGGRAPH__CHECKPOINT_BACKEND", "memory")
    monkeypatch.setenv("AGENT_WORKER__EXECUTOR__DEFAULT", "stub")
    monkeypatch.setenv("AGENT_WORKER__REPO__OWNER", "acme")
    monkeypatch.setenv("AGENT_WORKER__REPO__NAME", "widget")
    # ``load_config`` calls ``load_dotenv(override=False)`` which would
    # otherwise re-read the repo's own ``.env`` and bring back a project YAML
    # (e.g. ``AGENT_WORKER_CONFIG=configs/examples/dimos.yaml``) — that overlay
    # would change ``stop_after``, ``sandbox.mode`` etc. and break Phase 1
    # tests in ways unrelated to the change under test. Setting the env var
    # to "" makes load_dotenv a no-op for this key (override=False) and the
    # loader treats falsy values as "no project YAML".
    monkeypatch.setenv("AGENT_WORKER_CONFIG", "")
    reset_cached_config()
    yield
    reset_cached_config()


@pytest.fixture
def app_config() -> AppConfig:
    return load_config()
