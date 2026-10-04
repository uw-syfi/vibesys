"""Private typed state durability bound to one orchestration plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

from vs_project.api import Project, validate_state_namespace
from vs_runtime._workspaces import RuntimeWorkspaces, run_sync
from vs_runtime.contracts import RuntimeContractError, StateModelError

if TYPE_CHECKING:
    from vs_project.api import StateModels
    from vs_runtime._checkpoint import MultiSlotRoundTransactionCoordinator
    from vs_runtime.contracts import State, Workspace, Workspaces


class CommittedStateObserver(Protocol):
    """Composition projection invoked only after a durable state commit."""

    def committed(self, previous: BaseModel | None, current: BaseModel) -> None:
        """Observe one durable transition without participating in persistence."""
        ...


class RuntimeState:
    """Exact-model state backed by the existing recoverable v5 checkpoint."""

    def __init__(
        self,
        model: type[BaseModel] | None,
        coordinator: MultiSlotRoundTransactionCoordinator | None,
        workspaces: RuntimeWorkspaces,
        observer: CommittedStateObserver | None,
    ) -> None:
        self._model = model
        self._coordinator = coordinator
        self._workspaces = workspaces
        self._observer = observer
        self._next_sequence = 1

    def namespace(self, name: str) -> StateModels:
        """Open a host subsystem through the owning Project's canonical layout."""
        validate_state_namespace(name)
        coordinator = self._require_coordinator()
        return Project.open(self._workspaces.root.path).state.local_namespace(
            coordinator.run_id, name
        )

    async def load[StateT: BaseModel](self, model: type[StateT]) -> StateT | None:
        self._require_model(model)
        coordinator = self._require_coordinator()
        return await run_sync(coordinator.namespace.slot("state.json", model).load_optional)

    async def commit(
        self,
        value: BaseModel,
        *,
        workspace: Workspace | None = None,
        label: str | None = None,
    ) -> None:
        model = self._model
        if model is None or type(value) is not model:
            raise StateModelError(model, type(value))
        coordinator = self._require_coordinator()
        if workspace is not None:
            try:
                is_root = self._workspaces.is_root(workspace)
            except (TypeError, ValueError):
                is_root = False
            if not is_root:
                message = "state can commit only the live root workspace for this run"
                raise RuntimeContractError(message)
        snapshot = model.model_validate_json(value.model_dump_json(round_trip=True))
        async with self._workspaces._mutation(self._workspaces.root):  # noqa: SLF001  # lint-waiver: LW-228419 [SLF001]; state and root Git mutations share the runtime workspace owner's serialization.
            previous = await run_sync(coordinator.namespace.slot("state.json", model).load_optional)
            sequence = self._next_sequence
            self._next_sequence += 1
            transaction = coordinator.begin(
                sequence,
                writes={"state.json": snapshot},
                candidate=workspace is not None,
                label=label,
            )
            await run_sync(transaction.complete)
        if self._observer is not None:
            self._observer.committed(previous, snapshot)

    def _require_model(self, requested: type[BaseModel]) -> None:
        if requested is not self._model:
            raise StateModelError(self._model, requested)

    def _require_coordinator(self) -> MultiSlotRoundTransactionCoordinator:
        if self._coordinator is None:
            message = "orchestration plugin did not declare durable state"
            raise TypeError(message)
        return self._coordinator


def create_state(
    model: type[BaseModel] | None,
    coordinator: MultiSlotRoundTransactionCoordinator | None,
    workspaces: Workspaces,
    observer: CommittedStateObserver | None = None,
) -> State:
    """Bind opaque state durability to a prepared runtime workspace owner."""
    if not isinstance(workspaces, RuntimeWorkspaces):
        message = "state requires runtime-owned workspaces"
        raise TypeError(message)
    if (model is None) is not (coordinator is None):
        message = "state model and checkpoint coordinator must be declared together"
        raise TypeError(message)
    return RuntimeState(model, coordinator, workspaces, observer)
