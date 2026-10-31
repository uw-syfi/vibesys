"""An in-memory daemon of step containers, for tests of the code that removes them by tag."""

from __future__ import annotations

from vs_sandbox.step_containers import StepContainer, StepContainerError


class FakeStepContainers:
    """The step containers of every run on one daemon; a view of it is one run's ``StepContainers``."""

    def __init__(self) -> None:
        """Start with an empty, reachable daemon."""
        self._held: dict[str, tuple[str, str]] = {}
        self._next = 0
        self.unreachable = False
        self.removal_fails_silently = False
        """A removal answers success and the container stays: the daemon lost the request."""

    def open(self, run_id: str, tag: str) -> StepContainer:
        """A container of *run_id* opened under the step mark *tag*."""
        self._next += 1
        identifier = f"c{self._next}"
        self._held[identifier] = (run_id, tag)
        return StepContainer(id=identifier, tag=tag)

    def count(self, run_id: str, matches: str) -> int:
        """How many containers of *run_id* carry the mark *matches*."""
        return sum(1 for run, tag in self._held.values() if run == run_id and tag == matches)

    def of_run(self, run_id: str) -> FakeRunStepContainers:
        """The daemon as run *run_id* sees it."""
        return FakeRunStepContainers(self, run_id)

    def listed_for(self, run_id: str) -> tuple[StepContainer, ...]:
        """The containers of *run_id*."""
        self._reachable()
        return tuple(
            StepContainer(id=identifier, tag=tag)
            for identifier, (run, tag) in self._held.items()
            if run == run_id
        )

    def remove(self, container: StepContainer) -> None:
        """Force-remove *container*, unless the daemon is unreachable or loses the request."""
        self._reachable()
        if not self.removal_fails_silently:
            self._held.pop(container.id, None)

    def _reachable(self) -> None:
        if self.unreachable:
            message = "the daemon cannot be reached"
            raise StepContainerError(message)


class FakeRunStepContainers:
    """One run's view of a :class:`FakeStepContainers` daemon."""

    def __init__(self, daemon: FakeStepContainers, run_id: str) -> None:
        """View *daemon* as run *run_id*."""
        self._daemon = daemon
        self._run_id = run_id

    def listed(self) -> tuple[StepContainer, ...]:
        """The run's step containers."""
        return self._daemon.listed_for(self._run_id)

    def remove(self, container: StepContainer) -> None:
        """Remove *container* unless the daemon loses the request."""
        self._daemon.remove(container)
