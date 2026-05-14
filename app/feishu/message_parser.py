"""Feishu message → structured task request.

The architecture doc proposes this format::

    @bot
    repo: <owner>/<name>
    priority: normal
    task:
    Fix the login endpoint that doesn't return 401 on expired tokens.
    Requirements:
    1. Add tests
    2. Do not change the database schema

We parse it into :class:`FeishuTaskRequest`. Robust to ordering and missing
fields; the only mandatory part is the task body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_FIELD_RE = re.compile(r"^\s*(repo|priority|labels|title|task)\s*:\s*(.*)$", re.IGNORECASE)
# Lines like "@bot" or "@机器人" appear at the top of Feishu messages; ignore
# them so the next field-shaped line still gets parsed.
_MENTION_RE = re.compile(r"^\s*@[\S]+\s*$")


@dataclass
class FeishuTaskRequest:
    repo: str = ""
    priority: str = "normal"
    title: str = ""
    body: str = ""
    labels: list[str] = field(default_factory=list)


def parse_feishu_message(text: str) -> FeishuTaskRequest:
    text = text or ""
    request = FeishuTaskRequest()
    body_lines: list[str] = []
    in_body = False

    for raw_line in text.splitlines():
        if in_body:
            body_lines.append(raw_line)
            continue

        m = _FIELD_RE.match(raw_line)
        if not m:
            stripped = raw_line.strip()
            if not stripped:
                continue
            if _MENTION_RE.match(stripped):
                continue
            body_lines.append(raw_line)
            in_body = True
            continue

        key, value = m.group(1).lower(), m.group(2).strip()
        if key == "repo":
            request.repo = value
        elif key == "priority":
            request.priority = value or "normal"
        elif key == "labels":
            request.labels = [tok.strip() for tok in value.split(",") if tok.strip()]
        elif key == "title":
            request.title = value
        elif key == "task":
            in_body = True
            if value:
                body_lines.append(value)

    request.body = "\n".join(body_lines).strip()
    if not request.title and request.body:
        request.title = request.body.splitlines()[0][:120].strip()
    return request
