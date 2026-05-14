"""GitHub Pull Request operations.

Phase 3 implementation. Surfaces the minimum set of PR operations the
deliverer node needs:

- ``commit_changes``: stage + commit *inside the worktree* (no remote ops).
- ``push_branch``:    push the agent branch to ``repo.push_remote``.
- ``find_for_branch``: locate an existing PR for the agent branch.
- ``create_or_update``: open a new PR or refresh the body of an existing one.
- ``comment``:        attach a comment to the PR.
- ``enable_auto_merge``: turn on auto-merge so CI success auto-merges.
- ``merge``:          synchronous merge (used by tests / manual ops).

We deliberately don't try to be clever: the deliverer either creates a PR or
updates its body. State machine transitions live in the workflow.

All git operations against the worktree go through the same ``subprocess.run``
shape used by :mod:`app.sandbox.worktree`. The agent worktree is the working
copy that holds the change.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config.models import GitHubConfig, RepoConfig
from app.github._gh_cli import GhClient, GhCommandError
from app.observability import get_logger

log = get_logger(__name__)


@dataclass
class PullRequest:
    number: int
    url: str
    title: str
    head_branch: str
    base_branch: str
    state: str = "open"
    raw: dict[str, Any] = field(default_factory=dict)


class GitOperationError(RuntimeError):
    def __init__(self, cmd: list[str], returncode: int, stdout: str, stderr: str) -> None:
        super().__init__(
            f"`{' '.join(cmd)}` failed with code {returncode}\n"
            f"stdout: {stdout.strip()}\nstderr: {stderr.strip()}"
        )
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_GIT_OP_TIMEOUT = 600
# `git push` over HTTPS can hang for minutes on a flaky network before the
# kernel returns. We instead let git itself fail fast: if throughput drops
# below 1000 B/s for 30s, abort. Then we retry the whole push.
_GIT_PUSH_LOW_SPEED_LIMIT = 1000
_GIT_PUSH_LOW_SPEED_TIME = 30
_GIT_PUSH_RETRY_ATTEMPTS = 4
_GIT_PUSH_RETRY_BACKOFF_SECONDS = 5
# When the deliverer commits/pushes, we don't want the user's local git
# identity to depend on environment hygiene; let the user override these via
# env. If unset, fall back to the value baked into the worktree's parent repo.
_BOT_NAME_ENV = "AGENT_WORKER_GIT_AUTHOR_NAME"
_BOT_EMAIL_ENV = "AGENT_WORKER_GIT_AUTHOR_EMAIL"
_DEFAULT_BOT_NAME = "issue-agent-worker"
_DEFAULT_BOT_EMAIL = "issue-agent-worker@local"


def _run_git(
    cwd: Path,
    *args: str,
    timeout: int = _GIT_OP_TIMEOUT,
    extra_env: dict[str, str] | None = None,
    check: bool = True,
) -> str:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    if check and completed.returncode != 0:
        raise GitOperationError(
            ["git", *args], completed.returncode, completed.stdout, completed.stderr
        )
    return completed.stdout


class GitHubPRService:
    """Public surface used by the deliverer node and Temporal activities."""

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

    # ---------- git operations on the worktree ---------------------------- #

    def commit_changes(
        self,
        workspace: Path,
        *,
        files: list[str] | None,
        message: str,
        allow_empty: bool = False,
    ) -> str | None:
        """Stage ``files`` (or everything if None) and commit. Returns sha or None.

        Returns ``None`` and logs at info level if there's nothing to commit,
        so callers can decide whether to fail or push an existing branch.
        """
        if files is None:
            _run_git(workspace, "add", "-A")
        elif files:
            _run_git(workspace, "add", "--", *files)

        # Detect "nothing to commit".
        status_out = _run_git(workspace, "status", "--porcelain")
        if not status_out.strip() and not allow_empty:
            log.info("pr.commit.nothing_to_commit", workspace=str(workspace))
            return None

        author_name = os.environ.get(_BOT_NAME_ENV, _DEFAULT_BOT_NAME)
        author_email = os.environ.get(_BOT_EMAIL_ENV, _DEFAULT_BOT_EMAIL)
        extra_env = {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
        args = ["commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        _run_git(workspace, *args, extra_env=extra_env)
        sha = _run_git(workspace, "rev-parse", "HEAD").strip()
        log.info("pr.commit.ok", workspace=str(workspace), sha=sha)
        return sha

    def push_branch(
        self,
        workspace: Path,
        *,
        branch: str,
        remote: str | None = None,
        force: bool = False,
    ) -> None:
        """Push ``branch`` to ``remote``, retrying on transient network errors.

        On flaky HTTPS links to github.com a single ``git push`` can sit in a
        broken TLS connection for minutes. We pair git's own
        ``http.lowSpeedLimit`` / ``lowSpeedTime`` knobs (fail fast when the
        connection stalls) with up to 4 attempts at increasing backoff.
        """
        import time

        remote = remote or self._repo.push_remote or "origin"
        base_args = [
            "-c",
            f"http.lowSpeedLimit={_GIT_PUSH_LOW_SPEED_LIMIT}",
            "-c",
            f"http.lowSpeedTime={_GIT_PUSH_LOW_SPEED_TIME}",
            "push",
            "--set-upstream",
            remote,
            branch,
        ]
        if force:
            base_args.append("--force-with-lease")

        last_err: GitOperationError | None = None
        for attempt in range(1, _GIT_PUSH_RETRY_ATTEMPTS + 1):
            try:
                # Tight per-attempt timeout so a hung TCP connection doesn't
                # block the workflow for the full _GIT_OP_TIMEOUT budget.
                _run_git(workspace, *base_args, timeout=120)
                log.info(
                    "pr.push.ok",
                    branch=branch,
                    remote=remote,
                    force=force,
                    attempt=attempt,
                )
                return
            except GitOperationError as exc:
                last_err = exc
                if attempt == _GIT_PUSH_RETRY_ATTEMPTS:
                    break
                log.warning(
                    "pr.push.retry",
                    branch=branch,
                    remote=remote,
                    attempt=attempt,
                    error=str(exc).splitlines()[0][:200],
                )
                time.sleep(_GIT_PUSH_RETRY_BACKOFF_SECONDS * attempt)
        assert last_err is not None
        raise last_err

    # ---------- PR lifecycle ----------------------------------------------- #

    def find_for_branch(self, head_branch: str) -> PullRequest | None:
        payload = self._client.run_json(
            "pr",
            "list",
            "--repo",
            self.repo_slug,
            "--head",
            head_branch,
            "--state",
            "open",
            "--json",
            "number,url,title,headRefName,baseRefName,state",
        )
        if not isinstance(payload, list) or not payload:
            return None
        first = payload[0]
        return PullRequest(
            number=int(first["number"]),
            url=str(first["url"]),
            title=str(first.get("title", "")),
            head_branch=str(first.get("headRefName", head_branch)),
            base_branch=str(first.get("baseRefName", "")),
            state=str(first.get("state", "open")).lower(),
            raw=first,
        )

    def create_or_update(
        self,
        *,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        draft: bool | None = None,
    ) -> PullRequest:
        existing = self.find_for_branch(head_branch)
        if existing is not None:
            log.info("pr.update.existing", number=existing.number, head=head_branch)
            self._client.run_checked(
                "pr",
                "edit",
                str(existing.number),
                "--repo",
                self.repo_slug,
                "--title",
                title,
                "--body-file",
                "-",
                input_text=body,
                timeout=120,
            )
            return existing

        draft_flag = self._gh_cfg.pr_draft if draft is None else draft
        args = [
            "pr",
            "create",
            "--repo",
            self.repo_slug,
            "--head",
            head_branch,
            "--base",
            base_branch,
            "--title",
            title,
            "--body-file",
            "-",
        ]
        if draft_flag:
            args.append("--draft")
        url = self._client.run_checked(*args, input_text=body, timeout=120).strip()
        log.info("pr.create.ok", head=head_branch, base=base_branch, url=url)
        # `gh pr create` returns the URL on success; re-fetch to get the number.
        pr = self.find_for_branch(head_branch)
        if pr is None:
            # In rare cases (e.g. PR queued for review) `gh pr list` may not
            # see it immediately; surface the URL even without a number.
            return PullRequest(
                number=-1,
                url=url,
                title=title,
                head_branch=head_branch,
                base_branch=base_branch,
            )
        return pr

    def comment(self, pr_number: int, body: str) -> None:
        if not body.strip():
            return
        self._client.run_checked(
            "pr",
            "comment",
            str(pr_number),
            "--repo",
            self.repo_slug,
            "--body-file",
            "-",
            input_text=body,
            timeout=120,
        )
        log.info("pr.comment.ok", number=pr_number, length=len(body))

    def enable_auto_merge(self, pr_number: int, *, method: str = "squash") -> None:
        try:
            self._client.run_checked(
                "pr",
                "merge",
                str(pr_number),
                "--repo",
                self.repo_slug,
                f"--{method}",
                "--auto",
            )
            log.info("pr.auto_merge.enabled", number=pr_number, method=method)
        except GhCommandError as exc:
            # Auto-merge requires branch protection rules — fall back to a
            # log warning so the workflow can still mark the PR ready.
            log.warning(
                "pr.auto_merge.failed",
                number=pr_number,
                error=exc.stderr.strip()[:300],
            )

    def merge(self, pr_number: int, *, method: str = "squash") -> None:
        args = [
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            self.repo_slug,
            f"--{method}",
        ]
        if self._gh_cfg.delete_branch_after_merge:
            args.append("--delete-branch")
        self._client.run_checked(*args)
        log.info("pr.merge.ok", number=pr_number, method=method)
