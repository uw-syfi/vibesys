"""Agent-supplied identifiers have one canonical spelling, checked at the model boundary."""

from __future__ import annotations

import unicodedata

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from vs_runtime.api import AgentId, validate_member_id

_AGENT_ID = TypeAdapter(AgentId)


def _canonical(value: str) -> bool:
    return (
        0 < len(value) <= 128
        and value.isprintable()
        and value == value.strip()
        and unicodedata.is_normalized("NFC", value)
    )


# Readable ids an agent might choose: letters, marks, digits, punctuation,
# symbols, and inner spaces, including dots, slashes, and uppercase.
_READABLE = st.text(
    st.characters(categories=("L", "M", "N", "P", "S"), include_characters=" "),
    min_size=1,
    max_size=128,
).filter(_canonical)


@given(value=st.text(max_size=160))
@example(value="0 ")
@example(value=" H1")
@example(value="H1\n")
@example(value="a\u200bb")
@example(value="a\u00a0b")
@example(value="e\u0301")
@example(value="")
@example(value="KV.Cache_v2 / ../Ünïcode")
@example(value="..")
@example(value="UPPER")
def test_an_identifier_is_accepted_unchanged_exactly_when_it_is_canonical(value: str) -> None:
    if _canonical(value):
        assert _AGENT_ID.validate_python(value) == value
        validate_member_id(value)
        return
    with pytest.raises(ValidationError) as rejected:
        _AGENT_ID.validate_python(value)
    if value:
        # The error quotes the offending value, so a correction turn can act on it.
        assert repr(value) in str(rejected.value)
    if len(value) <= 128:
        with pytest.raises(ValueError, match="invalid agent member ID"):
            validate_member_id(value)


@given(value=_READABLE)
def test_readable_identifiers_keep_case_dots_and_inner_spaces(value: str) -> None:
    assert _AGENT_ID.validate_python(value) == value


@given(value=_READABLE, padding=st.sampled_from([" ", "\t", "\n", "\u00a0", "\u3000"]))
def test_padded_identifier_is_rejected_with_its_trimmed_spelling(value: str, padding: str) -> None:
    for padded in (padding + value, value + padding):
        with pytest.raises(ValidationError, match="not a valid identifier"):
            _AGENT_ID.validate_python(padded)


def test_whitespace_padding_names_the_spelling_to_use() -> None:
    with pytest.raises(ValidationError, match=r"leading or trailing whitespace; use '0'"):
        _AGENT_ID.validate_python("0 ")
