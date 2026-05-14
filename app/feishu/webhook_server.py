"""Feishu webhook server (FastAPI).

Phase 4 implementation. The flow:

1. POST arrives at ``feishu.webhook_path``.
2. Optional HMAC-SHA256 signature check against ``feishu.verify_token``.
3. Feishu URL-verification challenge requests are echoed back.
4. Otherwise we parse the user-typed message body into a structured task.
5. Create a GitHub issue (``feishu.default_labels`` applied).
6. If ``feishu.auto_start_workflow``, dispatch a Temporal workflow keyed by
   ``stable_workflow_id(repo, issue_number)``.

The endpoint returns a small JSON receipt so the user gets feedback in Feishu
chat (Feishu bots typically just acknowledge silently — this is for debugging).

Importing this module fails fast with a clear hint if the optional
``feishu`` extra (FastAPI + uvicorn) is not installed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any

from app.config import get_config
from app.config.models import AppConfig
from app.feishu.message_parser import FeishuTaskRequest, parse_feishu_message
from app.observability import get_logger

log = get_logger(__name__)


@dataclass
class _DispatchResult:
    issue_number: int
    issue_url: str
    repo: str
    workflow_id: str | None


def build_app(feishu_or_config: Any) -> Any:
    """Construct the FastAPI application.

    Accepts either a :class:`FeishuConfig` (legacy, for tests) or a full
    :class:`AppConfig` (preferred — gives us repo + temporal config to
    dispatch with).
    """
    try:
        from fastapi import FastAPI, HTTPException, Request
    except ImportError as exc:  # pragma: no cover - install hint
        raise ImportError(
            "FastAPI is required for the Feishu webhook server. "
            "Install with `pip install issue-agent-worker[feishu]`."
        ) from exc

    if isinstance(feishu_or_config, AppConfig):
        cfg = feishu_or_config
    else:
        cfg = get_config()
        # If caller passed a FeishuConfig, allow it to override the global.
        cfg = cfg.model_copy(update={"feishu": feishu_or_config})

    app = FastAPI(title="issue-agent-worker — feishu webhook")

    @app.post(cfg.feishu.webhook_path)
    async def receive(request: Request) -> dict[str, Any]:
        if not cfg.feishu.enabled:
            raise HTTPException(503, "feishu integration disabled in config")

        body = await request.body()
        if cfg.feishu.verify_token:
            timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
            nonce = request.headers.get("X-Lark-Request-Nonce", "")
            signature = request.headers.get("X-Lark-Signature", "")
            if not _verify_signature(
                token=cfg.feishu.verify_token,
                timestamp=timestamp,
                nonce=nonce,
                body=body,
                given=signature,
            ):
                log.warning("feishu.bad_signature")
                raise HTTPException(401, "invalid signature")

        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"invalid JSON: {exc}") from exc

        # Feishu sometimes pings the endpoint with a URL-verification challenge.
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}

        text = _extract_text(payload)
        parsed = parse_feishu_message(text)
        # Apply config defaults.
        repo = parsed.repo or cfg.feishu.default_repo
        if not repo:
            raise HTTPException(
                400,
                "no repo specified in message and feishu.default_repo is empty",
            )
        if not parsed.body.strip():
            raise HTTPException(400, "no task body found in message")

        labels = sorted({*parsed.labels, *cfg.feishu.default_labels})
        title = parsed.title or parsed.body.splitlines()[0][:120]

        dispatched = await _dispatch(
            cfg=cfg,
            request=parsed,
            repo_slug=repo,
            title=title,
            labels=labels,
        )

        return {
            "received": True,
            "issue_number": dispatched.issue_number,
            "issue_url": dispatched.issue_url,
            "repo": dispatched.repo,
            "workflow_id": dispatched.workflow_id,
            "labels": labels,
            "title": title,
        }

    @app.get("/health")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _verify_signature(*, token: str, timestamp: str, nonce: str, body: bytes, given: str) -> bool:
    if not given:
        return False
    msg = (timestamp + nonce + token).encode("utf-8") + body
    expected = hmac.new(token.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


def _extract_text(payload: dict[str, Any]) -> str:
    """Best-effort extraction of the user-typed message from a Feishu payload.

    Accepts:
      - ``{"text": "..."}`` (testing convenience)
      - ``{"event": {"message": {"content": "{\"text\":\"...\"}"}}}`` (Feishu v2)
    """
    if "text" in payload and isinstance(payload["text"], str):
        return payload["text"]
    event = payload.get("event") or {}
    message = event.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        try:
            inner = json.loads(content)
        except json.JSONDecodeError:
            return content
        if isinstance(inner, dict):
            return str(inner.get("text") or inner.get("content") or "")
    return ""


async def _dispatch(
    *,
    cfg: AppConfig,
    request: FeishuTaskRequest,
    repo_slug: str,
    title: str,
    labels: list[str],
) -> _DispatchResult:
    """Create a GitHub issue + (optionally) start the Temporal workflow.

    Blocking work goes through ``asyncio.to_thread`` so the ASGI loop stays
    responsive even when ``gh`` takes a few seconds to call GitHub.
    """
    from app.github.issue_service import GitHubIssueService

    owner, _, name = repo_slug.partition("/")
    if not owner or not name:
        raise ValueError(f"repo must be 'owner/name', got {repo_slug!r}")
    repo_cfg = cfg.repo.model_copy(update={"owner": owner, "name": name})

    issues = GitHubIssueService(repo_cfg, cfg.github)
    issue_number, issue_url = await asyncio.to_thread(
        _create_issue, issues=issues, title=title, body=request.body, labels=labels
    )

    workflow_id: str | None = None
    if cfg.feishu.auto_start_workflow:
        try:
            workflow_id = await _start_workflow(
                cfg=cfg.model_copy(update={"repo": repo_cfg}),
                issue_number=issue_number,
            )
        except Exception as exc:
            log.warning("feishu.workflow_start_failed", error=str(exc))
            workflow_id = None

    return _DispatchResult(
        issue_number=issue_number,
        issue_url=issue_url,
        repo=repo_slug,
        workflow_id=workflow_id,
    )


def _create_issue(
    *,
    issues: Any,
    title: str,
    body: str,
    labels: list[str],
) -> tuple[int, str]:
    """Thin sync wrapper. Calls ``gh issue create``."""
    payload = issues._client.run_checked(
        "issue",
        "create",
        "--repo",
        issues.repo_slug,
        "--title",
        title,
        "--body-file",
        "-",
        *(arg for lbl in labels for arg in ("--label", lbl)),
        input_text=body,
        timeout=120,
    ).strip()
    # gh issue create returns the URL of the created issue.
    issue_url = payload.splitlines()[-1].strip() if payload else ""
    issue_number = _parse_issue_number(issue_url)
    log.info("feishu.issue.created", number=issue_number, url=issue_url, repo=issues.repo_slug)
    return issue_number, issue_url


def _parse_issue_number(url: str) -> int:
    # https://github.com/<owner>/<repo>/issues/<n>
    if "/issues/" in url:
        try:
            return int(url.rstrip("/").split("/issues/", 1)[1].split("/")[0])
        except ValueError:
            return 0
    return 0


async def _start_workflow(*, cfg: AppConfig, issue_number: int) -> str | None:
    """Dispatch the Temporal workflow for ``issue_number``."""
    try:
        from app.temporal_app.client import start_issue_workflow
    except ImportError as exc:  # pragma: no cover
        log.warning("feishu.temporal_unavailable", error=str(exc))
        return None
    # Suppress noisy temporal connection logs during webhook handling — the
    # dispatcher logs its own success line.
    logging.getLogger("temporalio").setLevel(logging.WARNING)
    handle, _outcome = await start_issue_workflow(cfg, issue_number=issue_number)
    return handle.id
