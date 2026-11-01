"""The ``LimitsReader`` contract, run against the fake and against the agentshim-backed reader.

Both implementations answer the same script: ``claude`` has a session and a weekly window and a
per-model one and no credits; ``codex`` has a weekly window and two reset credits. The real reader
runs its real code against agentshim's fake CLIs (``limits_executor``), so what is checked is the
reader's mapping and its handling of a spend, not a mock of it.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api.testing import (
    ClaudeStreamPeers,
    CodexAccount,
    CodexScript,
    CodexWindow,
    FakeLimitsReader,
    claude_account,
    claude_usage,
    limits_executor,
)
from vs_agent.api.usage_limits import (
    AgentLimitsReader,
    LimitsReader,
    ProviderLimits,
    ResetCredit,
    SpendOutcome,
    UnavailableReason,
    Window,
    WindowKind,
    providers,
)

if TYPE_CHECKING:
    from collections.abc import Callable

RESET = dt.datetime(2026, 11, 1, 18, 0, tzinfo=dt.UTC)
CREDITS = ("c1", "c2")
ENV = {"PATH": "/bin"}

type Make = Callable[[], LimitsReader]


def _fake() -> LimitsReader:
    claude = ProviderLimits(
        provider="claude",
        windows=(
            Window("five_hour", WindowKind.SESSION, "Session", 0.39, RESET, 300),
            Window("seven_day", WindowKind.WEEKLY, "Weekly", 0.28, RESET, 10_080),
            Window("seven_day_fable", WindowKind.WEEKLY, "Weekly · Fable", 0.0, RESET, 10_080),
        ),
        plan="team",
    )
    codex = ProviderLimits(
        provider="codex",
        windows=(Window("codex:primary", WindowKind.WEEKLY, "Weekly", 0.03, RESET, 10_080),),
        plan="pro",
        reset_credits_available=len(CREDITS),
        reset_credits=tuple(ResetCredit(credit, None, "Reset") for credit in CREDITS),
    )
    return FakeLimitsReader({"claude": claude, "codex": codex}, holding={"codex": CREDITS})


def _agent() -> LimitsReader:
    claude = ClaudeStreamPeers(
        account=claude_account(),
        usage=claude_usage(
            five_hour=(39, RESET.isoformat()),
            seven_day=(28, RESET.isoformat()),
            model_scoped=[("Fable", 0, RESET.isoformat())],
        ),
    )
    codex = CodexScript(
        account=CodexAccount(
            buckets={"codex": (CodexWindow(3, 10_080, int(RESET.timestamp())), None)},
            credits=CREDITS,
        )
    )
    return AgentLimitsReader(ENV, 5.0, executor=limits_executor(claude=claude, codex=codex))


@pytest.fixture(params=[pytest.param(_fake, id="fake"), pytest.param(_agent, id="agentshim")])
def make(request: pytest.FixtureRequest) -> Make:
    return request.param


class TestRead:
    def test_each_window_comes_back_once_with_a_share_and_a_kind(self, make: Make) -> None:
        claude = make().read("claude")
        assert claude.unavailable is None
        assert [(w.id, w.kind, w.label) for w in claude.windows] == [
            ("five_hour", WindowKind.SESSION, "Session"),
            ("seven_day", WindowKind.WEEKLY, "Weekly"),
            ("seven_day_fable", WindowKind.WEEKLY, "Weekly · Fable"),
        ]
        assert [w.used_fraction for w in claude.windows] == pytest.approx([0.39, 0.28, 0.0])
        assert claude.reset_credits_available is None
        codex = make().read("codex")
        assert [(w.id, w.kind) for w in codex.windows] == [("codex:primary", WindowKind.WEEKLY)]
        assert codex.windows[0].used_fraction == pytest.approx(0.03)
        assert codex.windows[0].resets_at == RESET
        assert codex.plan == "pro"

    def test_the_credits_a_provider_holds_are_listed_with_their_count(self, make: Make) -> None:
        codex = make().read("codex")
        assert codex.reset_credits_available == len(CREDITS)
        assert [credit.id for credit in codex.reset_credits] == list(CREDITS)

    def test_a_provider_with_no_read_says_so_and_has_no_windows(self, make: Make) -> None:
        reading = make().read("opencode")
        assert reading.windows == ()
        assert reading.unavailable is not None
        assert reading.unavailable.reason is UnavailableReason.UNSUPPORTED

    @given(st.integers(1, 5))
    def test_reading_never_spends_a_credit(self, reads: int) -> None:
        for make in (_fake, _agent):
            reader = make()
            for _ in range(reads):
                assert reader.read("codex").reset_credits_available == len(CREDITS)

    def test_the_providers_that_can_be_read_are_the_ones_with_a_reader(self) -> None:
        assert providers() == ("claude", "codex")


class TestSpend:
    def test_a_spend_empties_the_windows_and_costs_one_credit(self, make: Make) -> None:
        result = make().spend("codex", idempotency_key="k1", credit_id="c2")
        assert result.outcome is SpendOutcome.RESET
        assert [credit.id for credit in result.limits.reset_credits] == ["c1"]
        assert result.limits.reset_credits_available == 1
        assert [w.used_fraction for w in result.limits.windows] == [0.0]

    def test_without_a_credit_id_the_provider_picks_one(self, make: Make) -> None:
        result = make().spend("codex", idempotency_key="k1", credit_id=None)
        assert result.outcome is SpendOutcome.RESET
        assert result.limits.reset_credits_available == len(CREDITS) - 1

    def test_a_credit_the_account_does_not_hold_spends_nothing(self, make: Make) -> None:
        result = make().spend("codex", idempotency_key="k1", credit_id="nope")
        assert result.outcome is SpendOutcome.NO_CREDIT
        assert result.limits.reset_credits_available == len(CREDITS)

    def test_a_provider_with_no_credits_and_a_blank_key_are_refused(self, make: Make) -> None:
        with pytest.raises(ValueError, match="credit"):
            make().spend("claude", idempotency_key="k1", credit_id=None)
        with pytest.raises(ValueError, match="idempotency"):
            make().spend("codex", idempotency_key=" ", credit_id=None)

    @given(
        st.lists(
            st.tuples(st.sampled_from("abc"), st.none() | st.sampled_from(["c1", "c2", "c9"])),
            max_size=8,
        )
    )
    def test_a_retry_with_the_same_key_spends_nothing_more(
        self, attempts: list[tuple[str, str | None]]
    ) -> None:
        for make in (_fake, _agent):
            reader = make()
            answered: dict[str, SpendOutcome] = {}
            for key, credit in attempts:
                result = reader.spend("codex", idempotency_key=key, credit_id=credit)
                assert result.outcome is answered.setdefault(key, result.outcome)
            spent = sum(outcome is SpendOutcome.RESET for outcome in answered.values())
            held = reader.read("codex").reset_credits_available
            assert held == len(CREDITS) - spent
            assert spent <= min(len(CREDITS), len(answered))
