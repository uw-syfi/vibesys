"""``AgentClient.close`` re-runs a close that failed instead of reporting it closed.

Regression: the client set its closed latch before closing its sessions and
launcher, so after one failed close every later ``close()`` returned at once as
success with the launcher never closed.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import AgentClient
from vs_agent.api.testing import FakeProvider, FakeProviderError
from vs_sim.api.testing import ManualClock, SimThreads


@given(failures=st.integers(min_value=0, max_value=3))
def test_a_failed_close_is_retried_until_the_launcher_closes(failures: int) -> None:
    launcher = FakeProvider(threads=SimThreads(), close_failures=failures)
    client = AgentClient(
        launcher, provider="mock", model_name="m", clock=ManualClock(), threads=SimThreads()
    )

    for _ in range(failures):
        with pytest.raises(FakeProviderError, match="close failed"):
            client.close()
    client.close()
    client.close()

    assert launcher.close_calls == failures + 1
    with pytest.raises(RuntimeError, match="closed"):
        client.__enter__()
