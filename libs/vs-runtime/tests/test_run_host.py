"""The run host's close: a teardown step that failed runs again on the next close."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_runtime.api import RunCleanupError
from vs_runtime.api.infrastructure import BlockingOperations, RunHostComponents, open_run_host
from vs_runtime.api.testing import FakeWorkspace, FakeWorkspaces
from vs_sim.api.testing import InlineBlockingRunner, VirtualClock, run_virtual

if TYPE_CHECKING:
    from vs_runtime.api.infrastructure import RuntimeRunHost
    from vs_sim.api import ReleaseStack


class _FlakyRelease:
    """A run-owned resource whose release raises ``failures`` times before it succeeds."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def close(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            message = "docker rm -f was killed"
            raise RuntimeError(message)


@given(failures=st.integers(1, 3), extra_closes=st.integers(0, 2))
def test_a_run_resource_whose_release_failed_is_released_again_on_the_next_close(
    failures: int, extra_closes: int
) -> None:
    """Regression: the host closed once, so a retried close never re-ran a failed release."""
    resource = _FlakyRelease(failures)
    runner = InlineBlockingRunner()

    def prepare(ownership: ReleaseStack) -> RunHostComponents:
        ownership.callback(resource.close)
        return RunHostComponents(
            workspaces=FakeWorkspaces(FakeWorkspace(path=Path("/project"))),
            blocking=BlockingOperations(runner),
        )

    async def scenario() -> None:
        opened: list[RuntimeRunHost] = []
        with pytest.raises(RunCleanupError):
            async with open_run_host(prepare, runner=runner) as host:
                opened.append(host)
        for _ in range(failures - 1):
            with pytest.raises(RunCleanupError):
                await opened[0].close()
        for _ in range(1 + extra_closes):
            await opened[0].close()

    run_virtual(VirtualClock(), scenario())

    assert resource.calls == failures + 1
