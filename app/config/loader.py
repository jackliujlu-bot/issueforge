"""Configuration loader.

Merges YAML files, environment variables, and CLI overrides into a single
:class:`AppConfig`.

Precedence (last wins):
    1. ``configs/default.yaml`` (shipped defaults)
    2. project YAML (``--config`` flag, or ``AGENT_WORKER_CONFIG`` env var)
    3. environment variables
    4. dict overrides passed by CLI

Environment variable rules:
    - ``AGENT_WORKER__<SECTION>__<FIELD>`` maps to ``config.<section>.<field>``
      (double underscore as separator). Example: ``AGENT_WORKER__REPO__OWNER=acme``.
      Lists may be passed as JSON: ``AGENT_WORKER__COMMANDS__TEST='["pytest","ruff"]'``.
    - A handful of well-known short names are accepted for ergonomics; see
      :data:`SHORT_ENV_MAP` below.

The loader never reads the environment outside of this module so test fixtures
can fully control the config tree.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from app.config.models import AppConfig

ENV_PREFIX = "AGENT_WORKER__"

# Short-name aliases for common env vars. These exist so users don't need to
# remember the AGENT_WORKER__SECTION__FIELD pattern for the most-used knobs.
SHORT_ENV_MAP: dict[str, tuple[str, ...]] = {
    "AGENT_WORKER_CONFIG": ("__CONFIG_PATH__",),
    "ARTIFACT_ROOT": ("system", "artifact_root"),
    "TEMPORAL_HOST": ("workflow", "temporal", "host"),
    "TEMPORAL_NAMESPACE": ("workflow", "temporal", "namespace"),
    "TEMPORAL_TASK_QUEUE": ("workflow", "temporal", "task_queue"),
    "LANGGRAPH_CHECKPOINT_DB": ("langgraph", "checkpoint_db"),
    "CURSOR_AGENT_BIN": ("executor", "cursor", "command"),
}

# Defer Path.resolve() so importing this module is side-effect-free. Temporal
# workflow sandboxing forbids ``Path.resolve()`` at import time; if we did the
# resolution eagerly here, importing app.config (via app.temporal_app.activities)
# would crash workflow validation.
_DEFAULT_YAML: Path | None = None


def _default_yaml_path() -> Path:
    global _DEFAULT_YAML
    if _DEFAULT_YAML is None:
        _DEFAULT_YAML = (
            Path(__file__).resolve().parent.parent.parent / "configs" / "default.yaml"
        )
    return _DEFAULT_YAML


_cached: AppConfig | None = None


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Top-level YAML in {path} must be a mapping; got {type(raw).__name__}")
    return raw


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into ``base``. Lists are *replaced*, not concatenated."""
    out = deepcopy(base)
    for key, value in overlay.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _coerce_env_value(raw: str) -> Any:
    """Best-effort scalar coercion for env vars.

    Recognises booleans, integers, floats, and JSON. Falls back to the raw string.
    """
    lowered = raw.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    stripped = raw.strip()
    if stripped.startswith(("[", "{", '"')):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return raw


def _set_nested(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cursor = target
    for key in path[:-1]:
        nxt = cursor.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[key] = nxt
        cursor = nxt
    cursor[path[-1]] = value


def _env_overrides() -> tuple[dict[str, Any], str | None]:
    """Return (overlay, project_config_path).

    project_config_path is the override of where to read the project YAML, if any.
    """
    overlay: dict[str, Any] = {}
    project_path: str | None = None

    for env_key, raw in os.environ.items():
        if env_key in SHORT_ENV_MAP:
            target = SHORT_ENV_MAP[env_key]
            if target == ("__CONFIG_PATH__",):
                project_path = raw
            else:
                _set_nested(overlay, target, _coerce_env_value(raw))
            continue
        if not env_key.startswith(ENV_PREFIX):
            continue
        path_str = env_key[len(ENV_PREFIX) :]
        if not path_str:
            continue
        path = tuple(p.lower() for p in path_str.split("__") if p)
        if not path:
            continue
        _set_nested(overlay, path, _coerce_env_value(raw))

    return overlay, project_path


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    cli_overrides: dict[str, Any] | None = None,
    *,
    load_dotenv_file: bool = True,
) -> AppConfig:
    """Load and validate the application configuration.

    Args:
        config_path: explicit path to the project YAML. Wins over the env var.
        cli_overrides: dict-shaped overrides applied last (highest precedence).
        load_dotenv_file: if True, read ``.env`` from cwd before reading env vars.

    Returns:
        Fully validated :class:`AppConfig`.
    """
    global _cached

    if load_dotenv_file:
        load_dotenv(override=False)

    base = _read_yaml(_default_yaml_path())

    env_overlay, env_project_path = _env_overrides()
    project_path = (
        Path(config_path) if config_path is not None
        else Path(env_project_path) if env_project_path
        else None
    )
    project_overlay = _read_yaml(project_path) if project_path else {}

    merged = _deep_merge(base, project_overlay)
    merged = _deep_merge(merged, env_overlay)
    if cli_overrides:
        merged = _deep_merge(merged, cli_overrides)

    config = AppConfig.model_validate(merged)
    _cached = config
    return config


def get_config() -> AppConfig:
    """Return the most recently loaded config, loading defaults if none yet."""
    if _cached is None:
        return load_config()
    return _cached


def reset_cached_config() -> None:
    """Drop the cached config (useful in tests)."""
    global _cached
    _cached = None
