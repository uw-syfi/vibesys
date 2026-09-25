"""``ctx.state``: checkpoint/commit and the round/experiment events they derive.

Split from ``runtime.py`` by capability; see that module's docstring.
"""

# Capabilities in this module share one private owner for resource lifetime.
# ruff: noqa: SLF001

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING, NotRequired, Protocol, TypedDict, TypeVar, Unpack

from pydantic import BaseModel

from vibesys.events import (
    CoreEventType,
    EventStatus,
    ExperimentsChangedData,
    RoundFinishedData,
)
from vibesys.orchestration import progress_log

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from vibesys.events import CoreEventData
    from vibesys.orchestration._host import HostResources
    from vibesys.orchestration.view import RoundSummary, RunView
    from vs_project.api import StateNamespace, StateSlot

T = TypeVar("T", bound=BaseModel)


class _CheckpointOptions(TypedDict):
    publish: NotRequired[BaseModel | None]
    candidate: NotRequired[bool]
    label: NotRequired[str | None]


class _CommittedStateProjector(Protocol):
    """The one projector method `ctx.state.commit` needs.

    Structurally identical to (and satisfied by any)
    `vibesys.orchestration.contracts.OrchestrationProjector`; declared locally
    rather than imported so this module, the dependency graph's base layer,
    never depends on `contracts` (which itself depends on this module).
    """

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """Project a state just committed by the host."""
        ...


class _EventSink(Protocol):
    """The one method `_emit_commit_events` needs from `ctx.events`.

    Declared locally (rather than typed as `EventJournal` directly) so the
    derivation logic can be exercised against a minimal fake in tests,
    without constructing a full `RunContext`.
    """

    def emit(
        self,
        event_type: CoreEventType,
        text: str = "",
        *,
        data: CoreEventData | None = None,
        **fields: object,
    ) -> object:
        """Create, record, and publish one core event."""
        ...


class _TypedRunStateSlot[T: BaseModel]:
    """Read one declared typed file from the policy's portable namespace."""

    def __init__(self, host: HostResources, slot: StateSlot[T]) -> None:
        self._host = host
        self._slot = slot

    async def load(self) -> T | None:
        """Load and validate the last staged or committed model."""
        return await self._host._run_blocking(self._slot.load_optional)


class _RunState:
    """Policy-bound portable state and machine-local staging paths."""

    def __init__(self, host: HostResources) -> None:
        self._host = host
        # Cache of the last published `RunView`, used by `commit` to derive
        # round/experiment events from a before/after diff without re-reading
        # the run's durable state on every call. `_last_view_loaded` is False
        # until this process has computed it once; the first `commit` call
        # then resolves it from whatever was already durable (see
        # `_previous_view`), so a resumed run does not replay events for
        # already-committed rounds.
        self._last_view: RunView | None = None
        self._last_view_loaded = False

    @property
    def namespace(self) -> StateNamespace:
        """Return the policy's portable namespace for existing paid-work journals."""
        setup = self._host._setup
        if setup.state_namespace is None:
            raise TypeError("policy did not declare a portable state namespace")  # noqa: TRY003
        context = self._host._resources
        return context.state.portable(setup.state_namespace)

    @property
    def local_namespace(self) -> StateNamespace:
        """Return machine-local state for uncommitted paid-work cursors."""
        setup = self._host._setup
        if setup.state_namespace is None:
            raise TypeError("policy did not declare a state namespace")  # noqa: TRY003
        return self._host._resources.state.local(setup.state_namespace)

    def local_path(self, name: str) -> Path:
        """Return a validated machine-local file path owned by this run."""
        relative = PurePosixPath(name)
        parent = relative.parent
        directory = self.local_namespace.external_directory(
            None if parent == PurePosixPath(".") else parent
        )
        if relative.name in {"", ".", ".."} or relative.is_absolute():
            raise ValueError(f"invalid local state path {name!r}")  # noqa: TRY003
        return directory / relative.name

    def artifact_path(self, name: str) -> Path:
        """Return a validated portable artifact directory path."""
        return self.namespace.external_directory(name)

    def slot(self, name: str, model: type[T]) -> _TypedRunStateSlot[T]:
        """Bind one declared typed portable file."""
        setup = self._host._setup
        declared = setup.state_slots or {}
        if declared.get(name) is not model:
            raise TypeError(f"policy state slot {name!r} is not declared with {model.__name__}")  # noqa: TRY003
        return _TypedRunStateSlot(self._host, self.namespace.slot(name, model))

    async def load(self, model: type[T]) -> T | None:
        """Load the validated state after interrupted checkpoint recovery."""
        return await self.slot("state.json", model).load()

    async def checkpoint(
        self,
        *,
        sequence: int,
        writes: Mapping[str, BaseModel],
        **options: Unpack[_CheckpointOptions],
    ) -> str:
        """Journal and commit typed writes with candidate edits, then publish."""
        async with self._host._parent_mutation_lock:
            revision, _committed = await self._host._run_blocking(
                self._checkpoint,
                sequence,
                writes,
                options.get("publish"),
                candidate=options.get("candidate", True),
                label=options.get("label"),
            )
            return revision

    async def commit(
        self,
        *,
        sequence: int,
        writes: Mapping[str, BaseModel],
        **options: Unpack[_CheckpointOptions],
    ) -> str:
        """Checkpoint typed writes, publish, then emit the events that follow.

        A thin wrapper over `checkpoint` that additionally diffs the read
        model before and after this write and emits `ROUND_FINISHED` for
        every newly completed round and `EXPERIMENTS_CHANGED` when the
        experiment revision moved, so strategies stop hand-rolling that
        sequence themselves. Strategies whose read projection carries no
        rounds or revision (evolve, issue_queue) see no events derived here.

        The framework log's one write point is flushed here, after the
        checkpoint durably lands and before this call returns -- always
        before the next turn, which is the only ordering the board needs.
        Resume never depends on this file: it is a derived, regenerable
        narration of state that is already durable by the time this writes
        it. A strategy declares its progress path once with
        `ctx.progress.declare` and notes pending blocks with
        `ctx.progress.note` (see `vibesys.orchestration.progress`); this
        drains and writes that buffer.
        """
        async with self._host._parent_mutation_lock:
            before = await self._previous_view()
            revision, committed = await self._host._run_blocking(
                self._checkpoint,
                sequence,
                writes,
                options.get("publish"),
                candidate=options.get("candidate", True),
                label=options.get("label"),
            )
        after = self._project(committed)
        self._last_view = after
        self._last_view_loaded = True
        _emit_commit_events(self._host.events, before, after)
        self._flush_progress()
        return revision

    def _flush_progress(self) -> None:
        """Write the host-owned buffer's pending framework-log blocks, in order."""
        progress = self._host.progress
        path = progress.path
        if path is None:
            return
        for block in progress.drain():
            progress_log.write(path, block)

    async def _previous_view(self) -> RunView | None:
        """Return the last published view, resolving it from disk once."""
        if not self._last_view_loaded:
            self._last_view = await self._load_previous_view()
            self._last_view_loaded = True
        return self._last_view

    async def _load_previous_view(self) -> RunView | None:
        setup = self._host._setup
        declared = setup.state_slots or {}
        model = declared.get("state.json")
        if model is None:
            return None
        state = await self.slot("state.json", model).load()
        return self._project(state)

    def _project(self, state: BaseModel | None) -> RunView | None:
        projector = self._host._projector
        namespace = self._host._setup.state_namespace
        if projector is None or namespace is None or state is None:
            return None
        return projector.project_committed(namespace, state, run_id=self._host._resources.run_id)

    def _checkpoint(
        self,
        sequence: int,
        writes: Mapping[str, BaseModel],
        publish: BaseModel | None,
        *,
        candidate: bool,
        label: str | None,
    ) -> tuple[str, BaseModel | None]:
        context = self._host._resources
        namespace = self._host._setup.state_namespace
        if namespace is None:
            raise TypeError("policy did not declare a durable state slot")  # noqa: TRY003
        coordinator = context._round_transaction_coordinator
        if coordinator is None:
            raise TypeError("policy did not declare checkpoint slots")  # noqa: TRY003
        coordinator.begin(sequence, writes=writes, candidate=candidate, label=label).complete()
        committed = publish or writes.get("state.json")
        if committed is not None:
            context.publish_committed_state(namespace, committed)
        revision = context.git.current_sha()
        if revision is None:
            raise RuntimeError("checkpoint completed without a Git revision")  # noqa: TRY003
        return revision, committed


def _round_entries(view: RunView | None) -> dict[int, RoundSummary]:
    if view is None:
        return {}
    return {round_summary.number: round_summary for round_summary in view.rounds}


def _emit_commit_events(events: _EventSink, before: RunView | None, after: RunView | None) -> None:
    """Emit the round/experiment events one `commit` newly made observable.

    Diffs `RunView.rounds`/`experiment_revision`, the typed fields every
    strategy projector populates (see `vibesys.orchestration.view.RunView`);
    a strategy that leaves them empty/`None` (no round concept) naturally
    yields no diff, so this holds no knowledge of any one policy's shape.
    """
    if after is None:
        return
    before_rounds = _round_entries(before)
    after_rounds = _round_entries(after)
    new_round_numbers = sorted(number for number in after_rounds if number not in before_rounds)
    for number in new_round_numbers:
        _emit_round_finished(events, after_rounds[number])
    # A run's very first commit has no prior view to diff against (see
    # `_previous_view`): nothing has been observed yet, so nothing changed,
    # regardless of the revision value that first view happens to carry.
    if before is None:
        return
    before_revision = before.experiment_revision
    after_revision = after.experiment_revision
    if after_revision is not None and after_revision != before_revision:
        reason = "round_persisted" if new_round_numbers else "active_hypothesis_changed"
        events.emit(
            CoreEventType.EXPERIMENTS_CHANGED,
            data=ExperimentsChangedData(reason=reason, revision=after_revision),
        )


def _emit_round_finished(events: _EventSink, round_summary: RoundSummary) -> None:
    status = EventStatus.FAILED if round_summary.status == "failed" else EventStatus.COMPLETED
    events.emit(
        CoreEventType.ROUND_FINISHED,
        status=status,
        round_label=f"round-{round_summary.number}",
        data=RoundFinishedData(
            attempts=round_summary.attempts,
            judge_verdict=round_summary.judge_verdict or "skipped",
            perf_metric=round_summary.perf_metric,
            perf_unit=round_summary.perf_unit,
            profile_skipped=round_summary.profile_skipped,
        ),
    )
