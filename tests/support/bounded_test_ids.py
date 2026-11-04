"""End the session on a test id too long to read in a log.

A parametrized value that is a long string or byte string becomes part of the test id. A
160,000 character id (a ``"é" * 40000`` message) cut the CI log of a shard at that test, and the
shard looked hung. Every report and every message between xdist processes carries the whole id.
Give such a value a short id with ``pytest.param(value, id="...")``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable

#: The most characters a test id may have, its file and test name included.
MAX_NODEID_LENGTH = 500
#: Overlong ids named in the error; the count says how many more there are.
_NAMED = 8
_SHOWN = 100


def overlong_ids(nodeids: Iterable[str], limit: int = MAX_NODEID_LENGTH) -> list[str]:
    """Each id over ``limit`` characters, shortened to its start and its length."""
    return [
        f"{nodeid[:_SHOWN]}... ({len(nodeid)} characters)"
        for nodeid in nodeids
        if len(nodeid) > limit
    ]


def explain_overlong_ids(overlong: list[str], limit: int = MAX_NODEID_LENGTH) -> str:
    """The error for ``overlong`` ids: what is wrong, the first few, and the fix."""
    named = "\n".join(f"  {line}" for line in overlong[:_NAMED])
    more = f"\n  and {len(overlong) - _NAMED} more" if len(overlong) > _NAMED else ""
    return (
        f"{len(overlong)} test ids are over {limit} characters, which a CI log and every xdist "
        f"message carry whole:\n{named}{more}\n"
        'Give the long parameter a short id: pytest.param(value, id="short-name"), or pass '
        "ids=[...] to parametrize."
    )


def stop_on_overlong_ids(nodeids: Iterable[str]) -> None:
    """End the session with a usage error when any id is over the limit."""
    overlong = overlong_ids(nodeids)
    if overlong:
        pytest.exit(explain_overlong_ids(overlong), returncode=pytest.ExitCode.USAGE_ERROR)


def pytest_collection_modifyitems(config: pytest.Config, items: Iterable[pytest.Item]) -> None:
    """Pytest hook: check the ids of a session that collects in this process.

    An xdist worker leaves the check to the controller (below): an exit raised in a worker
    surfaces as an internal error that hides the message.
    """
    if not hasattr(config, "workerinput"):
        stop_on_overlong_ids(item.nodeid for item in items)


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(ids: Iterable[str]) -> None:
    """Pytest-xdist hook: check the ids one worker collected, in the controller."""
    stop_on_overlong_ids(ids)
