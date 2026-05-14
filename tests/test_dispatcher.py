"""Dispatcher tests.

The dispatcher is the bridge between "user keeps creating GitHub issues" and
"Temporal keeps spinning up workflows for them". These tests exercise the two
responsibilities of one cycle:

1. ``agent:todo`` issues get dispatched (and re-dispatch is a no-op).
2. ``agent:blocked`` issues get classified by their PR's real CI status and
   acted on (mark done / redispatch / wait) with throttling.

Everything is faked: no Temporal server, no GitHub API. We inject
:class:`DispatcherDeps` so each test reads as a small behaviour script.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.config.models import AppConfig, ExecutorConfig
from app.dispatcher import DispatcherConfig
from app.dispatcher.loop import (
    DispatcherDeps,
    _DispatchedHandle,
    _RecoveryState,
    run_one_iteration,
)
from app.github.ci_service import CIPollResult
from app.github.issue_service import Issue


# --------- fakes ------------------------------------------------------------ #


@dataclass
class _FakeIssue:
    number: int
    labels: list[str]
    title: str = "fake"
    url: str = ""

    def as_issue(self) -> Issue:
        return Issue(
            number=self.number,
            title=self.title,
            body="",
            labels=list(self.labels),
            state="open",
            url=self.url,
        )


@dataclass
class _FakeIssuesAPI:
    by_label: dict[str, list[_FakeIssue]] = field(default_factory=dict)
    transitions: list[tuple[int, str, tuple[str, ...]]] = field(default_factory=list)
    comments: list[tuple[int, str]] = field(default_factory=list)

    def list_open_with_label(self, label: str, *, limit: int = 100) -> list[Issue]:
        return [f.as_issue() for f in self.by_label.get(label, [])]

    def list_open_with_any_label(
        self, labels: list[str], *, limit_per_label: int = 50
    ) -> list[Issue]:
        seen: dict[int, Issue] = {}
        for label in labels:
            for issue in self.list_open_with_label(label, limit=limit_per_label):
                seen.setdefault(issue.number, issue)
        return list(seen.values())

    def comment(self, issue_number: int, body: str) -> None:
        self.comments.append((issue_number, body))

    def transition_label(
        self,
        issue_number: int,
        *,
        to_label: str,
        from_labels: list[str] | None = None,
    ) -> None:
        self.transitions.append(
            (issue_number, to_label, tuple(from_labels or []))
        )


@dataclass
class _FakePR:
    number: int
    head_branch: str


@dataclass
class _FakePRAPI:
    prs_by_branch: dict[str, _FakePR] = field(default_factory=dict)

    def find_for_branch(self, head_branch: str) -> _FakePR | None:
        return self.prs_by_branch.get(head_branch)


@dataclass
class _FakeCIAPI:
    polls_by_branch: dict[str, CIPollResult] = field(default_factory=dict)
    calls: list[tuple[int | None, str | None]] = field(default_factory=list)

    def poll(
        self,
        *,
        pr_number: int | None = None,
        head_branch: str | None = None,
    ) -> CIPollResult:
        self.calls.append((pr_number, head_branch))
        if head_branch and head_branch in self.polls_by_branch:
            return self.polls_by_branch[head_branch]
        return CIPollResult(status="unknown", completed=False)


@dataclass
class _RecordingDispatcher:
    """Stand-in for ``start_issue_workflow`` that records every call."""

    outcomes: dict[int, str] = field(default_factory=dict)
    calls: list[int] = field(default_factory=list)
    next_id: int = 0
    raise_for: set[int] = field(default_factory=set)

    async def __call__(self, issue_number: int) -> _DispatchedHandle:
        self.calls.append(issue_number)
        if issue_number in self.raise_for:
            raise RuntimeError(f"simulated dispatch failure for {issue_number}")
        outcome: Any = self.outcomes.get(issue_number, "started")
        return _DispatchedHandle(
            workflow_id=f"wf-{issue_number}", outcome=outcome
        )


@dataclass
class _RecordingWorkflowStatus:
    """Stand-in for ``describe_workflow_status``. ``by_id`` maps workflow id
    to the status string the production helper would return; missing keys
    default to ``"absent"`` (i.e. "Temporal forgot about it")."""

    by_id: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    raise_for: set[str] = field(default_factory=set)

    async def __call__(self, workflow_id: str) -> str:
        self.calls.append(workflow_id)
        if workflow_id in self.raise_for:
            raise RuntimeError(f"simulated describe failure for {workflow_id}")
        return self.by_id.get(workflow_id, "absent")


def _make_deps(
    *,
    issues: _FakeIssuesAPI | None = None,
    prs: _FakePRAPI | None = None,
    ci: _FakeCIAPI | None = None,
    dispatcher: _RecordingDispatcher | None = None,
    workflow_status: _RecordingWorkflowStatus | None = None,
    now_value: float = 1000.0,
) -> tuple[
    DispatcherDeps,
    _FakeIssuesAPI,
    _FakePRAPI,
    _FakeCIAPI,
    _RecordingDispatcher,
    _RecordingWorkflowStatus,
]:
    issues = issues or _FakeIssuesAPI()
    prs = prs or _FakePRAPI()
    ci = ci or _FakeCIAPI()
    dispatcher = dispatcher or _RecordingDispatcher()
    workflow_status = workflow_status or _RecordingWorkflowStatus()
    deps = DispatcherDeps(
        issues=issues,
        prs=prs,
        ci=ci,
        dispatch=dispatcher,
        workflow_status=workflow_status,
        now=lambda: now_value,
    )
    return deps, issues, prs, ci, dispatcher, workflow_status


def _make_config(
    *,
    todo_label: str = "agent-todo",
    blocked_label: str = "agent-blocked",
    done_label: str = "agent-done",
    ci_ignore_workflows: list[str] | None = None,
) -> AppConfig:
    # Use the stub executor (always enabled) so AppConfig's executor-default
    # validator passes; this fixture-free constructor doesn't read env.
    cfg = AppConfig(executor=ExecutorConfig(default="stub"))
    cfg.repo.owner = "acme"
    cfg.repo.name = "widget"
    cfg.repo.working_branch_prefix = "agent"
    # Use hyphen-style labels consistently across all states; matches what
    # the dimos overlay does in real life. The default GitHubConfig values
    # are colon-style which would cause the in-flight labels (used by the
    # orphan scan) to mismatch the fake's by_label keys.
    cfg.github.issue_label_todo = todo_label
    cfg.github.issue_label_blocked = blocked_label
    cfg.github.issue_label_done = done_label
    cfg.github.issue_label_failed = "agent-failed"
    cfg.github.issue_label_running = "agent-running"
    cfg.github.issue_label_planning = "agent-planning"
    cfg.github.issue_label_coding = "agent-coding"
    cfg.github.issue_label_testing = "agent-testing"
    cfg.github.issue_label_pr_created = "agent-pr-created"
    cfg.github.issue_label_ci_running = "agent-ci-running"
    cfg.github.issue_label_review = "agent-review"
    cfg.github.issue_label_queued = "agent-queued"
    if ci_ignore_workflows is not None:
        cfg.github.ci_ignore_workflows = ci_ignore_workflows
    return cfg


# --------- todo handling --------------------------------------------------- #


@pytest.mark.asyncio
async def test_dispatcher_dispatches_each_open_todo_issue() -> None:
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={
            "agent-todo": [
                _FakeIssue(number=1, labels=["agent-todo"]),
                _FakeIssue(number=2, labels=["agent-todo"]),
            ]
        }
    )
    deps, _, _, _, dispatcher, _ = _make_deps(issues=issues)
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=DispatcherConfig(),
        deps=deps,
    )
    assert dispatcher.calls == [1, 2]
    assert result.stats.todo_seen == 2
    assert result.stats.todo_dispatched == 2


@pytest.mark.asyncio
async def test_dispatcher_skips_running_workflow_as_noop_attach() -> None:
    """A workflow that is already RUNNING should be reported as attach, not start."""
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={"agent-todo": [_FakeIssue(number=5, labels=["agent-todo"])]}
    )
    dispatcher = _RecordingDispatcher(outcomes={5: "attached_running"})
    deps, *_ = _make_deps(issues=issues, dispatcher=dispatcher)
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=DispatcherConfig(),
        deps=deps,
    )
    assert dispatcher.calls == [5]
    assert result.stats.todo_dispatched == 0
    assert result.stats.todo_attached_running == 1


@pytest.mark.asyncio
async def test_dispatcher_caps_dispatch_per_cycle() -> None:
    """``max_dispatch_per_cycle`` limits how many issues we kick off per cycle.

    Important for fairness: a 100-issue backlog shouldn't starve the
    dispatcher of CPU for blocked recovery or starve the worker of capacity.
    """
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={
            "agent-todo": [
                _FakeIssue(number=n, labels=["agent-todo"]) for n in range(1, 11)
            ]
        }
    )
    deps, _, _, _, dispatcher, _ = _make_deps(issues=issues)
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=DispatcherConfig(max_dispatch_per_cycle=3),
        deps=deps,
    )
    assert dispatcher.calls == [1, 2, 3]
    assert result.stats.todo_seen == 10
    assert result.stats.todo_dispatched == 3
    assert any("max_dispatch_per_cycle" in n for n in result.notes)


@pytest.mark.asyncio
async def test_dispatcher_continues_after_a_single_dispatch_failure() -> None:
    """One poison-pill issue should not block the rest of the backlog."""
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={
            "agent-todo": [
                _FakeIssue(number=1, labels=["agent-todo"]),
                _FakeIssue(number=2, labels=["agent-todo"]),
                _FakeIssue(number=3, labels=["agent-todo"]),
            ]
        }
    )
    dispatcher = _RecordingDispatcher(raise_for={2})
    deps, *_ = _make_deps(issues=issues, dispatcher=dispatcher)
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=DispatcherConfig(),
        deps=deps,
    )
    # All three issues are attempted; #2 raises but #1 and #3 still go through.
    assert dispatcher.calls == [1, 2, 3]
    assert result.stats.todo_dispatched == 2
    assert result.stats.errors == 1


# --------- blocked recovery ----------------------------------------------- #


@pytest.mark.asyncio
async def test_blocked_with_passing_ci_marks_issue_done() -> None:
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={"agent-blocked": [_FakeIssue(number=7, labels=["agent-blocked"])]}
    )
    prs = _FakePRAPI(
        prs_by_branch={"agent/issue-7": _FakePR(number=42, head_branch="agent/issue-7")}
    )
    ci = _FakeCIAPI(
        polls_by_branch={
            "agent/issue-7": CIPollResult(status="passed", completed=True)
        }
    )
    deps, _, _, _, dispatcher, _ = _make_deps(issues=issues, prs=prs, ci=ci)
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=DispatcherConfig(),
        deps=deps,
    )
    assert result.stats.blocked_seen == 1
    assert result.stats.blocked_marked_done == 1
    assert dispatcher.calls == []
    assert issues.transitions and issues.transitions[0][0] == 7
    assert issues.transitions[0][1] == "agent-done"
    assert issues.comments and issues.comments[0][0] == 7


@pytest.mark.asyncio
async def test_blocked_with_failed_ci_redispatches_workflow() -> None:
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={"agent-blocked": [_FakeIssue(number=8, labels=["agent-blocked"])]}
    )
    prs = _FakePRAPI(
        prs_by_branch={"agent/issue-8": _FakePR(number=44, head_branch="agent/issue-8")}
    )
    ci = _FakeCIAPI(
        polls_by_branch={
            "agent/issue-8": CIPollResult(
                status="failed",
                completed=True,
                summary="boom",
                failed_jobs=["ci"],
                log_excerpts={"ci": "stacktrace"},
            )
        }
    )
    dispatcher = _RecordingDispatcher(outcomes={8: "restarted_after_close"})
    deps, *_ = _make_deps(issues=issues, prs=prs, ci=ci, dispatcher=dispatcher)
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=DispatcherConfig(),
        deps=deps,
    )
    assert dispatcher.calls == [8]
    assert result.stats.blocked_redispatched == 1
    assert result.stats.blocked_marked_done == 0


@pytest.mark.asyncio
async def test_blocked_with_pending_ci_leaves_issue_alone() -> None:
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={"agent-blocked": [_FakeIssue(number=9, labels=["agent-blocked"])]}
    )
    prs = _FakePRAPI(
        prs_by_branch={"agent/issue-9": _FakePR(number=45, head_branch="agent/issue-9")}
    )
    ci = _FakeCIAPI(
        polls_by_branch={
            "agent/issue-9": CIPollResult(status="pending", completed=False)
        }
    )
    deps, _, _, _, dispatcher, _ = _make_deps(issues=issues, prs=prs, ci=ci)
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=DispatcherConfig(),
        deps=deps,
    )
    assert dispatcher.calls == []
    assert result.stats.blocked_skipped_pending == 1
    assert result.stats.blocked_marked_done == 0
    assert result.stats.blocked_redispatched == 0


@pytest.mark.asyncio
async def test_blocked_without_pr_is_left_alone() -> None:
    """If we can't find a PR for ``agent/issue-N``, the recovery loop bails.

    This covers the Phase-1 ``planning_done`` blocked state where there's no
    code change yet — re-dispatching from todo would lose the planner output.
    """
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={"agent-blocked": [_FakeIssue(number=10, labels=["agent-blocked"])]}
    )
    deps, _, _, _, dispatcher, _ = _make_deps(issues=issues)
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=DispatcherConfig(),
        deps=deps,
    )
    assert result.stats.blocked_skipped_pending == 1
    assert dispatcher.calls == []
    assert any("no PR" in n for n in result.notes)


@pytest.mark.asyncio
async def test_blocked_recovery_throttles_per_issue() -> None:
    """A blocked issue should not be re-checked within ``blocked_recover_min_interval_seconds``."""
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={"agent-blocked": [_FakeIssue(number=11, labels=["agent-blocked"])]}
    )
    prs = _FakePRAPI(
        prs_by_branch={"agent/issue-11": _FakePR(number=46, head_branch="agent/issue-11")}
    )
    ci = _FakeCIAPI(
        polls_by_branch={
            "agent/issue-11": CIPollResult(status="failed", completed=True)
        }
    )
    state = _RecoveryState()
    cycle_cfg = DispatcherConfig(blocked_recover_min_interval_seconds=600)

    # First cycle at t=1000: should re-dispatch.
    deps_1, _, _, _, dispatcher_1, _ = _make_deps(
        issues=issues, prs=prs, ci=ci, now_value=1000.0
    )
    r1 = await run_one_iteration(
        config=cfg, dispatcher_config=cycle_cfg, deps=deps_1, state=state
    )
    assert r1.stats.blocked_redispatched == 1
    assert dispatcher_1.calls == [11]

    # Second cycle at t=1100 (only 100s later): throttled, no action.
    deps_2, _, _, _, dispatcher_2, _ = _make_deps(
        issues=issues, prs=prs, ci=ci, now_value=1100.0
    )
    r2 = await run_one_iteration(
        config=cfg, dispatcher_config=cycle_cfg, deps=deps_2, state=state
    )
    assert r2.stats.blocked_skipped_throttled == 1
    assert dispatcher_2.calls == []

    # Third cycle at t=2000 (past the 600s throttle): re-dispatches again.
    deps_3, _, _, _, dispatcher_3, _ = _make_deps(
        issues=issues, prs=prs, ci=ci, now_value=2000.0
    )
    r3 = await run_one_iteration(
        config=cfg, dispatcher_config=cycle_cfg, deps=deps_3, state=state
    )
    assert r3.stats.blocked_redispatched == 1
    assert dispatcher_3.calls == [11]


@pytest.mark.asyncio
async def test_blocked_recovery_off_when_auto_recover_disabled() -> None:
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={"agent-blocked": [_FakeIssue(number=12, labels=["agent-blocked"])]}
    )
    prs = _FakePRAPI(
        prs_by_branch={"agent/issue-12": _FakePR(number=47, head_branch="agent/issue-12")}
    )
    ci = _FakeCIAPI(
        polls_by_branch={
            "agent/issue-12": CIPollResult(status="passed", completed=True)
        }
    )
    deps, _, _, _, dispatcher, _ = _make_deps(issues=issues, prs=prs, ci=ci)
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=DispatcherConfig(auto_recover_blocked=False),
        deps=deps,
    )
    assert result.stats.blocked_seen == 0
    assert result.stats.blocked_marked_done == 0
    assert dispatcher.calls == []


# --------- orphan revival -------------------------------------------------- #


def _make_orphan_config() -> DispatcherConfig:
    """Default-ish dispatcher config, but with the orphan throttle relaxed
    to zero so single-cycle tests can observe the first revive attempt."""
    return DispatcherConfig(
        orphan_check_interval_seconds=0,
        orphan_revive_min_interval_seconds=0,
    )


def _wf_id(issue_number: int) -> str:
    # Matches the inline implementation in app/dispatcher/loop.py
    # (kept independent so a regression in _workflow_id_for is caught).
    return f"issue-agent--acme--widget--issue-{issue_number}"


@pytest.mark.asyncio
async def test_orphan_absent_workflow_is_redispatched() -> None:
    """The Temporal-DB-reset case: label is in-flight, workflow id unknown.

    This is exactly the failure we hit in production when the dev-mode
    Temporal restart wiped state and #52 was stuck on ``agent-planning``.
    The orphan scan should redispatch it without waiting for the user to
    relabel the issue.
    """
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={
            "agent-planning": [_FakeIssue(number=52, labels=["agent-planning"])]
        }
    )
    wf_status = _RecordingWorkflowStatus(by_id={_wf_id(52): "absent"})
    deps, _, _, _, dispatcher, _ = _make_deps(issues=issues, workflow_status=wf_status)
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=_make_orphan_config(),
        deps=deps,
    )
    assert wf_status.calls == [_wf_id(52)]
    assert dispatcher.calls == [52]
    assert result.stats.orphan_seen == 1
    assert result.stats.orphan_revived == 1
    assert result.stats.orphan_healthy == 0


@pytest.mark.asyncio
async def test_orphan_running_workflow_is_left_alone() -> None:
    """A workflow that's still RUNNING is healthy — leave it."""
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={
            "agent-coding": [_FakeIssue(number=80, labels=["agent-coding"])]
        }
    )
    wf_status = _RecordingWorkflowStatus(by_id={_wf_id(80): "running"})
    deps, _, _, _, dispatcher, _ = _make_deps(issues=issues, workflow_status=wf_status)
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=_make_orphan_config(),
        deps=deps,
    )
    assert dispatcher.calls == []
    assert result.stats.orphan_seen == 1
    assert result.stats.orphan_healthy == 1
    assert result.stats.orphan_revived == 0


@pytest.mark.asyncio
async def test_orphan_closed_workflow_is_redispatched() -> None:
    """Workflow finished but its terminal label transition didn't reach GH.

    Surfaced as label still on ``agent-coding`` even though Temporal says
    ``completed`` / ``failed`` / ``terminated``. Re-dispatching lets the
    new workflow's transition activities push the label forward.
    """
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={
            "agent-coding": [_FakeIssue(number=81, labels=["agent-coding"])]
        }
    )
    wf_status = _RecordingWorkflowStatus(by_id={_wf_id(81): "failed"})
    dispatcher = _RecordingDispatcher(outcomes={81: "restarted_after_close"})
    deps, _, _, _, _, _ = _make_deps(
        issues=issues, workflow_status=wf_status, dispatcher=dispatcher
    )
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=_make_orphan_config(),
        deps=deps,
    )
    assert dispatcher.calls == [81]
    assert result.stats.orphan_revived == 1


@pytest.mark.asyncio
async def test_orphan_with_terminal_label_is_skipped() -> None:
    """Some in-flight labels can linger alongside ``agent-done`` because the
    workflow transitioned the terminal label first but didn't have time to
    strip the intermediate ones. Don't revive these — they've actually shipped.
    """
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={
            "agent-coding": [
                _FakeIssue(number=90, labels=["agent-coding", "agent-done"])
            ]
        }
    )
    wf_status = _RecordingWorkflowStatus(by_id={_wf_id(90): "completed"})
    deps, _, _, _, dispatcher, _ = _make_deps(issues=issues, workflow_status=wf_status)
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=_make_orphan_config(),
        deps=deps,
    )
    assert dispatcher.calls == []
    # Stats: scan ran, found 1 issue, but skipped on terminal-label gate.
    assert result.stats.orphan_seen == 1
    assert result.stats.orphan_revived == 0
    assert result.stats.orphan_healthy == 0


@pytest.mark.asyncio
async def test_orphan_dedupes_across_in_flight_labels() -> None:
    """An issue carrying two in-flight labels at once should only be inspected once."""
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={
            "agent-coding": [
                _FakeIssue(number=91, labels=["agent-coding", "agent-running"])
            ],
            "agent-running": [
                _FakeIssue(number=91, labels=["agent-coding", "agent-running"])
            ],
        }
    )
    wf_status = _RecordingWorkflowStatus(by_id={_wf_id(91): "absent"})
    deps, _, _, _, dispatcher, _ = _make_deps(issues=issues, workflow_status=wf_status)
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=_make_orphan_config(),
        deps=deps,
    )
    assert dispatcher.calls == [91]  # only one dispatch
    assert wf_status.calls == [_wf_id(91)]  # only one status check
    assert result.stats.orphan_seen == 1
    assert result.stats.orphan_revived == 1


@pytest.mark.asyncio
async def test_orphan_scan_respects_throttle_within_window() -> None:
    """Two cycles inside ``orphan_revive_min_interval_seconds`` should
    only revive once."""
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={
            "agent-planning": [_FakeIssue(number=70, labels=["agent-planning"])]
        }
    )
    wf_status = _RecordingWorkflowStatus(by_id={_wf_id(70): "absent"})
    state = _RecoveryState()
    cycle_cfg = DispatcherConfig(
        orphan_check_interval_seconds=0,
        orphan_revive_min_interval_seconds=600,
    )

    # First cycle (t=1000): revives.
    deps_1, _, _, _, dispatcher_1, _ = _make_deps(
        issues=issues, workflow_status=wf_status, now_value=1000.0
    )
    r1 = await run_one_iteration(
        config=cfg, dispatcher_config=cycle_cfg, deps=deps_1, state=state
    )
    assert r1.stats.orphan_revived == 1
    assert dispatcher_1.calls == [70]

    # Second cycle (t=1100, 100s later): throttled.
    deps_2, _, _, _, dispatcher_2, _ = _make_deps(
        issues=issues, workflow_status=wf_status, now_value=1100.0
    )
    r2 = await run_one_iteration(
        config=cfg, dispatcher_config=cycle_cfg, deps=deps_2, state=state
    )
    assert r2.stats.orphan_revived == 0
    assert r2.stats.orphan_skipped_throttled == 1
    assert dispatcher_2.calls == []

    # Third cycle (t=2000, past throttle window): revives again.
    deps_3, _, _, _, dispatcher_3, _ = _make_deps(
        issues=issues, workflow_status=wf_status, now_value=2000.0
    )
    r3 = await run_one_iteration(
        config=cfg, dispatcher_config=cycle_cfg, deps=deps_3, state=state
    )
    assert r3.stats.orphan_revived == 1
    assert dispatcher_3.calls == [70]


@pytest.mark.asyncio
async def test_orphan_scan_is_skipped_until_check_interval_elapses() -> None:
    """The scan as a whole is throttled by ``orphan_check_interval_seconds``
    so we don't burn the gh-API budget every 30s."""
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={
            "agent-planning": [_FakeIssue(number=71, labels=["agent-planning"])]
        }
    )
    wf_status = _RecordingWorkflowStatus(by_id={_wf_id(71): "absent"})
    state = _RecoveryState()
    cycle_cfg = DispatcherConfig(
        orphan_check_interval_seconds=300,
        orphan_revive_min_interval_seconds=0,
    )

    # Cycle 1 at t=1000: scan runs (state was 0.0).
    deps_1, _, _, _, _, wf_1 = _make_deps(
        issues=issues, workflow_status=wf_status, now_value=1000.0
    )
    r1 = await run_one_iteration(
        config=cfg, dispatcher_config=cycle_cfg, deps=deps_1, state=state
    )
    assert r1.stats.orphan_scan_ran is True
    assert wf_1.calls == [_wf_id(71)]

    # Cycle 2 at t=1100 (100s later, < 300s): scan skipped.
    fresh_status = _RecordingWorkflowStatus(by_id={_wf_id(71): "absent"})
    deps_2, _, _, _, _, _ = _make_deps(
        issues=issues, workflow_status=fresh_status, now_value=1100.0
    )
    r2 = await run_one_iteration(
        config=cfg, dispatcher_config=cycle_cfg, deps=deps_2, state=state
    )
    assert r2.stats.orphan_scan_ran is False
    assert fresh_status.calls == []  # the gh / temporal calls didn't even fire


@pytest.mark.asyncio
async def test_orphan_revival_off_when_revive_orphans_disabled() -> None:
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={
            "agent-planning": [_FakeIssue(number=72, labels=["agent-planning"])]
        }
    )
    wf_status = _RecordingWorkflowStatus(by_id={_wf_id(72): "absent"})
    deps, _, _, _, dispatcher, _ = _make_deps(issues=issues, workflow_status=wf_status)
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=DispatcherConfig(revive_orphans=False),
        deps=deps,
    )
    assert dispatcher.calls == []
    assert wf_status.calls == []
    assert result.stats.orphan_scan_ran is False


@pytest.mark.asyncio
async def test_orphan_describe_failure_is_counted_as_error_not_revival() -> None:
    """A failing workflow-status call must not redispatch the issue.

    Otherwise a temporary Temporal connectivity blip would re-dispatch every
    in-flight issue every cycle.
    """
    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={
            "agent-planning": [_FakeIssue(number=73, labels=["agent-planning"])]
        }
    )
    wf_status = _RecordingWorkflowStatus(raise_for={_wf_id(73)})
    deps, _, _, _, dispatcher, _ = _make_deps(issues=issues, workflow_status=wf_status)
    result = await run_one_iteration(
        config=cfg,
        dispatcher_config=_make_orphan_config(),
        deps=deps,
    )
    assert dispatcher.calls == []
    assert result.stats.errors == 1
    assert result.stats.orphan_revived == 0


# --------- loop integration ------------------------------------------------ #


@pytest.mark.asyncio
async def test_loop_runs_max_cycles_and_returns() -> None:
    from app.dispatcher import run_dispatcher_loop

    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={"agent-todo": [_FakeIssue(number=1, labels=["agent-todo"])]}
    )
    deps, _, _, _, dispatcher, _ = _make_deps(issues=issues)
    await run_dispatcher_loop(
        config=cfg,
        dispatcher_config=DispatcherConfig(
            poll_interval_seconds=0,  # don't sleep between cycles in the test
            max_cycles=3,
        ),
        deps=deps,
    )
    assert dispatcher.calls == [1, 1, 1]


@pytest.mark.asyncio
async def test_loop_stops_when_stop_event_is_set() -> None:
    from app.dispatcher import run_dispatcher_loop

    cfg = _make_config()
    issues = _FakeIssuesAPI(
        by_label={"agent-todo": [_FakeIssue(number=1, labels=["agent-todo"])]}
    )
    deps, _, _, _, dispatcher, _ = _make_deps(issues=issues)
    stop = asyncio.Event()

    async def _flip() -> None:
        # let the loop run at least once
        await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(
        run_dispatcher_loop(
            config=cfg,
            dispatcher_config=DispatcherConfig(poll_interval_seconds=0),
            deps=deps,
            stop_event=stop,
        ),
        _flip(),
    )
    # The dispatcher ran at least once before stop_event resolved.
    assert len(dispatcher.calls) >= 1
