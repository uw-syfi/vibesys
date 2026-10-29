"""What the evolve search knows of its run, and the plain decisions that follow from it.

`EvolveRecord` is the root flow's one piece of mutable state. Children report back through
`admit`, which folds them into the population; nothing else writes it. It answers the questions
the root asks of it: which round comes next, who leads, why a run has no winner. It reads nothing
from the run and awaits nothing, so every rule here is a plain function of its record. It is
rebuilt by replay after a restart, so it is never saved.
"""

from dataclasses import dataclass, field

from vibesys.candidates.api import ending_reason, space_of
from vibesys.hypothesis import RunEnding
from vibesys.orchestration.evolve.population import (
    Individual,
    PopulationSearch,
    PopulationState,
)
from vibesys.orchestration.evolve.search import _population as policy
from vibesys.orchestration.evolve.search._config import EvolveConfig
from vibesys.orchestration.evolve.search._context import child_prompt, cold_start_prompt
from vibesys.orchestration.evolve.search._round import Round
from vibesys.orchestration.evolve.search._values import ChildDone, ChildStart, Stage
from vibesys.search_spans.api import adoption_label
from vs_core.api.search import (
    INPUT,
    Input,
    Kept,
    Label,
    NoWinner,
    RevisionRef,
    SpanId,
    TaskTerms,
    Value,
)

NOTE_LIMIT = 20
"""Advisory notes the record keeps for an operator to read."""


class EvolveSnapshot(Value):
    """What the search publishes of its record, for the projector and for tests."""

    population: PopulationState
    stage: Stage
    generation_open: bool = False
    """Whether a generation's children are still running."""
    notes: tuple[str, ...] = ()


@dataclass
class EvolveRecord:
    """The population and the bookkeeping around it for one evolve run."""

    config: EvolveConfig
    base: RevisionRef
    """The revision the run started from."""
    search: PopulationSearch
    population: PopulationState
    stage: Stage = Stage.BOOTSTRAP
    bootstrap_spent: int = 0
    """Cold-start attempts that have ended, with or without an individual."""
    revisions: dict[int, RevisionRef] = field(default_factory=dict)
    """The revision core retained for an individual, by id."""
    kept: dict[int, Kept] = field(default_factory=dict)
    """The winner-eligible candidates closed workspaces retained, by individual id."""
    notes: tuple[str, ...] = ()
    generation_open: bool = False
    rounds_opened: int = 0
    """Rounds opened so far: the next round's number is one more."""
    built: dict[str, SpanId] = field(default_factory=dict)
    """The workstream that kept each retained revision, by revision id."""

    @classmethod
    def begin(cls, config: EvolveConfig, base: RevisionRef, terms: TaskTerms) -> "EvolveRecord":
        """The record of a run that has done nothing yet."""
        search = policy.search_of(config, space_of(terms))
        return cls(config=config, base=base, search=search, population=search.initial())

    # -- rounds ----------------------------------------------------------------------------------

    def open_round(self, *, stopping: bool) -> Round | None:
        """The next cold start or generation, or None when the search is over.

        A generation freezes the population as it stands: every proposal is chosen from it, so the
        children of one generation never see each other.
        """
        if stopping:
            return None
        number = self.rounds_opened + 1
        if self.stage is Stage.BOOTSTRAP:
            if self.bootstrap_spent >= self.config.bootstrap_max_attempts:
                return None
            self.rounds_opened = number
            return self._cold_start(number)
        if self.population.generation >= self.config.max_generations:
            return None
        self.rounds_opened = number
        return self._generation(number)

    def _cold_start(self, round_number: int) -> Round:
        """The next cold-start attempt, repairing the last failure's work in progress if it left some."""
        seed = self.search.wip_seed(self.population)
        seeded = None if seed is None else self.revision_of(seed)
        number = self.bootstrap_spent + 1
        start = ChildStart(
            key=f"bootstrap.{number}",
            slot=number,
            generation=0,
            round=round_number,
            base=seeded or self.base,
            prompt=cold_start_prompt(
                self.search, self.population, seeded or self.base, repair=seeded is not None
            ),
            config=self.config,
        )
        return Round(starts=(start,), width=1, generation_open=False)

    def _generation(self, round_number: int) -> Round:
        started = self.search.end_generation(self.population)
        self.population = started
        chosen = policy.proposals(self.search, started, self.config.children_per_generation)
        starts: list[ChildStart] = []
        for index, proposal in enumerate(chosen, start=1):
            parent = None if proposal is None else policy.individual(started, proposal.parent_id)
            base = None if parent is None else self.revision_of(parent)
            if proposal is None or base is None:
                continue  # nothing to build on: the slot earns no individual
            prompt = child_prompt(started, base, proposal)
            if prompt is None:
                continue
            starts.append(
                ChildStart(
                    key=f"g{started.generation}.c{index}",
                    slot=index,
                    generation=started.generation,
                    round=round_number,
                    proposal=proposal,
                    base=base,
                    prompt=prompt,
                    config=self.config,
                )
            )
        self.generation_open = True
        return Round(starts=tuple(starts), width=self.config.max_parallelism, generation_open=True)

    def revision_of(self, member: Individual) -> RevisionRef | None:
        """The revision core retained for an individual, else the one its commit names."""
        found = self.revisions.get(member.id)
        if found is not None:
            return found
        return RevisionRef.of_git_commit(member.commit) if member.commit else None

    def close_round(self, round_: Round) -> None:
        """Mark a finished round: a generation is over once all its children are accounted for."""
        if round_.generation_open:
            self.generation_open = False

    # -- admission -------------------------------------------------------------------------------

    def admit(self, done: ChildDone, span: SpanId | None = None) -> None:
        """Admit one child into the population, recording the settlement it rests on.

        ``span`` is the child's own workstream, which later children that build on what it
        kept link to.
        """
        outcome = policy.outcome_of(done)
        if outcome is not None:
            member, self.population = self.search.admit(self.population, outcome)
            settlement = done.settlement
            if settlement is not None and settlement.candidate is not None and member.commit:
                self.revisions[member.id] = settlement.candidate
                retained = Kept.of(settlement)
                if retained is not None and member.passed:
                    self.kept[member.id] = retained
        kept = None if done.settlement is None else done.settlement.candidate
        if kept is not None and span is not None:
            self.built[kept.revision_id.root] = span
        if done.start.cold_start:
            self._after_cold_start()
        if done.closing.notes:
            self.notes = (*self.notes, *done.closing.notes)[-NOTE_LIMIT:]

    def _after_cold_start(self) -> None:
        """Count the finished cold-start attempt; a passing one starts the generations."""
        self.bootstrap_spent += 1
        if not self.search.needs_bootstrap(self.population):
            self.stage = Stage.GENERATION

    def built_by(self, revision: RevisionRef) -> SpanId | None:
        """The workstream that kept ``revision``; None for the input."""
        return self.built.get(revision.revision_id.root)

    # -- the winner ------------------------------------------------------------------------------

    def leader(self) -> Kept | Input:
        """The candidate that leads now, else the input: what the run would adopt if it ended."""
        return self.leading()[0]

    def leading(self) -> tuple[Kept | Input, Label]:
        """The leader, with the label of the span that adopts it (``ctx.choose``'s arguments).

        Evolve never measures the input, so the label says what the leader measured and nothing
        of how it compares.
        """
        best = self.search.best(self.population)
        found = None if best is None else self.kept.get(best.id)
        if best is None or found is None:
            return INPUT, adoption_label(INPUT, None, None)
        return found, adoption_label(found, best.perf_metric, None)

    def ending(self) -> NoWinner | None:
        """None when the run ends with a winner; else why it has none."""
        if not isinstance(self.leader(), Input):
            return None
        return NoWinner(reason=self._reason())

    def _reason(self) -> str:
        if self.search.best(self.population) is not None:
            return "the best individual has no retained settlement to adopt"
        return ending_reason(
            RunEnding.NO_TRUSTED_RESULT,
            "no cold start passed review and the configured gates in "
            f"{self.config.bootstrap_max_attempts} attempts",
        )

    # -- the record ------------------------------------------------------------------------------

    def snapshot(self) -> EvolveSnapshot:
        """The record as it is published."""
        return EvolveSnapshot(
            population=self.population,
            stage=self.stage,
            generation_open=self.generation_open,
            notes=self.notes,
        )
