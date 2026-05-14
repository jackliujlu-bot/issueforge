"""Preflight validation.

`agent-worker doctor` walks every prerequisite the worker needs and reports
PASS / WARN / FAIL with actionable hints. With ``--fix`` it can also create
GitHub labels, init missing directories, and clone the repository.

The intent is that a user can run ``agent-worker doctor`` after ``init`` and
either get a clean bill of health or a checklist of remaining steps. No
silent failures.

Each check is a small function returning a :class:`DoctorReport`. The
top-level :func:`run_doctor` aggregates them; the CLI renders the result.
Tests can call :func:`run_doctor` with stub clients to exercise paths
without hitting the network.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from app.config import AppConfig
from app.config.models import ExecutorEntry


class CheckOutcome(StrEnum):
    """Outcome of a single check.

    PASS: the check succeeded, no action needed.
    WARN: not blocking but the user should know (e.g. Temporal not running).
    FAIL: blocking — the worker will refuse to operate without this fixed.
    SKIP: the check doesn't apply to this configuration.
    """

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass
class DoctorReport:
    """Structured result from one preflight check."""

    name: str
    outcome: CheckOutcome
    detail: str = ""
    hint: str = ""
    fix_applied: str = ""


@dataclass
class DoctorResult:
    """All checks plus the aggregate exit code."""

    reports: list[DoctorReport] = field(default_factory=list)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.reports if r.outcome == CheckOutcome.FAIL)

    @property
    def warn_count(self) -> int:
        return sum(1 for r in self.reports if r.outcome == CheckOutcome.WARN)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.reports if r.outcome == CheckOutcome.PASS)

    @property
    def exit_code(self) -> int:
        """0 if every check is PASS / SKIP / WARN; 1 if any FAIL."""
        return 1 if self.fail_count else 0

    def add(self, report: DoctorReport) -> None:
        self.reports.append(report)


# A shell runner that returns (returncode, stdout, stderr). Pluggable so tests
# can pass a fake.
ShellRunner = Callable[..., tuple[int, str, str]]


def _default_shell(
    cmd: list[str], *, cwd: Path | None = None, timeout: float = 30.0
) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired as exc:
        return 124, "", str(exc)
    return completed.returncode, completed.stdout, completed.stderr


def run_doctor(
    config: AppConfig,
    *,
    fix: bool = False,
    check_temporal: bool = True,
    shell: ShellRunner | None = None,
) -> DoctorResult:
    """Run every preflight check and return the aggregate result.

    Args:
        config: resolved AppConfig.
        fix: when True, apply auto-fixes (create labels, init dirs, clone).
        check_temporal: when False, skip the Temporal reachability check.
            Useful for ``init`` time when the user hasn't started docker yet.
        shell: optional injected shell runner (for tests).
    """
    runner: ShellRunner = shell or _default_shell
    result = DoctorResult()

    # ---- Project intent ------------------------------------------------- #
    result.add(_check_project(config))

    # ---- Repo identity -------------------------------------------------- #
    result.add(_check_repo_identity(config))

    # ---- gh CLI + auth + repo access + push perm ------------------------ #
    gh_present = _check_gh_present(config, result, runner)
    if gh_present:
        gh_authed = _check_gh_auth(config, result, runner)
        if gh_authed and config.repo.slug:
            _check_repo_access(config, result, runner)
            _check_push_permission(config, result, runner)
            _check_required_labels(config, result, runner, fix=fix)

    # ---- Local checkout / scaffold target ------------------------------- #
    _check_local_path(config, result, runner, fix=fix)

    # ---- Base branch ---------------------------------------------------- #
    _check_base_branch(config, result, runner)

    # ---- Commands sanity ------------------------------------------------ #
    result.add(_check_commands(config))

    # ---- Executor binary ------------------------------------------------ #
    result.add(_check_executor_binary(config))

    # ---- Filesystem (artifact_root + checkpoint dir) -------------------- #
    _check_filesystem(config, result, fix=fix)

    # ---- Temporal ------------------------------------------------------- #
    if check_temporal:
        result.add(_check_temporal(config))

    return result


# --------------------------------------------------------------------------- #
#  Individual checks                                                          #
# --------------------------------------------------------------------------- #


def _check_project(config: AppConfig) -> DoctorReport:
    mode = config.project.mode
    desc_present = bool(config.project.description.strip())
    if not desc_present:
        return DoctorReport(
            name="project intent",
            outcome=CheckOutcome.WARN,
            detail=f"mode={mode}, no description set",
            hint=(
                'Add `project.description: "..."` so the planner has standing '
                "context about what this worker is for."
            ),
        )
    return DoctorReport(
        name="project intent",
        outcome=CheckOutcome.PASS,
        detail=f"mode={mode}",
    )


def _check_repo_identity(config: AppConfig) -> DoctorReport:
    if config.repo.slug:
        return DoctorReport(
            name="repo identity",
            outcome=CheckOutcome.PASS,
            detail=config.repo.slug,
        )
    return DoctorReport(
        name="repo identity",
        outcome=CheckOutcome.FAIL,
        detail="repo.owner / repo.name not set",
        hint="Set them in your project YAML or via AGENT_WORKER__REPO__OWNER / __NAME.",
    )


def _check_gh_present(config: AppConfig, result: DoctorResult, runner: ShellRunner) -> bool:
    binary = config.github.cli
    location = shutil.which(binary)
    if not location:
        result.add(
            DoctorReport(
                name="gh CLI installed",
                outcome=CheckOutcome.FAIL,
                detail=f"{binary!r} not on PATH",
                hint=("Install GitHub CLI from https://cli.github.com and run `gh auth login`."),
            )
        )
        return False
    result.add(
        DoctorReport(
            name="gh CLI installed",
            outcome=CheckOutcome.PASS,
            detail=location,
        )
    )
    return True


def _check_gh_auth(config: AppConfig, result: DoctorResult, runner: ShellRunner) -> bool:
    rc, stdout, stderr = runner([config.github.cli, "auth", "status"], timeout=15)
    if rc != 0:
        result.add(
            DoctorReport(
                name="gh authenticated",
                outcome=CheckOutcome.FAIL,
                detail=(stderr or stdout).strip().splitlines()[0]
                if (stderr or stdout)
                else "auth status non-zero",
                hint="Run `gh auth login` and retry.",
            )
        )
        return False
    # gh prints "Logged in to github.com account <user>" on stderr historically.
    text = (stderr + "\n" + stdout).strip()
    first_line = text.splitlines()[0] if text else "ok"
    result.add(
        DoctorReport(
            name="gh authenticated",
            outcome=CheckOutcome.PASS,
            detail=first_line,
        )
    )
    return True


def _check_repo_access(config: AppConfig, result: DoctorResult, runner: ShellRunner) -> None:
    rc, stdout, stderr = runner(
        [
            config.github.cli,
            "repo",
            "view",
            config.repo.slug,
            "--json",
            "name,owner,visibility,defaultBranchRef",
        ],
        timeout=20,
    )
    if rc != 0:
        result.add(
            DoctorReport(
                name="GitHub repo accessible",
                outcome=CheckOutcome.FAIL,
                detail=(stderr or stdout).strip().splitlines()[0]
                if (stderr or stdout)
                else "gh repo view failed",
                hint=(
                    f"Confirm {config.repo.slug!r} exists and your token has "
                    "read access. Private repos require the `repo` scope."
                ),
            )
        )
        return
    try:
        payload = json.loads(stdout)
        default_branch = payload.get("defaultBranchRef", {}).get("name", "?") if payload else "?"
    except json.JSONDecodeError:
        default_branch = "?"
    result.add(
        DoctorReport(
            name="GitHub repo accessible",
            outcome=CheckOutcome.PASS,
            detail=f"default branch on remote: {default_branch}",
        )
    )


def _check_push_permission(config: AppConfig, result: DoctorResult, runner: ShellRunner) -> None:
    rc, stdout, _ = runner(
        [
            config.github.cli,
            "api",
            f"repos/{config.repo.slug}",
            "--jq",
            ".permissions",
        ],
        timeout=20,
    )
    if rc != 0:
        result.add(
            DoctorReport(
                name="push permission on repo",
                outcome=CheckOutcome.WARN,
                detail="could not determine permissions",
                hint=(
                    "API call failed; you may not be authenticated for this "
                    "repo. The agent will fail at push time if push access "
                    "isn't granted."
                ),
            )
        )
        return
    try:
        perms = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        perms = {}
    can_push = bool(perms.get("push"))
    is_admin = bool(perms.get("admin"))
    if can_push:
        suffix = " (admin)" if is_admin else ""
        result.add(
            DoctorReport(
                name="push permission on repo",
                outcome=CheckOutcome.PASS,
                detail=f"push=true{suffix}",
            )
        )
    else:
        result.add(
            DoctorReport(
                name="push permission on repo",
                outcome=CheckOutcome.FAIL,
                detail=f"permissions={perms}",
                hint=(
                    "You don't have push access. Either request collaborator "
                    "rights, or work on a fork (set repo.push_remote and "
                    "clone_url accordingly)."
                ),
            )
        )


def _check_required_labels(
    config: AppConfig,
    result: DoctorResult,
    runner: ShellRunner,
    *,
    fix: bool,
) -> None:
    cfg = config.github
    required = [
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
    rc, stdout, _ = runner(
        [
            config.github.cli,
            "label",
            "list",
            "--repo",
            config.repo.slug,
            "--limit",
            "200",
            "--json",
            "name",
        ],
        timeout=30,
    )
    if rc != 0:
        result.add(
            DoctorReport(
                name="required labels",
                outcome=CheckOutcome.WARN,
                detail="could not list labels",
                hint="Re-check repo access; we'll create labels on first run.",
            )
        )
        return
    try:
        existing = {entry["name"] for entry in json.loads(stdout or "[]")}
    except (json.JSONDecodeError, KeyError, TypeError):
        existing = set()
    missing = [lab for lab in required if lab not in existing]
    if not missing:
        result.add(
            DoctorReport(
                name="required labels",
                outcome=CheckOutcome.PASS,
                detail=f"all {len(required)} present",
            )
        )
        return
    if not fix:
        result.add(
            DoctorReport(
                name="required labels",
                outcome=CheckOutcome.WARN,
                detail=f"missing {len(missing)}: {', '.join(missing)}",
                hint="Run `agent-worker doctor --fix` to create them.",
            )
        )
        return
    created: list[str] = []
    failed: list[str] = []
    for label in missing:
        rc, _, err = runner(
            [
                config.github.cli,
                "label",
                "create",
                label,
                "--repo",
                config.repo.slug,
                "--color",
                "ededed",
            ],
            timeout=20,
        )
        if rc == 0:
            created.append(label)
        else:
            failed.append(f"{label} ({err.strip().splitlines()[0] if err else 'error'})")
    detail = f"created {len(created)}/{len(missing)}"
    if failed:
        result.add(
            DoctorReport(
                name="required labels",
                outcome=CheckOutcome.WARN,
                detail=detail + f"; failed: {', '.join(failed)}",
                fix_applied=", ".join(created),
            )
        )
    else:
        result.add(
            DoctorReport(
                name="required labels",
                outcome=CheckOutcome.PASS,
                detail=detail,
                fix_applied=", ".join(created),
            )
        )


def _check_local_path(
    config: AppConfig,
    result: DoctorResult,
    runner: ShellRunner,
    *,
    fix: bool,
) -> None:
    if config.sandbox.mode != "worktree" and config.project.mode != "scaffold":
        # Only worktree mode and scaffold mode require the local checkout.
        result.add(
            DoctorReport(
                name="local checkout",
                outcome=CheckOutcome.SKIP,
                detail=f"sandbox.mode={config.sandbox.mode}, project.mode={config.project.mode}",
            )
        )
        return

    local = config.repo.local_path
    if not local:
        result.add(
            DoctorReport(
                name="local checkout",
                outcome=CheckOutcome.FAIL,
                detail="repo.local_path not set",
                hint=(
                    "Set repo.local_path in your YAML to an absolute checkout "
                    "of the target repo. Re-run `agent-worker init` to do this "
                    "interactively."
                ),
            )
        )
        return
    path = Path(local).expanduser()
    if path.exists() and (path / ".git").exists():
        result.add(
            DoctorReport(
                name="local checkout",
                outcome=CheckOutcome.PASS,
                detail=str(path),
            )
        )
        return
    if config.project.mode == "scaffold":
        if not fix:
            result.add(
                DoctorReport(
                    name="local checkout",
                    outcome=CheckOutcome.WARN,
                    detail=f"{path} not a git repo (scaffold mode)",
                    hint="Run `agent-worker doctor --fix` to `git init` it.",
                )
            )
            return
        path.mkdir(parents=True, exist_ok=True)
        rc, _, err = runner(["git", "init", "--initial-branch", config.repo.base_branch], cwd=path)
        if rc != 0:
            result.add(
                DoctorReport(
                    name="local checkout",
                    outcome=CheckOutcome.FAIL,
                    detail=f"git init failed: {err.strip().splitlines()[0] if err else 'unknown'}",
                )
            )
            return
        result.add(
            DoctorReport(
                name="local checkout",
                outcome=CheckOutcome.PASS,
                detail=f"{path} (initialized)",
                fix_applied=f"git init {path}",
            )
        )
        return

    # Existing mode + path missing or not a git repo.
    if not fix:
        clone_url = config.repo.resolved_clone_url or "(set repo.clone_url)"
        result.add(
            DoctorReport(
                name="local checkout",
                outcome=CheckOutcome.FAIL,
                detail=f"{path} missing or not a git repo",
                hint=(f"Run `agent-worker doctor --fix` to clone {clone_url} into {path}."),
            )
        )
        return
    if not config.repo.resolved_clone_url:
        result.add(
            DoctorReport(
                name="local checkout",
                outcome=CheckOutcome.FAIL,
                detail="cannot clone — repo.clone_url empty and owner/name unset",
            )
        )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not (path / ".git").exists():
        result.add(
            DoctorReport(
                name="local checkout",
                outcome=CheckOutcome.FAIL,
                detail=f"{path} exists but is not a git repo; remove it manually before retrying.",
            )
        )
        return
    rc, _, err = runner(["git", "clone", config.repo.resolved_clone_url, str(path)], timeout=600)
    if rc != 0:
        result.add(
            DoctorReport(
                name="local checkout",
                outcome=CheckOutcome.FAIL,
                detail=f"git clone failed: {err.strip().splitlines()[0] if err else 'unknown'}",
            )
        )
        return
    result.add(
        DoctorReport(
            name="local checkout",
            outcome=CheckOutcome.PASS,
            detail=f"{path} (cloned)",
            fix_applied=f"git clone {config.repo.resolved_clone_url}",
        )
    )


def _check_base_branch(config: AppConfig, result: DoctorResult, runner: ShellRunner) -> None:
    if not config.repo.local_path:
        result.add(
            DoctorReport(
                name="base branch resolvable",
                outcome=CheckOutcome.SKIP,
                detail="no local_path",
            )
        )
        return
    path = Path(config.repo.local_path).expanduser()
    if not (path / ".git").exists():
        result.add(
            DoctorReport(
                name="base branch resolvable",
                outcome=CheckOutcome.SKIP,
                detail="local checkout missing or not git",
            )
        )
        return
    base = config.repo.base_branch
    for ref in (base, f"refs/heads/{base}", f"refs/remotes/origin/{base}"):
        rc, _, _ = runner(["git", "rev-parse", "--verify", "--quiet", ref], cwd=path)
        if rc == 0:
            result.add(
                DoctorReport(
                    name="base branch resolvable",
                    outcome=CheckOutcome.PASS,
                    detail=f"{ref}",
                )
            )
            return
    result.add(
        DoctorReport(
            name="base branch resolvable",
            outcome=CheckOutcome.FAIL,
            detail=f"{base!r} not found in {path}",
            hint=f"Run `git -C {path} fetch origin {base}`.",
        )
    )


def _check_commands(config: AppConfig) -> DoctorReport:
    cmds = config.commands
    populated = [
        name
        for name, value in (
            ("setup", cmds.setup),
            ("lint", cmds.lint),
            ("test", cmds.test),
            ("build", cmds.build),
        )
        if value
    ]
    if config.project.mode == "scaffold":
        return DoctorReport(
            name="commands populated",
            outcome=CheckOutcome.SKIP,
            detail="scaffold mode: commands optional until code exists",
        )
    if not populated:
        return DoctorReport(
            name="commands populated",
            outcome=CheckOutcome.WARN,
            detail="no setup/lint/test/build commands set",
            hint=(
                "The tester node will be a no-op. Add `commands.test: [...]` "
                "in your YAML so the agent has a quality gate."
            ),
        )
    return DoctorReport(
        name="commands populated",
        outcome=CheckOutcome.PASS,
        detail=", ".join(populated),
    )


def _check_executor_binary(config: AppConfig) -> DoctorReport:
    name = config.executor.default
    entry: ExecutorEntry = config.executor.entry(name)
    if name == "stub":
        return DoctorReport(
            name="default executor binary",
            outcome=CheckOutcome.PASS,
            detail="stub (no external binary needed)",
        )
    binary = entry.command
    if not binary:
        return DoctorReport(
            name="default executor binary",
            outcome=CheckOutcome.FAIL,
            detail=f"executor.{name}.command is empty",
        )
    location = shutil.which(binary)
    if not location:
        return DoctorReport(
            name="default executor binary",
            outcome=CheckOutcome.WARN,
            detail=f"{binary!r} not on PATH",
            hint=(
                f"Either install the {name} CLI, or set "
                f"executor.{name}.command (or env override) to its path. "
                "If you only want to dry-run, use `executor.default: stub`."
            ),
        )
    return DoctorReport(
        name="default executor binary",
        outcome=CheckOutcome.PASS,
        detail=f"{name}: {location}",
    )


def _check_filesystem(config: AppConfig, result: DoctorResult, *, fix: bool) -> None:
    artifact_root = Path(config.system.artifact_root).expanduser()
    if not artifact_root.exists():
        if fix:
            artifact_root.mkdir(parents=True, exist_ok=True)
            result.add(
                DoctorReport(
                    name="artifact_root writable",
                    outcome=CheckOutcome.PASS,
                    detail=str(artifact_root),
                    fix_applied="mkdir -p",
                )
            )
        else:
            result.add(
                DoctorReport(
                    name="artifact_root writable",
                    outcome=CheckOutcome.WARN,
                    detail=f"{artifact_root} does not exist yet",
                    hint="Run `agent-worker doctor --fix` to create it.",
                )
            )
    elif not os.access(artifact_root, os.W_OK):
        result.add(
            DoctorReport(
                name="artifact_root writable",
                outcome=CheckOutcome.FAIL,
                detail=f"{artifact_root} not writable",
            )
        )
    else:
        result.add(
            DoctorReport(
                name="artifact_root writable",
                outcome=CheckOutcome.PASS,
                detail=str(artifact_root),
            )
        )

    checkpoint_db = Path(config.langgraph.checkpoint_db).expanduser()
    parent = checkpoint_db.parent
    if not parent.exists():
        if fix:
            parent.mkdir(parents=True, exist_ok=True)
            result.add(
                DoctorReport(
                    name="checkpoint dir writable",
                    outcome=CheckOutcome.PASS,
                    detail=str(parent),
                    fix_applied="mkdir -p",
                )
            )
        else:
            result.add(
                DoctorReport(
                    name="checkpoint dir writable",
                    outcome=CheckOutcome.WARN,
                    detail=f"{parent} does not exist yet",
                    hint="Run `agent-worker doctor --fix` to create it.",
                )
            )
    else:
        result.add(
            DoctorReport(
                name="checkpoint dir writable",
                outcome=CheckOutcome.PASS,
                detail=str(parent),
            )
        )


def _check_temporal(config: AppConfig) -> DoctorReport:
    host = config.workflow.temporal.host
    if ":" not in host:
        return DoctorReport(
            name="Temporal reachable",
            outcome=CheckOutcome.WARN,
            detail=f"malformed host {host!r}",
        )
    name, _, port_str = host.rpartition(":")
    try:
        port = int(port_str)
    except ValueError:
        return DoctorReport(
            name="Temporal reachable",
            outcome=CheckOutcome.WARN,
            detail=f"malformed port in {host!r}",
        )
    try:
        with socket.create_connection((name, port), timeout=2.0):
            pass
    except OSError as exc:
        return DoctorReport(
            name="Temporal reachable",
            outcome=CheckOutcome.WARN,
            detail=f"{host} unreachable: {exc}",
            hint="Run `docker compose up -d` to start a local Temporal cluster.",
        )
    return DoctorReport(
        name="Temporal reachable",
        outcome=CheckOutcome.PASS,
        detail=host,
    )
