"""Issue dispatcher: turns ``agent:todo`` labels into running Temporal workflows.

The dispatcher is a long-running loop that closes the "manual ``run-issue``"
gap described in the architecture doc — it lets the user dump issues into
GitHub and have them picked up automatically, plus it resurrects issues that
got stuck on ``agent:blocked``.

The loop is a plain asyncio task (not itself a Temporal workflow) so it can
be embedded in the worker process or run as a separate ``agent-worker
dispatcher`` CLI command without adding more workflow definitions.
"""

from app.dispatcher.loop import (
    DispatcherConfig,
    DispatcherDeps,
    DispatcherIteration,
    DispatcherStats,
    build_default_deps,
    run_dispatcher_loop,
    run_one_iteration,
)

__all__ = [
    "DispatcherConfig",
    "DispatcherDeps",
    "DispatcherIteration",
    "DispatcherStats",
    "build_default_deps",
    "run_dispatcher_loop",
    "run_one_iteration",
]
