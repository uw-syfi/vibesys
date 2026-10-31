"""What the macOS table reads from ``ps``, and what each host's Fake table shows."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import mark_in, parse_ps

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
