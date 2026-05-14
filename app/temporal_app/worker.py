"""Temporal worker entry point.

Run with::

    agent-worker worker

or::

    python -m app.temporal_app.worker

The worker can optionally embed the auto-dispatcher loop (see
``app.dispatcher``) in the same process by setting
``workflow.dispatcher.enabled: true`` in the project YAML or by passing
``with_dispatcher=True`` to :func:`run_worker`. The CLI exposes this as
``agent-worker worker --with-dispatcher``.
"""

from __future__ import annotations

import asyncio
import signal

from temporalio.worker import Worker

from app.config import get_config, load_config
from app.observability import configure_logging, get_logger
from app.temporal_app.activities import build_activities
from app.temporal_app.client import build_client
from app.temporal_app.workflows import IssueAgentWorkflow

log = get_logger(__name__)


async def run_worker(*, with_dispatcher: bool | None = None) -> None:
    """Run the Temporal worker until SIGINT/SIGTERM.

    Args:
        with_dispatcher: force the embedded dispatcher on (``True``), off
            (``False``), or honour the config (``None``, default). The
            dispatcher polls GitHub for ``agent:todo`` issues and starts
            workflows for them — the "issue → automatic pickup" half of the
            architecture.
    """
    config = get_config()
    configure_logging(level=config.system.log_level, fmt=config.system.log_format)

    dispatcher_enabled = (
        with_dispatcher if with_dispatcher is not None else config.workflow.dispatcher.enabled
    )

    client = await build_client(config)
    _, activity_callables = build_activities(config)

    worker = Worker(
        client,
        task_queue=config.workflow.temporal.task_queue,
        workflows=[IssueAgentWorkflow],
        activities=activity_callables,
    )

    log.info(
        "worker.starting",
        task_queue=config.workflow.temporal.task_queue,
        host=config.workflow.temporal.host,
        namespace=config.workflow.temporal.namespace,
        dispatcher_enabled=dispatcher_enabled,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    dispatcher_task: asyncio.Task[None] | None = None

    async with worker:
        if dispatcher_enabled:
            dispatcher_task = asyncio.create_task(
                _run_dispatcher(config, stop_event), name="dispatcher"
            )
        try:
            await stop_event.wait()
        finally:
            if dispatcher_task is not None:
                stop_event.set()
                try:
                    await asyncio.wait_for(dispatcher_task, timeout=10)
                except TimeoutError:
                    dispatcher_task.cancel()
                    try:
                        await dispatcher_task
                    except (asyncio.CancelledError, Exception):
                        pass
    log.info("worker.stopped")


async def _run_dispatcher(config, stop_event: asyncio.Event) -> None:  # type: ignore[no-untyped-def]
    """Long-running asyncio task that polls GitHub for new / blocked issues."""
    from app.dispatcher import DispatcherConfig, run_dispatcher_loop

    section = config.workflow.dispatcher
    dispatcher_config = DispatcherConfig(
        poll_interval_seconds=section.poll_interval_seconds,
        max_dispatch_per_cycle=section.max_dispatch_per_cycle,
        auto_recover_blocked=section.auto_recover_blocked,
        blocked_recover_min_interval_seconds=section.blocked_recover_min_interval_seconds,
        revive_orphans=section.revive_orphans,
        orphan_check_interval_seconds=section.orphan_check_interval_seconds,
        orphan_revive_min_interval_seconds=section.orphan_revive_min_interval_seconds,
    )
    await run_dispatcher_loop(
        config=config,
        dispatcher_config=dispatcher_config,
        stop_event=stop_event,
    )


def main() -> None:
    load_config()
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
