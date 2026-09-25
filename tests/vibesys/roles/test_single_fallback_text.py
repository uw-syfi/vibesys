"""Regression tests for the single / profile_single combined-turn fallbacks.

Phase 3c gave ``SINGLE_COMBINED``/``PROFILE_SINGLE_COMBINED`` a single
``fallback`` used for both a structured-parse failure and a timed-out turn,
so both said "Single-agent invocation timed out." even when the reply just
failed to parse. ``Role.timeout_fallback`` restores the pre-3c distinction:
``fallback`` keeps the parse-failure text, ``timeout_fallback`` reproduces
a6e361c1's timeout text with the real elapsed seconds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st

from vibesys.roles.common import Verdict
from vibesys.roles.single_agent import (
    PROFILE_SINGLE_COMBINED,
    SINGLE_COMBINED,
    SingleAgentRoundResponse,
)

if TYPE_CHECKING:
    from vibesys.runtime import Role

_ROLES = (SINGLE_COMBINED, PROFILE_SINGLE_COMBINED)


@given(st.sampled_from(_ROLES))
def test_parse_failure_fallback_does_not_mention_timeout(role: Role) -> None:
    reply = role.fallback()
    assert isinstance(reply, SingleAgentRoundResponse)
    assert reply.verdict is Verdict.FAIL
    assert "timed out" not in reply.summary
    assert "timed out" not in reply.self_review
    assert reply.summary == "Single-agent produced no structured response."
    assert reply.self_review == "No structured response received."
    assert reply.feedback == "No structured response received."


@given(st.sampled_from(_ROLES), st.floats(min_value=0.1, max_value=10_000))
def test_timeout_fallback_reports_the_real_seconds(role: Role, timeout: float) -> None:
    assert role.timeout_fallback is not None
    reply = role.timeout_fallback(timeout)
    assert isinstance(reply, SingleAgentRoundResponse)
    assert reply.verdict is Verdict.FAIL
    assert reply.summary == "Single-agent invocation timed out."
    assert f"{timeout:g} seconds" in reply.self_review
    assert "Inspect retained evidence" in reply.feedback


@given(
    st.sampled_from(_ROLES),
    st.lists(st.sampled_from(["timeout", "unparseable", "valid"]), min_size=1, max_size=20),
    st.floats(min_value=0.1, max_value=10_000),
)
def test_fallback_text_matches_cause_for_a_random_reply_mix(
    role: Role, causes: list[str], timeout: float
) -> None:
    """A random mix of valid/unparseable/timeout replies always resolves to
    the fallback text matching its own cause, never the other cause's text.
    """
    for cause in causes:
        if cause == "valid":
            reply = SingleAgentRoundResponse(
                summary="ok",
                expected_behavior="ok",
                self_review="ok",
                feedback="ok",
                verdict=Verdict.PASS,
                bottlenecks="",
                suggestions="",
                profile_analysis="",
            )
        elif cause == "unparseable":
            reply = role.fallback()
        else:
            assert role.timeout_fallback is not None
            reply = role.timeout_fallback(timeout)

        assert isinstance(reply, SingleAgentRoundResponse)
        if cause == "unparseable":
            assert reply.summary == "Single-agent produced no structured response."
            assert "timed out" not in reply.summary
        elif cause == "timeout":
            assert reply.summary == "Single-agent invocation timed out."
            assert f"{timeout:g} seconds" in reply.self_review
