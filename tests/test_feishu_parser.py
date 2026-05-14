"""Feishu message parsing."""

from __future__ import annotations

from app.feishu.message_parser import parse_feishu_message


def test_parses_full_message() -> None:
    raw = """
    repo: acme/widget
    priority: high
    labels: bug, login
    title: Fix 401 on token expiry
    task:
    Token expired path returns 500 instead of 401.
    Steps:
    1. add test
    2. fix handler
    """
    parsed = parse_feishu_message(raw)
    assert parsed.repo == "acme/widget"
    assert parsed.priority == "high"
    assert parsed.labels == ["bug", "login"]
    assert parsed.title == "Fix 401 on token expiry"
    assert "Steps:" in parsed.body


def test_parses_body_only_message() -> None:
    parsed = parse_feishu_message("just a one-liner")
    assert parsed.body == "just a one-liner"
    assert parsed.title.startswith("just a one-liner")


def test_handles_empty_input() -> None:
    parsed = parse_feishu_message("")
    assert parsed.body == ""
    assert parsed.title == ""
