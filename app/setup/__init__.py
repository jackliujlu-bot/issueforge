"""Setup helpers: configuration wizard and preflight doctor.

These modules are used by the ``init`` / ``doctor`` / ``bootstrap`` CLI
commands. They are deliberately self-contained and have no transitive
dependency on Temporal, LangGraph, or any executor backend so that running
``agent-worker doctor`` on a fresh checkout (or before installing optional
extras) doesn't fail on import-time errors.

Public entrypoints:
    - :func:`run_wizard` - interactive YAML generator
    - :func:`run_doctor` - preflight checks with optional auto-fix
    - :func:`detect_commands` - guess setup/lint/test/build commands from a repo
"""

from app.setup.detection import detect_base_branch, detect_commands
from app.setup.doctor import CheckOutcome, DoctorReport, DoctorResult, run_doctor
from app.setup.orchestrator import (
    BootstrapResult,
    PhaseResult,
    PhaseStatus,
    run_bootstrap,
)
from app.setup.wizard import WizardAnswers, WizardIO, run_wizard

__all__ = [
    "BootstrapResult",
    "CheckOutcome",
    "DoctorReport",
    "DoctorResult",
    "PhaseResult",
    "PhaseStatus",
    "WizardAnswers",
    "WizardIO",
    "detect_base_branch",
    "detect_commands",
    "run_bootstrap",
    "run_doctor",
    "run_wizard",
]
