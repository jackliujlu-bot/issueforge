# Changelog

All notable changes to IssueForge are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial open-source release of IssueForge under MIT license.
- `configs/examples/generic-python.yaml` — clean template for new users with
  4 placeholder fields and inline comments for `commands.{setup,lint,test}`
  selection.
- `configs/examples/dimos.yaml` — mature real-world example explaining every
  knob (renamed from `configs/example-dimos.yaml` to make its example status
  obvious).
- `recipes/github-workflows/ci.yml` and `recipes/github-workflows/auto-merge.yml` —
  drop-in GitHub Actions workflow files for the *business repo* the worker
  operates on. Pre-vetted to avoid the matrix / codecov / fail-fast noise
  patterns that make the agent burn retries on things it can't fix.
- `docs/PORTING.md` — comprehensive porting guide covering uv / poetry / pip /
  pnpm / Go / Rust setups, the lint-chain pattern, the test-budget tradeoff,
  and a "When something goes wrong" cheat sheet.
- `docs/ARCHITECTURE.md` — internals reference: three recovery layers,
  boundary discipline, extension points.
- `LICENSE` (MIT).
- `CONTRIBUTING.md` with the portability rule, dev setup, and PR checklist.
- `.github/workflows/ci.yml` for the worker's own CI (ruff + mypy + pytest
  on Python 3.11 and 3.12).
- `.github/ISSUE_TEMPLATE/` with `bug_report.yml`, `feature_request.yml`,
  `porting_help.yml`, and `config.yml`.
- `.github/pull_request_template.md` enforcing the portability rule.
- `issueforge` as the canonical CLI command name (alongside the existing
  `agent-worker` alias).
- Project URLs and PyPI classifiers in `pyproject.toml`.

### Changed
- README rewritten to be open-source-friendly: 30-second pitch, 5-minute
  quickstart, links to PORTING.md for the full walkthrough. Architecture
  details moved to `docs/ARCHITECTURE.md`.
- `docker-compose.yml`: removed the hardcoded `/home/lenovo/dimos2`
  fallback for `WORKER_BUSINESS_REPO_PATH`. The variable is now required
  via `${WORKER_BUSINESS_REPO_PATH:?...}` so `docker compose up` fails
  loudly with a useful message instead of silently mounting a non-existent
  path. Container names also rebranded from `agent-worker-*` to
  `issueforge-*`.
- `.env.example` cleaned up: now defaults to
  `AGENT_WORKER_CONFIG=configs/examples/generic-python.yaml` and documents
  `WORKER_BUSINESS_REPO_PATH` (used by docker compose) in a dedicated
  section.
- License changed from Apache-2.0 (declared in pyproject only) to MIT
  (declared in both pyproject and a real `LICENSE` file).

### Notes
- Pre-existing mypy debt (~40 untyped-dict warnings across 12 files) is
  not fixed in this release; CI's `type-check` job is currently
  `continue-on-error: true` until the debt is cleared. Tracking a follow-up
  to flip the flag back.
- The worker continues to ship `agent-worker` as a CLI alias to avoid
  breaking existing scripts and docs that reference it.

---

[Unreleased]: https://github.com/jackliujlu-bot/issueforge/compare/HEAD...HEAD
