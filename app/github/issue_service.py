"""GitHub Issue operations: read, comment, label transitions.

All methods accept the issue number; the repo slug is taken from the injected
:class:`RepoConfig` so every callsite is repo-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config.models import GitHubConfig, RepoConfig
from app.github._gh_cli import GhClient
from app.observability import get_logger

log = get_logger(__name__)


@dataclass
class Issue:
    number: int
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    state: str = "open"
    url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class GitHubIssueService:
    def __init__(
        self,
        repo: RepoConfig,
        github: GitHubConfig,
        *,
        client: GhClient | None = None,
    ) -> None:
        self._repo = repo
        self._gh_cfg = github
        self._client = client or GhClient(binary=github.cli)

    @property
    def repo_slug(self) -> str:
        if not self._repo.slug:
            raise ValueError(
                "repo.owner and repo.name must be set (in YAML or env) before "
                "calling GitHubIssueService."
            )
        return self._repo.slug

    def fetch(self, issue_number: int) -> Issue:
        payload = self._client.run_json(
            "issue",
            "view",
            str(issue_number),
            "--repo",
            self.repo_slug,
            "--json",
            "number,title,body,labels,state,url",
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected gh issue view payload: {payload!r}")
        return Issue(
            number=int(payload.get("number", issue_number)),
            title=str(payload.get("title", "")),
            body=str(payload.get("body") or ""),
            labels=[lab["name"] for lab in payload.get("labels", []) if "name" in lab],
            state=str(payload.get("state", "open")).lower(),
            url=str(payload.get("url", "")),
            raw=payload,
        )

    def list_open_with_label(self, label: str, *, limit: int = 100) -> list[Issue]:
        """Return open issues carrying ``label`` (no body fetch, light JSON).

        Used by the dispatcher to scan for tasks. We deliberately don't pull
        the body here: most issues have multi-KB bodies, and the dispatcher
        only needs (number, labels). The IssueAgentWorkflow does its own
        ``load_issue`` activity once dispatched.
        """
        if not label:
            return []
        payload = self._client.run_json(
            "issue",
            "list",
            "--repo",
            self.repo_slug,
            "--state",
            "open",
            "--label",
            label,
            "--limit",
            str(limit),
            "--json",
            "number,title,labels,url",
            timeout=60.0,
        )
        if not isinstance(payload, list):
            return []
        out: list[Issue] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            out.append(
                Issue(
                    number=int(raw.get("number", 0)),
                    title=str(raw.get("title", "")),
                    body="",
                    labels=[
                        lab["name"]
                        for lab in raw.get("labels", [])
                        if isinstance(lab, dict) and "name" in lab
                    ],
                    state="open",
                    url=str(raw.get("url", "")),
                    raw=raw,
                )
            )
        return out

    def list_open_with_any_label(
        self, labels: list[str], *, limit_per_label: int = 50
    ) -> list[Issue]:
        """Return open issues carrying *any* of ``labels`` (OR semantics).

        ``gh issue list --label X --label Y`` is AND, not OR — so we call
        once per label and de-dup by issue number. Used by the orphan-recovery
        scan (in-flight labels like ``agent-running``/``agent-planning`` are
        OR-joined into one set).

        Each issue's ``labels`` list reflects what GitHub actually has on the
        issue at fetch time (so the caller can route by current label even if
        the requested label has since changed).
        """
        if not labels:
            return []
        seen: dict[int, Issue] = {}
        for label in labels:
            for issue in self.list_open_with_label(label, limit=limit_per_label):
                if issue.number > 0:
                    seen.setdefault(issue.number, issue)
        return list(seen.values())

    def comment(self, issue_number: int, body: str) -> None:
        self._client.run_checked(
            "issue",
            "comment",
            str(issue_number),
            "--repo",
            self.repo_slug,
            "--body-file",
            "-",
            input_text=body,
            timeout=120,
        )
        log.info("issue.commented", issue=issue_number, repo=self.repo_slug, length=len(body))

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        if not labels:
            return
        self._client.run_checked(
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            self.repo_slug,
            "--add-label",
            ",".join(labels),
        )
        log.info("issue.labels.added", issue=issue_number, labels=labels)

    def remove_labels(self, issue_number: int, labels: list[str]) -> None:
        if not labels:
            return
        self._client.run_checked(
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            self.repo_slug,
            "--remove-label",
            ",".join(labels),
        )
        log.info("issue.labels.removed", issue=issue_number, labels=labels)

    def transition_label(
        self,
        issue_number: int,
        *,
        to_label: str,
        from_labels: list[str] | None = None,
    ) -> None:
        """Set ``to_label`` and remove any of ``from_labels`` that exist.

        Used for state-machine transitions like agent:planning -> agent:coding.
        Failures on remove are non-fatal because the issue may not have the prior label.
        """
        try:
            if from_labels:
                self.remove_labels(issue_number, from_labels)
        except Exception as exc:
            log.warning(
                "issue.label.remove_failed",
                issue=issue_number,
                labels=from_labels,
                error=str(exc),
            )
        self.add_labels(issue_number, [to_label])

    def all_agent_labels(self) -> list[str]:
        """Convenience: return the full set of agent:* labels for cleanup."""
        cfg = self._gh_cfg
        return [
            cfg.issue_label_todo,
            cfg.issue_label_queued,
            cfg.issue_label_running,
            cfg.issue_label_planning,
            cfg.issue_label_coding,
            cfg.issue_label_testing,
            cfg.issue_label_pr_created,
            cfg.issue_label_ci_running,
            cfg.issue_label_review,
            cfg.issue_label_blocked,
            cfg.issue_label_failed,
            cfg.issue_label_done,
        ]
