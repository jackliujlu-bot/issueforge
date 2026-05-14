"""Interactive configuration wizard.

`agent-worker init` walks the user through the four questions that define a
deployment of the worker:

    1. What are we doing — optimizing an existing repo, or scaffolding a new one?
    2. Which GitHub repository (owner/name)?
    3. What's the baseline branch and where is the local checkout?
    4. How do we install / lint / test this codebase, and which executor runs?

The wizard then writes a project YAML under ``configs/<slug>.yaml`` and
optionally updates ``.env`` to point ``AGENT_WORKER_CONFIG`` at it.

Design notes:
    - All input/output goes through the :class:`WizardIO` protocol so tests
      can drive the wizard with canned answers.
    - The wizard never *requires* an external service. It will gladly write a
      YAML that fails ``doctor`` — that's the point: configure first, validate
      second. If you want a one-shot, use ``agent-worker bootstrap``.
    - Defaults are auto-detected (``gh repo view`` for the slug, git for the
      base branch, ``app.setup.detection`` for the commands) and surfaced as
      pre-filled values; the user can accept by pressing Enter.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import yaml

from app.setup.detection import (
    CommandSuggestion,
    detect_base_branch,
    detect_commands,
    detect_repo_from_gh_dir,
    detect_repo_slug_from_remote,
)

# Executor backends the wizard can offer. Order matters — first is default.
KNOWN_EXECUTORS = ["cursor", "claude_code", "codex", "openhands", "shell", "stub"]
KNOWN_SANDBOX_MODES = ["worktree", "local", "docker"]
KNOWN_PROJECT_MODES = ["existing", "scaffold"]

# Files in configs/ we treat as shipped infrastructure, not selectable project
# overlays. default.yaml is the always-loaded base; if a user names their own
# YAML this they're on their own.
RESERVED_CONFIG_FILENAMES = {"default.yaml"}


@dataclass(frozen=True)
class ConfigPreview:
    """Summary of an existing project YAML, shown in the import menu."""

    path: Path
    project_mode: str = ""
    project_description: str = ""
    repo_slug: str = ""
    base_branch: str = ""
    executor_default: str = ""

    def short(self) -> str:
        bits: list[str] = []
        if self.repo_slug:
            bits.append(self.repo_slug)
        if self.base_branch:
            bits.append(f"@{self.base_branch}")
        if self.project_mode:
            bits.append(f"mode={self.project_mode}")
        if self.executor_default:
            bits.append(f"executor={self.executor_default}")
        return ", ".join(bits) or "(empty overlay)"


@dataclass(frozen=True)
class ConfigChoice:
    """Outcome of the entry-point menu.

    - ``use_existing``: point AGENT_WORKER_CONFIG at ``source_path``; no edits.
    - ``clone_and_edit``: copy ``source_path`` to ``new_name`` so the user can
      tweak a copy without losing the template.
    - ``wizard``: fall through to the Q&A wizard.
    """

    strategy: Literal["use_existing", "clone_and_edit", "wizard"]
    source_path: Path | None = None
    new_name: str | None = None


@dataclass
class WizardAnswers:
    """Everything the wizard collected, in the shape we'll serialize to YAML.

    Kept as a flat dataclass (not nested by section) for ergonomic test
    construction; ``to_config_dict`` reshapes into the ``AppConfig`` tree.
    """

    project_mode: str = "existing"
    project_description: str = ""

    repo_owner: str = ""
    repo_name: str = ""
    repo_clone_url: str = ""
    repo_base_branch: str = "main"
    repo_local_path: str = ""

    commands_setup: list[str] = field(default_factory=list)
    commands_lint: list[str] = field(default_factory=list)
    commands_test: list[str] = field(default_factory=list)
    commands_build: list[str] = field(default_factory=list)

    executor_default: str = "cursor"
    sandbox_mode: str = "worktree"

    feishu_enabled: bool = False
    feishu_default_repo: str = ""

    # Where the wizard will write the YAML (relative to the project root).
    output_yaml_path: Path = field(default_factory=lambda: Path("configs/my-project.yaml"))
    # Whether to also update .env's AGENT_WORKER_CONFIG.
    update_dotenv: bool = True
    # Path to the .env file. Resolved relative to project root.
    dotenv_path: Path = field(default_factory=lambda: Path(".env"))

    def to_config_dict(self) -> dict[str, object]:
        """Reshape the flat answers into the nested AppConfig YAML layout."""
        return {
            "project": {
                "mode": self.project_mode,
                "description": self.project_description,
            },
            "repo": {
                "owner": self.repo_owner,
                "name": self.repo_name,
                "clone_url": self.repo_clone_url,
                "base_branch": self.repo_base_branch,
                "local_path": self.repo_local_path,
            },
            "commands": {
                "setup": list(self.commands_setup),
                "lint": list(self.commands_lint),
                "test": list(self.commands_test),
                "build": list(self.commands_build),
            },
            "executor": {"default": self.executor_default},
            "sandbox": {"mode": self.sandbox_mode},
            "feishu": {
                "enabled": self.feishu_enabled,
                "default_repo": self.feishu_default_repo,
            },
        }


class WizardIO(Protocol):
    """Abstract input/output, so tests can run the wizard headlessly."""

    def ask(self, prompt: str, *, default: str = "", choices: list[str] | None = None) -> str: ...
    def ask_bool(self, prompt: str, *, default: bool = False) -> bool: ...
    def info(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...


def run_wizard(
    io: WizardIO,
    *,
    project_root: Path,
    cwd: Path | None = None,
) -> WizardAnswers:
    """Drive the wizard end-to-end and return the collected answers.

    The caller is responsible for actually writing the YAML and updating
    ``.env`` — see :func:`write_project_yaml` and :func:`update_dotenv`. Keeping
    those two side-effects out of the wizard makes it easier to dry-run.
    """
    cwd = cwd or Path.cwd()
    answers = WizardAnswers()

    io.info("Welcome to issue-agent-worker setup.")
    io.info(
        "We'll write a project YAML under configs/. You can re-run this any "
        "time and overwrite the file."
    )

    # ---- Q1: what are we doing ------------------------------------------ #
    answers.project_mode = io.ask(
        "Project mode (existing = optimize a repo that already has code; "
        "scaffold = build from zero)",
        default="existing",
        choices=KNOWN_PROJECT_MODES,
    )
    answers.project_description = io.ask(
        "One-line description of what this worker is supposed to do "
        "(shown to the agent in every planner prompt)",
        default="",
    )

    # ---- Q2: which repo ------------------------------------------------- #
    detected_slug = detect_repo_from_gh_dir(cwd) or detect_repo_slug_from_remote(cwd)
    slug = io.ask(
        "GitHub repository (owner/name)",
        default=detected_slug,
    )
    if "/" not in slug:
        io.warn(f"'{slug}' is not a valid owner/name slug; you'll need to fix it later.")
        answers.repo_owner = slug
        answers.repo_name = ""
    else:
        answers.repo_owner, answers.repo_name = slug.split("/", 1)

    custom_clone_url = io.ask(
        "Custom clone URL (leave empty to derive git@github.com:owner/name.git)",
        default="",
    )
    answers.repo_clone_url = custom_clone_url

    # ---- Q3: baseline + checkout ---------------------------------------- #
    detected_base = detect_base_branch(cwd)
    answers.repo_base_branch = io.ask(
        "Base branch the agent's PRs will target",
        default=detected_base or "main",
    )

    if answers.project_mode == "existing":
        default_local_path = str(cwd) if (cwd / ".git").exists() else ""
        answers.repo_local_path = io.ask(
            "Absolute path to a local checkout of this repo "
            "(used for the worktree sandbox; doctor --fix can clone for you)",
            default=default_local_path,
        )
    else:
        # Scaffold mode: ask where to create the brand-new repo.
        answers.repo_local_path = io.ask(
            "Where should the worker create the new repo? "
            "(absolute path; doctor --fix will git init this directory)",
            default=str(cwd / answers.repo_name) if answers.repo_name else "",
        )

    # ---- Q4: commands --------------------------------------------------- #
    # Suggest based on the local checkout if we have one.
    suggestion = _suggest_commands(answers.repo_local_path, project_mode=answers.project_mode)
    if suggestion.toolchain != "unknown":
        io.info(f"Detected toolchain: {suggestion.toolchain}")
    elif answers.project_mode == "existing":
        io.warn(
            "Could not auto-detect a toolchain. You can either fill commands "
            "in now or edit the YAML afterwards."
        )

    answers.commands_setup = _ask_command_list(
        io, "Setup commands (run once before any agent round)", suggestion.setup
    )
    answers.commands_lint = _ask_command_list(
        io, "Lint commands (gates the tester node)", suggestion.lint
    )
    answers.commands_test = _ask_command_list(
        io, "Test commands (gates the tester node)", suggestion.test
    )
    answers.commands_build = _ask_command_list(io, "Build commands (optional)", suggestion.build)

    # ---- Executor ------------------------------------------------------- #
    answers.executor_default = io.ask(
        "Default code executor (cursor = cursor-agent CLI; stub = no-op for smoke tests)",
        default="cursor",
        choices=KNOWN_EXECUTORS,
    )

    # ---- Sandbox -------------------------------------------------------- #
    sandbox_default = "worktree" if answers.repo_local_path else "local"
    answers.sandbox_mode = io.ask(
        "Sandbox mode (worktree = isolated git checkout per issue; "
        "local = no checkout, planning-only)",
        default=sandbox_default,
        choices=KNOWN_SANDBOX_MODES,
    )

    # ---- Feishu --------------------------------------------------------- #
    answers.feishu_enabled = io.ask_bool(
        "Enable Feishu webhook intake? (off if you only use GitHub Issues directly)",
        default=False,
    )
    if answers.feishu_enabled and slug:
        answers.feishu_default_repo = slug

    # ---- Output paths --------------------------------------------------- #
    suggested_filename = f"{answers.repo_name or 'my-project'}.yaml"
    yaml_input = io.ask(
        "Where to write the project YAML",
        default=f"configs/{suggested_filename}",
    )
    answers.output_yaml_path = Path(yaml_input)
    answers.update_dotenv = io.ask_bool(
        "Update .env so AGENT_WORKER_CONFIG points at this YAML?",
        default=True,
    )

    return answers


def _suggest_commands(local_path: str, *, project_mode: str) -> CommandSuggestion:
    if not local_path:
        return CommandSuggestion(toolchain="unknown")
    path = Path(local_path).expanduser()
    if project_mode == "scaffold" and not path.exists():
        # Empty target — nothing to detect yet.
        return CommandSuggestion(toolchain="unknown")
    return detect_commands(path)


def _ask_command_list(io: WizardIO, label: str, suggested: list[str]) -> list[str]:
    """Prompt for a comma-separated command list, defaulting to ``suggested``."""
    default_str = " ; ".join(suggested)
    raw = io.ask(
        f"{label} (semicolon-separated; empty = no-op)",
        default=default_str,
    )
    if not raw.strip():
        return []
    return [seg.strip() for seg in raw.split(";") if seg.strip()]


# --------------------------------------------------------------------------- #
#  Discovery + entry-point menu                                               #
# --------------------------------------------------------------------------- #


def discover_existing_configs(project_root: Path) -> list[ConfigPreview]:
    """Return a previewed list of project YAMLs in ``configs/``.

    Excludes ``default.yaml`` (always-loaded base) and any non-mapping files.
    Files that fail to parse are silently skipped — the menu should never
    crash on a broken YAML, just hide it.
    """
    configs_dir = project_root / "configs"
    if not configs_dir.is_dir():
        return []

    previews: list[ConfigPreview] = []
    for path in sorted(configs_dir.glob("*.yaml")):
        if path.name in RESERVED_CONFIG_FILENAMES:
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(raw, dict):
            continue
        repo = raw.get("repo") or {}
        project = raw.get("project") or {}
        executor = raw.get("executor") or {}
        owner = str(repo.get("owner") or "")
        name = str(repo.get("name") or "")
        slug = f"{owner}/{name}" if owner and name else ""
        previews.append(
            ConfigPreview(
                path=path,
                project_mode=str(project.get("mode") or ""),
                project_description=str(project.get("description") or ""),
                repo_slug=slug,
                base_branch=str(repo.get("base_branch") or ""),
                executor_default=str(executor.get("default") or ""),
            )
        )
    return previews


def choose_config_strategy(io: WizardIO, project_root: Path) -> ConfigChoice:
    """Top-level menu shown by ``init`` / ``bootstrap`` before the wizard.

    If ``configs/`` already has at least one usable overlay, the user gets:
      [1..N] use one of the existing overlays as-is
      [c]    clone an existing overlay to a new file (then edit by hand)
      [w]    run the wizard from scratch
      [q]    abort

    If no overlays exist, returns ``ConfigChoice("wizard")`` immediately
    (no menu — there's nothing to choose between).
    """
    previews = discover_existing_configs(project_root)
    if not previews:
        io.info("No existing project YAMLs in configs/ — running the wizard.")
        return ConfigChoice(strategy="wizard")

    io.info("Found existing project YAMLs in configs/:")
    for i, p in enumerate(previews, start=1):
        io.info(f"  [{i}] {p.path.name}  — {p.short()}")
    io.info("  [c] clone one of the above to a new file (then edit by hand)")
    io.info("  [w] run the interactive wizard from scratch")
    io.info("  [q] abort")

    while True:
        raw = io.ask(
            "Pick an option (number to use as-is, or c / w / q)",
            default="w",
        )
        choice = raw.strip().lower()
        if choice == "q":
            return ConfigChoice(strategy="wizard", source_path=None)  # caller treats no-op as abort
        if choice == "w":
            return ConfigChoice(strategy="wizard")
        if choice == "c":
            base = _pick_template(io, previews)
            if base is None:
                continue
            new_name = io.ask(
                f"New filename under configs/ (e.g. my-{base.path.stem.replace('example-', '')}.yaml)",
                default=_suggest_clone_name(base.path, previews),
            )
            return ConfigChoice(strategy="clone_and_edit", source_path=base.path, new_name=new_name)
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(previews):
                return ConfigChoice(strategy="use_existing", source_path=previews[idx - 1].path)
        io.info(f"Unrecognized choice {choice!r}; try again.")


def _pick_template(io: WizardIO, previews: list[ConfigPreview]) -> ConfigPreview | None:
    """Sub-prompt: choose which existing YAML to clone from."""
    if len(previews) == 1:
        return previews[0]
    while True:
        raw = io.ask("Which YAML to clone from? (number)", default="1")
        if raw.strip().isdigit():
            idx = int(raw)
            if 1 <= idx <= len(previews):
                return previews[idx - 1]
        io.info(f"Pick a number 1..{len(previews)}.")


def _suggest_clone_name(source: Path, existing: list[ConfigPreview]) -> str:
    """Suggest a non-colliding filename derived from ``source``."""
    stem = source.stem.replace("example-", "")
    candidate = f"my-{stem}.yaml"
    taken = {p.path.name for p in existing}
    if candidate not in taken:
        return candidate
    n = 2
    while f"my-{stem}-{n}.yaml" in taken:
        n += 1
    return f"my-{stem}-{n}.yaml"


def clone_existing_yaml(source: Path, *, new_name: str, project_root: Path) -> Path:
    """Copy ``source`` to ``configs/<new_name>``. Returns the destination."""
    if not new_name.endswith(".yaml"):
        new_name = f"{new_name}.yaml"
    dest = (project_root / "configs" / new_name).resolve()
    if dest.exists():
        raise FileExistsError(
            f"{dest} already exists. Pick a different name or remove the file first."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return dest


# --------------------------------------------------------------------------- #
#  Side-effecting helpers (called after the wizard returns).                  #
# --------------------------------------------------------------------------- #


def write_project_yaml(answers: WizardAnswers, project_root: Path) -> Path:
    """Serialize ``answers`` to the chosen YAML path. Returns the resolved path."""
    out = (project_root / answers.output_yaml_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = answers.to_config_dict()
    text = _yaml_with_header(payload, mode=answers.project_mode)
    out.write_text(text, encoding="utf-8")
    return out


def _yaml_with_header(payload: dict[str, object], *, mode: str) -> str:
    """Pretty-print the YAML with a brief header comment for human readers."""
    header = (
        "# Generated by `agent-worker init`. Edit freely; values here override\n"
        "# configs/default.yaml. Re-run the wizard with `agent-worker init` to\n"
        "# regenerate (it will prompt before overwriting).\n"
        f"#\n# Project mode: {mode}\n#\n"
    )
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return header + body


def update_dotenv(env_path: Path, *, config_path: Path) -> None:
    """Idempotently set ``AGENT_WORKER_CONFIG=<config_path>`` in ``env_path``.

    Creates the file if missing. Replaces the line if present. Keeps every
    other line intact so user secrets aren't clobbered.
    """
    line = f"AGENT_WORKER_CONFIG={config_path}"
    if env_path.exists():
        text = env_path.read_text(encoding="utf-8")
        new_lines: list[str] = []
        replaced = False
        for raw_line in text.splitlines():
            if raw_line.startswith("AGENT_WORKER_CONFIG="):
                new_lines.append(line)
                replaced = True
            else:
                new_lines.append(raw_line)
        if not replaced:
            new_lines.append(line)
        env_path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
    else:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(line + "\n", encoding="utf-8")
