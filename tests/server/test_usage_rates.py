"""The host keeps one price list and one price book on disk, and a read never touches the network.

The files are the host's real ones (``rates.pb``, ``prices.pb``) in a scratch directory; the network
is a scripted client that answers or fails. What a person set is the only copy, so a save is
checked against the revision it was made from, and a retry of the same save changes nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from server.usage_pricing import PriceBookError
from server.usage_scan import (
    FilePriceBook,
    HttpsFetcher,
    ProtoRateStore,
    refresh_rates,
    save_prices,
    stored_pricing,
)
from vibesys.api.host_operations.api import (
    PricesRefusedError,
    serve_usage_prices,
)
from vs_project.api.testing import FakeAdmissionStore
from vs_proto.api import (
    MutateRequest,
    MutateResponse,
    Oneof,
    Timestamp,
    UsageSpeed,
    ValidationError,
    host_payloads,
    usage_state,
    validate,
)
from vs_sim.api import HttpsGetError
from vs_sim.api.testing import FakeHttpsGet

START = dt.datetime(2026, 10, 31, 12, tzinfo=dt.UTC)
FLOOR = dt.timedelta(seconds=60)
TTL = dt.timedelta(hours=24)


def _document(price: float) -> bytes:
    return json.dumps(
        {"listed-model": {"input_cost_per_token": price, "output_cost_per_token": price}}
    ).encode()


@dataclass(frozen=True)
class _Step:
    after: dt.timedelta
    answer: str
    """``good``, ``changed`` (another good document), ``error``, ``garbage`` or ``empty``."""
    force: bool


_STEPS = st.builds(
    _Step,
    after=st.sampled_from(
        [
            dt.timedelta(0),
            dt.timedelta(seconds=30),
            dt.timedelta(seconds=61),
            dt.timedelta(hours=2),
            dt.timedelta(hours=25),
        ]
    ),
    answer=st.sampled_from(["good", "changed", "error", "garbage", "empty"]),
    force=st.booleans(),
)


def _outcome(answer: str) -> bytes | HttpsGetError:
    return {
        "good": _document(1e-6),
        "changed": _document(2e-6),
        "garbage": b"<html>not a price list</html>",
        "empty": b"{}",
    }.get(answer) or HttpsGetError("the network is down")


@settings(max_examples=40)
@given(steps=st.lists(_STEPS, max_size=10))
@example(
    steps=[
        _Step(dt.timedelta(0), "good", force=False),
        _Step(dt.timedelta(hours=2), "garbage", force=True),
    ]
)
def test_a_good_list_is_never_replaced_by_a_bad_one_and_the_floor_holds(steps: list[_Step]) -> None:
    """Over any sequence of answers and times: the policy of the document's life.

    A fetch happens when no attempt was made in the last minute, and either nothing good is held,
    it is a day old, or the caller forces it. A document that is not a price list, an empty one,
    and a failure leave the last good document in place and still count as an attempt.
    """
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        now = START
        held: bytes | None = None
        held_at: dt.datetime | None = None
        last_attempt: dt.datetime | None = None
        for step in steps:
            now += step.after
            network = FakeHttpsGet(_outcome(step.answer))
            refresh_rates(directory, HttpsFetcher(network), now=now, force=step.force)
            due = held is None or step.force or (held_at is not None and now - held_at >= TTL)
            in_floor = last_attempt is not None and abs(now - last_attempt) < FLOOR
            fetched = bool(network.requests)
            assert fetched == (due and not in_floor)
            if fetched:
                last_attempt = now
                answered = _outcome(step.answer)
                if step.answer in {"good", "changed"} and isinstance(answered, bytes):
                    held, held_at = answered, now
            stored = ProtoRateStore(directory / "rates.pb").read()
            assert (stored.document if stored is not None else None) == held


@given(damage=st.binary(max_size=200))
def test_a_damaged_list_file_is_a_miss_and_a_read_still_prices(damage: bytes) -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        (directory / "rates.pb").write_bytes(damage)
        pricing = stored_pricing(directory, now=START)
        assert pricing.pricing.status in {
            usage_state.UsagePricingStatus.BUNDLED,
            usage_state.UsagePricingStatus.CACHED,
            usage_state.UsagePricingStatus.FRESH,
        }
        assert pricing.pricing.known_models > 0


def test_a_read_never_fetches() -> None:
    """``stored_pricing`` takes no client: it can only read what is on disk."""
    with tempfile.TemporaryDirectory() as raw:
        pricing = stored_pricing(Path(raw), now=START)
        assert pricing.pricing.status is usage_state.UsagePricingStatus.BUNDLED
        assert pricing.pricing.refresh_due


def test_a_fetched_list_prices_a_model_the_pinned_table_does_not_know() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        refresh_rates(
            directory, HttpsFetcher(FakeHttpsGet(_document(1e-6))), now=START, force=False
        )
        pricing = stored_pricing(directory, now=START + dt.timedelta(hours=1))
        assert pricing.pricing.status is usage_state.UsagePricingStatus.FRESH
        rate, source = pricing.resolved("claude_code", "listed-model", UsageSpeed.STANDARD)
        assert source is usage_state.UsageEstimateSource.MODEL
        assert rate is not None
        assert rate.input == pytest.approx(1.0)


# --- The price book ---


def _save(
    directory: Path,
    revision: int,
    overrides: dict[str, float],
    aliases: dict[str, str] | None = None,
) -> host_payloads.UsagePricesSaveResponse:
    return save_prices(
        directory,
        host_payloads.UsagePricesSaveRequest(
            request_id="r",
            base_revision=revision,
            overrides=[
                usage_state.UsageRateOverride(
                    model=model, rate=usage_state.UsageRate(input=price, output=price)
                )
                for model, price in overrides.items()
            ],
            aliases=[usage_state.UsageAlias(from_=a, to=b) for a, b in (aliases or {}).items()],
        ),
    )


def test_a_save_against_the_hosts_revision_is_kept_and_counts_up() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        first = _save(directory, 0, {"Model-A": 2.0})
        assert first.prices is not None
        assert (first.saved, first.prices.revision) == (True, 1)
        assert FilePriceBook(directory / "prices.pb").read() == first.prices
        second = _save(directory, 1, {"model-a": 2.0, "model-b": 0.0})
        assert second.prices is not None
        assert (second.saved, second.prices.revision) == (True, 2)


def test_a_save_made_against_an_old_revision_changes_nothing_and_returns_the_hosts_book() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        kept = _save(directory, 0, {"model-a": 2.0}).prices
        refused = _save(directory, 0, {"model-a": 9.0})
        assert refused.saved is False
        assert refused.prices == kept
        assert FilePriceBook(directory / "prices.pb").read() == kept


@given(
    models=st.dictionaries(st.sampled_from(["a", "b", "C"]), st.floats(0, 100), max_size=3),
    again=st.integers(min_value=1, max_value=3),
)
def test_saving_the_same_book_again_is_a_no_op_at_any_revision(
    models: dict[str, float], again: int
) -> None:
    """A retry after an unknown outcome has the old revision and the same content: it is saved."""
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        first = _save(directory, 0, models)
        for _ in range(again):
            retry = _save(directory, 0, models)
            assert retry.saved is True
            assert retry.prices == first.prices


def test_a_book_that_repeats_a_key_or_loops_is_refused_and_nothing_is_written() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        with pytest.raises(PriceBookError, match="aliases"):
            _save(directory, 0, {}, {"a": "b", "b": "a"})
        assert not (directory / "prices.pb").exists()


def test_a_damaged_book_reads_as_empty_so_the_next_save_replaces_it() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        (directory / "prices.pb").write_bytes(b"\xff\xfe broken")
        assert FilePriceBook(directory / "prices.pb").read().revision == 0
        assert _save(directory, 0, {"a": 1.0}).saved


# --- The admitted save ---


def _envelope(request_id: str, overrides: dict[str, float]) -> bytes:
    request = MutateRequest(
        checkout="/src/checkout",
        method=Oneof(
            "usage_prices_save",
            host_payloads.UsagePricesSaveRequest(
                request_id=request_id,
                base_revision=0,
                overrides=[
                    usage_state.UsageRateOverride(
                        model=model, rate=usage_state.UsageRate(input=price, output=price)
                    )
                    for model, price in overrides.items()
                ],
            ),
        ),
    )
    validate(request)
    return request.to_binary()


def test_a_save_request_id_applies_once_and_then_replays() -> None:
    applied: list[str] = []

    def apply(
        request: host_payloads.UsagePricesSaveRequest,
    ) -> host_payloads.UsagePricesSaveResponse:
        applied.append(request.request_id)
        return host_payloads.UsagePricesSaveResponse(
            request_id=request.request_id, prices=usage_state.UsagePriceBook(revision=1), saved=True
        )

    store = FakeAdmissionStore()
    payload = _envelope("request-a", {"model-a": 1.0})
    first = MutateResponse.from_binary(
        serve_usage_prices(payload, store=store, host_user="host-a", apply=apply)
    )
    second = MutateResponse.from_binary(
        serve_usage_prices(payload, store=store, host_user="host-a", apply=apply)
    )
    assert applied == ["request-a"]
    assert first.result is not None
    assert first.result.field == "usage_prices_save"
    assert second.result == first.result


def test_a_refused_book_is_stored_as_a_refusal_and_not_applied_again() -> None:
    applied = 0

    def apply(
        request: host_payloads.UsagePricesSaveRequest,
    ) -> host_payloads.UsagePricesSaveResponse:
        nonlocal applied
        applied += 1
        message = f"aliases: {request.request_id!r} maps to itself"
        raise PricesRefusedError(message)

    store = FakeAdmissionStore()
    payload = _envelope("request-b", {})
    first = MutateResponse.from_binary(
        serve_usage_prices(payload, store=store, host_user="host-a", apply=apply)
    )
    second = MutateResponse.from_binary(
        serve_usage_prices(payload, store=store, host_user="host-a", apply=apply)
    )
    assert applied == 1
    assert first.result is not None
    assert first.result.field == "failure"
    assert "maps to itself" in first.result.value.message
    assert second.result == first.result


def test_a_save_over_two_overrides_of_one_model_is_invalid_at_the_wire() -> None:
    request = host_payloads.UsagePricesSaveRequest(
        request_id="r",
        overrides=[
            usage_state.UsageRateOverride(model="a", rate=usage_state.UsageRate()),
            usage_state.UsageRateOverride(model="a", rate=usage_state.UsageRate()),
        ],
    )
    with pytest.raises(ValidationError):
        validate(request)


def test_the_refresh_request_needs_a_time() -> None:
    with pytest.raises(ValidationError):
        validate(host_payloads.UsageRatesRefreshRequest(request_id="r"))
    validate(
        host_payloads.UsageRatesRefreshRequest(request_id="r", now=Timestamp.from_datetime(START))
    )
