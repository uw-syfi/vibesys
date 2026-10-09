"""``[agent.quota]``: the unattended policy for a provider quota stop, validated at startup."""

from __future__ import annotations

import tomllib

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.config import Config
from vs_runtime.api.infrastructure import QuotaAction, QuotaPolicy


def _config(**quota: object) -> Config:
    return Config.model_validate({"model": {"name": "test"}, "agent": {"quota": quota}})


def test_the_default_is_to_pause_for_the_operator() -> None:
    assert Config(model={"name": "test"}).agent.quota.to_policy() == QuotaPolicy()


@given(
    wait=st.none() | st.integers(min_value=1, max_value=10**7),
    retry=st.integers(min_value=1, max_value=10**5),
)
def test_a_wait_policy_reaches_the_runtime_with_its_numbers(wait: int | None, retry: int) -> None:
    policy = _config(policy="wait", wait_seconds=wait, retry_seconds=retry).agent.quota.to_policy()

    assert policy == QuotaPolicy(QuotaAction.WAIT, wait, retry)


def test_the_toml_section_is_read_from_the_agent_table() -> None:
    raw = tomllib.loads(
        '[model]\nname = "m"\n[agent.quota]\npolicy = "wait"\nwait_seconds = 21600\n'
    )

    assert Config.model_validate(raw).agent.quota.to_policy() == QuotaPolicy(
        QuotaAction.WAIT, 21600
    )


@pytest.mark.parametrize(
    ("quota", "named"),
    [
        ({"polcy": "wait"}, "polcy"),
        ({"policy": "retry"}, "policy"),
        ({"policy": "wait", "wait_seconds": 0}, "wait_seconds"),
        ({"policy": "wait", "wait_seconds": "60"}, "wait_seconds"),
        ({"policy": "wait", "wait_seconds": True}, "wait_seconds"),
        ({"retry_seconds": 0}, "retry_seconds"),
        ({"policy": "pause", "wait_seconds": 60}, "wait_seconds"),
        ({"policy": "fail", "wait_seconds": 60}, "wait_seconds"),
    ],
)
def test_a_bad_quota_section_is_rejected_naming_the_offending_key(
    quota: dict[str, object], named: str
) -> None:
    with pytest.raises(ValidationError, match=named) as raised:
        _config(**quota)

    assert "agent.quota" in str(raised.value) or "quota" in str(raised.value)
