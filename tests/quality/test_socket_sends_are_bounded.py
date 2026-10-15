"""Product code bounds every send on a connection: a peer that stops reading cannot hold it.

``Connection.send(data)`` with no timeout waits for ever once the peer's socket buffer is
full. A server or broker that writes to a client that stopped reading then holds that
client's worker, and whatever waits for the worker (an attached server's exit, a Slurm
job's output, a drain), for ever. Pass a timeout and treat ``TimeoutError`` as the peer
gone (``unix_jsonl._SEND_BOUND_SECONDS``, ``host_command_broker._SEND_BOUND_SECONDS``).

The check is syntactic: a ``.send(...)`` with one argument on a name that reads as a
connection (``connection``, ``self._connection``, ``conn``, ``sock``, ``client``), and every
``.sendall(...)`` on such a name. ``ALLOWED`` holds
the sites whose bound lives elsewhere, with counts that only shrink.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source, walk_nodes

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (REPO / "src", *sorted((REPO / "libs").glob("*/src")))
CONNECTION = re.compile(r"(^|_)(connection|conn|sock|socket|client)$")
SENDS = frozenset({"send", "sendall"})

ALLOWED: dict[str, tuple[int, str]] = {
    "src/server/relay.py": (
        1,
        "the relay's supervisor ends a write that makes no progress for "
        "BridgeLimits.write_deadline_seconds and closes both sides",
    ),
    "libs/vs-runtime/src/vs_runtime/_agent_evaluation.py": (
        2,
        "one reply line, far under a socket buffer, so the send never waits for room",
    ),
    "libs/vs-sim/src/vs_sim/concurrency_contracts.py": (
        6,
        "contract cases of the network itself, whose peers always read",
    ),
    "libs/vs-sandbox/src/vs_sandbox/host_command_client.py": (
        1,
        "the in-container client's own socket sends one request line, far under a socket "
        "buffer, to the host broker, which always reads it",
    ),
    "libs/vs-sim/src/vs_sim/network.py": (
        1,
        "the real Connection.send itself, on its no-timeout path; callers choose the bound",
    ),
}


def _receiver(call: ast.Call) -> str:
    function = call.func
    if not isinstance(function, ast.Attribute) or function.attr not in SENDS:
        return ""
    value = function.value
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return ""


def unbounded_sends(source: str) -> list[int]:
    """Line numbers of a one-argument ``.send`` on a connection-like name."""
    if ".send(" not in source:
        return []
    return sorted(
        node.lineno
        for node in walk_nodes(parse_source(source))
        if isinstance(node, ast.Call)
        and CONNECTION.search(_receiver(node))
        and len(node.args) == 1
        and not node.keywords
    )


def test_the_checker_flags_an_unbounded_send_and_not_a_bounded_one() -> None:
    source = (
        "def reply(connection, sock, coroutine):\n"
        "    connection.send(b'x')\n"
        "    self._connection.send(b'x')\n"
        "    connection.send(b'x', 5.0)\n"
        "    sock.send(b'x', timeout=5.0)\n"
        "    coroutine.send(None)\n"
        "    client.send(b'x')\n"
        "    self._sock.sendall(b'x')\n"
        "    client.send(b'x', 5.0)\n"
    )
    assert unbounded_sends(source) == [2, 3, 7, 8]


def test_every_send_on_a_connection_has_a_bound() -> None:
    found: dict[str, int] = {}
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            count = len(unbounded_sends(path.read_text(encoding="utf-8")))
            if count:
                found[path.relative_to(REPO).as_posix()] = count
    allowed = {path: count for path, (count, _reason) in ALLOWED.items()}
    assert found == allowed, (
        "a send with no timeout waits for ever on a peer that stopped reading; pass a "
        "timeout and treat TimeoutError as the peer gone, and lower the ALLOWED count of "
        "any site you bounded"
    )
