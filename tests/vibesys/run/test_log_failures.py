"""A run's log store that cannot write never stalls or fails the work it logs.

The store is the Fake over a :class:`~vs_logs.api.testing.MemoryVolume` whose capacity
runs out (a full disk: the last append is short and raises ``ENOSPC``). A local command
whose output outgrows a pipe buffer still drains and returns its whole bounded result,
and so does one whose stream a cancel closed while it still wrote. The stream records
what fit, is marked degraded, and the failure is said once through ``warn``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.events import GateKind
from vibesys.log_owners import LOG_OWNER, GateStage
from vs_logs.api import MemoryLogStore, StreamOutcome
from vs_logs.api.testing import MemoryVolume
from vs_logs.read import reader_of
from vs_sandbox.api import LocalShellRunner
from vs_sim.api.testing import FakePipedProcessLauncher, ScriptedPipedProcess, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.log_owners import LogOwner
    from vs_sandbox.api import CommandResult

_PIPE_BUFFER = 64 * 1024
_OWNER = GateStage(execution_id="vs-measurement", gate=GateKind.BENCHMARK, attempt=1)
# Three times a pipe buffer, in lines, so a drain that stopped would leave the command blocked.
_STDOUT = "".join(f"line {number:06d}\n" for number in range(3 * _PIPE_BUFFER // 12))
_STDERR = "warning: slow\n" * 10


def _runner(tmp_path: Path, threads: SimThreads, *, exits: bool) -> LocalShellRunner:
    """A runner whose command writes ``_STDOUT`` in chunks, then exits or waits to be stopped."""

    def start(_request: object) -> ScriptedPipedProcess:
        scripted = ScriptedPipedProcess(threads)
        for start_at in range(0, len(_STDOUT), _PIPE_BUFFER // 2):
            scripted.write(_STDOUT[start_at : start_at + _PIPE_BUFFER // 2], _STDERR[:14])
        if exits:
            scripted.exit(0)
        return scripted

    return LocalShellRunner(
        tmp_path,
        threads=threads,
        launcher=FakePipedProcessLauncher(factory=start),
        max_output_chars=len(_STDOUT) + len(_STDERR),
    )


def _full_store(
    threads: SimThreads, warnings: list[str]
) -> tuple[MemoryLogStore[LogOwner], MemoryVolume]:
    volume = MemoryVolume()
    store = MemoryLogStore(LOG_OWNER, threads=threads, warn=warnings.append, volume=volume)
    return store, volume


def test_a_local_command_drains_its_whole_output_past_a_full_log_disk(tmp_path: Path) -> None:
    threads = SimThreads()
    warnings: list[str] = []
    store, volume = _full_store(threads, warnings)

    def case() -> CommandResult:
        with store.open(_OWNER, "benchmark") as sink:
            volume.capacity = volume.used + 1000
            result = _runner(tmp_path, threads, exits=True).execute(
                "make bench", timeout=600, output=sink
            )
            volume.capacity = None  # space freed: the close can say the stream lost bytes
            return result

    result = threads.run(case)

    assert (result.exit_code, result.stdout) == (0, _STDOUT)
    (info,) = reader_of(store, LOG_OWNER).list_streams()
    assert (info.outcome, info.degraded) == (StreamOutcome.ENDED, True)
    assert 0 < info.size < len(_STDOUT)
    assert len(warnings) == 1


def test_a_command_whose_stream_a_cancel_closed_still_drains_and_stops(tmp_path: Path) -> None:
    threads = SimThreads()
    warnings: list[str] = []
    store, _ = _full_store(threads, warnings)

    def case() -> CommandResult:
        cancel = threads.event()
        with store.open(_OWNER, "benchmark") as sink:
            pass  # the cancel closed the stream before the command's output drained
        cancel.set()
        return _runner(tmp_path, threads, exits=False).execute(
            "make bench", timeout=600, cancel=cancel, output=sink
        )

    result = threads.run(case)

    assert result.cancelled
    assert result.stdout.startswith(_STDOUT)
    (info,) = reader_of(store, LOG_OWNER).list_streams()
    assert (info.size, info.outcome, info.degraded) == (0, StreamOutcome.ENDED, False)
    assert warnings == []
