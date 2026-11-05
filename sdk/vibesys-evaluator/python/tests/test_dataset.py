"""The dataset file: deterministic JSON of the task's dataset, refused where JSON cannot hold it."""

from __future__ import annotations

import base64
import datetime
import json
from dataclasses import dataclass

import pytest
from hypothesis import given
from hypothesis import strategies as st

# test-isolation: the dataset file is written by main()'s private benchmark command.
from vibesys_evaluator._private.dataset import DatasetError, digest, encode

from .vse_support import kv


@dataclass(frozen=True)
class Hotel:
    id: str
    rooms: int
    opened: datetime.date
    tags: frozenset[str]


def test_dataclasses_objects_and_bytes_become_json() -> None:
    data = {
        "hotels": (Hotel("1", 3, datetime.date(2015, 4, 9), frozenset({"b", "a"})),),
        "blob": b"\x00\xff",
    }
    assert json.loads(encode(data)) == {
        "hotels": [{"id": "1", "rooms": 3, "opened": "2015-04-09", "tags": ["a", "b"]}],
        "blob": base64.b64encode(b"\x00\xff").decode(),
    }
    users = json.loads(encode(kv.Data()))["users"]
    assert users[0][0] == "u0"


def test_the_digest_is_the_same_for_the_same_dataset() -> None:
    assert digest(encode(kv.Data())) == digest(encode(kv.Data()))


@given(
    st.recursive(
        st.none()
        | st.booleans()
        | st.integers()
        | st.text()
        | st.floats(allow_nan=False, allow_infinity=False),
        lambda inner: st.lists(inner) | st.dictionaries(st.text(), inner),
        max_leaves=20,
    )
)
def test_plain_json_values_are_written_as_they_are(value: object) -> None:
    assert json.loads(encode(value)) == value


@pytest.mark.parametrize(
    "value", [float("nan"), {"x": [object()]}, {"f": float("inf")}], ids=["nan", "object", "inf"]
)
def test_values_json_cannot_hold_are_refused_with_their_path(value: object) -> None:
    with pytest.raises(DatasetError, match="dataset"):
        encode(value)
