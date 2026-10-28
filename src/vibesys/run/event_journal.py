"""Core-event binding for the runtime-owned durable journal."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import vs_proto.api as p
from vibesys.events import (
    CoreEvent,
    CoreEventData,
    CoreEventType,
    EventStatus,
    IntentStateData,
    RunFailedData,
    RunFailure,
    RunFailureKind,
    make_core_event,
)
from vibesys.run.runlog_tee import RunLogTee
from vs_runlog.api import FileRunContent, FileRunLogWriter
from vs_runtime.api.infrastructure import (
    DurableEventJournal,
    EventCodec,
    SteerRequest,
    WithdrawRequest,
)
from vs_sim.api import SleepingClock, Threads, utc_datetime

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.workspace_changes import AttemptChanges
    from vs_project.api import Project
    from vs_runtime.api.infrastructure import ControlRequest


class _CoreEventCodec(EventCodec[CoreEvent]):
    """Bind VibeSys's semantic event envelope to generic JSONL mechanics."""

    def decode(self, data: bytes) -> CoreEvent:
        """Decode one persisted core event."""
        return CoreEvent.model_validate_json(data)

    def encode(self, event: CoreEvent) -> bytes:
        """Encode one core event without a trailing newline."""
        return event.model_dump_json().encode()

    def sequence(self, event: CoreEvent) -> int:
        """Return the durable cursor carried by *event*."""
        return event.sequence

    def stamp(self, event: CoreEvent, *, sequence: int, stream_id: str) -> CoreEvent:
        """Assign the journal cursor and run identity."""
        return event.model_copy(update={"sequence": sequence, "run_id": stream_id})


class EventJournal(DurableEventJournal[CoreEvent]):
    """Persist VibeSys core events using runtime-owned journal mechanics."""

    def __init__(self, clock: SleepingClock, threads: Threads) -> None:
        """Create an unattached journal for ``core-events.jsonl`` stamped by epoch ``clock``."""
        super().__init__(_CoreEventCodec(), filename="core-events.jsonl", threads=threads)
        self._threads = threads
        self._clock = clock
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._runlog_configured = False
        self._runlog: RunLogTee | None = None
        self.checkout: tuple[str, bool] = ("", False)

    def enable_runlog(self, project: Project, run_id: str) -> None:
        """Enable the additive transport and publish discovery once for this process."""
        if self._runlog_configured:
            return
        self._runlog_configured = True
        try:
            path = project.run_log_path_for(project.root, run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._runlog = RunLogTee(
                FileRunLogWriter(path, clock=self._clock, threads=self._threads),
                FileRunContent(
                    project.run_blobs_directory_for(project.root, run_id),
                    project.log_streams_directory_for(project.root, run_id),
                ),
                run_id,
                history=self.read,
                checkout=self.checkout,
            )
            self._runlog.remember_project_root(str(project.root))
            project.publish_run_for(project.root, run_id)
            self.set_append_sink(self._runlog.append)
        except BlockingIOError as error:
            self.close()
            message = f"cannot relaunch run {run_id!r}: run.lock is held by a live writer"
            raise RuntimeError(message) from error
        except Exception:
            self.close()
            logging.getLogger(__name__).exception("Run log tee disabled during initialization")

    def follow_attempt_changes(self, read: Callable[[str, str], AttemptChanges | None]) -> None:
        """Keep what each attempt changed on its span as it settles; nothing without a run log."""
        if self._runlog is not None:
            self._runlog.follow_changes(read)

    def attach(self, directory: Path, stream_id: str) -> None:
        """Attach the JSONL journal, then publish this writer's epoch if it is live.

        The epoch record is the first fact written after the writer lock, before
        the caller opens the run environment. A buffered event flushed by
        attachment may already have opened it.
        """
        super().attach(directory, stream_id)
        if self._runlog is not None:
            self._runlog.publish_successor(utc_datetime(self._clock))

    def fail_open_epoch(self, reason: str) -> bool:
        """End this process's open epoch the way a running epoch fails.

        Return whether a typed failure was recorded for a published epoch.
        The session calls this when startup failed before resources were ready.
        A crash that never reaches this call stays an interrupted epoch for the next writer.
        """
        if self._runlog is None or not self._runlog.has_open_epoch():
            return False
        if not reason:
            reason = "environment startup failed"
        self.emit(
            CoreEventType.RUN_FAILED,
            reason,
            status=EventStatus.FAILED,
            data=RunFailedData(
                failure=RunFailure(
                    kind=RunFailureKind.RUN_ENVIRONMENT_UNAVAILABLE,
                    reason=reason,
                    workstreams_started=0,
                    workstream_budget=0,
                    candidates_kept=0,
                )
            ),
        )
        return True

    def close(self) -> None:
        """Release the additive transport's append handle on run teardown."""
        if self._runlog is not None:
            try:
                self._runlog.close()
            except Exception:
                logging.getLogger(__name__).exception("Run log tee failed during final commit")
            finally:
                self._runlog = None

    @property
    def epoch(self) -> int:
        """The published process epoch, required before native recovery or controls.

        Startup may publish before acquiring a core lease, so writer and lease epochs
        are independent. Native closing requests always name this writer's epoch.
        """
        if self._runlog is None or self._runlog.epoch == 0:
            message = "run.log has no published epoch; recovery and controls cannot proceed"
            raise RuntimeError(message)
        return self._runlog.epoch

    def intent_decision(self, control_id: str) -> tuple[bool, str | None] | None:
        """Recover a typed decision whose inbox marker may not have reached disk."""
        intent = None if self._runlog is None else self._runlog.intent(control_id)
        if intent is None:
            return None
        return intent.step is p.IntentStep.APPLIED, intent.reason or None

    def intent_outcome(self, request: ControlRequest, reason: str | None) -> None:
        """Journal and commit the decision before the inbox marks the request answered."""
        intent = p.IntentState(
            control_id=request.control_id,
            action=p.IntentAction[request.action.name],
            step=p.IntentStep.APPLIED if reason is None else p.IntentStep.REFUSED,
            reason=reason,
        )
        if isinstance(request, SteerRequest):
            intent.mode = p.SteerMode[request.mode.name]
            intent.target = p.SteerTarget(
                kind=p.SteerTargetKind[request.target.kind.name], id=request.target.id
            )
        elif isinstance(request, WithdrawRequest):
            intent.withdraws = request.withdraws
        p.validate(intent)
        self.emit(CoreEventType.INTENT_STATE, data=IntentStateData(state_json=intent.to_json()))
        if self._runlog is not None:
            self._runlog.commit()

    def emit(
        self,
        event_type: CoreEventType,
        text: str = "",
        *,
        data: CoreEventData | None = None,
        **fields: object,
    ) -> CoreEvent:
        """Create, record, and publish one semantic core event."""
        return self.record(
            make_core_event(
                event_type, text, timestamp=utc_datetime(self._clock), data=data, **fields
            )
        )

    def has_recorded(self, event_type: CoreEventType) -> bool:
        """Whether an event of ``event_type`` was recorded, this process's or an earlier one's."""
        return any(event.type is event_type for event in self.history())
