"""GitHub Actions CI status reader.

Phase 4 implementation. Two public surfaces:

- :meth:`GitHubCIService.poll` — a single status check used by the Temporal
  ``fetch_ci_status`` activity. It returns a :class:`CIPollResult` that the
  workflow can use to decide between "wait longer" / "succeed" / "fail and
  hand back to LangGraph".
- :meth:`GitHubCIService.fetch_failure_logs` — pulls the failing-job logs for
  a run so the next coder round has concrete error context.

We deliberately do not block in this module. Long waits live in the workflow
(``workflow.sleep`` between polls). Activities stay short and idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.config.models import GitHubConfig, RepoConfig
from app.github._gh_cli import GhClient, GhCommandError
from app.observability import get_logger

log = get_logger(__name__)

CIStatus = Literal["unknown", "queued", "in_progress", "success", "failure", "cancelled", "skipped"]


@dataclass
class CIRun:
    id: int
    status: str  # workflow_runs.status: queued / in_progress / completed
    conclusion: str  # success / failure / cancelled / skipped / "" while running
    branch: str
    head_sha: str
    url: str
    workflow_name: str = ""
    created_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CIPollResult:
    """Output of one CI status poll."""

    status: str  # pending | passed | failed | unknown
    completed: bool
    summary: str = ""
    failed_jobs: list[str] = field(default_factory=list)
    log_excerpts: dict[str, str] = field(default_factory=dict)
    runs: list[CIRun] = field(default_factory=list)


_LOG_EXCERPT_CHARS = 2000  # per-job clip we surface to coder retries


class GitHubCIService:
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
            raise ValueError("repo.owner / repo.name not configured.")
        return self._repo.slug

    # ---------- public --------------------------------------------------- #

    def poll(
        self,
        *,
        pr_number: int | None = None,
        head_branch: str | None = None,
    ) -> CIPollResult:
        """Return a snapshot of the CI state for a PR or branch.

        ``status`` is one of:
            - ``pending``  — at least one run is still running.
            - ``passed``   — all completed runs succeeded, none are running.
            - ``failed``   — at least one run finished with failure / cancelled.
            - ``unknown``  — no runs found (CI hasn't started yet, or branch
              has no workflows). Caller should usually treat this as "wait".

        Workflow names matching ``github.ci_ignore_workflows`` (case-insensitive
        substring) are filtered out before the judgment — useful for excluding
        human-gated workflows like a Codex-reviewed "Auto Merge" job.
        """
        runs = self.list_runs_for_branch_or_pr(pr_number=pr_number, head_branch=head_branch)
        if not runs:
            return CIPollResult(status="unknown", completed=False, runs=[])

        ignored = [s.lower() for s in (self._gh_cfg.ci_ignore_workflows or []) if s]
        if ignored:
            filtered: list[CIRun] = []
            skipped: list[str] = []
            for run in runs:
                name_low = (run.workflow_name or "").lower()
                if name_low and any(pat in name_low for pat in ignored):
                    skipped.append(run.workflow_name)
                    continue
                filtered.append(run)
            if skipped:
                log.info("ci.ignored_workflows", skipped=sorted(set(skipped)))
            runs = filtered
            if not runs:
                # All known runs were ignored — treat as "no real CI signal yet".
                return CIPollResult(status="unknown", completed=False, runs=[])

        # Collapse to the most recent run per workflow (avoid stale failures
        # shadowing a successful re-run).
        latest: dict[str, CIRun] = {}
        for run in runs:
            key = run.workflow_name or str(run.id)
            existing = latest.get(key)
            if existing is None or run.created_at > existing.created_at:
                latest[key] = run

        any_pending = any(r.status != "completed" for r in latest.values())
        if any_pending:
            return CIPollResult(
                status="pending",
                completed=False,
                summary=_pending_summary(list(latest.values())),
                runs=list(latest.values()),
            )

        failed = [r for r in latest.values() if r.conclusion not in ("success", "skipped")]
        if failed:
            log_excerpts: dict[str, str] = {}
            for run in failed:
                try:
                    log_excerpts[run.workflow_name or str(run.id)] = self.fetch_failure_logs(run.id)
                except Exception as exc:  # gh CLI failure, network blip
                    log_excerpts[run.workflow_name or str(run.id)] = (
                        f"(failed to fetch logs: {exc})"
                    )
            return CIPollResult(
                status="failed",
                completed=True,
                summary=_failure_summary(failed),
                failed_jobs=[r.workflow_name or str(r.id) for r in failed],
                log_excerpts=log_excerpts,
                runs=list(latest.values()),
            )

        return CIPollResult(
            status="passed",
            completed=True,
            summary=_passed_summary(list(latest.values())),
            runs=list(latest.values()),
        )

    def list_runs_for_branch_or_pr(
        self,
        *,
        pr_number: int | None = None,
        head_branch: str | None = None,
        limit: int = 20,
    ) -> list[CIRun]:
        if not pr_number and not head_branch:
            return []
        if pr_number:
            # `gh run list --workflow ... --branch <head>` works, but
            # `gh pr checks <pr>` is per-PR. We use `gh run list --branch <head>`
            # because we already know the head branch (from state.branch).
            args = ["run", "list", "--repo", self.repo_slug, "--branch", head_branch or ""]
            if not head_branch:
                # fall back to gh pr checks (returns less info but suffices)
                return self._list_runs_via_pr_checks(pr_number)
        else:
            args = ["run", "list", "--repo", self.repo_slug, "--branch", head_branch or ""]
        args += [
            "--limit",
            str(limit),
            "--json",
            "databaseId,status,conclusion,headBranch,headSha,url,workflowName,createdAt",
        ]
        try:
            payload = self._client.run_json(*args)
        except GhCommandError as exc:
            log.warning("ci.list_failed", error=exc.stderr.strip()[:300])
            return []
        if not isinstance(payload, list):
            return []
        return [
            CIRun(
                id=int(p["databaseId"]),
                status=str(p.get("status", "")),
                conclusion=str(p.get("conclusion") or ""),
                branch=str(p.get("headBranch") or ""),
                head_sha=str(p.get("headSha") or ""),
                url=str(p.get("url") or ""),
                workflow_name=str(p.get("workflowName") or ""),
                created_at=str(p.get("createdAt") or ""),
                raw=p,
            )
            for p in payload
        ]

    def fetch_failure_logs(self, run_id: int) -> str:
        try:
            raw = self._client.run_checked(
                "run",
                "view",
                str(run_id),
                "--repo",
                self.repo_slug,
                "--log-failed",
                timeout=180,
            )
        except GhCommandError as exc:
            return f"(could not fetch logs for run {run_id}: {exc.stderr.strip()[:200]})"
        # Logs can be huge; clip the tail (failures are usually at the bottom).
        if len(raw) > _LOG_EXCERPT_CHARS:
            raw = "...[earlier output truncated]...\n" + raw[-_LOG_EXCERPT_CHARS:]
        return raw

    # ---------- internal ------------------------------------------------- #

    def _list_runs_via_pr_checks(self, pr_number: int) -> list[CIRun]:
        try:
            payload = self._client.run_json(
                "pr",
                "checks",
                str(pr_number),
                "--repo",
                self.repo_slug,
                "--json",
                "name,state,bucket,link,workflow",
            )
        except GhCommandError as exc:
            log.warning("ci.pr_checks_failed", error=exc.stderr.strip()[:300])
            return []
        if not isinstance(payload, list):
            return []
        out: list[CIRun] = []
        for c in payload:
            bucket = str(c.get("bucket", "")).lower()
            status = "completed" if bucket in ("pass", "fail", "skipping") else "in_progress"
            conclusion = {
                "pass": "success",
                "fail": "failure",
                "skipping": "skipped",
            }.get(bucket, "")
            out.append(
                CIRun(
                    id=0,
                    status=status,
                    conclusion=conclusion,
                    branch="",
                    head_sha="",
                    url=str(c.get("link", "")),
                    workflow_name=str(c.get("workflow") or c.get("name", "")),
                    created_at="",
                    raw=c,
                )
            )
        return out


def _pending_summary(runs: list[CIRun]) -> str:
    in_flight = [r.workflow_name or str(r.id) for r in runs if r.status != "completed"]
    return f"CI still running: {', '.join(in_flight) or '(unknown jobs)'}"


def _failure_summary(failed: list[CIRun]) -> str:
    lines = [f"- {r.workflow_name or r.id}: {r.conclusion} ({r.url})" for r in failed]
    return "CI failed:\n" + "\n".join(lines)


def _passed_summary(runs: list[CIRun]) -> str:
    names = [r.workflow_name or str(r.id) for r in runs]
    return f"CI passed: {', '.join(names)}"
