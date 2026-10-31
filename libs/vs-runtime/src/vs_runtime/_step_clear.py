"""Which steps an environment cannot clear, said when the run starts rather than when it waits.

``clear`` may raise ``ClearUnavailableError`` only for an outage that ends: the loop retries it and
shows the run waiting. An environment that has no way to find what a step left (a host with no
process table it can read) would make that wait endless, so it refuses at the start instead, naming
the step kinds that could not run there.
"""

from __future__ import annotations

AGENT_STEP_KINDS = ("dispatch_turn", "resume_session_turn", "execute_registered_operation")
"""The steps that start agent processes: a turn, a resumed turn, a registered session-turn operation."""


class StepClearUnsupportedError(RuntimeError):
    """An environment has no way to end the work of a step, so these step kinds cannot run there."""

    def __init__(self, environment: str, step_kinds: tuple[str, ...], reason: str) -> None:
        """Name the environment, the step kinds it cannot run, and why."""
        super().__init__(
            f"the {environment} environment cannot clear {', '.join(step_kinds)} steps: {reason}"
        )
        self.environment = environment
        self.step_kinds = step_kinds
        self.reason = reason
