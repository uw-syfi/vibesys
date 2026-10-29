"""Carry a Slurm batch's stage output, as the job writes it, into one stream per stage.

A batch's stages run on the cluster, each writing its stdout and stderr to files there
(``Cluster.stage_output``). :class:`StageOutputFollower` copies what they hold into the
stream a :data:`SlurmStageOutputOpener` opens for each stage:

- While the job runs, the executor calls :meth:`StageOutputFollower.follow` once per
  scheduler poll with the stage now running. It opens that stage's stream (and those of
  stages that ran between polls), and reads at most :data:`READS_PER_POLL` chunks per
  file, so a poll costs a bounded number of remote reads.
- When the job has been collected, :meth:`StageOutputFollower.settle` writes the rest of
  each stage's output from the collected files (no further remote reads), and closes each
  stream: ended when its stage ran to an exit status, failed otherwise.
- :meth:`StageOutputFollower.abort` closes every open stream failed, for a job that was
  cancelled or whose wait failed.

Every byte reaches a stream once and in order. A file that shrank or was replaced is
reported (logged, and its stream ends failed) and read no further, so no byte is repeated.
Reading is advisory: a lost remote read is retried at the next poll and never fails the
evaluation. Writing needs no guard: an ``OutputSink`` never raises (a log stream that
cannot write degrades instead), and neither does exiting an opened stream.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vs_sim.api import ByteSourceChangedError, OutputStream
from vs_slurm.api import SlurmError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from contextlib import AbstractContextManager

    from vs_sim.api import ByteSource, OutputSink

type SlurmStageOutputOpener = Callable[[str, str], AbstractContextManager[OutputSink]]
"""Opens the stream of one batch stage, named by the evaluation's handle and the stage's name.

Called on the executor's event loop, once per stage, when the stage is first seen running
or, for a stage that ran between two polls, when it is first seen to have run. The entered
sink is written from a worker thread; the context is exited when the batch has settled,
been cancelled, or failed, with an exception when the stage did not run to an exit status.
"""

READS_PER_POLL = 4
"""The most remote reads of one output file per scheduler poll."""
READ_BYTES = 256 * 1024
"""The most bytes one remote read asks for."""

_LOG = logging.getLogger(__name__)
_PIPES = (OutputStream.STDOUT, OutputStream.STDERR)


class StageOutputCutError(Exception):
    """The stage did not run to an exit status, or its output changed under the reader."""


@dataclass(frozen=True, slots=True)
class CollectedStage:
    """What collection found of one stage: its whole output and whether it ran to an exit."""

    stdout: bytes
    stderr: bytes
    ran: bool


@dataclass
class _File:
    source: ByteSource
    offset: int = 0
    changed: bool = False


@dataclass
class _Stage:
    sink: OutputSink
    stream: AbstractContextManager[OutputSink]
    """The opened stream, exited once when the stage's stream ends."""
    files: dict[OutputStream, _File] = field(default_factory=dict)
    drained: bool = False


class StageOutputFollower:
    """Follows one batch's stage output files into one opened stream per stage."""

    def __init__(
        self,
        opener: SlurmStageOutputOpener | None,
        handle_id: str,
        stage_names: Sequence[str],
        source: Callable[[int, OutputStream], ByteSource],
    ) -> None:
        """Follow the stages named ``stage_names``, in order, reading each through ``source``.

        Without an ``opener`` nobody asked for the output: nothing is opened or read.
        """
        self._opener = opener
        self._handle_id = handle_id
        self._names = tuple(stage_names)
        self._source = source
        self._stages: dict[int, _Stage] = {}

    def open_through(self, index: int) -> None:
        """Open the stream of every stage up to ``index`` that has none; on the event loop."""
        for earlier in range(min(index, len(self._names) - 1) + 1):
            self._open(earlier)

    def follow(self, running: int) -> None:
        """Copy new output of the stages opened so far; blocking, on a worker thread.

        Each file is read at most :data:`READS_PER_POLL` chunks. A stage before ``running``
        has ended, so once its files are read to their end it is not read again until
        the batch settles.
        """
        for index, stage in sorted(self._stages.items()):
            if index <= running and not stage.drained:
                reached = [_copy(stage.sink, pipe, file) for pipe, file in stage.files.items()]
                stage.drained = index < running and all(reached)

    def settle(self, collected: Mapping[int, CollectedStage]) -> None:
        """Write each stage's remainder from its collected files, then close every stream."""
        for index, result in sorted(collected.items()):
            if index not in self._stages and (result.ran or result.stdout or result.stderr):
                self._open(index)
        for index, stage in sorted(self._stages.items()):
            result = collected.get(index)
            ended = result is not None and self._write_rest(index, stage, result) and result.ran
            self._close(stage, ended=ended)
        self._stages.clear()

    def abort(self) -> None:
        """Close every open stream failed: the batch did not settle."""
        for _, stage in sorted(self._stages.items()):
            self._close(stage, ended=False)
        self._stages.clear()

    def _open(self, index: int) -> None:
        if self._opener is None or index in self._stages:
            return
        stream = self._opener(self._handle_id, self._names[index])
        sink = stream.__enter__()
        self._stages[index] = _Stage(
            sink, stream, {pipe: _File(self._source(index, pipe)) for pipe in _PIPES}
        )

    def _write_rest(self, index: int, stage: _Stage, result: CollectedStage) -> bool:
        """Write what the collected files hold past what was streamed; whether it all fit."""
        whole = True
        for pipe, data in (
            (OutputStream.STDOUT, result.stdout),
            (OutputStream.STDERR, result.stderr),
        ):
            file = stage.files[pipe]
            if file.changed or len(data) < file.offset:
                _LOG.warning(
                    "stage %s %s output changed while it was read; its stream ends failed",
                    self._names[index],
                    pipe.value,
                )
                whole = False
                continue
            stage.sink.write(pipe, data[file.offset :])
            file.offset = len(data)
        return whole

    @staticmethod
    def _close(stage: _Stage, *, ended: bool) -> None:
        if ended:
            stage.stream.__exit__(None, None, None)
            return
        cut = StageOutputCutError()
        stage.stream.__exit__(type(cut), cut, None)


def _copy(sink: OutputSink, pipe: OutputStream, file: _File) -> bool:
    """Copy at most a poll's reads of ``file`` past its offset; whether it reached its end.

    A file that changed is never read again, and counts as at its end.
    """
    for _ in range(READS_PER_POLL):
        if file.changed:
            return True
        try:
            data = file.source.read(file.offset, READ_BYTES)
        except FileNotFoundError:
            return True  # the stage has not created it (yet)
        except ByteSourceChangedError as error:
            _LOG.warning("stage output %s; it is not read further", error)
            file.changed = True
            return True
        except (SlurmError, OSError) as error:
            _LOG.debug("stage output read failed; read again at the next poll: %s", error)
            return False
        if not data:
            return True
        sink.write(pipe, data)
        file.offset += len(data)
    return file.changed


__all__ = [
    "READS_PER_POLL",
    "READ_BYTES",
    "CollectedStage",
    "SlurmStageOutputOpener",
    "StageOutputCutError",
    "StageOutputFollower",
]
