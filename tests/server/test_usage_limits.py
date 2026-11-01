"""The host's reading of provider limits: when a stored reading answers, and what a failure keeps.

The files are the host's real one (``limits.pb``) in a scratch directory; the providers are
``FakeLimitsReader`` scripts. The policy is checked against a model written beside the test: over
any sequence of calls (each at a later time, with its own ``max_age_s`` and a script of what each
provider's CLI will say), every answer is exactly what the model says, the store holds exactly the
model's last good readings, and a CLI that is not asked is not started.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from tests.server.host_helper_support import fake_host

from server.host_helper_requests import registry_read_source
from server.host_operation_reads import answer_registered_read
from server.usage_limits import LIMITS_FILE, collect, decide
from server.usage_limits_store import FileLimitsStore
from vs_agent.api.testing import FakeLimitsReader
from vs_agent.api.usage_limits import (
    ProviderLimits,
    ResetCredit,
    Unavailable,
    UnavailableReason,
    Window,
    WindowKind,
)
from vs_proto.api import (
    Oneof,
    ReadRequest,
    ReadResponse,
    Timestamp,
    host_payloads,
    usage_state,
    validate,
)
from vs_sim.api.testing import SimNetwork, SimThreads

START = dt.datetime(2026, 10, 31, 12, tzinfo=dt.UTC)
PROVIDERS = ("claude", "codex")
LIVE = host_payloads.UsageLimitsStatus.LIVE
STORED = host_payloads.UsageLimitsStatus.STORED
STALE = host_payloads.UsageLimitsStatus.STALE
UNAVAILABLE = host_payloads.UsageLimitsStatus.UNAVAILABLE
FAILED = usage_state.UsageLimitsUnavailable.PROBE_FAILED


def _good(provider: str, used: float) -> ProviderLimits:
    return ProviderLimits(
        provider=provider,
        windows=(Window("w", WindowKind.WEEKLY, "Weekly", used, START, 10_080),),
        account="digest",
        plan="pro",
        reset_credits_available=1,
        reset_credits=(ResetCredit("c1", None, "Reset"),),
        usage_url="https://example.test/usage",
    )


def _failed(provider: str, reason: UnavailableReason) -> ProviderLimits:
    return ProviderLimits(provider=provider, unavailable=Unavailable(reason, "/private/path broke"))


def _request(now: dt.datetime, max_age_s: int) -> host_payloads.UsageLimitsRequest:
    return host_payloads.UsageLimitsRequest(
        request_id="r", now=Timestamp.from_datetime(now), max_age_s=max_age_s
    )


@dataclass(frozen=True)
class _Call:
    after_s: int
    max_age_s: int
    says: dict[str, str]
    """What each provider's CLI answers if it is asked: ``good``, ``failed``, ``api_key``, ``none``."""


_CALLS = st.builds(
    _Call,
    after_s=st.sampled_from([0, 1, 59, 60, 61, 3_600]),
    max_age_s=st.sampled_from([0, 60, 300]),
    says=st.fixed_dictionaries(
        {name: st.sampled_from(["good", "failed", "api_key", "none"]) for name in PROVIDERS}
    ),
)


def _reading(provider: str, says: str, used: float) -> ProviderLimits:
    return {
        "good": _good(provider, used),
        "failed": _failed(provider, UnavailableReason.PROBE_FAILED),
        "api_key": _failed(provider, UnavailableReason.API_KEY),
        "none": _failed(provider, UnavailableReason.UNSUPPORTED),
    }[says]


def _read_at(account: usage_state.UsageAccountLimits) -> dt.datetime:
    assert account.read_at is not None
    return account.read_at.to_datetime()


def _used(answer: host_payloads.UsageProviderLimits) -> float:
    assert answer.limits is not None
    return answer.limits.windows[0].used_fraction


@given(calls=st.lists(_CALLS, min_size=1, max_size=8))
@example(
    calls=[
        _Call(0, 0, {"claude": "good", "codex": "good"}),
        _Call(3_600, 300, {"claude": "failed", "codex": "failed"}),
    ]
)
@example(
    calls=[
        _Call(0, 0, {"claude": "good", "codex": "good"}),
        _Call(3_600, 300, {"claude": "api_key", "codex": "none"}),
        _Call(1, 300, {"claude": "good", "codex": "failed"}),
    ]
)
@example(
    calls=[
        _Call(0, 60, {"claude": "good", "codex": "good"}),
        _Call(59, 60, {"claude": "none", "codex": "none"}),
        _Call(2, 60, {"claude": "failed", "codex": "good"}),
    ]
)
def test_every_answer_is_what_the_model_says_and_the_store_holds_the_last_good_readings(
    calls: list[_Call],
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = FileLimitsStore(Path(raw) / LIMITS_FILE)
        now = START
        model: dict[str, tuple[dt.datetime, float]] = {}
        for index, call in enumerate(calls):
            now += dt.timedelta(seconds=call.after_s)
            used = (index + 1) / 100
            reader = FakeLimitsReader(
                {name: _reading(name, call.says[name], used) for name in PROVIDERS}
            )
            response = collect(_request(now, call.max_age_s), reader, store, PROVIDERS)
            validate(response)
            assert [each.provider for each in response.providers] == list(PROVIDERS)
            for answer in response.providers:
                provider = answer.provider
                held = model.get(provider)
                fresh = (
                    held is not None
                    and call.max_age_s > 0
                    and 0 <= (now - held[0]).total_seconds() <= call.max_age_s
                )
                if fresh:
                    assert held is not None
                    assert answer.status == STORED
                    assert _used(answer) == held[1]
                    assert provider not in reader.reads
                    continue
                assert provider in reader.reads
                says = call.says[provider]
                if says == "good":
                    assert answer.status == LIVE
                    assert answer.limits is not None
                    assert _read_at(answer.limits) == now
                    assert _used(answer) == used
                    model[provider] = (now, used)
                elif says == "failed" and held is not None:
                    assert answer.status == STALE
                    assert answer.unavailable == FAILED
                    assert _used(answer) == held[1]
                    assert answer.limits is not None
                    assert _read_at(answer.limits) == held[0]
                else:
                    assert answer.status == UNAVAILABLE
                    assert answer.limits is None
                    assert answer.unavailable is not None
                    model.pop(provider, None)
            kept = {each.provider: each for each in store.read().providers}
            assert set(kept) == set(model)
            for provider, (at, share) in model.items():
                assert _read_at(kept[provider]) == at
                assert kept[provider].windows[0].used_fraction == share


def test_a_reply_names_no_detail_of_a_failure() -> None:
    with tempfile.TemporaryDirectory() as raw:
        reader = FakeLimitsReader({"claude": _failed("claude", UnavailableReason.PROBE_FAILED)})
        response = collect(
            _request(START, 0), reader, FileLimitsStore(Path(raw) / LIMITS_FILE), ["claude"]
        )
    assert "/private" not in response.to_json()


def test_a_second_client_within_the_age_starts_no_cli() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = FileLimitsStore(Path(raw) / LIMITS_FILE)
        first = FakeLimitsReader({name: _good(name, 0.2) for name in PROVIDERS})
        collect(_request(START, 0), first, store, PROVIDERS)
        second = FakeLimitsReader({name: _good(name, 0.9) for name in PROVIDERS})
        response = collect(_request(START + dt.timedelta(seconds=30), 60), second, store, PROVIDERS)
    assert second.reads == []
    assert [each.status for each in response.providers] == [STORED, STORED]
    assert [_used(each) for each in response.providers] == [0.2, 0.2]


@given(
    st.integers(-3_600, 3_600),
    st.integers(0, 86_400),
)
def test_a_reading_is_fresh_only_when_it_is_not_from_the_future_and_within_the_age(
    age_s: int, max_age_s: int
) -> None:
    stored = usage_state.UsageAccountLimits(
        provider="codex", read_at=Timestamp.from_datetime(START)
    )
    now = START + dt.timedelta(seconds=age_s)
    assert decide(stored, now=now, max_age_s=max_age_s) == (0 <= age_s <= max_age_s > 0)
    assert not decide(None, now=now, max_age_s=max_age_s)


class TestTheFile:
    def test_a_damaged_or_foreign_file_is_no_readings_and_the_next_read_rebuilds_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / LIMITS_FILE
            store = FileLimitsStore(path)
            assert store.read().providers == []
            path.write_bytes(b"\xff\xfenot a message")
            assert store.read().providers == []
            path.write_bytes(usage_state.UsageLimitsState(version=999).to_binary())
            assert store.read().providers == []
            reader = FakeLimitsReader({"codex": _good("codex", 0.5)})
            collect(_request(START, 0), reader, store, ["codex"])
            assert [each.provider for each in store.read().providers] == ["codex"]

    def test_the_directory_is_made_and_a_write_that_cannot_happen_costs_only_the_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            nested = Path(raw) / "state" / "usage" / LIMITS_FILE
            reader = FakeLimitsReader({"codex": _good("codex", 0.5)})
            collect(_request(START, 0), reader, FileLimitsStore(nested), ["codex"])
            assert [each.provider for each in FileLimitsStore(nested).read().providers] == ["codex"]
            blocked = Path(raw) / "a-file"
            blocked.write_bytes(b"")
            response = collect(
                _request(START, 0), reader, FileLimitsStore(blocked / LIMITS_FILE), ["codex"]
            )
            assert [each.status for each in response.providers] == [LIVE]

    def test_a_failure_that_changes_nothing_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / LIMITS_FILE
            reader = FakeLimitsReader({"codex": _failed("codex", UnavailableReason.PROBE_FAILED)})
            collect(_request(START, 0), reader, FileLimitsStore(path), ["codex"])
            assert not path.exists()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_request_without_a_time_is_refused(provider: str) -> None:
    with tempfile.TemporaryDirectory() as raw:
        request = host_payloads.UsageLimitsRequest(request_id="r")
        with pytest.raises(ValueError, match="now"):
            collect(
                request, FakeLimitsReader({}), FileLimitsStore(Path(raw) / LIMITS_FILE), [provider]
            )


class TestTheRegisteredRead:
    """``vibesys host usage-limits``: the wire in, the wire out, and the file under the state home."""

    def _ask(
        self, reader: FakeLimitsReader, max_age_s: int, at: dt.datetime
    ) -> host_payloads.UsageLimitsResponse:
        envelope = ReadRequest(
            checkout="/checkout", method=Oneof("usage_limits", _request(at, max_age_s))
        )
        validate(envelope)
        source = registry_read_source(
            fake_host(SimThreads(), SimNetwork(SimThreads())), limits=lambda: reader
        )
        reply = ReadResponse.from_binary(
            answer_registered_read("usage-limits", envelope.to_binary(), source)
        )
        assert reply.result is not None
        assert reply.result.field == "usage_limits"
        assert isinstance(reply.result.value, host_payloads.UsageLimitsResponse)
        return reply.result.value

    def test_a_read_is_answered_kept_and_then_served_from_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
        first = FakeLimitsReader({name: _good(name, 0.25) for name in PROVIDERS})
        answered = self._ask(first, 0, START)
        assert [(each.provider, each.status) for each in answered.providers] == [
            ("claude", LIVE),
            ("codex", LIVE),
        ]
        assert (tmp_path / "state" / "usage" / LIMITS_FILE).is_file()
        second = FakeLimitsReader({name: _good(name, 0.75) for name in PROVIDERS})
        served = self._ask(second, 120, START + dt.timedelta(seconds=90))
        assert second.reads == []
        assert [each.status for each in served.providers] == [STORED, STORED]
        assert [_used(each) for each in served.providers] == [0.25, 0.25]
