"""A provider account's plan limits, read on request: the Usage screen's view of agentshim.

agentshim owns how each CLI is asked (the control request one answers, the app-server method the
other does, what their replies mean, how a turn's rate-limit event names the same window). This
sub-facade is the one place VibeSys code reaches it for the Limits tab, and none of agentshim's
types cross it: a window, a credit and a reading are the frozen dataclasses below.

``LimitsReader`` is the role the host calls. ``AgentLimitsReader`` implements it over agentshim,
starting each provider's CLI in the environment a run finds it in, with no prompt and no model
call; ``vs_agent.api.testing.FakeLimitsReader`` implements it from a script. A read that cannot
be done is a reading with ``unavailable`` set, never an exception, so a caller keeps its last good
reading and says why it is old. Spending a reset credit is a separate method, because it costs the
account something that cannot be undone.

Public surface: ``WindowKind``, ``Window``, ``ResetCredit``, ``UnavailableReason``,
``Unavailable``, ``ProviderLimits``, ``SpendOutcome``, ``SpendResult``, ``LimitsReader``,
``AgentLimitsReader`` and ``providers``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import agentshim

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from agentshim.execution.executor import CommandExecutor


class WindowKind(StrEnum):
    """What a window is, in words every provider shares."""

    SESSION = "session"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    OTHER = "other"


@dataclass(frozen=True)
class Window:
    """One limit of an account: a share of an allowance and when it refills."""

    id: str
    """Stable for the same window across reads; readings merge by it."""
    kind: WindowKind
    label: str
    used_fraction: float
    """The share used, ``0.0`` to ``1.0``; above 1 when the provider reports over-use."""
    resets_at: datetime | None
    window_minutes: int | None


@dataclass(frozen=True)
class ResetCredit:
    """One credit that refills the account's limits at once when spent."""

    id: str
    expires_at: datetime | None
    title: str | None


class UnavailableReason(StrEnum):
    """Why a read has no windows."""

    UNSUPPORTED = "unsupported"
    """The provider has no limits read."""
    API_KEY = "api_key"
    """The login is billed by token, so no plan applies."""
    PROBE_FAILED = "probe_failed"
    """No CLI, not signed in, timed out, or an error answer."""


@dataclass(frozen=True)
class Unavailable:
    """A read that produced no windows, and why."""

    reason: UnavailableReason
    detail: str
    """For the host's log; it can name a path, so it never goes into a reply."""


@dataclass(frozen=True)
class ProviderLimits:
    """What one read of one provider's limits returned."""

    provider: str
    windows: tuple[Window, ...] = ()
    account: str | None = None
    """A digest of the account, never an email or an id: equal across machines for one account."""
    plan: str | None = None
    reset_credits_available: int | None = None
    """The count the provider states; ``reset_credits`` can list fewer."""
    reset_credits: tuple[ResetCredit, ...] = ()
    usage_url: str | None = None
    unavailable: Unavailable | None = None


class SpendOutcome(StrEnum):
    """How the provider answered a request to spend a reset credit."""

    RESET = "reset"
    NOTHING_TO_RESET = "nothing_to_reset"
    NO_CREDIT = "no_credit"
    ALREADY_REDEEMED = "already_redeemed"


@dataclass(frozen=True)
class SpendResult:
    """What spending a credit did, and the limits read after it."""

    outcome: SpendOutcome
    limits: ProviderLimits


class LimitsReader(Protocol):
    """Reads provider limits where a run would run the provider's CLI."""

    def read(self, provider: str) -> ProviderLimits:
        """Read ``provider``'s limits now. Never raises for the CLI; ``unavailable`` says why."""
        ...

    def spend(self, provider: str, *, idempotency_key: str, credit_id: str | None) -> SpendResult:
        """Spend one reset credit: ``credit_id``, or the provider's next one when ``None``.

        ``idempotency_key`` names one logical attempt: a retry with it spends at most once.
        Raises ``ValueError`` for a provider with no credits or an empty key, and
        ``RuntimeError`` when the call did not settle (the credit may or may not be spent: read).
        """
        ...


def providers() -> tuple[str, ...]:
    """The providers whose limits can be read, in name order."""
    return agentshim.limit_providers()


class AgentLimitsReader:
    """``LimitsReader`` over agentshim, for the environment ``env`` a run finds provider CLIs in."""

    def __init__(
        self,
        env: Mapping[str, str],
        timeout_s: float,
        *,
        executor: CommandExecutor | None = None,
    ) -> None:
        """Bind the environment and the bound on one provider's read.

        ``executor`` is for tests: the process fakes of ``vs_agent.api.testing``; a host passes
        none and the CLI runs on this machine.
        """
        self._env = dict(env)
        self._timeout_s = timeout_s
        self._executor = executor

    def read(self, provider: str) -> ProviderLimits:
        """Read ``provider``'s limits through agentshim."""
        return _provider_limits(
            agentshim.read_limits(
                provider, executor=self._executor, env=self._env, timeout=self._timeout_s
            )
        )

    def spend(self, provider: str, *, idempotency_key: str, credit_id: str | None) -> SpendResult:
        """Spend one credit through agentshim, then read again."""
        try:
            result = agentshim.use_reset_credit(
                provider,
                idempotency_key=idempotency_key,
                credit_id=credit_id,
                executor=self._executor,
                env=self._env,
                timeout=self._timeout_s,
            )
        except agentshim.ProviderCapabilityError as error:
            raise ValueError(str(error)) from error
        except agentshim.AgentShimError as error:
            message = f"the reset did not settle: {error}"
            raise RuntimeError(message) from error
        return SpendResult(
            outcome=SpendOutcome(result.outcome.value), limits=_provider_limits(result.limits)
        )


def _provider_limits(read: agentshim.AccountLimits) -> ProviderLimits:
    held = read.reset_credits
    return ProviderLimits(
        provider=read.provider,
        windows=tuple(
            Window(
                id=window.id,
                kind=WindowKind(window.kind.value),
                label=window.label,
                used_fraction=window.used_fraction,
                resets_at=window.resets_at,
                window_minutes=window.window_minutes,
            )
            for window in read.windows
        ),
        account=read.account,
        plan=read.plan,
        reset_credits_available=None if held is None else held.available,
        reset_credits=()
        if held is None
        else tuple(
            ResetCredit(id=credit.id, expires_at=credit.expires_at, title=credit.title)
            for credit in held.credits
        ),
        usage_url=read.usage_url,
        unavailable=None
        if read.unavailable is None
        else Unavailable(UnavailableReason(read.unavailable.reason.value), read.unavailable.detail),
    )
