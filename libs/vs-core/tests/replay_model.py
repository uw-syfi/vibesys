"""Deliberately linear reference model for small generated causal histories."""

from vs_core.api.replay import EMPTY_CAUSALITY, AcceptedReplay, chain_dependency
from vs_proto.api import replay


def digest(observations: list[bytes]) -> bytes:
    result = EMPTY_CAUSALITY
    for observation in observations:
        result = chain_dependency(result, observation)
    return result


class LinearCausality:
    def __init__(self, branches: int) -> None:
        self.children: list[list[bytes]] = [[] for _ in range(branches)]
        self.root: list[bytes] = []
        self.seed = b""
        self.planned: set[str] = set()

    def check(self, start: replay.OperationReplay) -> None:
        assert start.key is not None
        name = start.key.operation
        if name.startswith("branch."):
            branch = int(name.split(".")[1])
            assert start.dependencies == digest([self.seed, *self.children[branch]])
        elif name.startswith("planning."):
            self._planning(name)
            assert start.dependencies == digest(self.root)
        elif name == "final":
            assert start.dependencies == digest(self.root)

    def _planning(self, name: str) -> None:
        if name in self.planned:
            return
        _, branch, sample = name.split(".")
        for observation in [self.seed, *self.children[int(branch)][: int(sample) + 1]]:
            if observation not in self.root:
                self.root.append(observation)
        self.planned.add(name)

    def accept(self, observation: AcceptedReplay) -> None:
        name = observation.key.operation
        if name == "seed":
            self.seed = observation.token
            self.root.append(self.seed)
        elif name.startswith("branch."):
            branch = int(name.split(".")[1])
            self.children[branch].append(observation.token)
        else:
            self.root.append(observation.token)
