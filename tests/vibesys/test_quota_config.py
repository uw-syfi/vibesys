"""``[agent.quota]``: the unattended policy for a provider quota stop, validated at startup."""

from __future__ import annotations

import tomllib

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.config import Config
from vs_runtime.api.infrastructure import FallbackTarget, QuotaAction, QuotaPolicy


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
        ({"policy": "fallback"}, "fallback_provider"),
        ({"fallback_provider": "codex"}, "fallback_model"),
        ({"fallback_model": "gpt-5"}, "fallback_provider"),
        ({"fallback_provider": "gpt", "fallback_model": "x"}, "fallback_provider"),
        ({"fallback_provider": "codex", "fallback_model": ""}, "fallback_model"),
        ({"fallback_provder": "codex"}, "fallback_provder"),
    ],
)
def test_a_bad_quota_section_is_rejected_naming_the_offending_key(
    quota: dict[str, object], named: str
) -> None:
    with pytest.raises(ValidationError, match=named) as raised:
        _config(**quota)

    assert "agent.quota" in str(raised.value) or "quota" in str(raised.value)


@given(
    wait=st.none() | st.integers(min_value=1, max_value=10**7),
    provider=st.sampled_from(["claude", "codex", "gemini", "opencode"]),
    model=st.text(min_size=1, max_size=20),
    action=st.sampled_from(["pause", "wait", "fail", "fallback"]),
)
def test_a_fallback_target_reaches_the_runtime_policy_under_any_action(
    wait: int | None, provider: str, model: str, action: str
) -> None:
    if wait is not None and action not in ("wait", "fallback"):
        wait = None

    policy = _config(
        policy=action, wait_seconds=wait, fallback_provider=provider, fallback_model=model
    ).agent.quota.to_policy()

    assert policy.fallback == FallbackTarget(provider, model)
    assert policy.action is QuotaAction(action)
    assert policy.wait_seconds == wait


def test_the_issue_example_waits_hours_then_falls_back_to_codex() -> None:
    raw = tomllib.loads(
        '[model]\nname = "m"\n[agent.quota]\npolicy = "fallback"\nwait_seconds = 14400\n'
        'fallback_provider = "codex"\nfallback_model = "gpt-5"\n'
    )

    assert Config.model_validate(raw).agent.quota.to_policy() == QuotaPolicy(
        QuotaAction.FALLBACK, 14400, fallback=FallbackTarget("codex", "gpt-5")
    )
