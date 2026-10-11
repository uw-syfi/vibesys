"""Relay this process's stdin and stdout to a run's Unix control socket.

``python -m entrypoints.stdio_bridge --socket PATH`` carries one protocol connection, so
the desktop runs one per connection over its own ssh exec channel. The exit status and
the stderr line are a contract owned by ``docs/contributing/wire-protocol.md``
(section "The stdio bridge"): 0 when the client closed, a distinct status per other outcome,
and one JSON line on stderr whenever the status is not 0.

Startup is on every connection's critical path, so this module imports only the
relay core; the Pydantic report model is imported only on the unsuccessful path.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import TYPE_CHECKING

from server.stdio_bridge import (
    BridgeOutcome,
    BridgeResult,
    ClientStreams,
    FdSink,
    FdSource,
    run_bridge,
)
from vs_sim.api import OsThreads, UnixNetwork

if TYPE_CHECKING:
    from collections.abc import Sequence

    from server.stdio_bridge import ByteSink, Dialer
    from vs_sim.api import Threads

_STDIN_FD = 0
_STDOUT_FD = 1
_STDERR_FD = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m entrypoints.stdio_bridge",
        description="Relay stdin and stdout to a VibeSys run's Unix control socket.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--socket", help="path of the run's Unix control socket")
    return parser


def _report(result: BridgeResult, errors: ByteSink) -> None:
    # lint-waiver: LW-178501 [PLC0415]; import the Pydantic report only when there is one to write.
    # > A module-level import costs every connection about 95ms of Pydantic startup
    # > (measured), more than the rest of the relay's imports; hand-written JSON would
    # > be a second, unvalidated definition of the contract the model owns.
    from server.stdio_bridge_report import BridgeReport  # noqa: PLC0415

    errors.write(BridgeReport.of(result).line().encode())


def bridge(
    argv: Sequence[str],
    *,
    network: Dialer,
    client: ClientStreams,
    errors: ByteSink,
    threads: Threads,
) -> int:
    """Parse ``argv``, relay until one side ends, report an unsuccessful end; the exit status."""
    arguments = _parser().parse_args(argv)
    result = run_bridge(network=network, address=arguments.socket, client=client, threads=threads)
    if result.outcome is not BridgeOutcome.CLIENT_CLOSED:
        _report(result, errors)
    return result.exit_status


def main(argv: list[str] | None = None) -> None:
    """Relay this process's stdin and stdout, then exit with the outcome's status."""
    status = bridge(
        sys.argv[1:] if argv is None else argv,
        network=UnixNetwork(),
        client=ClientStreams(FdSource(_STDIN_FD), FdSink(_STDOUT_FD)),
        errors=FdSink(_STDERR_FD),
        threads=OsThreads(),
    )
    # A copy loop may still be blocked reading stdin or writing a stalled stdout; neither
    # can be interrupted portably, and the outcome is already decided, so leave at once.
    os._exit(status)


if __name__ == "__main__":
    main()
