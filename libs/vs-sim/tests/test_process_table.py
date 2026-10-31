"""What the macOS table reads from ``ps``, and what each host's Fake table shows."""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import (
    MarkedProcess,
    NoProcessTable,
    ProcessTableUnavailableError,
    ProcfsProcessTable,
    mark_in,
    parse_ps,
)

if TYPE_CHECKING:
    from pathlib import Path

# Rows as macOS ``ps -axww -o pid=,pgid=,stat=,lstart=,command=`` prints them: a leader whose
# arguments carry the mark, its inner shell, a child, a zombie, and a day-of-month under ten.
_LISTING = """\
53193 53193 Ss   Fri Oct 30 17:46:59 2026     /bin/sh -c "$1" -c "$2"; exit "$?" VIBESYS_STEP_TAG=run-a:vs-abc /bin/sh sleep 30 & wait
53194 53193 S    Fri Oct 30 17:46:59 2026     /bin/sh -c sleep 30 & wait
53195 53193 S    Fri Oct 30 17:46:59 2026     sleep 30
  400   400 Z    Mon Oct  5 09:01:02 2026     (sh)
not a process row
"""


def test_a_ps_listing_is_read_row_by_row_and_a_stray_line_is_skipped() -> None:
    rows = parse_ps(_LISTING)
    assert [(row.pid, row.pgid, row.stat) for row in rows] == [
        (53193, 53193, "Ss"),
        (53194, 53193, "S"),
        (53195, 53193, "S"),
        (400, 400, "Z"),
    ]
    assert rows[0].started == "Fri Oct 30 17:46:59 2026"
    assert rows[0].command.endswith("sleep 30 & wait")
    assert rows[3].started == "Mon Oct  5 09:01:02 2026"


@given(
    name=st.from_regex(r"[A-Z_]{1,12}", fullmatch=True),
    value=st.from_regex(r"[^\s]{1,20}", fullmatch=True),
    before=st.from_regex(r"[^\s]{0,8}", fullmatch=True),
)
def test_a_mark_is_read_only_as_a_whole_argument(name: str, value: str, before: str) -> None:
    assert mark_in(f"/bin/sh -c x {name}={value} y", name) == value
    assert mark_in(f"/bin/sh {name}={value}", name) == value
    if before:
        assert mark_in(f"/bin/sh {before}{name}={value}", name) is None


@given(name=st.from_regex(r"[A-Z_]{1,12}", fullmatch=True))
def test_a_host_with_no_table_neither_lists_nor_kills_and_so_is_never_read_as_empty(
    name: str,
) -> None:
    table = NoProcessTable()

    with pytest.raises(ProcessTableUnavailableError, match=name):
        table.marked(name, lambda _value: True)
    with pytest.raises(ProcessTableUnavailableError, match=name):
        table.kill(MarkedProcess(pid=1, started="", value="x"), name)


_STAT = (
    "{pid} (sh) S 1 {pgid} 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {start} 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
)


def _process(root: Path, pid: int, environment: dict[str, str] | None, *, pgid: int = 0) -> Path:
    """A ``/proc/<pid>`` the table can read: its environment (NUL-separated) and its stat."""
    directory = root / str(pid)
    directory.mkdir()
    environ = "".join(f"{k}={v}\0" for k, v in (environment or {}).items())
    (directory / "environ").write_bytes(environ.encode())
    (directory / "stat").write_text(_STAT.format(pid=pid, pgid=pgid or pid, start=1000 + pid))
    return directory


def test_a_proc_tree_is_listed_by_the_processes_that_set_the_mark(tmp_path: Path) -> None:
    _process(tmp_path, 11, {"VIBESYS_STEP_TAG": "run-a:vs-x", "PATH": "/bin"})
    _process(tmp_path, 12, {"VIBESYS_STEP_TAG": "run-b:vs-x"})
    _process(tmp_path, 13, {"PATH": "/bin"})
    (tmp_path / "self").mkdir()

    found = ProcfsProcessTable(tmp_path).marked(
        "VIBESYS_STEP_TAG", lambda value: value.startswith("run-a:")
    )

    assert [(process.pid, process.value) for process in found] == [(11, "run-a:vs-x")]


def test_a_process_that_is_gone_or_not_readable_by_this_user_is_not_this_hosts_work(
    tmp_path: Path,
) -> None:
    """ENOENT (it exited between the listing and the read) and EACCES (another user's) skip it."""
    _process(tmp_path, 21, {"VIBESYS_STEP_TAG": "m"})
    (tmp_path / "22").mkdir()  # listed, then gone: no environ, no stat
    hidden = _process(tmp_path, 23, {"VIBESYS_STEP_TAG": "m"})
    (hidden / "environ").chmod(0)
    unreadable = os.access(hidden / "environ", os.R_OK)  # root reads it regardless of its mode

    found = ProcfsProcessTable(tmp_path).marked("VIBESYS_STEP_TAG", lambda _value: True)

    assert [process.pid for process in found] == ([21, 23] if unreadable else [21])
    (hidden / "environ").chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_a_process_that_cannot_be_read_for_any_other_reason_is_never_read_as_gone(
    tmp_path: Path,
) -> None:
    """Out of descriptors, out of memory or an I/O error says nothing about the process.

    A directory in place of ``environ`` fails the read with ``EISDIR``, which is none of the
    reasons a process is gone or another user's.
    """
    _process(tmp_path, 31, {"VIBESYS_STEP_TAG": "m"})
    broken = _process(tmp_path, 32, None)
    (broken / "environ").unlink()
    (broken / "environ").mkdir()

    with pytest.raises(ProcessTableUnavailableError, match="process 32"):
        ProcfsProcessTable(tmp_path).marked("VIBESYS_STEP_TAG", lambda _value: True)


def test_a_proc_tree_that_cannot_be_listed_is_unavailable_and_not_empty(tmp_path: Path) -> None:
    with pytest.raises(ProcessTableUnavailableError, match="cannot be listed"):
        ProcfsProcessTable(tmp_path / "missing").marked("VIBESYS_STEP_TAG", lambda _value: True)
