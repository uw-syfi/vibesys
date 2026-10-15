"""``@search``: how a search is written down, and the driver core runs it with.

A search is one root flow with a name and a config model::

    @search("sequential", config=SequentialConfig)
    async def sequential(ctx: Flow, config: SequentialConfig) -> NoWinner | None: ...

Everything else core needs is derived: the operations the search's namespace owns, the
lifecycle capabilities it may use, the schema of its persisted state, and from the declared
``roles`` the reply schemas, the role ids and the exchanges core starts with each agent. The
decorator returns a ``SearchDef``; the host gives it a config and the operation registry and
gets a ``FlowDriver``.

The driver is core's private mechanism for running flows: it delivers one event at a time and
returns the decisions the flows issued. Persistence is replay. ``FlowState`` holds the run's
facts and a journal with one ``Step`` per event: the event, every read made while handling it,
and a digest of the decisions it produced. To resume a saved state the driver runs the flow
code again from ``init`` and feeds it the journal; the code is deterministic, so it ends up
suspended where it was. A digest that differs means the code changed: every running flow is
then restarted from its latest ``ctx.checkpoint`` snapshot with the new code, and the journal
starts over from there.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from vs_core._ids import IdScheme
from vs_core._registry import ContractError, OperationRegistration, OperationRegistry
from vs_core.types.common import OperationSchemaRef, StrategyId
from vs_core.types.strategy import SearchDeclaration

from ._defs import FlowDef
from ._errors import FlowCodeChanged
from ._resource import kit_of
from ._roles import Role, with_models
from ._run import Wiring, _Run
from ._state import FlowState, Step, Waiting, digest, key, recorded
from ._winner import NoWinner, conclude

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from pydantic import BaseModel

    from vs_core._strategy_operations import StrategyOperations
    from vs_core.types.common import RunFacts, SchemaRef
    from vs_core.types.search import Event, RunView
    from vs_core.types.strategy import Decision

    from ._flow import Flow
    from ._state import LiveFlows

type Root[C] = Callable[[Flow, C], Coroutine[Any, Any, NoWinner | None]]

_CAPABILITIES = frozenset({"park", "interrupt", "steer", "suspend", "profile-capture"})


@dataclass(frozen=True)
class SearchDef[C]:
    """A registered search: its name, config model, root flow and what it shares with core."""

    name: str
    config: type[C]
    root: Root[C]
    operations: StrategyOperations | None = None
    roles: tuple[Role, ...] = ()
    """The agents the search asks: the only ones ``ctx.agents.session`` accepts."""
    owned: tuple[OperationRegistration, ...] = ()
    """Operations other libraries' owners perform for the search (``ctx.owners.perform``)."""

    @property
    def ids(self) -> IdScheme:
        """The identities the search's decisions, sessions and roles are named with."""
        return IdScheme(self.name)

    @property
    def reply_schemas(self) -> Mapping[SchemaRef, type[BaseModel]]:
        """The model every declared role's turn answers with, by the schema it is asked for."""
        return {role.reply_schema: role.wire_reply for role in self.roles}

    def with_models(self, models: Mapping[str, str]) -> SearchDef[C]:
        """The same search with the models an operator chose per role (a launch-time setting)."""
        return replace(self, roles=with_models(self.roles, models))

    def registry(self) -> OperationRegistry | None:
        """The codec of every operation the search names, or None when it names none."""
        if self.operations is None:
            return None
        return OperationRegistry((*self.operations.registrations(), *self.owned))

    @property
    def declaration(self) -> SearchDeclaration:
        """What the host must provide for this search, derived from the registration."""
        required, optional = (), ()
        ops = self.operations
        if ops is not None:
            kinds = ("render_role_artifacts", "verify_parent_revision", "interpret_evidence")
            required = tuple(ops.schema_ref(f"{ops.namespace}.{k}") for k in kinds)
            registered = {r.descriptor.kind for r in ops.registrations()}
            optional = tuple(
                ops.schema_ref(kind)
                for kind in sorted(registered - {f"{ops.namespace}.{k}" for k in kinds})
            )
        required = (*required, *(_schema_ref(item) for item in self.owned))
        return SearchDeclaration(
            strategy_id=StrategyId(root=self.name),
            required_operations=required,
            optional_operations=optional,
            optional=frozenset(_CAPABILITIES),  # ty: ignore[invalid-argument-type]
        )

    @property
    def state_model(self) -> type[FlowState]:
        """The class of the persisted state."""
        return FlowState

    def driver(self, config: C, registry: OperationRegistry | None = None) -> FlowDriver[C]:
        """The driver that runs this search on ``config``."""
        return FlowDriver(self, config, registry)


def search[C](
    name: str,
    *,
    config: type[C],
    operations: StrategyOperations | None = None,
    roles: tuple[Role, ...] = (),
    owned: tuple[OperationRegistration, ...] = (),
) -> Callable[[Root[C]], SearchDef[C]]:
    """Register ``root`` as the search ``name``, run on a ``config`` model.

    ``roles`` are the agents the search asks; everything core needs to ask them is derived
    from the declarations. ``operations`` are the namespace's shapes for what is not a role:
    the verification, evidence and retention of workspaces and measurements, and the context
    types of the prompts. ``owned`` are the operations another library's owner performs
    for the search (an issue board, say): their codec is registered, the host must provide an
    owner for each, and a flow asks one with ``ctx.owners.perform``.
    """
    names = [role.name for role in roles]
    if len(set(names)) != len(names):
        message = f"roles of search {name!r} must have distinct names: {sorted(names)}"
        raise ContractError(("roles",), message)

    def register(root: Root[C]) -> SearchDef[C]:
        return SearchDef(name, config, root, operations, roles, owned)

    return register


class FlowDriver[C]:
    """Runs one search's flows: ``init`` once, then ``update`` per event.

    ``update`` keeps the live runtime of the newest state it produced. Given any other state
    (after a restart, or a test branching from an older one) it rebuilds the runtime by
    replaying the journal.
    """

    def __init__(
        self, definition: SearchDef[C], config: C, registry: OperationRegistry | None
    ) -> None:
        """Build the driver; ``registry`` decodes the operation results of the journal."""
        self.definition = definition
        self.declaration = definition.declaration
        self._registry = registry
        self._config = config
        self._wiring = Wiring(
            registry,
            definition.operations,
            definition.ids,
            definition.owned,
            {role.name: role for role in definition.roles},
        )
        root = definition.root

        async def supervised(ctx: Flow, config: C) -> None:
            await conclude(kit_of(ctx), await root(ctx, config))

        self._root = FlowDef("root", supervised, type(config))
        self._cached: tuple[tuple[int, str], _Run] | None = None

    def close(self) -> None:
        """Retire the live runtime (when the driver is dropped, or replaced by a fresh one)."""
        if self._cached is not None:
            self._cached[1].close()
            self._cached = None

    def __del__(self) -> None:
        """Retire the live runtime with the driver."""
        self.close()

    def fresh(self) -> FlowDriver[C]:
        """A driver with the same search and config and no live runtime, as a restart builds."""
        return FlowDriver(self.definition, self._config, self._registry)

    @property
    def state_model(self) -> type[FlowState]:
        """The class of the persisted state."""
        return FlowState

    def init(self, config: C, facts: RunFacts, view: RunView) -> tuple[FlowState, list[Decision]]:
        """Start the root flow; the journal begins with the step that started it."""
        if config != self._config:
            raise ContractError(("config",), "init must get the config the driver was built with")
        run = self._new_run(facts)
        decisions = run.start(view)
        return self._first_state(run, facts, None, decisions), decisions

    def update(
        self, state: FlowState, event: Event, view: RunView
    ) -> tuple[FlowState, list[Decision]]:
        """Deliver ``event`` to the flows it unblocks and return what they decided."""
        restarted: list[Decision] = []
        try:
            run = self._runtime(state)
        except FlowCodeChanged:
            state, run, restarted = self._restart(state, view)
        self._cached = None  # a failed update leaves the runtime half-way: never reuse it
        journaled, kept = recorded(event, self._registry)
        item = journaled or kept
        if item is None:
            raise ContractError(("event",), "an event is journaled as itself or as a record")
        decisions = run.feed(item, view, None)
        step = Step(
            event=journaled,
            recorded=kept,
            reads=run.reads,
            digest=digest(state.journal[-1].digest, item, run.reads, decisions),
        )
        new = state.model_copy(
            update={
                "journal": (*state.journal, step),
                "live": run.checkpoint(),
                "dropped": tuple(run.dropped),
            }
        )
        self._cached = (key(new), run)
        return new, [*restarted, *decisions]

    @staticmethod
    def published[M: BaseModel](state: FlowState, model: type[M]) -> M | None:
        """The record the flows last published (``ctx.publish``), or None if none yet."""
        text = state.live.published
        return None if text is None else model.model_validate_json(text)

    def waiting(self, state: FlowState) -> tuple[Waiting, ...]:
        """What each suspended flow waits for in ``state``: its sources, as data."""
        return tuple(
            Waiting(flow=path, sources=sources) for path, sources in self._runtime(state).waiting()
        )

    # -- replay and restart ------------------------------------------------------------------

    def _new_run(self, facts: RunFacts) -> _Run:
        return _Run(self._root, self._config, facts, self._wiring)

    def _first_state(
        self,
        run: _Run,
        facts: RunFacts,
        base: LiveFlows | None,
        decisions: list[Decision],
    ) -> FlowState:
        """The state whose journal has just its first step: the start of the flows."""
        first = Step(reads=run.reads, digest=digest("", None, run.reads, decisions))
        state = FlowState(
            schema_version=1,
            facts=facts,
            journal=(first,),
            live=run.checkpoint(),
            base=base,
        )
        self._cached = (key(state), run)
        return state

    def _runtime(self, state: FlowState) -> _Run:
        """The live runtime for ``state``: the cached one, or one rebuilt by replay."""
        if self._cached is not None and self._cached[0] == key(state):
            return self._cached[1]
        self.close()
        run = self._new_run(state.facts)
        before = ""
        for index, step in enumerate(state.journal):
            item = step.event or step.recorded
            try:
                if item is not None:
                    decisions = run.feed(item, None, step.reads)
                elif state.base is None:
                    decisions = run.start(None, step.reads)
                else:
                    decisions = run.restore(state.base, None, step.reads)
            except FlowCodeChanged as changed:
                raise FlowCodeChanged(index) from changed
            if digest(before, item, step.reads, decisions) != step.digest:
                raise FlowCodeChanged(index)
            before = step.digest
        return run

    def _restart(self, state: FlowState, view: RunView) -> tuple[FlowState, _Run, list[Decision]]:
        """Start every running flow again from its latest snapshot, with a fresh journal.

        A task that is not a registered flow has no snapshot to start from; ``restore`` drops it
        and leaves it to its parent, which restarts from its own snapshot, to start again.
        """
        run = self._new_run(state.facts)
        decisions = run.restore(state.live, view, None)
        restarted = self._first_state(run, state.facts, state.live, decisions)
        return restarted, run, decisions


def _schema_ref(registration: OperationRegistration) -> OperationSchemaRef:
    descriptor = registration.descriptor
    return OperationSchemaRef(
        kind=descriptor.kind,
        request_schema=descriptor.request_schema,
        outcome_schema=descriptor.outcome_schema,
        lifecycle=descriptor.lifecycle,
    )
