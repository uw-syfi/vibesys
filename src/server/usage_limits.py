"""What each provider's plan limits are now, and the host's last good reading of them.

``usage.limits`` asks each provider's CLI (through ``vs_agent.api.usage_limits``, in the
environment a run finds it in, with no prompt and no model call) for its account's windows and
reset credits, and keeps the last good reading of each in ``limits.pb`` in the host's usage state
directory. The file is a cache of the providers: damage rebuilds it with the next read.

Per provider, ``decide`` and ``settle`` are the whole policy and are pure; ``collect`` is the shell
that reads the store, asks the reader for the providers ``decide`` says need it, and writes the
store back:

- a stored reading younger than the request's ``max_age_s`` is answered as it is (``STORED``), so a
  second client of the host does not start a CLI;
- otherwise the provider is read. A reading is ``LIVE`` and replaces the stored one;
- a read that fails now (``PROBE_FAILED``) keeps the last good reading and shows it as ``STALE``
  with its own age, or is ``UNAVAILABLE`` when there is none;
- a provider that has no limits to read (``UNSUPPORTED``, or a login billed by token) is
  ``UNAVAILABLE`` and its stored reading is dropped: it describes an account that is not in use.

A reply carries no detail text of a failure: it can name a path, and the screen shows a status word.

Public surface: ``LIMITS_FILE``, ``PROBE_TIMEOUT_S``, ``LimitsStore``, ``FileLimitsStore``,
``decide``, ``settle``, ``Settled``, ``collect``, ``read_here`` and ``reader_here``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol

from server.host_agent_logins import launch_environment
from vs_agent.api.usage_limits import AgentLimitsReader, LimitsReader, UnavailableReason
from vs_agent.api.usage_limits import providers as readable_providers
from vs_project.api import Project, ProjectError, atomic_write_bytes
from vs_proto.api import Timestamp, ValidationError, host_payloads, usage_state, validate

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from vs_agent.api.usage_limits import ProviderLimits, ResetCredit, Window

LIMITS_FILE: Final = "limits.pb"
LIMITS_VERSION: Final = 1

PROBE_TIMEOUT_S: Final = 12.0
"""What one provider's read may take. The providers are read one after another, so two stay inside
the operation's 30 second cap."""

_KINDS: Final = {
    "session": usage_state.UsageLimitKind.SESSION,
    "weekly": usage_state.UsageLimitKind.WEEKLY,
    "monthly": usage_state.UsageLimitKind.MONTHLY,
    "other": usage_state.UsageLimitKind.OTHER,
}
_REASONS: Final = {
    UnavailableReason.UNSUPPORTED: usage_state.UsageLimitsUnavailable.UNSUPPORTED,
    UnavailableReason.API_KEY: usage_state.UsageLimitsUnavailable.API_KEY,
    UnavailableReason.PROBE_FAILED: usage_state.UsageLimitsUnavailable.PROBE_FAILED,
}
_STALE_REASONS: Final = frozenset({UnavailableReason.PROBE_FAILED})
"""The failures that may be transient: a stored reading outlives them. The others say the account
has no limits, so a stored reading would describe an account that is not in use."""


class LimitsStore(Protocol):
    """Where the last good reading of each provider is kept."""

    def read(self) -> usage_state.UsageLimitsState:
        """The stored readings; empty when there are none or the file fails its rules."""
        ...

    def write(self, state: usage_state.UsageLimitsState) -> None:
        """Replace the stored readings; a write that fails loses only the next read's fallback."""
        ...


class FileLimitsStore:
    """``LimitsStore`` in ``limits.pb``, written atomically."""

    def __init__(self, path: Path) -> None:
        """Keep the readings at ``path``."""
        self._path = path

    def read(self) -> usage_state.UsageLimitsState:
        """The stored readings; a missing, undecodable or invalid file is no readings."""
        try:
            found = usage_state.UsageLimitsState.from_binary(self._path.read_bytes())
            validate(found)
        except (OSError, ValueError, ValidationError):
            return usage_state.UsageLimitsState(version=LIMITS_VERSION)
        if found.version != LIMITS_VERSION:
            return usage_state.UsageLimitsState(version=LIMITS_VERSION)
        return found

    def write(self, state: usage_state.UsageLimitsState) -> None:
        """Replace the file, creating its directory; an I/O error loses only the fallback."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write_bytes(self._path, state.to_binary())
        except (OSError, ProjectError):
            return


def decide(stored: usage_state.UsageAccountLimits | None, *, now: datetime, max_age_s: int) -> bool:
    """Whether ``stored`` can be answered as it is: it exists and is at most ``max_age_s`` old.

    A reading from the future (the clock moved back) is never fresh. Zero ages out everything.
    """
    if stored is None or stored.read_at is None or max_age_s <= 0:
        return False
    age = (now - stored.read_at.to_datetime()).total_seconds()
    return 0 <= age <= max_age_s


@dataclass(frozen=True)
class Settled:
    """One provider's answer, and the reading to keep for it."""

    answer: host_payloads.UsageProviderLimits
    keep: usage_state.UsageAccountLimits | None
    """What the store holds for the provider afterwards; ``None`` drops it."""


def settle(
    provider: str,
    stored: usage_state.UsageAccountLimits | None,
    read: ProviderLimits | None,
    *,
    now: datetime,
) -> Settled:
    """The answer for ``provider`` given what is stored and what a read found.

    ``read`` is ``None`` when ``decide`` said the stored reading is fresh and nobody asked the CLI.
    """
    status = host_payloads.UsageLimitsStatus
    if read is None:
        if stored is None:
            message = "a provider is read unless a stored reading answers it"
            raise ValueError(message)
        return Settled(_answer(provider, status.STORED, stored), stored)
    if read.unavailable is None:
        made = account_limits(read, now=now)
        return Settled(_answer(provider, status.LIVE, made), made)
    reason = _REASONS[read.unavailable.reason]
    if stored is not None and read.unavailable.reason in _STALE_REASONS:
        return Settled(_answer(provider, status.STALE, stored, reason), stored)
    answer = host_payloads.UsageProviderLimits(
        provider=provider, status=status.UNAVAILABLE, unavailable=reason
    )
    return Settled(answer, None)


def account_limits(read: ProviderLimits, *, now: datetime) -> usage_state.UsageAccountLimits:
    """A reading as the host stores and answers it, stamped ``now``. Only a good read is one."""
    made = usage_state.UsageAccountLimits(provider=read.provider, read_at=_timestamp(now))
    if read.account is not None:
        made.account = read.account
    if read.plan is not None:
        made.plan = read.plan
    if read.reset_credits_available is not None:
        made.reset_credits_available = read.reset_credits_available
    if read.usage_url is not None:
        made.usage_url = read.usage_url
    made.windows.extend(_window(window) for window in read.windows)
    made.reset_credits.extend(_credit(credit) for credit in read.reset_credits)
    return made


def collect(
    request: host_payloads.UsageLimitsRequest,
    reader: LimitsReader,
    store: LimitsStore,
    providers: Iterable[str],
) -> host_payloads.UsageLimitsResponse:
    """The reply to ``request``: each of ``providers`` answered from the store or a read.

    The store is written once, and only when a reading changed.
    """
    if request.now is None:
        message = "now: required"
        raise ValueError(message)
    now = request.now.to_datetime()
    held = {each.provider: each for each in store.read().providers}
    kept: dict[str, usage_state.UsageAccountLimits] = dict(held)
    answers: list[host_payloads.UsageProviderLimits] = []
    for provider in providers:
        stored = held.get(provider)
        read: ProviderLimits | None = None
        if not decide(stored, now=now, max_age_s=request.max_age_s):
            read = reader.read(provider)
        settled = settle(provider, stored, read, now=now)
        answers.append(settled.answer)
        if settled.keep is None:
            kept.pop(provider, None)
        else:
            kept[provider] = settled.keep
    if kept != held:
        store.write(
            usage_state.UsageLimitsState(
                version=LIMITS_VERSION, providers=[kept[name] for name in sorted(kept)]
            )
        )
    return host_payloads.UsageLimitsResponse(
        request_id=request.request_id, providers=answers, read_at=_timestamp(now)
    )


def read_here(
    request: host_payloads.UsageLimitsRequest, reader: LimitsReader
) -> host_payloads.UsageLimitsResponse:
    """The reply to ``request`` for every provider that has limits, kept in the host's usage state."""
    store = FileLimitsStore(Project.usage_state_directory() / LIMITS_FILE)
    return collect(request, reader, store, readable_providers())


def reader_here() -> LimitsReader:
    """The reader for this machine: each provider's CLI, found where a run finds it."""
    return AgentLimitsReader(launch_environment(), PROBE_TIMEOUT_S)


def _answer(
    provider: str,
    status: host_payloads.UsageLimitsStatus,
    limits: usage_state.UsageAccountLimits,
    unavailable: usage_state.UsageLimitsUnavailable | None = None,
) -> host_payloads.UsageProviderLimits:
    answer = host_payloads.UsageProviderLimits(provider=provider, status=status, limits=limits)
    if unavailable is not None:
        answer.unavailable = unavailable
    return answer


def _window(window: Window) -> usage_state.UsageLimitWindow:
    wire = usage_state.UsageLimitWindow(
        id=window.id,
        kind=_KINDS[window.kind.value],
        label=window.label,
        used_fraction=_share(window.used_fraction),
    )
    if window.resets_at is not None:
        wire.resets_at = _timestamp(window.resets_at)
    if window.window_minutes is not None:
        wire.window_minutes = window.window_minutes
    return wire


def _credit(credit: ResetCredit) -> usage_state.UsageResetCredit:
    held = usage_state.UsageResetCredit(id=credit.id)
    if credit.expires_at is not None:
        held.expires_at = _timestamp(credit.expires_at)
    if credit.title is not None:
        held.title = credit.title[:128]
    return held


def _share(fraction: float) -> float:
    return min(max(fraction, 0.0), 1_000_000.0) if math.isfinite(fraction) else 0.0


def _timestamp(at: datetime) -> Timestamp:
    return Timestamp.from_datetime(at.astimezone(UTC))


__all__ = [
    "LIMITS_FILE",
    "PROBE_TIMEOUT_S",
    "FileLimitsStore",
    "LimitsReader",
    "LimitsStore",
    "Settled",
    "account_limits",
    "collect",
    "decide",
    "read_here",
    "reader_here",
    "settle",
]
