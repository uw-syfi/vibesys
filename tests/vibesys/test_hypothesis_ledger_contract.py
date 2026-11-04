"""The hypothesis ledger's persisted carry-over is decoded strictly.

The carry-over (the regression and exhaustion notices for the next planning turn) is saved in
the strategy state and read back on the next turn and on resume. A key it does not declare is
a field written under another name, and dropping it would lose the notice without a word.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.hypothesis import (
    CarryOver,
    ExhaustionNotice,
    HypothesisState,
    OfficialCandidateNotRetained,
    rounds,
)

_ROUNDS = st.integers(min_value=0, max_value=99)
_TEXT = st.none() | st.text(max_size=8)
_EXHAUSTION = st.builds(ExhaustionNotice, round_number=_ROUNDS, attempts=_ROUNDS, feedback=_TEXT)
_REGRESSION = st.builds(
    OfficialCandidateNotRetained,
    round_number=_ROUNDS,
    perf_metric=st.none() | st.integers(-5, 5) | st.floats(allow_nan=False, allow_infinity=False),
    perf_unit=_TEXT,
)
_CARRIES = st.builds(
    CarryOver, regression=st.none() | _REGRESSION, exhaustion=st.none() | _EXHAUSTION
)
_UNKNOWN_KEYS = st.from_regex(r"[a-z][a-z_]{0,15}", fullmatch=True).filter(
    lambda key: key not in {"regression", "exhaustion"}
)


def _encoded(carry: CarryOver) -> rounds.EncodedLedger:
    return rounds.encode(rounds.Ledger(HypothesisState(), carry))


@given(_CARRIES)
def test_every_carry_over_round_trips_through_the_ledger(carry: CarryOver) -> None:
    assert rounds.decode(_encoded(carry)).carry == carry


@given(_CARRIES, _UNKNOWN_KEYS, st.none() | st.integers() | st.text(max_size=8))
def test_a_carry_over_with_an_undeclared_key_is_rejected_naming_the_key(
    carry: CarryOver, key: str, value: object
) -> None:
    encoded = _encoded(carry)
    document = {**json.loads(encoded.carry_json), key: value}
    tampered = rounds.EncodedLedger(encoded.search_json, json.dumps(document))
    with pytest.raises(ValidationError) as raised:
        rounds.decode(tampered)
    assert [error["loc"] for error in raised.value.errors()] == [(key,)]
