"""OpenHands executor stub.

Phase 3+. OpenHands is typically run as a long-lived process / container, so
the eventual implementation will likely talk to the OpenHands HTTP API rather
than spawning a CLI. The stub keeps the interface in place.
"""

from __future__ import annotations

from app.executors.base import (
    CodeExecutor,
    ExecutorRequest,
    ExecutorResult,
    register_executor,
)


class OpenHandsExecutor(CodeExecutor):
    name = "openhands"

    def run(self, request: ExecutorRequest) -> ExecutorResult:
        raise NotImplementedError(
            "OpenHandsExecutor is a stub. Implement against the OpenHands HTTP API "
            "or its docker-compose runner in Phase 3+."
        )


register_executor("openhands", OpenHandsExecutor)
