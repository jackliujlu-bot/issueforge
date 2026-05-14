"""Artifact store layout."""

from __future__ import annotations

from pathlib import Path

from app.sandbox.artifact_store import ArtifactStore


def test_run_dir_creates_subdirs(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    rd = store.run_dir("acme--widget--issue-1")
    for sub in (rd.input_dir, rd.planning_dir, rd.execution_dir, rd.evidence_dir, rd.review_dir):
        assert sub.is_dir(), sub


def test_issue_key_replaces_slash() -> None:
    assert ArtifactStore.issue_key("acme/widget", 123) == "acme--widget--issue-123"


def test_write_text_creates_parents(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    rd = store.run_dir("k")
    p = store.write_text(rd.plan_md, "hello")
    assert p.read_text() == "hello"


def test_append_jsonl_and_log(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    rd = store.run_dir("k")
    store.append_jsonl(rd.tool_calls_jsonl, {"a": 1})
    store.append_jsonl(rd.tool_calls_jsonl, {"b": 2})
    store.append_log(rd.commands_log, "first")
    assert rd.tool_calls_jsonl.read_text().count("\n") == 2
    assert "first" in rd.commands_log.read_text()
