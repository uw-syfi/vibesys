"""A provider's capacity limit reaches the caller as a typed ``AgentQuotaError``.

Turns are scripted in each provider's real failure format, so the whole path
runs: agentshim classifies the failure, the driver decides whether it is a
capacity limit, and the client either hands it to the installed gate or raises.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import agentshim
import pytest
from agentshim.testing import FakeExecutor, FakeRun, scripted_failure, scripted_turn
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import AgentClient, AgentQuotaError, QuotaCondition
from vs_agent.drivers.agentshim import AgentShimDriver
from vs_sandbox.api import SANDBOX_DISABLE_ENV

PROVIDERS = ("claude", "codex")
_NOT_QUOTA = (agentshim.FailureKind.AUTH, agentshim.FailureKind.OTHER)


@pytest.fixture(scope="module")
def home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway operator HOME: the driver prepares provider state under it."""
    return tmp_path_factory.mktemp("operator-home")


def _client(provider: str, runs: list[FakeRun], home: Path) -> AgentClient:
    fake = FakeExecutor(runs)
    return AgentClient(
        AgentShimDriver(
            provider=provider,
            executor_factory=lambda: fake,
            launcher_env=lambda: {
                "PATH": "/usr/bin:/bin",
                "HOME": str(home),
                SANDBOX_DISABLE_ENV: "off",
            },
            transient_retry_delays=(),
        ),
        provider=provider,
        run_log_file=None,
    )


def _invoke(client: AgentClient, root: Path) -> str:
    return client.invoke_text(
        kind="implementer",
        workspace=root,
        system_prompt="sys",
        user_prompt="go",
        round_label="round-1",
        invocation_id="inv-1",
    )


def _exhausted_window(resets_at: int) -> str:
    info = {
        "status": "rejected",
        "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": resets_at}},
    }
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info}) + "\n"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_usage_limit_is_a_quota_error_naming_the_provider(
    provider: str, home: Path, tmp_path: Path
) -> None:
    client = _client(
        provider, [scripted_failure(provider, agentshim.FailureKind.USAGE_LIMIT)], home
    )

    with pytest.raises(AgentQuotaError) as excinfo:
        _invoke(client, tmp_path)

    assert excinfo.value.provider == provider
    assert excinfo.value.condition is QuotaCondition.QUOTA_EXHAUSTED
    assert excinfo.value.detail


@pytest.mark.parametrize("provider", PROVIDERS)
@given(kind=st.sampled_from(_NOT_QUOTA))
def test_a_failure_that_is_not_a_capacity_limit_stays_a_plain_failure(
    provider: str, kind: agentshim.FailureKind, home: Path
) -> None:
    with TemporaryDirectory() as tmp:
        client = _client(provider, [scripted_failure(provider, kind)], home)

        with pytest.raises(agentshim.TurnFailedError) as excinfo:
            _invoke(client, Path(tmp))

    assert not isinstance(excinfo.value, AgentQuotaError)
    assert excinfo.value.kind is kind


@pytest.mark.parametrize("provider", PROVIDERS)
def test_an_overload_with_no_exhausted_window_is_not_a_capacity_limit(
    provider: str, home: Path, tmp_path: Path
) -> None:
    run = scripted_failure(provider, agentshim.FailureKind.TRANSIENT)
    client = _client(provider, [run], home)

    with pytest.raises(agentshim.TurnFailedError) as excinfo:
        _invoke(client, tmp_path)

    assert excinfo.value.kind is agentshim.FailureKind.TRANSIENT


@given(resets_at=st.integers(min_value=1_700_000_000, max_value=1_900_000_000))
def test_sustained_rate_limiting_is_a_quota_error_that_carries_the_reset_time(
    resets_at: int, home: Path
) -> None:
    failure = scripted_failure("claude", agentshim.FailureKind.TRANSIENT)
    run = FakeRun(
        stdout=[_exhausted_window(resets_at), *failure.stdout],
        stderr=failure.stderr,
        returncode=failure.returncode,
    )
    with TemporaryDirectory() as tmp:
        client = _client("claude", [run], home)

        with pytest.raises(AgentQuotaError) as excinfo:
            _invoke(client, Path(tmp))

    assert excinfo.value.condition is QuotaCondition.RATE_LIMITED
    assert excinfo.value.resets_at == float(resets_at)


def test_a_turn_after_a_quota_stop_does_not_inherit_its_exhausted_windows(
    home: Path, tmp_path: Path
) -> None:
    failure = scripted_failure("claude", agentshim.FailureKind.TRANSIENT)
    stale = FakeRun(
        stdout=[_exhausted_window(1_800_000_000), *failure.stdout],
        stderr=failure.stderr,
        returncode=failure.returncode,
    )
    plain = scripted_failure("claude", agentshim.FailureKind.TRANSIENT)
    client = _client("claude", [stale, scripted_turn("claude", text="x"), plain], home)
    with pytest.raises(AgentQuotaError):
        _invoke(client, tmp_path)
    assert _invoke(client, tmp_path) == "x"

    with pytest.raises(agentshim.TurnFailedError) as excinfo:
        _invoke(client, tmp_path)

    assert not isinstance(excinfo.value, AgentQuotaError)
