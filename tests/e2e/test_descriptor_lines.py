"""``DescriptorLines`` returns each line a child wrote, however the pipe chunked them."""

from __future__ import annotations

import os
import select
from typing import TYPE_CHECKING

import pytest
from tests.support.descriptor_lines import DescriptorLines

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import IO

BOUND_S = 120.0
"""A hang guard only: every case below has its bytes in hand, or an end, before it waits."""


@pytest.fixture
def two_lines_in_one_write() -> Iterator[tuple[IO[str], int]]:
    """A pipe holding two lines that one write put there, its writer still open: no end of file.

    This is what a pipe holds when a child prints two lines close together. Yields the reading
    stream, and the writing end.
    """
    read_end, write_end = os.pipe()
    os.write(write_end, b"4242\nready\n")
    stream = os.fdopen(read_end, encoding="utf-8")
    try:
        yield stream, write_end
    finally:
        stream.close()
        os.close(write_end)


def test_a_buffered_readline_keeps_the_second_line_where_select_cannot_see_it(
    two_lines_in_one_write: tuple[IO[str], int],
) -> None:
    """Why this reader exists: a ``select`` before a ``readline`` waits on bytes already read."""
    stream, _ = two_lines_in_one_write

    assert stream.readline() == "4242\n"
    # A poll, not a wait: the descriptor is empty, and "ready" is in the stream's buffer.
    assert select.select([stream], [], [], 0)[0] == []
    assert stream.readline() == "ready\n"


def test_two_lines_that_arrive_together_are_each_reported_with_no_wait_for_more_bytes(
    two_lines_in_one_write: tuple[IO[str], int],
) -> None:
    stream, _ = two_lines_in_one_write

    # The bound is not met: both lines are in hand, so no ``select`` is made for the second.
    lines = DescriptorLines(stream.fileno(), bound=BOUND_S)

    assert lines.next_line() == "4242"
    assert lines.next_line() == "ready"


def test_a_line_split_across_writes_is_reported_whole() -> None:
    read_end, write_end = os.pipe()
    try:
        lines = DescriptorLines(read_end, bound=BOUND_S)
        os.write(write_end, b"12")
        os.write(write_end, b"34\nnext")
        assert lines.next_line() == "1234"
        os.write(write_end, b"\n")
        assert lines.next_line() == "next"
    finally:
        os.close(read_end)
        os.close(write_end)


def test_a_child_that_ends_before_it_reports_is_named_and_not_waited_for() -> None:
    read_end, write_end = os.pipe()
    os.write(write_end, b"4242\n")
    os.close(write_end)
    try:
        lines = DescriptorLines(read_end, bound=BOUND_S)
        assert lines.next_line() == "4242"
        with pytest.raises(AssertionError, match="ended before it reported"):
            lines.next_line()
    finally:
        os.close(read_end)


def test_a_second_descriptor_that_becomes_readable_ends_the_wait_unless_a_line_is_held() -> None:
    read_end, write_end = os.pipe()
    ended_read, ended_write = os.pipe()
    try:
        lines = DescriptorLines(read_end, bound=BOUND_S)
        os.write(write_end, b"last\n")
        os.close(ended_write)  # the child's own process ended: its descriptor reads end-of-file

        # A line already written is still delivered; only then does the end win.
        assert lines.next_line(unless_readable=ended_read) == "last"
        assert lines.next_line(unless_readable=ended_read) is None
    finally:
        for descriptor in (read_end, write_end, ended_read):
            os.close(descriptor)
