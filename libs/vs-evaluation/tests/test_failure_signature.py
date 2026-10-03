"""Failure signatures identify one defect across incidental message differences."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_evaluation.api import failure_signature

_NAMES = st.from_regex(r"[a-z_]{1,12}", fullmatch=True)
_TYPES = st.from_regex(r"[A-Z][A-Za-z]{0,15}(Error|Exception)", fullmatch=True)
_TEXT = st.text(st.characters(categories=("L", "N", "Zs")), max_size=60)


def _traceback(directory: str, file: str, line: int, error: str, message: str) -> str:
    return (
        "Traceback (most recent call last):\n"
        f'  File "/srv/{directory}/entry.py", line 7, in <module>\n'
        "    main()\n"
        f'  File "/srv/{directory}/engine/{file}.py", line {line}, in forward\n'
        "    raise ValueError(message)\n"
        "    ^^^^^^^^^^^^^^^^^^^^^^^^^\n"
        f"{error}: {message}\n"
    )


@given(
    directories=st.tuples(_NAMES, _NAMES),
    file=_NAMES,
    line=st.integers(1, 99_999),
    error=_TYPES,
    messages=st.tuples(_TEXT, _TEXT),
)
def test_the_signature_ignores_the_message_and_the_staging_directory(
    directories: tuple[str, str],
    file: str,
    line: int,
    error: str,
    messages: tuple[str, str],
) -> None:
    first = _traceback(directories[0], file, line, error, messages[0])
    second = "status 500\n" + _traceback(directories[1], file, line, error, messages[1])

    assert failure_signature(first) == f"{error} at {file}.py:{line}"
    assert failure_signature(second) == failure_signature(first)


@given(line=st.integers(1, 99_999), other=st.integers(1, 99_999))
def test_a_different_line_or_type_is_a_different_signature(line: int, other: int) -> None:
    base = failure_signature(_traceback("a", "model", line, "ValueError", "x"))
    moved = failure_signature(_traceback("a", "model", other, "ValueError", "x"))
    retyped = failure_signature(_traceback("a", "model", line, "KeyError", "x"))

    assert (base == moved) == (line == other)
    assert base != retyped


def test_the_last_traceback_holds_the_cause() -> None:
    # A host message quotes the client's traceback before the server log tail.
    client = _traceback("checker", "resume", 88, "HTTPError", "500 Internal Server Error")
    server = _traceback("stage", "model", 442, "ValueError", "length 22 exceeds capacity 21")
    message = f"{client}\n--- server log tail ---\nINFO: loaded weights\n{server}INFO: done\n"

    assert failure_signature(message) == "ValueError at model.py:442"


@given(text=_TEXT)
def test_a_message_without_a_complete_traceback_has_no_signature(text: str) -> None:
    assert failure_signature(text) is None
    assert failure_signature(f"Traceback (most recent call last):\n{text}") is None
