"""Git worktree manager.

A worktree is the *only* place a coder agent is allowed to mutate files. Each
issue gets a stable per-issue path so a restarted worker reuses the same
checkout instead of creating a parallel one. Branches are similarly stable,
named ``<working_branch_prefix>/issue-<n>``.

The :class:`GitWorktreeBackend` implementation shells out to ``git worktree``
against ``repo.local_path`` (the user's existing checkout). It is idempotent:

- If the worktree path is already a worktree of the source repo, reuse it.
- If the branch exists already, attach a worktree to it.
- Otherwise create branch + worktree from ``base_branch``.

Cleanup is best-effort: ``git worktree remove --force`` followed by best-effort
branch deletion (only if the branch matches our prefix, to avoid stomping
human branches).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.observability import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Worktree:
    branch: str
    path: Path
    base_branch: str
    repo_slug: str
    # The actual ref ``git worktree add`` was started from (e.g. ``feipeng/dev``).
    # Empty when we attached to an existing branch rather than creating one.
    base_ref: str = ""


class WorktreeBackend(Protocol):
    def ensure(
        self, *, repo_slug: str, branch: str, base_branch: str, target_path: Path
    ) -> Worktree: ...
    def cleanup(self, worktree: Worktree) -> None: ...


class GitCommandError(RuntimeError):
    def __init__(self, cmd: list[str], returncode: int, stdout: str, stderr: str) -> None:
        super().__init__(
            f"`{' '.join(cmd)}` failed with code {returncode}\n"
            f"stdout: {stdout.strip()}\nstderr: {stderr.strip()}"
        )
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _git(
    cwd: Path,
    *args: str,
    timeout: int = 60,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
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
    return completed.returncode, completed.stdout, completed.stderr


def _git_checked(
    cwd: Path,
    *args: str,
    timeout: int = 60,
    extra_env: dict[str, str] | None = None,
) -> str:
    rc, stdout, stderr = _git(cwd, *args, timeout=timeout, extra_env=extra_env)
    if rc != 0:
        raise GitCommandError(["git", *args], rc, stdout, stderr)
    return stdout


# `git worktree add` on a large LFS-backed repo can take several minutes
# because LFS smudge runs synchronously per file. We:
#   1. bump the timeout to 10 minutes (plenty of head-room),
#   2. skip LFS smudge entirely — the agent rarely needs binary assets and
#      LFS files would just bloat the worktree anyway.
_WORKTREE_ADD_TIMEOUT_SECONDS = 600
_WORKTREE_ADD_ENV = {"GIT_LFS_SKIP_SMUDGE": "1"}


class GitWorktreeBackend:
    """Filesystem-backed git worktree provisioner.

    Args:
        source_repo: path to an existing checkout of the business repo. Required;
            we never clone here. (Cloning belongs to repo provisioning, not
            sandbox creation, and would surprise users by adding remotes.)
        preferred_remote: name of the remote we'll eventually push to. When
            resolving a bare branch name (e.g. ``"dev"``), we prefer this
            remote's tracking branch over the local branch so the worktree's
            base matches the eventual PR target — and unpushed local commits
            on the developer's machine don't accidentally leak into the
            agent's PR.
        auto_fetch_base: when True (default), each fresh worktree creation
            runs ``git fetch <preferred_remote> <base_branch>`` first so the
            agent always branches from the very latest remote ref. Set
            False to inherit the local branch instead — useful if you want
            the agent to build on top of work you haven't pushed yet.
    """

    def __init__(
        self,
        source_repo: Path,
        *,
        preferred_remote: str = "",
        auto_fetch_base: bool = True,
    ) -> None:
        self.source_repo = Path(source_repo).expanduser().resolve()
        self.preferred_remote = preferred_remote
        self.auto_fetch_base = auto_fetch_base
        if not (self.source_repo / ".git").exists():
            raise FileNotFoundError(
                f"GitWorktreeBackend requires an existing checkout at "
                f"{self.source_repo}; .git not found. Set repo.local_path "
                "in your project YAML."
            )

    def ensure(
        self, *, repo_slug: str, branch: str, base_branch: str, target_path: Path
    ) -> Worktree:
        target = Path(target_path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)

        # Reuse first — never re-fetch on attach, the agent might be mid-coding
        # and a sudden base move would surprise it.
        if target.exists():
            self._validate_existing_worktree(target, branch)
            self._clear_stale_lock(target)
            resolved_base = self._resolve_branch(base_branch, prefer_remote=True) or ""
            log.info(
                "worktree.reuse",
                path=str(target),
                branch=branch,
                source=str(self.source_repo),
            )
            return Worktree(
                branch=branch,
                path=target,
                base_branch=base_branch,
                repo_slug=repo_slug,
                base_ref=resolved_base,
            )

        # Creating a fresh worktree: if the agent branch doesn't already exist,
        # this is the moment to refresh the base from the remote so we don't
        # branch off a stale local main.
        if not self._branch_exists(branch) and self.auto_fetch_base:
            self._fetch_base(base_branch)

        resolved_base = self._resolve_branch(base_branch, prefer_remote=True) or ""

        if self._branch_exists(branch):
            _git_checked(
                self.source_repo,
                "worktree",
                "add",
                str(target),
                branch,
                timeout=_WORKTREE_ADD_TIMEOUT_SECONDS,
                extra_env=_WORKTREE_ADD_ENV,
            )
            log.info("worktree.attached_to_existing_branch", path=str(target), branch=branch)
        else:
            if not resolved_base:
                raise GitCommandError(
                    ["git", "rev-parse", "--verify", base_branch],
                    1,
                    "",
                    f"base branch {base_branch!r} not found in {self.source_repo}. "
                    "Fetch it before running the agent.",
                )
            _git_checked(
                self.source_repo,
                "worktree",
                "add",
                "-b",
                branch,
                str(target),
                resolved_base,
                timeout=_WORKTREE_ADD_TIMEOUT_SECONDS,
                extra_env=_WORKTREE_ADD_ENV,
            )
            log.info(
                "worktree.created",
                path=str(target),
                branch=branch,
                base=base_branch,
                base_ref=resolved_base,
            )

        return Worktree(
            branch=branch,
            path=target,
            base_branch=base_branch,
            repo_slug=repo_slug,
            base_ref=resolved_base,
        )

    def _fetch_base(self, base_branch: str) -> None:
        """Refresh the remote-tracking ref for ``base_branch`` from the
        configured push_remote (or ``origin`` if unset).

        Best-effort: a missing remote / no network is logged at warn level
        but never crashes. The caller will fall back to whatever local ref
        we can resolve.
        """
        remote = self.preferred_remote
        if not remote:
            rc, stdout, _ = _git(self.source_repo, "remote")
            remotes = stdout.split()
            if not remotes:
                log.info(
                    "worktree.fetch_skipped",
                    reason="no git remote configured",
                )
                return
            remote = "origin" if "origin" in remotes else remotes[0]

        log.info(
            "worktree.fetch_base.start",
            remote=remote,
            base_branch=base_branch,
            source=str(self.source_repo),
        )
        rc, stdout, stderr = _git(
            self.source_repo,
            "fetch",
            "--prune",
            "--no-tags",
            remote,
            base_branch,
            timeout=180,
        )
        if rc != 0:
            log.warning(
                "worktree.fetch_base.failed",
                remote=remote,
                base_branch=base_branch,
                stderr=stderr.strip()[:300],
            )
            return
        log.info("worktree.fetch_base.ok", remote=remote, base_branch=base_branch)

    def cleanup(self, worktree: Worktree) -> None:
        if not worktree.path.exists():
            return
        rc, _, stderr = _git(self.source_repo, "worktree", "remove", "--force", str(worktree.path))
        if rc != 0:
            log.warning(
                "worktree.remove_failed",
                path=str(worktree.path),
                stderr=stderr.strip(),
            )
        else:
            log.info("worktree.removed", path=str(worktree.path), branch=worktree.branch)

    def _branch_exists(self, ref: str) -> bool:
        # _branch_exists is used to decide whether to create a fresh agent
        # branch vs reuse one — that lookup should only see LOCAL branches.
        # A remote-tracking ref would falsely report "branch exists" and
        # cause ``git worktree add`` to try to attach to the remote ref.
        if not ref:
            return False
        rc, _, _ = _git(self.source_repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{ref}")
        return rc == 0

    def _resolve_branch(self, ref: str, *, prefer_remote: bool = False) -> str | None:
        """Return the most-specific ref that ``ref`` resolves to, or None.

        When ``prefer_remote=True`` (used for the agent's base branch), the
        remote-tracking ref wins over the local branch — so the agent
        branches from ``origin/main`` rather than the developer's stale
        local ``main``. When False (legacy), local branches win first.
        """
        if not ref:
            return None

        # Already-qualified refs (``feipeng/dev``, ``refs/remotes/foo/bar``)
        # always go through git's own resolution.
        if "/" in ref:
            rc, _, _ = _git(self.source_repo, "rev-parse", "--verify", "--quiet", ref)
            if rc == 0:
                return ref

        rc, stdout, _ = _git(self.source_repo, "remote")
        remotes = stdout.split()

        def _check_local() -> str | None:
            rc, _, _ = _git(
                self.source_repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{ref}"
            )
            return ref if rc == 0 else None

        def _check_remote() -> str | None:
            ordered: list[str] = []
            if self.preferred_remote and self.preferred_remote in remotes:
                ordered.append(self.preferred_remote)
            for r in remotes:
                if r not in ordered:
                    ordered.append(r)
            for remote in ordered:
                candidate = f"refs/remotes/{remote}/{ref}"
                rc, _, _ = _git(self.source_repo, "rev-parse", "--verify", "--quiet", candidate)
                if rc == 0:
                    return f"{remote}/{ref}"
            return None

        if prefer_remote:
            return _check_remote() or _check_local()
        return _check_local() or _check_remote()

    def _clear_stale_lock(self, target: Path) -> None:
        """Remove a stale ``index.lock`` left behind by an interrupted git/cursor run.

        Stale here means ``mtime`` more than 60 seconds ago. If it's fresher we
        leave it (likely another live process). The lock lives under
        ``<source_repo>.git/worktrees/<name>/index.lock`` for worktrees.
        """
        import time

        # Look up the actual gitdir; for worktrees this is under the source repo.
        rc, stdout, _ = _git(target, "rev-parse", "--git-dir")
        if rc != 0:
            return
        git_dir = Path(stdout.strip())
        if not git_dir.is_absolute():
            git_dir = (target / git_dir).resolve()
        lock = git_dir / "index.lock"
        if not lock.exists():
            return
        age = time.time() - lock.stat().st_mtime
        if age < 60:
            log.warning(
                "worktree.lock_present",
                path=str(lock),
                age_seconds=round(age, 1),
            )
            return
        try:
            lock.unlink()
            log.warning("worktree.stale_lock_removed", path=str(lock), age_seconds=round(age, 1))
        except OSError as exc:
            log.warning("worktree.stale_lock_remove_failed", path=str(lock), error=str(exc))

    def _validate_existing_worktree(self, target: Path, expected_branch: str) -> None:
        if not (target / ".git").exists():
            raise GitCommandError(
                ["check-worktree"],
                1,
                "",
                f"{target} exists but is not a git worktree. Remove it manually.",
            )
        rc, stdout, stderr = _git(target, "rev-parse", "--abbrev-ref", "HEAD")
        if rc != 0:
            raise GitCommandError(["git", "rev-parse", "--abbrev-ref", "HEAD"], rc, stdout, stderr)
        actual_branch = stdout.strip()
        if actual_branch != expected_branch:
            raise GitCommandError(
                ["check-worktree-branch"],
                1,
                "",
                f"worktree at {target} is on branch {actual_branch!r}, "
                f"expected {expected_branch!r}. Remove or rename it manually.",
            )


class WorktreeManager:
    """Owns the ``worktree_root`` directory and dispatches to a backend.

    The manager is the public face; ``backend`` is the strategy. If no backend
    is provided we raise loudly on first use — that catches misconfiguration
    (sandbox.mode=worktree without repo.local_path set) instead of silently
    producing no-op semantics.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        backend: GitWorktreeBackend | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._backend = backend

    @property
    def backend(self) -> GitWorktreeBackend:
        if self._backend is None:
            raise NotImplementedError(
                "WorktreeManager has no backend wired up. Either set "
                "repo.local_path and sandbox.mode=worktree, or inject a "
                "backend in tests."
            )
        return self._backend

    def path_for(self, issue_key: str) -> Path:
        # issue_key is already filesystem-safe (uses '--' as separator).
        return self.root / issue_key

    def ensure(self, *, repo_slug: str, branch: str, base_branch: str, issue_key: str) -> Worktree:
        target = self.path_for(issue_key)
        return self.backend.ensure(
            repo_slug=repo_slug,
            branch=branch,
            base_branch=base_branch,
            target_path=target,
        )

    def cleanup(self, worktree: Worktree) -> None:
        self.backend.cleanup(worktree)
